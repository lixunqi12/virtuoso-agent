"""CLI tests for scripts.scaffold_spec topology-intent wiring."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import scripts.scaffold_spec as scaffold_cli  # noqa: E402


class _FakeClient:
    @staticmethod
    def from_env() -> object:
        return object()


class _FakeBridge:
    last: "_FakeBridge | None" = None

    def __init__(self, client: object, pdk_map: str, remote_skill_dir: str | None = None):
        self.client = client
        self.pdk_map = pdk_map
        self.remote_skill_dir = remote_skill_dir
        self.read_circuit_called = False
        _FakeBridge.last = self

    def generate_spec_scaffold(
        self, lib: str, cell: str, tb_cell: str, scs_path: str | None = None,
    ) -> dict:
        return {
            "lib": lib,
            "cell": cell,
            "tb_cell": tb_cell,
            "dut": {
                "lib": "GENERIC_PDK",
                "cell": cell,
                "pins": [
                    {"name": "Vin_p", "direction": "input"},
                    {"name": "Vin_n", "direction": "input"},
                    {"name": "Vout_p", "direction": "output"},
                    {"name": "Vout_n", "direction": "output"},
                ],
            },
            "tb": {"lib": "GENERIC_PDK", "cell": tb_cell, "pins": []},
            "design_vars": [
                {"name": "nfin_n", "default": "1"},
                {"name": "nfin_p_2", "default": "1"},
            ],
            "analyses": [],
        }

    def read_circuit(self, lib: str, cell: str) -> dict:
        self.read_circuit_called = True
        return {
            "instances": [
                {
                    "instName": "M1",
                    "cell": "NMOS_SVT",
                    "nets": {"G": "Vin_n", "D": "net6", "S": "net2"},
                    "params": {"fingers": "nfin_n"},
                },
                {
                    "instName": "M0",
                    "cell": "NMOS_SVT",
                    "nets": {"G": "Vin_p", "D": "net3", "S": "net2"},
                    "params": {"fingers": "nfin_n"},
                },
                {
                    "instName": "M8",
                    "cell": "PMOS_SVT",
                    "nets": {"G": "net3", "D": "Vout_p", "S": "vdd!"},
                    "params": {"fingers": "nfin_p_2"},
                },
                {
                    "instName": "M6",
                    "cell": "PMOS_SVT",
                    "nets": {"G": "net6", "D": "Vout_n", "S": "vdd!"},
                    "params": {"fingers": "nfin_p_2"},
                },
            ],
        }


def test_scaffold_cli_defaults_to_topology_inference(
    tmp_path: Path, monkeypatch,
) -> None:
    pdk_map = tmp_path / "pdk_map.yaml"
    pdk_map.write_text("{}\n", encoding="utf-8")
    out = tmp_path / "spec.md"
    intent_json = tmp_path / "topology_intent.json"
    monkeypatch.setattr(scaffold_cli, "VirtuosoClient", _FakeClient)
    monkeypatch.setattr(scaffold_cli, "SafeBridge", _FakeBridge)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scaffold_spec.py",
            "--lib", "pll",
            "--cell", "opamp",
            "--tb-cell", "opamp_test",
            "--pdk-map", str(pdk_map),
            "--out", str(out),
            "--topology-intent-json", str(intent_json),
        ],
    )

    assert scaffold_cli.main() == 0
    assert _FakeBridge.last is not None
    assert _FakeBridge.last.read_circuit_called is True
    text = out.read_text(encoding="utf-8")
    assert "Topology intent hypothesis" in text
    assert "fully_differential_two_stage_opamp" in text
    payload = json.loads(intent_json.read_text(encoding="utf-8"))
    assert payload["circuit_class"] == "fully_differential_two_stage_opamp"


def test_scaffold_cli_no_infer_keeps_legacy_todo(
    tmp_path: Path, monkeypatch,
) -> None:
    pdk_map = tmp_path / "pdk_map.yaml"
    pdk_map.write_text("{}\n", encoding="utf-8")
    out = tmp_path / "spec.md"
    monkeypatch.setattr(scaffold_cli, "VirtuosoClient", _FakeClient)
    monkeypatch.setattr(scaffold_cli, "SafeBridge", _FakeBridge)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scaffold_spec.py",
            "--lib", "pll",
            "--cell", "opamp",
            "--tb-cell", "opamp_test",
            "--pdk-map", str(pdk_map),
            "--out", str(out),
            "--no-infer-topology",
        ],
    )

    assert scaffold_cli.main() == 0
    assert _FakeBridge.last is not None
    assert _FakeBridge.last.read_circuit_called is False
    text = out.read_text(encoding="utf-8")
    assert "<TODO_one_paragraph_topology_description>" in text
