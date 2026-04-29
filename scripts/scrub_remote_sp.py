#!/usr/bin/env python3
"""Fetch a remote .sp / netlist file, scrub it via src.hspice_scrub.scrub_sp,
write the scrubbed text locally.

The unscrubbed payload is held only in memory between SSH cat and
scrub_sp; only the scrubbed text ever touches local disk.

Usage:
    python scripts/scrub_remote_sp.py \
        --remote /project/.../dut_tb.sp \
        --output projects/<name>/circuit/dut_tb.scrubbed.sp

SSH host / user are read from config/.env (VB_REMOTE_HOST, VB_REMOTE_USER).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from src.hspice_scrub import ScrubError, scrub_sp


def main() -> int:
    load_dotenv(PROJECT_ROOT / "config" / ".env")
    host = os.environ.get("VB_REMOTE_HOST")
    user = os.environ.get("VB_REMOTE_USER")
    if not host or not user:
        print(
            "ERROR: VB_REMOTE_HOST / VB_REMOTE_USER missing in config/.env",
            file=sys.stderr,
        )
        return 1

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--remote", required=True,
        help="absolute POSIX path on the remote host (cobi)",
    )
    ap.add_argument(
        "--output", required=True,
        help="local output path; parent dirs created if needed",
    )
    args = ap.parse_args()

    if not args.remote.startswith("/"):
        print(
            f"ERROR: --remote must be absolute, got {args.remote!r}",
            file=sys.stderr,
        )
        return 2

    remote_q = "'" + args.remote.replace("'", "'\\''") + "'"
    cmd = ["ssh", f"{user}@{host}", f"/bin/cat {remote_q}"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        print("ERROR: ssh cat timed out after 60s", file=sys.stderr)
        return 3

    if proc.returncode != 0:
        tail = (proc.stderr or "")[-200:]
        print(
            f"ERROR: ssh cat failed rc={proc.returncode}: {tail}",
            file=sys.stderr,
        )
        return 4

    raw = proc.stdout
    raw_bytes = len(raw.encode("utf-8"))

    try:
        scrubbed = scrub_sp(raw)
    except ScrubError as exc:
        print(f"SCRUB FAILED: {exc}", file=sys.stderr)
        print(
            "  Some tokens slipped past the scrub pass. Add them to "
            "config/hspice_scrub_patterns.private.yaml banned_tokens "
            "and re-run.",
            file=sys.stderr,
        )
        return 5

    scrubbed_bytes = len(scrubbed.encode("utf-8"))

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(scrubbed, encoding="utf-8")

    print(f"OK  {args.remote}")
    print(f"    -> {out_path}")
    print(
        f"    raw {raw_bytes:>7} B  -> scrubbed {scrubbed_bytes:>7} B  "
        f"(delta {scrubbed_bytes - raw_bytes:+d})"
    )
    print(
        f"    <redacted>={scrubbed.count('<redacted>'):>4}  "
        f"<path>={scrubbed.count('<path>'):>4}  "
        f"<model_lib>={scrubbed.count('<model_lib>'):>4}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
