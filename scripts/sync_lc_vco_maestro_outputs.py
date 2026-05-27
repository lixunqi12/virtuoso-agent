"""Sync LC_VCO_tb spec metrics → Maestro Outputs (one-shot, write)."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro import session

from src.maestro_metric_sync import sync_spec_metrics_to_maestro
from src.safe_bridge import SafeBridge
from src.spec_evaluator import extract_eval_block

SPEC_PATH = PROJECT_ROOT / "projects" / "lc_vco_base" / "constraints" / "spec.md"
PDK_MAP = PROJECT_ROOT / "config" / "pdk_map.yaml"

client = VirtuosoClient(host="127.0.0.1", port=65061)

sess = session.find_open_session(client)
print(f"open session: {sess!r}")
if sess is None:
    print("no open Maestro session — open LC_VCO_tb in Maestro on cobi first")
    sys.exit(1)

eval_block = extract_eval_block(SPEC_PATH.read_text(encoding="utf-8"))
if eval_block is None:
    print(f"failed to parse eval block from {SPEC_PATH}")
    sys.exit(1)
print(f"loaded {len(eval_block.get('metrics', []))} metric(s) from spec")

bridge = SafeBridge(client, str(PDK_MAP))
bridge.set_scope("pll", "LC_VCO", tb_cell="LC_VCO_tb")
print("SafeBridge scope: pll/LC_VCO (tb=LC_VCO_tb)")

result = sync_spec_metrics_to_maestro(bridge, eval_block, test="pll_LC_VCO_tb_1")
print(f"\n=== sync result ===")
print(f"added ({len(result['added'])}): {result['added']}")
print(f"skipped ({len(result['skipped'])}):")
for name, reason in result["skipped"]:
    print(f"  - {name}: {reason}")
