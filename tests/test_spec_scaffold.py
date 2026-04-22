"""Tests for src.spec_scaffold.render_spec_scaffold."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.spec_scaffold import (  # noqa: E402
    _classify_pins,
    render_spec_scaffold,
)


def _base_scaffold() -> dict:
    return {
        "lib": "pll",
        "cell": "LC_VCO",
        "tb_cell": "LC_VCO_tb",
        "dut": {
            "lib": "GENERIC_PDK",
            "cell": "LC_VCO",
            "pins": [
                {"name": "Vout_p", "direction": "output"},
                {"name": "Vout_n", "direction": "output"},
                {"name": "vdd",    "direction": "inputOutput"},
                {"name": "vss",    "direction": "inputOutput"},
                {"name": "Vtune",  "direction": "input"},
            ],
        },
        "tb": {
            "lib": "GENERIC_PDK",
            "cell": "LC_VCO_tb",
            "pins": [
                {"name": "vdd", "direction": "inputOutput"},
                {"name": "gnd", "direction": "inputOutput"},
            ],
        },
        "design_vars": [
            {"name": "Ibias", "default": "500u"},
            {"name": "nfin_neg", "default": "16"},
        ],
        "analyses": [
            {"name": "tran", "kwargs": [("stop", "200n"), ("maxiters", "5")]},
        ],
    }


class TestClassifyPins:
    def test_outputs_become_probes(self):
        pins = [{"name": "Vout_p", "direction": "output"}]
        probes, supplies, others = _classify_pins(pins)
        assert probes == pins
        assert supplies == []
        assert others == []

    def test_vdd_becomes_supply(self):
        pins = [{"name": "vdd", "direction": "inputOutput"}]
        probes, supplies, others = _classify_pins(pins)
        assert supplies == pins
        assert probes == []

    def test_supply_wins_over_direction(self):
        """A pin named `vdd` stays classified as supply even when direction
        is `output` — protects against classifying power rails as probes."""
        pins = [{"name": "VDD_core", "direction": "output"}]
        _, supplies, _ = _classify_pins(pins)
        assert len(supplies) == 1

    def test_input_goes_to_others(self):
        pins = [{"name": "Vtune", "direction": "input"}]
        _, _, others = _classify_pins(pins)
        assert others == pins


class TestRenderScaffold:
    def test_returns_non_empty_markdown(self):
        out = render_spec_scaffold(_base_scaffold())
        assert isinstance(out, str)
        assert len(out) > 500

    def test_five_sections_present(self):
        out = render_spec_scaffold(_base_scaffold())
        for heading in (
            "## 1. Design under test",
            "## 2. Machine-readable eval block",
            "## 3. Design variables the LLM may adjust",
            "## 4. Startup convergence aids",
            "## 5. Honest caveats",
        ):
            assert heading in out, f"missing heading: {heading}"

    def test_cell_names_in_header(self):
        out = render_spec_scaffold(_base_scaffold())
        assert "pll / LC_VCO" in out
        assert "pll / LC_VCO_tb" in out
        assert "LC_VCO Optimization Spec" in out

    def test_probe_pins_listed_in_section_1(self):
        out = render_spec_scaffold(_base_scaffold())
        assert "`Vout_p`" in out
        assert "`Vout_n`" in out

    def test_supply_pins_listed(self):
        out = render_spec_scaffold(_base_scaffold())
        assert "`vdd`" in out
        assert "`vss`" in out

    def test_probe_paths_appear_in_eval_block(self):
        out = render_spec_scaffold(_base_scaffold())
        # First two output pins become the Vdiff hint inside the YAML
        assert '"/Vout_p"' in out
        assert '"/Vout_n"' in out

    def test_design_vars_table_populated(self):
        out = render_spec_scaffold(_base_scaffold())
        assert "| `Ibias` |" in out
        assert "| `nfin_neg` |" in out
        assert "default `500u`" in out

    def test_analyses_block_populated(self):
        out = render_spec_scaffold(_base_scaffold())
        assert "`tran`" in out
        assert "`stop=200n`" in out

    def test_todo_markers_present(self):
        out = render_spec_scaffold(_base_scaffold())
        # Scaffold must be clearly unfinished — the 5-section skeleton
        # leaves the numbers the user must author as <TODO> stubs.
        assert "<TODO" in out

    def test_no_lc_vco_hardcoded_fallback(self):
        """When no circuit info is given, output should still be generic —
        no leftover LC_VCO / Vdiff literals from the template itself."""
        empty = {
            "lib": "mylib",
            "cell": "MyCell",
            "tb_cell": "MyCell_tb",
            "dut": {"lib": "GENERIC_PDK", "cell": "MyCell", "pins": []},
            "tb":  {"lib": "GENERIC_PDK", "cell": "MyCell_tb", "pins": []},
            "design_vars": [],
            "analyses": [],
        }
        out = render_spec_scaffold(empty)
        # Cell names must be the ones we supplied, not hardcoded samples.
        assert "MyCell" in out
        assert "LC_VCO" not in out
        assert "Ibias" not in out

    def test_empty_design_vars_emits_placeholder_row(self):
        data = _base_scaffold()
        data["design_vars"] = []
        out = render_spec_scaffold(data)
        assert "<TODO_var_name>" in out

    def test_empty_analyses_block_emits_note(self):
        data = _base_scaffold()
        data["analyses"] = []
        out = render_spec_scaffold(data)
        assert "No analyses discovered" in out

    def test_platform_contract_preamble_present(self):
        out = render_spec_scaffold(_base_scaffold())
        assert "Platform contract" in out
        assert "docs/llm_protocol.md" in out

    def test_sections_separated_by_hrules(self):
        out = render_spec_scaffold(_base_scaffold())
        # F-A's style: sections separated by "---" horizontal rules.
        # Header + five sections → five separators.
        assert out.count("\n---\n") == 5


class TestRenderRobustness:
    def test_missing_direction_handled(self):
        data = _base_scaffold()
        data["dut"]["pins"] = [{"name": "foo"}]
        out = render_spec_scaffold(data)
        assert "foo" in out

    def test_single_output_pin_still_renders(self):
        data = _base_scaffold()
        data["dut"]["pins"] = [{"name": "Vout", "direction": "output"}]
        out = render_spec_scaffold(data)
        # Half-filled probe hint: [/Vout] — still valid Markdown
        assert '"/Vout"' in out

    def test_pin_with_unknown_direction(self):
        data = _base_scaffold()
        data["dut"]["pins"] = [{"name": "NetA", "direction": ""}]
        out = render_spec_scaffold(data)
        assert "NetA" in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
