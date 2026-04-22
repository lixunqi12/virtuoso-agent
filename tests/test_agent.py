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
    SAFEGUARD_AMP_HOLD_MIN,
    SAFEGUARD_CONSECUTIVE_LIMIT,
    _VALID_DESIGN_VAR_NAMES,
    _coerce_float,
)


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
        """Two consecutive bad responses → abort, no simulation."""
        import json

        bad_resp1 = json.dumps({
            "design_parameters": {"I_bias_mA": 1.0},
            "action": "increase bias",
        })
        bad_resp2 = json.dumps({
            "design_vars": {"V_dd": "1.2V"},
            "expected_outcome": "more headroom",
        })
        agent, llm = self._make_agent_with_responses(
            bad_resp1,  # initial chat → bad
            bad_resp2,  # repair retry → still bad
        )
        result = agent.run(
            "pll", "LC_VCO", "LC_VCO_tb",
            max_iter=3,
            scs_path="/fake/input.scs",
            transcript_path=str(tmp_path / "t.jsonl"),
        )
        # LLM called exactly twice: initial + one repair
        assert llm.chat.call_count == 2
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

