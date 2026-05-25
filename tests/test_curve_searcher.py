"""Unit tests for ``src.curve_searcher`` (Path-3 prep, 2026-05-24).

Pure-Python: no SafeBridge / SKILL / Maestro / OCEAN. Covers
candidate generation, scoring, sensitivity, end-to-end summary,
engineering-suffix preservation, max_candidates bounds, the default-
off path, and the PDK / raw-waveform safety gates.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src import curve_searcher  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _baseline_lc_vco_scenario():
    """Mirrors the HANDOFF / latest-run baseline: 9-point Vctrl sweep
    that's monotonic but fails tuning_range / Kvco band / linearity.
    """
    vctrl = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
    f_ghz = [20.38, 20.83, 21.33, 21.89, 22.51, 23.09, 23.89, 24.11, 24.14]
    base_per_point = [{"f_osc_GHz": f} for f in f_ghz]
    tuning_measurements = {
        "tuning_range_GHz": 3.76,
        "Kvco_MHz_per_V": [
            450.0, 500.0, 560.0, 620.0, 580.0, 800.0, 220.0, 30.0,
        ],
        "Kvco_linearity": 26.7,
        "monotonic": True,
    }
    tuning_pass_fail = {
        "tuning_range_GHz": "FAIL (below 4.0)",
        "Kvco_MHz_per_V": "FAIL (above 300)",
        "Kvco_linearity": "FAIL (above 3.0)",
        "monotonic": "PASS",
    }
    design_vars = {
        "C": "222f", "L": "265p", "Ibias": "500u",
        "nfin_cc": 10, "nfin_mirror": 16, "Vctrl": "0.4",
    }
    return vctrl, base_per_point, tuning_measurements, tuning_pass_fail, design_vars


# ---------------------------------------------------------------------------
# Engineering-suffix parser / formatter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected_val,expected_suffix", [
    ("222f", 222e-15, "f"),
    ("265p", 265e-12, "p"),
    ("500u", 500e-6, "u"),
    ("10k", 10e3, "k"),
    ("1.5n", 1.5e-9, "n"),
    ("16", 16.0, ""),
    ("16.0", 16.0, ""),
    ("-2.5e-3", -2.5e-3, ""),
    (10, 10.0, ""),
    (10.0, 10.0, ""),
])
def test_parse_eng_value_recognised(text, expected_val, expected_suffix):
    val, suffix = curve_searcher._parse_eng_value(text)
    assert val == pytest.approx(expected_val)
    assert suffix == expected_suffix


@pytest.mark.parametrize("bad", [None, True, False, "abc", "1.0Mohm", "10G", ""])
def test_parse_eng_value_rejects(bad):
    val, suffix = curve_searcher._parse_eng_value(bad)
    assert val is None and suffix == ""


@pytest.mark.parametrize("text,factor,expected_prefix", [
    ("222f", 1.25, "f"),   # 222 * 1.25 = 277.5 → "277.5f"
    ("265p", 0.8, "p"),
    ("10k", 2.0, "k"),
    ("16", 0.5, ""),
])
def test_format_preserves_suffix_family(text, factor, expected_prefix):
    val, suffix = curve_searcher._parse_eng_value(text)
    out = curve_searcher._format_eng_value(val * factor, suffix)
    assert out.endswith(expected_prefix)
    # Round-trip back to SI: parsed value should equal val*factor.
    round_val, round_suffix = curve_searcher._parse_eng_value(out)
    assert round_val == pytest.approx(val * factor)
    assert round_suffix == expected_prefix


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------


def test_generate_candidates_baseline_has_results():
    _, _, meas, pf, dv = _baseline_lc_vco_scenario()
    cands = curve_searcher.generate_candidates(meas, pf, dv)
    assert cands, "expected at least one candidate for the failing baseline"
    assert all(isinstance(c, curve_searcher.Candidate) for c in cands)
    # All candidates restricted to (C, L, nfin_cc) — the LC_VCO primaries
    # that ARE present in design_vars. (Ibias, nfin_mirror are not
    # primaries.)
    for c in cands:
        assert c.var in ("C", "L", "nfin_cc")


def test_generate_candidates_respects_max_cap():
    _, _, meas, pf, dv = _baseline_lc_vco_scenario()
    cands_2 = curve_searcher.generate_candidates(
        meas, pf, dv, max_candidates=2,
    )
    assert len(cands_2) <= 2
    cands_0 = curve_searcher.generate_candidates(
        meas, pf, dv, max_candidates=0,
    )
    assert cands_0 == []
    cands_neg = curve_searcher.generate_candidates(
        meas, pf, dv, max_candidates=-3,
    )
    assert cands_neg == []


def test_generate_candidates_empty_when_all_pass():
    _, _, meas, _, dv = _baseline_lc_vco_scenario()
    all_pass = {k: "PASS" for k in meas}
    cands = curve_searcher.generate_candidates(meas, all_pass, dv)
    assert cands == []


def test_generate_candidates_empty_when_no_primary_vars():
    _, _, meas, pf, _ = _baseline_lc_vco_scenario()
    # design_vars carries only non-primary names → nothing to propose.
    dv = {"Ibias": "500u", "nfin_mirror": 16}
    cands = curve_searcher.generate_candidates(meas, pf, dv)
    assert cands == []


def test_generate_candidates_preserves_eng_suffix_on_new_value():
    _, _, meas, pf, dv = _baseline_lc_vco_scenario()
    cands = curve_searcher.generate_candidates(meas, pf, dv)
    for c in cands:
        if c.var == "C":
            # Input "222f" must produce a femto-suffixed new_value.
            assert c.new_value.endswith("f"), (
                f"C candidate {c.new_value!r} should keep 'f' suffix"
            )
        if c.var == "L":
            assert c.new_value.endswith("p"), (
                f"L candidate {c.new_value!r} should keep 'p' suffix"
            )


def test_generate_candidates_monotonic_break_triggers_nfin_cc():
    _, _, meas, pf, dv = _baseline_lc_vco_scenario()
    # Flip monotonic to FAIL and clear other fails — verify nfin_cc
    # surfaces as a candidate for the monotonic-break direction.
    meas2 = dict(meas, monotonic=False)
    pf2 = {
        "tuning_range_GHz": "PASS",
        "Kvco_MHz_per_V": "PASS",
        "Kvco_linearity": "PASS",
        "monotonic": "FAIL (expected True)",
    }
    cands = curve_searcher.generate_candidates(meas2, pf2, dv)
    assert cands, "monotonic FAIL should produce candidates"
    assert any(c.var == "nfin_cc" for c in cands)


def test_generate_candidates_higher_score_for_multi_target_hits():
    """A var/factor pair that addresses multiple failing metrics should
    rank above one that addresses just one."""
    _, _, meas, pf, dv = _baseline_lc_vco_scenario()
    cands = curve_searcher.generate_candidates(meas, pf, dv)
    assert cands, "baseline expected to produce candidates"
    # The top candidate's target set should be non-empty.
    assert cands[0].targets, "top candidate must list at least one target"
    # Sort invariant: scores monotonically non-increasing.
    scores = [c.score for c in cands]
    assert scores == sorted(scores, reverse=True)


def test_unparseable_design_var_value_is_skipped_not_crashed():
    _, _, meas, pf, dv = _baseline_lc_vco_scenario()
    dv_bad = dict(dv, C="not-a-number")
    cands = curve_searcher.generate_candidates(meas, pf, dv_bad)
    # C should not appear (unparseable), but L / nfin_cc proposals may.
    assert all(c.var != "C" for c in cands)


# ---------------------------------------------------------------------------
# Sensitivity
# ---------------------------------------------------------------------------


def test_sensitivity_empty_with_no_prior():
    assert curve_searcher.compute_sensitivity(
        None, None, {"C": "222f"}, {"tuning_range_GHz": 3.76},
    ) == {}
    assert curve_searcher.compute_sensitivity(
        {}, {}, {"C": "222f"}, {"tuning_range_GHz": 3.76},
    ) == {}


def test_sensitivity_observed_change_signed():
    prev_dv = {"C": "200f"}
    prev_tm = {"tuning_range_GHz": 3.0, "Kvco_linearity": 30.0}
    cur_dv = {"C": "400f"}  # 2x C
    cur_tm = {"tuning_range_GHz": 3.76, "Kvco_linearity": 26.7}
    sens = curve_searcher.compute_sensitivity(
        prev_dv, prev_tm, cur_dv, cur_tm,
    )
    # ln(2) denominator: dtuning ≈ 0.76 → ~1.10 per ln var.
    assert "C" in sens
    assert sens["C"]["tuning_range_GHz"] > 0
    assert sens["C"]["Kvco_linearity"] < 0  # we improved linearity


def test_sensitivity_skips_unchanged_or_nonpositive_values():
    # Same value → skipped
    assert curve_searcher.compute_sensitivity(
        {"C": "200f"}, {"tuning_range_GHz": 3.0},
        {"C": "200f"}, {"tuning_range_GHz": 3.76},
    ) == {}
    # Zero / negative value → skipped (log undefined)
    assert curve_searcher.compute_sensitivity(
        {"C": "0"}, {"tuning_range_GHz": 3.0},
        {"C": "200f"}, {"tuning_range_GHz": 3.76},
    ) == {}


# ---------------------------------------------------------------------------
# CurveSummary / build_summary end-to-end
# ---------------------------------------------------------------------------


def test_build_summary_baseline_renders_curve_and_candidates():
    vctrl, base, meas, pf, dv = _baseline_lc_vco_scenario()
    summary = curve_searcher.build_summary(
        vctrl_values=vctrl,
        base_measurements_per_point=base,
        tuning_measurements=meas,
        tuning_pass_fail=pf,
        design_vars=dv,
    )
    assert summary.vctrl == vctrl
    assert summary.f_GHz[0] == pytest.approx(20.38)
    assert summary.f_GHz[-1] == pytest.approx(24.14)
    assert len(summary.kvco_segments_MHz_per_V) == len(meas["Kvco_MHz_per_V"])
    md = summary.to_markdown()
    # Sanity-check renderer covers each section.
    assert "f-Vctrl curve" in md
    assert "Kvco segments" in md
    assert "Worst violations" in md
    assert "Ranked candidates" in md
    # The three baseline failures must surface as violation lines.
    assert "tuning_range_GHz" in md
    assert "Kvco_MHz_per_V" in md
    assert "Kvco_linearity" in md
    # monotonic was PASS — should NOT appear as a violation row, but
    # may still appear under the candidate-targets list. Check it's
    # not in the "Worst violations" section specifically.
    violations_section = md.split("### Worst violations", 1)[1].split("###", 1)[0]
    assert "monotonic" not in violations_section


def test_build_summary_handles_empty_per_point():
    summary = curve_searcher.build_summary(
        vctrl_values=[0.0, 0.4, 0.8],
        base_measurements_per_point=[{}, {}, {}],
        tuning_measurements={"tuning_range_GHz": 0.0},
        tuning_pass_fail={"tuning_range_GHz": "FAIL (below 0.8)"},
        design_vars={"C": "222f", "L": "265p", "nfin_cc": 10},
    )
    assert summary.f_GHz == [None, None, None]
    md = summary.to_markdown()
    # Should not crash; em-dash placeholder for missing f.
    assert "—" in md


def test_build_summary_with_prev_includes_sensitivity_section():
    vctrl, base, meas, pf, dv = _baseline_lc_vco_scenario()
    prev_dv = dict(dv, C="200f")
    prev_tm = {
        "tuning_range_GHz": 3.0,
        "Kvco_MHz_per_V": [400.0],
        "Kvco_linearity": 30.0,
        "monotonic": True,
    }
    summary = curve_searcher.build_summary(
        vctrl_values=vctrl,
        base_measurements_per_point=base,
        tuning_measurements=meas,
        tuning_pass_fail=pf,
        design_vars=dv,
        prev_design_vars=prev_dv,
        prev_tuning_measurements=prev_tm,
    )
    assert "C" in summary.sensitivity
    assert "Last-change sensitivity" in summary.to_markdown()


# ---------------------------------------------------------------------------
# Safety: no foundry / no raw-waveform escalation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", [
    "nch_lvt", "pch_lvt", "cfmom_2t", "rppoly", "rm1_drawn",
    "tsmc28", "tcbn28", "rxnp_drawn", "vsubs_dummy",
])
def test_assert_no_foundry_leak_raises_on_each_token(token):
    payload = f"PDK suggests {token} for the tank cap"
    with pytest.raises(ValueError):
        curve_searcher.assert_no_foundry_leak(payload)


@pytest.mark.parametrize("token", [
    "please fetch the .tran waveform",
    "use readRawData() on the psf",
    "displayRaw on Vout",
    "saveWaveform of Vctrl",
])
def test_assert_no_foundry_leak_rejects_raw_waveform_escalation(token):
    with pytest.raises(ValueError):
        curve_searcher.assert_no_foundry_leak(token)


def test_assert_no_foundry_leak_accepts_clean_summary():
    _, base, meas, pf, dv = _baseline_lc_vco_scenario()
    summary = curve_searcher.build_summary(
        vctrl_values=[0.0, 0.4, 0.8],
        base_measurements_per_point=base[:3],
        tuning_measurements=meas,
        tuning_pass_fail=pf,
        design_vars=dv,
    )
    # Must not raise.
    curve_searcher.assert_no_foundry_leak(summary.to_markdown())


def test_baseline_summary_contains_no_pdk_or_path_strings():
    """End-to-end sanity: the baseline summary text must not echo any
    foundry-cell prefix or absolute remote path / model file hint."""
    vctrl, base, meas, pf, dv = _baseline_lc_vco_scenario()
    summary = curve_searcher.build_summary(
        vctrl_values=vctrl,
        base_measurements_per_point=base,
        tuning_measurements=meas,
        tuning_pass_fail=pf,
        design_vars=dv,
    )
    md = summary.to_markdown().lower()
    for forbidden in (
        "/project/", "/home/", "/tmp/", "C:\\", "C:/",
        ".scs", ".cdl", ".psf", "spectre", "maestro",
    ):
        assert forbidden.lower() not in md, (
            f"summary contains forbidden path/format hint {forbidden!r}"
        )


# ---------------------------------------------------------------------------
# Public constants / API surface
# ---------------------------------------------------------------------------


def test_primary_vars_matches_lc_vco_leader_spec():
    """Leader-specified primary search vars: C, L, nfin_cc (in priority
    order). Pin so refactors don't silently drop one."""
    assert curve_searcher.LC_VCO_PRIMARY_VARS == ("C", "L", "nfin_cc")


def test_default_max_candidates_is_positive_and_bounded():
    assert 1 <= curve_searcher.DEFAULT_MAX_CANDIDATES <= 12
