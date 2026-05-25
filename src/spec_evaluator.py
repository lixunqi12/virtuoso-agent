"""Spec-driven PC-side pass/fail evaluator.

Stage 1 rev 4 (2026-04-18): promotes the LC_VCO-specific 7-metric
extractor from SKILL to a generic spec-declarative flow.

Stage 1 rev 5 (2026-04-19): adds three-state verdicts (PASS / FAIL /
UNMEASURABLE) and optional per-metric ``sanity: [lo, hi]`` bounds.
Motivation: the old two-state verdict conflated "metric physically
out of target" (FAIL — LLM should adjust design_vars) with "metric
could not be computed" (e.g. spec math unreachable, SKILL returned
no crossing, dump missing — LLM should not touch design_vars; a
human must debug the measurement chain). Conflating the two caused
the LLM to burn iterations tuning a circuit that was actually
oscillating, just measured wrong.

The authoring contract:
    - Spec author writes a ```yaml signals: / windows: / metrics: ``` block
      inside ``config/*_spec.md``. The block is machine-readable while the
      surrounding MD still renders for humans.
    - ``CircuitAgent`` calls ``extract_eval_block(spec_text)`` to parse it,
      ``build_dump_spec(block)`` to derive the SafeBridge dump arguments,
      and ``evaluate(block, dumps, bridge=...)`` to get the measurements
      + pass_fail dicts for one iteration.

The evaluator trusts nothing from the LLM response; measurements and
pass_fail are fully reconstructed from the SKILL dump every iteration.
Rev 1/2/3 delegated these to the LLM (rev 1/2) or to LC_VCO-specific
SKILL (rev 3); both paths coupled the agent to one circuit shape.

YAML schema:

    signals:
      - name: Vdiff           # [A-Za-z_][A-Za-z0-9_]* (<=32 chars)
        kind: Vdiff           # V | I | Vdiff | Vsum_half
        paths: ["/Vout_p", "/Vout_n"]   # list; 1 entry for V/I, 2 for *diff/half

    windows:
      late:   [150e-9, 200e-9]
      early:  [75e-9, 125e-9]
      full:   [100e-9, 200e-9]

    metrics:
      # simple: read one stat from dumps[signal][window]
      - name: f_osc_GHz
        signal: Vdiff
        window: full
        stat: freq_Hz         # one of {mean, min, max, ptp, rms, mean_abs,
                              #          freq_Hz, duty_pct}
        scale: 1.0e-9         # optional; multiply raw stat by this
        pass: [19.5, 20.5]    # [lo, hi]; either may be null for open-ended

      # compound: ratio of two (signal, window, stat) reads
      - name: amp_hold_ratio
        compound: ratio
        numerator:   {signal: Vdiff, window: late,  stat: rms}
        denominator: {signal: Vdiff, window: early, stat: rms}
        pass: [0.95, null]

      # compound: first threshold-crossing, threshold = frac * (dumps ref stat)
      - name: t_startup_ns
        compound: t_cross_frac
        signal: Vdiff           # which signal to probe in SKILL
        frac: 0.45
        ref:                    # threshold = frac * dumps[ref.signal][ref.window][ref.stat]
          signal: Vdiff
          window: late
          stat: ptp
        window: startup         # cross-search window name (in `windows:`)
        direction: rising       # rising | falling | either
        use_abs: true
        scale: 1.0e9            # seconds -> ns
        pass: [null, 10]
        sanity: [0.0, 50.0]     # Tier 2 rev 5: physically plausible range;
                                # values outside are flagged UNMEASURABLE

Signal bounds (rev 5, optional but recommended):

    signals:
      - name: Vdiff
        kind: Vdiff
        paths: ["/Vout_p", "/Vout_n"]
        bounds:
          max_abs: 1.0    # |Vdiff(t)| physically cannot exceed this
          ptp_max: 2.0    # ptp(Vdiff) physically cannot exceed this

The values are used by ``spec_validator.validate_spec_feasibility`` at
agent startup to statically reject infeasible metric formulas (e.g.
``frac=0.9`` with ``stat: ptp`` when ``ptp_max = 2*max_abs`` so the
abs threshold 0.9*ptp_max = 1.8*max_abs is unreachable).
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# Simple stats live verbatim in the SKILL dump per (signal, window).
_SIMPLE_STATS: frozenset[str] = frozenset({
    "mean", "min", "max", "ptp", "rms", "mean_abs", "freq_Hz", "duty_pct",
})
_COMPOUND_KINDS: frozenset[str] = frozenset({"ratio", "t_cross_frac"})

_ALLOWED_DIRECTIONS: frozenset[str] = frozenset({"rising", "falling", "either"})

# Path-2 (2026-05-19): sweep / tuning_metrics schema. A sweep block declares
# a 1-D parameter sweep (e.g. Vctrl ∈ [0, 0.8] V, 9 points). Tuning metrics
# operate on the per-point list of an existing §2 metric OR another tuning
# metric (allowing Kvco_linearity ← Kvco_MHz_per_V chains). The list of ops
# is closed — adding a new op needs a handler in `evaluate_swept`. See
# `_resolve_tuning_order` for the dep-graph + cycle detection.
_SWEPT_OPS: frozenset[str] = frozenset({
    "swept_max_minus_min",
    "swept_segment_slope",
    "swept_ratio_max_over_min",
    "swept_same_sign",
})
_TUNING_REQUIRED_KEYS: frozenset[str] = frozenset({"name", "op", "of"})

# Sweep variable name regex: same shape as design_var names. Mirrors the
# SafeBridge `_is_allowed_param_name` allow-list spirit (case-insensitive
# alpha-num + underscore) — the actual write-side gate stays in SafeBridge;
# this regex is only the spec-schema check.
_SWEEP_VAR_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")

# A YAML eval block is any fenced yaml block whose parsed form is a dict
# carrying all three top-level keys. Pick the first one that matches —
# the spec author may keep other yaml code fences (e.g. examples) but
# only the signals+windows+metrics triple is treated as live config.
_YAML_FENCE_RE = re.compile(
    r"```(?:yaml|yml)\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
_REQUIRED_KEYS = {"signals", "windows", "metrics"}


# --------------------------------------------------------------------- #
#  Extraction + validation
# --------------------------------------------------------------------- #

def extract_eval_block(spec_text: str) -> dict | None:
    """Find the first yaml fence in ``spec_text`` that carries an eval
    block, parse and validate it, return the normalized dict.

    Returns ``None`` if no such block exists — callers then fall back to
    the legacy LLM-judged flow.

    Path-2 (2026-05-19): optional `sweep:` and `tuning_metrics:` keys
    may live in the primary fence OR in subsequent fences (so spec
    authors can keep tuning-curve config in §6 without flattening §2).
    A standalone fence is recognized by having `sweep` and/or
    `tuning_metrics` as its top-level keys with no overlap with the
    primary `_REQUIRED_KEYS`. Splicing is a no-op when the primary
    fence already carries those keys.
    """
    if not isinstance(spec_text, str):
        return None
    primary: dict | None = None
    sweep_addon: Any = None
    tuning_addon: Any = None
    for match in _YAML_FENCE_RE.finditer(spec_text):
        try:
            data = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            logger.warning("spec eval: skipping malformed yaml fence: %s", exc)
            continue
        if not isinstance(data, dict):
            continue
        if primary is None and _REQUIRED_KEYS.issubset(data.keys()):
            primary = data
            continue
        # Path-2 addon fences: collect sweep / tuning_metrics from
        # auxiliary fences once the primary fence is known. Reject
        # mixed shapes (an addon fence carrying signals/windows/metrics
        # too would be a second primary; ignored to avoid surprises).
        if primary is not None and not (_REQUIRED_KEYS & data.keys()):
            if "sweep" in data and sweep_addon is None:
                sweep_addon = data["sweep"]
            if "tuning_metrics" in data and tuning_addon is None:
                tuning_addon = data["tuning_metrics"]
    if primary is None:
        return None
    if sweep_addon is not None and "sweep" not in primary:
        primary["sweep"] = sweep_addon
    if tuning_addon is not None and "tuning_metrics" not in primary:
        primary["tuning_metrics"] = tuning_addon
    validate_eval_block(primary)
    return primary


def validate_eval_block(block: dict) -> None:
    """Raise ``ValueError`` on any structural problem in the eval block.

    The PC-side ``SafeBridge`` re-validates each signal/window/metric
    anyway before forwarding to SKILL, but the agent wants to fail
    fast on a malformed spec rather than after one OCEAN round-trip.
    """
    signals = block.get("signals")
    if not isinstance(signals, list) or not signals:
        raise ValueError("spec eval: 'signals' must be a non-empty list")
    signal_names: set[str] = set()
    for s in signals:
        if not isinstance(s, dict):
            raise ValueError("spec eval: each signal entry must be a mapping")
        name = s.get("name")
        kind = s.get("kind")
        if not isinstance(name, str) or not name:
            raise ValueError("spec eval: signal needs a non-empty name")
        if name in signal_names:
            raise ValueError(f"spec eval: duplicate signal name {name!r}")
        signal_names.add(name)
        if not isinstance(kind, str) or not kind:
            raise ValueError(f"spec eval: signal {name!r} needs a kind")
        paths = _coerce_paths(s)
        if not paths:
            raise ValueError(f"spec eval: signal {name!r} needs at least one path")
        _validate_signal_bounds(s)

    windows = block.get("windows")
    if not isinstance(windows, dict) or not windows:
        raise ValueError("spec eval: 'windows' must be a non-empty mapping")
    window_names: set[str] = set()
    for wname, bounds in windows.items():
        if not isinstance(wname, str) or not wname:
            raise ValueError("spec eval: window name must be a non-empty string")
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            raise ValueError(f"spec eval: window {wname!r} needs [tStart, tEnd]")
        window_names.add(wname)

    metrics = block.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise ValueError("spec eval: 'metrics' must be a non-empty list")
    metric_names: set[str] = set()
    for m in metrics:
        if not isinstance(m, dict):
            raise ValueError("spec eval: each metric entry must be a mapping")
        name = m.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("spec eval: metric needs a non-empty name")
        if name in metric_names:
            raise ValueError(f"spec eval: duplicate metric name {name!r}")
        metric_names.add(name)
        _validate_metric(m, signal_names, window_names)

    # Path-2 optional blocks. `sweep` and `tuning_metrics` are paired —
    # the dep-graph resolver in `_resolve_tuning_order` runs only when
    # both are present; spec authors that declare neither keep the old
    # single-point flow unchanged. Either-without-the-other is an error
    # (a sweep with no tuning metrics is useless; tuning metrics with
    # no sweep have nothing to iterate over).
    sweep = block.get("sweep")
    tuning_metrics = block.get("tuning_metrics")
    if sweep is None and tuning_metrics is None:
        return
    if sweep is None or tuning_metrics is None:
        raise ValueError(
            "spec eval: `sweep` and `tuning_metrics` must be declared "
            "together (or both omitted)"
        )
    _validate_sweep_block(sweep)
    _validate_tuning_metrics(tuning_metrics, metric_names)


_BOUND_KEYS: frozenset[str] = frozenset({
    "max_abs", "ptp_max", "min", "max",
})


def _validate_signal_bounds(signal_entry: dict) -> None:
    """Accept an optional ``bounds:`` sub-mapping on a signal entry.

    The concrete keys (``max_abs`` / ``ptp_max`` / ``min`` / ``max``) are
    consumed by ``spec_validator.py`` for static feasibility checks; this
    function only enforces that, when present, the block is a mapping of
    known keys → finite numbers. Unknown keys are ignored (forward-
    compatibility for new stat kinds).
    """
    bounds = signal_entry.get("bounds")
    if bounds is None:
        return
    if not isinstance(bounds, dict):
        raise ValueError(
            f"signal {signal_entry.get('name')!r}: bounds must be a mapping"
        )
    for k, v in bounds.items():
        if k not in _BOUND_KEYS:
            # Unknown bound keys: tolerated (a future stat may need them)
            # but flag at warning-level via logger so spec authors see
            # typos.
            logger.warning(
                "signal %s: unknown bound key %r (known: %s)",
                signal_entry.get("name"), k, sorted(_BOUND_KEYS),
            )
            continue
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(
                f"signal {signal_entry.get('name')!r}: bounds[{k!r}] "
                "must be a finite number"
            )


def _coerce_paths(signal_entry: dict) -> list[str]:
    """Accept either ``path: "/x"`` or ``paths: ["/x", "/y"]`` and
    normalize to a list. Purely a spec-readability affordance."""
    if "paths" in signal_entry:
        raw = signal_entry["paths"]
    elif "path" in signal_entry:
        raw = [signal_entry["path"]]
    else:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError(
            f"signal {signal_entry.get('name')!r}: paths must be a list"
        )
    out: list[str] = []
    for p in raw:
        if not isinstance(p, str) or not p:
            raise ValueError(
                f"signal {signal_entry.get('name')!r}: each path must be non-empty str"
            )
        out.append(p)
    return out


def _validate_metric(m: dict, signals: set[str], windows: set[str]) -> None:
    compound = m.get("compound")
    if compound is None:
        signal = m.get("signal")
        window = m.get("window")
        stat = m.get("stat")
        if signal not in signals:
            raise ValueError(
                f"metric {m['name']!r}: signal {signal!r} not declared"
            )
        if window not in windows:
            raise ValueError(
                f"metric {m['name']!r}: window {window!r} not declared"
            )
        if stat not in _SIMPLE_STATS:
            raise ValueError(
                f"metric {m['name']!r}: stat {stat!r} not in {sorted(_SIMPLE_STATS)}"
            )
        _validate_pass_range(m)
        return

    if compound not in _COMPOUND_KINDS:
        raise ValueError(
            f"metric {m['name']!r}: compound {compound!r} not in {sorted(_COMPOUND_KINDS)}"
        )

    if compound == "ratio":
        for role in ("numerator", "denominator"):
            sub = m.get(role)
            if not isinstance(sub, dict):
                raise ValueError(
                    f"metric {m['name']!r}: {role!r} must be a mapping"
                )
            if sub.get("signal") not in signals:
                raise ValueError(
                    f"metric {m['name']!r}: {role!r} signal not declared"
                )
            if sub.get("window") not in windows:
                raise ValueError(
                    f"metric {m['name']!r}: {role!r} window not declared"
                )
            if sub.get("stat") not in _SIMPLE_STATS:
                raise ValueError(
                    f"metric {m['name']!r}: {role!r} stat not in {sorted(_SIMPLE_STATS)}"
                )
    elif compound == "t_cross_frac":
        if m.get("signal") not in signals:
            raise ValueError(
                f"metric {m['name']!r}: signal {m.get('signal')!r} not declared"
            )
        if m.get("window") not in windows:
            raise ValueError(
                f"metric {m['name']!r}: cross-window {m.get('window')!r} not declared"
            )
        frac = m.get("frac")
        if not isinstance(frac, (int, float)) or not (0.0 < frac <= 1.0):
            raise ValueError(
                f"metric {m['name']!r}: frac must be a number in (0, 1]"
            )
        ref = m.get("ref")
        if not isinstance(ref, dict):
            raise ValueError(f"metric {m['name']!r}: ref must be a mapping")
        if ref.get("signal") not in signals:
            raise ValueError(f"metric {m['name']!r}: ref signal not declared")
        if ref.get("window") not in windows:
            raise ValueError(f"metric {m['name']!r}: ref window not declared")
        if ref.get("stat") not in _SIMPLE_STATS:
            raise ValueError(
                f"metric {m['name']!r}: ref stat not in {sorted(_SIMPLE_STATS)}"
            )
        direction = m.get("direction", "rising")
        if direction not in _ALLOWED_DIRECTIONS:
            raise ValueError(
                f"metric {m['name']!r}: direction must be one of "
                f"{sorted(_ALLOWED_DIRECTIONS)}"
            )
    _validate_pass_range(m)
    _validate_sanity_range(m)


def _validate_pass_range(m: dict) -> None:
    pr = m.get("pass")
    if pr is None:
        return
    if not isinstance(pr, (list, tuple)) or len(pr) != 2:
        raise ValueError(f"metric {m['name']!r}: pass must be [lo, hi]")
    lo, hi = pr
    for val, label in ((lo, "lo"), (hi, "hi")):
        if val is None:
            continue
        if not isinstance(val, (int, float)) or not math.isfinite(val):
            raise ValueError(
                f"metric {m['name']!r}: pass {label} must be finite number or null"
            )
    if lo is not None and hi is not None and lo > hi:
        raise ValueError(f"metric {m['name']!r}: pass lo > hi")


def _validate_sanity_range(m: dict) -> None:
    """Tier 2 (rev 5): optional ``sanity: [lo, hi]`` per metric.

    sanity is physically plausible range (wider than ``pass``). A value
    inside ``sanity`` but outside ``pass`` is a legitimate FAIL the LLM
    should try to fix. A value outside ``sanity`` is treated as
    UNMEASURABLE — the number itself is suspect (measurement chain
    broken, spec unit wrong, etc.).
    """
    sr = m.get("sanity")
    if sr is None:
        return
    if not isinstance(sr, (list, tuple)) or len(sr) != 2:
        raise ValueError(f"metric {m['name']!r}: sanity must be [lo, hi]")
    lo, hi = sr
    for val, label in ((lo, "lo"), (hi, "hi")):
        if val is None:
            continue
        if not isinstance(val, (int, float)) or not math.isfinite(val):
            raise ValueError(
                f"metric {m['name']!r}: sanity {label} must be finite "
                "number or null"
            )
    if lo is not None and hi is not None and lo > hi:
        raise ValueError(f"metric {m['name']!r}: sanity lo > hi")


def _validate_sweep_block(sweep: Any) -> None:
    """Optional `sweep:` block declaring a 1-D parameter sweep.

    Schema (all fields required when sweep is declared):
        variable: str (allow-list regex)
        range:    [lo, hi] finite floats, lo < hi
        points:   int in [2, 64]
        unit:     str (advisory; rendered into log/prompt only)

    SafeBridge re-validates `sweep_root` (CLI flag, not spec field)
    independently — this validator only covers the spec schema.
    """
    if not isinstance(sweep, dict):
        raise ValueError("spec eval: `sweep` must be a mapping")
    var = sweep.get("variable")
    if not isinstance(var, str) or not _SWEEP_VAR_NAME_RE.fullmatch(var):
        raise ValueError(
            "spec eval: sweep.variable must match "
            f"{_SWEEP_VAR_NAME_RE.pattern!r}"
        )
    rng = sweep.get("range")
    if not isinstance(rng, (list, tuple)) or len(rng) != 2:
        raise ValueError("spec eval: sweep.range must be [lo, hi]")
    lo, hi = rng
    for v, label in ((lo, "lo"), (hi, "hi")):
        # R2 (2026-05-19, codex P2 BLOCKER): bool is a subclass of int, so
        # ``isinstance(False, (int, float))`` is True. YAML ``range: [false,
        # true]`` would silently round-trip to ``[0.0, 1.0]`` and produce a
        # 9-entry manifest spanning the wrong control voltage. Reject bool
        # explicitly BEFORE the numeric/finite check.
        if isinstance(v, bool):
            raise ValueError(
                f"spec eval: sweep.range {label} must be numeric, not boolean"
            )
        if not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            raise ValueError(
                f"spec eval: sweep.range {label} must be a finite number"
            )
    if float(lo) >= float(hi):
        raise ValueError("spec eval: sweep.range lo must be < hi")
    points = sweep.get("points")
    if not isinstance(points, int) or isinstance(points, bool):
        raise ValueError("spec eval: sweep.points must be an int")
    if not (2 <= points <= 64):
        raise ValueError("spec eval: sweep.points must be in [2, 64]")
    unit = sweep.get("unit")
    if unit is not None and not isinstance(unit, str):
        raise ValueError("spec eval: sweep.unit must be a string when set")


def _validate_tuning_metrics(
    tuning_metrics: Any, base_metric_names: set[str]
) -> None:
    """Validate `tuning_metrics` list + dep-graph (cycles, unknown refs).

    Each entry must reference (`of:`) either a §2 metric name or another
    tuning metric — chains like `Kvco_linearity ← Kvco_MHz_per_V` are
    intentionally supported. `_resolve_tuning_order` performs the actual
    topological sort.
    """
    if not isinstance(tuning_metrics, list) or not tuning_metrics:
        raise ValueError("spec eval: `tuning_metrics` must be a non-empty list")
    seen: set[str] = set()
    for entry in tuning_metrics:
        if not isinstance(entry, dict):
            raise ValueError(
                "spec eval: each tuning_metrics entry must be a mapping"
            )
        missing = _TUNING_REQUIRED_KEYS - entry.keys()
        if missing:
            raise ValueError(
                f"spec eval: tuning metric missing required keys "
                f"{sorted(missing)}"
            )
        name = entry["name"]
        if not isinstance(name, str) or not name:
            raise ValueError("spec eval: tuning metric needs a non-empty name")
        if name in base_metric_names:
            raise ValueError(
                f"spec eval: tuning metric name {name!r} collides with §2 "
                "metric — pick a distinct name"
            )
        if name in seen:
            raise ValueError(f"spec eval: duplicate tuning metric {name!r}")
        seen.add(name)
        op = entry["op"]
        if op not in _SWEPT_OPS:
            raise ValueError(
                f"spec eval: tuning metric {name!r}: op {op!r} not in "
                f"{sorted(_SWEPT_OPS)}"
            )
        of = entry["of"]
        if not isinstance(of, str) or not of:
            raise ValueError(
                f"spec eval: tuning metric {name!r}: `of` must be a non-empty "
                "string referencing a §2 metric or earlier tuning metric"
            )
        # `of` may refer to a tuning_metric defined later in the list —
        # cycles are caught by `_resolve_tuning_order`, undefined refs
        # too. Don't reject forward references here.
        scale = entry.get("scale")
        if scale is not None:
            # PyYAML 1.1 parses `1.0e3` as a string (no sign after `e`),
            # so accept str-coercible values too; the existing simple-
            # metric scale path (`_compute_metric`) also coerces lazily
            # via float().
            if isinstance(scale, bool):
                raise ValueError(
                    f"spec eval: tuning metric {name!r}: scale must be a "
                    "finite number when set"
                )
            try:
                fscale = float(scale)
            except (TypeError, ValueError):
                raise ValueError(
                    f"spec eval: tuning metric {name!r}: scale must be a "
                    "finite number when set"
                )
            if not math.isfinite(fscale):
                raise ValueError(
                    f"spec eval: tuning metric {name!r}: scale must be a "
                    "finite number when set"
                )
        if op == "swept_same_sign":
            _validate_bool_pass(entry)
        else:
            _validate_pass_range(entry)
        _validate_sanity_range(entry)

    # Dep-graph + cycle check. _resolve_tuning_order raises on cycles
    # or undefined `of:` references.
    _resolve_tuning_order(
        {"metrics": [{"name": n} for n in base_metric_names],
         "tuning_metrics": tuning_metrics}
    )


def _validate_bool_pass(entry: dict) -> None:
    """`swept_same_sign` returns a bool; its pass range is `[true, true]`
    or `[false, false]` to gate on a specific value. Other forms are
    nonsense (a bool can't fall "between" two values)."""
    pr = entry.get("pass")
    if pr is None:
        return
    if not isinstance(pr, (list, tuple)) or len(pr) != 2:
        raise ValueError(
            f"tuning metric {entry.get('name')!r}: pass must be [bool, bool] "
            "for bool-valued op"
        )
    lo, hi = pr
    if not isinstance(lo, bool) or not isinstance(hi, bool):
        raise ValueError(
            f"tuning metric {entry.get('name')!r}: bool-valued op requires "
            "pass entries to be bool literals (true/false)"
        )
    if lo != hi:
        raise ValueError(
            f"tuning metric {entry.get('name')!r}: pass [lo, hi] must be the "
            "same bool — a bool value cannot fall 'between' true and false"
        )


def _resolve_tuning_order(block: dict) -> list[dict]:
    """Topological order over `tuning_metrics` so dependent ops eval
    after their `of:` reference is computed.

    A tuning metric's `of:` may reference either a §2 metric (always
    available — supplied as per-point list by caller) or another tuning
    metric defined in the same block. Cycles and dangling refs raise
    ValueError. Returns the list of tuning_metric dicts in the order
    `evaluate_swept` should compute them.
    """
    tuning = block.get("tuning_metrics") or []
    if not tuning:
        return []
    base_names = {m.get("name") for m in (block.get("metrics") or [])}
    by_name = {t["name"]: t for t in tuning}

    visited: dict[str, str] = {}  # "visiting" | "done"
    ordered: list[dict] = []

    def visit(name: str, stack: tuple[str, ...]) -> None:
        state = visited.get(name)
        if state == "done":
            return
        if state == "visiting":
            cycle = " -> ".join(stack + (name,))
            raise ValueError(f"spec eval: tuning_metrics cycle: {cycle}")
        visited[name] = "visiting"
        entry = by_name[name]
        of = entry["of"]
        if of in by_name:
            visit(of, stack + (name,))
        elif of not in base_names:
            raise ValueError(
                f"spec eval: tuning metric {name!r}: `of` {of!r} is neither "
                "a §2 metric nor a tuning metric in this block"
            )
        visited[name] = "done"
        ordered.append(entry)

    for t in tuning:
        visit(t["name"], ())
    return ordered


# --------------------------------------------------------------------- #
#  Conversion to SafeBridge-shaped args
# --------------------------------------------------------------------- #

def build_dump_spec(block: dict) -> tuple[
    list[tuple[str, str, list[str]]],
    list[tuple[str, float, float]],
]:
    """Produce the (signals, windows) tuples that
    ``SafeBridge.run_ocean_dump_all`` expects."""
    signals: list[tuple[str, str, list[str]]] = []
    for s in block["signals"]:
        signals.append((s["name"], s["kind"], _coerce_paths(s)))
    windows: list[tuple[str, float, float]] = []
    for wname, bounds in block["windows"].items():
        windows.append((wname, float(bounds[0]), float(bounds[1])))
    return signals, windows


def extract_osc_signals(block: dict) -> list[str] | None:
    """Return the ``[pathP, pathN]`` pair that designates the circuit's
    differential output, or ``None`` if the spec does not declare one.

    Looked up by convention: the first signal whose ``kind == "Vdiff"``
    and whose ``paths`` has exactly 2 entries. This list is passed to
    ``OceanWorker.dump_all(osc_signals=...)`` so the OCEAN worker can
    short-circuit on degenerate (non-oscillating) runs before entering
    cross-based stats. Topology-agnostic — any spec that declares a
    Vdiff signal opts in automatically.
    """
    for s in block.get("signals", []):
        if s.get("kind") == "Vdiff":
            paths = _coerce_paths(s)
            if len(paths) == 2:
                return list(paths)
    return None


def _signal_spec(block: dict, name: str) -> tuple[str, list[str]]:
    for s in block["signals"]:
        if s["name"] == name:
            return s["kind"], _coerce_paths(s)
    raise KeyError(f"signal {name!r} not declared")


def _window_bounds(block: dict, name: str) -> tuple[float, float]:
    bounds = block["windows"][name]
    return float(bounds[0]), float(bounds[1])


# --------------------------------------------------------------------- #
#  Evaluation
# --------------------------------------------------------------------- #

def evaluate(
    block: dict,
    dumps: dict[str, Any],
    bridge: Any = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Compute ``(measurements, pass_fail)`` from ``block`` + SKILL ``dumps``.

    ``bridge`` is only needed if the block declares any ``t_cross_frac``
    compound metric — in that case ``evaluate`` issues one
    ``run_ocean_t_cross`` SKILL call per such metric (threshold is a
    function of already-dumped stats, computed on PC).

    Rev 5 (2026-04-19): verdict strings are three-state:
        - ``"PASS"``  — value exists, inside pass/sanity ranges
        - ``"FAIL (<reason>)"``  — value exists but outside ``pass``
        - ``"UNMEASURABLE (<reason>)"`` — value could not be computed,
          OR value is outside ``sanity`` and therefore physically
          suspect. UNMEASURABLE does NOT count as PASS for convergence
          (``_all_pass`` still requires every verdict to start with
          "PASS"), but it does signal the LLM / human to debug the
          measurement chain rather than tweak ``design_vars``.
    """
    measurements: dict[str, Any] = {}
    pass_fail: dict[str, str] = {}
    for m in block["metrics"]:
        name = m["name"]
        try:
            value, reason = _compute_metric(m, block, dumps, bridge)
        except Exception as exc:  # noqa: BLE001 — evaluator never crashes the loop
            logger.warning(
                "spec eval: metric %s computation failed: %s",
                name, exc,
            )
            value, reason = None, f"exception: {type(exc).__name__}"
        measurements[name] = value
        pass_fail[name] = _verdict(value, m.get("pass"), m.get("sanity"), reason)
    return measurements, pass_fail


def _compute_metric(
    m: dict,
    block: dict,
    dumps: dict[str, Any],
    bridge: Any,
) -> tuple[float | None, str | None]:
    """Return ``(value, reason)``.

    ``reason`` is ``None`` on success. Non-None means the metric could
    not be computed and ``value`` is ``None``; the string explains why
    so the LLM / log can distinguish "circuit broken" from "measurement
    chain broken".
    """
    scale = float(m.get("scale", 1.0))
    compound = m.get("compound")

    if compound is None:
        raw = _read_simple(dumps, m["signal"], m["window"], m["stat"])
        if raw is None:
            return None, (
                f"dump missing {m['signal']}/{m['window']}/{m['stat']}"
            )
        return float(raw) * scale, None

    if compound == "ratio":
        num = _read_simple(
            dumps,
            m["numerator"]["signal"],
            m["numerator"]["window"],
            m["numerator"]["stat"],
        )
        den = _read_simple(
            dumps,
            m["denominator"]["signal"],
            m["denominator"]["window"],
            m["denominator"]["stat"],
        )
        if num is None:
            return None, "ratio numerator missing from dumps"
        if den is None:
            return None, "ratio denominator missing from dumps"
        if den == 0:
            return None, "ratio denominator is zero"
        return (float(num) / float(den)) * scale, None

    if compound == "t_cross_frac":
        if bridge is None:
            logger.warning(
                "spec eval: metric %s needs bridge for SKILL t_cross but "
                "none was passed", m["name"]
            )
            return None, "bridge unavailable for t_cross SKILL call"
        ref = _read_simple(
            dumps,
            m["ref"]["signal"],
            m["ref"]["window"],
            m["ref"]["stat"],
        )
        if ref is None:
            return None, (
                f"ref stat {m['ref']['signal']}/{m['ref']['window']}/"
                f"{m['ref']['stat']} missing from dumps"
            )
        frac = float(m["frac"])
        threshold = frac * float(ref)
        if not math.isfinite(threshold) or threshold == 0.0:
            return None, (
                f"threshold degenerate (frac={frac}, ref={ref})"
            )
        kind, paths = _signal_spec(block, m["signal"])
        ts, te = _window_bounds(block, m["window"])
        direction = m.get("direction", "rising")
        use_abs = bool(m.get("use_abs", False))
        try:
            result = bridge.run_ocean_t_cross(
                kind=kind,
                paths=paths,
                threshold=threshold,
                t_start=ts,
                t_end=te,
                direction=direction,
                use_abs=use_abs,
            )
        except Exception as exc:  # noqa: BLE001 — never fail evaluation
            logger.warning(
                "spec eval: t_cross SKILL call failed for %s: %s",
                m["name"], exc,
            )
            return None, f"t_cross SKILL call error: {type(exc).__name__}"
        if not result.get("ok"):
            err = result.get("error") or "no crossing found"
            return None, f"t_cross: {err}"
        value = result.get("value")
        if value is None:
            return None, "t_cross returned ok but no value"
        return float(value) * scale, None

    return None, f"unknown compound kind {compound!r}"  # unreachable


def _read_simple(
    dumps: dict[str, Any],
    signal: str,
    window: str,
    stat: str,
) -> float | None:
    sig = dumps.get(signal) or {}
    win = sig.get(window) or {}
    raw = win.get(stat)
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None


def _verdict(
    value: Any,
    pass_range: Any,
    sanity_range: Any = None,
    reason: str | None = None,
) -> str:
    """Rev 5: three-state verdict. Path-2 (2026-05-19): bool values flow
    through the bool branch — same UNMEASURABLE/FAIL/PASS three-state but
    the comparison reduces to equality against `pass_range[0]` (which
    `_validate_bool_pass` already enforces equal to `pass_range[1]`).

    UNMEASURABLE conditions (in precedence order):
        1. value is None and we captured a reason
        2. value is not None but outside ``sanity_range`` (physically
           implausible — blame measurement, not circuit)

    Otherwise FAIL / PASS on pass_range membership.
    """
    if value is None:
        return f"UNMEASURABLE ({reason or 'no value'})"

    # Bool path runs before the numeric sanity comparison: comparing a
    # bool against numeric bounds with `<` would silently coerce True/False
    # to 1/0, masking misuse. `_validate_bool_pass` keeps bool ops
    # bool-only, so we only need a bool branch here, not a guard against
    # mixed types.
    if isinstance(value, bool):
        if pass_range is None:
            return "PASS"
        lo, hi = pass_range
        if isinstance(lo, bool) and isinstance(hi, bool):
            if value == lo:
                return "PASS"
            return f"FAIL (expected {lo}, got {value})"
        # Numeric pass range against a bool metric — shouldn't happen
        # because validator rejects it, but be explicit.
        return f"UNMEASURABLE (numeric pass range on bool value)"

    if sanity_range is not None:
        s_lo, s_hi = sanity_range
        if s_lo is not None and value < s_lo:
            return (
                f"UNMEASURABLE (suspect: value {value:.6g} < sanity lo "
                f"{s_lo})"
            )
        if s_hi is not None and value > s_hi:
            return (
                f"UNMEASURABLE (suspect: value {value:.6g} > sanity hi "
                f"{s_hi})"
            )

    if pass_range is None:
        return "PASS"
    lo, hi = pass_range
    if lo is not None and value < lo:
        return f"FAIL (below {lo})"
    if hi is not None and value > hi:
        return f"FAIL (above {hi})"
    return "PASS"


# --------------------------------------------------------------------- #
#  Path-2: swept (tuning-curve) evaluation
# --------------------------------------------------------------------- #

def evaluate_swept(
    block: dict,
    base_measurements_per_point: list[dict[str, Any]],
    vctrl_values: list[float],
    *,
    bridge: Any = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Compute the tuning-curve metrics in ``block['tuning_metrics']``.

    ``base_measurements_per_point[i]`` is the dict of §2 metric values
    measured at sweep point ``i``; ``vctrl_values[i]`` is the sweep
    variable value at that index. Both lists are assumed already sorted
    by Vctrl ascending (the agent / `analyze_tuning_curve.py` orders
    them after reading the Maestro sweep manifest — the Maestro point
    index is NOT sequential, see `scripts/_ocean_tuning_extract.ocn`).

    Each tuning metric's ``of:`` references either a §2 metric (then the
    per-point value list is the column from ``base_measurements_per_point``)
    or another tuning metric (then it's whatever that op produced —
    e.g. the N-1-element list from ``swept_segment_slope``). Returns
    ``(tuning_measurements, tuning_pass_fail)``; values may be float,
    list[float], or bool depending on the op.

    ``bridge`` is unused today (no SKILL calls); the kwarg is reserved
    so the signature matches ``evaluate(...)`` and future ops can opt in.
    """
    del bridge  # reserved for future use
    if not block.get("tuning_metrics"):
        return {}, {}
    if len(base_measurements_per_point) != len(vctrl_values):
        raise ValueError(
            "evaluate_swept: base_measurements_per_point and vctrl_values "
            "must have the same length "
            f"({len(base_measurements_per_point)} vs {len(vctrl_values)})"
        )

    tuning_measurements: dict[str, Any] = {}
    tuning_pass_fail: dict[str, str] = {}

    base_metric_names = {
        m.get("name") for m in (block.get("metrics") or [])
    }

    for entry in _resolve_tuning_order(block):
        name = entry["name"]
        op = entry["op"]
        of = entry["of"]
        # `entry.get("scale") or 1.0` would coerce 0 / "" / None to 1.0;
        # be explicit so a typo-zero scale isn't silently re-floored.
        raw_scale = entry.get("scale")
        scale = 1.0 if raw_scale is None else float(raw_scale)
        try:
            value, reason = _compute_swept(
                op, of, entry, base_metric_names,
                base_measurements_per_point, vctrl_values,
                tuning_measurements,
            )
        except Exception as exc:  # noqa: BLE001 — evaluator never crashes
            logger.warning(
                "spec eval: tuning metric %s computation failed: %s",
                name, exc,
            )
            value, reason = None, f"exception: {type(exc).__name__}"

        # Scale is applied only to numeric (scalar or list) outputs;
        # bools (swept_same_sign) ignore it. Apply BEFORE the verdict
        # so the pass band can be expressed in the user-facing unit
        # (e.g. MHz/V, not GHz/V).
        if value is not None and scale != 1.0:
            if isinstance(value, list):
                value = [v * scale for v in value]
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                value = float(value) * scale

        tuning_measurements[name] = value
        # For ops that produce a list (segment_slope), the verdict is
        # computed against the worst-case element so a single bad
        # segment cannot hide behind an in-range max. The
        # tuning_measurements value stays the full list so the LLM
        # prompt can show every segment.
        verdict_value = _worst_case_for_pass(value, entry.get("pass"))
        tuning_pass_fail[name] = _verdict(
            verdict_value, entry.get("pass"), entry.get("sanity"), reason,
        )

    return tuning_measurements, tuning_pass_fail


def _compute_swept(
    op: str,
    of: str,
    entry: dict,
    base_metric_names: set[str],
    base_per_point: list[dict[str, Any]],
    vctrl_values: list[float],
    tuning_results: dict[str, Any],
) -> tuple[Any, str | None]:
    """Per-op handler. Returns (value, reason); reason is non-None when
    value is None / UNMEASURABLE."""
    # Resolve `of:` to a value series. Two source shapes:
    #   - base metric: paired (vctrl, value) list with None drops
    #   - tuning metric producing a list (segment_slope): scalar-ish
    #     list, no x-axis pairing (ratio / same_sign / max_minus_min)
    if of in base_metric_names:
        pairs = [
            (v, p.get(of)) for v, p in zip(vctrl_values, base_per_point)
        ]
        valid = [(v, y) for v, y in pairs if y is not None]
        dropped = len(pairs) - len(valid)
    elif of in tuning_results:
        source = tuning_results[of]
        if isinstance(source, list):
            # No paired x-axis; ops that need one (segment_slope) must
            # reference a base metric. We catch that branch per-op.
            valid = [(None, y) for y in source if y is not None]
            dropped = len(source) - len(valid)
        else:
            return None, (
                f"of {of!r} is a scalar tuning result; only list-valued "
                "sources (e.g. swept_segment_slope) can feed another op"
            )
    else:
        # Should be caught by _resolve_tuning_order, but defensive.
        return None, f"of {of!r} not declared in this block"

    # R2 (2026-05-19, claude P3 #3): source-type gate runs BEFORE the
    # count gate. A swept_segment_slope misconfigured with a tuning-op
    # source AND fewer than 2 points used to surface as "only N valid
    # points" — actionable but pointing at the wrong dial. The
    # source-type error is the real cause; once it's fixed the count
    # gate may pass.
    if op == "swept_segment_slope" and of not in base_metric_names:
        return None, (
            "swept_segment_slope needs a base §2 metric source for "
            "the x-axis (Vctrl); cannot chain off another tuning op"
        )

    min_required = 2 if op == "swept_segment_slope" else 1
    if len(valid) < min_required:
        return None, (
            f"only {len(valid)} valid points after dropping {dropped} None "
            f"(need ≥ {min_required} for op {op})"
        )

    if op == "swept_max_minus_min":
        ys = [y for _, y in valid]
        return float(max(ys) - min(ys)), None

    if op == "swept_segment_slope":
        slopes: list[float] = []
        for (v0, y0), (v1, y1) in zip(valid, valid[1:]):
            dv = float(v1) - float(v0)
            if dv == 0.0:
                return None, (
                    f"duplicate Vctrl {v0}; segment slope undefined"
                )
            slopes.append((float(y1) - float(y0)) / dv)
        return slopes, None

    if op == "swept_ratio_max_over_min":
        abs_ys = [abs(float(y)) for _, y in valid]
        mx, mn = max(abs_ys), min(abs_ys)
        if mn == 0.0:
            return None, "min |value| is 0; ratio undefined"
        return mx / mn, None

    if op == "swept_same_sign":
        ys = [float(y) for _, y in valid]
        all_pos = all(y > 0 for y in ys)
        all_neg = all(y < 0 for y in ys)
        return bool(all_pos or all_neg), None

    # Unreachable — validator gates this.
    return None, f"unknown swept op {op!r}"


def _worst_case_for_pass(value: Any, pass_range: Any) -> Any:
    """For list-valued ops, return the element worst-positioned against
    the pass range so `_verdict` flags a single bad segment as FAIL
    rather than letting it hide inside the list. Scalars / bools pass
    through unchanged. ``None`` short-circuits to None so the
    UNMEASURABLE path stays clean.
    """
    if value is None or not isinstance(value, list):
        return value
    if not value:
        return None
    if pass_range is None:
        return value[0]
    lo, hi = pass_range
    # Worst-case: the element farthest outside [lo, hi]. If none are
    # outside, return any in-range element (the first) so the verdict
    # falls through to PASS.
    worst = value[0]
    worst_margin = 0.0
    for v in value:
        margin = 0.0
        if lo is not None and v < lo:
            margin = max(margin, lo - v)
        if hi is not None and v > hi:
            margin = max(margin, v - hi)
        if margin > worst_margin:
            worst_margin = margin
            worst = v
    return worst
