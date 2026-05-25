"""Integration tests for the curve-searcher wiring in CircuitAgent
(Path-3 prep, 2026-05-24).

Verifies:
- The new optional CircuitAgent.run kwargs exist with the documented
  defaults (off, max_candidates=6).
- `_run_sweep_phase` stashes per-point measurements and Vctrl values
  on `_last_sweep_curve_state` (the searcher's data source) without
  mutating its public 2-tuple return shape.
- scripts/run_agent.py CLI parses the new --enable-curve-searcher /
  --curve-searcher-max-candidates flags with the documented defaults.
- R2 codex_reviewer_v4 fixes (2026-05-24):
    * fix #1: helper returns "" when curve state is missing, so a prior
      iteration's summary cannot bleed into the next prompt;
    * fix #2: a sweep that fails before populating the curve state
      cannot reuse a previous successful sweep's state;
    * fix #3: any foundry-leaky summary text is dropped silently rather
      than forwarded into the LLM prompt.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.run_agent import parse_args  # noqa: E402
from src import curve_searcher  # noqa: E402
from src.agent import CircuitAgent  # noqa: E402


# ---------------------------------------------------------------------------
# run() signature defaults
# ---------------------------------------------------------------------------


def test_run_kwargs_default_curve_searcher_off():
    sig = inspect.signature(CircuitAgent.run)
    params = sig.parameters
    assert "curve_searcher_enabled" in params
    assert params["curve_searcher_enabled"].default is False, (
        "Path-2 baseline must stay default — curve_searcher_enabled "
        "must default to False so existing callers keep their existing "
        "behaviour."
    )
    assert "curve_searcher_max_candidates" in params
    assert (
        params["curve_searcher_max_candidates"].default
        == curve_searcher.DEFAULT_MAX_CANDIDATES
    )


# ---------------------------------------------------------------------------
# _run_sweep_phase side-effect: curve state stash
# ---------------------------------------------------------------------------


_ROOT = "/home/u/sim/cell_tb/maestro/results/maestro/Interactive.0"


def _agent_with_sweep(bridge: MagicMock) -> CircuitAgent:
    spec = (
        "```yaml\n"
        "signals:\n"
        "  - {name: V, kind: V, path: \"/A\"}\n"
        "windows:\n"
        "  full: [0, 1.0e-7]\n"
        "metrics:\n"
        "  - {name: V_rms, signal: V, window: full, stat: rms, "
        "pass: [null, 10.0]}\n"
        "```\n"
        "```yaml\n"
        "sweep: {variable: Vctrl, range: [0.0, 0.2], points: 3, unit: V}\n"
        "tuning_metrics:\n"
        "  - {name: tuning_range, op: swept_max_minus_min, of: V_rms, "
        "pass: [1.5, null], sanity: [0.0, 10.0]}\n"
        "```\n"
    )
    return CircuitAgent(
        bridge=bridge, llm=MagicMock(),
        spec=spec, ocean_worker=MagicMock(),
    )


def test_sweep_phase_stashes_curve_state_for_searcher():
    bridge = MagicMock()
    bridge.read_sweep_manifest.return_value = {1: 0.0, 2: 0.1, 3: 0.2}
    bridge.write_sweep_manifest = MagicMock(
        side_effect=AssertionError("manifest already matches"),
    )
    bridge.run_ocean_dump_all_swept.return_value = {
        1: {"ok": True, "dumps": {"V": {"full": {"rms": 1.0}}}},
        2: {"ok": True, "dumps": {"V": {"full": {"rms": 2.0}}}},
        3: {"ok": True, "dumps": {"V": {"full": {"rms": 3.0}}}},
    }
    agent = _agent_with_sweep(bridge)
    assert agent._last_sweep_curve_state is None

    measurements, pass_fail = agent._run_sweep_phase(
        sweep_results_root=_ROOT,
        tb_cell="LC_VCO_tb",
        result_test="pll_LC_VCO_tb_1",
    )

    # Public return shape unchanged — Path-2 baseline preserved.
    assert isinstance(measurements, dict) and isinstance(pass_fail, dict)
    assert measurements["tuning_range"] == pytest.approx(2.0)
    assert pass_fail["tuning_range"] == "PASS"

    # Side-effect: curve searcher state populated with vctrls + base
    # per-point measurements in sweep order.
    state = agent._last_sweep_curve_state
    assert state is not None
    assert state["vctrls"] == [0.0, 0.1, 0.2]
    assert len(state["base_per_point"]) == 3
    assert state["base_per_point"][0]["V_rms"] == pytest.approx(1.0)
    assert state["base_per_point"][2]["V_rms"] == pytest.approx(3.0)


def test_sweep_phase_state_reusable_by_curve_searcher_end_to_end():
    """Smoke-check: the data `_run_sweep_phase` stashes is directly
    consumable by `curve_searcher.build_summary` without massaging."""
    bridge = MagicMock()
    bridge.read_sweep_manifest.return_value = {1: 0.0, 2: 0.1, 3: 0.2}
    bridge.write_sweep_manifest = MagicMock(return_value=3)
    bridge.run_ocean_dump_all_swept.return_value = {
        1: {"ok": True, "dumps": {"V": {"full": {"rms": 1.0}}}},
        2: {"ok": True, "dumps": {"V": {"full": {"rms": 2.0}}}},
        3: {"ok": True, "dumps": {"V": {"full": {"rms": 3.0}}}},
    }
    agent = _agent_with_sweep(bridge)
    tuning_meas, tuning_pf = agent._run_sweep_phase(
        sweep_results_root=_ROOT,
        tb_cell="LC_VCO_tb",
        result_test="pll_LC_VCO_tb_1",
    )
    state = agent._last_sweep_curve_state
    summary = curve_searcher.build_summary(
        vctrl_values=state["vctrls"],
        base_measurements_per_point=state["base_per_point"],
        tuning_measurements=tuning_meas,
        tuning_pass_fail=tuning_pf,
        design_vars={"C": "222f", "L": "265p", "nfin_cc": 10},
        f_metric_name="V_rms",
        kvco_metric_name="tuning_range",
    )
    assert summary.vctrl == [0.0, 0.1, 0.2]
    assert summary.f_GHz == [1.0, 2.0, 3.0]
    md = summary.to_markdown()
    # PASS case → no violations / no candidates section.
    assert "Worst violations" not in md
    assert "Ranked candidates" not in md


# ---------------------------------------------------------------------------
# scripts/run_agent.py CLI flag parse
# ---------------------------------------------------------------------------


_MIN_ARGV = [
    "run_agent.py",
    "--spec", "spec.md",
    "--lib", "pll",
    "--cell", "LC_VCO",
    "--tb-cell", "LC_VCO_tb",
]


def test_curve_searcher_flags_default_off(monkeypatch):
    monkeypatch.setattr(sys, "argv", _MIN_ARGV)
    args = parse_args()
    assert args.enable_curve_searcher is False
    assert args.curve_searcher_max_candidates == 6


def test_curve_searcher_flags_enabled_and_capped(monkeypatch):
    monkeypatch.setattr(
        sys, "argv",
        _MIN_ARGV + [
            "--enable-curve-searcher",
            "--curve-searcher-max-candidates", "3",
        ],
    )
    args = parse_args()
    assert args.enable_curve_searcher is True
    assert args.curve_searcher_max_candidates == 3


def test_curve_searcher_max_candidates_accepts_zero_to_disable(monkeypatch):
    """Zero disables candidate generation while keeping the curve
    summary text — useful for ablation runs."""
    monkeypatch.setattr(
        sys, "argv",
        _MIN_ARGV + [
            "--enable-curve-searcher",
            "--curve-searcher-max-candidates", "0",
        ],
    )
    args = parse_args()
    assert args.curve_searcher_max_candidates == 0


# ---------------------------------------------------------------------------
# R2 codex_reviewer_v4 patch tests (2026-05-24)
# ---------------------------------------------------------------------------


def _bare_agent() -> CircuitAgent:
    """Minimal agent for unit-testing _build_curve_searcher_section
    behaviour without going through the full run() loop."""
    return _agent_with_sweep(MagicMock())


def test_build_curve_section_returns_empty_when_state_missing():
    """Fix #1 + #2 (defence-in-depth): without `_last_sweep_curve_state`
    the helper must return "" — never call into curve_searcher to
    construct a summary out of stale data."""
    agent = _bare_agent()
    agent._last_sweep_curve_state = None
    md = agent._build_curve_searcher_section(
        tuning_measurements={"tuning_range": 0.5},
        tuning_pass_fail={"tuning_range": "FAIL"},
        design_vars={"C": "222f", "L": "265p", "nfin_cc": 10},
        prev_design_vars={},
        prev_tuning_measurements={},
        max_candidates=6,
    )
    assert md == ""


def test_failed_sweep_clears_curve_state_so_no_stale_carryover():
    """Fix #2: a sweep that early-returns (manifest unreadable / dump
    failure / etc.) must wipe any curve state inherited from a
    previous successful sweep BEFORE returning, so the helper run
    afterwards sees no state."""
    bridge = MagicMock()
    bridge.read_sweep_manifest.return_value = {1: 0.0, 2: 0.1, 3: 0.2}
    bridge.run_ocean_dump_all_swept.return_value = {
        1: {"ok": True, "dumps": {"V": {"full": {"rms": 1.0}}}},
        2: {"ok": True, "dumps": {"V": {"full": {"rms": 2.0}}}},
        3: {"ok": True, "dumps": {"V": {"full": {"rms": 3.0}}}},
    }
    agent = _agent_with_sweep(bridge)

    # 1) First sweep succeeds → curve state is populated.
    agent._run_sweep_phase(
        sweep_results_root=_ROOT, tb_cell="LC_VCO_tb",
        result_test="pll_LC_VCO_tb_1",
    )
    assert agent._last_sweep_curve_state is not None

    # 2) Second sweep hits manifest_read_failed BEFORE state is written.
    bridge.read_sweep_manifest.side_effect = RuntimeError("boom")
    # Force the manifest cache to miss so read_sweep_manifest is called.
    agent._sweep_manifest_cache.clear()
    # Also stub _ensure_sweep_manifest's helpers so it does not short
    # the failure path. MagicMock bridge gives sensible defaults.
    tuning_meas, tuning_pf = agent._run_sweep_phase(
        sweep_results_root=_ROOT, tb_cell="LC_VCO_tb",
        result_test="pll_LC_VCO_tb_1",
    )
    # Sweep declared all metrics UNMEASURABLE — verdicts may carry a
    # parenthetical reason, e.g. "UNMEASURABLE (manifest_read_failed)".
    assert tuning_pf  # non-empty verdict dict
    assert all(
        str(v).startswith("UNMEASURABLE") for v in tuning_pf.values()
    ), tuning_pf
    # … and crucially, did NOT leave the prior successful state behind.
    assert agent._last_sweep_curve_state is None

    # 3) Now the helper, given the cleared state, must return "".
    md = agent._build_curve_searcher_section(
        tuning_measurements=tuning_meas,
        tuning_pass_fail=tuning_pf,
        design_vars={"C": "222f", "L": "265p", "nfin_cc": 10},
        prev_design_vars={},
        prev_tuning_measurements={},
        max_candidates=6,
    )
    assert md == ""


def test_build_curve_section_drops_foundry_leaky_summary(monkeypatch):
    """Fix #3: even if a future change to build_summary leaks a
    forbidden foundry / waveform token into the rendered Markdown,
    the agent-level helper must drop the section silently — not
    propagate it into the LLM prompt."""
    agent = _bare_agent()
    # Pre-populate the curve state so the helper takes the build path.
    agent._last_sweep_curve_state = {
        "vctrls": [0.0, 0.1, 0.2],
        "base_per_point": [
            {"V_rms": 1.0}, {"V_rms": 2.0}, {"V_rms": 3.0},
        ],
    }

    class _LeakySummary:
        def to_markdown(self) -> str:
            # `nch_` is one of the foundry tokens in
            # curve_searcher._FOUNDRY_LEAK_TOKENS.
            return "## f-Vctrl curve\nUse cell `nch_lvt_25` here."

    from src import agent as agent_module
    monkeypatch.setattr(
        agent_module.curve_searcher, "build_summary",
        lambda *a, **kw: _LeakySummary(),
    )
    md = agent._build_curve_searcher_section(
        tuning_measurements={"tuning_range": 0.5},
        tuning_pass_fail={"tuning_range": "FAIL"},
        design_vars={"C": "222f", "L": "265p", "nfin_cc": 10},
        prev_design_vars={},
        prev_tuning_measurements={},
        max_candidates=6,
    )
    assert md == "", (
        "foundry-leaky summary must be silently dropped — never "
        "forwarded into the next-iter prompt."
    )


def test_build_curve_section_drops_waveform_escalation_summary(monkeypatch):
    """Fix #3 cont.: same gate covers raw-waveform escalation tokens
    (``.tran`` / ``readraw`` / ``displayraw`` / ``savewaveform``),
    which would invite the LLM to ask for forbidden raw payloads."""
    agent = _bare_agent()
    agent._last_sweep_curve_state = {
        "vctrls": [0.0, 0.1, 0.2],
        "base_per_point": [
            {"V_rms": 1.0}, {"V_rms": 2.0}, {"V_rms": 3.0},
        ],
    }

    class _EscalatingSummary:
        def to_markdown(self) -> str:
            return "Run `.tran 0 100n` to see the waveform."

    from src import agent as agent_module
    monkeypatch.setattr(
        agent_module.curve_searcher, "build_summary",
        lambda *a, **kw: _EscalatingSummary(),
    )
    md = agent._build_curve_searcher_section(
        tuning_measurements={"tuning_range": 0.5},
        tuning_pass_fail={"tuning_range": "FAIL"},
        design_vars={"C": "222f", "L": "265p", "nfin_cc": 10},
        prev_design_vars={},
        prev_tuning_measurements={},
        max_candidates=6,
    )
    assert md == ""
