"""Primitive-probe regression tests for CircuitAgent (Stage 1 rev 2).

These cover the three new behavioral contracts introduced in the
OCEAN-driven rewrite:

1. `_all_pass` convergence predicate Ã¢â‚¬â€ strict PASS-prefix check, not
   substring.
2. SAFEGUARD streak counter Ã¢â‚¬â€ resets on a good iteration, trips only
   after three *consecutive* sub-0.3 amp_hold_ratio reads.
3. Writeback preservation (user directive Q2) Ã¢â‚¬â€ the final metrics report
   must survive a Maestro writeback failure; the agent surfaces the
   failure via ``writeback_status`` without re-raising.

The tests are deliberately primitive: they exercise the static/instance
methods directly rather than the full closed-loop ``run()`` so that a
future refactor cannot slip past them by changing the loop's shape.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.agent import (  # noqa: E402
    CircuitAgent,
    HspiceAgent,
    SAFEGUARD_AMP_HOLD_MIN,
    SAFEGUARD_CONSECUTIVE_LIMIT,
    TOPOLOGY_SANITY_VIOLATION_LIMIT,
    _VALID_DESIGN_VAR_NAMES,
    _coerce_float,
    _has_sanity_violation,
)
from src.hspice_worker import HspiceRunResult  # noqa: E402


# ---------------------------------------------------------------- #
#  Fixtures
# ---------------------------------------------------------------- #

@pytest.fixture
def agent():
    """CircuitAgent wired to mock bridge + mock LLM, no real IO."""
    return CircuitAgent(
        bridge=MagicMock(),
        llm=MagicMock(),
        spec={"f_osc": "19.5"},
        analysis_type="tran",
        ocean_worker=MagicMock(),
    )


# ---------------------------------------------------------------- #
#  _all_pass Ã¢â‚¬â€ convergence predicate
# ---------------------------------------------------------------- #

class TestAllPass:
    def test_all_pass_strings_accepted(self):
        pf = {"f_osc": "PASS", "V_diff_pp": "PASS"}
        assert CircuitAgent._all_pass(pf) is True

    def test_single_fail_rejects(self):
        pf = {"f_osc": "FAIL (target 19.5Ã¢â‚¬â€œ20.5 GHz)", "V_diff_pp": "PASS"}
        assert CircuitAgent._all_pass(pf) is False

    def test_pass_with_trailing_annotation_accepted(self):
        """Regression vs rev 2 reviewer's MAJOR finding: the old
        substring-based check would wrongly reject this because it
        contained 'fail'. Prefix check must accept."""
        pf = {
            "f_osc": "PASS (target 19.5Ã¢â‚¬â€œ20.5 GHz Ã¢â‚¬â€ previously FAILED once)",
            "V_diff_pp": "PASS",
        }
        assert CircuitAgent._all_pass(pf) is True

    def test_case_insensitive_prefix(self):
        pf = {"f_osc": "pass", "V_diff_pp": "Pass"}
        assert CircuitAgent._all_pass(pf) is True

    def test_leading_whitespace_tolerated(self):
        pf = {"f_osc": "  PASS", "V_diff_pp": "\tPASS"}
        assert CircuitAgent._all_pass(pf) is True

    def test_empty_dict_does_not_converge(self):
        """No pass_fail at all must NOT be treated as converged Ã¢â‚¬â€ the LLM
        could have emitted an incomplete block and the agent must keep
        iterating rather than silently exit."""
        assert CircuitAgent._all_pass({}) is False

    def test_non_string_value_rejected(self):
        """Defensive: numeric/None values in pass_fail aren't prefixed
        with 'PASS' and therefore fail the predicate."""
        assert CircuitAgent._all_pass({"f_osc": 1, "V_diff_pp": None}) is False


# ---------------------------------------------------------------- #
#  SAFEGUARD streak counter
# ---------------------------------------------------------------- #
#
# The streak logic lives inside CircuitAgent.run(). To exercise it
# deterministically without spinning up an LLM + bridge we simulate the
# sequence directly: each test builds a list of measurements and walks
# the same counter semantics. This couples the test to the production
# logic minus the wrap-around Ã¢â‚¬â€ acceptable because the counter itself
# is the unit under test; the embedding loop is covered by mypy/unit
# smoke in the integration scripts.

def _simulate_streak(amp_hold_values: list[float | None]) -> tuple[bool, int]:
    """Walk the streak rule through a sequence.

    Returns (tripped, final_streak). The rule, verbatim from agent.py:
      - if amp_hold is not None and < MIN:   streak += 1
      - else:                                 streak = 0
      - trip at streak >= LIMIT
    """
    streak = 0
    for v in amp_hold_values:
        amp = _coerce_float(v)
        if amp is not None and amp < SAFEGUARD_AMP_HOLD_MIN:
            streak += 1
        else:
            streak = 0
        if streak >= SAFEGUARD_CONSECUTIVE_LIMIT:
            return (True, streak)
    return (False, streak)


class TestSafeguardStreak:
    def test_three_consecutive_dips_trip(self):
        tripped, n = _simulate_streak([0.1, 0.2, 0.29])
        assert tripped is True
        assert n == 3

    def test_two_dips_do_not_trip(self):
        tripped, n = _simulate_streak([0.1, 0.2])
        assert tripped is False
        assert n == 2

    def test_dip_dip_recover_dip_does_not_trip(self):
        """Critical Ã‚Â§7 semantics: the streak must be **consecutive**.
        A recovery iteration resets the counter even if dips resume."""
        tripped, n = _simulate_streak([0.1, 0.2, 0.95, 0.1, 0.2])
        assert tripped is False
        assert n == 2

    def test_boundary_value_not_dip(self):
        """amp_hold_ratio == 0.3 is NOT a dip (strict <)."""
        tripped, _ = _simulate_streak([0.3, 0.3, 0.3, 0.3])
        assert tripped is False

    def test_missing_amp_hold_does_not_advance_streak(self):
        """None measurements ARE a reset, not a dip. Rationale: a
        missing metric could mean the LLM is just truncating an annotation,
        not that the circuit isn't oscillating."""
        tripped, n = _simulate_streak([0.1, None, 0.1])
        assert tripped is False
        assert n == 1  # last dip increments from reset

    def test_coerce_string_numeric(self):
        """_coerce_float tolerates stringly-typed measurements so a
        sloppy LLM quoting '0.1' as a string still triggers the rule."""
        tripped, _ = _simulate_streak(["0.1", "0.1", "0.1"])
        assert tripped is True

    def test_garbage_string_treated_as_missing(self):
        tripped, n = _simulate_streak([0.1, "oscillating", 0.1, 0.1])
        assert tripped is False
        assert n == 2


# ---------------------------------------------------------------- #
#  Stage 1 rev 3: SKILL-computed measurements override LLM's guess
# ---------------------------------------------------------------- #


class TestSkillMeasurementsOverride:
    """The agent must record `sim_result["measurements"]` in history,
    not the LLM's (often fabricated) measurements block. This is the
    regression test for the 2026-04-18 all-zero SAFEGUARD-abort bug:
    prior to rev 3 the LLM was being asked to derive the 7 metrics
    and would guess zeros, driving SAFEGUARD on every run.
    """

    @staticmethod
    def _make_llm_response(design_vars: dict, amp_hold: float) -> str:
        """Compose one fenced JSON block the agent's parser accepts."""
        import json

        payload = {
            "design_vars": design_vars,
            # LLM reports LIES here Ã¢â‚¬â€ the override must win.
            "measurements": {"amp_hold_ratio": amp_hold, "f_osc_GHz": 0.0},
            "pass_fail": {"f_osc": "FAIL"},
            "reasoning": "mock",
        }
        return "```json\n" + json.dumps(payload) + "\n```"

    def test_sim_measurements_win_over_llm(self, agent):
        agent.llm.chat.return_value = self._make_llm_response(
            {"nfin_cc": 12}, amp_hold=0.0
        )
        agent.bridge.run_ocean_sim.return_value = {
            "ok": True,
            "measurements": {
                "f_osc_GHz": 19.9,
                "amp_hold_ratio": 0.96,
                "V_diff_pp_V": 0.55,
            },
        }
        agent.bridge.write_and_save_maestro.return_value = {
            "ok": True, "saved": True, "wrote": 1,
        }
        agent.run(lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb", max_iter=1)

        assert len(agent.history) == 1
        # The recorded metric came from the bridge, not the LLM's zero.
        assert agent.history[0].measurements["f_osc_GHz"] == 19.9
        assert agent.history[0].measurements["amp_hold_ratio"] == 0.96

    def test_llm_fake_amp_hold_does_not_trip_safeguard(self, agent):
        """LLM says amp_hold=0.0 every iter (would trip SAFEGUARD
        pre-rev-3). Sim says amp_hold=0.95. Agent should NOT abort.
        Each iter proposes different vars to avoid the stuck_streak
        guard (R1a)."""
        responses = [
            self._make_llm_response({"nfin_cc": 12 + i}, amp_hold=0.0)
            for i in range(4)  # initial + 3 iterations
        ]
        agent.llm.chat.side_effect = responses
        agent.bridge.run_ocean_sim.return_value = {
            "ok": True,
            "measurements": {"amp_hold_ratio": 0.95, "f_osc_GHz": 19.9},
        }
        agent.bridge.write_and_save_maestro.return_value = {
            "ok": True, "saved": True, "wrote": 1,
        }
        result = agent.run(
            lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb", max_iter=3
        )
        # 3 iterations of LLM-reported amp_hold=0 would have tripped
        # SAFEGUARD before rev 3. Real amp_hold is 0.95 Ã¢â€ â€™ streak stays
        # at zero. Abort reason must be max_iter, NOT safeguard.
        assert result["abort_reason"] == "max_iter"

    def test_sim_without_measurements_falls_back_to_llm(self, agent):
        """If SKILL side returned no metrics (e.g. VT() unavailable),
        the agent must still surface the LLM's measurement block so
        SAFEGUARD has something to evaluate and the optimization loop
        is not silently blinded."""
        agent.llm.chat.return_value = self._make_llm_response(
            {"nfin_cc": 12}, amp_hold=0.91
        )
        agent.bridge.run_ocean_sim.return_value = {
            "ok": True, "measurements": {}, "measure_error": "VT()",
        }
        agent.bridge.write_and_save_maestro.return_value = {
            "ok": True, "saved": True, "wrote": 1,
        }
        agent.run(lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb", max_iter=1)

        assert agent.history[0].measurements["amp_hold_ratio"] == 0.91


# ---------------------------------------------------------------- #
#  Writeback preservation (Q2 directive)
# ---------------------------------------------------------------- #

class TestWritebackPreservation:
    def test_writeback_ok_returns_ok(self, agent):
        agent.bridge.write_and_save_maestro.return_value = {
            "ok": True, "saved": True, "wrote": 2,
        }
        status = agent._run_writeback({"nfin_cc": 12})
        assert status == "ok"

    def test_writeback_skipped_when_no_vars(self, agent):
        status = agent._run_writeback({})
        assert status == "skipped"
        agent.bridge.write_and_save_maestro.assert_not_called()

    def test_writeback_runtime_error_swallowed(self, agent):
        """Q2: Maestro-session-missing RuntimeError must NOT propagate Ã¢â‚¬â€
        the metrics report is authoritative and must be delivered."""
        agent.bridge.write_and_save_maestro.side_effect = RuntimeError(
            "no matching Maestro session"
        )
        status = agent._run_writeback({"nfin_cc": 12})
        assert status.startswith("failed:")
        assert "RuntimeError" in status

    def test_writeback_value_error_swallowed(self, agent):
        """A Layer-1/Layer-2 whitelist trip should fall into the same
        channel Ã¢â‚¬â€ user still gets metrics."""
        agent.bridge.write_and_save_maestro.side_effect = ValueError(
            "not allowed"
        )
        status = agent._run_writeback({"nfin_cc": 12})
        assert status.startswith("failed:")
        assert "ValueError" in status

    def test_writeback_connection_error_swallowed(self, agent):
        agent.bridge.write_and_save_maestro.side_effect = ConnectionError(
            "remote down"
        )
        status = agent._run_writeback({"nfin_cc": 12})
        assert status.startswith("failed:")

    def test_writeback_unexpected_exception_still_propagates(self, agent):
        """Negative control: truly unknown exceptions (e.g. a bug) MUST
        propagate so CI / users see them Ã¢â‚¬â€ only the three enumerated
        classes are swallowed. This is the progressive-unmasking guard
        against a future 'except Exception:' regression."""
        agent.bridge.write_and_save_maestro.side_effect = KeyError("unexpected")
        with pytest.raises(KeyError):
            agent._run_writeback({"nfin_cc": 12})

    def test_saved_false_surfaces_even_without_exception(self, agent):
        """Belt-and-suspenders: if the bridge returned ok:true,saved:false
        (bug in safe_bridge.py's own guard) the agent still flags it."""
        agent.bridge.write_and_save_maestro.return_value = {
            "ok": True, "saved": False,
        }
        status = agent._run_writeback({"nfin_cc": 12})
        assert status == "failed: saved=False"


# ---------------------------------------------------------------- #
#  _parse_llm_response
# ---------------------------------------------------------------- #

class TestParseLlmResponse:
    def test_fenced_json_block(self):
        text = 'prefix\n```json\n{"design_vars": {"nfin_cc": 12}}\n```\nsuffix'
        data = CircuitAgent._parse_llm_response(text)
        assert data == {"design_vars": {"nfin_cc": 12}}

    def test_unlabeled_fence_also_accepted(self):
        text = '```\n{"design_vars": {"R0": "10k"}}\n```'
        data = CircuitAgent._parse_llm_response(text)
        assert data == {"design_vars": {"R0": "10k"}}

    def test_bare_json_fallback(self):
        text = 'LLM chatter then {"design_vars": {"C0": "50f"}} trailing'
        data = CircuitAgent._parse_llm_response(text)
        assert data == {"design_vars": {"C0": "50f"}}

    def test_no_json_returns_empty_dict(self):
        data = CircuitAgent._parse_llm_response("no json here, sorry")
        assert data == {}

    def test_malformed_json_returns_empty(self):
        text = '```json\n{"design_vars": {"nfin_cc": 12,\n```'  # missing close
        data = CircuitAgent._parse_llm_response(text)
        assert data == {}


# ---------------------------------------------------------------- #
#  _format_topology round-trip (read_schematic.py consumer)
# ---------------------------------------------------------------- #

class TestSpecEmbedding:
    """Stage 1 rev 3 (2026-04-18): CircuitAgent.spec accepts str | dict.

    The run() first-turn prompt must embed MD text verbatim (no json.dumps
    wrapping), and must json.dumps a legacy dict inside a ```json fence.
    Exercised by inspecting the prompt the mock LLM receives.
    """

    def test_str_spec_embedded_verbatim(self, agent):
        agent.bridge.run_ocean_sim.return_value = {"ok": True}
        agent.llm.chat.return_value = ""  # will trigger no_changes abort
        agent.spec = (
            "# LC_VCO Spec\n\n## Ã‚Â§3 Metrics\n| Metric | Target |\n"
            "| --- | --- |\n| f_osc | 19.5Ã¢â‚¬â€œ20.5 GHz |\n"
        )
        agent.run(lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb", max_iter=1)

        prompt = agent.llm.chat.call_args[0][0][0]["content"]
        assert "## Ã‚Â§3 Metrics" in prompt
        assert "| f_osc | 19.5Ã¢â‚¬â€œ20.5 GHz |" in prompt
        # MUST NOT be wrapped in a ```json fence Ã¢â‚¬â€ that would destroy the
        # MD table structure and slow the LLM down.
        assert "```json\n# LC_VCO" not in prompt

    def test_dict_spec_json_dumped_in_fence(self, agent):
        agent.llm.chat.return_value = ""  # no_changes abort
        agent.spec = {"f_osc": "19.5", "V_diff_pp": "0.40-0.90"}
        agent.run(lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb", max_iter=1)

        prompt = agent.llm.chat.call_args[0][0][0]["content"]
        assert "```json" in prompt
        assert '"f_osc": "19.5"' in prompt
        assert '"V_diff_pp": "0.40-0.90"' in prompt


# ---------------------------------------------------------------- #
#  stuck_streak — empty-diff guard abort logic (R1a / R5)
# ---------------------------------------------------------------- #


class TestStuckStreak:
    """Exercises the stuck_streak counter in CircuitAgent.run().

    Uses legacy path (dict spec → no eval_block) so ocean_worker is
    not exercised; the empty-diff guard operates purely on
    accumulated_vars vs history[-1].design_vars.
    """

    @staticmethod
    def _make_llm_response(design_vars: dict) -> str:
        import json
        payload = {
            "design_vars": design_vars,
            "measurements": {"f_osc_GHz": 0.0},
            "pass_fail": {"f_osc": "FAIL"},
            "reasoning": "mock",
        }
        return "```json\n" + json.dumps(payload) + "\n```"

    def _setup_identical_vars_fail(self, agent, max_iter=3):
        """Wire agent so LLM always proposes identical vars + FAIL."""
        agent.llm.chat.return_value = self._make_llm_response(
            {"nfin_cc": 12}
        )
        agent.bridge.run_ocean_sim.return_value = {
            "ok": True,
            "measurements": {"f_osc_GHz": 0.0, "amp_hold_ratio": 0.95},
        }
        agent.bridge.write_and_save_maestro.return_value = {
            "ok": True, "saved": True, "wrote": 1,
        }
        return agent.run(
            lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb",
            max_iter=max_iter,
        )

    def test_stuck_streak_increments_on_identical_vars_fail(self, agent):
        """Two consecutive identical-vars-after-fail iterations should
        push stuck_streak to 2 and abort early (before max_iter=5)."""
        result = self._setup_identical_vars_fail(agent, max_iter=5)
        # Aborted at iter 3 (i=0: no guard, i=1: streak=1, i=2: streak=2 → abort)
        assert len(agent.history) == 2  # only 2 records before abort on i=2
        assert result["abort_reason"] == "stuck_identical_vars"

    def test_stuck_streak_resets_on_differing_vars(self, agent):
        """When the LLM proposes different vars mid-sequence, the
        streak must reset to 0 and the loop must NOT abort early."""
        responses = [
            # before loop: initial vars
            self._make_llm_response({"nfin_cc": 12}),
            # i=0 end: same vars (will trigger streak=1 at i=1)
            self._make_llm_response({"nfin_cc": 12}),
            # i=1 end: DIFFERENT vars (streak resets at i=2)
            self._make_llm_response({"nfin_cc": 14}),
            # i=2 end: consumed by final chat() call in loop
            self._make_llm_response({"nfin_cc": 14}),
        ]
        agent.llm.chat.side_effect = responses
        agent.bridge.run_ocean_sim.return_value = {
            "ok": True,
            "measurements": {"f_osc_GHz": 0.0, "amp_hold_ratio": 0.95},
        }
        agent.bridge.write_and_save_maestro.return_value = {
            "ok": True, "saved": True, "wrote": 1,
        }
        result = agent.run(
            lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb",
            max_iter=3,
        )
        # All 3 iters should run (streak=1 at i=1, resets at i=2)
        assert len(agent.history) == 3
        assert result["abort_reason"] == "max_iter"

    def test_stuck_identical_vars_abort_reason(self, agent):
        """The abort_reason must be exactly 'stuck_identical_vars'
        and the result dict must reflect the state at abort time."""
        result = self._setup_identical_vars_fail(agent, max_iter=10)
        assert result["abort_reason"] == "stuck_identical_vars"
        assert result["converged"] is False
        # design_vars should be the last accumulated set
        assert result["design_vars"]["nfin_cc"] == 12

    def test_stuck_streak_resets_when_perturb_modifies_ibias(self, agent, tmp_path):
        """When LLM repeats identical vars BUT has Ibias key,
        _auto_perturb_ibias mutates accumulated_vars in-place;
        next iter's same_vars check sees the perturbed value in
        history[-1].design_vars, so stuck_streak resets."""
        import json as _json

        ibias_vars = {"Ibias": "500u", "nfin_cc": 12}
        responses = [
            self._make_llm_response(ibias_vars),  # before loop
            self._make_llm_response(ibias_vars),  # i=0 end
            self._make_llm_response(ibias_vars),  # i=1 end
            self._make_llm_response(ibias_vars),  # i=2 end
        ]
        agent.llm.chat.side_effect = responses
        agent.bridge.run_ocean_sim.return_value = {
            "ok": True,
            "measurements": {"f_osc_GHz": 0.0, "amp_hold_ratio": 0.95},
        }
        agent.bridge.write_and_save_maestro.return_value = {
            "ok": True, "saved": True, "wrote": 1,
        }
        transcript = tmp_path / "transcript.jsonl"
        result = agent.run(
            lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb",
            max_iter=3,
            transcript_path=str(transcript),
        )
        # Perturb modifies Ibias on i=1 (streak=1), so i=2 sees
        # different prev.design_vars → streak resets. All 3 iters run.
        assert len(agent.history) == 3
        assert result["abort_reason"] == "max_iter"

        # Verify perturbed Ibias landed in history[1]
        # 500u * 2 = 1000u (1mA cap not hit: 500e-6*2 = 1e-3 = cap)
        assert agent.history[1].design_vars["Ibias"] == "1000u"

        # Verify JSONL event: proposed_vars is pre-perturb,
        # live_vars_after_guard is post-perturb
        events = [
            _json.loads(line)
            for line in transcript.read_text(encoding="utf-8").splitlines()
        ]
        guard_events = [
            _json.loads(e["content"])
            for e in events
            if e["role"] == "system"
            and "empty_diff_guard_fired" in e.get("content", "")
        ]
        assert len(guard_events) >= 1
        ev = guard_events[0]
        assert ev["perturb_applied"] is True
        assert ev["perturb_keys"] == ["Ibias"]
        # proposed_vars = LLM's raw submission (pre-perturb)
        assert ev["proposed_vars"]["Ibias"] == "500u"
        # live_vars_after_guard = post-perturb
        assert ev["live_vars_after_guard"]["Ibias"] == "1000u"
        assert ev["prev_meets_spec"] is False


class TestFormatTopology:
    def test_basic_instance_rendering(self):
        """read_schematic.py's preflight tool imports this staticmethod.
        A regression that breaks its output shape would silently corrupt
        the spec.md drafting workflow."""
        circuit = {
            "instances": [
                {"name": "M0", "cell": "NMOS", "params": {"w": "1u", "nfin": 8}},
            ],
            "pins": [{"name": "out_p", "direction": "output"}],
        }
        out = CircuitAgent._format_topology(circuit)
        assert "### Instances" in out
        assert "M0" in out and "NMOS" in out
        assert "w=1u" in out
        assert "### Pins" in out
        assert "out_p" in out


# ---------------------------------------------------------------- #
#  §4 contract enforcement (HARD CONSTRAINTS + one-shot repair)
# ---------------------------------------------------------------- #

class TestContractViolationDetection:
    """_check_contract_violation catches §4 violations."""

    def test_valid_response_no_violation(self):
        parsed = {
            "design_vars": {"C": "1.5f", "Ibias": "500u"},
            "reasoning": "ok",
            "measurements": {},
            "pass_fail": {},
            "iteration": 1,
        }
        assert CircuitAgent._check_contract_violation(parsed) is None

    def test_invalid_vars_triggers_violation(self):
        parsed = {
            "design_vars": {
                "I_sp": "none",
                "V_dd": "1.2",
                "target_frequency": "20",
            },
        }
        reason = CircuitAgent._check_contract_violation(parsed)
        assert reason is not None
        assert "I_sp" in reason
        assert "V_dd" in reason
        assert "target_frequency" in reason

    def test_missing_required_fields_triggers_violation(self):
        """Schema requires design_vars + measurements + pass_fail + reasoning."""
        parsed = {"reasoning": "thinking..."}
        reason = CircuitAgent._check_contract_violation(parsed)
        assert reason is not None
        assert "missing required top-level key(s)" in reason
        assert "design_vars" in reason
        assert "measurements" in reason
        assert "pass_fail" in reason

    def test_mixed_valid_and_invalid(self):
        """Even one invalid name triggers a violation."""
        parsed = {
            "design_vars": {"C": "1.5f", "I_bias_mA": "0.5"},
        }
        reason = CircuitAgent._check_contract_violation(parsed)
        assert reason is not None
        assert "I_bias_mA" in reason

    def test_unknown_top_level_keys(self):
        """Extra top-level keys like 'action', 'expected_outcome'."""
        parsed = {
            "design_vars": {"C": "1.5f"},
            "reasoning": "ok",
            "action": "tune capacitor",
            "expected_outcome": "higher freq",
        }
        reason = CircuitAgent._check_contract_violation(parsed)
        assert reason is not None
        assert "action" in reason
        assert "expected_outcome" in reason

    def test_physical_unit_suffix_rejected(self):
        """Values with physical units (mA, pF, GHz) trigger violation."""
        parsed = {
            "design_vars": {"Ibias": "500mA", "C": "1.5pF"},
        }
        reason = CircuitAgent._check_contract_violation(parsed)
        assert reason is not None
        assert "mA" in reason
        assert "pF" in reason

    def test_engineering_suffix_accepted(self):
        """Engineering suffixes (u, f, n, k) are fine."""
        parsed = {
            "design_vars": {"Ibias": "500u", "C": "1.5f", "R": "3k"},
            "measurements": {},
            "pass_fail": {},
            "reasoning": "ok",
        }
        assert CircuitAgent._check_contract_violation(parsed) is None


class TestJSONSchemaValidation:
    """Dedicated schema-level tests (F-A, 2026-04-22): required fields,
    types, and design-var whitelist coverage."""

    def test_fully_compliant_response(self):
        """Happy path — all 4 required fields present with correct types."""
        parsed = {
            "iteration": 3,
            "measurements": {"f_osc_GHz": 19.8},
            "pass_fail": {"f_osc_GHz": "PASS"},
            "reasoning": "f_osc centered in pass band.",
            "design_vars": {"L": "506p", "C": "50f"},
        }
        assert CircuitAgent._check_contract_violation(parsed) is None

    def test_missing_single_required_field(self):
        """Missing just one of the 4 required keys."""
        parsed = {
            "measurements": {},
            "pass_fail": {},
            "design_vars": {},
            # reasoning omitted
        }
        reason = CircuitAgent._check_contract_violation(parsed)
        assert reason is not None
        assert "missing required top-level key(s)" in reason
        assert "reasoning" in reason

    def test_missing_all_required_fields(self):
        """Empty dict — all 4 required keys missing."""
        reason = CircuitAgent._check_contract_violation({})
        assert reason is not None
        for key in ("design_vars", "measurements", "pass_fail", "reasoning"):
            assert key in reason

    def test_wrong_type_measurements_is_list(self):
        """measurements must be a dict, not a list."""
        parsed = {
            "measurements": [1, 2, 3],
            "pass_fail": {},
            "reasoning": "",
            "design_vars": {},
        }
        reason = CircuitAgent._check_contract_violation(parsed)
        assert reason is not None
        assert "'measurements' has wrong type" in reason
        assert "list" in reason

    def test_wrong_type_reasoning_is_dict(self):
        """reasoning must be a string, not a dict."""
        parsed = {
            "measurements": {},
            "pass_fail": {},
            "reasoning": {"why": "because"},
            "design_vars": {},
        }
        reason = CircuitAgent._check_contract_violation(parsed)
        assert reason is not None
        assert "'reasoning' has wrong type" in reason

    def test_design_vars_non_whitelist_key(self):
        """design_vars keys outside the spec whitelist trigger a violation."""
        # Pick a name that is guaranteed not in the whitelist.
        bad_name = "__not_a_real_var__"
        assert bad_name not in _VALID_DESIGN_VAR_NAMES
        parsed = {
            "measurements": {},
            "pass_fail": {},
            "reasoning": "",
            "design_vars": {bad_name: "1k"},
        }
        reason = CircuitAgent._check_contract_violation(parsed)
        assert reason is not None
        assert "invalid design_vars key(s)" in reason
        assert bad_name in reason

    def test_design_vars_whitelist_subset_ok(self):
        """Any subset of the whitelist (including single key) is fine."""
        # Pick one name actually in the whitelist at test time — keeps
        # the test spec-agnostic.
        first = sorted(_VALID_DESIGN_VAR_NAMES)[0]
        parsed = {
            "measurements": {},
            "pass_fail": {},
            "reasoning": "",
            "design_vars": {first: "1k"},
        }
        assert CircuitAgent._check_contract_violation(parsed) is None


class TestRepairFlowE2E:
    """End-to-end: one-shot repair triggers LLM re-request."""

    @staticmethod
    def _make_agent_with_responses(*responses):
        """Build a CircuitAgent whose LLM returns canned responses.

        The first response seeds the initial chat; subsequent ones are
        returned by successive chat() calls inside the loop.
        """
        import json

        llm = MagicMock()
        # First call = initial prompt response, rest = iteration / repair
        llm.chat.side_effect = list(responses)

        bridge = MagicMock()
        bridge._scope_lib = "pll"
        bridge._scope_tb_cell = "LC_VCO_tb"
        bridge.set_scope = MagicMock()
        bridge.list_design_vars.return_value = [
            {"name": "C", "default": "1f"},
        ]
        bridge.run_ocean_sim.return_value = {
            "ok": True,
            "resultsDir": "/tmp",
            "varsApplied": 1,
            "analyses": ["tran"],
            "measurements": {
                "f_osc_GHz": 20.0,
                "amp_hold_ratio": 0.95,
            },
        }
        bridge.write_and_save_maestro.return_value = {
            "ok": True, "saved": True, "varsWritten": 1,
        }
        bridge._is_allowed_param_name.return_value = True
        bridge.read_circuit.return_value = {"instances": []}

        agent = CircuitAgent(
            bridge=bridge,
            llm=llm,
            spec={"f_osc": "19.5"},
            analysis_type="tran",
            ocean_worker=MagicMock(),
        )
        # Disable eval_block to simplify the prompt path
        agent.eval_block = None
        return agent, llm

    def test_bad_response_triggers_repair_then_continues(self, tmp_path):
        """LLM first returns hallucinated keys, repair fixes it, sim runs."""
        import json

        bad_resp = json.dumps({
            "design_parameters": {"I_bias_mA": 1.0},
            "action": "increase bias",
        })
        good_resp = json.dumps({
            "iteration": 1,
            "design_vars": {"C": "1.5f"},
            "measurements": {"f_osc_GHz": 20.0},
            "pass_fail": {"f_osc_GHz": "PASS"},
            "reasoning": "tuned C",
        })
        agent, llm = self._make_agent_with_responses(
            bad_resp,   # initial chat → bad
            good_resp,  # repair retry → good
            good_resp,  # iteration 1 next prompt (won't be reached — converges)
        )
        result = agent.run(
            "pll", "LC_VCO", "LC_VCO_tb",
            max_iter=1,
            scs_path="/fake/input.scs",
            transcript_path=str(tmp_path / "t.jsonl"),
        )
        # LLM should have been called at least twice: initial + repair
        assert llm.chat.call_count >= 2
        # The repair message should mention "HARD CONSTRAINTS"
        repair_call_msgs = llm.chat.call_args_list[1][0][0]
        repair_user_msgs = [
            m for m in repair_call_msgs if m["role"] == "user"
        ]
        assert any("HARD CONSTRAINTS" in m["content"] for m in repair_user_msgs)

    def test_valid_response_no_repair(self, tmp_path):
        """Valid first response does NOT trigger a repair call."""
        import json

        good_resp = json.dumps({
            "iteration": 1,
            "design_vars": {"C": "1.5f"},
            "measurements": {"f_osc_GHz": 20.0},
            "pass_fail": {"f_osc_GHz": "PASS"},
            "reasoning": "tuned C",
        })
        agent, llm = self._make_agent_with_responses(
            good_resp,  # initial chat → good
            good_resp,  # iteration next (won't be reached)
        )
        result = agent.run(
            "pll", "LC_VCO", "LC_VCO_tb",
            max_iter=1,
            scs_path="/fake/input.scs",
            transcript_path=str(tmp_path / "t.jsonl"),
        )
        # Only the initial chat call — no repair needed
        assert llm.chat.call_count == 1
        # No user message should contain "HARD CONSTRAINTS" violation text
        initial_msgs = llm.chat.call_args_list[0][0][0]
        user_msgs = [m for m in initial_msgs if m["role"] == "user"]
        assert not any(
            "violated HARD CONSTRAINTS" in m["content"] for m in user_msgs
        )

    def test_bad_bad_aborts_without_sim(self, tmp_path):
        """Track C v2 raised the repair cap from 1 to 3 (see
        _CONTRACT_REPAIR_MAX in src/agent.py): 4 consecutive bad
        responses (initial + 3 repair attempts) → abort, no sim."""
        import json
        from src.agent import _CONTRACT_REPAIR_MAX

        bad_resp1 = json.dumps({
            "design_parameters": {"I_bias_mA": 1.0},
            "action": "increase bias",
        })
        bad_resp2 = json.dumps({
            "design_vars": {"V_dd": "1.2V"},
            "expected_outcome": "more headroom",
        })
        bad_resp3 = json.dumps({
            "design_vars": {"BogusName": "10"},
            "extra_key": "still wrong",
        })
        bad_resp4 = json.dumps({
            "design_vars": {"target_frequency": "20"},
            "still_wrong": True,
        })
        agent, llm = self._make_agent_with_responses(
            bad_resp1,  # initial chat → bad
            bad_resp2,  # repair attempt 1 → still bad
            bad_resp3,  # repair attempt 2 → still bad
            bad_resp4,  # repair attempt 3 → still bad, abort here
        )
        result = agent.run(
            "pll", "LC_VCO", "LC_VCO_tb",
            max_iter=3,
            scs_path="/fake/input.scs",
            transcript_path=str(tmp_path / "t.jsonl"),
        )
        # LLM called initial + cap repair attempts.
        assert llm.chat.call_count == 1 + _CONTRACT_REPAIR_MAX
        # No simulation should have run
        assert agent.bridge.run_ocean_sim.call_count == 0
        # Should abort with contract_violation reason
        assert result["abort_reason"] == "contract_violation"


# ---------------------------------------------------------------- #
#  _display_waveform — openResults + selectResult + plot
# ---------------------------------------------------------------- #

class TestDisplayWaveform:
    """_display_waveform best-effort wrapper + B3 spec-driven nets."""

    @pytest.fixture
    def wf_agent(self):
        a = CircuitAgent(
            bridge=MagicMock(),
            llm=MagicMock(),
            spec={"f_osc": "19.5"},
            analysis_type="tran",
            ocean_worker=MagicMock(),
        )
        # Simulate a parsed eval_block with a Vdiff signal
        a.eval_block = {
            "signals": [
                {
                    "name": "Vdiff",
                    "kind": "Vdiff",
                    "paths": ["/Vout_p", "/Vout_n"],
                },
            ],
            "windows": {},
            "metrics": [],
        }
        return a

    def test_delegates_to_bridge_with_vdiff_nets(self, wf_agent):
        """B3: nets come from spec's signals[kind==Vdiff].paths."""
        wf_agent.bridge.last_results_dir = "/home/user/sim/out"
        wf_agent._display_waveform({})
        wf_agent.bridge.display_transient_waveform.assert_called_once_with(
            "/home/user/sim/out", "/Vout_p", "/Vout_n"
        )

    def test_b3_custom_vdiff_paths(self, wf_agent):
        """Alternative Vdiff paths in spec are propagated to bridge."""
        wf_agent.eval_block["signals"][0]["paths"] = ["/outP", "/outN"]
        wf_agent.bridge.last_results_dir = "/sim/out"
        wf_agent._display_waveform({})
        wf_agent.bridge.display_transient_waveform.assert_called_once_with(
            "/sim/out", "/outP", "/outN"
        )

    def test_b3_no_eval_block_skips(self, wf_agent):
        """No eval_block in spec → skip display, no bridge call."""
        wf_agent.eval_block = None
        wf_agent.bridge.last_results_dir = "/sim/out"
        wf_agent._display_waveform({})
        wf_agent.bridge.display_transient_waveform.assert_not_called()

    def test_b3_no_vdiff_signal_skips(self, wf_agent):
        """Spec declares no Vdiff signal → skip display, no bridge call."""
        wf_agent.eval_block["signals"] = [
            {"name": "Vout_p", "kind": "V", "path": "/Vout_p"},
        ]
        wf_agent.bridge.last_results_dir = "/sim/out"
        wf_agent._display_waveform({})
        wf_agent.bridge.display_transient_waveform.assert_not_called()

    def test_empty_psf_dir_skips_display(self, wf_agent, caplog):
        """No results dir → log warning and return, no bridge call."""
        wf_agent.bridge.last_results_dir = None
        with caplog.at_level("WARNING"):
            wf_agent._display_waveform({})
        assert "no results dir" in caplog.text
        wf_agent.bridge.display_transient_waveform.assert_not_called()

    def test_bridge_exception_swallowed(self, wf_agent, caplog):
        """bridge.display_transient_waveform raising is swallowed."""
        wf_agent.bridge.last_results_dir = "/home/user/sim/out"
        wf_agent.bridge.display_transient_waveform.side_effect = RuntimeError("bad")
        with caplog.at_level("WARNING"):
            wf_agent._display_waveform({})
        assert "non-fatal" in caplog.text

    def test_bridge_exception_message_exposed(self, wf_agent, caplog):
        """Task D: exc message content (not just class) appears in log."""
        wf_agent.bridge.last_results_dir = "/home/user/sim/out"
        wf_agent.bridge.display_transient_waveform.side_effect = RuntimeError(
            "openResults failed; psfDir may not exist"
        )
        with caplog.at_level("WARNING"):
            wf_agent._display_waveform({})
        assert "RuntimeError" in caplog.text
        assert "openResults failed; psfDir may not exist" in caplog.text


# ---------------------------------------------------------------- #
#  unselectResult() per-iter cleanup (phase3 §3)
# ---------------------------------------------------------------- #

class TestUnselectResultCleanup:
    """A2: unselectResult() called each iter, best-effort."""

    @staticmethod
    def _run_one_iter_agent(tmp_path, *, unselect_side_effect=None):
        """Build agent that converges in 1 iter, return (agent, llm)."""
        import json as _json

        good_resp = _json.dumps({
            "iteration": 1,
            "design_vars": {"C": "1.5f"},
            "measurements": {"f_osc_GHz": 20.0},
            "pass_fail": {"f_osc_GHz": "PASS"},
            "reasoning": "tuned C",
        })
        llm = MagicMock()
        llm.chat.side_effect = [good_resp]

        bridge = MagicMock()
        bridge._scope_lib = "pll"
        bridge._scope_tb_cell = "LC_VCO_tb"
        bridge.list_design_vars.return_value = [{"name": "C", "default": "1f"}]
        bridge.run_ocean_sim.return_value = {
            "ok": True, "resultsDir": "/tmp",
            "varsApplied": 1, "analyses": ["tran"],
            "measurements": {"f_osc_GHz": 20.0, "amp_hold_ratio": 0.95},
        }
        bridge.write_and_save_maestro.return_value = {
            "ok": True, "saved": True, "varsWritten": 1,
        }
        bridge._is_allowed_param_name.return_value = True
        bridge.read_circuit.return_value = {"instances": []}
        bridge.last_results_dir = "/sim/out"

        if unselect_side_effect:
            bridge.client.execute_skill.side_effect = unselect_side_effect

        agent = CircuitAgent(
            bridge=bridge, llm=llm,
            spec={"f_osc": "19.5"}, analysis_type="tran",
            ocean_worker=MagicMock(),
        )
        agent.eval_block = None
        result = agent.run(
            "pll", "LC_VCO", "LC_VCO_tb",
            max_iter=1, scs_path="/fake/input.scs",
            transcript_path=str(tmp_path / "t.jsonl"),
        )
        return agent, bridge, result

    def test_unselect_result_called(self, tmp_path):
        """unselectResult() is called during the iteration (errset-wrapped)."""
        _, bridge, _ = self._run_one_iter_agent(tmp_path)
        unselect_calls = [
            c for c in bridge.client.execute_skill.call_args_list
            if "unselectResult()" in c[0][0]
        ]
        assert len(unselect_calls) >= 1
        # Must be wrapped in errset(... t) so CIW does not log an error
        # when the SKILL build doesn't expose unselectResult.
        assert all("errset" in c[0][0] for c in unselect_calls)

    def test_unselect_result_exception_swallowed(self, tmp_path):
        """unselectResult() failure does not abort the iteration."""
        _, bridge, result = self._run_one_iter_agent(
            tmp_path,
            unselect_side_effect=ConnectionError("remote host down"),
        )
        # The run should still converge despite unselectResult failing
        assert result["converged"] is True

    def test_op_point_exception_message_exposed(self, tmp_path, caplog):
        """Task D: read_op_point_after_tran exc message appears in warning."""
        import json as _json
        good_resp = _json.dumps({
            "iteration": 1,
            "design_vars": {"C": "1.5f"},
            "measurements": {"f_osc_GHz": 20.0},
            "pass_fail": {"f_osc_GHz": "PASS"},
            "reasoning": "tuned C",
        })
        llm = MagicMock()
        llm.chat.side_effect = [good_resp]

        bridge = MagicMock()
        bridge._scope_lib = "pll"
        bridge._scope_tb_cell = "LC_VCO_tb"
        bridge.list_design_vars.return_value = [{"name": "C", "default": "1f"}]
        bridge.run_ocean_sim.return_value = {
            "ok": True, "resultsDir": "/tmp",
            "varsApplied": 1, "analyses": ["tran"],
            "measurements": {"f_osc_GHz": 20.0, "amp_hold_ratio": 0.95},
        }
        bridge.write_and_save_maestro.return_value = {
            "ok": True, "saved": True, "varsWritten": 1,
        }
        bridge._is_allowed_param_name.return_value = True
        bridge.read_circuit.return_value = {"instances": []}
        bridge.last_results_dir = "/sim/out"
        bridge.read_op_point_after_tran.side_effect = RuntimeError(
            "selectResult('tranOp) failed; tran may not have run"
        )

        agent = CircuitAgent(
            bridge=bridge, llm=llm,
            spec={"f_osc": "19.5"}, analysis_type="tran",
            ocean_worker=MagicMock(),
        )
        agent.eval_block = None

        with caplog.at_level("WARNING"):
            agent.run(
                "pll", "LC_VCO", "LC_VCO_tb",
                max_iter=1, scs_path="/fake/input.scs",
                transcript_path=str(tmp_path / "t.jsonl"),
            )
        assert "read_op_point_after_tran failed" in caplog.text
        assert "RuntimeError" in caplog.text
        assert "selectResult('tranOp) failed; tran may not have run" in caplog.text


# ---------------------------------------------------------------- #
#  T8.8 — topology abort_reason classification
# ---------------------------------------------------------------- #

_SUSPECT_HI = "UNMEASURABLE (suspect: value 1e6 > sanity hi 1e3)"
_SUSPECT_LO = "UNMEASURABLE (suspect: value -5 < sanity lo 0)"


class TestHasSanityViolation:
    """Direct unit-tests of the sanity-violation detector."""

    def test_suspect_hi_detected(self):
        assert _has_sanity_violation({"f_osc": _SUSPECT_HI}) is True

    def test_suspect_lo_detected(self):
        assert _has_sanity_violation({"f_osc": _SUSPECT_LO}) is True

    def test_pass_only_returns_false(self):
        assert _has_sanity_violation({"f_osc": "PASS", "Vpp": "PASS"}) is False

    def test_plain_fail_returns_false(self):
        assert _has_sanity_violation({"f_osc": "FAIL (above 20.5)"}) is False

    def test_other_unmeasurable_flavors_not_topology(self):
        """Non-suspect UNMEASURABLE flavors are instrumentation problems,
        not topology — must NOT trip the topology streak."""
        assert _has_sanity_violation(
            {"f_osc": "UNMEASURABLE (no value)"}
        ) is False
        assert _has_sanity_violation(
            {"f_osc": "UNMEASURABLE (DumpStatus.TIMEOUT)"}
        ) is False
        assert _has_sanity_violation(
            {"f_osc": "UNMEASURABLE (mean: needs >=2 finite samples)"}
        ) is False

    def test_empty_dict_returns_false(self):
        assert _has_sanity_violation({}) is False

    def test_none_returns_false(self):
        assert _has_sanity_violation(None) is False

    def test_mixed_dict_any_suspect_trips(self):
        """A single sanity-violating metric is enough."""
        pf = {"f_osc": "PASS", "Vpp": _SUSPECT_HI, "noise": "FAIL (above 1)"}
        assert _has_sanity_violation(pf) is True

    def test_non_string_values_ignored(self):
        """Defensive: numeric/None values can't start with the prefix."""
        assert _has_sanity_violation({"f_osc": 0.0, "Vpp": None}) is False


def _simulate_topology_streak(verdicts: list[dict]) -> tuple[bool, int]:
    """Walk the topology streak rule across a sequence of pass_fail dicts.

    Returns (would_relabel_at_max_iter, final_streak). Mirrors the rule
    in agent.py: increment on any sanity-violation, reset to 0 otherwise.
    Relabel iff final streak >= TOPOLOGY_SANITY_VIOLATION_LIMIT.
    """
    streak = 0
    for pf in verdicts:
        if _has_sanity_violation(pf):
            streak += 1
        else:
            streak = 0
    return (streak >= TOPOLOGY_SANITY_VIOLATION_LIMIT, streak)


class TestTopologyStreak:
    """Streak-counter semantics (mirrors TestSafeguardStreak shape)."""

    def test_three_consecutive_relabel(self):
        relabel, n = _simulate_topology_streak(
            [{"m": _SUSPECT_HI}] * 3
        )
        assert relabel is True
        assert n == 3

    def test_two_consecutive_keep_max_iter(self):
        relabel, n = _simulate_topology_streak(
            [{"m": _SUSPECT_HI}] * 2
        )
        assert relabel is False
        assert n == 2

    def test_recovery_iter_resets_streak(self):
        """Final streak — not 'ever happened' — drives relabel.
        Sequence: 3 violations, then a recovery iter, then 2 violations.
        Final streak = 2 < 3, so DON'T relabel."""
        seq = (
            [{"m": _SUSPECT_HI}] * 3
            + [{"m": "FAIL (above 1)"}]
            + [{"m": _SUSPECT_LO}] * 2
        )
        relabel, n = _simulate_topology_streak(seq)
        assert relabel is False
        assert n == 2

    def test_pass_iter_resets_streak(self):
        relabel, n = _simulate_topology_streak(
            [{"m": _SUSPECT_HI}, {"m": _SUSPECT_HI}, {"m": "PASS"}]
        )
        assert relabel is False
        assert n == 0

    def test_other_unmeasurable_does_not_advance(self):
        """Instrumentation-flavor UNMEASURABLE is a streak reset."""
        relabel, n = _simulate_topology_streak(
            [{"m": _SUSPECT_HI}, {"m": "UNMEASURABLE (no value)"},
             {"m": _SUSPECT_HI}, {"m": _SUSPECT_HI}]
        )
        assert relabel is False
        assert n == 2

    def test_more_than_limit_still_relabels(self):
        relabel, n = _simulate_topology_streak(
            [{"m": _SUSPECT_HI}] * 7
        )
        assert relabel is True
        assert n == 7


class TestTopologyClassifierOcean:
    """End-to-end OCEAN-path: agent.run() must relabel max_iter→topology
    when the LAST N consecutive iters all carry sanity-violations."""

    @staticmethod
    def _llm_response(design_vars: dict, pass_fail: dict) -> str:
        import json
        payload = {
            "design_vars": design_vars,
            # amp_hold > 0.3 prevents safeguard from firing first.
            "measurements": {"amp_hold_ratio": 0.95, "f_osc_GHz": 19.9},
            "pass_fail": pass_fail,
            "reasoning": "mock",
        }
        return "```json\n" + json.dumps(payload) + "\n```"

    @staticmethod
    def _wire(agent, responses: list[str]) -> None:
        agent.llm.chat.side_effect = responses
        agent.bridge.run_ocean_sim.return_value = {
            "ok": True,
            "measurements": {"amp_hold_ratio": 0.95, "f_osc_GHz": 19.9},
        }
        agent.bridge.write_and_save_maestro.return_value = {
            "ok": True, "saved": True, "wrote": 1,
        }

    def test_three_consecutive_sanity_violations_relabel_topology(self, agent):
        """3 iters all sanity-violating + different vars (avoid stuck) →
        max_iter relabeled to topology."""
        sanity_pf = {"f_osc": _SUSPECT_HI}
        # initial response + 3 in-loop responses
        responses = [
            self._llm_response({"nfin_cc": 12 + i}, sanity_pf)
            for i in range(4)
        ]
        self._wire(agent, responses)
        result = agent.run(
            lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb", max_iter=3,
        )
        assert result["abort_reason"] == "topology"
        assert result["converged"] is False

    def test_two_consecutive_keeps_max_iter(self, agent):
        """Threshold strictly N=3; only 2 sanity iters → max_iter."""
        responses = [
            self._llm_response({"nfin_cc": 12}, {"f_osc": "FAIL (above 20.5)"}),
            self._llm_response({"nfin_cc": 13}, {"f_osc": "FAIL (above 20.5)"}),
            self._llm_response({"nfin_cc": 14}, {"f_osc": _SUSPECT_HI}),
            self._llm_response({"nfin_cc": 15}, {"f_osc": _SUSPECT_HI}),
        ]
        self._wire(agent, responses)
        result = agent.run(
            lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb", max_iter=3,
        )
        assert result["abort_reason"] == "max_iter"

    def test_recovery_iter_resets_streak_keeps_max_iter(self, agent):
        """3 sanity iters, then 1 plain-FAIL iter, then 1 sanity iter:
        final streak=1 < 3, so still max_iter (not topology)."""
        responses = [
            self._llm_response({"nfin_cc": 12}, {"f_osc": _SUSPECT_HI}),
            self._llm_response({"nfin_cc": 13}, {"f_osc": _SUSPECT_HI}),
            self._llm_response({"nfin_cc": 14}, {"f_osc": _SUSPECT_HI}),
            self._llm_response({"nfin_cc": 15}, {"f_osc": "FAIL (above 1)"}),
            self._llm_response({"nfin_cc": 16}, {"f_osc": _SUSPECT_HI}),
            self._llm_response({"nfin_cc": 17}, {"f_osc": _SUSPECT_HI}),
        ]
        self._wire(agent, responses)
        result = agent.run(
            lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb", max_iter=5,
        )
        assert result["abort_reason"] == "max_iter"

    def test_other_unmeasurable_flavor_does_not_relabel(self, agent):
        """UNMEASURABLE (no value) is instrumentation, not topology —
        max_iter, not topology."""
        responses = [
            self._llm_response(
                {"nfin_cc": 12 + i},
                {"f_osc": "UNMEASURABLE (no value)"},
            )
            for i in range(4)
        ]
        self._wire(agent, responses)
        result = agent.run(
            lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb", max_iter=3,
        )
        assert result["abort_reason"] == "max_iter"

    def test_converged_does_not_relabel(self, agent):
        """If the run converges, abort_reason stays None even if earlier
        iters had sanity-violations (only matters at max_iter)."""
        # Iter 1: sanity-violation. Iter 2: PASS → converged.
        sanity_pf = {"f_osc": _SUSPECT_HI}
        pass_pf = {"f_osc": "PASS"}
        responses = [
            self._llm_response({"nfin_cc": 12}, sanity_pf),
            self._llm_response({"nfin_cc": 13}, sanity_pf),
            self._llm_response({"nfin_cc": 14}, pass_pf),
        ]
        self._wire(agent, responses)
        # _log_final_converged_values reads bridge._scope_lib/_scope_tb_cell;
        # with a MagicMock bridge those auto-resolve to MagicMocks and the
        # SafeBridge log helper rejects them. Pin to None to keep the test
        # focused on abort_reason classification.
        agent.bridge._scope_lib = None
        agent.bridge._scope_tb_cell = None
        result = agent.run(
            lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb", max_iter=3,
        )
        assert result["abort_reason"] is None
        assert result["converged"] is True


class TestTopologyClassifierHspice:
    """End-to-end HSpice-path: HspiceAgent.run() must relabel max_iter→
    topology when the last N iters all carry sanity-violations."""

    @staticmethod
    def _llm_response(design_vars: dict) -> str:
        import json
        payload = {
            "design_vars": design_vars,
            "measurements": {},
            "pass_fail": {},
            "reasoning": "mock",
        }
        return "```json\n" + json.dumps(payload) + "\n```"

    @staticmethod
    def _build_agent(monkeypatch):
        """HspiceAgent with mocks that bypass ssh / hspice entirely.

        The remote patcher and the worker are MagicMocks; evaluate_hspice
        is monkey-patched with a programmable side_effect so each iter's
        pass_fail can be controlled."""
        from unittest.mock import MagicMock

        from src import agent as _agent_mod
        from src.hspice_resolver import EvaluationResult
        from src.remote_patch import RemotePatchResult

        agent = HspiceAgent.__new__(HspiceAgent)
        agent.llm = MagicMock()
        agent.worker = MagicMock()
        agent.spec_text = "spec"
        agent.spec_metrics = [{"name": "f_osc"}]
        agent.whitelist = frozenset({"nfin_cc"})
        agent.remote_target_path = "/remote/target.sp"
        agent.remote_run_path = "/remote/run.sp"
        agent._remote_patcher = MagicMock()
        agent.history = []

        agent._remote_patcher.patch.return_value = RemotePatchResult(
            keys_patched=1, backup_path="/remote/backup",
            noop=False, backup_already_existed=False,
        )

        # worker.run returns a real-ish HspiceRunResult; mt_files content
        # is irrelevant because evaluate_hspice is also mocked.
        agent.worker.run.return_value = HspiceRunResult(
            returncode=0, stdout_scrubbed="", stderr_scrubbed="",
            mt_files={}, lis_scrubbed=None,
            run_dir_remote="/remote/run", sp_base="run",
        )

        return agent, _agent_mod, EvaluationResult

    def test_three_consecutive_sanity_violations_relabel_topology(
        self, monkeypatch
    ):
        agent, mod, EvaluationResult = self._build_agent(monkeypatch)
        # initial + 3 in-loop responses
        responses = [
            self._llm_response({"nfin_cc": 12 + i}) for i in range(4)
        ]
        agent.llm.chat.side_effect = responses

        sanity_eval = EvaluationResult(
            measurements={"f_osc": [1e6]},
            pass_fail={"f_osc": _SUSPECT_HI},
            per_row_verdicts={"f_osc": [_SUSPECT_HI]},
        )
        monkeypatch.setattr(
            mod, "evaluate_hspice", lambda mt, m: sanity_eval,
        )

        result = agent.run(max_iter=3)
        assert result["abort_reason"] == "topology"
        assert result["converged"] is False

    def test_two_consecutive_keeps_max_iter(self, monkeypatch):
        agent, mod, EvaluationResult = self._build_agent(monkeypatch)
        responses = [
            self._llm_response({"nfin_cc": 12 + i}) for i in range(4)
        ]
        agent.llm.chat.side_effect = responses

        evals = [
            EvaluationResult(
                measurements={"f_osc": [0.0]},
                pass_fail={"f_osc": "FAIL (above 1)"},
                per_row_verdicts={"f_osc": ["FAIL (above 1)"]},
            ),
            EvaluationResult(
                measurements={"f_osc": [1e6]},
                pass_fail={"f_osc": _SUSPECT_HI},
                per_row_verdicts={"f_osc": [_SUSPECT_HI]},
            ),
            EvaluationResult(
                measurements={"f_osc": [1e6]},
                pass_fail={"f_osc": _SUSPECT_HI},
                per_row_verdicts={"f_osc": [_SUSPECT_HI]},
            ),
        ]
        eval_iter = iter(evals)
        monkeypatch.setattr(
            mod, "evaluate_hspice", lambda mt, m: next(eval_iter),
        )

        result = agent.run(max_iter=3)
        assert result["abort_reason"] == "max_iter"

    def test_converged_does_not_relabel(self, monkeypatch):
        agent, mod, EvaluationResult = self._build_agent(monkeypatch)
        responses = [self._llm_response({"nfin_cc": 12}) for _ in range(3)]
        agent.llm.chat.side_effect = responses

        evals = [
            EvaluationResult(
                measurements={"f_osc": [1e6]},
                pass_fail={"f_osc": _SUSPECT_HI},
                per_row_verdicts={"f_osc": [_SUSPECT_HI]},
            ),
            EvaluationResult(
                measurements={"f_osc": [19.9]},
                pass_fail={"f_osc": "PASS"},
                per_row_verdicts={"f_osc": ["PASS"]},
            ),
        ]
        eval_iter = iter(evals)
        monkeypatch.setattr(
            mod, "evaluate_hspice", lambda mt, m: next(eval_iter),
        )

        result = agent.run(max_iter=5)
        assert result["abort_reason"] is None
        assert result["converged"] is True


# ---------------------------------------------------------------- #
#  Path-2.5 (2026-05-19): spec-derived Maestro setup
# ---------------------------------------------------------------- #


class TestDeriveMaestroSetupFromSpec:
    """The agent must translate spec §2 (signals/windows/metrics) into a
    Maestro setup payload deterministically, replacing the LLM-emit path
    that small models (haiku-4-5) regularly mis-shape.

    The contract: ``_derive_maestro_setup_from_spec(tb_cell)`` returns
    a dict matching ``apply_maestro_setup``'s schema —
    ``{"analyses": [...], "outputs": [...]}`` — with one Maestro output
    per spec metric (V→VT, I→IT, Vdiff→VT-VT, Vsum_half→(VT+VT)/2) and
    a single ``tran`` analyses entry on the scoped testbench.
    """

    @staticmethod
    def _agent(spec_text: str) -> "CircuitAgent":
        return CircuitAgent(
            bridge=MagicMock(),
            llm=MagicMock(),
            spec=spec_text,
            ocean_worker=MagicMock(),
        )

    @staticmethod
    def _lc_vco_spec() -> str:
        """Minimal §2 fixture matching projects/lc_vco_base/.../spec.md."""
        return (
            "# LC_VCO\n\n"
            "## 2. Eval block\n\n"
            "```yaml\n"
            "signals:\n"
            "  - name: Vdiff\n"
            "    kind: Vdiff\n"
            "    paths: [\"/Vout_p\", \"/Vout_n\"]\n"
            "  - name: Vcm\n"
            "    kind: Vsum_half\n"
            "    paths: [\"/Vout_p\", \"/Vout_n\"]\n"
            "  - name: Vout_p\n"
            "    kind: V\n"
            "    path: \"/Vout_p\"\n"
            "  - name: I_tail\n"
            "    kind: I\n"
            "    path: \"/I0/M2/D\"\n"
            "\n"
            "windows:\n"
            "  full:  [1.0e-7, 2.0e-7]\n"
            "  late:  [1.5e-7, 2.0e-7]\n"
            "  early: [7.5e-8, 1.25e-7]\n"
            "\n"
            "metrics:\n"
            "  - {name: f_osc_GHz, signal: Vdiff, window: full, "
            "stat: freq_Hz, scale: 1.0e-9, pass: [19.5, 20.5]}\n"
            "  - {name: V_diff_pp_V, signal: Vdiff, window: late, "
            "stat: ptp, pass: [0.40, null]}\n"
            "  - {name: V_cm_V, signal: Vcm, window: late, "
            "stat: mean, pass: [0.70, 0.81]}\n"
            "  - {name: I_core_uA, signal: I_tail, window: late, "
            "stat: mean_abs, scale: 1.0e6, pass: [null, 800]}\n"
            "  - name: amp_hold_ratio\n"
            "    compound: ratio\n"
            "    numerator:   {signal: Vdiff, window: late,  stat: rms}\n"
            "    denominator: {signal: Vdiff, window: early, stat: rms}\n"
            "    pass: [0.95, null]\n"
            "```\n"
        )

    def test_returns_empty_when_no_eval_block(self):
        """A dict-spec agent has no eval_block → empty dict signals the
        caller to fall through to the legacy LLM-emit path."""
        agent = CircuitAgent(
            bridge=MagicMock(), llm=MagicMock(),
            spec={"f_osc": "19.5"}, ocean_worker=MagicMock(),
        )
        assert agent._derive_maestro_setup_from_spec("LC_VCO_tb") == {}

    def test_analyses_is_single_tran_on_scoped_tb_cell(self):
        agent = self._agent(self._lc_vco_spec())
        out = agent._derive_maestro_setup_from_spec("LC_VCO_tb")
        assert out["analyses"] == [{
            "test": "LC_VCO_tb",
            "analysis": "tran",
            "enable": True,
        }]

    def test_outputs_one_entry_per_metric(self):
        """Snapshot: 5 metrics -> 5 outputs; expression matches metric math."""
        agent = self._agent(self._lc_vco_spec())
        out = agent._derive_maestro_setup_from_spec("LC_VCO_tb")
        by_name = {o["name"]: o for o in out["outputs"]}
        # 4 simple + 1 compound-ratio (numerator signal = Vdiff)
        assert sorted(by_name) == sorted([
            "f_osc_GHz", "V_diff_pp_V", "V_cm_V",
            "I_core_uA", "amp_hold_ratio",
        ])
        # The derive helper delegates to ``maestro_metric_sync`` so
        # derived outputs mirror the PC-side evaluator's metric math.
        assert by_name["f_osc_GHz"]["expr"] == (
            '(frequency(clip((VT("/Vout_p") - VT("/Vout_n")) '
            '1e-07 2e-07)) * 1e-09)'
        )
        assert by_name["V_diff_pp_V"]["expr"] == (
            'peakToPeak(clip((VT("/Vout_p") - VT("/Vout_n")) '
            '1.5e-07 2e-07))'
        )
        assert by_name["V_cm_V"]["expr"] == (
            'average(clip(((VT("/Vout_p") + VT("/Vout_n")) / 2.0) '
            '1.5e-07 2e-07))'
        )
        assert by_name["I_core_uA"]["expr"] == (
            '(average(abs(clip(IT("/I0/M2/D") 1.5e-07 2e-07))) * '
            '1000000.0)'
        )
        assert by_name["amp_hold_ratio"]["expr"] == (
            '(rms(clip((VT("/Vout_p") - VT("/Vout_n")) '
            '1.5e-07 2e-07)) / rms(clip((VT("/Vout_p") - '
            'VT("/Vout_n")) 7.5e-08 1.25e-07)))'
        )
        # Every output is keyed to the scoped tb_cell as the test.
        for entry in out["outputs"]:
            assert entry["test"] == "LC_VCO_tb"
            assert entry["output_type"] == ""

    def test_v_kind_uses_single_path(self):
        """A signal of kind V feeds the metric expression builder."""
        spec = (
            "```yaml\n"
            "signals:\n"
            "  - {name: VA, kind: V, path: \"/A\"}\n"
            "windows:\n"
            "  full: [0, 1.0e-7]\n"
            "metrics:\n"
            "  - {name: VA_rms, signal: VA, window: full, stat: rms, "
            "pass: [null, 1.0]}\n"
            "```\n"
        )
        agent = self._agent(spec)
        out = agent._derive_maestro_setup_from_spec("tb")
        assert out["outputs"][0]["expr"] == 'rms(clip(VT("/A") 0.0 1e-07))'

    def test_unknown_signal_kind_skipped_not_crash(self):
        """An unknown ``kind`` produces no output for that metric rather
        than a malformed expression. spec_evaluator already validates
        kinds upstream, but the helper must be robust if a kind sneaks
        through (e.g. future schema extension)."""
        agent = self._agent(self._lc_vco_spec())
        # Mutate eval_block in-place after construction to inject a kind
        # the helper doesn't understand.
        agent.eval_block["signals"].append({
            "name": "Mystery", "kind": "Q_dot",
            "paths": ["/whatever"],
        })
        agent.eval_block["metrics"].append({
            "name": "mystery_metric", "signal": "Mystery",
            "window": "full", "stat": "mean", "pass": [0, 1],
        })
        out = agent._derive_maestro_setup_from_spec("LC_VCO_tb")
        assert "mystery_metric" not in {o["name"] for o in out["outputs"]}
        # Known metrics still get emitted.
        assert "f_osc_GHz" in {o["name"] for o in out["outputs"]}

    def test_derived_payload_passes_setup_block_validator(self):
        """Round-trip: every derived entry must pass
        ``validate_maestro_setup_block`` so ``apply_maestro_setup`` can
        consume it without any contract-repair detour."""
        from src.maestro_setup import validate_maestro_setup_block
        agent = self._agent(self._lc_vco_spec())
        payload = agent._derive_maestro_setup_from_spec("LC_VCO_tb")
        assert validate_maestro_setup_block(payload) is None


class TestStripLLMSetupBlocksWhenDerived:
    """R2 P1 codex BLOCKER: ``_strip_llm_setup_blocks_if_derived`` MUST
    remove the four Maestro setup keys from the parsed LLM response
    BEFORE the contract check sees them whenever the spec-derived path
    is active. Otherwise the per-entry shape validator inside
    ``validate_maestro_setup_block`` flags small-model typos
    (``outputs: dict``, ``analysis: 'transient'``) as contract
    violations, burning every repair retry on a payload the agent
    intends to ignore anyway.
    """

    @staticmethod
    def _agent_with_eval_block() -> "CircuitAgent":
        spec = (
            "```yaml\n"
            "signals:\n"
            "  - {name: V, kind: V, path: \"/A\"}\n"
            "windows:\n"
            "  full: [0, 1.0e-7]\n"
            "metrics:\n"
            "  - {name: V_rms, signal: V, window: full, stat: rms, "
            "pass: [null, 1.0]}\n"
            "```\n"
        )
        agent = CircuitAgent(
            bridge=MagicMock(), llm=MagicMock(),
            spec=spec, ocean_worker=MagicMock(),
        )
        assert agent.eval_block is not None  # sanity for the test
        return agent

    def test_contract_check_skips_maestro_setup_keys_when_eval_block_present(
        self,
    ):
        """The PoC from codex's review: haiku's malformed ``outputs:
        dict`` + ``analysis: 'transient'`` payload must NOT trip
        contract violation when the agent has a spec-derived path
        because the stripper drops those keys first."""
        agent = self._agent_with_eval_block()
        parsed = {
            "outputs": {"name": "m", "expr": 'VT("/V")'},  # bad shape
            "analyses": [{"test": "tb", "analysis": "transient",
                          "enable": True}],  # bad analysis name
            "measurements": {},
            "pass_fail": {},
            "reasoning": "x",
            "design_vars": {},
        }
        agent._strip_llm_setup_blocks_if_derived(parsed, iter_idx=0)
        # The four setup keys are gone; the rest of the payload is intact.
        assert "outputs" not in parsed
        assert "analyses" not in parsed
        assert "tests" not in parsed
        assert "corners" not in parsed
        for k in ("measurements", "pass_fail", "reasoning", "design_vars"):
            assert k in parsed
        # Post-strip, the static contract checker should green-light it.
        assert CircuitAgent._check_contract_violation(parsed) is None

    def test_strip_is_noop_when_eval_block_is_none(self):
        """Dict-spec agents (legacy LLM-emit path) must keep getting
        the maestro_setup blocks fed to the contract validator —
        otherwise the legacy flow loses its per-entry shape gate."""
        agent = CircuitAgent(
            bridge=MagicMock(), llm=MagicMock(),
            spec={"f_osc": "19.5"}, ocean_worker=MagicMock(),
        )
        parsed = {
            "outputs": [{"name": "m", "expr": 'VT("/V")'}],
            "measurements": {}, "pass_fail": {},
            "reasoning": "x", "design_vars": {},
        }
        before = dict(parsed)
        agent._strip_llm_setup_blocks_if_derived(parsed, iter_idx=0)
        assert parsed == before  # untouched

    def test_strip_is_noop_when_no_setup_keys_present(self):
        """When the LLM correctly omits all four blocks (the new
        steady-state behavior under the path-2.5 prompt), the helper
        must not log a misleading 'ignoring' warning."""
        agent = self._agent_with_eval_block()
        parsed = {
            "measurements": {}, "pass_fail": {},
            "reasoning": "x", "design_vars": {},
        }
        before = dict(parsed)
        agent._strip_llm_setup_blocks_if_derived(parsed, iter_idx=0)
        assert parsed == before


class TestDerivedSetupPromptConditional:
    """R2 P2: the 'Do NOT emit tests/analyses/outputs/corners'
    instruction must only appear when the spec-derived path is active.
    Issuing it unconditionally would regress legacy dict-spec projects
    that still depend on the LLM-emit path
    (``_slice_maestro_setup_payload``)."""

    @staticmethod
    def _str_spec() -> str:
        return (
            "# Spec\n\n"
            "```yaml\n"
            "signals:\n"
            "  - {name: V, kind: V, path: \"/A\"}\n"
            "windows:\n"
            "  full: [0, 1.0e-7]\n"
            "metrics:\n"
            "  - {name: V_rms, signal: V, window: full, stat: rms, "
            "pass: [null, 1.0]}\n"
            "```\n"
        )

    def test_derive_note_present_when_eval_block_active(self, agent):
        agent.spec = self._str_spec()
        from src import spec_evaluator
        agent.eval_block = spec_evaluator.extract_eval_block(agent.spec)
        assert agent.eval_block is not None
        agent.llm.chat.return_value = ""  # no_changes abort
        agent.run(lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb", max_iter=1)
        prompt = agent.llm.chat.call_args[0][0][0]["content"]
        assert "Maestro setup is derived from the spec automatically" in prompt
        assert "Do NOT emit `tests`, `analyses`, `outputs`, or `corners`" in prompt

    def test_derive_note_absent_when_no_eval_block(self, agent):
        """Legacy dict-spec path: no eval_block → no instruction."""
        # The `agent` fixture is dict-spec by default → eval_block=None.
        assert agent.eval_block is None
        agent.llm.chat.return_value = ""  # no_changes abort
        agent.run(lib="pll", cell="LC_VCO", tb_cell="LC_VCO_tb", max_iter=1)
        prompt = agent.llm.chat.call_args[0][0][0]["content"]
        assert "Maestro setup is derived from the spec automatically" not in prompt


class TestDerivedSetupAppliedFlag:
    """R2 P3 NIT 3: ``_maestro_setup_applied`` must flip to True after
    a successful apply, and stay False after a failure so the next
    iter re-attempts the derived payload (rather than silently leaving
    Maestro's Outputs Setup empty for the rest of the run)."""

    @staticmethod
    def _spec() -> str:
        return (
            "```yaml\n"
            "signals:\n"
            "  - {name: V, kind: V, path: \"/A\"}\n"
            "windows:\n"
            "  full: [0, 1.0e-7]\n"
            "metrics:\n"
            "  - {name: V_rms, signal: V, window: full, stat: rms, "
            "pass: [null, 1.0]}\n"
            "```\n"
        )

    def test_flag_starts_false(self):
        agent = CircuitAgent(
            bridge=MagicMock(), llm=MagicMock(),
            spec=self._spec(), ocean_worker=MagicMock(),
        )
        assert agent._maestro_setup_applied is False


class TestResolveMaestroSetupTest:
    @staticmethod
    def _agent_with_bridge(bridge: MagicMock) -> "CircuitAgent":
        return CircuitAgent(
            bridge=bridge, llm=MagicMock(),
            spec=TestDerivedSetupAppliedFlag._spec(),
            ocean_worker=MagicMock(),
        )

    def test_explicit_maestro_test_wins(self):
        bridge = MagicMock()
        bridge._resolve_maestro_test.side_effect = lambda test: test
        agent = self._agent_with_bridge(bridge)
        assert agent._resolve_maestro_setup_test(
            tb_cell="LC_VCO_tb",
            maestro_test="pll_LC_VCO_tb_1",
        ) == "pll_LC_VCO_tb_1"
        bridge._list_remote_maestro_tests.assert_not_called()

    def test_auto_uses_tb_cell_when_it_is_test_row(self):
        bridge = MagicMock()
        bridge._list_remote_maestro_tests.return_value = {
            "LC_VCO_tb", "other_test",
        }
        agent = self._agent_with_bridge(bridge)
        assert agent._resolve_maestro_setup_test(
            tb_cell="LC_VCO_tb",
            maestro_test=None,
        ) == "LC_VCO_tb"

    def test_auto_uses_sole_test_row_when_tb_cell_differs(self):
        bridge = MagicMock()
        bridge._list_remote_maestro_tests.return_value = {"pll_LC_VCO_tb_1"}
        agent = self._agent_with_bridge(bridge)
        assert agent._resolve_maestro_setup_test(
            tb_cell="LC_VCO_tb",
            maestro_test=None,
        ) == "pll_LC_VCO_tb_1"

    def test_auto_skips_when_ambiguous(self, caplog):
        bridge = MagicMock()
        bridge._list_remote_maestro_tests.return_value = {"a", "b"}
        agent = self._agent_with_bridge(bridge)
        with caplog.at_level("WARNING"):
            resolved = agent._resolve_maestro_setup_test(
                tb_cell="LC_VCO_tb",
                maestro_test=None,
            )
        assert resolved is None
        assert any("Maestro setup sync skipped" in r.message for r in caplog.records)


# ====================================================================== #
#  Path-2 (2026-05-19) — _ensure_sweep_manifest + _derive_sweep_entries
#  Authoring side: the agent writes .tuning_manifest.json from spec.md
#  §6.1 so Maestro doesn't need to know anything about sweep ordering.
# ====================================================================== #


class TestDeriveSweepEntries:
    """``_derive_sweep_entries`` translates spec.md §6.1 ``sweep:`` into
    a list of {point, vctrl} records — same shape that ``read_sweep_manifest``
    parses on the way back in."""

    @staticmethod
    def _spec_with_sweep(points: int, lo: float, hi: float) -> str:
        return (
            "```yaml\n"
            "signals:\n"
            "  - {name: V, kind: V, path: \"/A\"}\n"
            "windows:\n"
            "  full: [0, 1.0e-7]\n"
            "metrics:\n"
            "  - {name: V_rms, signal: V, window: full, stat: rms, "
            "pass: [null, 1.0]}\n"
            "```\n"
            "```yaml\n"
            f"sweep: {{variable: Vctrl, range: [{lo}, {hi}], "
            f"points: {points}, unit: V}}\n"
            "tuning_metrics:\n"
            "  - {name: tuning_range, op: swept_max_minus_min, of: V_rms, "
            "pass: [0.0, null]}\n"
            "```\n"
        )

    def test_9_points_0_to_0_8_matches_baseline_curve(self):
        agent = CircuitAgent(
            bridge=MagicMock(), llm=MagicMock(),
            spec=self._spec_with_sweep(9, 0.0, 0.8),
            ocean_worker=MagicMock(),
        )
        entries = agent._derive_sweep_entries()
        assert len(entries) == 9
        points = [e["point"] for e in entries]
        assert points == list(range(1, 10))
        vctrls = [e["vctrl"] for e in entries]
        # Endpoints exact, mid-points equispaced 0.1 V (matches the
        # baseline f–V curve in projects/lc_vco_base/constraints/spec.md).
        assert vctrls[0] == pytest.approx(0.0)
        assert vctrls[-1] == pytest.approx(0.8)
        for i in range(1, 9):
            assert vctrls[i] - vctrls[i - 1] == pytest.approx(0.1)

    def test_raises_when_no_sweep_block(self):
        spec_no_sweep = (
            "```yaml\n"
            "signals:\n"
            "  - {name: V, kind: V, path: \"/A\"}\n"
            "windows:\n"
            "  full: [0, 1.0e-7]\n"
            "metrics:\n"
            "  - {name: V_rms, signal: V, window: full, stat: rms, "
            "pass: [null, 1.0]}\n"
            "```\n"
        )
        agent = CircuitAgent(
            bridge=MagicMock(), llm=MagicMock(),
            spec=spec_no_sweep, ocean_worker=MagicMock(),
        )
        with pytest.raises(ValueError, match=r"(?i)sweep"):
            agent._derive_sweep_entries()


class TestEnsureSweepManifest:
    """``_ensure_sweep_manifest(sweep_root)`` returns None when the file
    is ready to read (existed-and-matched OR freshly written), and a
    string reason when the caller must abort (mismatch on disk, write
    failed, derive failed, etc.). The bridge calls are mocked."""

    _ROOT = "/home/u/sim/cell_tb/maestro/results/maestro/Interactive.0"

    @staticmethod
    def _agent_with_sweep(bridge: MagicMock) -> CircuitAgent:
        spec = (
            "```yaml\n"
            "signals:\n"
            "  - {name: V, kind: V, path: \"/A\"}\n"
            "windows:\n"
            "  full: [0, 1.0e-7]\n"
            "metrics:\n"
            "  - {name: V_rms, signal: V, window: full, stat: rms, "
            "pass: [null, 1.0]}\n"
            "```\n"
            "```yaml\n"
            "sweep: {variable: Vctrl, range: [0.0, 0.8], points: 9, unit: V}\n"
            "tuning_metrics:\n"
            "  - {name: tuning_range, op: swept_max_minus_min, of: V_rms, "
            "pass: [0.0, null]}\n"
            "```\n"
        )
        return CircuitAgent(
            bridge=bridge, llm=MagicMock(),
            spec=spec, ocean_worker=MagicMock(),
        )

    def test_no_op_when_spec_has_no_sweep(self):
        """Dict-spec (legacy) or single-point spec.md: ensure-helper must
        be a true no-op so existing projects don't trip the new path."""
        bridge = MagicMock()
        bridge.read_sweep_manifest = MagicMock(
            side_effect=AssertionError("must not be called")
        )
        bridge.write_sweep_manifest = MagicMock(
            side_effect=AssertionError("must not be called")
        )
        agent = CircuitAgent(
            bridge=bridge, llm=MagicMock(),
            spec={"f_osc": "20"}, ocean_worker=MagicMock(),
        )
        assert agent._ensure_sweep_manifest(self._ROOT) is None

    def test_writes_when_file_missing(self):
        bridge = MagicMock()
        bridge.read_sweep_manifest.side_effect = RuntimeError(
            "No .tuning_manifest.json at sweep root"
        )
        bridge.write_sweep_manifest.return_value = 9
        agent = self._agent_with_sweep(bridge)
        reason = agent._ensure_sweep_manifest(self._ROOT)
        assert reason is None
        bridge.write_sweep_manifest.assert_called_once()
        args, _ = bridge.write_sweep_manifest.call_args
        assert args[0] == self._ROOT
        entries = args[1]
        assert len(entries) == 9
        assert entries[0] == {"point": 1, "vctrl": pytest.approx(0.0)}
        assert entries[-1] == {"point": 9, "vctrl": pytest.approx(0.8)}

    def test_skips_write_when_existing_matches_spec(self):
        bridge = MagicMock()
        bridge.read_sweep_manifest.return_value = {
            i + 1: round(i * 0.1, 1) for i in range(9)
        }
        bridge.write_sweep_manifest = MagicMock(
            side_effect=AssertionError("must not overwrite a matching file")
        )
        agent = self._agent_with_sweep(bridge)
        reason = agent._ensure_sweep_manifest(self._ROOT)
        assert reason is None
        bridge.write_sweep_manifest.assert_not_called()

    def test_skips_write_when_existing_has_shuffled_mapping(self):
        """Maestro's point execution order can be shuffled. The manifest's
        point->Vctrl mapping should be accepted when it has the same point set
        and the same Vctrl grid, even if point 1 is not the low endpoint."""
        bridge = MagicMock()
        bridge.read_sweep_manifest.return_value = {
            1: 0.3, 2: 0.0, 3: 0.8, 4: 0.4, 5: 0.1,
            6: 0.7, 7: 0.2, 8: 0.6, 9: 0.5,
        }
        bridge.write_sweep_manifest = MagicMock(
            side_effect=AssertionError("must not overwrite a matching file")
        )
        agent = self._agent_with_sweep(bridge)
        reason = agent._ensure_sweep_manifest(self._ROOT)
        assert reason is None
        bridge.write_sweep_manifest.assert_not_called()

    def test_aborts_with_mismatch_when_existing_disagrees(self, caplog):
        """Hand-written manifest takes precedence over the spec — the
        agent refuses to silently overwrite it. Caller must see the
        ``manifest_mismatch`` reason and surface UNMEASURABLE."""
        bridge = MagicMock()
        # 3-point file on disk vs 9-point spec → mismatch
        bridge.read_sweep_manifest.return_value = {1: 0.0, 2: 0.4, 3: 0.8}
        bridge.write_sweep_manifest = MagicMock(
            side_effect=AssertionError("must not overwrite a mismatching file")
        )
        agent = self._agent_with_sweep(bridge)
        with caplog.at_level("WARNING"):
            reason = agent._ensure_sweep_manifest(self._ROOT)
        assert reason == "manifest_mismatch"
        bridge.write_sweep_manifest.assert_not_called()
        assert any(
            "does not match spec" in rec.message for rec in caplog.records
        )

    def test_aborts_with_mismatch_when_same_length_point_set_differs(self):
        bridge = MagicMock()
        bridge.read_sweep_manifest.return_value = {
            1: 0.0, 2: 0.1, 3: 0.2, 4: 0.3, 5: 0.4,
            6: 0.5, 7: 0.6, 8: 0.7, 99: 0.8,
        }
        bridge.write_sweep_manifest = MagicMock(
            side_effect=AssertionError("must not overwrite a mismatching file")
        )
        agent = self._agent_with_sweep(bridge)
        assert agent._ensure_sweep_manifest(self._ROOT) == "manifest_mismatch"
        bridge.write_sweep_manifest.assert_not_called()

    def test_aborts_with_mismatch_when_vctrl_grid_differs(self):
        bridge = MagicMock()
        bridge.read_sweep_manifest.return_value = {
            1: 0.0, 2: 0.1, 3: 0.2, 4: 0.3, 5: 0.45,
            6: 0.5, 7: 0.6, 8: 0.7, 9: 0.8,
        }
        bridge.write_sweep_manifest = MagicMock(
            side_effect=AssertionError("must not overwrite a mismatching file")
        )
        agent = self._agent_with_sweep(bridge)
        assert agent._ensure_sweep_manifest(self._ROOT) == "manifest_mismatch"
        bridge.write_sweep_manifest.assert_not_called()

    def test_aborts_when_write_fails(self):
        bridge = MagicMock()
        bridge.read_sweep_manifest.side_effect = RuntimeError(
            "manifest missing"
        )
        bridge.write_sweep_manifest.side_effect = RuntimeError(
            "disk full"
        )
        agent = self._agent_with_sweep(bridge)
        reason = agent._ensure_sweep_manifest(self._ROOT)
        assert reason == "manifest_write_failed"

    def test_drops_cached_manifest_after_write(self):
        """A stale entry in ``_sweep_manifest_cache`` from a previous
        iter would shadow the newly-written file. The ensure helper
        must invalidate it so the next read picks up fresh values."""
        bridge = MagicMock()
        bridge.read_sweep_manifest.side_effect = RuntimeError(
            "manifest missing"
        )
        bridge.write_sweep_manifest.return_value = 9
        agent = self._agent_with_sweep(bridge)
        agent._sweep_manifest_cache[self._ROOT] = {1: 9.99}  # stale
        agent._ensure_sweep_manifest(self._ROOT)
        assert self._ROOT not in agent._sweep_manifest_cache


class TestRunSweepPhase:
    _ROOT = "/home/u/sim/cell_tb/maestro/results/maestro/Interactive.0"

    @staticmethod
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

    def test_unwraps_dump_all_wrapper_before_swept_eval(self):
        """Real SafeBridge swept dumps return {"ok": true, "dumps": ...}.
        The sweep evaluator must hand the inner dumps payload to
        spec_evaluator.evaluate(); otherwise every base metric reads as
        None and tuning metrics become UNMEASURABLE."""
        bridge = MagicMock()
        bridge.read_sweep_manifest.return_value = {1: 0.0, 2: 0.1, 3: 0.2}
        bridge.write_sweep_manifest = MagicMock(
            side_effect=AssertionError("manifest already matches")
        )
        bridge.run_ocean_dump_all_swept.return_value = {
            1: {"ok": True, "dumps": {"V": {"full": {"rms": 1.0}}}},
            2: {"ok": True, "dumps": {"V": {"full": {"rms": 2.0}}}},
            3: {"ok": True, "dumps": {"V": {"full": {"rms": 3.0}}}},
        }
        agent = self._agent_with_sweep(bridge)

        measurements, pass_fail = agent._run_sweep_phase(
            sweep_results_root=self._ROOT,
            tb_cell="LC_VCO_tb",
            result_test="pll_LC_VCO_tb_1",
        )

        assert measurements["tuning_range"] == pytest.approx(2.0)
        assert pass_fail["tuning_range"] == "PASS"

    def test_fresh_sweep_reruns_current_design_vars_per_vctrl(self):
        """Closed-loop tuning must not reuse stale Interactive.0 PSFs.

        The manifest still supplies the point-to-Vctrl mapping, but each
        point is simulated with the current design variables so tuning
        verdicts track the LLM's latest C/L/varactor proposal.
        """
        bridge = MagicMock()
        bridge.read_sweep_manifest.return_value = {1: 0.2, 2: 0.0, 3: 0.1}
        bridge.write_sweep_manifest = MagicMock(
            side_effect=AssertionError("manifest already matches")
        )
        bridge.run_ocean_dump_all_swept = MagicMock(
            side_effect=AssertionError("must not read stale sweep PSFs")
        )

        def fake_run_ocean_sim(**kwargs):
            vctrl = kwargs["design_vars"]["Vctrl"]
            bridge.last_results_dir = f"/tmp/psf_vctrl_{vctrl}"
            return {"ok": True}

        bridge.run_ocean_sim.side_effect = fake_run_ocean_sim
        agent = self._agent_with_sweep(bridge)
        agent.ocean_worker.dump_all.side_effect = [
            {"ok": True, "dumps": {"V": {"full": {"rms": 1.0}}}},
            {"ok": True, "dumps": {"V": {"full": {"rms": 2.0}}}},
            {"ok": True, "dumps": {"V": {"full": {"rms": 3.0}}}},
        ]

        measurements, pass_fail = agent._run_sweep_phase(
            sweep_results_root=self._ROOT,
            tb_cell="LC_VCO_tb",
            result_test="pll_LC_VCO_tb_1",
            lib="pll",
            cell="LC_VCO",
            design_vars={"C": "69f", "L": "574p", "Vctrl": "0.4"},
            analyses=["tran"],
        )

        called_vctrls = [
            call.kwargs["design_vars"]["Vctrl"]
            for call in bridge.run_ocean_sim.call_args_list
        ]
        assert called_vctrls == [0.0, 0.1, 0.2]
        assert all(
            call.kwargs["design_vars"]["C"] == "69f"
            for call in bridge.run_ocean_sim.call_args_list
        )
        bridge.run_ocean_dump_all_swept.assert_not_called()
        assert measurements["tuning_range"] == pytest.approx(2.0)
        assert pass_fail["tuning_range"] == "PASS"
