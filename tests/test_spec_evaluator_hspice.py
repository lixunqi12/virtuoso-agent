"""Unit tests for src/hspice_resolver.py (T4).

Pure Python — no SSH / HSpice / parse_mt0 text round-trip. Tests
build ``Mt0Result`` fixtures directly so they focus on the
resolver's column-lookup + multi-alter + verdict-aggregation
behaviour, not the parser (parse_mt0 has its own 37-test suite).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.hspice_resolver import (  # noqa: E402
    EvaluationResult,
    HspiceMetricNotFoundError,
    evaluate_hspice,
)
from src.parse_mt0 import Mt0Result  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _mt0(
    columns: list[str],
    rows: list[list[float]],
    alter: int = 1,
    title: str = "test",
) -> Mt0Result:
    return Mt0Result(
        header={"source": "HSPICE", "version": "V1"},
        title=title,
        columns=columns,
        rows=rows,
        alter_number=alter,
    )


# LC_VCO-style columns: three measures + the tail (temper, alter#).
_COLUMNS = ["f_osc_GHz", "V_diff_pp_V", "V_cm_V", "temper", "alter#"]


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------


class TestHappyPath:
    def test_single_alter_single_row_all_pass(self):
        mt = {
            "sim.mt0": _mt0(_COLUMNS, [[20.0, 0.6, 0.75, 25.0, 1.0]]),
        }
        metrics = [
            {"name": "f_osc_GHz", "pass": [19.5, 20.5]},
            {"name": "V_diff_pp_V", "pass": [0.40, None]},
            {"name": "V_cm_V", "pass": [0.70, 0.81]},
        ]
        res = evaluate_hspice(mt, metrics)
        assert isinstance(res, EvaluationResult)
        assert res.measurements["f_osc_GHz"] == [20.0]
        assert res.measurements["V_diff_pp_V"] == [0.6]
        assert res.measurements["V_cm_V"] == [0.75]
        assert res.pass_fail["f_osc_GHz"] == "PASS"
        assert res.pass_fail["V_diff_pp_V"] == "PASS"
        assert res.pass_fail["V_cm_V"] == "PASS"

    def test_per_row_verdicts_populated(self):
        mt = {
            "sim.mt0": _mt0(_COLUMNS, [[20.0, 0.6, 0.75, 25.0, 1.0]]),
        }
        metrics = [{"name": "f_osc_GHz", "pass": [19.5, 20.5]}]
        res = evaluate_hspice(mt, metrics)
        assert res.per_row_verdicts["f_osc_GHz"] == ["PASS"]


# --------------------------------------------------------------------------
# Pass range violations
# --------------------------------------------------------------------------


class TestPassRange:
    def test_value_below_lo_fails(self):
        mt = {"sim.mt0": _mt0(_COLUMNS, [[19.0, 0.6, 0.75, 25.0, 1.0]])}
        metrics = [{"name": "f_osc_GHz", "pass": [19.5, 20.5]}]
        res = evaluate_hspice(mt, metrics)
        v = res.pass_fail["f_osc_GHz"]
        assert v.startswith("FAIL"), v
        assert "19.5" in v

    def test_value_above_hi_fails(self):
        mt = {"sim.mt0": _mt0(_COLUMNS, [[21.0, 0.6, 0.75, 25.0, 1.0]])}
        metrics = [{"name": "f_osc_GHz", "pass": [19.5, 20.5]}]
        res = evaluate_hspice(mt, metrics)
        v = res.pass_fail["f_osc_GHz"]
        assert v.startswith("FAIL"), v
        assert "20.5" in v

    def test_open_ended_hi_ignores_upper(self):
        # pass: [0.40, null] → any value >= 0.40 passes, no upper check
        mt = {"sim.mt0": _mt0(_COLUMNS, [[20.0, 99.0, 0.75, 25.0, 1.0]])}
        metrics = [{"name": "V_diff_pp_V", "pass": [0.40, None]}]
        res = evaluate_hspice(mt, metrics)
        assert res.pass_fail["V_diff_pp_V"] == "PASS"

    def test_open_ended_lo_ignores_lower(self):
        mt = {"sim.mt0": _mt0(_COLUMNS, [[20.0, 0.6, -10.0, 25.0, 1.0]])}
        metrics = [{"name": "V_cm_V", "pass": [None, 0.81]}]
        res = evaluate_hspice(mt, metrics)
        assert res.pass_fail["V_cm_V"] == "PASS"

    def test_tolerance_boundary_inclusive_lo(self):
        # value == lo bound must PASS (membership is inclusive).
        mt = {"sim.mt0": _mt0(_COLUMNS, [[19.5, 0.6, 0.75, 25.0, 1.0]])}
        metrics = [{"name": "f_osc_GHz", "pass": [19.5, 20.5]}]
        res = evaluate_hspice(mt, metrics)
        assert res.pass_fail["f_osc_GHz"] == "PASS"

    def test_tolerance_boundary_inclusive_hi(self):
        mt = {"sim.mt0": _mt0(_COLUMNS, [[20.5, 0.6, 0.75, 25.0, 1.0]])}
        metrics = [{"name": "f_osc_GHz", "pass": [19.5, 20.5]}]
        res = evaluate_hspice(mt, metrics)
        assert res.pass_fail["f_osc_GHz"] == "PASS"


# --------------------------------------------------------------------------
# Scale
# --------------------------------------------------------------------------


class TestScale:
    def test_scale_applied_to_raw_value(self):
        # t_startup raw is seconds; spec uses scale=1e9 for ns.
        mt = {
            "sim.mt0": _mt0(
                ["t_startup", "temper", "alter#"],
                [[5.0e-9, 25.0, 1.0]],
            ),
        }
        metrics = [
            {"name": "t_startup", "scale": 1.0e9, "pass": [None, 10.0]},
        ]
        res = evaluate_hspice(mt, metrics)
        assert res.measurements["t_startup"] == [pytest.approx(5.0)]
        assert res.pass_fail["t_startup"] == "PASS"

    def test_scale_defaults_to_one_when_missing(self):
        mt = {"sim.mt0": _mt0(_COLUMNS, [[20.0, 0.6, 0.75, 25.0, 1.0]])}
        metrics = [{"name": "f_osc_GHz", "pass": [19.5, 20.5]}]
        res = evaluate_hspice(mt, metrics)
        assert res.measurements["f_osc_GHz"] == [20.0]

    def test_scale_nonfinite_rejected(self):
        mt = {"sim.mt0": _mt0(_COLUMNS, [[20.0, 0.6, 0.75, 25.0, 1.0]])}
        metrics = [
            {"name": "f_osc_GHz", "scale": float("inf"), "pass": [19.5, 20.5]},
        ]
        with pytest.raises(ValueError, match="scale"):
            evaluate_hspice(mt, metrics)


# --------------------------------------------------------------------------
# Sanity (three-state verdict)
# --------------------------------------------------------------------------


class TestSanity:
    def test_value_outside_sanity_flagged_unmeasurable(self):
        # 200 GHz is inside pass bounds neither — and outside sanity,
        # so the primary verdict must be UNMEASURABLE (measurement
        # chain suspect), not FAIL (circuit broken).
        mt = {"sim.mt0": _mt0(_COLUMNS, [[200.0, 0.6, 0.75, 25.0, 1.0]])}
        metrics = [
            {"name": "f_osc_GHz", "pass": [19.5, 20.5],
             "sanity": [0.1, 100.0]},
        ]
        res = evaluate_hspice(mt, metrics)
        v = res.pass_fail["f_osc_GHz"]
        assert v.startswith("UNMEASURABLE"), v

    def test_value_inside_sanity_outside_pass_fails_normally(self):
        # 25 GHz is beyond pass but physically plausible → FAIL, not
        # UNMEASURABLE.
        mt = {"sim.mt0": _mt0(_COLUMNS, [[25.0, 0.6, 0.75, 25.0, 1.0]])}
        metrics = [
            {"name": "f_osc_GHz", "pass": [19.5, 20.5],
             "sanity": [0.1, 100.0]},
        ]
        res = evaluate_hspice(mt, metrics)
        assert res.pass_fail["f_osc_GHz"].startswith("FAIL")


# --------------------------------------------------------------------------
# Multi-alter (multiple .mt<k> tables)
# --------------------------------------------------------------------------


class TestMultiAlter:
    def test_multi_alter_all_pass(self):
        mt = {
            "sim.mt0": _mt0(_COLUMNS, [[20.0, 0.6, 0.75, 25.0, 1.0]], alter=1),
            "sim.mt1": _mt0(_COLUMNS, [[20.1, 0.6, 0.75, 25.0, 2.0]], alter=2),
            "sim.mt2": _mt0(_COLUMNS, [[19.9, 0.6, 0.75, 25.0, 3.0]], alter=3),
        }
        metrics = [{"name": "f_osc_GHz", "pass": [19.5, 20.5]}]
        res = evaluate_hspice(mt, metrics)
        assert res.measurements["f_osc_GHz"] == [20.0, 20.1, 19.9]
        assert res.pass_fail["f_osc_GHz"] == "PASS"

    def test_multi_alter_one_fails_aggregates_to_fail(self):
        mt = {
            "sim.mt0": _mt0(_COLUMNS, [[20.0, 0.6, 0.75, 25.0, 1.0]], alter=1),
            "sim.mt1": _mt0(_COLUMNS, [[22.0, 0.6, 0.75, 25.0, 2.0]], alter=2),
        }
        metrics = [{"name": "f_osc_GHz", "pass": [19.5, 20.5]}]
        res = evaluate_hspice(mt, metrics)
        v = res.pass_fail["f_osc_GHz"]
        assert v.startswith("FAIL")
        # Multi-row verdict must identify which row failed.
        assert "row 1/2" in v
        assert res.per_row_verdicts["f_osc_GHz"] == [
            "PASS", "FAIL (above 20.5)",
        ]

    def test_multi_alter_preserves_basename_order(self):
        # Out-of-order dict keys must still be iterated sorted so that
        # mt0 < mt1 < mt2 — otherwise downstream logs mis-label the
        # alter index.
        mt = {
            "sim.mt2": _mt0(_COLUMNS, [[30.0, 0.6, 0.75, 25.0, 3.0]], alter=3),
            "sim.mt0": _mt0(_COLUMNS, [[10.0, 0.6, 0.75, 25.0, 1.0]], alter=1),
            "sim.mt1": _mt0(_COLUMNS, [[20.0, 0.6, 0.75, 25.0, 2.0]], alter=2),
        }
        metrics = [{"name": "f_osc_GHz", "pass": [None, None]}]
        res = evaluate_hspice(mt, metrics)
        assert res.measurements["f_osc_GHz"] == [10.0, 20.0, 30.0]

    def test_multi_digit_alter_ordering(self):
        # Codex T4 R2 blocker: plain lex sort of basenames would put
        # ``sim.mt10`` before ``sim.mt2``. Natural sort by the trailing
        # ``.mt<N>`` integer must yield 0 -> 2 -> 10 -> 12 regardless
        # of dict insertion order.
        mt = {
            "sim.mt12": _mt0(_COLUMNS, [[40.0, 0.6, 0.75, 25.0, 13.0]],
                             alter=13),
            "sim.mt0":  _mt0(_COLUMNS, [[10.0, 0.6, 0.75, 25.0, 1.0]],
                             alter=1),
            "sim.mt10": _mt0(_COLUMNS, [[30.0, 0.6, 0.75, 25.0, 11.0]],
                             alter=11),
            "sim.mt2":  _mt0(_COLUMNS, [[20.0, 0.6, 0.75, 25.0, 3.0]],
                             alter=3),
        }
        metrics = [{"name": "f_osc_GHz", "pass": [None, None]}]
        res = evaluate_hspice(mt, metrics)
        assert res.measurements["f_osc_GHz"] == [10.0, 20.0, 30.0, 40.0]

    def test_non_mt_basename_sorts_after_mt_entries(self):
        # Natural-sort fallback: a basename that does NOT end in
        # ``.mt<N>`` must not crash the sort and must be ordered after
        # all matched entries (lex tiebreak within the fallback bucket).
        mt = {
            "sim.mt2":   _mt0(_COLUMNS, [[20.0, 0.6, 0.75, 25.0, 3.0]]),
            "sim.weird": _mt0(_COLUMNS, [[99.0, 0.6, 0.75, 25.0, 9.0]]),
            "sim.mt0":   _mt0(_COLUMNS, [[10.0, 0.6, 0.75, 25.0, 1.0]]),
        }
        metrics = [{"name": "f_osc_GHz", "pass": [None, None]}]
        res = evaluate_hspice(mt, metrics)
        assert res.measurements["f_osc_GHz"] == [10.0, 20.0, 99.0]


# --------------------------------------------------------------------------
# Multi-row sweep (one .mt<k> with many rows)
# --------------------------------------------------------------------------


class TestSweepMultiRow:
    def test_sweep_all_rows_pass(self):
        mt = {
            "sim.mt0": _mt0(_COLUMNS, [
                [20.0, 0.6, 0.75, -40.0, 1.0],
                [20.1, 0.6, 0.75, 25.0, 1.0],
                [20.2, 0.6, 0.75, 125.0, 1.0],
            ]),
        }
        metrics = [{"name": "f_osc_GHz", "pass": [19.5, 20.5]}]
        res = evaluate_hspice(mt, metrics)
        assert res.measurements["f_osc_GHz"] == [20.0, 20.1, 20.2]
        assert res.pass_fail["f_osc_GHz"] == "PASS"
        assert res.per_row_verdicts["f_osc_GHz"] == ["PASS", "PASS", "PASS"]

    def test_sweep_one_row_fails(self):
        mt = {
            "sim.mt0": _mt0(_COLUMNS, [
                [20.0, 0.6, 0.75, -40.0, 1.0],
                [18.0, 0.6, 0.75, 25.0, 1.0],   # cold-corner fails lo
                [20.2, 0.6, 0.75, 125.0, 1.0],
            ]),
        }
        metrics = [{"name": "f_osc_GHz", "pass": [19.5, 20.5]}]
        res = evaluate_hspice(mt, metrics)
        v = res.pass_fail["f_osc_GHz"]
        assert v.startswith("FAIL")
        assert "row 1/3" in v
        assert "18" in v


# --------------------------------------------------------------------------
# Not-found — hard raise
# --------------------------------------------------------------------------


class TestNotFound:
    def test_missing_metric_name_raises(self):
        mt = {"sim.mt0": _mt0(_COLUMNS, [[20.0, 0.6, 0.75, 25.0, 1.0]])}
        metrics = [{"name": "bogus_metric", "pass": [0, 1]}]
        with pytest.raises(HspiceMetricNotFoundError) as ei:
            evaluate_hspice(mt, metrics)
        assert ei.value.metric_name == "bogus_metric"
        # available list must contain the real columns for the LLM/log
        # to surface what the netlist actually emitted.
        assert "f_osc_GHz" in ei.value.available
        assert "V_diff_pp_V" in ei.value.available

    def test_empty_mt_results_raises_not_found(self):
        metrics = [{"name": "f_osc_GHz", "pass": [19.5, 20.5]}]
        with pytest.raises(HspiceMetricNotFoundError):
            evaluate_hspice({}, metrics)

    def test_metric_present_in_some_but_not_all_tables(self):
        # If column exists in at least one table, no raise — values
        # collected only from tables that have it.
        mt = {
            "sim.mt0": _mt0(_COLUMNS, [[20.0, 0.6, 0.75, 25.0, 1.0]]),
            "sim.mt1": _mt0(
                ["other_col", "temper", "alter#"],
                [[42.0, 25.0, 2.0]],
            ),
        }
        metrics = [{"name": "f_osc_GHz", "pass": [19.5, 20.5]}]
        res = evaluate_hspice(mt, metrics)
        assert res.measurements["f_osc_GHz"] == [20.0]

    def test_missing_name_in_metric_entry_raises_value_error(self):
        mt = {"sim.mt0": _mt0(_COLUMNS, [[20.0, 0.6, 0.75, 25.0, 1.0]])}
        metrics = [{"pass": [0, 1]}]  # no name
        with pytest.raises(ValueError, match="name"):
            evaluate_hspice(mt, metrics)


# --------------------------------------------------------------------------
# Compound metric fallback
# --------------------------------------------------------------------------


class TestCompoundSkip:
    def test_compound_ratio_flagged_unmeasurable(self):
        mt = {"sim.mt0": _mt0(_COLUMNS, [[20.0, 0.6, 0.75, 25.0, 1.0]])}
        metrics = [
            {"name": "amp_hold_ratio", "compound": "ratio",
             "numerator": {"signal": "Vdiff", "window": "late", "stat": "rms"},
             "denominator": {"signal": "Vdiff", "window": "early", "stat": "rms"},
             "pass": [0.95, None]},
        ]
        res = evaluate_hspice(mt, metrics)
        v = res.pass_fail["amp_hold_ratio"]
        assert v.startswith("UNMEASURABLE")
        assert "ratio" in v
        # No values collected for compound metrics.
        assert res.measurements["amp_hold_ratio"] == []

    def test_compound_t_cross_flagged_unmeasurable(self):
        mt = {"sim.mt0": _mt0(_COLUMNS, [[20.0, 0.6, 0.75, 25.0, 1.0]])}
        metrics = [
            {"name": "t_startup_ns", "compound": "t_cross_frac",
             "signal": "Vdiff", "frac": 0.45,
             "ref": {"signal": "Vdiff", "window": "late", "stat": "ptp"},
             "window": "startup", "direction": "rising",
             "use_abs": True, "scale": 1.0e9, "pass": [None, 10]},
        ]
        res = evaluate_hspice(mt, metrics)
        v = res.pass_fail["t_startup_ns"]
        assert v.startswith("UNMEASURABLE")
        assert "t_cross_frac" in v

    def test_compound_skip_does_not_prevent_other_metrics(self):
        mt = {"sim.mt0": _mt0(_COLUMNS, [[20.0, 0.6, 0.75, 25.0, 1.0]])}
        metrics = [
            {"name": "amp_hold_ratio", "compound": "ratio",
             "numerator": {"signal": "Vdiff", "window": "late", "stat": "rms"},
             "denominator": {"signal": "Vdiff", "window": "early", "stat": "rms"},
             "pass": [0.95, None]},
            {"name": "f_osc_GHz", "pass": [19.5, 20.5]},
        ]
        res = evaluate_hspice(mt, metrics)
        assert res.pass_fail["amp_hold_ratio"].startswith("UNMEASURABLE")
        assert res.pass_fail["f_osc_GHz"] == "PASS"


# --------------------------------------------------------------------------
# Ordering / determinism
# --------------------------------------------------------------------------


class TestOrdering:
    def test_flat_values_follow_mt_basename_then_row_order(self):
        mt = {
            "sim.mt0": _mt0(_COLUMNS, [
                [1.0, 0.6, 0.75, 25.0, 1.0],
                [2.0, 0.6, 0.75, 25.0, 1.0],
            ]),
            "sim.mt1": _mt0(_COLUMNS, [
                [3.0, 0.6, 0.75, 25.0, 2.0],
                [4.0, 0.6, 0.75, 25.0, 2.0],
            ]),
        }
        metrics = [{"name": "f_osc_GHz", "pass": [None, None]}]
        res = evaluate_hspice(mt, metrics)
        assert res.measurements["f_osc_GHz"] == [1.0, 2.0, 3.0, 4.0]
