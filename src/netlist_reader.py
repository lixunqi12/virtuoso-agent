"""HSpice netlist + testbench reader (T8.2 — feeds LLM closed-loop).

Parses a Virtuoso-exported HSpice ``.sp`` netlist and an optional
testbench file into a structured form, then renders an LLM-friendly
Markdown view that mirrors the shape of
``CircuitAgent._format_topology`` (per-instance cell + connections +
params). All foundry-leaking tokens are scrubbed via
:func:`hspice_scrub.scrub_sp` BEFORE parsing, so the parser itself is
PDK-agnostic and the rendered output is safe to ship to an LLM or
attach to a PR description.

Scope (MVP):
- Recognises ``.subckt`` / ``.ends`` blocks, the trailing toplevel
  flat block ending in ``.END``, and ``** Library/Cell/View name:``
  comment metadata Virtuoso emits.
- For each instance line, splits into ``(refdes, nets, cell, params)``
  using HSpice convention: tokens after the refdes that contain ``=``
  are params, tokens before the first ``=`` are nets except the LAST
  net-shaped token, which is the cell reference.
- For testbench: extracts ``.option`` / ``.temp`` / ``.include`` /
  ``.lib`` / ``.param`` / ``.tran`` / ``.measure`` / ``.alter`` and
  enumerates voltage sources.

Out-of-scope (deliberately):
- No ``.include`` recursion. The caller passes both files explicitly.
- No transistor sizing analysis or DC op-point inference.
- No syntax validation — HSpice itself is the canonical validator.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.hspice_scrub import load_patterns, scrub_sp

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "Instance",
    "Subcircuit",
    "ParsedNetlist",
    "ParsedTestbench",
    "parse_netlist",
    "parse_testbench",
    "render_netlist_markdown",
    "render_testbench_markdown",
    "read_and_render",
]


_LIBRARY_RE = re.compile(r"^\*\*\s*Library name:\s*(\S+)", re.IGNORECASE)
_CELL_RE = re.compile(r"^\*\*\s*Cell name:\s*(\S+)", re.IGNORECASE)
_VIEW_RE = re.compile(r"^\*\*\s*View name:\s*(\S+)", re.IGNORECASE)
_DESIGN_LIB_RE = re.compile(r"^\*\*\s*Design library name:\s*(\S+)", re.IGNORECASE)
_DESIGN_CELL_RE = re.compile(r"^\*\*\s*Design cell name:\s*(\S+)", re.IGNORECASE)
_DESIGN_VIEW_RE = re.compile(r"^\*\*\s*Design view name:\s*(\S+)", re.IGNORECASE)


@dataclass
class Instance:
    refdes: str
    cell: str
    nets: list[str]
    params: dict[str, str]
    value: str = ""


@dataclass
class Subcircuit:
    name: str
    library: str
    ports: list[str]
    instances: list[Instance] = field(default_factory=list)


@dataclass
class ParsedNetlist:
    header: dict[str, str]
    subcircuits: list[Subcircuit]
    toplevel: Subcircuit | None
    raw_line_count: int


@dataclass
class ParsedTestbench:
    options: str
    temp_C: str | None
    includes: list[str]
    libs: list[dict[str, str]]
    params_baseline: dict[str, str]
    sources: list[dict[str, str]]
    tran: dict[str, str] | None
    measures: list[dict[str, str]]
    alters: list[dict[str, Any]]
    raw_line_count: int


def _join_continuations(text: str) -> list[tuple[int, str]]:
    """Collapse HSpice line-continuation (``+`` prefix) into single lines.

    Returns ``[(original_lineno, joined_line), ...]``. Comment lines
    (``*`` / ``**``) are kept on their own logical line so callers can
    use them for metadata extraction. A blank or ``*`` line that
    appears WHILE a ``+`` continuation block is still open is
    quarantined into a pending list — if the next non-blank line is
    another ``+``, the interleaved comment/blank is dropped (real
    HSpice tools tolerate it inside a continued statement); otherwise
    the continuation closes and the pending lines are flushed at the
    boundary so standalone metadata after a ``.param`` block survives.
    """
    out: list[tuple[int, str]] = []
    cur_line: str | None = None
    cur_lineno: int = 0
    pending: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped:
            if cur_line is not None:
                pending.append((lineno, ""))
                continue
            out.append((lineno, ""))
            continue
        if stripped.startswith("*"):
            if cur_line is not None:
                pending.append((lineno, stripped))
                continue
            out.append((lineno, stripped))
            continue
        if stripped.startswith("+"):
            cont = stripped[1:].strip()
            if cur_line is not None:
                cur_line = cur_line + " " + cont
                pending.clear()
            else:
                cur_line = cont
                cur_lineno = lineno
            continue
        if cur_line is not None:
            out.append((cur_lineno, cur_line))
            cur_line = None
            out.extend(pending)
            pending.clear()
        cur_line = stripped
        cur_lineno = lineno
    if cur_line is not None:
        out.append((cur_lineno, cur_line))
    out.extend(pending)
    return out


_TOKEN_RE = re.compile(r"(?:[^\s'\"]+|'[^']*'|\"[^\"]*\")+")


def _tokenize_quoted(line: str) -> list[str]:
    """Whitespace-tokenize while keeping single/double-quoted runs intact.

    HSpice behavioral expressions like ``Q='V(a) * 1p'`` carry spaces
    inside quotes that belong to the expression, not the tokenizer.
    Plain ``str.split`` shreds them. This helper concatenates runs of
    (non-quote-non-space chars) and (quoted strings) into single tokens
    with quotes preserved, so round-trip rendering keeps the source
    form (``Q='V(a) * 1p'`` stays one param value).
    """
    return _TOKEN_RE.findall(line)


def _split_instance(line: str) -> tuple[str, str, list[str], dict[str, str]]:
    """Split ``xname net1 net2 ... cell p1=v1 p2=v2`` into structured form.

    HSpice rule: the first token is the refdes; the LAST token before
    the first ``=`` is the cell reference; everything between is nets.
    Tokens after the cell that contain ``=`` are key=value params.
    """
    tokens = _tokenize_quoted(line)
    if not tokens:
        return "", "", [], {}
    refdes = tokens[0]
    rest = tokens[1:]
    eq_idx = next((i for i, t in enumerate(rest) if "=" in t), len(rest))
    head = rest[:eq_idx]
    tail = rest[eq_idx:]
    if not head:
        return refdes, "", [], _split_params(tail)
    cell = head[-1]
    nets = head[:-1]
    return refdes, cell, nets, _split_params(tail)


_PASSIVE_KIND = {"r": "resistor", "c": "capacitor", "l": "inductor"}
_SOURCE_KIND = {"v": "source-V", "i": "source-I"}


def _split_passive(
    line: str, prefix: str,
) -> tuple[str, str, list[str], str, dict[str, str]]:
    """``R1 a b 1k tc1=0.001`` → (refdes, kind, nets, value, params).

    Convention: 2 nets, then optionally a value token (any token that
    does NOT contain ``=``), then ``key=value`` params. ``kind`` is the
    passive type derived from the refdes prefix. Uses the quote-aware
    tokenizer so behavioral expressions like ``Q='V(a) * 1p'`` survive.
    """
    tokens = _tokenize_quoted(line)
    kind = _PASSIVE_KIND[prefix]
    if not tokens:
        return "", kind, [], "", {}
    refdes = tokens[0]
    rest = tokens[1:]
    nets = rest[:2]
    after = rest[2:]
    if not after:
        return refdes, kind, nets, "", {}
    if "=" in after[0]:
        return refdes, kind, nets, "", _split_params(after)
    value = after[0]
    return refdes, kind, nets, value, _split_params(after[1:])


def _split_source(
    line: str, prefix: str,
) -> tuple[str, str, list[str], str]:
    """``V1 n+ n- 0.9V PWL (...)`` → (refdes, kind, [n+, n-], value).

    The value field collapses everything after the second net into a
    single string so PWL/SIN/PULSE waveforms render verbatim. Matches
    the precedent set by :func:`parse_testbench`'s top-level V-source
    handling.
    """
    tokens = line.split(None, 3)
    kind = _SOURCE_KIND[prefix]
    if not tokens:
        return "", kind, [], ""
    if len(tokens) < 4:
        return tokens[0], kind, tokens[1:3], ""
    return tokens[0], kind, [tokens[1], tokens[2]], tokens[3]


def _split_params(tokens: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for tok in tokens:
        if "=" not in tok:
            continue
        k, _, v = tok.partition("=")
        if k:
            params[k] = v
    return params


def _parse_subckt_header(line: str) -> tuple[str, list[str]]:
    """Parse ``.subckt NAME port1 port2 ...`` → (name, [ports])."""
    parts = line.split()
    if len(parts) < 2:
        return "", []
    return parts[1], parts[2:]


def _parse_param_block(rhs: str) -> dict[str, str]:
    """Parse a ``.param`` body like ``delay = 50p PROSIGN = 0V`` into dict.

    HSpice tolerates ``a=b``, ``a = b``, ``a= b``, ``a =b`` — normalise
    by inserting spaces around ``=`` then re-tokenising into ``[k, =, v,
    k, =, v, ...]``.
    """
    norm = re.sub(r"\s*=\s*", " = ", rhs)
    tokens = norm.split()
    out: dict[str, str] = {}
    i = 0
    while i + 2 < len(tokens):
        k = tokens[i]
        if tokens[i + 1] != "=":
            i += 1
            continue
        v = tokens[i + 2]
        out[k] = v
        i += 3
    return out


def _parse_measure(line: str) -> dict[str, str]:
    """``.measure tran NAME trig ... targ ...`` → {name, mode, directive}."""
    parts = line.split(None, 3)
    if len(parts) < 4:
        return {"name": "", "mode": "", "directive": line}
    return {"name": parts[2], "mode": parts[1], "directive": parts[3]}


def _parse_tran(line: str) -> dict[str, str]:
    """``.tran step stop [sweep var lo hi step]`` → loose dict."""
    out: dict[str, str] = {"raw": line}
    parts = line.split()
    if len(parts) >= 3:
        out["step"] = parts[1]
        out["stop"] = parts[2]
    if "sweep" in [p.lower() for p in parts]:
        sweep_idx = [i for i, p in enumerate(parts) if p.lower() == "sweep"][0]
        rest = parts[sweep_idx + 1:]
        if len(rest) >= 4:
            out["sweep_var"] = rest[0]
            out["sweep_lo"] = rest[1]
            out["sweep_hi"] = rest[2]
            out["sweep_step"] = rest[3]
    return out


def _parse_lib(line: str) -> dict[str, str]:
    """``.lib "PATH" SECTION`` → {path, section}. Path may be quoted."""
    m = re.match(r'^\.lib\s+(?:"([^"]*)"|(\S+))(?:\s+(\S+))?', line, re.IGNORECASE)
    if not m:
        return {"path": "", "section": ""}
    path = m.group(1) if m.group(1) is not None else (m.group(2) or "")
    section = m.group(3) or ""
    return {"path": path, "section": section}


def parse_netlist(scrubbed_text: str) -> ParsedNetlist:
    """Parse a SCRUBBED netlist .sp text into ParsedNetlist.

    Caller is responsible for running :func:`hspice_scrub.scrub_sp`
    first; this parser does no scrubbing of its own.
    """
    logical = _join_continuations(scrubbed_text)
    header = {"library": "", "cell": "", "view": ""}
    subckts: list[Subcircuit] = []
    cur_lib = ""
    cur_cell = ""
    cur_view = ""
    cur_subckt: Subcircuit | None = None
    toplevel_instances: list[Instance] = []
    in_toplevel = False

    for lineno, line in logical:
        if not line:
            continue

        m = _DESIGN_LIB_RE.match(line)
        if m:
            header["library"] = m.group(1)
            continue
        m = _DESIGN_CELL_RE.match(line)
        if m:
            header["cell"] = m.group(1)
            continue
        m = _DESIGN_VIEW_RE.match(line)
        if m:
            header["view"] = m.group(1)
            continue

        m = _LIBRARY_RE.match(line)
        if m:
            cur_lib = m.group(1)
            continue
        m = _CELL_RE.match(line)
        if m:
            cur_cell = m.group(1)
            continue
        m = _VIEW_RE.match(line)
        if m:
            cur_view = m.group(1)
            continue

        if line.startswith("*"):
            continue

        low = line.lower()
        if low.startswith(".subckt"):
            name, ports = _parse_subckt_header(line)
            cur_subckt = Subcircuit(
                name=name, library=cur_lib, ports=list(ports),
            )
            in_toplevel = False
            continue
        if low.startswith(".ends"):
            if cur_subckt is not None:
                subckts.append(cur_subckt)
                cur_subckt = None
            continue
        if low.startswith(".end"):
            break
        if low.startswith("."):
            continue

        # Instance line. Inside .subckt → goes to cur_subckt.instances;
        # outside → toplevel flat block.
        prefix = line[:1].lower()
        inst: Instance | None = None
        if prefix in ("x", "m", "d", "q"):
            refdes, cell, nets, params = _split_instance(line)
            inst = Instance(
                refdes=refdes, cell=cell, nets=nets, params=params,
            )
        elif prefix in ("r", "c", "l"):
            refdes, kind, nets, value, params = _split_passive(line, prefix)
            inst = Instance(
                refdes=refdes, cell=kind, nets=nets,
                params=params, value=value,
            )
        elif prefix in ("v", "i"):
            refdes, kind, nets, value = _split_source(line, prefix)
            inst = Instance(
                refdes=refdes, cell=kind, nets=nets,
                params={}, value=value,
            )
        else:
            _LOGGER.warning(
                "netlist_reader: skipping unrecognized line %d: %s",
                lineno, line[:60],
            )
            continue
        if cur_subckt is not None:
            cur_subckt.instances.append(inst)
        else:
            in_toplevel = True
            toplevel_instances.append(inst)

    toplevel: Subcircuit | None = None
    if toplevel_instances or in_toplevel:
        toplevel = Subcircuit(
            name=header.get("cell") or "TOPLEVEL",
            library=header.get("library", ""),
            ports=[],
            instances=toplevel_instances,
        )

    return ParsedNetlist(
        header=header,
        subcircuits=subckts,
        toplevel=toplevel,
        raw_line_count=len(scrubbed_text.splitlines()),
    )


def parse_testbench(scrubbed_text: str) -> ParsedTestbench:
    """Parse a SCRUBBED testbench .sp text into ParsedTestbench."""
    logical = _join_continuations(scrubbed_text)
    options = ""
    temp_C: str | None = None
    includes: list[str] = []
    libs: list[dict[str, str]] = []
    params_baseline: dict[str, str] = {}
    sources: list[dict[str, str]] = []
    tran: dict[str, str] | None = None
    measures: list[dict[str, str]] = []
    alters: list[dict[str, Any]] = []
    cur_alter: dict[str, Any] | None = None
    seen_first_param = False

    for lineno, line in logical:
        if not line:
            continue

        low = line.lower()

        # .alter is a comment-shaped marker; HSpice sees ``.alter`` then
        # everything after on the same logical line is the label.
        if low.startswith(".alter"):
            label = re.sub(r"^[*\s]+", "", line[len(".alter"):]).strip()
            cur_alter = {"label": label, "params": {}}
            alters.append(cur_alter)
            continue

        if line.startswith("*"):
            continue

        if low.startswith(".option"):
            options = line[len(".option"):].strip()
            continue
        if low.startswith(".temp"):
            parts = line.split()
            if len(parts) >= 2:
                temp_C = parts[1]
            continue
        if low.startswith(".include"):
            parts = line.split(None, 1)
            if len(parts) == 2:
                includes.append(parts[1].strip().strip('"'))
            continue
        if low.startswith(".lib"):
            libs.append(_parse_lib(line))
            continue
        if low.startswith(".param"):
            body = line[len(".param"):].strip()
            parsed = _parse_param_block(body)
            if cur_alter is not None:
                cur_alter["params"].update(parsed)
            else:
                params_baseline.update(parsed)
                seen_first_param = True
            continue
        if low.startswith(".tran"):
            tran = _parse_tran(line)
            continue
        if low.startswith(".measure") or low.startswith(".meas"):
            measures.append(_parse_measure(line))
            continue
        if low.startswith(".end"):
            break
        if low.startswith("."):
            continue

        # Voltage source line: V<name> n+ n- value
        if line[:1].lower() == "v":
            parts = line.split(None, 3)
            if len(parts) >= 4:
                sources.append({
                    "name": parts[0],
                    "node_pos": parts[1],
                    "node_neg": parts[2],
                    "value": parts[3],
                })
            continue

    return ParsedTestbench(
        options=options,
        temp_C=temp_C,
        includes=includes,
        libs=libs,
        params_baseline=params_baseline,
        sources=sources,
        tran=tran,
        measures=measures,
        alters=alters,
        raw_line_count=len(scrubbed_text.splitlines()),
    )


def _render_instance(inst: Instance) -> list[str]:
    cell_disp = inst.cell or "?"
    head = f"- **{inst.refdes}** ({cell_disp})"
    extras: list[str] = []
    if inst.value:
        extras.append(f"value={inst.value}")
    if inst.params:
        extras.append(", ".join(f"{k}={v}" for k, v in inst.params.items()))
    if extras:
        head = head + ": " + ", ".join(extras)
    lines = [head]
    if inst.nets:
        lines.append(f"  Connections: {', '.join(inst.nets)}")
    return lines


def _render_subcircuit(sub: Subcircuit, *, header_label: str) -> list[str]:
    lines: list[str] = []
    lib = f" (library: {sub.library})" if sub.library else ""
    lines.append(f"## {header_label}: {sub.name}{lib}")
    if sub.ports:
        lines.append(f"Ports: {', '.join(sub.ports)}")
    lines.append("")
    if not sub.instances:
        lines.append("(no instances)")
        return lines
    lines.append("### Instances")
    cell_tally: dict[str, int] = {}
    for inst in sub.instances:
        lines.extend(_render_instance(inst))
        cell_tally[inst.cell or "?"] = cell_tally.get(inst.cell or "?", 0) + 1
    lines.append("")
    tally = ", ".join(f"{n}× {c}" for c, n in sorted(cell_tally.items()))
    lines.append(f"_Tally: {tally}_")
    return lines


def render_netlist_markdown(parsed: ParsedNetlist, *, source_name: str) -> str:
    """Render a ParsedNetlist as LLM-friendly Markdown."""
    out: list[str] = []
    lib = parsed.header.get("library") or "(unknown)"
    cell = parsed.header.get("cell") or "(unknown)"
    out.append(f"# HSpice netlist: {lib} / {cell}")
    out.append(f"Source: {source_name} ({parsed.raw_line_count} lines, scrubbed)")
    out.append("")

    for sub in parsed.subcircuits:
        out.extend(_render_subcircuit(sub, header_label="Subcircuit"))
        out.append("")

    if parsed.toplevel is not None:
        out.extend(_render_subcircuit(parsed.toplevel, header_label="Toplevel"))
        out.append("")

    return "\n".join(out)


def render_testbench_markdown(
    parsed: ParsedTestbench, *, source_name: str,
) -> str:
    """Render a ParsedTestbench as LLM-friendly Markdown."""
    out: list[str] = []
    out.append(f"# HSpice testbench: {source_name}")
    out.append(f"Source: {parsed.raw_line_count} lines, scrubbed")
    out.append("")

    if parsed.options:
        out.append(f"## Options\n`{parsed.options}`")
        out.append("")
    if parsed.temp_C is not None:
        out.append(f"## Temperature\n{parsed.temp_C} °C")
        out.append("")

    if parsed.includes:
        out.append("## Includes")
        for inc in parsed.includes:
            out.append(f"- {inc}")
        out.append("")

    if parsed.libs:
        out.append("## Model libraries")
        for lib in parsed.libs:
            section = f" (section {lib.get('section')})" if lib.get("section") else ""
            out.append(f"- `{lib.get('path', '')}`{section}")
        out.append("")

    if parsed.params_baseline:
        out.append("## Baseline `.param`")
        out.append("| name | value |")
        out.append("|---|---|")
        for k, v in parsed.params_baseline.items():
            out.append(f"| `{k}` | `{v}` |")
        out.append("")

    if parsed.sources:
        out.append(f"## Voltage sources ({len(parsed.sources)})")
        for s in parsed.sources:
            val = s.get("value", "")
            if len(val) > 80:
                val = val[:77] + "..."
            out.append(
                f"- **{s.get('name')}**: "
                f"{s.get('node_pos')} → {s.get('node_neg')}, `{val}`"
            )
        out.append("")

    if parsed.tran is not None:
        t = parsed.tran
        head = f"step={t.get('step', '?')}, stop={t.get('stop', '?')}"
        if "sweep_var" in t:
            head += (
                f", sweep `{t['sweep_var']}` "
                f"from {t.get('sweep_lo')} to {t.get('sweep_hi')} "
                f"step {t.get('sweep_step')}"
            )
        out.append(f"## Transient\n{head}")
        out.append("")

    if parsed.measures:
        out.append(f"## `.measure` ({len(parsed.measures)})")
        for m in parsed.measures:
            out.append(
                f"- **{m.get('name')}** ({m.get('mode')}): "
                f"`{m.get('directive')}`"
            )
        out.append("")

    if parsed.alters:
        out.append(f"## `.alter` blocks ({len(parsed.alters)})")
        for a in parsed.alters:
            label = a.get("label") or "(unlabeled)"
            params = a.get("params") or {}
            param_str = (
                ", ".join(f"{k}={v}" for k, v in params.items())
                if params else "(no .param overrides)"
            )
            out.append(f"- `{label}` — {param_str}")
        out.append("")

    return "\n".join(out)


def read_and_render(
    netlist_path: str | Path,
    testbench_path: str | Path | None = None,
    *,
    patterns_path: str | Path | None = None,
) -> str:
    """End-to-end: read both files, scrub, parse, render combined Markdown."""
    netlist_p = Path(netlist_path)
    raw_netlist = netlist_p.read_text(encoding="utf-8")
    patterns = load_patterns(patterns_path) if patterns_path else load_patterns()
    scrubbed_netlist = scrub_sp(raw_netlist, patterns)
    parsed_net = parse_netlist(scrubbed_netlist)
    md = render_netlist_markdown(parsed_net, source_name=netlist_p.name)

    if testbench_path is not None:
        tb_p = Path(testbench_path)
        raw_tb = tb_p.read_text(encoding="utf-8")
        scrubbed_tb = scrub_sp(raw_tb, patterns)
        parsed_tb = parse_testbench(scrubbed_tb)
        md += "\n---\n\n"
        md += render_testbench_markdown(parsed_tb, source_name=tb_p.name)

    return md
