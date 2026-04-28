"""Rewrite the first ``.PARAM`` block of an HSpice ``.sp`` file.

T8.3 (2026-04-25): closes the LLM loop on HSpice specs by giving the
agent a generic, spec-driven way to mutate design variables between
iterations. The spec author declares which file holds the rewritable
``.PARAM`` block (``hspice.param_rewrite_target: netlist|testbench``)
and which variable names are tunable (``§3``/``§4`` Markdown table).

Two SPICE syntactic forms are both supported because both appear in
real testbenches:

  Multi-line (e.g. ``sch_test_05_02_2024.sp``)::

      .PARAM delay = 50p
      + SIGN = 0V
      + LSB  = 0V

  Single-line (e.g. ``netlist.sp``)::

      .PARAM num_finger_n0=1 num_finger_n1=1 num_finger_p0=1 num_finger_p1=1

By construction only the FIRST ``.PARAM`` block is touched. ``.alter``
blocks downstream that carry their own ``.PARAM`` directives are left
intact -- they encode the test sweep, not the design point. ``.measure``
directives, ``PWL`` stimulus tuples, and any ``KEY=VALUE`` pairs that
appear outside the leading block are likewise untouched, because the
rewriter never scans past the first non-``+`` line that follows the
lead ``.param``.

Whitelist matching is case-insensitive (HSpice itself tokenises
case-insensitively) so a spec listing ``delay`` accepts
``{"DELAY": "75p"}`` from the LLM without complaint. Units are
preserved when the LLM supplies a bare number -- ``50p`` + ``75``
becomes ``75p``, but ``50p`` + ``75n`` becomes ``75n`` (an
LLM-supplied suffix wins on the principle that an explicit unit
overrides an inferred one).

The module is intentionally text-only: no ssh, no fetch, no push.
``HspiceAgent`` (src/agent.py) wraps this with the remote-file
transfer plumbing.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "ParamRewriteError",
    "rewrite_params",
    "rewrite_param_file",
]


class ParamRewriteError(ValueError):
    """Raised on whitelist violation, missing-key proposals, or a
    structurally absent .PARAM block.

    Subclasses ``ValueError`` so callers that wrap the rewrite in a
    broad ``except ValueError`` (the agent's contract-violation path)
    pick it up without an explicit import.
    """


# Lead `.param` directive. HSpice tokenises case-insensitively, and the
# trailing ``\b`` keeps ``.parameter`` / ``.params`` (no such directive
# in HSpice but defense in depth) from accidentally matching.
_PARAM_LEAD_RE = re.compile(r"^\s*\.param\b", re.IGNORECASE)
# Continuation line: a single ``+`` at the start of the line (after
# optional whitespace). HSpice's lexer treats this as "append to the
# previous statement".
_CONT_LINE_RE = re.compile(r"^\s*\+")

# Match ``KEY = VALUE`` or ``KEY=VALUE``. VALUE is either a single-
# quoted expression (``'a+b'``) or a bareword that runs to the next
# whitespace. SPICE identifiers are ``[A-Za-z_][A-Za-z0-9_]*``.
_KV_RE = re.compile(
    r"""
    (?P<key>[A-Za-z_][A-Za-z0-9_]*)
    \s*=\s*
    (?P<value>
        '[^']*'                         # quoted expression
      | [^\s']+                         # bareword: numeric+suffix or symbol
    )
    """,
    re.VERBOSE,
)

# Split a numeric value into mantissa and an optional alpha suffix
# (``50p`` -> ``("50", "p")``, ``-1.2e-3`` -> ``("-1.2e-3", "")``).
_NUM_SPLIT_RE = re.compile(
    r"""
    ^
    (?P<num>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)
    (?P<suf>[A-Za-z]+)?
    $
    """,
    re.VERBOSE,
)


def rewrite_params(
    sp_text: str,
    new_params: dict[str, Any],
    whitelist: Iterable[str],
) -> str:
    """Rewrite values in the first ``.PARAM`` block of ``sp_text``.

    Args:
        sp_text: full text of the .sp file. Line endings are preserved
            via ``splitlines(keepends=True)`` so a CRLF source survives
            the round trip.
        new_params: ``{key: value}`` overrides. Keys must be in
            ``whitelist`` (case-insensitive) AND declared in the first
            .PARAM block. Values may be int / float / str -- non-string
            values are stringified before unit handling.
        whitelist: spec-declared parameter names (any iterable;
            normalised to a lowercased set internally).

    Returns:
        The rewritten .sp text. An empty ``new_params`` returns
        ``sp_text`` unchanged (no work, no error).

    Raises:
        ParamRewriteError: ``new_params`` contains a key outside the
            whitelist; or a key the leading .PARAM block does not
            declare; or the file has no ``.PARAM`` directive at all.
    """
    if not new_params:
        return sp_text

    wl_lower = {str(k).lower() for k in whitelist}
    proposed_lower: dict[str, Any] = {
        str(k).lower(): v for k, v in new_params.items()
    }

    bad = sorted(set(proposed_lower) - wl_lower)
    if bad:
        raise ParamRewriteError(
            f"design_vars key(s) not in whitelist: {bad}; "
            f"allowed: {sorted(wl_lower)}"
        )

    lines = sp_text.splitlines(keepends=True)
    lead_idx = -1
    for i, line in enumerate(lines):
        if _PARAM_LEAD_RE.match(line):
            lead_idx = i
            break
    if lead_idx < 0:
        raise ParamRewriteError(
            "no .PARAM directive found in input; refusing to rewrite "
            "(spec may point at the wrong file via param_rewrite_target)"
        )

    block_end = lead_idx + 1
    while block_end < len(lines) and _CONT_LINE_RE.match(lines[block_end]):
        block_end += 1

    touched: set[str] = set()
    for j in range(lead_idx, block_end):
        lines[j] = _rewrite_line(lines[j], proposed_lower, touched)

    missing = sorted(set(proposed_lower) - touched)
    if missing:
        raise ParamRewriteError(
            f"design_vars key(s) not declared in first .PARAM block: "
            f"{missing}"
        )

    return "".join(lines)


def rewrite_param_file(
    path: Path | str,
    new_params: dict[str, Any],
    whitelist: Iterable[str],
) -> bool:
    """Read, rewrite, and atomically replace the .sp at ``path``.

    Atomic via a sibling ``<path>.tmp`` plus ``os.replace`` -- a
    partial write never leaves an unparseable .sp on disk for HSpice
    to choke on the next iteration. Returns ``True`` when the file
    actually changed, ``False`` when the rewrite was a no-op (e.g.
    empty ``new_params`` or proposals already match the file).
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    out = rewrite_params(text, new_params, whitelist)
    if out == text:
        return False
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, p)
    return True


def _rewrite_line(
    line: str,
    proposed_lower: dict[str, Any],
    touched: set[str],
) -> str:
    """Replace each ``KEY=VALUE`` pair in ``line`` whose lowered key is
    in ``proposed_lower``. Surrounding text -- the ``.PARAM`` directive,
    leading ``+``, whitespace, separator commas -- is preserved
    byte-for-byte. ``touched`` accumulates the lowered keys we
    successfully rewrote so the caller can detect phantom proposals.
    """

    def repl(m: re.Match) -> str:
        key = m.group("key")
        klow = key.lower()
        if klow not in proposed_lower:
            return m.group(0)
        new_val = _format_value(m.group("value"), proposed_lower[klow])
        touched.add(klow)
        # Surgical replacement: keep everything from the match except
        # the value substring. Preserves spacing around ``=`` so the
        # diff stays minimal and reviewer-friendly.
        full = m.group(0)
        val_start = m.start("value") - m.start(0)
        val_end = m.end("value") - m.start(0)
        return full[:val_start] + new_val + full[val_end:]

    return _KV_RE.sub(repl, line)


def _format_value(old_value: str, new_value: Any) -> str:
    """Render ``new_value`` keeping the engineering suffix of
    ``old_value`` when the LLM supplied a bare number.

    Examples::

        ('50p',  75)     -> '75p'
        ('50p',  '75n')  -> '75n'      # LLM-supplied unit wins
        ('1',    2)      -> '2'
        ('0V',   0.8)    -> '0.8V'
        ("'a+b'", '3')   -> '3'        # old expression dropped on rewrite
        ('5',    "'x'")  -> "'x'"      # quoted-expr LLM proposal kept

    Quoted-expression *new* values pass through verbatim so a future
    spec author who wants a derived param still has an escape hatch.
    """
    new_str = str(new_value).strip()
    if new_str.startswith("'") and new_str.endswith("'"):
        return new_str

    new_match = _NUM_SPLIT_RE.match(new_str)
    if new_match and not new_match.group("suf"):
        old_clean = old_value.strip()
        if old_clean.startswith("'") and old_clean.endswith("'"):
            return new_str
        old_match = _NUM_SPLIT_RE.match(old_clean)
        if old_match and old_match.group("suf"):
            return f"{new_match.group('num')}{old_match.group('suf')}"
    return new_str
