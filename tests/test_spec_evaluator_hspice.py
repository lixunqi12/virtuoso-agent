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
    HspiceConfigError,
    HspiceMetricNotFoundError,
    HspiceShapeError,
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


# --------------------------------------------------------------------------
# T8.6: generic ``reduce:`` block — cross-mt reduction, op dispatch,
# UNMEASURABLE edge-case handling, and config-time validation.
# --------------------------------------------------------------------------


# Coupling-test layout: 4 .mt files (= 4 weight codes), each with a
# 3-row TRAN sweep on the same column ``h_tphl``. The reducer collapses
# the 4 weight-code samples per row into a single linregress / mean /
# std / range output, leaving 3 output rows aligned with the TRAN sweep.
_REDUCE_COLUMNS = ["h_tphl", "v_tplh", "temper", "alter#"]


def _reduce_mt_set(values_per_file: list[list[float]]) -> dict[str, Mt0Result]:
    """Build a dict of N .mt files where each row is [h_tphl, 0.0, 25, k+1].

    Inner list = rows for that file; outer list = files in mt0..mtN order.
    """
    out: dict[str, Mt0Result] = {}
    for k, rows in enumerate(values_per_file):
        out[f"sim.mt{k}"] = _mt0(
            _REDUCE_COLUMNS,
            [[v, 0.0, 25.0, float(k + 1)] for v in rows],
            alter=k + 1,
        )
    return out


class TestT86Reduce:
    # --- Linregress: happy path -------------------------------------------

    def test_linregress_slope_abs_per_row(self):
        # 4 mt files, x = [0, 2, 4, 6]; y per row is intentionally linear.
        # Row 0: y = 1*x + 10 → slope=1.0, slope_abs=1.0
        # Row 1: y = 2*x + 20 → slope=2.0
        # Row 2: y = -3*x + 5 → slope=-3.0, slope_abs=3.0
        mt = _reduce_mt_set([
            [10.0, 20.0, 5.0],   # mt0 (x=0)
            [12.0, 24.0, -1.0],  # mt1 (x=2)
            [14.0, 28.0, -7.0],  # mt2 (x=4)
            [16.0, 32.0, -13.0], # mt3 (x=6)
        ])
        metrics = [{
            "name": "slope_abs_metric",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "linregress",
                       "x": [0, 2, 4, 6], "output": "slope_abs"},
            "pass": [None, None],
        }]
        res = evaluate_hspice(mt, metrics)
        vals = res.measurements["slope_abs_metric"]
        assert vals[0] == pytest.approx(1.0)
        assert vals[1] == pytest.approx(2.0)
        assert vals[2] == pytest.approx(3.0)
        assert res.pass_fail["slope_abs_metric"] == "PASS"

    def test_linregress_r_squared_perfect_fit(self):
        # Row 0 is exactly linear → R²=1.0
        mt = _reduce_mt_set([
            [10.0],
            [12.0],
            [14.0],
            [16.0],
        ])
        metrics = [{
            "name": "r2_metric",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "linregress",
                       "x": [0, 2, 4, 6], "output": "r_squared"},
            "pass": [0.95, 1.0],
        }]
        res = evaluate_hspice(mt, metrics)
        assert res.measurements["r2_metric"][0] == pytest.approx(1.0)
        assert res.pass_fail["r2_metric"] == "PASS"

    def test_linregress_slope_signed(self):
        # output: slope returns signed slope (negative).
        mt = _reduce_mt_set([
            [10.0],
            [4.0],
            [-2.0],
            [-8.0],
        ])
        metrics = [{
            "name": "slope_signed",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "linregress",
                       "x": [0, 2, 4, 6], "output": "slope"},
            "pass": [None, None],
        }]
        res = evaluate_hspice(mt, metrics)
        assert res.measurements["slope_signed"][0] == pytest.approx(-3.0)

    def test_linregress_intercept(self):
        # y = 1*x + 10 → intercept=10
        mt = _reduce_mt_set([
            [10.0],
            [12.0],
            [14.0],
            [16.0],
        ])
        metrics = [{
            "name": "intercept_metric",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "linregress",
                       "x": [0, 2, 4, 6], "output": "intercept"},
            "pass": [None, None],
        }]
        res = evaluate_hspice(mt, metrics)
        assert res.measurements["intercept_metric"][0] == pytest.approx(10.0)

    # --- Other ops --------------------------------------------------------

    def test_mean_op(self):
        mt = _reduce_mt_set([
            [10.0],
            [20.0],
            [30.0],
            [40.0],
        ])
        metrics = [{
            "name": "mean_metric",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "mean", "x": [0, 2, 4, 6]},
            "pass": [None, None],
        }]
        res = evaluate_hspice(mt, metrics)
        assert res.measurements["mean_metric"][0] == pytest.approx(25.0)

    def test_max_min_range(self):
        mt = _reduce_mt_set([
            [10.0],
            [20.0],
            [30.0],
            [40.0],
        ])
        for op, expected in (("max", 40.0), ("min", 10.0), ("range", 30.0)):
            metrics = [{
                "name": f"{op}_metric",
                "source": "h_tphl",
                "reduce": {"across": "mt_files", "op": op,
                           "x": [0, 2, 4, 6]},
                "pass": [None, None],
            }]
            res = evaluate_hspice(mt, metrics)
            assert res.measurements[f"{op}_metric"][0] == pytest.approx(expected), op

    def test_std_population_ddof_zero(self):
        # Population std of [10, 20, 30, 40] = sqrt(125) ≈ 11.180339887
        mt = _reduce_mt_set([
            [10.0],
            [20.0],
            [30.0],
            [40.0],
        ])
        metrics = [{
            "name": "std_metric",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "std",
                       "x": [0, 2, 4, 6]},
            "pass": [None, None],
        }]
        res = evaluate_hspice(mt, metrics)
        assert res.measurements["std_metric"][0] == pytest.approx(
            (125.0) ** 0.5
        )

    # --- Schema / shape errors -------------------------------------------

    def test_unknown_op_raises_config_error(self):
        mt = _reduce_mt_set([[1.0], [2.0]])
        metrics = [{
            "name": "bad_op",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "median", "x": [0, 1]},
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError, match="reduce.op"):
            evaluate_hspice(mt, metrics)

    def test_unknown_output_for_linregress_raises(self):
        mt = _reduce_mt_set([[1.0], [2.0]])
        metrics = [{
            "name": "bad_output",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "linregress",
                       "x": [0, 1], "output": "stderr"},
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError, match="reduce.output"):
            evaluate_hspice(mt, metrics)

    def test_x_length_mismatch_raises(self):
        # 3 mt files but x has 4 entries — only validated for linregress
        # post-R2 (B2). Use linregress here so the length check fires.
        mt = _reduce_mt_set([[1.0], [2.0], [3.0]])
        metrics = [{
            "name": "bad_x_len",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "linregress",
                       "x": [0, 1, 2, 3], "output": "slope"},
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError, match="reduce.x length"):
            evaluate_hspice(mt, metrics)

    def test_row_count_mismatch_raises_shape_error(self):
        # mt0 has 2 rows, mt1 has 3 rows.
        mt = {
            "sim.mt0": _mt0(_REDUCE_COLUMNS, [
                [1.0, 0.0, 25.0, 1.0],
                [2.0, 0.0, 25.0, 1.0],
            ], alter=1),
            "sim.mt1": _mt0(_REDUCE_COLUMNS, [
                [3.0, 0.0, 25.0, 2.0],
                [4.0, 0.0, 25.0, 2.0],
                [5.0, 0.0, 25.0, 2.0],
            ], alter=2),
        }
        metrics = [{
            "name": "row_mismatch",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "mean", "x": [0, 1]},
            "pass": [None, None],
        }]
        with pytest.raises(HspiceShapeError, match="row count mismatch"):
            evaluate_hspice(mt, metrics)

    def test_sweep_rows_across_not_implemented(self):
        mt = _reduce_mt_set([[1.0], [2.0]])
        metrics = [{
            "name": "sweep",
            "source": "h_tphl",
            "reduce": {"across": "sweep_rows", "op": "mean", "x": [0, 1]},
            "pass": [None, None],
        }]
        with pytest.raises(NotImplementedError, match="sweep_rows"):
            evaluate_hspice(mt, metrics)

    # --- New cases A–F from contract v2 ----------------------------------

    def test_A_linregress_two_finite_points_r_squared_unmeasurable(self):
        # n=2 → slope/intercept defined; r_squared UNMEASURABLE
        # by contract (perfect fit, statistically meaningless).
        mt = _reduce_mt_set([
            [float("nan")],
            [10.0],
            [14.0],
            [float("nan")],
        ])
        metrics = [{
            "name": "two_pt_r2",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "linregress",
                       "x": [0, 2, 4, 6], "output": "r_squared"},
            "pass": [None, None],
        }]
        res = evaluate_hspice(mt, metrics)
        v = res.pass_fail["two_pt_r2"]
        assert v.startswith("UNMEASURABLE"), v
        assert "r_squared" in v or "n=2" in v or "perfect fit" in v

        # And slope_abs on the same data must succeed.
        metrics2 = [{
            "name": "two_pt_slope",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "linregress",
                       "x": [0, 2, 4, 6], "output": "slope_abs"},
            "pass": [None, None],
        }]
        res2 = evaluate_hspice(mt, metrics2)
        assert res2.measurements["two_pt_slope"][0] == pytest.approx(2.0)
        assert res2.pass_fail["two_pt_slope"] == "PASS"

    def test_B_linregress_y_constant_slope_zero_r_squared_unmeasurable(self):
        # y constant → slope=0, intercept=mean(y), r_squared UNMEASURABLE
        # (ss_tot=0).
        mt = _reduce_mt_set([
            [7.0],
            [7.0],
            [7.0],
            [7.0],
        ])
        # slope path → 0
        metrics = [{
            "name": "y_const_slope",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "linregress",
                       "x": [0, 2, 4, 6], "output": "slope"},
            "pass": [None, None],
        }]
        res = evaluate_hspice(mt, metrics)
        assert res.measurements["y_const_slope"][0] == pytest.approx(0.0)

        # r_squared path → UNMEASURABLE
        metrics2 = [{
            "name": "y_const_r2",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "linregress",
                       "x": [0, 2, 4, 6], "output": "r_squared"},
            "pass": [None, None],
        }]
        res2 = evaluate_hspice(mt, metrics2)
        assert res2.pass_fail["y_const_r2"].startswith("UNMEASURABLE")

    def test_C_linregress_x_constant_all_unmeasurable(self):
        # x all equal → all 4 outputs UNMEASURABLE (zero variance in x).
        mt = _reduce_mt_set([[1.0], [2.0], [3.0], [4.0]])
        for output in ("slope", "slope_abs", "intercept", "r_squared"):
            metrics = [{
                "name": f"x_const_{output}",
                "source": "h_tphl",
                "reduce": {"across": "mt_files", "op": "linregress",
                           "x": [3, 3, 3, 3], "output": output},
                "pass": [None, None],
            }]
            res = evaluate_hspice(mt, metrics)
            v = res.pass_fail[f"x_const_{output}"]
            assert v.startswith("UNMEASURABLE"), (output, v)

    def test_D_x_contains_nan_raises_config_error(self):
        # Post-R2 (B2): x is only validated for linregress; non-finite
        # x must still be rejected eagerly there.
        mt = _reduce_mt_set([[1.0], [2.0], [3.0], [4.0]])
        metrics = [{
            "name": "x_nan",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "linregress",
                       "x": [0, float("nan"), 4, 6], "output": "slope"},
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError, match="non-finite"):
            evaluate_hspice(mt, metrics)

        # And inf is rejected too.
        metrics_inf = [{
            "name": "x_inf",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "linregress",
                       "x": [0, 2, float("inf"), 6], "output": "slope"},
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError, match="non-finite"):
            evaluate_hspice(mt, metrics_inf)

    def test_E_source_missing_in_one_mt_raises_config_error(self):
        # mt1 lacks the source column → HspiceConfigError (not the
        # legacy soft-skip behaviour the no-reduce path uses).
        mt = {
            "sim.mt0": _mt0(_REDUCE_COLUMNS, [[1.0, 0.0, 25.0, 1.0]]),
            "sim.mt1": _mt0(
                ["other", "temper", "alter#"],
                [[2.0, 25.0, 2.0]],
                alter=2,
            ),
        }
        metrics = [{
            "name": "source_missing",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "mean"},
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError, match="reduce.source"):
            evaluate_hspice(mt, metrics)

    def test_F_scale_invariance_of_r_squared(self):
        # scale applies BEFORE the reducer, but R² is scale-invariant
        # (ss_res and ss_tot both scale by k², ratio preserved).
        rows_seconds = [
            [1.0e-12],   # mt0
            [3.0e-12],   # mt1
            [5.1e-12],   # mt2 (slight noise so r² < 1)
            [7.0e-12],   # mt3
        ]
        mt = _reduce_mt_set(rows_seconds)

        metric_unit_scale = [{
            "name": "r2_unit",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "linregress",
                       "x": [0, 2, 4, 6], "output": "r_squared"},
            "scale": 1.0,
            "pass": [None, None],
        }]
        metric_pico_scale = [{
            "name": "r2_pico",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "linregress",
                       "x": [0, 2, 4, 6], "output": "r_squared"},
            "scale": 1.0e12,
            "pass": [None, None],
        }]
        res_u = evaluate_hspice(mt, metric_unit_scale)
        res_p = evaluate_hspice(mt, metric_pico_scale)
        r2_u = res_u.measurements["r2_unit"][0]
        r2_p = res_p.measurements["r2_pico"][0]
        assert r2_u == pytest.approx(r2_p, rel=1e-9, abs=1e-12)
        # And the value is below 1 (noise present) but above 0.9.
        assert 0.9 < r2_u < 1.0


# --------------------------------------------------------------------------
# T8.6 R2 — blockers from dual review.
#   B1: std/range with finite samples < 2 must be UNMEASURABLE
#       (single-sample std/range collapse to 0.0 and would silently
#       pass a "spread <= X" gate).
#   B2: reduce.x is only required / validated for op=linregress;
#       mean/max/min/std/range must not require it.
#   B3: HspiceConfigError on missing source must NOT leak the raw .mt
#       column list (.mt column names are PDK-derived).
# --------------------------------------------------------------------------


class TestT86R2Blockers:
    # --- B1 -------------------------------------------------------------

    def test_B1_std_n1_unmeasurable(self):
        # 4 mt files, but row 0 has only one finite y (rest NaN).
        mt = _reduce_mt_set([
            [10.0],
            [float("nan")],
            [float("nan")],
            [float("nan")],
        ])
        metrics = [{
            "name": "std_one_sample",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "std"},
            "pass": [None, None],
        }]
        res = evaluate_hspice(mt, metrics)
        v = res.pass_fail["std_one_sample"]
        assert v.startswith("UNMEASURABLE"), v
        assert "std" in v

    def test_B1_range_n1_unmeasurable(self):
        mt = _reduce_mt_set([
            [10.0],
            [float("nan")],
            [float("nan")],
            [float("nan")],
        ])
        metrics = [{
            "name": "range_one_sample",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "range"},
            "pass": [None, None],
        }]
        res = evaluate_hspice(mt, metrics)
        v = res.pass_fail["range_one_sample"]
        assert v.startswith("UNMEASURABLE"), v
        assert "range" in v

    def test_B1_std_n2_still_works(self):
        # Boundary: n=2 finite samples must succeed for std/range.
        mt = _reduce_mt_set([
            [10.0],
            [20.0],
            [float("nan")],
            [float("nan")],
        ])
        metrics = [{
            "name": "std_n2",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "std"},
            "pass": [None, None],
        }]
        res = evaluate_hspice(mt, metrics)
        # population std of [10, 20] = 5.0
        assert res.measurements["std_n2"][0] == pytest.approx(5.0)
        assert res.pass_fail["std_n2"] == "PASS"

    # --- B2 -------------------------------------------------------------

    def test_B2_mean_without_x_succeeds(self):
        # No reduce.x on mean — must NOT raise.
        mt = _reduce_mt_set([[10.0], [20.0], [30.0], [40.0]])
        metrics = [{
            "name": "mean_no_x",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "mean"},
            "pass": [None, None],
        }]
        res = evaluate_hspice(mt, metrics)
        assert res.measurements["mean_no_x"][0] == pytest.approx(25.0)
        assert res.pass_fail["mean_no_x"] == "PASS"

    def test_B2_max_min_range_std_without_x_succeed(self):
        mt = _reduce_mt_set([[10.0], [20.0], [30.0], [40.0]])
        for op, expected in (
            ("max", 40.0),
            ("min", 10.0),
            ("range", 30.0),
            ("std", (125.0) ** 0.5),
        ):
            metrics = [{
                "name": f"{op}_no_x",
                "source": "h_tphl",
                "reduce": {"across": "mt_files", "op": op},
                "pass": [None, None],
            }]
            res = evaluate_hspice(mt, metrics)
            assert res.measurements[f"{op}_no_x"][0] == pytest.approx(
                expected
            ), op

    def test_B2_linregress_without_x_still_raises(self):
        # B2 must NOT relax linregress's x requirement.
        mt = _reduce_mt_set([[10.0], [20.0], [30.0], [40.0]])
        metrics = [{
            "name": "linreg_no_x",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "linregress",
                       "output": "slope"},
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError, match="reduce.x"):
            evaluate_hspice(mt, metrics)

    def test_B2_non_linregress_does_not_validate_x_contents(self):
        # Even if x is malformed (NaN), non-linregress ops must not
        # care — x is ignored for them.
        mt = _reduce_mt_set([[10.0], [20.0], [30.0], [40.0]])
        metrics = [{
            "name": "mean_with_bad_x",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "mean",
                       "x": [0, float("nan"), 4, 6]},
            "pass": [None, None],
        }]
        res = evaluate_hspice(mt, metrics)
        # Reducer ignores x; mean of [10,20,30,40] = 25.
        assert res.measurements["mean_with_bad_x"][0] == pytest.approx(25.0)

    # --- B3 -------------------------------------------------------------

    def test_B3_missing_source_does_not_leak_column_names(self):
        # Construct a .mt table with foundry-shaped column names that
        # MUST never end up in HspiceConfigError. The exception text
        # is allowed to mention the source name (spec author wrote it,
        # already public) and the basename, but not the actual column
        # list.
        FOUNDRY_LIKE_COLS = ["vth_n_28", "gm_p_lvt", "ids_corr",
                             "temper", "alter#"]
        mt = {
            "sim.mt0": _mt0(
                ["h_tphl", "temper", "alter#"],
                [[1.0, 25.0, 1.0]],
                alter=1,
            ),
            "sim.mt1": _mt0(
                FOUNDRY_LIKE_COLS,
                [[0.4, 1e-3, 5e-6, 25.0, 2.0]],
                alter=2,
            ),
        }
        metrics = [{
            "name": "leak_check",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "mean"},
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError) as ei:
            evaluate_hspice(mt, metrics)
        msg = str(ei.value)
        # Allowed: source name + basename + column count.
        assert "h_tphl" in msg
        assert "sim.mt1" in msg
        # Disallowed: any of the foundry-shaped names.
        for col in FOUNDRY_LIKE_COLS:
            if col in ("temper", "alter#"):  # innocuous, may appear elsewhere
                continue
            assert col not in msg, (
                f"column name {col!r} leaked into HspiceConfigError: {msg!r}"
            )


# --------------------------------------------------------------------------
# T8.7 — mt_indices subset, eval_rows, derived source.expr, diff_paired.
# --------------------------------------------------------------------------


_T87_COLUMNS = ["h_tphl", "v_tphl", "temper", "alter#"]


def _t87_mt_set(values_per_file: list[list[tuple[float, float]]]) -> dict[str, Mt0Result]:
    """Like _reduce_mt_set but two source columns (h_tphl, v_tphl) per row."""
    out: dict[str, Mt0Result] = {}
    for k, rows in enumerate(values_per_file):
        out[f"sim.mt{k}"] = _mt0(
            _T87_COLUMNS,
            [[h, v, 25.0, float(k + 1)] for (h, v) in rows],
            alter=k + 1,
        )
    return out


class TestT87MtIndices:
    def test_subset_to_pos_half(self):
        # 8 mt files, but only mt0..mt3 (POS) should drive the regression.
        # mt4..mt7 (NEG) carry corrupted data that would tank slope/R².
        rows_per_mt = [
            [(10.0, 0.0)],   # mt0 (x=0)  → POS, clean
            [(12.0, 0.0)],   # mt1 (x=2)  → POS, clean
            [(14.0, 0.0)],   # mt2 (x=4)  → POS, clean
            [(16.0, 0.0)],   # mt3 (x=6)  → POS, clean
            [(99.0, 0.0)],   # mt4 (x=0)  → NEG, garbage
            [(99.0, 0.0)],   # mt5 (x=-2) → NEG, garbage
            [(99.0, 0.0)],   # mt6 (x=-4) → NEG, garbage
            [(99.0, 0.0)],   # mt7 (x=-6) → NEG, garbage
        ]
        mt = _t87_mt_set(rows_per_mt)
        metrics = [{
            "name": "pos_only_slope",
            "source": "h_tphl",
            "reduce": {
                "across": "mt_files",
                "mt_indices": [0, 1, 2, 3],
                "op": "linregress",
                "x": [0, 2, 4, 6],
                "output": "slope_abs",
            },
            "pass": [0.99, 1.01],
        }]
        res = evaluate_hspice(mt, metrics)
        # Slope = 1.0 from mt0..mt3 only (NEG garbage ignored)
        assert res.measurements["pos_only_slope"][0] == pytest.approx(1.0)
        assert res.pass_fail["pos_only_slope"] == "PASS"

    def test_x_length_must_match_subset(self):
        # mt_indices picks 4 → x must have 4 entries
        mt = _t87_mt_set([[(10.0, 0.0)], [(12.0, 0.0)], [(14.0, 0.0)],
                          [(16.0, 0.0)], [(99.0, 0.0)]])
        metrics = [{
            "name": "bad_x_len",
            "source": "h_tphl",
            "reduce": {
                "across": "mt_files",
                "mt_indices": [0, 1, 2, 3],
                "op": "linregress",
                "x": [0, 2, 4, 6, 99],   # 5 entries, mismatch
                "output": "slope",
            },
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError, match="reduce.x length"):
            evaluate_hspice(mt, metrics)

    def test_unknown_index_raises(self):
        mt = _t87_mt_set([[(1.0, 0.0)], [(2.0, 0.0)]])
        metrics = [{
            "name": "bad_idx",
            "source": "h_tphl",
            "reduce": {
                "across": "mt_files",
                "mt_indices": [0, 9],
                "op": "mean",
            },
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError, match="mt9"):
            evaluate_hspice(mt, metrics)

    def test_duplicate_index_rejected(self):
        mt = _t87_mt_set([[(1.0, 0.0)], [(2.0, 0.0)]])
        metrics = [{
            "name": "dup_idx",
            "source": "h_tphl",
            "reduce": {
                "across": "mt_files",
                "mt_indices": [0, 0],
                "op": "mean",
            },
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError, match="duplicate"):
            evaluate_hspice(mt, metrics)

    def test_empty_indices_rejected(self):
        mt = _t87_mt_set([[(1.0, 0.0)], [(2.0, 0.0)]])
        metrics = [{
            "name": "empty_idx",
            "source": "h_tphl",
            "reduce": {
                "across": "mt_files",
                "mt_indices": [],
                "op": "mean",
            },
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError, match="non-empty"):
            evaluate_hspice(mt, metrics)


class TestT87EvalRows:
    def test_eval_rows_restricts_aggregate_to_selected(self):
        # 4 mt files, 3 rows. Row 0 fails (slope_abs=10), rows 1&2 pass.
        # Without eval_rows: aggregate FAIL (row 0 fails).
        # With eval_rows=[1, 2]: aggregate PASS.
        mt = _reduce_mt_set([
            [10.0, 10.0, 10.0],
            [30.0, 12.0, 14.0],
            [50.0, 14.0, 18.0],
            [70.0, 16.0, 22.0],
        ])
        # Row 0: slope=10 (fails [0, 5]), Row 1: slope=1, Row 2: slope=2.
        common = {
            "name": "slope_pass_late",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "linregress",
                       "x": [0, 2, 4, 6], "output": "slope_abs"},
            "pass": [0, 5],
        }
        # Without eval_rows: FAIL because row 0 has slope_abs=10
        res_no_filter = evaluate_hspice(mt, [common])
        assert res_no_filter.pass_fail["slope_pass_late"].startswith("FAIL")

        # With eval_rows=[1,2]: only those rows count → PASS
        with_rows = dict(common); with_rows["eval_rows"] = [1, 2]
        res_filtered = evaluate_hspice(mt, [with_rows])
        assert res_filtered.pass_fail["slope_pass_late"] == "PASS"
        # Per-row verdicts still emit all 3 rows (observability preserved)
        assert len(res_filtered.per_row_verdicts["slope_pass_late"]) == 3

    def test_eval_rows_out_of_range_raises(self):
        mt = _reduce_mt_set([[1.0], [2.0], [3.0], [4.0]])
        metrics = [{
            "name": "oob",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "mean"},
            "pass": [None, None],
            "eval_rows": [0, 99],
        }]
        with pytest.raises(HspiceConfigError, match="out of range"):
            evaluate_hspice(mt, metrics)

    def test_eval_rows_negative_rejected(self):
        mt = _reduce_mt_set([[1.0], [2.0]])
        metrics = [{
            "name": "neg",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "mean"},
            "pass": [None, None],
            "eval_rows": [-1],
        }]
        with pytest.raises(HspiceConfigError, match="non-negative"):
            evaluate_hspice(mt, metrics)


class TestT87SourceExpr:
    def test_simple_average_expression(self):
        # source: (h_tphl + v_tphl) / 2 — common-mode midpoint.
        mt = _t87_mt_set([
            [(10.0, 20.0)],   # mid = 15
            [(30.0, 40.0)],   # mid = 35
        ])
        metrics = [{
            "name": "midpoint_mean",
            "source": {"expr": "(h_tphl + v_tphl) / 2"},
            "reduce": {"across": "mt_files", "op": "mean"},
            "pass": [None, None],
        }]
        res = evaluate_hspice(mt, metrics)
        # mean(15, 35) = 25
        assert res.measurements["midpoint_mean"][0] == pytest.approx(25.0)

    def test_unary_minus_and_constant(self):
        mt = _t87_mt_set([[(10.0, 4.0)], [(20.0, 4.0)]])
        metrics = [{
            "name": "neg_and_const",
            "source": {"expr": "-h_tphl + 100"},
            "reduce": {"across": "mt_files", "op": "mean"},
            "pass": [None, None],
        }]
        res = evaluate_hspice(mt, metrics)
        # vals: -10+100=90, -20+100=80 → mean=85
        assert res.measurements["neg_and_const"][0] == pytest.approx(85.0)

    def test_disallowed_function_call_rejected(self):
        mt = _t87_mt_set([[(1.0, 2.0)], [(3.0, 4.0)]])
        metrics = [{
            "name": "bad",
            "source": {"expr": "max(h_tphl, v_tphl)"},
            "reduce": {"across": "mt_files", "op": "mean"},
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError, match="disallowed"):
            evaluate_hspice(mt, metrics)

    def test_unknown_column_in_expr_raises(self):
        mt = _t87_mt_set([[(1.0, 2.0)], [(3.0, 4.0)]])
        metrics = [{
            "name": "bad_col",
            "source": {"expr": "h_tphl + nonexistent"},
            "reduce": {"across": "mt_files", "op": "mean"},
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError, match="nonexistent"):
            evaluate_hspice(mt, metrics)

    def test_syntax_error_in_expr_rejected(self):
        mt = _t87_mt_set([[(1.0, 2.0)]])
        metrics = [{
            "name": "syntax",
            "source": {"expr": "h_tphl +"},
            "reduce": {"across": "mt_files", "op": "mean"},
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError, match="syntax error"):
            evaluate_hspice(mt, metrics)

    def test_string_source_still_works(self):
        # Backwards compat: source as plain string column name.
        mt = _t87_mt_set([[(10.0, 0.0)], [(20.0, 0.0)]])
        metrics = [{
            "name": "plain",
            "source": "h_tphl",
            "reduce": {"across": "mt_files", "op": "mean"},
            "pass": [None, None],
        }]
        res = evaluate_hspice(mt, metrics)
        assert res.measurements["plain"][0] == pytest.approx(15.0)


class TestT87DiffPaired:
    def test_max_abs_diff_4_pairs(self):
        # 8 mt files. Pairs (0,4), (1,5), (2,6), (3,7).
        # h_tphl values: mt0=10, mt1=11, mt2=12, mt3=13,
        #                mt4=10, mt5=11.5, mt6=14, mt7=20
        # diffs: |0|, |-0.5|, |-2|, |-7| → max_abs_diff = 7
        rows = [
            [(10.0, 0.0)], [(11.0, 0.0)], [(12.0, 0.0)], [(13.0, 0.0)],
            [(10.0, 0.0)], [(11.5, 0.0)], [(14.0, 0.0)], [(20.0, 0.0)],
        ]
        mt = _t87_mt_set(rows)
        metrics = [{
            "name": "pos_neg_diff",
            "source": "h_tphl",
            "reduce": {
                "across": "mt_files",
                "op": "diff_paired",
                "pairs": [[0, 4], [1, 5], [2, 6], [3, 7]],
                "output": "max_abs_diff",
            },
            "pass": [None, None],
        }]
        res = evaluate_hspice(mt, metrics)
        assert res.measurements["pos_neg_diff"][0] == pytest.approx(7.0)

    def test_signed_diff_single_pair(self):
        rows = [
            [(10.0, 0.0)], [(0.0, 0.0)], [(0.0, 0.0)], [(0.0, 0.0)],
            [(7.0, 0.0)],
        ]
        mt = _t87_mt_set(rows)
        metrics = [{
            "name": "sign_dc",
            "source": "h_tphl",
            "reduce": {
                "across": "mt_files",
                "op": "diff_paired",
                "pairs": [[0, 4]],
                "output": "signed_diff",
            },
            "pass": [-5, 5],
        }]
        res = evaluate_hspice(mt, metrics)
        # mt0(10) - mt4(7) = +3
        assert res.measurements["sign_dc"][0] == pytest.approx(3.0)
        assert res.pass_fail["sign_dc"] == "PASS"

    def test_signed_diff_requires_single_pair(self):
        mt = _t87_mt_set([[(1.0, 0.0)]] * 5)
        metrics = [{
            "name": "bad_signed",
            "source": "h_tphl",
            "reduce": {
                "across": "mt_files",
                "op": "diff_paired",
                "pairs": [[0, 1], [2, 3]],
                "output": "signed_diff",
            },
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError, match="exactly one entry"):
            evaluate_hspice(mt, metrics)

    def test_pair_references_unknown_index(self):
        mt = _t87_mt_set([[(1.0, 0.0)], [(2.0, 0.0)]])
        metrics = [{
            "name": "bad_ref",
            "source": "h_tphl",
            "reduce": {
                "across": "mt_files",
                "op": "diff_paired",
                "pairs": [[0, 9]],
                "output": "max_abs_diff",
            },
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError, match="mt9"):
            evaluate_hspice(mt, metrics)

    def test_degenerate_pair_rejected(self):
        mt = _t87_mt_set([[(1.0, 0.0)], [(2.0, 0.0)]])
        metrics = [{
            "name": "self_pair",
            "source": "h_tphl",
            "reduce": {
                "across": "mt_files",
                "op": "diff_paired",
                "pairs": [[0, 0]],
                "output": "max_abs_diff",
            },
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError, match="degenerate"):
            evaluate_hspice(mt, metrics)

    def test_diff_paired_with_mt_indices_rejected(self):
        mt = _t87_mt_set([[(1.0, 0.0)], [(2.0, 0.0)], [(3.0, 0.0)]])
        metrics = [{
            "name": "both",
            "source": "h_tphl",
            "reduce": {
                "across": "mt_files",
                "mt_indices": [0, 1],
                "op": "diff_paired",
                "pairs": [[0, 1]],
                "output": "max_abs_diff",
            },
            "pass": [None, None],
        }]
        with pytest.raises(HspiceConfigError, match="cannot both be set"):
            evaluate_hspice(mt, metrics)


class TestT87Combined:
    def test_subset_plus_eval_rows_plus_expr(self):
        # End-to-end use of three features at once: derived midpoint over
        # a subset of mt files, evaluated only at row 1.
        rows = [
            [(10.0, 0.0), (20.0, 0.0)],   # mt0  → mid = [5, 10]
            [(12.0, 0.0), (22.0, 0.0)],   # mt1  → mid = [6, 11]
            [(14.0, 0.0), (24.0, 0.0)],   # mt2  → mid = [7, 12]
            [(16.0, 0.0), (26.0, 0.0)],   # mt3  → mid = [8, 13]
            [(99.0, 0.0), (99.0, 0.0)],   # mt4  → garbage, excluded
        ]
        mt = _t87_mt_set(rows)
        metrics = [{
            "name": "combo",
            "source": {"expr": "(h_tphl + v_tphl) / 2"},
            "reduce": {
                "across": "mt_files",
                "mt_indices": [0, 1, 2, 3],
                "op": "linregress",
                "x": [0, 1, 2, 3],
                "output": "slope",
            },
            "pass": [None, None],
            "eval_rows": [1],
        }]
        res = evaluate_hspice(mt, metrics)
        # Row 0 mids: 5, 6, 7, 8 → slope=1
        # Row 1 mids: 10, 11, 12, 13 → slope=1
        # eval_rows=[1] → aggregate considers only row 1
        assert res.measurements["combo"][0] == pytest.approx(1.0)
        assert res.measurements["combo"][1] == pytest.approx(1.0)
        assert res.pass_fail["combo"] == "PASS"
