"""Add pnoise analysis to pll_LC_VCO_tb_pss (assumes pss already exists)."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from virtuoso_bridge import VirtuosoClient
from src.safe_bridge import SafeBridge

PDK_MAP = PROJECT_ROOT / "config" / "pdk_map.yaml"
TEST_NAME = "pll_LC_VCO_tb_pss"

client = VirtuosoClient(host="127.0.0.1", port=65061)
bridge = SafeBridge(client, str(PDK_MAP))
bridge.set_scope("pll", "LC_VCO", tb_cell="LC_VCO_tb")
print(f"SafeBridge scope: pll/LC_VCO (tb=LC_VCO_tb)")

# Re-affirm pss to populate PC-side cache (idempotent on remote — same opts).
PSS_OPTS = {
    "fund": "20G",
    "harms": "10",
    "errpreset": "conservative",
    "oscillator": "yes",
    "autonomous": "yes",
    "tstab": "200n",
}
print(f"\n=== re-affirming pss on {TEST_NAME} (cache primer) ===")
out = bridge.set_maestro_analysis(
    analysis="pss",
    enable=True,
    options=PSS_OPTS,
    test=TEST_NAME,
)
print(f"set_maestro_analysis(pss) → {out[:100]}")

PNOISE_OPTS = {
    "noisetype": "sources",
    "p": "/Vout_p",
    "n": "/Vout_n",
    "start": "1k",
    "stop": "100M",
    "dec": "10",
    "maxsideband": "7",
    "sweeptype": "absolute",
}

print(f"\n=== enabling pnoise on {TEST_NAME} ===")
print(f"options: {PNOISE_OPTS}")
out = bridge.set_maestro_analysis(
    analysis="pnoise",
    enable=True,
    options=PNOISE_OPTS,
    test=TEST_NAME,
)
print(f"set_maestro_analysis(pnoise) → {out[:200]}")
