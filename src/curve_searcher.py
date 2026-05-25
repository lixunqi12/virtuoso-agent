"""LC_VCO curve-level candidate searcher (Path-3 prep, 2026-05-24).

After a Vctrl sweep has produced ``tuning_measurements`` /
``tuning_pass_fail`` but BEFORE the next-iteration LLM prompt is sent,
this module generates a bounded ranked list of variable-delta
CANDIDATES (directional suggestions) targeting the worst-violating
tuning metric, and formats them into a structured summary the agent
can append to the prompt.

PDK-safe boundaries (enforced by construction, asserted in tests):
    - Pure Python; no SafeBridge / OCEAN / SKILL / Spectre / SSH calls.
    - No waveform / raw payload fetch; consumes only data the agent
      already holds (per-point §2 measurements, tuning_measurements,
      live design_vars).
    - Variable proposals are intersected with the live ``design_vars``
      keys — never invents a variable the design doesn't expose.
    - Engineering-suffix family (f / p / n / u / m / k) is preserved
      so the OCEAN-side rewriter stays format-stable.
    - Summary text is pure metric/curve content — never echoes foundry
      cell prefixes / PDK file paths (asserted by
      :func:`assert_no_foundry_leak`).

Optional and off by default at the agent level; this module is inert
unless ``CircuitAgent.run(..., curve_searcher_enabled=True, ...)``.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

logger = logging.getLogger(__name__)


LC_VCO_PRIMARY_VARS: tuple[str, ...] = ("C", "L", "nfin_cc")

DEFAULT_MAX_CANDIDATES = 6

_SUFFIX_TO_MULT: dict[str, float] = {
    "f": 1e-15,
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "k": 1e3,
}

_NUM_SUFFIX_RE = re.compile(
    r"^\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([fpnumk]?)\s*$",
)

# Mirrors safe_bridge._FOUNDRY_LEAK_RE token list (kept independent so
# this module does not import SafeBridge; the safety test cross-checks).
_FOUNDRY_LEAK_TOKENS: tuple[str, ...] = (
    "nch_", "pch_", "cfmom", "rppoly", "rm1_", "tsmc", "tcbn",
    "rxnp", "vsubs",
)

# Substrings whose appearance in summaries would indicate raw-waveform
# escalation requests (forbidden — Path-2 boundary, see HANDOFF §4).
_WAVEFORM_FETCH_TOKENS: tuple[str, ...] = (
    ".tran", "readraw", "displayraw", "savewaveform",
)


def _parse_eng_value(text: Any) -> tuple[float | None, str]:
    """Return ``(numeric_value, original_suffix)``.

    Numbers pass through with empty suffix. Strings like ``"222f"`` /
    ``"265p"`` / ``"10k"`` parse to (SI float, suffix). Anything that
    does not match the eng-numeric grammar returns ``(None, "")`` so
    callers can skip the variable without crashing.
    """
    if isinstance(text, bool):
        return None, ""
    if isinstance(text, (int, float)):
        return float(text), ""
    if not isinstance(text, str):
        return None, ""
    m = _NUM_SUFFIX_RE.match(text)
    if not m:
        return None, ""
    base = float(m.group(1))
    suffix = m.group(2)
    if suffix and suffix not in _SUFFIX_TO_MULT:
        return None, ""
    scaled = base * _SUFFIX_TO_MULT.get(suffix, 1.0)
    return scaled, suffix


def _format_eng_value(value: float, suffix: str) -> str:
    """Re-emit ``value`` in the same engineering-suffix family used by
    the input — keeps OCEAN's var-rewrite path format-stable.
    """
    mult = _SUFFIX_TO_MULT.get(suffix, 1.0) if suffix else 1.0
    return f"{value / mult:g}{suffix}"


@dataclass(frozen=True)
class Candidate:
    """A single proposed design_var delta.

    ``score`` is a heuristic predictive score (higher = more promising);
    the candidate is NOT executed here — execution belongs to the next
    iteration of the agent loop via the LLM-emitted ``design_vars``.
    """
    var: str
    old_value: str
    new_value: str
    factor: float
    reason: str
    score: float
    targets: tuple[str, ...]


@dataclass
class CurveSummary:
    """Structured summary the agent passes to the LLM."""
    vctrl: list[float]
    f_GHz: list[float | None]
    kvco_segments_MHz_per_V: list[float]
    tuning_measurements: dict[str, Any]
    tuning_pass_fail: dict[str, str]
    worst_violations: list[str]
    candidates: list[Candidate]
    sensitivity: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_markdown(self) -> str:
        lines: list[str] = ["## Curve-level searcher (Path-3 prep)"]
        lines.append("### f-Vctrl curve")
        for v, f in zip(self.vctrl, self.f_GHz):
            shown = "—" if not isinstance(f, (int, float)) else f"{float(f):.4f}"
            lines.append(f"  - Vctrl={v:.3f} V → f_osc={shown} GHz")
        if self.kvco_segments_MHz_per_V:
            lines.append("### Kvco segments (MHz/V)")
            for i, k in enumerate(self.kvco_segments_MHz_per_V):
                lines.append(f"  - seg{i}: {k:.1f}")
        if self.worst_violations:
            lines.append("### Worst violations")
            for w in self.worst_violations:
                lines.append(f"  - {w}")
        if self.sensitivity:
            lines.append("### Last-change sensitivity (observed dy/d ln var)")
            for var, by_metric in self.sensitivity.items():
                metric_strs = ", ".join(
                    f"{m}={d:+.3g}" for m, d in by_metric.items()
                )
                lines.append(f"  - {var}: {metric_strs}")
        if self.candidates:
            lines.append("### Ranked candidates (proposals only — not yet run)")
            for i, c in enumerate(self.candidates, 1):
                targets_str = ",".join(c.targets) or "—"
                lines.append(
                    f"  {i}. {c.var}: {c.old_value} → {c.new_value} "
                    f"(×{c.factor:g}) — {c.reason} "
                    f"[score={c.score:.2f}, targets: {targets_str}]"
                )
            lines.append(
                "Candidates are heuristic directional priors derived from "
                "the curve diagnostics above. Treat them as suggestions; "
                "emit different design_vars if your reasoning differs."
            )
        return "\n".join(lines) + "\n\n"


def _worst_violations(
    tuning_measurements: dict[str, Any],
    tuning_pass_fail: dict[str, str],
) -> list[str]:
    out: list[str] = []
    for name, verdict in tuning_pass_fail.items():
        if isinstance(verdict, str) and verdict.startswith("PASS"):
            continue
        value = tuning_measurements.get(name)
        if isinstance(value, list):
            nums = [v for v in value if isinstance(v, (int, float)) and not isinstance(v, bool)]
            value_s = "[" + ", ".join(f"{v:.3g}" for v in nums) + "]"
        elif isinstance(value, bool):
            value_s = str(value)
        elif isinstance(value, (int, float)):
            value_s = f"{float(value):.4g}"
        else:
            value_s = str(value)
        out.append(f"{name}={value_s} → {verdict}")
    return out


# Heuristic priors keyed by (metric_name, direction).
# Direction "high"/"low" comes from `_verdict()` output ("above"/"below").
# Earlier entries in each list rank higher (multiplicative score decay).
#
# Physics intuition (LC_VCO LC tank):
#   f₀ ≈ 1/(2π√(LC)); Kvco = ∂f₀/∂Vctrl ∝ varactor-d(Cvar)/dVctrl / C_total
#   - Kvco too HIGH → grow C_fixed (denominator), or grow L (lowers f₀
#     scale), narrowing relative Cvar swing.
#   - tuning_range too LOW → shrink C_fixed so the varactor pulls more
#     of the tank capacitance; bump L to recenter f₀ if needed.
#   - Kvco_linearity high (non-uniform) → adjust nfin_cc (cross-couple
#     transconductance affects loop gain shape across Vctrl).
#   - monotonic FAIL → firm up nfin_cc so the negative-gm cell keeps
#     the tank oscillating across the whole Vctrl range.
_PRIORS: dict[tuple[str, str], list[tuple[str, float, str]]] = {
    ("Kvco_MHz_per_V", "high"): [
        ("C", 1.25, "raise C → narrow varactor relative swing → lower Kvco"),
        ("L", 1.20, "raise L → lower f₀ scale → lower Kvco"),
        ("C", 0.80, "counter-prior: lower C if first move overshoots"),
    ],
    ("Kvco_MHz_per_V", "low"): [
        ("C", 0.80, "lower C → widen varactor relative swing → raise Kvco"),
        ("L", 0.83, "lower L → raise f₀ sensitivity"),
    ],
    ("tuning_range_GHz", "low"): [
        ("C", 0.75, "lower C → widen swing → wider tuning range"),
        ("L", 1.20, "raise L → recenter f₀ if range is offset"),
    ],
    ("tuning_range_GHz", "high"): [
        ("C", 1.25, "raise C → narrow swing → narrower tuning range"),
    ],
    ("Kvco_linearity", "high"): [
        ("nfin_cc", 1.5, "scale nfin_cc up → larger negative-gm cell → "
                         "more uniform gain vs Vctrl"),
        ("C", 1.10, "small bump in C → softens varactor curvature"),
        ("nfin_cc", 0.67, "counter-prior: scale nfin_cc down"),
    ],
    ("monotonic", "low"): [
        ("nfin_cc", 1.5, "scale nfin_cc up → firmer negative-gm → "
                         "monotonic Vctrl→f"),
        ("C", 1.10, "small C bump → reduce loop-cap instability"),
    ],
}


def _verdict_direction(value: Any, verdict: str) -> str | None:
    if isinstance(value, bool):
        return None if value else "low"
    if not isinstance(verdict, str):
        return None
    if "above" in verdict:
        return "high"
    if "below" in verdict:
        return "low"
    return None


def generate_candidates(
    tuning_measurements: dict[str, Any],
    tuning_pass_fail: dict[str, str],
    design_vars: dict[str, Any],
    *,
    primary_vars: Sequence[str] = LC_VCO_PRIMARY_VARS,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> list[Candidate]:
    """Return up to ``max_candidates`` ranked candidates.

    Empty list when ``max_candidates <= 0``, when ``design_vars`` has
    none of the primary vars, or when every verdict already PASSes.
    """
    if max_candidates <= 0:
        return []
    available = {v: design_vars[v] for v in primary_vars if v in design_vars}
    if not available:
        return []

    targets: list[tuple[str, str]] = []
    for name, verdict in tuning_pass_fail.items():
        if isinstance(verdict, str) and verdict.startswith("PASS"):
            continue
        value = tuning_measurements.get(name)
        direction = _verdict_direction(value, verdict if isinstance(verdict, str) else "")
        if direction is None:
            continue
        targets.append((name, direction))

    if not targets:
        return []

    by_key: dict[tuple[str, float], dict[str, Any]] = {}
    for metric, direction in targets:
        priors = _PRIORS.get((metric, direction), [])
        for rank, (var, factor, reason) in enumerate(priors):
            if var not in available:
                continue
            entry = by_key.setdefault(
                (var, factor),
                {"reasons": [], "score": 0.0, "targets": []},
            )
            entry["score"] += max(0.2, 1.0 - 0.2 * rank)
            entry["reasons"].append(reason)
            entry["targets"].append(metric)

    candidates: list[Candidate] = []
    for (var, factor), info in by_key.items():
        old_raw = available[var]
        value, suffix = _parse_eng_value(old_raw)
        if value is None or value == 0.0:
            logger.debug(
                "curve_searcher: skipping %s — unparseable value %r",
                var, old_raw,
            )
            continue
        new_str = _format_eng_value(value * factor, suffix)
        candidates.append(Candidate(
            var=var,
            old_value=str(old_raw),
            new_value=new_str,
            factor=factor,
            reason=info["reasons"][0],
            score=info["score"],
            targets=tuple(sorted(set(info["targets"]))),
        ))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:max_candidates]


def _scalar_for_sensitivity(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list) and value:
        nums = [v for v in value if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if nums:
            return float(max(nums))
    return None


def compute_sensitivity(
    prev_design_vars: dict[str, Any] | None,
    prev_tuning_measurements: dict[str, Any] | None,
    cur_design_vars: dict[str, Any],
    cur_tuning_measurements: dict[str, Any],
    *,
    primary_vars: Sequence[str] = LC_VCO_PRIMARY_VARS,
) -> dict[str, dict[str, float]]:
    """Observed dy/d(ln var) over the last iter pair.

    Restricted to ``primary_vars`` that actually changed between the
    two iterations. Returns ``{}`` when no usable pair exists.
    """
    if not prev_design_vars or not prev_tuning_measurements:
        return {}
    out: dict[str, dict[str, float]] = {}
    for var in primary_vars:
        if var not in prev_design_vars or var not in cur_design_vars:
            continue
        prev_val, _ = _parse_eng_value(prev_design_vars[var])
        cur_val, _ = _parse_eng_value(cur_design_vars[var])
        if prev_val is None or cur_val is None:
            continue
        if prev_val <= 0.0 or cur_val <= 0.0 or prev_val == cur_val:
            continue
        d_log = math.log(cur_val) - math.log(prev_val)
        if d_log == 0.0:
            continue
        per_metric: dict[str, float] = {}
        for metric, cur_y in cur_tuning_measurements.items():
            prev_y = prev_tuning_measurements.get(metric)
            cur_num = _scalar_for_sensitivity(cur_y)
            prev_num = _scalar_for_sensitivity(prev_y)
            if cur_num is None or prev_num is None:
                continue
            per_metric[metric] = (cur_num - prev_num) / d_log
        if per_metric:
            out[var] = per_metric
    return out


def build_summary(
    vctrl_values: Sequence[float],
    base_measurements_per_point: Sequence[dict[str, Any]],
    tuning_measurements: dict[str, Any],
    tuning_pass_fail: dict[str, str],
    design_vars: dict[str, Any],
    *,
    prev_design_vars: dict[str, Any] | None = None,
    prev_tuning_measurements: dict[str, Any] | None = None,
    f_metric_name: str = "f_osc_GHz",
    kvco_metric_name: str = "Kvco_MHz_per_V",
    primary_vars: Sequence[str] = LC_VCO_PRIMARY_VARS,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> CurveSummary:
    """Top-level entry point. Returns a :class:`CurveSummary`."""
    f_curve: list[float | None] = []
    for m in base_measurements_per_point:
        if isinstance(m, dict):
            v = m.get(f_metric_name)
            f_curve.append(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None)
        else:
            f_curve.append(None)
    kvco_raw = tuning_measurements.get(kvco_metric_name)
    kvco_segs: list[float] = (
        [float(k) for k in kvco_raw if isinstance(k, (int, float)) and not isinstance(k, bool)]
        if isinstance(kvco_raw, list) else []
    )
    violations = _worst_violations(tuning_measurements, tuning_pass_fail)
    candidates = generate_candidates(
        tuning_measurements, tuning_pass_fail, design_vars,
        primary_vars=primary_vars, max_candidates=max_candidates,
    )
    sensitivity = compute_sensitivity(
        prev_design_vars, prev_tuning_measurements,
        design_vars, tuning_measurements,
        primary_vars=primary_vars,
    )
    return CurveSummary(
        vctrl=[float(v) for v in vctrl_values],
        f_GHz=f_curve,
        kvco_segments_MHz_per_V=kvco_segs,
        tuning_measurements=dict(tuning_measurements),
        tuning_pass_fail=dict(tuning_pass_fail),
        worst_violations=violations,
        candidates=candidates,
        sensitivity=sensitivity,
    )


def assert_no_foundry_leak(text: str) -> None:
    """Raise ``ValueError`` if ``text`` carries a foundry token or a
    raw-waveform escalation token. Used by the safety test; production
    callers may use it as a defensive postcondition before logging."""
    if not isinstance(text, str):
        return
    lowered = text.lower()
    for tok in _FOUNDRY_LEAK_TOKENS:
        if tok in lowered:
            raise ValueError(
                f"curve_searcher: text contains forbidden foundry "
                f"token {tok!r}"
            )
    for tok in _WAVEFORM_FETCH_TOKENS:
        if tok in lowered:
            raise ValueError(
                f"curve_searcher: text contains forbidden raw-waveform "
                f"escalation token {tok!r}"
            )
