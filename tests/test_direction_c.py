"""Direction C (OCEAN + Maestro writeback) unit tests.

Exercises the SafeBridge entrypoints used by CircuitAgent for the
Direction C flow:
- run_ocean_sim
- write_and_save_maestro

Stage 1 rev 1 (2026-04-18): MaestroWriter / OceanRunner thin wrappers
were removed. Their one piece of added logic (treat SKILL-reported
saved=False as RuntimeError instead of silent success) now lives in
SafeBridge.write_and_save_maestro itself. Callers (CircuitAgent,
run_agent.py) invoke bridge methods directly.

All SKILL I/O is mocked through a fake VirtuosoClient returning an
object with a ``.output`` attribute (matching the real client's shape
— see _execute_skill_json).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.safe_bridge import SafeBridge, _OCEAN_ALLOWED_ANALYSES  # noqa: E402


# ---------------------------------------------------------------- #
#  Fixtures
# ---------------------------------------------------------------- #

@pytest.fixture
def pdk_map_file(tmp_path):
    content = """\
generic_cell_name: "GENERIC_DEVICE"

valid_aliases:
  - NMOS
  - PMOS

model_info_keys:
  - toxe
  - u0

allowed_params:
  - w
  - l
  - nf
  - m
  - multi
  - wf
  - r
  - c
"""
    path = tmp_path / "pdk_map.yaml"
    path.write_text(content, encoding="utf-8")
    return str(path)


class _FakeResult:
    """Shape-compatible stand-in for VirtuosoClient.execute_skill result."""

    def __init__(self, payload: dict):
        self.ok = True
        self.errors: list[str] = []
        self.output = json.dumps(payload)


@pytest.fixture
def mock_client():
    return MagicMock()


@pytest.fixture
def bridge(mock_client, pdk_map_file, tmp_path):
    b = SafeBridge(
        mock_client,
        pdk_map_file,
        skill_dir=tmp_path / "no_skill",
    )
    # Pretend SKILL side loaded OK without touching disk.
    b._skill_loaded = True
    return b


# ---------------------------------------------------------------- #
#  run_ocean_sim
# ---------------------------------------------------------------- #

class TestRunOceanSim:
    def test_builds_expected_skill_call(self, bridge, mock_client):
        # Stage 1 rev 3 (2026-04-18): run_ocean_sim now makes TWO SKILL
        # calls per invocation — safeOceanRun then safeOceanMeasure.
        # Using side_effect to return distinct payloads per call.
        mock_client.execute_skill.side_effect = [
            _FakeResult({
                "ok": True,
                "resultsDir": "<results>",
                "varsApplied": 1,
                "analyses": ["tran"],
            }),
            _FakeResult({
                "ok": True,
                "metrics": {
                    "f_osc_GHz": 19.8,
                    "V_diff_pp_V": 0.62,
                    "V_cm_V": 0.41,
                    "duty_cycle_pct": 49.5,
                    "amp_hold_ratio": 0.97,
                    "t_startup_ns": 6.3,
                    "I_core_uA": 640,
                },
            }),
        ]
        out = bridge.run_ocean_sim(
            "pllLib", "LC_VCO", "LC_VCO_tb",
            design_vars={"r": "3k"},
            analyses=["tran"],
        )
        assert out["ok"] is True
        calls = mock_client.execute_skill.call_args_list
        assert len(calls) == 2
        first_expr = calls[0][0][0]
        second_expr = calls[1][0][0]
        assert first_expr.startswith(
            'safeOceanRun("pllLib" "LC_VCO" "LC_VCO_tb"'
        )
        assert 'list("r" "3k")' in first_expr
        assert 'list("tran")' in first_expr
        assert second_expr.startswith('safeOceanMeasure("/I0")')
        # Measurements are merged into the returned dict so the agent
        # does not have to re-fetch them.
        assert out["measurements"]["f_osc_GHz"] == 19.8
        assert out["measurements"]["amp_hold_ratio"] == 0.97

    def test_measure_failure_non_fatal(self, bridge, mock_client):
        """safeOceanRun succeeded but safeOceanMeasure returned ok:false.
        The run must NOT raise — measurements stays empty, measure_error
        surfaces. The agent's SAFEGUARD fallback path depends on this.
        """
        mock_client.execute_skill.side_effect = [
            _FakeResult({
                "ok": True, "resultsDir": "<results>",
                "varsApplied": 1, "analyses": ["tran"],
            }),
            _FakeResult({
                "ok": False,
                "error": "VT(/out_p)-VT(/out_n) unavailable",
            }),
        ]
        out = bridge.run_ocean_sim(
            "pllLib", "LC_VCO", "LC_VCO_tb",
            design_vars={"r": "3k"},
            analyses=["tran"],
        )
        assert out["ok"] is True
        assert out["measurements"] == {}
        assert "VT(" in out["measure_error"]

    def test_rejects_invalid_dut_path(self, bridge, mock_client):
        with pytest.raises(ValueError, match="dut_path"):
            bridge.run_ocean_sim(
                "pllLib", "LC_VCO", "LC_VCO_tb",
                design_vars={"r": "3k"},
                analyses=["tran"],
                dut_path="/I0; load(\"evil\")",
            )
        # Stage 1 rev 3 M2 (2026-04-18): dut_path validation now runs
        # BEFORE safeOceanRun, so a malformed dut_path must not trigger
        # any remote host round-trip at all. Prior expectation was
        # call_count == 1 (safeOceanRun ran, safeOceanMeasure was
        # rejected later); M2 tightens to 0 so we never pay for a
        # simulation whose measurement step is doomed upfront.
        assert mock_client.execute_skill.call_count == 0

    def test_rejects_non_allowlisted_analysis(self, bridge):
        with pytest.raises(ValueError):
            bridge.run_ocean_sim(
                "L", "C", "Ctb",
                design_vars={"r": "3k"},
                analyses=["sp"],  # not in _OCEAN_ALLOWED_ANALYSES
            )

    def test_rejects_non_whitelisted_design_var(self, bridge):
        with pytest.raises(ValueError):
            bridge.run_ocean_sim(
                "L", "C", "Ctb",
                design_vars={"vdd": "1.2"},  # not in allowed_params
                analyses=["tran"],
            )

    @pytest.mark.parametrize("name", [
        "VDD", "VSS", "GND", "VCC", "VEE",   # uppercase
        "Vdd", "gNd",                          # mixed case
    ])
    def test_rejects_supply_rail_case_insensitive(self, bridge, name):
        with pytest.raises(ValueError):
            bridge.run_ocean_sim(
                "L", "C", "Ctb",
                design_vars={name: "1.2"},
                analyses=["tran"],
            )

    @pytest.mark.parametrize("name", [
        "avdd", "dvdd", "dvss", "vddio",       # prefix/suffix
        "my_gnd_tap", "vref_gnd",               # embedded substring
    ])
    def test_rejects_supply_rail_substring_variants(self, bridge, name):
        with pytest.raises(ValueError):
            bridge.run_ocean_sim(
                "L", "C", "Ctb",
                design_vars={name: "1.2"},
                analyses=["tran"],
            )

    def test_scope_enforced(self, bridge):
        bridge.set_scope("libA", "cellA")
        with pytest.raises(ValueError):
            bridge.run_ocean_sim(
                "libB", "cellA", "cellA_tb",
                design_vars={"r": "3k"},
                analyses=["tran"],
            )

    def test_skill_not_loaded_raises(self, mock_client, pdk_map_file, tmp_path):
        b = SafeBridge(
            mock_client, pdk_map_file,
            skill_dir=tmp_path / "no_skill",
        )
        # _skill_loaded stays False by default
        with pytest.raises(RuntimeError, match="SKILL helpers"):
            b.run_ocean_sim(
                "L", "C", "Ctb", design_vars={"r": "3k"}, analyses=["tran"],
            )

    def test_scrub_applied_to_results_dir(self, bridge, mock_client):
        mock_client.execute_skill.side_effect = [
            _FakeResult({
                "ok": True,
                # Simulated path leak past SKILL-side scrub.
                "resultsDir": "/project/foo/bar/sim/results/psf",
                "varsApplied": 0,
                "analyses": ["tran"],
            }),
            _FakeResult({"ok": True, "metrics": {}}),
        ]
        out = bridge.run_ocean_sim(
            "L", "C", "Ctb", design_vars={"r": "3k"}, analyses=["tran"],
        )
        assert "/project/" not in out["resultsDir"]
        assert "<path>" in out["resultsDir"]

    def test_skill_side_error_raises(self, bridge, mock_client):
        # Note: when SKILL returns {"ok": False, "error": ...}, the "error"
        # key is intercepted in _execute_skill_json before reach this
        # wrapper; either surfacing of the error is acceptable as long as
        # the caller sees a RuntimeError.
        mock_client.execute_skill.return_value = _FakeResult({
            "ok": False,
            "error": "run() returned nil",
        })
        with pytest.raises(RuntimeError, match="run\\(\\) returned nil"):
            bridge.run_ocean_sim(
                "L", "C", "Ctb", design_vars={"r": "3k"}, analyses=["tran"],
            )

    def test_ok_false_without_error_key_raises(self, bridge, mock_client):
        mock_client.execute_skill.return_value = _FakeResult({
            "ok": False,
            "detail": "something went wrong",
        })
        with pytest.raises(RuntimeError, match="safeOceanRun failed"):
            bridge.run_ocean_sim(
                "L", "C", "Ctb", design_vars={"r": "3k"}, analyses=["tran"],
            )


# ---------------------------------------------------------------- #
#  write_and_save_maestro (atomic single entry point)
# ---------------------------------------------------------------- #

class TestWriteAndSaveMaestro:
    def test_requires_scope(self, bridge):
        with pytest.raises(RuntimeError, match="set_scope"):
            bridge.write_and_save_maestro({"r": "3k"})

    def test_empty_mapping_rejected(self, bridge):
        bridge.set_scope("pllLib", "LC_VCO", tb_cell="LC_VCO_tb")
        with pytest.raises(ValueError):
            bridge.write_and_save_maestro({})

    def test_non_whitelisted_var_rejected(self, bridge):
        bridge.set_scope("pllLib", "LC_VCO", tb_cell="LC_VCO_tb")
        with pytest.raises(ValueError):
            bridge.write_and_save_maestro({"vdd": "1.2"})

    @pytest.mark.parametrize("name", [
        "VDD", "VSS", "GND", "VCC", "VEE",
        "Vdd", "gNd",
    ])
    def test_maestro_rejects_supply_rail_case_insensitive(self, bridge, name):
        bridge.set_scope("pllLib", "LC_VCO", tb_cell="LC_VCO_tb")
        with pytest.raises(ValueError):
            bridge.write_and_save_maestro({name: "1.2"})

    @pytest.mark.parametrize("name", [
        "avdd", "dvdd", "dvss", "vddio",
        "my_gnd_tap", "vref_gnd",
    ])
    def test_maestro_rejects_supply_rail_substring_variants(self, bridge, name):
        bridge.set_scope("pllLib", "LC_VCO", tb_cell="LC_VCO_tb")
        with pytest.raises(ValueError):
            bridge.write_and_save_maestro({name: "1.2"})

    def test_builds_expected_skill_call(self, bridge, mock_client):
        bridge.set_scope("pllLib", "LC_VCO", tb_cell="LC_VCO_tb")
        mock_client.execute_skill.return_value = _FakeResult({
            "ok": True, "varsWritten": 1, "saved": True,
        })
        bridge.write_and_save_maestro({"r": "3k"})
        expr = mock_client.execute_skill.call_args[0][0]
        assert expr.startswith('safeMaeWriteAndSave("pllLib" "LC_VCO_tb" ')
        assert 'list("r" "3k")' in expr

    def test_single_round_trip(self, bridge, mock_client):
        """Atomic design goal: one SKILL call for write+save, not two."""
        bridge.set_scope("pllLib", "LC_VCO", tb_cell="LC_VCO_tb")
        mock_client.execute_skill.return_value = _FakeResult({
            "ok": True, "varsWritten": 2, "saved": True,
        })
        bridge.write_and_save_maestro({"r": "3k", "c": "5f"})
        assert mock_client.execute_skill.call_count == 1

    def test_session_not_found_raises(self, bridge, mock_client):
        """SKILL side reports no matching Maestro session for scope."""
        bridge.set_scope("pllLib", "LC_VCO", tb_cell="LC_VCO_tb")
        mock_client.execute_skill.return_value = _FakeResult({
            "ok": False,
            "error": "No open Maestro session for scope — open Maestro ...",
        })
        with pytest.raises(RuntimeError, match="No open Maestro session"):
            bridge.write_and_save_maestro({"r": "3k"})

    def test_dotted_scope_passed_literally(self, bridge, mock_client):
        """Scope names can contain "." (regex metachar). They must be
        passed through to SKILL verbatim — the SKILL side must then do
        literal substring matching, NOT regex matching. Previously
        rexMatchp would let `pll.v1` spuriously match `pllXv1` titles.
        This test is the PC-side half of that guard; the SKILL half
        lives in safe_maestro.il::safeMaestro_substrp.
        """
        bridge.set_scope("pll.v1", "LC_VCO", tb_cell="LC_VCO.tb")
        mock_client.execute_skill.return_value = _FakeResult({
            "ok": True, "varsWritten": 1, "saved": True,
        })
        bridge.write_and_save_maestro({"r": "3k"})
        expr = mock_client.execute_skill.call_args[0][0]
        assert expr.startswith('safeMaeWriteAndSave("pll.v1" "LC_VCO.tb" ')

    def test_raises_when_saved_false(self, bridge, mock_client):
        """SKILL reports ok:true but saved:false — previously silent.
        Stage 1 rev 1 (2026-04-18): this check lived in the removed
        MaestroWriter thin wrapper; it now lives in the bridge method
        itself so callers (CircuitAgent, run_agent.py) are protected
        directly without an intermediate class.
        """
        bridge.set_scope("pllLib", "LC_VCO", tb_cell="LC_VCO_tb")
        mock_client.execute_skill.return_value = _FakeResult({
            "ok": True, "varsWritten": 1, "saved": False,
        })
        with pytest.raises(RuntimeError, match="saved=False"):
            bridge.write_and_save_maestro({"r": "3k"})


# ---------------------------------------------------------------- #
#  Constant sanity
# ---------------------------------------------------------------- #

class TestManualSyncLog:
    """R13: verify the copy-pasteable table is logged after writeback."""

    def test_manual_sync_table_logged_on_success(
        self, bridge, mock_client, caplog
    ):
        bridge.set_scope("pllLib", "LC_VCO", tb_cell="LC_VCO_tb")
        mock_client.execute_skill.return_value = _FakeResult({
            "ok": True, "varsWritten": 2, "saved": True,
            "session": "fnxSession3",
        })
        with caplog.at_level("INFO", logger="src.safe_bridge"):
            bridge.write_and_save_maestro({"C": "1.5f", "Ibias": "501u"})
        log_text = caplog.text
        assert "MANUAL SYNC REQUIRED" in log_text
        assert "paste into Maestro Design Variables" in log_text
        assert "C" in log_text
        assert "1.5f" in log_text
        assert "Ibias" in log_text
        assert "501u" in log_text

    def test_final_converged_banner_format(self, caplog):
        """_log_manual_sync_table with FINAL CONVERGED VALUES banner."""
        with caplog.at_level("INFO", logger="src.safe_bridge"):
            SafeBridge._log_manual_sync_table(
                {"W": "2u", "L": "100n"},
                banner="FINAL CONVERGED VALUES",
                scope_lib="pll",
                scope_tb_cell="LC_VCO_tb",
            )
        log_text = caplog.text
        assert "FINAL CONVERGED VALUES" in log_text
        assert "W" in log_text
        assert "2u" in log_text
        assert "pll / LC_VCO_tb / maestro" in log_text


def test_ocean_allowed_analyses_matches_skill_side():
    """Keep PC-side allow-list in sync with safe_ocean.il."""
    assert _OCEAN_ALLOWED_ANALYSES == frozenset(
        {"tran", "ac", "dc", "noise", "xf", "stb"}
    )
