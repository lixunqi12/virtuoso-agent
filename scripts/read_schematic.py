#!/usr/bin/env python3
"""Read-only schematic extractor CLI.

Pulls a single (lib, cell) schematic out of Virtuoso through SafeBridge
(PDK-sanitized) and emits it in an LLM-friendly format. Never calls
set_params / simulate / Maestro writeback.

Default Markdown output is byte-for-byte identical to the topology
section CircuitAgent feeds the LLM — it is exactly
``CircuitAgent._format_topology(circuit)`` with no additions. Pass
``--with-summary`` to append a human-oriented ``### Summary`` tally
(NOT sent to the agent).

Usage (Virtuoso schematic mode):
    python scripts/read_schematic.py --lib pllLib --cell LC_VCO
    python scripts/read_schematic.py --lib pllLib --cell LC_VCO \\
        --format both --output ./out/lc_vco
    python scripts/read_schematic.py --lib pllLib --cell LC_VCO \\
        --remote-skill-dir /project/.../skill

Usage (HSpice netlist mode — T8.2):
    python scripts/read_schematic.py --netlist path/to/netlist.sp
    python scripts/read_schematic.py --netlist netlist.sp \\
        --testbench sch_test.sp --output ./out/dut.md

Netlist mode runs the file(s) through ``src/hspice_scrub.scrub_sp``
before parsing, then renders a per-subcircuit Markdown view that mirrors
the schematic-mode topology format. ``--lib`` / ``--cell`` are ignored
in netlist mode; the design library / cell come from the netlist's
``** Design library/cell name:`` header comments.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from virtuoso_bridge import VirtuosoClient

from src.agent import CircuitAgent
from src.netlist_reader import read_and_render
from src.project import resolve_project
from src.safe_bridge import SafeBridge

# Must match the SKILL-side hard cap in safeReadSchematicDeep. Keep in
# sync with skill/safe_read_schematic.il (clamped to [1, 50]).
_DEPTH_AUTO_MAX = 50


def _parse_depth(raw: str) -> int:
    """argparse type for --depth: accepts a positive int or 'auto'/'all'.

    'auto' and 'all' both expand to the SKILL-side hard cap so BFS runs
    until the tree is exhausted. Traversal terminates when the queue
    empties, so on a 2-level design 'auto' costs the same as --depth 2.
    """
    if isinstance(raw, str) and raw.lower() in ("auto", "all"):
        return _DEPTH_AUTO_MAX
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"--depth must be a positive int or 'auto' (got {raw!r})"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only schematic extractor. Emits a sanitized, "
            "LLM-friendly dump of a single (lib, cell)."
        )
    )
    # --lib/--cell are required for the Virtuoso path but unused in
    # netlist mode. We validate the combination in main() so we can give
    # a precise error message instead of relying on argparse's terse
    # ``required`` failure.
    parser.add_argument("--lib", default=None, help="Virtuoso library name (schematic mode)")
    parser.add_argument("--cell", default=None, help="Cell name to read (schematic mode)")
    parser.add_argument(
        "--netlist",
        default=None,
        help=(
            "Path to a HSpice .sp netlist (T8.2 — HSpice mode). When "
            "given, --lib/--cell are ignored and the file is parsed + "
            "scrubbed locally instead of routing through SafeBridge. "
            "Output is always Markdown."
        ),
    )
    parser.add_argument(
        "--testbench",
        default=None,
        help=(
            "Optional companion HSpice testbench .sp (the file with "
            "`.tran` / `.measure` / `.alter`). Only meaningful with "
            "--netlist. If given, its scrubbed view is appended to the "
            "rendered Markdown."
        ),
    )
    parser.add_argument(
        "--depth",
        type=_parse_depth,
        default=1,
        metavar="N|auto",
        help=(
            "Hierarchy depth. 1 (default) = flat read, identical to the "
            "legacy single-cellview output — keeps the agent payload "
            "byte-for-byte stable. 2+ walks same-library subcell masters "
            "and emits an additional 'subcells' section in both Markdown "
            "and JSON. 'auto' (alias 'all') expands to the hard cap "
            f"({_DEPTH_AUTO_MAX}); BFS stops when the tree is exhausted, "
            "so on a shallow design 'auto' costs the same as a matching "
            "integer depth. Clamped SKILL-side to [1, 50]."
        ),
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json", "both"],
        default="markdown",
        help=(
            "Output format. 'markdown' matches exactly what the agent "
            "feeds the LLM; 'json' is the raw sanitized structure for "
            "audit/diff; 'both' writes both files. Default: markdown."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Output path. For --format markdown|json: a file path "
            "(or '-' for stdout, default). For --format both: REQUIRED "
            "directory path; <cell>.md and <cell>.json are written "
            "there. 'both' does not support stdout."
        ),
    )
    parser.add_argument(
        "--with-summary",
        action="store_true",
        help=(
            "Append a human-oriented '### Summary' cell-type tally to "
            "the Markdown output. Disabled by default so that the "
            "Markdown matches byte-for-byte what the agent feeds the "
            "LLM. Does not affect JSON output."
        ),
    )
    parser.add_argument(
        "--pdk-map",
        default=str(PROJECT_ROOT / "config" / "pdk_map.yaml"),
        help="Path to PDK map YAML",
    )
    parser.add_argument(
        "--remote-skill-dir",
        default=None,
        help=(
            "POSIX path on the remote host where safe_*.il SKILL helpers "
            "live. If omitted, SafeBridge falls back to a local PC-side "
            "skill dir (will NOT load on remote host)."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to .env file (default: config/.env)",
    )
    parser.add_argument(
        "--project",
        default=None,
        help=(
            "Project name. When given (and --output is omitted), the "
            "scrubbed circuit is written under "
            "projects/<name>/circuit/<cell>.{md,json} instead of "
            "stdout. Useful when the read is the first step of a new "
            "optimization run — the file lands in the right place for "
            "the next run_agent.py invocation. Pure-stdout callers can "
            "ignore this flag."
        ),
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )
    return parser.parse_args()


def _render_markdown(circuit: dict) -> str:
    """Return EXACTLY what CircuitAgent.run() feeds to the LLM.

    This must stay byte-for-byte identical to the topology string used
    in agent.py's first-turn prompt. Any addendum (e.g. summary tally)
    belongs to a separate, explicitly opt-in artifact — never mixed
    into the default payload.
    """
    return CircuitAgent._format_topology(circuit)


def _render_summary(circuit: dict) -> str:
    """One-line cell-type tally, appended to Markdown as a global hint
    before LLM reasoning (e.g. '3 NMOS, 2 PMOS, 1 inductor')."""
    tally: dict[str, int] = {}
    for inst in circuit.get("instances", []):
        cell = inst.get("cell", "?")
        tally[cell] = tally.get(cell, 0) + 1
    if not tally:
        return ""
    parts = [f"{n} {c}" for c, n in sorted(tally.items())]
    return "\n\n### Summary\n" + ", ".join(parts)


def _render_hierarchical_markdown(hier: dict) -> str:
    """Format a hierarchical schematic payload as Markdown.

    Emits the root cellview using the same topology formatter the flat
    path uses, followed by one `## Subcell <handle>` section per
    deduplicated same-library subcell. Intended for human review /
    audit; the agent still consumes the flat `read_circuit` output.
    """
    parts: list[str] = []
    depth_limit_hit = hier.get("depth_limit_hit")
    parts.append(
        f"# Hierarchical schematic: {hier.get('lib')}/{hier.get('cell')}"
    )
    parts.append(
        f"Depth reached: {hier.get('max_depth_reached', 0)} "
        f"(cap: {hier.get('max_depth', 0)}"
        + (", LIMIT HIT" if depth_limit_hit else "")
        + ")"
    )
    parts.append("")
    root = hier.get("root") or {}
    parts.append(f"## Root (ROOT, depth=0)")
    parts.append(CircuitAgent._format_topology(root))
    for sub in hier.get("subcells") or []:
        handle = sub.get("handle", "?")
        cell = sub.get("cell", "?")
        depth = sub.get("depth", "?")
        parts.append("")
        parts.append(f"## Subcell {handle} — {cell} (depth={depth})")
        parts.append(CircuitAgent._format_topology(sub))
    return "\n".join(parts)


def _run_netlist_mode(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Handle --netlist [--testbench] mode.

    Reads from local disk only — remote fetch is not in scope for the
    MVP; users on COBI can ``scp`` files down first or invoke the
    script over ssh.
    """
    netlist_path = Path(args.netlist)
    if not netlist_path.exists():
        logger.error("netlist not found: %s", netlist_path)
        return 1
    testbench_path: Path | None = None
    if args.testbench:
        testbench_path = Path(args.testbench)
        if not testbench_path.exists():
            logger.error("testbench not found: %s", testbench_path)
            return 1

    logger.info(
        "Netlist mode: scrub + parse %s%s",
        netlist_path,
        f" + {testbench_path}" if testbench_path else "",
    )
    md_text = read_and_render(netlist_path, testbench_path)
    if not md_text.endswith("\n"):
        md_text += "\n"

    out = args.output
    if out is None or out == "-":
        # The renderer emits a `→` for V-source nodes; on Windows the
        # default stdout codec is cp1252 and would crash on that
        # codepoint. Write the bytes through the binary buffer so the
        # CLI is portable regardless of console code page.
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            buf.write(md_text.encode("utf-8"))
        else:
            sys.stdout.write(md_text)
        return 0
    _write(Path(out), md_text)
    logger.info("Wrote %s", out)
    return 0


def _write(out_path: Path, text: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")


def main() -> int:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("read_schematic")

    # Mode detection. Netlist mode short-circuits the SafeBridge path
    # entirely — no Virtuoso connection, no env load, no PDK map.
    if args.netlist is not None:
        if args.lib or args.cell:
            logger.warning(
                "--lib/--cell are ignored in netlist mode "
                "(--netlist=%s)", args.netlist,
            )
        if args.format != "markdown":
            logger.error(
                "--format=%r is not supported in netlist mode "
                "(only markdown). Drop --format or pass markdown.",
                args.format,
            )
            return 2
        return _run_netlist_mode(args, logger)

    if not args.lib or not args.cell:
        logger.error(
            "schematic mode requires --lib and --cell "
            "(or pass --netlist for HSpice .sp mode)."
        )
        return 2

    # --format both has strict output semantics: a real directory is
    # required and stdout is not supported. Validate up front so we
    # don't silently write into a directory literally named '-' or
    # dump generated files into an un-ignored default location.
    if args.format == "both":
        if args.output is None or args.output == "-":
            logger.error(
                "--format both requires an explicit --output DIR "
                "(stdout and default location are not supported)."
            )
            return 2

    env_file = args.env_file or str(PROJECT_ROOT / "config" / ".env")
    env_path = Path(env_file)
    if env_path.exists():
        load_dotenv(env_path)
        logger.info("Loaded env from %s", env_path)

    pdk_map_path = Path(args.pdk_map)
    if not pdk_map_path.exists():
        logger.error("PDK map file not found: %s", pdk_map_path)
        return 1

    logger.info("Connecting to Virtuoso bridge (read-only)...")
    client = VirtuosoClient.from_env()

    # Stage 1 rev 2 (2026-04-18): the spectre parameter was dropped from
    # SafeBridge along with bridge.simulate(); read_schematic never used
    # it anyway.
    bridge = SafeBridge(
        client,
        str(pdk_map_path),
        remote_skill_dir=args.remote_skill_dir,
    )
    # Lock to the single (lib, cell) — same scope guard the agent uses.
    bridge.set_scope(args.lib, args.cell)
    logger.info("Scope bound: %s/%s", args.lib, args.cell)

    if args.depth < 1:
        logger.error("--depth must be >= 1 (got %d)", args.depth)
        return 2

    if args.depth == 1:
        # Flat read — byte-for-byte identical to pre-H1 behavior so
        # everyone downstream (agent, scaffold, specs) keeps working.
        logger.info("Reading circuit (sanitized, depth=1 flat)...")
        circuit = bridge.read_circuit(args.lib, args.cell)
        md_text = _render_markdown(circuit)
        if args.with_summary:
            md_text += _render_summary(circuit)
        md_text += "\n"
        json_text = json.dumps(circuit, indent=2, ensure_ascii=False) + "\n"
    else:
        # Hierarchical read. --depth N means "visit N levels including
        # the root", so the SKILL-side max_depth is N-1.
        max_depth = args.depth - 1
        logger.info(
            "Reading circuit hierarchically (sanitized, cli_depth=%d, "
            "skill_max_depth=%d)...",
            args.depth, max_depth,
        )
        hier = bridge.read_circuit_hierarchical(
            args.lib, args.cell, max_depth=max_depth,
        )
        md_text = _render_hierarchical_markdown(hier) + "\n"
        json_text = json.dumps(hier, indent=2, ensure_ascii=False) + "\n"

    fmt = args.format
    out = args.output

    # If --project is given and the caller did not pass --output, default
    # the destination to projects/<name>/circuit/. For markdown/json this
    # becomes a single file; for "both" it becomes the directory.
    if out is None and args.project:
        proj = resolve_project(args.project, repo_root=PROJECT_ROOT)
        proj.ensure()
        if fmt == "both":
            out = str(proj.circuit_dir)
        else:
            ext = "md" if fmt == "markdown" else "json"
            out = str(proj.circuit_dir / f"{args.cell}.{ext}")

    if fmt == "both":
        # Validated above: out is a real directory path.
        out_dir = Path(out)
        md_path = out_dir / f"{args.cell}.md"
        json_path = out_dir / f"{args.cell}.json"
        _write(md_path, md_text)
        _write(json_path, json_text)
        logger.info("Wrote %s", md_path)
        logger.info("Wrote %s", json_path)
        return 0

    payload = md_text if fmt == "markdown" else json_text
    if out is None or out == "-":
        sys.stdout.write(payload)
        return 0

    _write(Path(out), payload)
    logger.info("Wrote %s", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
