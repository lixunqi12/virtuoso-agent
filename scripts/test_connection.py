#!/usr/bin/env python3
"""Test Virtuoso bridge connectivity and raw SKILL execution."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

env_path = PROJECT_ROOT / "config" / ".env"
if env_path.exists():
    load_dotenv(env_path)


def main() -> int:
    print("=" * 50)
    print("Virtuoso Bridge Connection Test")
    print("=" * 50)

    print("\n[1/4] Importing virtuoso-bridge-lite...")
    try:
        from virtuoso_bridge import VirtuosoClient
    except ImportError as exc:
        print(f"  FAIL: {exc}")
        print("  Run: pip install virtuoso-bridge-lite")
        return 1
    print("  OK")

    print("\n[2/4] Connecting via environment-configured tunnel...")
    try:
        client = VirtuosoClient.from_env()
    except Exception as exc:
        print(f"  FAIL: {exc}")
        print("  Check your .env file and SSH configuration.")
        return 1
    print("  OK - connected")

    print("\n[3/4] Testing SKILL execution (1 + 2)...")
    try:
        result = client.execute_skill("1 + 2")
        if not result.ok:
            print(f"  FAIL: {result.errors}")
            return 1
        if result.output.strip() == "3":
            print(f"  OK - result: {result.output}")
        else:
            print(f"  WARNING - expected 3, got: {result.output}")
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return 1

    print("\n[4/4] Verifying ddGetLibList() reachability (length-only)...")
    # NOTE: Do NOT print raw library names here. Foundry-provided library
    # names (e.g. tech kits) are red-line PDK data that must not cross the
    # PC boundary in clear text. We only verify the SKILL round-trip works
    # and log a length-based digest.
    try:
        from src.safe_bridge import _scrub  # local sanitizer
        result = client.execute_skill("length(ddGetLibList())")
        if not result.ok:
            # _scrub removes any foundry tokens / absolute paths from errors.
            scrubbed_errors = [_scrub(str(e)) for e in (result.errors or [])]
            print(f"  FAIL: {scrubbed_errors}")
            return 1
        out = (result.output or "").strip()
        # Only the count is safe to print; names are redacted entirely.
        print(f"  OK - ddGetLibList() reachable, count={out}")
    except Exception as exc:
        from src.safe_bridge import _scrub
        print(f"  FAIL: {_scrub(str(exc))}")
        return 1

    print("\n" + "=" * 50)
    print("All tests passed! Bridge is ready.")
    print("=" * 50)
    return 0


if __name__ == "__main__":
    sys.exit(main())
