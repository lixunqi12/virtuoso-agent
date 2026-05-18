"""Track C v2 agent-loop integration tests.

Covers:
  * ``_check_contract_violation`` now accepts the four optional
    structural blocks (tests/analyses/outputs/corners)
  * Backward compat: legacy responses (no v2 keys) still pass
  * The contract repair cap is now 3 (was 1 pre-v2)
  * Per-block per-entry validation triggers the repair loop

This file deliberately keeps to static-method-level tests rather than
spinning up a full ``run()`` loop — the in-loop dispatch (iter 0 vs
iter > 0 gating) is exercised through behavior assertions on the
helper module rather than a full integration run, because the actual
loop has 480+ lines of unrelated wiring (op-point analysis, SAFEGUARD
streaks, etc.) that aren't germane to this contract change.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.agent import (  # noqa: E402
    CircuitAgent, _CONTRACT_REPAIR_MAX, _VALID_RESPONSE_KEYS,
)


# --------------------------------------------------------------------- #
#  Contract validator accepts new optional blocks
# --------------------------------------------------------------------- #


class TestContractAcceptsV2Blocks:
    """The legacy validator must keep working AND accept the four new
    optional structural blocks, validating their internal shape via the
    delegated maestro_setup helper."""

    def _base_response(self):
        # Minimal legacy-compliant response — passes pre-v2.
        return {
            "design_vars": {"C": "1.5f"},
            "measurements": {},
            "pass_fail": {},
            "reasoning": "ok",
        }

    def test_legacy_response_still_passes(self):
        assert CircuitAgent._check_contract_violation(
            self._base_response()
        ) is None

    @pytest.mark.parametrize("key", [
        "tests", "analyses", "outputs", "corners",
    ])
    def test_empty_v2_block_passes(self, key):
        parsed = self._base_response()
        parsed[key] = []
        assert CircuitAgent._check_contract_violation(parsed) is None

    def test_well_formed_v2_blocks_pass(self):
        parsed = self._base_response()
        parsed.update({
            "tests":    [{"name": "T1", "lib": "L", "cell": "C",
                          "simulator": "spectre"}],
            "analyses": [{"test": "T1", "analysis": "tran"}],
            "outputs":  [{"name": "Vrms", "expr": "rms(VT(/V))"}],
            "corners":  [{"name": "tt_25"}],
        })
        assert CircuitAgent._check_contract_violation(parsed) is None

    def test_malformed_outputs_signal_xor_expr(self):
        parsed = self._base_response()
        parsed["outputs"] = [
            {"name": "x", "signal_name": "/V", "expr": "rms(VT(/V))"},
        ]
        err = CircuitAgent._check_contract_violation(parsed)
        assert err is not None
        assert "exactly one" in err

    def test_malformed_block_triggers_violation(self):
        parsed = self._base_response()
        parsed["tests"] = [{"lib": "L"}]  # missing name + cell
        err = CircuitAgent._check_contract_violation(parsed)
        assert err is not None
        assert "tests[0]" in err

    def test_unknown_top_level_key_still_rejected(self):
        parsed = self._base_response()
        parsed["nonsense_key"] = 42
        err = CircuitAgent._check_contract_violation(parsed)
        assert err is not None
        assert "unknown top-level" in err

    def test_v2_block_wrong_type_rejected_at_top_level(self):
        # ``tests: {"x": 1}`` — wrong top-level type (must be list).
        parsed = self._base_response()
        parsed["tests"] = {"x": 1}
        err = CircuitAgent._check_contract_violation(parsed)
        assert err is not None
        assert "tests" in err


# --------------------------------------------------------------------- #
#  Repair-attempt cap raised to 3
# --------------------------------------------------------------------- #


class TestContractRepairCap:
    def test_cap_is_three(self):
        assert _CONTRACT_REPAIR_MAX == 3


# --------------------------------------------------------------------- #
#  Schema-key allow-list registers the v2 fields
# --------------------------------------------------------------------- #


class TestValidResponseKeys:
    """Belt-and-suspenders: ensure the contract allow-list lists every
    v2 key so a future refactor that adds a key elsewhere can't ship
    without updating the schema gate."""

    @pytest.mark.parametrize("key", [
        "tests", "analyses", "outputs", "corners",
    ])
    def test_v2_key_is_allow_listed(self, key):
        assert key in _VALID_RESPONSE_KEYS

    @pytest.mark.parametrize("key", [
        # Legacy keys must remain.
        "iteration", "measurements", "pass_fail", "reasoning", "design_vars",
    ])
    def test_legacy_key_still_allow_listed(self, key):
        assert key in _VALID_RESPONSE_KEYS


# --------------------------------------------------------------------- #
#  R2 P2-3: iter > 0 dispatch slice
# --------------------------------------------------------------------- #


class TestIterGatingDispatchSlice:
    """``_slice_maestro_setup_payload`` strips structural keys past
    iter 0 so an LLM mid-loop can't restructure the live testbench.
    Tested in isolation (no full ``run()`` loop) — the helper is a
    pure function of ``(parsed, iter_idx)``.
    """

    def _parsed(self):
        # A complete v2 payload: structural + additive blocks.
        return {
            "tests":    [{"name": "TB", "lib": "L", "cell": "C"}],
            "analyses": [{"test": "TB", "analysis": "tran"}],
            "corners":  [{"name": "tt_25"}],
            "outputs":  [{"name": "Vrms", "expr": "rms(VT(/V))"}],
            "design_vars": {"C": "1.5f"},
        }

    def test_iter_zero_forwards_full_payload(self):
        log = MagicMock()
        payload = CircuitAgent._slice_maestro_setup_payload(
            self._parsed(), iter_idx=0, log=log,
        )
        # Iter 0 is the setup phase — all four blocks pass through.
        assert "tests" in payload
        assert "analyses" in payload
        assert "corners" in payload
        assert "outputs" in payload
        # No WARN should fire on iter 0.
        assert not log.warning.called

    def test_iter_one_strips_structural_keys(self):
        log = MagicMock()
        payload = CircuitAgent._slice_maestro_setup_payload(
            self._parsed(), iter_idx=1, log=log,
        )
        # Only ``outputs`` survives — additive after setup.
        assert set(payload.keys()) == {"outputs"}
        # A WARN must mention every rejected structural block.
        assert log.warning.called
        warn_msg = log.warning.call_args.args[0] % log.warning.call_args.args[1:]
        assert "tests" in warn_msg
        assert "analyses" in warn_msg
        assert "corners" in warn_msg

    def test_iter_one_outputs_only_no_warning(self):
        # If the LLM only proposes outputs (no tests/analyses/corners)
        # after iter 0, that's fine — no warning, payload passes
        # through.
        log = MagicMock()
        parsed = {"outputs": [{"name": "v", "expr": "rms(VT(/V))"}]}
        payload = CircuitAgent._slice_maestro_setup_payload(
            parsed, iter_idx=1, log=log,
        )
        assert payload == {"outputs": parsed["outputs"]}
        assert not log.warning.called

    def test_no_structural_keys_returns_none(self):
        # No v2 keys present — nothing to dispatch.
        log = MagicMock()
        assert CircuitAgent._slice_maestro_setup_payload(
            {"design_vars": {"C": "1.5f"}}, iter_idx=0, log=log,
        ) is None
        assert CircuitAgent._slice_maestro_setup_payload(
            {"design_vars": {"C": "1.5f"}}, iter_idx=3, log=log,
        ) is None

    def test_iter_one_with_only_stripped_keys_returns_none(self):
        # LLM proposed only ``tests`` post-iter-0 — all stripped, nothing
        # to apply. Helper returns None so the caller skips the
        # ``apply_maestro_setup`` call entirely.
        log = MagicMock()
        parsed = {"tests": [{"name": "TB", "lib": "L", "cell": "C"}]}
        payload = CircuitAgent._slice_maestro_setup_payload(
            parsed, iter_idx=1, log=log,
        )
        assert payload is None
        # WARN still fires — the LLM's intent is visible in the log.
        assert log.warning.called
