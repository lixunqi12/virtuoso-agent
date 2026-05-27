"""Probe full_opamp hierarchy via safe_bridge (one-shot)."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.safe_bridge import SafeBridge

skill_dir = os.environ.get("VB_REMOTE_SKILL_DIR")
bridge = SafeBridge(
    host="127.0.0.1",
    port=65061,
    remote_skill_dir=skill_dir,
)

candidates = [
    ("pll", "full_opamp"),
    ("pll", "opamp_1"),
    ("analog_proj", "full_opamp"),
]

for lib, cell in candidates:
    print(f"\n=== try lib={lib!r} cell={cell!r} ===", flush=True)
    try:
        data = bridge.read_circuit_hierarchical(lib, cell, max_depth=4)
        print(f"OK — depth_limit_hit={data.get('depth_limit_hit')}, "
              f"max_depth_reached={data.get('max_depth_reached')}, "
              f"subcells={len(data.get('subcells', []))}")
        out_path = Path(__file__).parent / f"probe_{lib}_{cell}.json"
        out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        print(f"  saved to {out_path}")
        break
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
