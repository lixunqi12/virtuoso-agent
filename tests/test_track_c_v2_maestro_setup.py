"""Unit tests for Track C v2 ``src.maestro_setup``.

Covers:
  * ``validate_maestro_setup_block`` schema gate (per-entry shape,
    required/optional fields, unknown-key rejection, xor on
    signal_name vs expr for outputs)
  * ``apply_maestro_setup`` dispatch (correct method called per block,
    fixed apply order, per-entry fail-soft)
  * Backward compat: an LLM response with NONE of the four blocks
    must produce a clean no-op summary, never raise.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.maestro_setup import (  # noqa: E402
    apply_maestro_setup,
    validate_maestro_setup_block,
)
from src.safe_bridge import SafeBridge  # noqa: E402


@pytest.fixture
def pdk_map_file(tmp_path):
    content = """\
generic_cell_name: "GENERIC_DEVICE"
valid_aliases:
  - NMOS
model_info_keys:
  - toxe
allowed_params:
  - w
"""
    path = tmp_path / "pdk_map.yaml"
    path.write_text(content, encoding="utf-8")
    return str(path)


def _make_client_mock() -> MagicMock:
    """``VirtuosoClient`` mock with a tame ``execute_skill`` default —
    the R2 v2 wrappers now consult SKILL probes (``maeGetSetup`` for
    remote test dedup, ``maeDeleteOutput`` for v2 wins) and the default
    MagicMock ``errors`` attribute is truthy, which would break the
    happy path. Returning an empty ``output`` + ``errors=None`` mirrors
    the "no remote state" condition exercised by most apply tests."""
    client = MagicMock()
    client.execute_skill.return_value = MagicMock(output="", errors=None)
    return client


@pytest.fixture
def bridge(pdk_map_file, tmp_path):
    b = SafeBridge(
        _make_client_mock(), pdk_map_file,
        skill_dir=tmp_path / "no_skill",
    )
    b.set_scope("mylib", "MYCELL", tb_cell="MYTB")
    return b


@pytest.fixture
def writer_mocks():
    with (
        patch("src.safe_bridge._mae_writer.create_test") as m_create,
        patch("src.safe_bridge._mae_writer.set_analysis") as m_an,
        patch("src.safe_bridge._mae_writer.add_output") as m_add,
        patch("src.safe_bridge._mae_writer.set_spec") as m_spec,
        patch("src.safe_bridge._mae_writer.setup_corner") as m_corner,
    ):
        for m in (m_create, m_an, m_add, m_spec, m_corner):
            m.return_value = "ok"
        yield {
            "create_test": m_create, "set_analysis": m_an,
            "add_output": m_add, "set_spec": m_spec,
            "setup_corner": m_corner,
        }


# --------------------------------------------------------------------- #
#  Schema validator
# --------------------------------------------------------------------- #


class TestSchemaValidator:

    def test_no_structural_blocks_returns_none(self):
        # Backward compat: legacy LLM response with only design_vars.
        assert validate_maestro_setup_block({
            "design_vars": {"w": "10u"},
            "measurements": {},
            "pass_fail": {},
            "reasoning": "",
        }) is None

    def test_empty_lists_return_none(self):
        # LLM explicitly declares "no proposals" — fine, no error.
        assert validate_maestro_setup_block({
            "tests": [], "analyses": [], "outputs": [], "corners": [],
        }) is None

    @pytest.mark.parametrize("block,entry", [
        ("tests",    {"name": "T1", "lib": "L", "cell": "C"}),
        ("analyses", {"test": "T1", "analysis": "tran"}),
        ("outputs",  {"name": "VDD_dc", "signal_name": "/Vdd"}),
        ("outputs",  {"name": "VDD_rms", "expr": "rms(VT(/Vdd))"}),
        ("corners",  {"name": "tt"}),
    ])
    def test_each_block_minimal_required(self, block, entry):
        assert validate_maestro_setup_block({block: [entry]}) is None

    @pytest.mark.parametrize("block,entry,missing_field", [
        ("tests",    {"lib": "L", "cell": "C"},             "name"),
        ("tests",    {"name": "T1", "cell": "C"},           "lib"),
        ("analyses", {"analysis": "tran"},                  "test"),
        ("analyses", {"test": "T1"},                        "analysis"),
        ("outputs",  {"signal_name": "/V"},                 "name"),
        ("corners",  {},                                    "name"),
    ])
    def test_missing_required_field(self, block, entry, missing_field):
        err = validate_maestro_setup_block({block: [entry]})
        assert err is not None
        assert missing_field in err

    @pytest.mark.parametrize("block,entry", [
        ("tests",    {"name": "T1", "lib": "L", "cell": "C", "junk": 1}),
        ("analyses", {"test": "T1", "analysis": "tran", "extra": "x"}),
        ("outputs",  {"name": "v", "signal_name": "/V", "wat": True}),
        ("corners",  {"name": "tt", "rogue": []}),
    ])
    def test_unknown_field_rejected(self, block, entry):
        err = validate_maestro_setup_block({block: [entry]})
        assert err is not None
        assert "unknown" in err

    def test_output_needs_signal_xor_expr(self):
        # Both present — error.
        err = validate_maestro_setup_block({"outputs": [
            {"name": "x", "signal_name": "/V", "expr": "rms(VT(/V))"},
        ]})
        assert err is not None and "exactly one" in err
        # Neither present — error.
        err = validate_maestro_setup_block({"outputs": [{"name": "x"}]})
        assert err is not None and "exactly one" in err

    @pytest.mark.parametrize("block,value", [
        ("tests",    "not a list"),
        ("analyses", {"oops": "dict"}),
        ("outputs",  42),
        ("corners",  None),
    ])
    def test_block_not_a_list_rejected(self, block, value):
        err = validate_maestro_setup_block({block: value})
        assert err is not None
        assert "list" in err

    def test_entry_not_a_dict_rejected(self):
        err = validate_maestro_setup_block({"tests": ["not_a_dict", 42]})
        assert err is not None
        assert "object" in err

    def test_multiple_blocks_problems_concatenate(self):
        err = validate_maestro_setup_block({
            "tests":    [{"name": "T1"}],                  # missing lib/cell
            "analyses": [{"analysis": "tran"}],             # missing test
        })
        assert err is not None
        # Both problems present.
        assert "tests[0]" in err
        assert "analyses[0]" in err


# --------------------------------------------------------------------- #
#  apply_maestro_setup
# --------------------------------------------------------------------- #


class TestApplyMaestroSetup:

    def test_no_blocks_is_clean_noop(self, bridge, writer_mocks):
        out = apply_maestro_setup(bridge, {"design_vars": {"w": "1u"}})
        assert out["applied"]  # dict shape
        assert sum(len(v) for v in out["applied"].values()) == 0
        # No writer called.
        for m in writer_mocks.values():
            assert not m.called

    def test_tests_block_calls_create_test(self, bridge, writer_mocks):
        out = apply_maestro_setup(bridge, {
            "tests": [{"name": "TB_A", "lib": "mylib", "cell": "MYCELL",
                       "simulator": "spectre"}],
        })
        assert writer_mocks["create_test"].called
        assert out["applied"]["tests"] == ["TB_A"]

    def test_analyses_block_calls_set_analysis(self, bridge, writer_mocks):
        out = apply_maestro_setup(bridge, {
            "analyses": [{"test": "MYTB", "analysis": "tran",
                          "options": {"stop": "200n"}}],
        })
        assert writer_mocks["set_analysis"].called
        assert out["applied"]["analyses"] == ["MYTB:tran"]

    def test_outputs_block_calls_add_output(self, bridge, writer_mocks):
        out = apply_maestro_setup(bridge, {
            "outputs": [{"name": "Vp_rms", "expr": "rms(VT(/Vp))"}],
        })
        assert writer_mocks["add_output"].called
        assert out["applied"]["outputs"] == ["Vp_rms"]

    def test_outputs_with_pass_bounds_calls_set_spec(self, bridge, writer_mocks):
        apply_maestro_setup(bridge, {
            "outputs": [{"name": "f_Hz", "expr": "frequency(VT(/V))",
                         "pass": [19.5e9, 20.5e9]}],
        })
        assert writer_mocks["add_output"].called
        assert writer_mocks["set_spec"].called

    def test_corners_block_calls_setup_corner(self, bridge, writer_mocks):
        out = apply_maestro_setup(bridge, {
            "corners": [{"name": "tt_25"}],
        })
        assert writer_mocks["setup_corner"].called
        assert out["applied"]["corners"] == ["tt_25"]

    def test_apply_order_is_tests_analyses_corners_outputs(
        self, bridge, writer_mocks,
    ):
        # The leader's spec: tests → analyses → corners → outputs.
        # Build a payload with all four; record call order via a shared
        # ticker, then assert the sequence.
        call_log: list[str] = []
        for name, m in writer_mocks.items():
            m.side_effect = lambda *a, _name=name, **kw: (
                call_log.append(_name) or "ok"
            )
        apply_maestro_setup(bridge, {
            "outputs":  [{"name": "Vrms", "expr": "rms(VT(/V))"}],
            "tests":    [{"name": "T", "lib": "mylib", "cell": "MYCELL"}],
            "analyses": [{"test": "MYTB", "analysis": "tran"}],
            "corners":  [{"name": "tt_25"}],
        })
        assert call_log == [
            "create_test", "set_analysis", "setup_corner", "add_output",
        ]

    def test_per_entry_failure_is_fail_soft(self, bridge, writer_mocks):
        # Two outputs; the first one's add raises, the second should
        # still go through.
        writer_mocks["add_output"].side_effect = [
            ValueError("expr rejected"),
            "ok",
        ]
        out = apply_maestro_setup(bridge, {
            "outputs": [
                {"name": "bad", "expr": "rms(VT(/V))"},
                {"name": "good", "expr": "rms(VT(/Vout))"},
            ],
        })
        assert out["applied"]["outputs"] == ["good"]
        assert out["skipped"]["outputs"][0][0] == "bad"

    def test_set_spec_failure_keeps_output_in_applied(self, bridge, writer_mocks):
        # Output landed; bound write fails — output still counts.
        writer_mocks["set_spec"].side_effect = RuntimeError("session lost")
        out = apply_maestro_setup(bridge, {
            "outputs": [
                {"name": "v", "expr": "rms(VT(/V))",
                 "pass": [0.0, 1.0]},
            ],
        })
        assert out["applied"]["outputs"] == ["v"]
        assert writer_mocks["add_output"].called

    def test_non_dict_payload_returns_empty_summary(self, bridge, writer_mocks):
        out = apply_maestro_setup(bridge, "not a dict")  # type: ignore[arg-type]
        assert out["applied"]
        assert sum(len(v) for v in out["applied"].values()) == 0
        for m in writer_mocks.values():
            assert not m.called

    def test_simulator_rejection_is_warn_skip_not_abort(
        self, bridge, writer_mocks,
    ):
        # An LLM proposes one bad sim and one good — apply should skip
        # the bad and keep the good.
        out = apply_maestro_setup(bridge, {
            "tests": [
                {"name": "T_bad",  "lib": "mylib", "cell": "MYCELL",
                 "simulator": "ngspice"},
                {"name": "T_good", "lib": "mylib", "cell": "MYCELL",
                 "simulator": "spectre"},
            ],
        })
        assert out["applied"]["tests"] == ["T_good"]
        assert out["skipped"]["tests"][0][0] == "T_bad"

    def test_path_injection_in_output_expr_is_warn_skipped(
        self, bridge, writer_mocks,
    ):
        # Re-confirms the R2 P1 fix interacts cleanly with v2: a path
        # that would have closed VT(...) still gets caught at the
        # SafeBridge gate, not by the maestro_setup dispatcher.
        # Patch add_output to raise like real SafeBridge would (because
        # the path injection happens at the bridge layer when the LLM
        # passes a poisoned signal_name).
        writer_mocks["add_output"].side_effect = ValueError(
            "Invalid signal_name"
        )
        out = apply_maestro_setup(bridge, {
            "outputs": [
                {"name": "evil", "signal_name": '/V) 0 0)) + VT(/SECRET'},
            ],
        })
        assert out["applied"]["outputs"] == []
        assert out["skipped"]["outputs"][0][0] == "evil"


# --------------------------------------------------------------------- #
#  R2 P1-2: Option I × v2 outputs dedup + v2-wins remove/re-add
# --------------------------------------------------------------------- #


class TestOutputsDedup:
    """When Option I sync has already added an output and the LLM
    proposes the same name in its v2 outputs block, v2 wins —
    SafeBridge removes the prior row and re-adds with the LLM's expr.

    R3 (2026-05-15): cache key is the resolved ``(name, test,
    session)`` tuple — the R2 implementation used a bare-name set and
    collapsed two tests' same-name outputs into a single bucket.
    """

    def test_pre_added_output_triggers_remove_then_add(
        self, bridge, writer_mocks,
    ):
        # Simulate Option I sync having recorded "m" already. The
        # scoped tb_cell is "MYTB" (see fixture), so the resolved key
        # is ``("m", "MYTB", "")``.
        bridge._added_maestro_outputs.add(("m", "MYTB", ""))
        # Patch the remote delete so we can assert it was called.
        with patch.object(
            bridge, "_delete_maestro_output_remote",
        ) as m_delete:
            out = apply_maestro_setup(bridge, {
                "outputs": [
                    {"name": "m", "expr": "rms(VT(/V))"},
                ],
            })
        assert m_delete.called
        assert m_delete.call_args.args[0] == "m"
        # And the v2 add still ran with the LLM's expression.
        assert writer_mocks["add_output"].called
        kwargs = writer_mocks["add_output"].call_args.kwargs
        assert kwargs["expr"] == "rms(VT(/V))"
        assert out["applied"]["outputs"] == ["m"]

    def test_unseen_output_skips_remove_branch(
        self, bridge, writer_mocks,
    ):
        # Clean state — no prior add. The dispatcher should NOT touch
        # the remote delete probe (saves a round-trip).
        with patch.object(
            bridge, "_delete_maestro_output_remote",
        ) as m_delete:
            apply_maestro_setup(bridge, {
                "outputs": [
                    {"name": "fresh", "expr": "rms(VT(/Vout))"},
                ],
            })
        assert not m_delete.called
        assert writer_mocks["add_output"].called
        # After a successful add the bridge MUST have recorded the
        # tuple — required for the next dedup pass to fire.
        assert ("fresh", "MYTB", "") in bridge._added_maestro_outputs

    def test_remove_failure_still_attempts_add(self, bridge, writer_mocks):
        # If the delete probe fails (e.g. SKILL transport hiccup), the
        # apply is still issued — Maestro's add-overwrites-by-name
        # semantics gives the v2 expr a chance to land.
        bridge._added_maestro_outputs.add(("m", "MYTB", ""))
        with patch.object(
            bridge, "_delete_maestro_output_remote",
            side_effect=RuntimeError("transport"),
        ) as m_delete:
            apply_maestro_setup(bridge, {
                "outputs": [
                    {"name": "m", "expr": "rms(VT(/V))"},
                ],
            })
        assert m_delete.called
        assert writer_mocks["add_output"].called

    def test_self_dup_in_outputs_triggers_schema_violation(self):
        # Two outputs with the same name on the same test in the SAME
        # LLM payload — the contract validator must trip, so the
        # agent's repair loop fires before the dispatcher reaches the
        # bridge.
        err = validate_maestro_setup_block({
            "outputs": [
                {"name": "dup", "expr": "rms(VT(/A))"},
                {"name": "dup", "expr": "rms(VT(/B))"},
            ],
        })
        assert err is not None
        assert "duplicate" in err
        assert "dup" in err

    def test_self_dup_in_tests_also_triggers(self):
        # Same protection extended to tests (creating two with the
        # same name is unambiguously the LLM's fault).
        err = validate_maestro_setup_block({
            "tests": [
                {"name": "TB_A", "lib": "L", "cell": "C"},
                {"name": "TB_A", "lib": "L", "cell": "C"},
            ],
        })
        assert err is not None
        assert "duplicate" in err

    def test_cross_iter_outputs_accumulate(self, bridge, writer_mocks):
        # iter 0 adds "a"; iter 1 adds "b". The set should grow,
        # neither add should trigger the delete branch.
        with patch.object(
            bridge, "_delete_maestro_output_remote",
        ) as m_delete:
            apply_maestro_setup(bridge, {
                "outputs": [{"name": "a", "expr": "rms(VT(/A))"}],
            })
            apply_maestro_setup(bridge, {
                "outputs": [{"name": "b", "expr": "rms(VT(/B))"}],
            })
        assert not m_delete.called
        assert bridge._added_maestro_outputs >= {
            ("a", "MYTB", ""), ("b", "MYTB", ""),
        }

    # ---------------- R3 regressions ---------------- #

    def test_default_test_path_still_dedups(self, bridge, writer_mocks):
        """R3 P1 regression — codex PoC that R2 missed.

        Option I sync recorded ``"m"`` (default-test path). The LLM
        then issues a v2 outputs entry with the same name but NO
        ``test`` field. R2 coerced ``entry.get("test") or ""`` to ``""``,
        ``_resolve_maestro_test("")`` raised, the fail-soft except
        swallowed it, and ``add_maestro_output`` produced a duplicate
        row. R3 forwards ``test=None`` so the bridge resolves to the
        scoped tb_cell. The expected writer call sequence is
        ``[delete(m, MYTB), add(m, MYTB)]`` with NO double-add.
        """
        # Pre-seed the default-test cache entry (the resolved tb_cell
        # is "MYTB" from the fixture).
        bridge._added_maestro_outputs.add(("m", "MYTB", ""))
        with patch.object(
            bridge, "_delete_maestro_output_remote",
        ) as m_delete:
            apply_maestro_setup(bridge, {
                "outputs": [
                    # No ``test`` field — the LLM-common shape.
                    {"name": "m", "expr": "average(clip(VT(/V) 0 1e-9))"},
                ],
            })
        # The remote delete fired exactly once, on the right (name,
        # test) pair.
        assert m_delete.call_count == 1
        assert m_delete.call_args.args[0] == "m"
        # ``test`` was forwarded as None (not as ``""``) so the bridge
        # default-resolves to MYTB.
        assert m_delete.call_args.kwargs.get("test") is None
        # The single subsequent add carries the v2 expr.
        assert writer_mocks["add_output"].call_count == 1
        assert writer_mocks["add_output"].call_args.kwargs["expr"] == (
            "average(clip(VT(/V) 0 1e-9))"
        )

    def test_outputs_dedup_disambiguates_by_test(self, bridge, writer_mocks):
        """R3 P2 — two tests, same output name. The cache MUST NOT
        treat them as a duplicate, so neither triggers a delete and
        both add_output calls reach the writer."""
        with patch.object(
            bridge, "_delete_maestro_output_remote",
        ) as m_delete:
            apply_maestro_setup(bridge, {
                "tests": [
                    {"name": "T1", "lib": "L", "cell": "C"},
                    {"name": "T2", "lib": "L", "cell": "C"},
                ],
                "outputs": [
                    {"name": "VOUT_rms", "test": "T1",
                     "expr": "rms(VT(/A))"},
                    {"name": "VOUT_rms", "test": "T2",
                     "expr": "rms(VT(/B))"},
                ],
            })
        assert not m_delete.called
        assert writer_mocks["add_output"].call_count == 2
        # Cache holds two distinct tuples.
        assert ("VOUT_rms", "T1", "") in bridge._added_maestro_outputs
        assert ("VOUT_rms", "T2", "") in bridge._added_maestro_outputs

    def test_schema_allows_same_name_across_tests(self):
        """R3 P2 — schema-level dedup is keyed on (name, test), not
        bare name. Two outputs sharing a name but on different tests
        are NOT a contract violation."""
        err = validate_maestro_setup_block({
            "outputs": [
                {"name": "VOUT_rms", "test": "T1",
                 "expr": "rms(VT(/A))"},
                {"name": "VOUT_rms", "test": "T2",
                 "expr": "rms(VT(/B))"},
            ],
        })
        assert err is None

    def test_schema_blocks_same_name_same_test(self):
        """R3 P2 — sanity: same name on same explicit test still
        trips the contract validator."""
        err = validate_maestro_setup_block({
            "outputs": [
                {"name": "VOUT_rms", "test": "T1",
                 "expr": "rms(VT(/A))"},
                {"name": "VOUT_rms", "test": "T1",
                 "expr": "rms(VT(/B))"},
            ],
        })
        assert err is not None
        assert "duplicate" in err
        # Now both entries match — the message names the tuple, not
        # the bare name.
        assert "T1" in err

    def test_schema_blocks_same_name_both_default_test(self):
        """R3 P2 — two entries both omit ``test`` (default-test path)
        and share a name. Cache and schema treat ``None == None`` as
        the same test, so this IS a violation."""
        err = validate_maestro_setup_block({
            "outputs": [
                {"name": "VOUT_rms", "expr": "rms(VT(/A))"},
                {"name": "VOUT_rms", "expr": "rms(VT(/B))"},
            ],
        })
        assert err is not None
        assert "duplicate" in err


# --------------------------------------------------------------------- #
#  R2 P2-2: nested type validation
# --------------------------------------------------------------------- #


class TestNestedTypeValidation:
    """Per-entry nested fields (variables / pass / options) shape-check
    at the contract layer so the LLM gets a repair-prompt rather than
    an inscrutable bridge-side TypeError."""

    @pytest.mark.parametrize("variables", [
        {"temp": {"nested": "dict"}},      # nested dict
        {"temp": [1, 2, 3]},                # list value
        {"temp": None},                     # None
        {1: "x"},                           # non-string key
    ])
    def test_corner_variables_must_be_scalar_dict(self, variables):
        err = validate_maestro_setup_block({
            "corners": [{"name": "tt", "variables": variables}],
        })
        assert err is not None
        assert "corners[0].variables" in err

    def test_corner_variables_scalar_passes(self):
        # int/float/str scalar — accepted. R3 P2 (2026-05-15) removed
        # bool from the accepted set; see ``test_corner_variables_bool_rejected``.
        err = validate_maestro_setup_block({
            "corners": [{"name": "tt", "variables": {
                "i": 25, "f": 1.32, "s": "85C",
            }}],
        })
        assert err is None

    @pytest.mark.parametrize("variables", [
        {"R0": True},
        {"skipdc": False},
        {"mixed": True, "other": 1.0},
    ])
    def test_corner_variables_bool_rejected(self, variables):
        """R3 P2 — bool is rejected by the contract validator (was
        previously accepted, then failed silently inside SafeBridge's
        ``_format_param_value``). SKILL has no native bool atom, so
        the LLM must encode as 0/1 or the literal strings 't'/'nil'.
        Rejecting at the contract layer triggers the repair loop.
        """
        err = validate_maestro_setup_block({
            "corners": [{"name": "tt", "variables": variables}],
        })
        assert err is not None
        assert "bool" in err
        assert "corners[0].variables" in err

    def test_analyses_options_bool_rejected(self):
        """R3 P2 — same bool reject extends to analyses.options."""
        err = validate_maestro_setup_block({
            "analyses": [{
                "test": "T", "analysis": "tran",
                "options": {"enable_extra": True},
            }],
        })
        assert err is not None
        assert "bool" in err
        assert "analyses[0].options" in err

    @pytest.mark.parametrize("bad_pass,problem_substr", [
        ([1.0],            "exactly 2"),  # too short
        ([1.0, 2.0, 3.0],  "exactly 2"),  # too long
        ([True, 2.0],      "bool"),       # bool rejected
        (["lo", 2.0],      "str"),        # str element
        ({"lo": 1, "hi": 2}, "list"),     # wrong outer type
    ])
    def test_outputs_pass_bounds_shape(self, bad_pass, problem_substr):
        err = validate_maestro_setup_block({
            "outputs": [
                {"name": "v", "expr": "rms(VT(/V))", "pass": bad_pass},
            ],
        })
        assert err is not None
        assert "outputs[0].pass" in err
        assert problem_substr in err

    @pytest.mark.parametrize("good_pass", [
        [None, 1.0],
        [-1.0, None],
        [0.0, 1.0],
        [None, None],   # vacuous but well-formed; apply layer no-ops
        [1, 2],         # plain ints
    ])
    def test_outputs_pass_bounds_well_formed(self, good_pass):
        err = validate_maestro_setup_block({
            "outputs": [
                {"name": "v", "expr": "rms(VT(/V))", "pass": good_pass},
            ],
        })
        assert err is None

    def test_analyses_options_must_be_scalar_dict(self):
        err = validate_maestro_setup_block({
            "analyses": [{
                "test": "T", "analysis": "tran",
                "options": {"stop": {"nested": "no"}},
            }],
        })
        assert err is not None
        assert "analyses[0].options" in err
