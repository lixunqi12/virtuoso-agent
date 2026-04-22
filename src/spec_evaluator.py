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
    """
    if not isinstance(spec_text, str):
        return None
    for match in _YAML_FENCE_RE.finditer(spec_text):
        try:
            data = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            logger.warning("spec eval: skipping malformed yaml fence: %s", exc)
            continue
        if not isinstance(data, dict):
            continue
        if not _REQUIRED_KEYS.issubset(data.keys()):
            continue
        validate_eval_block(data)
        return data
    return None


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
    """Rev 5: three-state verdict.

    UNMEASURABLE conditions (in precedence order):
        1. value is None and we captured a reason
        2. value is not None but outside ``sanity_range`` (physically
           implausible — blame measurement, not circuit)

    Otherwise FAIL / PASS on pass_range membership.
    """
    if value is None:
        return f"UNMEASURABLE ({reason or 'no value'})"

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
