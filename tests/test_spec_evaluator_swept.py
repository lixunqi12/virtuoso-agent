"""Unit tests for ``spec_evaluator.evaluate_swept`` (path-2, 2026-05-19).

Pure-Python: builds eval blocks in code, hands synthetic per-point
measurements to ``evaluate_swept``, asserts the four swept ops behave
correctly across the documented good / degenerate inputs. No bridge,
no SKILL, no Maestro — the schema gates exercised here also cover
``validate_eval_block`` and ``extract_eval_block`` (the spec.md fence
splice + cycle detection) so a future ops author doesn't regress them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src import spec_evaluator  # noqa: E402


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _base_block(metrics: list[dict] | None = None) -> dict:
    """Build a minimal block with the §2 plumbing the schema requires
    so tuning_metrics validation can still resolve `of:` references.

    The `sweep:` block is included by default because the schema gates
    pair it with `tuning_metrics:`; tests that don't care about its
    contents can leave it as-is.
    """
    return {
        "signals": [
            {"name": "Vdiff", "kind": "Vdiff",
             "paths": ["/Vout_p", "/Vout_n"]},
        ],
        "windows": {"late": [1.5e-7, 2.0e-7]},
        "metrics": metrics if metrics is not None else [
            {"name": "f_osc_GHz", "signal": "Vdiff",
             "window": "late", "stat": "freq_Hz", "scale": 1.0e-9,
             "pass": [19.5, 20.5], "sanity": [0.1, 100.0]},
        ],
        "sweep": {
            "variable": "Vctrl",
            "range": [0.0, 0.8],
            "points": 9,
            "unit": "V",
        },
    }


def _per_point(values: list[float | None]) -> list[dict]:
    return [
        {} if v is None else {"f_osc_GHz": v}
        for v in values
    ]


# --------------------------------------------------------------------------
# swept_max_minus_min — tuning range
# --------------------------------------------------------------------------

def test_swept_max_minus_min_pass():
    block = _base_block()
    block["tuning_metrics"] = [{
        "name": "tuning_range_GHz",
        "op": "swept_max_minus_min",
        "of": "f_osc_GHz",
        "pass": [3.0, None],
        "sanity": [0.0, 20.0],
    }]
    meas, pf = spec_evaluator.evaluate_swept(
        block,
        _per_point([20.0, 21.0, 22.0, 23.0, 24.0]),
        [0.0, 0.2, 0.4, 0.6, 0.8],
    )
    assert meas["tuning_range_GHz"] == pytest.approx(4.0)
    assert pf["tuning_range_GHz"] == "PASS"


def test_swept_max_minus_min_fail_below_band():
    block = _base_block()
    block["tuning_metrics"] = [{
        "name": "tuning_range_GHz",
        "op": "swept_max_minus_min",
        "of": "f_osc_GHz",
        "pass": [3.0, None],
        "sanity": [0.0, 20.0],
    }]
    meas, pf = spec_evaluator.evaluate_swept(
        block, _per_point([20.0, 20.5, 21.0]), [0.0, 0.4, 0.8],
    )
    assert meas["tuning_range_GHz"] == pytest.approx(1.0)
    assert pf["tuning_range_GHz"].startswith("FAIL")


def test_swept_max_minus_min_drops_missing_points():
    block = _base_block()
    block["tuning_metrics"] = [{
        "name": "tuning_range_GHz",
        "op": "swept_max_minus_min",
        "of": "f_osc_GHz",
        "pass": [3.0, None],
        "sanity": [0.0, 20.0],
    }]
    meas, pf = spec_evaluator.evaluate_swept(
        block,
        _per_point([20.0, None, 22.0, None, 24.0]),
        [0.0, 0.2, 0.4, 0.6, 0.8],
    )
    assert meas["tuning_range_GHz"] == pytest.approx(4.0)
    assert pf["tuning_range_GHz"] == "PASS"


# --------------------------------------------------------------------------
# swept_segment_slope — Kvco (df/dV between adjacent points)
# --------------------------------------------------------------------------

def test_segment_slope_uniform_pass():
    block = _base_block()
    block["tuning_metrics"] = [{
        "name": "Kvco_MHz_per_V",
        "op": "swept_segment_slope",
        "of": "f_osc_GHz",
        "scale": 1.0e3,
        "pass": [150.0, 1500.0],
        "sanity": [-5000.0, 5000.0],
    }]
    meas, pf = spec_evaluator.evaluate_swept(
        block,
        _per_point([20.0, 20.5, 21.0, 21.5]),
        [0.0, 0.2, 0.4, 0.6],
    )
    # 0.5 GHz / 0.2 V = 2500 MHz/V — wait, that's above pass.hi 1500.
    # Pick numbers that actually land in band:
    meas, pf = spec_evaluator.evaluate_swept(
        block,
        _per_point([20.0, 20.04, 20.08, 20.12]),
        [0.0, 0.2, 0.4, 0.6],
    )
    # 0.04 GHz / 0.2 V * 1000 = 200 MHz/V × 3 segments — uniform 200.
    assert all(abs(k - 200.0) < 1e-9 for k in meas["Kvco_MHz_per_V"])
    assert pf["Kvco_MHz_per_V"] == "PASS"


def test_segment_slope_one_segment_over_band_fails():
    block = _base_block()
    block["tuning_metrics"] = [{
        "name": "Kvco_MHz_per_V",
        "op": "swept_segment_slope",
        "of": "f_osc_GHz",
        "scale": 1.0e3,
        "pass": [150.0, 1500.0],
        "sanity": [-5000.0, 5000.0],
    }]
    # 3 segments uniformly 200 MHz/V except the last bumps to 2000.
    meas, pf = spec_evaluator.evaluate_swept(
        block,
        _per_point([20.0, 20.04, 20.08, 20.48]),
        [0.0, 0.2, 0.4, 0.6],
    )
    slopes = meas["Kvco_MHz_per_V"]
    assert slopes[0] == pytest.approx(200.0)
    assert slopes[-1] == pytest.approx(2000.0)
    # Worst case 2000 > 1500 → FAIL
    assert pf["Kvco_MHz_per_V"].startswith("FAIL")


def test_segment_slope_segment_in_sanity_violation():
    """One segment outside sanity → UNMEASURABLE (suspect) for worst-case."""
    block = _base_block()
    block["tuning_metrics"] = [{
        "name": "Kvco_MHz_per_V",
        "op": "swept_segment_slope",
        "of": "f_osc_GHz",
        "scale": 1.0e3,
        "pass": [150.0, 1500.0],
        "sanity": [-5000.0, 5000.0],
    }]
    # 8000 MHz/V segment is above sanity hi 5000.
    meas, pf = spec_evaluator.evaluate_swept(
        block,
        _per_point([20.0, 20.04, 21.64, 21.68]),
        [0.0, 0.2, 0.4, 0.6],
    )
    assert pf["Kvco_MHz_per_V"].startswith("UNMEASURABLE")
    assert "suspect" in pf["Kvco_MHz_per_V"]


def test_segment_slope_requires_two_points():
    block = _base_block()
    block["tuning_metrics"] = [{
        "name": "Kvco_MHz_per_V",
        "op": "swept_segment_slope",
        "of": "f_osc_GHz",
        "scale": 1.0e3,
        "pass": [150.0, 1500.0],
        "sanity": [-5000.0, 5000.0],
    }]
    meas, pf = spec_evaluator.evaluate_swept(
        block,
        _per_point([20.0, None, None]),
        [0.0, 0.2, 0.4],
    )
    assert meas["Kvco_MHz_per_V"] is None
    assert pf["Kvco_MHz_per_V"].startswith("UNMEASURABLE")


# --------------------------------------------------------------------------
# swept_ratio_max_over_min — Kvco linearity (depends on prior tuning op)
# --------------------------------------------------------------------------

def test_ratio_max_over_min_chained():
    block = _base_block()
    block["tuning_metrics"] = [
        {
            "name": "Kvco_MHz_per_V",
            "op": "swept_segment_slope",
            "of": "f_osc_GHz",
            "scale": 1.0e3,
            "pass": [150.0, 1500.0],
            "sanity": [-5000.0, 5000.0],
        },
        {
            "name": "Kvco_linearity",
            "op": "swept_ratio_max_over_min",
            "of": "Kvco_MHz_per_V",
            "pass": [None, 3.0],
            "sanity": [1.0, 50.0],
        },
    ]
    # Slopes will be 200, 200, 800 → max/min = 4.0 → FAIL ≤3.0
    meas, pf = spec_evaluator.evaluate_swept(
        block,
        _per_point([20.0, 20.04, 20.08, 20.24]),
        [0.0, 0.2, 0.4, 0.6],
    )
    assert meas["Kvco_linearity"] == pytest.approx(4.0)
    assert pf["Kvco_linearity"].startswith("FAIL")


def test_ratio_max_over_min_uniform_pass():
    block = _base_block()
    block["tuning_metrics"] = [
        {
            "name": "Kvco_MHz_per_V",
            "op": "swept_segment_slope",
            "of": "f_osc_GHz",
            "scale": 1.0e3,
            "pass": [150.0, 1500.0],
            "sanity": [-5000.0, 5000.0],
        },
        {
            "name": "Kvco_linearity",
            "op": "swept_ratio_max_over_min",
            "of": "Kvco_MHz_per_V",
            "pass": [None, 3.0],
            "sanity": [1.0, 50.0],
        },
    ]
    meas, pf = spec_evaluator.evaluate_swept(
        block,
        _per_point([20.0, 20.04, 20.08, 20.12]),
        [0.0, 0.2, 0.4, 0.6],
    )
    assert meas["Kvco_linearity"] == pytest.approx(1.0)
    assert pf["Kvco_linearity"] == "PASS"


# --------------------------------------------------------------------------
# swept_same_sign — monotonicity (bool result)
# --------------------------------------------------------------------------

def test_same_sign_monotonic_up_passes():
    block = _base_block()
    block["tuning_metrics"] = [
        {
            "name": "Kvco_MHz_per_V",
            "op": "swept_segment_slope",
            "of": "f_osc_GHz",
            "scale": 1.0e3,
            "pass": [-5000.0, 5000.0],
            "sanity": [-5000.0, 5000.0],
        },
        {
            "name": "monotonic",
            "op": "swept_same_sign",
            "of": "Kvco_MHz_per_V",
            "pass": [True, True],
            "sanity": [False, True],
        },
    ]
    meas, pf = spec_evaluator.evaluate_swept(
        block,
        _per_point([20.0, 21.0, 22.0, 23.0]),
        [0.0, 0.2, 0.4, 0.6],
    )
    assert meas["monotonic"] is True
    assert pf["monotonic"] == "PASS"


def test_same_sign_non_monotonic_fails():
    block = _base_block()
    block["tuning_metrics"] = [
        {
            "name": "Kvco_MHz_per_V",
            "op": "swept_segment_slope",
            "of": "f_osc_GHz",
            "scale": 1.0e3,
            "pass": [-5000.0, 5000.0],
            "sanity": [-5000.0, 5000.0],
        },
        {
            "name": "monotonic",
            "op": "swept_same_sign",
            "of": "Kvco_MHz_per_V",
            "pass": [True, True],
            "sanity": [False, True],
        },
    ]
    # Up-down-up → mixed signs → not monotonic
    meas, pf = spec_evaluator.evaluate_swept(
        block,
        _per_point([20.0, 21.0, 20.5, 22.0]),
        [0.0, 0.2, 0.4, 0.6],
    )
    assert meas["monotonic"] is False
    assert pf["monotonic"].startswith("FAIL")


# --------------------------------------------------------------------------
# Schema validation — cycles, dangling refs, bool vs numeric pass
# --------------------------------------------------------------------------

def test_validate_rejects_cycle_in_tuning_of():
    block = _base_block()
    block["tuning_metrics"] = [
        {
            "name": "a", "op": "swept_max_minus_min", "of": "b",
            "pass": [0.0, None], "sanity": [0.0, 100.0],
        },
        {
            "name": "b", "op": "swept_max_minus_min", "of": "a",
            "pass": [0.0, None], "sanity": [0.0, 100.0],
        },
    ]
    with pytest.raises(ValueError, match=r"(?i)cycle|cyclic"):
        spec_evaluator.validate_eval_block(block)


def test_validate_rejects_dangling_of():
    block = _base_block()
    block["tuning_metrics"] = [{
        "name": "lonely",
        "op": "swept_max_minus_min",
        "of": "does_not_exist",
        "pass": [0.0, None],
        "sanity": [0.0, 100.0],
    }]
    with pytest.raises(ValueError, match=r"(?i)does_not_exist|dangling|unknown"):
        spec_evaluator.validate_eval_block(block)


def test_validate_rejects_pass_lo_gt_hi():
    block = _base_block()
    block["tuning_metrics"] = [{
        "name": "tuning_range_GHz",
        "op": "swept_max_minus_min",
        "of": "f_osc_GHz",
        "pass": [10.0, 5.0],
        "sanity": [0.0, 100.0],
    }]
    with pytest.raises(ValueError, match=r"(?i)lo > hi|lo.*hi"):
        spec_evaluator.validate_eval_block(block)


def test_validate_rejects_unknown_op():
    block = _base_block()
    block["tuning_metrics"] = [{
        "name": "foo",
        "op": "swept_polynomial_fit",
        "of": "f_osc_GHz",
        "pass": [0.0, None],
        "sanity": [0.0, 100.0],
    }]
    with pytest.raises(ValueError, match=r"(?i)op .*not in"):
        spec_evaluator.validate_eval_block(block)


def test_validate_rejects_numeric_pass_for_same_sign():
    block = _base_block()
    block["tuning_metrics"] = [{
        "name": "monotonic",
        "op": "swept_same_sign",
        "of": "f_osc_GHz",
        "pass": [0.0, 1.0],
        "sanity": [False, True],
    }]
    with pytest.raises(ValueError):
        spec_evaluator.validate_eval_block(block)


# --------------------------------------------------------------------------
# R2 (2026-05-19, codex P2 BLOCKER) — bool in sweep.range silently coerces
# to 0.0/1.0 because ``isinstance(False, int)`` is True. YAML ``range:
# [false, true]`` would round-trip to a 9-entry manifest spanning the
# wrong control voltage. Reject explicitly before the numeric/finite check.
# --------------------------------------------------------------------------

# The sweep validator is only reached when ``tuning_metrics`` is also
# present (the schema pairs them). All P2 tests attach a minimal valid
# tuning metric so the sweep block gets validated.
def _block_with_tuning(sweep_override: dict) -> dict:
    block = _base_block()
    block["sweep"] = sweep_override
    block["tuning_metrics"] = [{
        "name": "tuning_range_GHz",
        "op": "swept_max_minus_min",
        "of": "f_osc_GHz",
        "pass": [0.0, None],
        "sanity": [0.0, 100.0],
    }]
    return block


def test_validate_rejects_bool_in_sweep_range_lo():
    block = _block_with_tuning({
        "variable": "Vctrl", "range": [False, 0.8], "points": 9, "unit": "V",
    })
    with pytest.raises(ValueError, match=r"(?i)boolean|bool"):
        spec_evaluator.validate_eval_block(block)


def test_validate_rejects_bool_in_sweep_range_hi():
    block = _block_with_tuning({
        "variable": "Vctrl", "range": [0.0, True], "points": 9, "unit": "V",
    })
    with pytest.raises(ValueError, match=r"(?i)boolean|bool"):
        spec_evaluator.validate_eval_block(block)


def test_validate_rejects_both_bool_in_sweep_range():
    """The codex PoC: ``range: [false, true]`` previously coerced to
    ``[0.0, 1.0]`` and produced a bogus manifest. Must hard-fail at
    spec validation."""
    block = _block_with_tuning({
        "variable": "Vctrl", "range": [False, True], "points": 9, "unit": "V",
    })
    with pytest.raises(ValueError, match=r"(?i)boolean|bool"):
        spec_evaluator.validate_eval_block(block)


def test_validate_rejects_mixed_bool_and_float_in_sweep_range():
    """``[0.0, false]`` is the subtle failure mode — a real float as lo
    masks the bool in hi from a quick reader. Must still reject."""
    block = _block_with_tuning({
        "variable": "Vctrl", "range": [0.0, False], "points": 9, "unit": "V",
    })
    with pytest.raises(ValueError, match=r"(?i)boolean|bool"):
        spec_evaluator.validate_eval_block(block)


def test_validate_rejects_bool_in_sweep_points():
    """``points: True`` would round to 1 and break ``points >= 2``
    only after the bool reaches ``_derive_sweep_entries``. The points
    guard at the schema layer was already present (codex-2026-05-19),
    but pin it down with a dedicated test so it can't regress."""
    block = _block_with_tuning({
        "variable": "Vctrl", "range": [0.0, 0.8], "points": True, "unit": "V",
    })
    with pytest.raises(ValueError, match=r"(?i)int"):
        spec_evaluator.validate_eval_block(block)


# --------------------------------------------------------------------------
# End-to-end against spec.md §6.3 baseline table
# --------------------------------------------------------------------------

def test_lc_vco_range_target_is_compatible_with_kvco_cap():
    """Keep the LC_VCO example physically consistent.

    With a 0.6 V control sweep and a hard 2000 MHz/V segment-slope cap,
    the required total frequency coverage cannot exceed 1.2 GHz.
    """
    text = (
        REPO / "projects" / "lc_vco_base" / "constraints" / "spec.md"
    ).read_text(encoding="utf-8")
    block = spec_evaluator.extract_eval_block(text)
    assert block is not None

    metrics = {
        metric["name"]: metric
        for metric in block.get("tuning_metrics") or []
    }
    sweep_lo, sweep_hi = block["sweep"]["range"]
    tuning_min_ghz = metrics["tuning_range_GHz"]["pass"][0]
    kvco_max_mhz_per_v = metrics["Kvco_MHz_per_V"]["pass"][1]
    max_range_ghz = (sweep_hi - sweep_lo) * kvco_max_mhz_per_v / 1000.0

    assert tuning_min_ghz <= max_range_ghz


def test_spec_md_baseline_matches_documented_verdict():
    """The §6.3 baseline table claims:
        tuning_range_GHz=3.28 PASS
        Kvco_MHz_per_V    FAIL (8000 exceeds configured upper)
        Kvco_linearity    FAIL (table reports about 3.5)
        monotonic         PASS (strictly up)
    Spec values are rounded to 2 dp so our exact float won't match
    the documented ratio exactly — verify the qualitative verdict and
    that the worst slope
    exceeds the documented pass band.
    """
    text = (
        REPO / "projects" / "lc_vco_base" / "constraints" / "spec.md"
    ).read_text(encoding="utf-8")
    block = spec_evaluator.extract_eval_block(text)
    assert block is not None
    assert block.get("sweep") is not None
    assert len(block.get("tuning_metrics") or []) == 4

    vctrl = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
    fghz = [20.83, 21.33, 21.89, 22.51, 23.09, 23.89, 24.11]
    base = [{"f_osc_GHz": f} for f in fghz]
    meas, pf = spec_evaluator.evaluate_swept(block, base, vctrl)

    assert meas["tuning_range_GHz"] == pytest.approx(
        fghz[-1] - fghz[0], abs=1e-6,
    )
    assert pf["tuning_range_GHz"] == "PASS"
    assert pf["monotonic"] == "PASS"
    # Kvco worst-case verdict: either FAIL (above band) or
    # UNMEASURABLE (suspect: above sanity hi) — both are NOT PASS.
    assert not pf["Kvco_MHz_per_V"].startswith("PASS")
    assert pf["Kvco_linearity"].startswith("FAIL")
    # Linearity is the ratio max/min of |Kvco|; rounded table values
    # still leave it above the 3.0 pass threshold.
    assert meas["Kvco_linearity"] > 3.0
