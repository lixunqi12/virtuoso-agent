"""LLM-driven Maestro setup applier (Track C v2).

The LLM may emit four optional structural blocks alongside the normal
``design_vars`` proposal:

  * ``tests``     — list of new Maestro test rows to create
  * ``analyses``  — list of analyses to (re)configure per test
  * ``corners``   — list of corners to create / reconfigure
  * ``outputs``   — list of Outputs Setup rows to add (plus optional
                    pass/fail spec bounds)

This module owns the schema validator and the dispatch helper that
applies them through ``SafeBridge``. Both are intentionally pure-Python
+ fail-soft so the existing agent loop's contract path (design_vars
only) keeps behaving exactly the way it did before Track C v2.

Generality (Track C v2 red lines):
  * Simulator gate lives in SafeBridge — this module only forwards the
    string; the bounded allow-list is the authoritative source.
  * Analysis types are NOT restricted here; ``set_maestro_analysis``
    already enforces the SafeBridge allow-list and the leader's brief
    explicitly said the LLM should be free to mix
    tran/ac/dc/noise/xf/stb/pss/pnoise as the spec demands.
  * Per-entry fail-soft: a malformed ``outputs[3]`` does NOT abort
    ``outputs[4..]`` or ``corners``; the helper records the skip and
    moves on. SafeBridge raises on a hard violation; the dispatcher
    catches and continues.

Backward compat: if the LLM emits none of the four blocks (or emits
empty lists), this module is a no-op. The legacy Phase 1 Option I sync
(``sync_spec_metrics_to_maestro``) remains the default fallback and
keeps running unchanged at agent startup.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .safe_bridge import (
    SafeBridge,
    _MAESTRO_ALLOWED_ANALYSES,
    _MAESTRO_RF_ANALYSIS_OPTIONS,
    _MAESTRO_RF_REQUIRED_OPTIONS,
    _format_maestro_analysis_option_value,
)


_DEFAULT_LOGGER = logging.getLogger(__name__)

# Top-level keys this module owns. Mirror the agent contract — any new
# key must be added here AND to ``_VALID_RESPONSE_KEYS`` in agent.py.
MAESTRO_SETUP_KEYS: frozenset[str] = frozenset({
    "tests", "analyses", "outputs", "corners",
})

# Per-entry required fields. The agent's per-iter loop enforces these
# BEFORE calling SafeBridge so a malformed entry produces a clean
# validation error the LLM can re-prompt against, rather than a generic
# TypeError from deep inside the bridge.
_TEST_REQUIRED = frozenset({"name", "lib", "cell"})
_TEST_OPTIONAL = frozenset({"view", "simulator"})
_ANALYSIS_REQUIRED = frozenset({"test", "analysis"})
_ANALYSIS_OPTIONAL = frozenset({"enable", "options"})
_OUTPUT_REQUIRED = frozenset({"name"})
# An output needs exactly one of ``signal_name`` / ``expr``. ``test`` is
# optional (defaults to scoped tb_cell). ``output_type`` may be empty.
# ``pass`` is the optional pass/fail bound (list/tuple of [lo, hi]).
_OUTPUT_OPTIONAL = frozenset({
    "test", "analysis", "output_type", "signal_name", "expr", "pass",
})
_CORNER_REQUIRED = frozenset({"name"})
_CORNER_OPTIONAL = frozenset({"model_file", "model_section", "variables"})


def validate_maestro_setup_block(parsed: dict) -> str | None:
    """Validate the four structural blocks in a parsed LLM response.

    Returns a semicolon-joined problem string (for the agent's repair
    prompt) or ``None`` if every present block is shape-valid. The
    caller decides what to do with the problems — typically re-prompt
    the LLM with the corrective message.

    Absent blocks are fine (backward compat). Empty lists are fine too
    — the LLM may declare ``"outputs": []`` to make explicit that it
    didn't propose any.
    """
    problems: list[str] = []

    for key in MAESTRO_SETUP_KEYS:
        if key not in parsed:
            continue
        value = parsed[key]
        if not isinstance(value, list):
            problems.append(
                f"'{key}' must be a list of objects; got "
                f"{type(value).__name__}"
            )
            continue
        for idx, entry in enumerate(value):
            if not isinstance(entry, dict):
                problems.append(
                    f"'{key}[{idx}]' must be an object; got "
                    f"{type(entry).__name__}"
                )
                continue
            err = _validate_entry(key, idx, entry)
            if err:
                problems.append(err)

        # R2 P1-2 / R3 P2 self-dup: a single response with two outputs
        # of the same name ON THE SAME TEST is ambiguous — the LLM
        # should pick one expression. Trip the contract repair loop
        # rather than fail-soft so the next iteration sees a clean
        # payload. For ``outputs`` we key on ``(name, test or None)``
        # so two distinct tests can each own a "VOUT_rms"-named entry
        # in the same response; for ``tests`` / ``analyses`` /
        # ``corners`` the key is the bare name (those blocks identify
        # rows globally).
        if key == "outputs":
            keys_seen: dict[tuple, int] = {}
            for idx, entry in enumerate(value):
                if not isinstance(entry, dict):
                    continue
                n = entry.get("name")
                if not isinstance(n, str):
                    continue
                t = entry.get("test")
                if t is not None and not isinstance(t, str):
                    # Bad test type — _validate_entry already flagged it
                    # at the keys layer; skip the dup probe for this
                    # entry to avoid a confusing double error.
                    continue
                k = (n, t)
                if k in keys_seen:
                    problems.append(
                        f"'{key}' has duplicate (name={n!r}, "
                        f"test={t!r}) at indices "
                        f"{keys_seen[k]} and {idx}; each (name, test) "
                        "pair must be unique."
                    )
                    keys_seen[k] = -1
                elif keys_seen.get(k) != -1:
                    keys_seen[k] = idx
        else:
            names_seen: dict[str, int] = {}
            for idx, entry in enumerate(value):
                if not isinstance(entry, dict):
                    continue
                n = entry.get("name")
                if not isinstance(n, str):
                    continue
                if n in names_seen:
                    problems.append(
                        f"'{key}' has duplicate name {n!r} at "
                        f"indices {names_seen[n]} and {idx}; "
                        "each entry must be uniquely named."
                    )
                    names_seen[n] = -1
                elif names_seen.get(n) != -1:
                    names_seen[n] = idx

    return "; ".join(problems) if problems else None


def _validate_entry(block: str, idx: int, entry: dict) -> str | None:
    """Per-block field validation. Returns a problem string or None.

    R2 P2-2 (2026-05-15): also rejects malformed nested values
    (``corners[i].variables`` must be ``dict[str, scalar]``;
    ``outputs[i].pass`` must be a length-2 list of numeric-or-None;
    ``analyses[i].options`` / ``tests[i].options`` must be flat dicts
    if present). The SafeBridge layer would catch these too, but doing
    it here triggers the contract repair loop (=> LLM gets a chance to
    fix) instead of fail-soft-skip during apply.
    """
    if block == "tests":
        err = _validate_keys(
            block, idx, entry, _TEST_REQUIRED, _TEST_OPTIONAL,
        )
        return err
    if block == "analyses":
        err = _validate_keys(
            block, idx, entry, _ANALYSIS_REQUIRED, _ANALYSIS_OPTIONAL,
        )
        if err is not None:
            return err
        test = entry.get("test")
        if not isinstance(test, str):
            return (
                f"'analyses[{idx}].test' must be a string; "
                f"got {type(test).__name__}"
            )
        analysis = entry.get("analysis")
        if not isinstance(analysis, str):
            return (
                f"'analyses[{idx}].analysis' must be a string; "
                f"got {type(analysis).__name__}"
            )
        opts = entry.get("options")
        if opts is not None:
            if not isinstance(opts, dict):
                return (
                    f"'analyses[{idx}].options' must be a dict; "
                    f"got {type(opts).__name__}"
                )
            shape_err = _validate_scalar_dict(
                opts, f"'analyses[{idx}].options'",
            )
            if shape_err is not None:
                return shape_err
        rf_err = _validate_rf_analysis_block(idx, entry)
        if rf_err is not None:
            return rf_err
        return None
    if block == "outputs":
        err = _validate_keys(
            block, idx, entry, _OUTPUT_REQUIRED, _OUTPUT_OPTIONAL,
        )
        if err is not None:
            return err
        # Exclusive-or on signal_name / expr — matches add_maestro_output.
        has_signal = bool(entry.get("signal_name"))
        has_expr = bool(entry.get("expr"))
        if has_signal == has_expr:
            return (
                f"'outputs[{idx}]' needs exactly one of "
                f"'signal_name' or 'expr' "
                f"(got signal_name={has_signal}, expr={has_expr})"
            )
        analysis_err = _validate_output_analysis_affinity(idx, entry)
        if analysis_err is not None:
            return analysis_err
        # ``pass`` (optional) — must be length-2 of [int|float|None].
        if "pass" in entry:
            pass_err = _validate_pass_bounds(
                entry["pass"], f"'outputs[{idx}].pass'",
            )
            if pass_err is not None:
                return pass_err
        return None
    if block == "corners":
        err = _validate_keys(
            block, idx, entry, _CORNER_REQUIRED, _CORNER_OPTIONAL,
        )
        if err is not None:
            return err
        if "variables" in entry:
            var_err = _validate_scalar_dict(
                entry["variables"], f"'corners[{idx}].variables'",
            )
            if var_err is not None:
                return var_err
        return None
    return None  # unreachable: MAESTRO_SETUP_KEYS already filters


_OUTPUT_TRAN_PROBE_RE = re.compile(r"\b[VI]T\s*\(")
_OUTPUT_AC_PROBE_RE = re.compile(r"\b[VI]F\s*\(")


def _validate_output_analysis_affinity(idx: int, entry: dict) -> str | None:
    """Validate optional ``outputs[].analysis`` metadata.

    The current Maestro writer does not create truly analysis-scoped output
    rows, but recipe authors can tag the intended domain. The tag lets the
    contract layer catch the common AC/DC mixup where a DC-looking row uses
    ``VT()`` or an AC row accidentally uses a transient-domain probe.
    """
    analysis = entry.get("analysis")
    if analysis is None:
        return None
    if not isinstance(analysis, str):
        return (
            f"'outputs[{idx}].analysis' must be a string; "
            f"got {type(analysis).__name__}"
        )
    if analysis not in _MAESTRO_ALLOWED_ANALYSES:
        return (
            f"'outputs[{idx}].analysis' must be one of "
            f"{sorted(_MAESTRO_ALLOWED_ANALYSES)}; got {analysis!r}"
        )
    expr = entry.get("expr")
    if not isinstance(expr, str) or not expr:
        # signal_name rows are analysis-neutral at the writer layer.
        return None
    has_tran_probe = bool(_OUTPUT_TRAN_PROBE_RE.search(expr))
    has_ac_probe = bool(_OUTPUT_AC_PROBE_RE.search(expr))
    if analysis == "ac" and has_tran_probe:
        return (
            f"'outputs[{idx}]' is tagged analysis='ac' but uses VT()/IT(); "
            "use VF()/IF() or retag it as tran."
        )
    if analysis == "tran" and has_ac_probe:
        return (
            f"'outputs[{idx}]' is tagged analysis='tran' but uses VF()/IF(); "
            "use VT()/IT() or retag it as ac."
        )
    if analysis == "dc" and (has_tran_probe or has_ac_probe):
        return (
            f"'outputs[{idx}]' is tagged analysis='dc' but uses waveform "
            "probes VT()/IT()/VF()/IF(); use signal_name or a DC/OP-specific "
            "expression instead."
        )
    return None


def _validate_scalar_dict(value: Any, label: str) -> str | None:
    """Reject anything that isn't ``dict[str, int|float|str]``.

    Used for ``corners[i].variables`` and ``*.options``: the SafeBridge
    side flattens these into SKILL atoms one key at a time, so a
    nested dict / list / None blows up there with a confusing TypeError
    that the LLM can't easily fix. Reject at the contract layer so the
    repair prompt is informative.

    R3 P2 (2026-05-15): ``bool`` is now rejected explicitly. The
    SafeBridge formatter ``_format_param_value`` raises on bool but
    only at apply time (no contract repair) — clamping here trips the
    LLM's repair loop instead. The bool check MUST come before the
    numeric ``isinstance(int)`` branch because Python's bool is an int
    subclass: ``isinstance(True, int) == True``.
    """
    if not isinstance(value, dict):
        return f"{label} must be a flat object (key→scalar); got {type(value).__name__}"
    for key, val in value.items():
        if not isinstance(key, str):
            return f"{label} key must be a string; got {type(key).__name__}"
        if isinstance(val, bool):
            return (
                f"{label}[{key!r}] must be scalar (int/float/str); "
                f"got bool (Cadence SKILL has no native bool atom — "
                "encode as 0/1 or the string 't'/'nil')"
            )
        if not isinstance(val, (int, float, str)):
            return (
                f"{label}[{key!r}] must be scalar (int/float/str); "
                f"got {type(val).__name__}"
            )
    return None


def _validate_rf_analysis_block(idx: int, entry: dict) -> str | None:
    """Schema-time RF gate for pss/pnoise analysis entries.

    SafeBridge remains the authoritative dispatch gate. This duplicate call
    exists so malformed RF setup blocks trigger the LLM repair path before
    ``apply_maestro_setup`` enters its fail-soft per-entry loop.
    """
    analysis = entry.get("analysis")
    if not isinstance(analysis, str):
        return (
            f"'analyses[{idx}].analysis' must be a string; "
            f"got {type(analysis).__name__}"
        )
    if analysis not in _MAESTRO_RF_ANALYSIS_OPTIONS:
        return None
    required = _MAESTRO_RF_REQUIRED_OPTIONS[analysis]
    opts = entry.get("options")
    if opts is None:
        return (
            f"'analyses[{idx}]' {analysis} options missing required "
            f"key(s): {sorted(required)}"
        )
    if not isinstance(opts, dict):
        # _validate_entry normally reports this first; keep defensive local
        # handling so this helper remains self-contained if called directly.
        return (
            f"'analyses[{idx}].options' must be a dict; "
            f"got {type(opts).__name__}"
        )

    present = {k.lower() for k in opts if isinstance(k, str)}
    missing = required - present
    if missing:
        return (
            f"'analyses[{idx}]' {analysis} options missing required "
            f"key(s): {sorted(missing)}"
        )

    for key, value in opts.items():
        try:
            _format_maestro_analysis_option_value(analysis, key, value)
        except ValueError as exc:
            return f"'analyses[{idx}]' invalid {analysis} option: {exc}"
    return None


def _validate_pass_bounds(value: Any, label: str) -> str | None:
    """Reject anything that isn't ``[lo, hi]`` with each element
    ``int | float | None``. Matches ``set_maestro_spec``'s contract."""
    if not isinstance(value, (list, tuple)):
        return f"{label} must be a 2-element list; got {type(value).__name__}"
    if len(value) != 2:
        return f"{label} must have exactly 2 elements; got {len(value)}"
    for i, b in enumerate(value):
        if b is None:
            continue
        if isinstance(b, bool):
            return f"{label}[{i}] must be number or None; got bool"
        if not isinstance(b, (int, float)):
            return (
                f"{label}[{i}] must be number or None; "
                f"got {type(b).__name__}"
            )
    return None


def _validate_keys(
    block: str, idx: int, entry: dict,
    required: frozenset[str], optional: frozenset[str],
) -> str | None:
    """Field-presence + unknown-key gate shared by all four blocks."""
    missing = required - set(entry.keys())
    if missing:
        return (
            f"'{block}[{idx}]' missing required field(s): {sorted(missing)}"
        )
    allowed = required | optional
    unknown = set(entry.keys()) - allowed
    if unknown:
        return (
            f"'{block}[{idx}]' has unknown field(s): {sorted(unknown)}. "
            f"Allowed: {sorted(allowed)}"
        )
    return None


def apply_maestro_setup(
    bridge: SafeBridge,
    parsed: dict,
    *,
    logger: logging.Logger | None = None,
) -> dict[str, list]:
    """Walk the four structural blocks and apply them through the bridge.

    Apply order is fixed: ``tests → analyses → corners → outputs``.
    Tests come first because the other three reference them; corners
    before outputs so an output that's later swept across a corner sees
    a corner row already in place.

    Per-entry failures are fail-soft: each bridge call is wrapped in
    try/except, the exception text is recorded in the returned summary,
    and the loop continues. The agent loop's main pass/fail surface is
    unaffected — the LLM-judged design_vars path remains authoritative.

    Returns ``{"applied": {...}, "skipped": {...}}`` where each is keyed
    by block name and the value is a list of per-entry status entries.
    """
    log = logger or _DEFAULT_LOGGER
    applied: dict[str, list] = {k: [] for k in MAESTRO_SETUP_KEYS}
    skipped: dict[str, list] = {k: [] for k in MAESTRO_SETUP_KEYS}

    if not isinstance(parsed, dict):
        # Defensive: the caller should already have ensured this.
        return {"applied": applied, "skipped": skipped}

    # tests → analyses → corners → outputs. The order matters because
    # analyses / outputs reference tests by name; corners are global but
    # outputs may filter on them downstream.
    for block in ("tests", "analyses", "corners", "outputs"):
        entries = parsed.get(block)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = _entry_label(block, entry)
            try:
                _apply_one(bridge, block, entry, log)
            except Exception as exc:  # noqa: BLE001 — fail-soft per entry
                log.warning(
                    "maestro_setup: %s[%s] failed (%s: %s); continuing.",
                    block, name, type(exc).__name__, exc,
                )
                skipped[block].append((name, f"{type(exc).__name__}: {exc}"))
                continue
            applied[block].append(name)

    log.info(
        "maestro_setup: applied %s; skipped %s.",
        {k: len(v) for k, v in applied.items()},
        {k: len(v) for k, v in skipped.items()},
    )
    return {"applied": applied, "skipped": skipped}


def _entry_label(block: str, entry: dict) -> str:
    """Best-effort name for log lines. Never raises."""
    if block == "analyses":
        # An analysis entry has no ``name`` field; identify by
        # ``<test>:<analysis>`` so the skip log is greppable.
        test = entry.get("test", "<no-test>")
        analysis = entry.get("analysis", "<no-analysis>")
        return f"{test}:{analysis}"
    val = entry.get("name", "<unnamed>")
    return val if isinstance(val, str) else "<unnamed>"


def _apply_one(
    bridge: SafeBridge, block: str, entry: dict,
    log: logging.Logger,
) -> None:
    """Dispatch one entry to the appropriate SafeBridge method.

    All keyword argument splatting goes through the SafeBridge
    validators — this function does not re-validate, it just routes.
    """
    if block == "tests":
        bridge.create_maestro_test(
            entry["name"],
            lib=entry["lib"],
            cell=entry["cell"],
            view=entry.get("view", "schematic"),
            simulator=entry.get("simulator", "spectre"),
        )
        return
    if block == "analyses":
        # ``test`` is required at this layer (the validator already
        # checked) — SafeBridge will resolve it against the scope.
        kwargs: dict[str, Any] = {
            "test": entry["test"],
            "enable": entry.get("enable", True),
        }
        opts = entry.get("options")
        if opts is not None:
            kwargs["options"] = opts
        bridge.set_maestro_analysis(entry["analysis"], **kwargs)
        return
    if block == "corners":
        kwargs = {}
        for k in ("model_file", "model_section", "variables"):
            if k in entry:
                kwargs[k] = entry[k]
        bridge.setup_maestro_corner(entry["name"], **kwargs)
        return
    if block == "outputs":
        out_kwargs: dict[str, Any] = {
            "output_type": entry.get("output_type", ""),
        }
        if "signal_name" in entry:
            out_kwargs["signal_name"] = entry["signal_name"]
        if "expr" in entry:
            out_kwargs["expr"] = entry["expr"]
        # ``test`` is preserved as-is (None when absent) so the bridge
        # uses its scoped-tb_cell default — R3 P1 fix: the prior
        # ``entry.get("test") or ""`` coerced "missing" into "" which
        # ``_resolve_maestro_test`` rejected, sending the delete branch
        # through the fail-soft except and letting a duplicate row land.
        entry_test = entry.get("test")
        if entry_test is not None:
            out_kwargs["test"] = entry_test
        # R2 P1-2 / R3 P2: v2 wins. If this output (name keyed by the
        # resolved test + session) was already issued by Option I sync
        # or an earlier iter, drop the prior row before re-adding so
        # the LLM's expression takes effect. Cache key is the tuple
        # ``(name, resolved_test, session)`` to keep two tests'
        # same-name outputs independent.
        out_name = entry["name"]
        try:
            resolved_test = bridge._resolve_maestro_test(entry_test)
        except Exception:  # noqa: BLE001 — defer to add_maestro_output
            # If the scoped tb_cell isn't bound, resolution fails — but
            # ``add_maestro_output`` will raise the same way, so let it
            # surface there with a single canonical error.
            resolved_test = None
        cache_key = (
            (out_name, resolved_test, "")
            if resolved_test is not None else None
        )
        # R3 P2: pre-add dedup probe uses the resolved tuple. Bare-name
        # collisions across different tests no longer trigger a delete.
        if cache_key is not None and cache_key in bridge._added_maestro_outputs:
            try:
                bridge._delete_maestro_output_remote(
                    out_name, test=entry_test,
                )
            except Exception as exc:  # noqa: BLE001 — fail-soft on remove
                # If the remove SKILL probe failed we still try the add
                # — Maestro semantics permit overwrite-by-add as
                # verified on Cadence IC23.1 (see maestro_metric_sync
                # ``overwrite-by-add`` claim). On non-IC23.1 builds the
                # add may produce a duplicate row; preferable to
                # silently keeping the Option I expr. Log so the human
                # can diagnose any stale-row buildup.
                log.warning(
                    "maestro_setup: outputs[%s] pre-remove failed "
                    "(%s: %s); attempting add anyway.",
                    out_name, type(exc).__name__, exc,
                )
            else:
                # Successful remove — pop the cached entry so the
                # post-add record below is the only canonical record.
                bridge._added_maestro_outputs.discard(cache_key)
        bridge.add_maestro_output(out_name, **out_kwargs)
        # Optional pass/fail bounds.
        pass_bounds = entry.get("pass")
        if isinstance(pass_bounds, (list, tuple)) and len(pass_bounds) == 2:
            lo, hi = pass_bounds
            if lo is not None or hi is not None:
                spec_kwargs: dict[str, Any] = {}
                if lo is not None:
                    spec_kwargs["gt"] = lo
                if hi is not None:
                    spec_kwargs["lt"] = hi
                if "test" in entry:
                    spec_kwargs["test"] = entry["test"]
                try:
                    bridge.set_maestro_spec(entry["name"], **spec_kwargs)
                except Exception as exc:  # noqa: BLE001 — pass-bound only
                    # The output landed; the spec call failed
                    # independently. Log but don't roll back the output
                    # — pass/fail in Maestro is advisory, the PC-side
                    # eval block remains authoritative.
                    log.warning(
                        "maestro_setup: outputs[%s] pass-bound write "
                        "failed (%s: %s); output still added.",
                        entry["name"], type(exc).__name__, exc,
                    )
        return
