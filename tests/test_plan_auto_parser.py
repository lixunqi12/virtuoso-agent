"""Tests for src.plan_auto._parse_startup_block / parse_startup_from_spec.

X2 (2026-04-22) regression: F-A trimmed the spec's `startup:` block so
`perturb_nodes` uses flow-style `- {name: ..., offset_mV: ...}` entries,
but the mini-yaml parser only recognized block-style `- name: ...`
entries. Result: empty perturb list → Plan Auto disabled → tran never
warm-starts → oscillator sits at the symmetric equilibrium and
`safeOceanDumpAll` sees no waveform.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.plan_auto import (  # noqa: E402
    PlanAuto,
    PerturbNode,
    StartupConfig,
    _parse_startup_block,
    parse_startup_from_spec,
)


# ---------------------------------------------------------------- #
#  Flow-style perturb_nodes — the X2 regression
# ---------------------------------------------------------------- #

class TestFlowStylePerturbNodes:
    """`- {name: X, offset_mV: Y}` must parse identically to block-style."""

    def test_two_flow_style_entries(self):
        block = (
            "startup:\n"
            "  warm_start: auto\n"
            "  perturb_nodes:\n"
            "    - {name: Vout_n, offset_mV: +5}\n"
            "    - {name: Vout_p, offset_mV: -5}\n"
            "  v_cm_hint_V: 0.75\n"
        )
        cfg = _parse_startup_block(block)
        assert cfg is not None
        assert cfg.warm_start == "auto"
        assert len(cfg.perturb_nodes) == 2
        assert cfg.perturb_nodes[0] == PerturbNode("Vout_n", 5.0)
        assert cfg.perturb_nodes[1] == PerturbNode("Vout_p", -5.0)
        assert cfg.v_cm_hint_V == pytest.approx(0.75)

    def test_flow_style_activates_plan_auto(self):
        """The end-to-end symptom: Plan Auto should be `enabled=True`
        when flow-style is used, not silently disabled."""
        block = (
            "startup:\n"
            "  warm_start: auto\n"
            "  perturb_nodes:\n"
            "    - {name: Vout_n, offset_mV: +5}\n"
            "    - {name: Vout_p, offset_mV: -5}\n"
        )
        cfg = _parse_startup_block(block)
        assert cfg.enabled is True

    def test_flow_style_whitespace_tolerated(self):
        """Extra whitespace inside the inline map must not break parse."""
        block = (
            "startup:\n"
            "  warm_start: auto\n"
            "  perturb_nodes:\n"
            "    - { name:  Vout_n ,  offset_mV: +5 }\n"
            "    - {name:Vout_p,offset_mV:-5}\n"
        )
        cfg = _parse_startup_block(block)
        assert len(cfg.perturb_nodes) == 2
        assert cfg.perturb_nodes[0] == PerturbNode("Vout_n", 5.0)
        assert cfg.perturb_nodes[1] == PerturbNode("Vout_p", -5.0)

    def test_flow_style_key_order_insensitive(self):
        """offset_mV may appear before name inside the inline map."""
        block = (
            "startup:\n"
            "  warm_start: auto\n"
            "  perturb_nodes:\n"
            "    - {offset_mV: +3, name: NodeA}\n"
        )
        cfg = _parse_startup_block(block)
        assert cfg.perturb_nodes == [PerturbNode("NodeA", 3.0)]

    def test_flow_style_plan_auto_describe(self):
        """PlanAuto orchestrator exposes the parsed perturb nodes."""
        spec = (
            "```yaml\n"
            "startup:\n"
            "  warm_start: auto\n"
            "  perturb_nodes:\n"
            "    - {name: Vout_n, offset_mV: +5}\n"
            "    - {name: Vout_p, offset_mV: -5}\n"
            "```\n"
        )
        cfg = parse_startup_from_spec(spec)
        pa = PlanAuto(cfg, scs_path="/tmp/input.scs", enabled_flag=True)
        assert pa.active is True
        desc = pa.describe()
        assert "Vout_n" in desc
        assert "Vout_p" in desc


# ---------------------------------------------------------------- #
#  Block-style perturb_nodes — regression guard
# ---------------------------------------------------------------- #

class TestBlockStyleStillWorks:
    """The flow-style fix must not break the legacy block-style path."""

    def test_block_style_two_entries(self):
        block = (
            "startup:\n"
            "  warm_start: auto\n"
            "  perturb_nodes:\n"
            "    - name: Vout_n\n"
            "      offset_mV: +5\n"
            "    - name: Vout_p\n"
            "      offset_mV: -5\n"
            "  v_cm_hint_V: 0.4\n"
        )
        cfg = _parse_startup_block(block)
        assert cfg is not None
        assert cfg.warm_start == "auto"
        assert len(cfg.perturb_nodes) == 2
        assert cfg.perturb_nodes[0] == PerturbNode("Vout_n", 5.0)
        assert cfg.perturb_nodes[1] == PerturbNode("Vout_p", -5.0)

    def test_block_style_single_entry(self):
        block = (
            "startup:\n"
            "  warm_start: auto\n"
            "  perturb_nodes:\n"
            "    - name: Vout_n\n"
            "      offset_mV: +10\n"
        )
        cfg = _parse_startup_block(block)
        assert cfg is not None
        assert cfg.perturb_nodes == [PerturbNode("Vout_n", 10.0)]


# ---------------------------------------------------------------- #
#  Mixed flow + block in the same list
# ---------------------------------------------------------------- #

class TestMixedFlowAndBlock:
    """A list where one entry is flow-style and another block-style
    must produce two distinct PerturbNodes — the flow entry must not
    accidentally absorb the block entry's keys."""

    def test_flow_then_block(self):
        block = (
            "startup:\n"
            "  warm_start: auto\n"
            "  perturb_nodes:\n"
            "    - {name: NodeA, offset_mV: +5}\n"
            "    - name: NodeB\n"
            "      offset_mV: -3\n"
        )
        cfg = _parse_startup_block(block)
        assert len(cfg.perturb_nodes) == 2
        assert cfg.perturb_nodes[0] == PerturbNode("NodeA", 5.0)
        assert cfg.perturb_nodes[1] == PerturbNode("NodeB", -3.0)

    def test_block_then_flow(self):
        block = (
            "startup:\n"
            "  warm_start: auto\n"
            "  perturb_nodes:\n"
            "    - name: NodeA\n"
            "      offset_mV: +5\n"
            "    - {name: NodeB, offset_mV: -3}\n"
        )
        cfg = _parse_startup_block(block)
        assert len(cfg.perturb_nodes) == 2
        assert cfg.perturb_nodes[0] == PerturbNode("NodeA", 5.0)
        assert cfg.perturb_nodes[1] == PerturbNode("NodeB", -3.0)


# ---------------------------------------------------------------- #
#  Smoke test against the real F-A spec
# ---------------------------------------------------------------- #

class TestRealFASpec:
    def test_parses_lc_vco_spec_md(self):
        spec_path = PROJECT_ROOT / "config" / "LC_VCO_spec.md"
        if not spec_path.exists():
            pytest.skip("config/LC_VCO_spec.md not present")
        spec_text = spec_path.read_text(encoding="utf-8")
        cfg = parse_startup_from_spec(spec_text)
        # F-A compressed to flow-style; both entries must land.
        names = [p.name for p in cfg.perturb_nodes]
        assert names == ["Vout_n", "Vout_p"], (
            "F-A spec startup.perturb_nodes must parse to 2 entries; got: "
            f"{cfg.perturb_nodes!r}"
        )
        assert cfg.warm_start == "auto"
        assert cfg.v_cm_hint_V == pytest.approx(0.75)
        assert cfg.enabled is True


# ---------------------------------------------------------------- #
#  Edge cases — malformed input should degrade, not crash
# ---------------------------------------------------------------- #

class TestMalformedInput:
    def test_flow_without_name_is_dropped(self):
        block = (
            "startup:\n"
            "  warm_start: auto\n"
            "  perturb_nodes:\n"
            "    - {offset_mV: +5}\n"
            "    - {name: NodeA, offset_mV: +3}\n"
        )
        cfg = _parse_startup_block(block)
        # Entry without name is dropped; the good one survives.
        assert cfg.perturb_nodes == [PerturbNode("NodeA", 3.0)]

    def test_flow_with_empty_body(self):
        block = (
            "startup:\n"
            "  warm_start: auto\n"
            "  perturb_nodes:\n"
            "    - {}\n"
            "    - {name: NodeA, offset_mV: +3}\n"
        )
        cfg = _parse_startup_block(block)
        assert cfg.perturb_nodes == [PerturbNode("NodeA", 3.0)]

    def test_flow_with_bad_offset(self):
        block = (
            "startup:\n"
            "  warm_start: auto\n"
            "  perturb_nodes:\n"
            "    - {name: NodeA, offset_mV: not_a_number}\n"
        )
        cfg = _parse_startup_block(block)
        # _finalize_perturb defaults bad offsets to 0.0 (legacy behavior).
        assert cfg.perturb_nodes == [PerturbNode("NodeA", 0.0)]


# ---------------------------------------------------------------- #
#  Absent startup block stays a no-op
# ---------------------------------------------------------------- #

class TestAbsentStartupBlock:
    def test_no_startup_block_returns_default(self):
        spec = (
            "```yaml\n"
            "signals:\n"
            "  - name: Vdiff\n"
            "    kind: Vdiff\n"
            "```\n"
        )
        cfg = parse_startup_from_spec(spec)
        assert cfg.warm_start == "none"
        assert cfg.perturb_nodes == []
        assert cfg.enabled is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
