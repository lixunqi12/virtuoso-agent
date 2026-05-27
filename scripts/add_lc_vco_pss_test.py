"""Create pll_LC_VCO_tb_pss Maestro test + enable PSS analysis."""
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

print(f"\n=== creating test row {TEST_NAME} ===")
out = bridge.create_maestro_test(
    test=TEST_NAME,
    lib="pll",
    cell="LC_VCO_tb",
    view="schematic",
    simulator="spectre",
)
print(f"create_maestro_test → {out[:200]}")

PSS_OPTS = {
    "fund": "20G",
    "harms": "10",
    "errpreset": "conservative",
    "oscillator": "yes",
    "autonomous": "yes",
    "tstab": "200n",
}

print(f"\n=== enabling pss on {TEST_NAME} ===")
print(f"options: {PSS_OPTS}")
out = bridge.set_maestro_analysis(
    analysis="pss",
    enable=True,
    options=PSS_OPTS,
    test=TEST_NAME,
)
print(f"set_maestro_analysis → {out[:200]}")

print(f"\n=== verifying remote test list ===")
print(sorted(bridge._list_remote_maestro_tests()))
