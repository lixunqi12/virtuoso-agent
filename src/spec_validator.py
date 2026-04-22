"""Spec static-feasibility validator (Tier 3 guardrail, rev 5 2026-04-19).

Purpose
-------
Before the agent loop even touches the simulator, statically check that
every metric in the ``signals/windows/metrics`` yaml block can, in
principle, produce a value that satisfies the ``pass`` range given the
physical bounds declared on each signal.

The classic failure this catches: ``t_startup`` declared as
``threshold = 0.9 * ptp`` with ``use_abs: true``. Since ``max(abs(x))
= ptp(x)/2`` for any zero-mean signal, the threshold is at most
``2*max(abs)`` (never < max(abs)) — so ``safeOceanTCross`` can NEVER
find a crossing regardless of circuit behavior. The PC spec eval
returned "No crossing found" five iterations in a row while Kimi
tried to "fix" an electrically-healthy VCO.

This module walks every metric and reports infeasibilities *before*
any OCEAN round-trip. Callers decide whether to warn-only or hard-fail.

Scope
-----
- Only relies on fields already declared in the yaml block (signals
  carry optional ``bounds: {max_abs, ptp_max, min, max}``; metrics
  carry optional ``sanity: [lo, hi]`` and the existing ``pass: [lo,
  hi]``).
- Does NOT inspect circuit topology / netlist — that's out of scope.
- Warnings only, no side effects. ``validate_spec_feasibility``
  returns a list of human-readable issue strings; empty list means the
  spec is feasible under the declared bounds.

Signal-bound → stat-upper-bound math
------------------------------------
For a signal ``x`` with declared ``max_abs = A`` (i.e. ``|x(t)| <= A``
for all t in any window):

    stat    | upper bound on stat(x)        | lower bound
    --------+-------------------------------+-------------------
    mean    | A                             | -A
    min     | A                             | -A
    max     | A                             | -A
    ptp     | ptp_max (explicit) or 2A      | 0
    rms     | A                             | 0
    mean_abs| A                             | 0
    freq_Hz | (no generic bound)            | 0

If a signal has ``ptp_max`` declared, it's used verbatim. Otherwise
``ptp_max`` is conservatively inferred as ``2 * max_abs``.

For ``t_cross_frac`` with ``use_abs: true``, the crossing signal
satisfies ``abs(signal) <= max_abs(signal)``; so a threshold >
max_abs is unreachable.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- #
#  Bound math
# --------------------------------------------------------------------- #

def _signal_bounds_of(block: dict, name: str) -> dict[str, float]:
    for s in block.get("signals", []):
        if s.get("name") == name:
            return s.get("bounds") or {}
    return {}


def _max_abs(bounds: dict) -> float | None:
    v = bounds.get("max_abs")
    return float(v) if isinstance(v, (int, float)) else None


def _ptp_max(bounds: dict) -> float | None:
    v = bounds.get("ptp_max")
    if isinstance(v, (int, float)):
        return float(v)
    ma = _max_abs(bounds)
    return 2.0 * ma if ma is not None else None


def _stat_upper(stat: str, bounds: dict) -> float | None:
    """Conservative upper bound on ``stat(signal)`` given declared bounds.

    Returns ``None`` when the bound cannot be derived (no declaration,
    or stat is one we don't model).
    """
    if stat == "ptp":
        return _ptp_max(bounds)
    if stat in ("mean", "min", "max", "rms", "mean_abs"):
        return _max_abs(bounds)
    # freq_Hz / duty_pct have domain-specific bounds that aren't
    # currently declarable; skip them.
    return None


def _stat_lower(stat: str, bounds: dict) -> float | None:
    if stat in ("ptp", "rms", "mean_abs"):
        return 0.0
    if stat in ("mean", "min", "max"):
        ma = _max_abs(bounds)
        return -ma if ma is not None else None
    return None


# --------------------------------------------------------------------- #
#  Per-metric feasibility checks
# --------------------------------------------------------------------- #

def _check_simple_metric(m: dict, block: dict) -> list[str]:
    """Simple stat metric: value lies in [stat_lower, stat_upper] after
    scale. Check that pass range is reachable in that interval."""
    issues: list[str] = []
    scale = float(m.get("scale", 1.0))
    bounds = _signal_bounds_of(block, m["signal"])
    if not bounds:
        return issues
    up = _stat_upper(m["stat"], bounds)
    lo = _stat_lower(m["stat"], bounds)
    pr = m.get("pass")
    if pr is None:
        return issues
    p_lo, p_hi = pr
    if up is not None and p_lo is not None:
        max_scaled = up * scale if scale >= 0 else lo * scale if lo is not None else None
        if max_scaled is not None and p_lo > max_scaled + 1e-18:
            issues.append(
                f"metric {m['name']!r}: pass lo {p_lo:g} exceeds stat "
                f"upper bound {max_scaled:g} (stat={m['stat']}, "
                f"signal={m['signal']}, scale={scale:g}) — always FAIL"
            )
    if lo is not None and p_hi is not None:
        min_scaled = lo * scale if scale >= 0 else up * scale if up is not None else None
        if min_scaled is not None and p_hi < min_scaled - 1e-18:
            issues.append(
                f"metric {m['name']!r}: pass hi {p_hi:g} below stat "
                f"lower bound {min_scaled:g} — always FAIL"
            )
    return issues


def _check_ratio_metric(m: dict, block: dict) -> list[str]:
    """Ratio metric: if both num/den have bounds and denominator could be
    zero without guarding, warn. (Light touch — ratios are hard to
    statically bound without signal sign info.)"""
    issues: list[str] = []
    den_spec = m["denominator"]
    den_bounds = _signal_bounds_of(block, den_spec["signal"])
    # Flag only when declared lower bound includes zero AND stat can be
    # zero (rms of a zero signal, etc.). Shallow check — we can't tell
    # if the circuit actually hits zero at this point.
    stat = den_spec["stat"]
    if stat in ("rms", "mean_abs", "ptp") and den_bounds:
        lo = _stat_lower(stat, den_bounds)
        if lo is not None and lo <= 0.0:
            # Not necessarily an issue at runtime; evaluator guards
            # divide-by-zero by returning UNMEASURABLE. Informational
            # warning only so spec authors know this metric may go
            # UNMEASURABLE on a dead circuit.
            logger.debug(
                "metric %s: ratio denominator stat %s can be 0; "
                "will return UNMEASURABLE on quiescent tank",
                m["name"], stat,
            )
    return issues


def _check_t_cross_frac_metric(m: dict, block: dict) -> list[str]:
    """t_cross_frac with use_abs: true — threshold = frac * ref_stat.

    Critical check: threshold must be reachable by the cross signal's
    envelope. With ``use_abs: true`` the envelope is
    ``max_abs(cross_signal)``. Without use_abs, the envelope is still
    ``max_abs`` for bi-polar signals.
    """
    issues: list[str] = []
    frac = float(m.get("frac", 0.0))
    ref_spec = m["ref"]
    ref_bounds = _signal_bounds_of(block, ref_spec["signal"])
    cross_bounds = _signal_bounds_of(block, m["signal"])

    ref_stat_up = _stat_upper(ref_spec["stat"], ref_bounds)
    cross_abs_up = _max_abs(cross_bounds)

    if ref_stat_up is None or cross_abs_up is None:
        # One of the two bounds not declared — can't check; advise.
        missing: list[str] = []
        if ref_stat_up is None:
            missing.append(f"signal {ref_spec['signal']!r} needs bounds to check ref stat upper")
        if cross_abs_up is None:
            missing.append(f"signal {m['signal']!r} needs bounds.max_abs to check reachability")
        logger.info(
            "metric %s: cannot check t_cross feasibility — %s",
            m["name"], "; ".join(missing),
        )
        return issues

    threshold_up = frac * ref_stat_up
    # Upper bound of what threshold could be this run
    if threshold_up > cross_abs_up + 1e-18:
        issues.append(
            f"metric {m['name']!r}: threshold = frac*ref_stat can be "
            f"as high as {frac:g} * {ref_stat_up:g} = {threshold_up:g}, "
            f"but |{m['signal']}| can never exceed {cross_abs_up:g} "
            f"(frac={frac}, ref.stat={ref_spec['stat']}, "
            f"use_abs={m.get('use_abs', False)}) — this is the classic "
            "ptp-vs-amplitude bug; use frac=0.45 when ref.stat=ptp to "
            "mean '90% of amplitude' or switch ref.stat to a half-ptp "
            "measure."
        )
    return issues


def _check_sanity_contains_pass(m: dict) -> list[str]:
    """``sanity`` must contain ``pass``; otherwise a legitimate PASS
    value would be flagged UNMEASURABLE."""
    issues: list[str] = []
    pr = m.get("pass")
    sr = m.get("sanity")
    if pr is None or sr is None:
        return issues
    p_lo, p_hi = pr
    s_lo, s_hi = sr
    if s_lo is not None and p_lo is not None and s_lo > p_lo:
        issues.append(
            f"metric {m['name']!r}: sanity lo {s_lo:g} > pass lo "
            f"{p_lo:g}; a legitimate PASS would be flagged UNMEASURABLE"
        )
    if s_hi is not None and p_hi is not None and s_hi < p_hi:
        issues.append(
            f"metric {m['name']!r}: sanity hi {s_hi:g} < pass hi "
            f"{p_hi:g}; a legitimate PASS would be flagged UNMEASURABLE"
        )
    return issues


# --------------------------------------------------------------------- #
#  Public API
# --------------------------------------------------------------------- #

def validate_spec_feasibility(block: dict) -> list[str]:
    """Run all static feasibility checks on an already-parsed block.

    Returns a list of human-readable issue strings. Empty list means
    the spec is feasible under declared bounds. Callers can choose
    warn-only (log each issue) or hard-fail (raise) behaviour.
    """
    issues: list[str] = []
    for m in block.get("metrics", []):
        issues.extend(_check_sanity_contains_pass(m))
        compound = m.get("compound")
        if compound is None:
            issues.extend(_check_simple_metric(m, block))
        elif compound == "ratio":
            issues.extend(_check_ratio_metric(m, block))
        elif compound == "t_cross_frac":
            issues.extend(_check_t_cross_frac_metric(m, block))
    return issues


def log_feasibility_report(block: dict, strict: bool = False) -> int:
    """Helper for run_agent.py: run checks, log each issue, return
    count. If ``strict`` and count > 0, the caller should abort."""
    issues = validate_spec_feasibility(block)
    if not issues:
        logger.info(
            "Spec static validator: 0 feasibility issues across %d metrics",
            len(block.get("metrics", [])),
        )
        return 0
    for issue in issues:
        if strict:
            logger.error("spec-validator: %s", issue)
        else:
            logger.warning("spec-validator: %s", issue)
    logger.warning(
        "Spec static validator: %d feasibility issue(s) — see warnings above",
        len(issues),
    )
    return len(issues)
