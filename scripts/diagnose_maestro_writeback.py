#!/usr/bin/env python3
"""One-shot diagnostic for Maestro writeback session binding.

Connects to live Virtuoso via remote host, runs safeMae_debugInfo to inspect
open ADE windows / sessions, writes 8 safe perturbation values via
safeMaeWriteAndSave, then re-reads via safeMae_debugInfo to see if
values stuck.

Usage (from virtuoso-agent root, on machine with remote host env vars set):
    python scripts/diagnose_maestro_writeback.py \
        --lib pll --cell LC_VCO_tb \
        --remote-skill-dir skill \
        --pdk-map config/pdk_map.yaml

Requires: Cadence Maestro open with a session for pll/LC_VCO_tb.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from dotenv import load_dotenv
from virtuoso_bridge import VirtuosoClient
from src.safe_bridge import SafeBridge

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("diagnose")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lib", required=True)
    p.add_argument("--cell", required=True,
                    help="Testbench cell (e.g. LC_VCO_tb)")
    p.add_argument("--dut-cell", default=None,
                    help="DUT cell for set_scope (defaults to --cell minus '_tb')")
    p.add_argument("--remote-skill-dir", default="skill")
    p.add_argument("--pdk-map", default=str(PROJECT_ROOT / "config" / "pdk_map.yaml"))
    return p.parse_args()


# Safe perturbation values — close to defaults, won't damage a working setup.
DIAG_VARS = {
    "C":           "1.5f",
    "Ibias":       "501u",
    "L":           "1.01u",
    "nfin_cc":     "2",
    "nfin_mirror": "2",
    "nfin_neg":    "2",
    "nfin_tail":   "2",
    "R":           "1.01k",
}


def pretty(obj: object) -> str:
    return json.dumps(obj, indent=2, default=str)


def _parse_skill_result(result: object, label: str) -> object:
    """Extract payload from SKILL result, always printing raw before parsing."""
    payload = getattr(result, "output", result)
    if isinstance(payload, str):
        print(f"[{label}] Raw payload (len={len(payload)}): {payload!r}")
        try:
            return json.loads(payload)
        except json.JSONDecodeError as e:
            logger.error("[%s] JSON parse failed: %s", label, e)
            logger.error("[%s] Char at error pos: %r", label,
                         payload[max(0, e.pos - 5):e.pos + 10])
            return {"_parse_error": str(e), "_raw": payload}
    if isinstance(payload, dict):
        print(f"[{label}] Already dict: {payload}")
        return payload
    print(f"[{label}] Unexpected type {type(payload).__name__}: {payload!r}")
    return payload


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_args()

    dut_cell = args.dut_cell or args.cell.replace("_tb", "")

    logger.info("Connecting to Virtuoso bridge ...")
    client = VirtuosoClient.from_env()

    bridge = SafeBridge(
        client,
        args.pdk_map,
        remote_skill_dir=args.remote_skill_dir,
    )
    bridge.set_scope(args.lib, dut_cell, tb_cell=args.cell)
    logger.info("SafeBridge ready (scope: lib=%s dut=%s tb=%s)",
                args.lib, dut_cell, args.cell)

    # safeMae_debugInfo is defined in safe_maestro.il (loaded by bridge init).
    # Verify it's available by checking the local source.
    local_il = Path(args.remote_skill_dir) / "safe_maestro.il"
    if local_il.exists() and "safeMae_debugInfo" not in local_il.read_text():
        logger.error(
            "safe_maestro.il exists but does not contain safeMae_debugInfo. "
            "Make sure the latest safe_maestro.il is synced to remote host."
        )
        sys.exit(1)

    # --- Phase 1: pre-write diagnostic ---
    print("\n" + "=" * 60)
    print("PHASE 1: PRE-WRITE safeMae_debugInfo")
    print("=" * 60)
    expr = f'safeMae_debugInfo("{args.lib}" "{args.cell}")'
    logger.info("Calling: %s", expr)
    pre = client.execute_skill(expr)
    pre_payload = _parse_skill_result(pre, "PRE-WRITE debugInfo")
    print(pretty(pre_payload))

    # --- Phase 2: write perturbation via production path ---
    print("\n" + "=" * 60)
    print("PHASE 2: safeMaeWriteAndSave (via bridge.write_and_save_maestro)")
    print(f"  vars = {DIAG_VARS}")
    print("=" * 60)
    try:
        result = bridge.write_and_save_maestro(DIAG_VARS)
        print(f"Result: {pretty(result)}")
    except Exception as exc:
        print(f"ERROR: {exc}")
        logger.exception("write_and_save_maestro failed")

    # --- Phase 3: post-write diagnostic ---
    print("\n" + "=" * 60)
    print("PHASE 3: POST-WRITE safeMae_debugInfo")
    print("=" * 60)
    post = client.execute_skill(expr)
    post_payload = _parse_skill_result(post, "POST-WRITE debugInfo")
    print(pretty(post_payload))

    # --- Phase 4: diff ---
    print("\n" + "=" * 60)
    print("PHASE 4: DIFF (pre vs post probeVars)")
    print("=" * 60)
    pre_vars = {v["var"]: v["value"] for v in (pre_payload or {}).get("probeVars", [])}
    post_vars = {v["var"]: v["value"] for v in (post_payload or {}).get("probeVars", [])}
    for vn in sorted(set(pre_vars) | set(post_vars)):
        old = pre_vars.get(vn, "<missing>")
        new = post_vars.get(vn, "<missing>")
        marker = " <<<< UNCHANGED" if old == new else ""
        print(f"  {vn:15s}  {old:>12s} -> {new:>12s}{marker}")

    print("\nDone. Send this entire output to the team.")


if __name__ == "__main__":
    main()
