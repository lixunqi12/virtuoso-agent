"""Tests for PDK-safe topology intent inference and OP diagnostics."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.topology_intent import (  # noqa: E402
    format_stage_diagnostics,
    infer_topology_intent,
    render_topology_intent_markdown,
)
from src.safe_bridge import assert_llm_feedback_safe  # noqa: E402


def _opamp_instances() -> list[dict]:
    return [
        {
            "instName": "M9",
            "cell": "NMOS_SVT",
            "nets": {"G": "cmfb_out", "D": "Vout_p", "S": "gnd!", "B": "gnd!"},
            "params": {"fingers": "nfin_n_2"},
        },
        {
            "instName": "M7",
            "cell": "NMOS_SVT",
            "nets": {"G": "cmfb_out", "D": "Vout_n", "S": "gnd!", "B": "gnd!"},
            "params": {"fingers": "nfin_n_2"},
        },
        {
            "instName": "M5",
            "cell": "NMOS_SVT",
            "nets": {"G": "net9", "D": "net2", "S": "gnd!", "B": "gnd!"},
            "params": {"fingers": "nfin_current"},
        },
        {
            "instName": "M4",
            "cell": "NMOS_SVT",
            "nets": {"G": "net9", "D": "net9", "S": "gnd!", "B": "gnd!"},
            "params": {"fingers": "nfin_bias"},
        },
        {
            "instName": "M1",
            "cell": "NMOS_SVT",
            "nets": {"G": "Vin_n", "D": "net6", "S": "net2", "B": "net2"},
            "params": {"fingers": "nfin_n"},
        },
        {
            "instName": "M0",
            "cell": "NMOS_SVT",
            "nets": {"G": "Vin_p", "D": "net3", "S": "net2", "B": "net2"},
            "params": {"fingers": "nfin_n"},
        },
        {
            "instName": "M10",
            "cell": "PMOS_SVT",
            "nets": {"D": "p_bias", "G": "p_bias", "B": "vdd!", "S": "vdd!"},
            "params": {"fingers": "nfin_p_bias"},
        },
        {
            "instName": "M8",
            "cell": "PMOS_SVT",
            "nets": {"D": "Vout_p", "G": "net3", "B": "vdd!", "S": "vdd!"},
            "params": {"fingers": "nfin_p_2"},
        },
        {
            "instName": "M6",
            "cell": "PMOS_SVT",
            "nets": {"D": "Vout_n", "G": "net6", "B": "vdd!", "S": "vdd!"},
            "params": {"fingers": "nfin_p_2"},
        },
        {
            "instName": "M3",
            "cell": "PMOS_SVT",
            "nets": {"D": "net3", "G": "p_bias", "B": "vdd!", "S": "vdd!"},
            "params": {"fingers": "nfin_p"},
        },
        {
            "instName": "M2",
            "cell": "PMOS_SVT",
            "nets": {"D": "net6", "G": "p_bias", "B": "vdd!", "S": "vdd!"},
            "params": {"fingers": "nfin_p"},
        },
        {
            "instName": "I2",
            "cell": "ISRC",
            "nets": {"PLUS": "p_bias", "MINUS": "gnd!"},
            "params": {"idc": "Ibias_pmos"},
        },
        {
            "instName": "I0",
            "cell": "ISRC",
            "nets": {"PLUS": "vdd!", "MINUS": "net9"},
            "params": {"idc": "Ibias"},
        },
        {
            "instName": "C1",
            "cell": "IDEAL_CAP",
            "nets": {"MINUS": "net3", "PLUS": "net4"},
            "params": {"c": "miller_c"},
        },
        {
            "instName": "R4",
            "cell": "RESISTOR",
            "nets": {"MINUS": "Vout_p", "PLUS": "net4"},
            "params": {"r": "miller_R"},
        },
        {
            "instName": "I6",
            "cell": "GENERIC_DEVICE",
            "nets": {
                "cmfb_out": "cmfb_out",
                "cmfb_vin_n": "net5",
                "cmfb_vin_p": "net1",
            },
            "params": {},
        },
    ]


def test_infers_two_stage_opamp_intent() -> None:
    intent = infer_topology_intent(
        _opamp_instances(),
        design_vars=[
            "Ibias", "Ibias_pmos", "nfin_n", "nfin_p", "nfin_p_2",
            "nfin_n_2", "nfin_current", "nfin_bias", "nfin_p_bias",
            "miller_c", "miller_R",
        ],
    )

    assert intent["circuit_class"] == "fully_differential_two_stage_opamp"
    assert intent["confidence"] == "high"
    assert intent["device_roles"]["M0"] == "input pair"
    assert intent["device_roles"]["M5"] == "tail current source"
    assert intent["device_roles"]["M8"] == "second-stage PMOS pull-up"
    assert intent["device_roles"]["M9"] == "CMFB-controlled NMOS pull-down"
    assert intent["design_var_roles"]["nfin_n"]["instances"] == ["M1", "M0"]
    assert "second-stage PMOS pull-up" in intent["design_var_roles"]["nfin_p_2"]["role"]


def test_unknown_topology_stays_low_confidence() -> None:
    intent = infer_topology_intent([
        {
            "instName": "R0",
            "cell": "RESISTOR",
            "nets": {"PLUS": "a", "MINUS": "b"},
            "params": {"r": "R"},
        }
    ], design_vars=["R"])

    assert intent["circuit_class"] == "unknown"
    assert intent["confidence"] == "low"
    assert intent["human_review_required"] is True
    assert "No differential MOS input pair" in intent["notes"][0]


def test_markdown_is_intent_not_raw_model_dump() -> None:
    intent = infer_topology_intent(_opamp_instances(), design_vars=["nfin_n"])
    md = render_topology_intent_markdown(intent)

    assert "Topology hypothesis" in md
    assert "input_pair" in md
    assert "nfin_n" in md
    assert "model" not in md.lower()
    assert "C:\\" not in md
    assert "/home/" not in md


def test_markdown_passes_llm_feedback_gate_with_bias_roles() -> None:
    intent = infer_topology_intent(
        _opamp_instances(),
        design_vars=["Ibias_pmos", "nfin_p_bias"],
    )
    md = render_topology_intent_markdown(intent)

    assert "pmos_bias" not in md
    assert "role=p-channel bias" in md
    assert assert_llm_feedback_safe(md, context="topology intent") == md


def test_stage_diagnostics_identifies_unbiased_second_stage() -> None:
    intent = infer_topology_intent(_opamp_instances(), design_vars=["nfin_n"])
    text = format_stage_diagnostics(
        intent,
        {
            "nodes": {
                "/I0/net3": 0.75,
                "/I0/net6": 0.75,
                "/I0/cmfb_out": 0.001,
                "/I0/net5": 0.393,
                "/I0/net1": 0.4,
                "/vdd!": 0.8,
            },
            "instances": {
                "/I0/M0": {
                    "region_label": "subthreshold", "id": 3.6e-6,
                    "gm": 108e-6, "gds": 3.2e-6,
                },
                "/I0/M1": {
                    "region_label": "subthreshold", "id": 3.6e-6,
                    "gm": 108e-6, "gds": 3.2e-6,
                },
                "/I0/M5": {
                    "region_label": "triode", "id": 7.2e-6,
                    "gm": 147e-6, "gds": 22e-6,
                },
                "/I0/M6": {
                    "region_label": "cutoff", "id": 75e-12,
                    "gm": 2.6e-9, "gds": 84e-12,
                },
                "/I0/M8": {
                    "region_label": "cutoff", "id": 75e-12,
                    "gm": 2.6e-9, "gds": 84e-12,
                },
                "/I0/M7": {"region_label": "cutoff", "id": 75e-12},
                "/I0/M9": {"region_label": "cutoff", "id": 75e-12},
            },
        },
    )

    assert "Stage-level diagnosis" in text
    assert "near VDD" in text
    assert "second-stage pull-up devices are off" in text
    assert "CMFB pull-down devices are off" in text
