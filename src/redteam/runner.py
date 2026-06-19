"""Offline red-team runner.

Builds a real SafeBridge against a mock EDA host, drives every fixed probe
through it, scans the resulting LLM-facing text with the oracle, and returns
Trial records (aggregate with ``schema.summarize``).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from src.safe_bridge import SafeBridge
from src.redteam.canaries import default_registry
from src.redteam.oracle import scan
from src.redteam.probes import ProbeContext, all_probes
from src.redteam.schema import Trial

# Minimal sanitizer config mirroring the test fixture: generic fallback cell,
# the known-safe aliases, the model-info keys to strip, and the param whitelist.
_PDK_MAP = """\
generic_cell_name: "GENERIC_DEVICE"
valid_aliases: [NMOS, NMOS_LVT, PMOS, PMOS_LVT, MIM_CAP]
model_info_keys: [toxe, u0, vth0, k1, k2, pclm]
allowed_params: [w, l, nf, m, multi, wf]
"""


def _build_bridge(tmp: Path) -> tuple[SafeBridge, MagicMock]:
    client = MagicMock()
    pdk = tmp / "pdk_map.yaml"
    pdk.write_text(_PDK_MAP, encoding="utf-8")
    bridge = SafeBridge(client, str(pdk), skill_dir=tmp / "no_skill")
    return bridge, client


def run_offline(probes=None) -> list[Trial]:
    canaries = default_registry()
    probes = probes if probes is not None else all_probes()
    trials: list[Trial] = []
    with tempfile.TemporaryDirectory() as d:
        bridge, client = _build_bridge(Path(d))
        ctx = ProbeContext(bridge=bridge, mock_client=client, canaries=canaries)
        for probe in probes:
            outcome = probe.run(ctx)
            leak = scan(outcome.text, canaries)
            trials.append(Trial(
                tier=probe.tier,
                attacker="fixed",
                trial_id=probe.probe_id,
                leaked=leak.leaked,
                matched=leak.matched,
            ))
    return trials
