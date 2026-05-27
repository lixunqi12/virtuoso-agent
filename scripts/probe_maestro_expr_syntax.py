"""Probe what expression syntax Maestro Outputs Setup accepts."""
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

TEST = "pll_LC_VCO_tb_1"

# Candidates to try (most→least likely)
candidates = [
    ("comma+quoted_net",
     'peakToPeak(clip(VT("/Vout_p") 1.5e-07 2e-07))'),
    ("comma_args+quoted_net",
     'peakToPeak(clip(VT("/Vout_p"), 1.5e-07, 2e-07))'),
    ("ade_calc_v",
     'peakToPeak(clip(v("/Vout_p" ?result "tran") 1.5e-07 2e-07))'),
    ("plain_v_unquoted",
     'peakToPeak(v("/Vout_p"))'),
    ("vt_plain_no_clip",
     'peakToPeak(VT("/Vout_p"))'),
    ("vt_unquoted",
     'peakToPeak(VT(/Vout_p))'),
]

for label, expr in candidates:
    name = f"probe_{label}"
    print(f"\n--- trying {label} ---")
    print(f"expr: {expr}")
    try:
        out = bridge.add_maestro_output(name=name, expr=expr, test=TEST)
        print(f"OK: {out[:200]}")
    except Exception as exc:
        msg = str(exc)
        print(f"FAIL: {msg[:300]}")
