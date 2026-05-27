"""Read LC_VCO_tb Maestro state via virtuoso_bridge (one-shot, read-only)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro import reader, session

client = VirtuosoClient(host="127.0.0.1", port=65061)

sess = session.find_open_session(client)
print(f"open session: {sess!r}")
if sess is None:
    print("no open Maestro session — open LC_VCO_tb in Maestro on cobi first")
    sys.exit(1)

cfg = reader.read_config(client, sess)
print(f"\n=== read_config keys ({len(cfg)}) ===")
for key, (skill_expr, raw) in cfg.items():
    print(f"\n--- {key} ---")
    print(f"skill: {skill_expr[:200]}")
    print(f"raw:   {raw[:600] if raw else '<empty>'}")
