"""Probe what test rows currently exist in LC_VCO_tb Maestro."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from virtuoso_bridge import VirtuosoClient
from src.safe_bridge import SafeBridge

PDK_MAP = PROJECT_ROOT / "config" / "pdk_map.yaml"

client = VirtuosoClient(host="127.0.0.1", port=65061)
bridge = SafeBridge(client, str(PDK_MAP))
bridge.set_scope("pll", "LC_VCO", tb_cell="LC_VCO_tb")

remote_tests = bridge._list_remote_maestro_tests()
print(f"remote tests: {sorted(remote_tests)}")
