"""HSpice PDK-scrub (Task T1 — HSpice backend foundation).

Takes raw HSpice artifacts from a user's working tree and emits a
PDK-neutral equivalent safe to feed to the agent, an LLM, or an
external reviewer. Three artifact types are supported:

- ``scrub_sp(text)``  — the main netlist (.sp). Lines to scrub are
  quoted model-library paths (``.INCLUDE`` / ``.LIB``), foundry cell
  names on instance lines (``nch_lvt`` / ``pch_svt`` etc.), explicit
  foundry tokens (``tsmc`` / ``N16`` / ``dkit`` / ``TUFP``).

- ``scrub_mt0(text)`` — measurement output (.mt0). Usually clean
  numeric data; the common leak is the ``.TITLE`` line quoting an
  absolute path into the foundry dkit.

- ``scrub_lis(text)`` — listing file (.lis). The heaviest of the
  three: operating-point tables quote real model names
  (``nch_lvt_mac`` / ``pch_svt_mac``) and the input-file header
  echoes the absolute path.

Design follows ``src/safe_bridge.py`` (``_scrub`` + ``_sanitize*``):

- **Seed list is source-of-truth in this file** (_FOUNDRY_LEAK_RE),
  mirroring the safe_bridge posture. The public YAML at
  ``config/hspice_scrub_patterns.yaml`` extends — it does not
  replace — the built-in seeds.
- **Post-scrub gate**: after substitution we re-scan the output and
  raise :class:`ScrubError` if any banned token / prefix survived.
  Half-scrubbed content is never returned.
- **Preserve tokens** (``top_tt`` corner, ``matching_test`` SPF stem,
  design signal names like ``WBL`` / ``WWL`` / ``h_in_mid``) are
  granted an exception in both the scrub and gate passes so design
  metadata the LLM legitimately needs stays intact.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import yaml

__all__ = [
    "ScrubError",
    "scrub_sp",
    "scrub_mt0",
    "scrub_lis",
    "load_patterns",
    "DEFAULT_PATTERNS_PATH",
]


DEFAULT_PATTERNS_PATH = (
    Path(__file__).resolve().parent.parent
    / "config"
    / "hspice_scrub_patterns.yaml"
)


# Round-2 (codex review): cap on residuals kept in a ScrubError so a
# .lis full of bad tokens can't balloon memory or encourage a caller
# to log megabytes of raw foundry content. 64 is enough to triage a
# scrub regression by category while staying well under typical log
# envelopes.
_MAX_RESIDUALS = 64


class ScrubError(Exception):
    """Raised when banned tokens survive the scrub.

    Attributes:
        residuals: ``list[str]`` — raw matched substrings, kept for
            local debug ONLY. Capped at :data:`_MAX_RESIDUALS`; any
            overflow sets ``truncated=True``.
        counts:    ``dict[str, int]`` — per-category match counts
            (``foundry_seed`` / ``banned_prefix`` / ``banned_token``
            / ``absolute_path`` / ``model_regex``). These are the
            numbers the ``__str__`` representation shows.
        stage:     which scrubber raised (``sp`` / ``mt0`` / ``lis``).
        truncated: ``True`` if more than :data:`_MAX_RESIDUALS` hits
            were seen (the ``residuals`` list was trimmed, but
            ``counts`` still reflects the true total).

    Round-2 (codex review): ``__str__`` intentionally emits counts &
    categories only — never the raw matched substring. Surfacing
    ``nch_lvt_foo`` in a traceback or a SARIF log would reverse the
    very PDK posture this module exists to enforce. Inspect
    ``err.residuals`` locally when debugging; do not log it.
    """

    def __init__(
        self,
        residuals: Iterable[str],
        *,
        stage: str = "",
        counts: dict[str, int] | None = None,
        truncated: bool = False,
    ):
        self.residuals: list[str] = list(residuals)
        self.stage: str = stage
        self.counts: dict[str, int] = dict(counts or {})
        self.truncated: bool = bool(truncated)
        total = sum(self.counts.values()) if self.counts else len(self.residuals)
        if self.counts:
            category_str = ", ".join(
                f"{k}={v}" for k, v in sorted(self.counts.items())
            )
        else:
            category_str = f"count={total}"
        head = f"[{stage}] " if stage else ""
        trunc_str = " [truncated]" if self.truncated else ""
        super().__init__(
            f"{head}scrub left {total} banned token(s) "
            f"({category_str}){trunc_str}"
        )


# Seed list — keep in lockstep with src/safe_bridge.py::_FOUNDRY_LEAK_RE.
# Augmented with the HSpice-specific tokens Claude Code called out:
# N16 (process node), TUFP (model-library section), dkit* (PDK kit tree).
# These are matched case-insensitively and extend (\w*) so identifiers
# that start with the seed are scrubbed as a whole.
#
# GREP-GATE EXCEPTION: this module is the authoritative banned-token
# source, so the regex itself must literally contain the seeds. No
# other module in src/ or config/ may mention them.
_FOUNDRY_LEAK_RE = re.compile(
    r"\b(?:nch_|pch_|cfmom|rppoly|rm1_|tsmc|tcbn|TUFP|dkits?|N16)\w*",
    re.IGNORECASE,
)

# Absolute path families — same four forms as safe_bridge, plus a
# ``dkits?`` root under the unix family (/usr/local/dkits/... is the
# canonical PDK install).
_ABS_WIN_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s'\"<>|*?]*")
_UNC_PATH_RE = re.compile(r"\\\\[^\s'\"<>|*?\\]+\\[^\s'\"<>|*?]*")
_FORWARD_UNC_PATH_RE = re.compile(r"//[^\s'\"<>|*?/]+/[^\s'\"<>|*?]*")
_ABS_UNIX_PATH_RE = re.compile(
    r"/(?:home|project|proj|tmp|var|Users|usr|opt|etc|nfs|mnt|srv|data"
    r"|tools|cadence|cad|pdk|eda|scratch|work|private|dkits?)"
    r"/[^\s'\"<>|*?]*"
)


def _validate_patterns(patterns: dict) -> None:
    """Fail-closed regex lint for ``model_regex`` entries.

    Round-2 (codex review): called from BOTH ``load_patterns`` (YAML
    path) AND ``_normalize_patterns`` (inline-dict path), so a broken
    regex raises ``ValueError`` regardless of how the patterns got
    into the scrubber. The previous implementation only validated the
    YAML path, so tests / callers passing a dict directly could smuggle
    an uncompilable regex all the way into ``_apply_scrub``, where it
    was silently skipped — leaving a configured sensitive pattern
    effectively disabled.
    """
    for rx in patterns.get("model_regex") or []:
        try:
            re.compile(str(rx))
        except re.error as exc:
            raise ValueError(
                "hspice scrub pattern model_regex entry is not a valid "
                f"regex: {exc}"
            ) from exc


def _normalize_patterns(patterns: dict | None) -> dict:
    """Coerce a (possibly partial) patterns dict into canonical shape.

    Round-2 (codex review): runs :func:`_validate_patterns` so inline
    callers get the same fail-closed guarantee as the YAML loader.
    """
    patterns = patterns or {}
    normalised = {
        "banned_prefixes": [
            str(p) for p in (patterns.get("banned_prefixes") or []) if p
        ],
        "banned_tokens": [
            str(t) for t in (patterns.get("banned_tokens") or []) if t
        ],
        "model_regex": [
            str(r) for r in (patterns.get("model_regex") or []) if r
        ],
        "preserve_tokens": [
            str(t) for t in (
                patterns.get("preserve_tokens")
                or ["top_tt", "matching_test"]
            ) if t
        ],
    }
    _validate_patterns(normalised)
    return normalised


def load_patterns(path: str | Path | None = None) -> dict:
    """Load & validate HSpice scrub patterns from YAML.

    ``path=None`` (default) loads the repo-bundled
    ``config/hspice_scrub_patterns.yaml``. Any of the four recognised
    keys may be absent — defaults apply. Unknown keys are ignored so a
    future schema extension does not break older callers.
    """
    resolved = Path(path) if path is not None else DEFAULT_PATTERNS_PATH
    if not resolved.exists():
        raise FileNotFoundError(
            f"hspice scrub patterns not found: {resolved.name}"
        )
    with open(resolved, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            "hspice scrub patterns must be a YAML mapping "
            f"(got {type(raw).__name__})"
        )
    return _normalize_patterns(raw)


def _preserve_set(patterns: dict) -> set[str]:
    return {t.lower() for t in patterns.get("preserve_tokens", [])}


def _sub_with_preserve(
    text: str,
    pattern: re.Pattern[str],
    replacement: str,
    preserve_lower: set[str],
) -> str:
    """re.sub that leaves preserve-token matches untouched.

    Every hit whose lowercased text exactly equals a preserve token is
    emitted verbatim; all other hits are replaced with ``replacement``.
    """
    def cb(m: re.Match[str]) -> str:
        hit = m.group(0)
        if hit.lower() in preserve_lower:
            return hit
        return replacement

    return pattern.sub(cb, text)


def _apply_scrub(text: str, patterns: dict) -> str:
    """Core scrub sequence applied by all three public scrubbers.

    Ordered so that more specific patterns run first: absolute paths
    and model-lib directive lines go before the per-token pass so the
    token pass doesn't see already-redacted ``<path>`` sentinels.
    """
    preserve = _preserve_set(patterns)

    # 1. Model-library regexes (most specific — whole-line style).
    # Round-2 (codex review): FAIL-CLOSED. A bad regex is caller
    # misconfig; swallowing it here would silently disable a pattern
    # the operator intended to apply. _normalize_patterns already
    # validated, so re.error should be unreachable — but if the
    # module grows a new entry point that bypasses normalisation,
    # this re-raise prevents the regression.
    for rx in patterns.get("model_regex", []):
        try:
            compiled = re.compile(rx)
        except re.error as exc:
            raise ValueError(
                "hspice scrub pattern model_regex entry is not a valid "
                f"regex: {exc}"
            ) from exc
        text = _sub_with_preserve(text, compiled, "<model_lib>", preserve)

    # 2. Explicit banned path prefixes from YAML.
    for prefix in patterns.get("banned_prefixes", []):
        # Match the prefix and greedily consume until whitespace or
        # quote. Case-insensitive so "/usr/local/dkits/..." and
        # "/USR/LOCAL/DKITS/..." both collapse to <path>.
        prefix_re = re.compile(
            re.escape(prefix) + r"[^\s'\"<>|*?]*",
            flags=re.IGNORECASE,
        )
        text = _sub_with_preserve(text, prefix_re, "<path>", preserve)

    # 3. Absolute paths — UNC forms FIRST (a drive-letter regex
    #    would partially consume a UNC path otherwise).
    text = _sub_with_preserve(text, _UNC_PATH_RE, "<path>", preserve)
    text = _sub_with_preserve(
        text, _FORWARD_UNC_PATH_RE, "<path>", preserve,
    )
    text = _sub_with_preserve(text, _ABS_WIN_PATH_RE, "<path>", preserve)
    text = _sub_with_preserve(text, _ABS_UNIX_PATH_RE, "<path>", preserve)

    # 4. Built-in foundry seed RE (source of truth).
    text = _sub_with_preserve(text, _FOUNDRY_LEAK_RE, "<redacted>", preserve)

    # 5. YAML-supplied extra banned tokens (word-boundary anchored).
    for tok in patterns.get("banned_tokens", []):
        tok_re = re.compile(
            r"\b" + re.escape(tok) + r"\w*",
            flags=re.IGNORECASE,
        )
        text = _sub_with_preserve(text, tok_re, "<redacted>", preserve)

    return text


def _gate(text: str, patterns: dict, stage: str) -> None:
    """Post-scrub gate: raise :class:`ScrubError` on residuals.

    Re-runs each banned-pattern class over the already-scrubbed text
    and collects surviving matches. Preserve tokens are exempt. A
    clean text passes in O(N) regex work.

    Round-2 (codex review):
      - ``model_regex`` entries are now re-scanned, so a custom
        directive-style pattern that wasn't fully replaced also
        trips the gate (previously the gate trusted
        ``_apply_scrub`` to have consumed those hits).
      - Residual *strings* are capped at :data:`_MAX_RESIDUALS` for
        :class:`ScrubError` payload hygiene, but ``counts`` always
        reflects the true total so the error message stays honest.
    """
    preserve = _preserve_set(patterns)
    residuals: list[str] = []
    counts: dict[str, int] = {}
    truncated = False

    def _record(category: str, hit: str) -> None:
        nonlocal truncated
        counts[category] = counts.get(category, 0) + 1
        if len(residuals) < _MAX_RESIDUALS:
            residuals.append(hit)
        else:
            truncated = True

    def _scan(category: str, compiled: re.Pattern[str]) -> None:
        for m in compiled.finditer(text):
            hit = m.group(0)
            if hit.lower() in preserve:
                continue
            _record(category, hit)

    # 1. Built-in foundry seeds.
    _scan("foundry_seed", _FOUNDRY_LEAK_RE)
    # 2. Explicit banned prefixes.
    for prefix in patterns.get("banned_prefixes", []):
        _scan(
            "banned_prefix",
            re.compile(
                re.escape(prefix) + r"[^\s'\"<>|*?]*",
                flags=re.IGNORECASE,
            ),
        )
    # 3. YAML-supplied banned tokens.
    for tok in patterns.get("banned_tokens", []):
        _scan(
            "banned_token",
            re.compile(
                r"\b" + re.escape(tok) + r"\w*",
                flags=re.IGNORECASE,
            ),
        )
    # 4. Model-library regexes (R2: gate must also scan these so a
    #    broken/partial scrub in _apply_scrub is still caught).
    for rx in patterns.get("model_regex", []):
        # Already validated by _normalize_patterns; compile is safe.
        _scan("model_regex", re.compile(rx))
    # 5. Absolute paths (surviving paths are a leak even if no foundry
    #    token is embedded — usernames in /home/<u>/... are also PII).
    _scan("absolute_path", _UNC_PATH_RE)
    _scan("absolute_path", _FORWARD_UNC_PATH_RE)
    _scan("absolute_path", _ABS_WIN_PATH_RE)
    _scan("absolute_path", _ABS_UNIX_PATH_RE)

    if counts:
        raise ScrubError(
            residuals, stage=stage, counts=counts, truncated=truncated,
        )


def _run_scrub(text: str, patterns: dict | None, stage: str) -> str:
    """Shared glue used by the three public scrubbers.

    ``patterns=None`` loads the bundled YAML defaults. Empty input is
    returned as-is (the gate on "" is a no-op, but we short-circuit to
    keep stack traces clean in tests).
    """
    if text == "":
        return ""
    pats = _normalize_patterns(
        patterns if patterns is not None else load_patterns()
    )
    out = _apply_scrub(text, pats)
    _gate(out, pats, stage=stage)
    return out


def scrub_sp(text: str, patterns: dict | None = None) -> str:
    """Scrub an HSpice ``.sp`` main-netlist text.

    ``patterns`` defaults to the bundled
    ``config/hspice_scrub_patterns.yaml``. Pass an inline dict for
    tests or custom deployment policies; the built-in foundry seeds
    always apply in addition.
    """
    return _run_scrub(text, patterns, stage="sp")


def scrub_mt0(text: str, patterns: dict | None = None) -> str:
    """Scrub an HSpice ``.mt0`` measurement-output text."""
    return _run_scrub(text, patterns, stage="mt0")


def scrub_lis(text: str, patterns: dict | None = None) -> str:
    """Scrub an HSpice ``.lis`` listing text (op-point + model names)."""
    return _run_scrub(text, patterns, stage="lis")
