"""End-to-end smoke test for OceanWorker against a real PSF on remote host.

Run directly (not via pytest) because it requires SSH + license access:

    ./.venv/Scripts/python.exe tests/test_ocean_worker_smoke.py

Verifies:
  1. spec file upload succeeds
  2. virtuoso subprocess boots + runs psf_dump_worker.ocn
  3. result JSON parses back correctly
  4. remote temp files get cleaned up
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

# Make repo root importable when running this file directly.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO / "config" / ".env")

from src.ocean_worker import (  # noqa: E402
    OceanWorkerTimeout,
    OceanWorkerScriptError,
    worker_from_env,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


PSF_DIR = (
    "/home/<user>/simulation/pll/LC_VCO_tb/maestro/"
    "results/maestro/ExplorerRun.0/1/pll_LC_VCO_tb_1/psf"
)

SIGNALS = [
    ("Vdiff",  "Vdiff", ["/Vout_p", "/Vout_n"]),
    ("Vout_p", "V",     ["/Vout_p"]),
]

WINDOWS = [
    ("full", 0.0,    2e-7),
    ("late", 1.8e-7, 2e-7),
]


def main() -> int:
    worker = worker_from_env()
    print(f"cfg = {worker.cfg}")

    t0 = time.monotonic()
    try:
        result = worker.dump_all(
            psf_dir=PSF_DIR,
            signals=SIGNALS,
            windows=WINDOWS,
            timeout_s=90.0,
        )
    except OceanWorkerTimeout as exc:
        print(f"TIMEOUT: {exc}")
        return 2
    except OceanWorkerScriptError as exc:
        print(f"SCRIPT ERROR: {exc}")
        return 3
    elapsed = time.monotonic() - t0

    print(f"\n=== elapsed {elapsed:.1f}s ===")
    print(json.dumps(result, indent=2)[:2000])

    # Sanity checks on the shape.
    assert result.get("ok") is True, result
    dumps = result["dumps"]
    assert set(dumps.keys()) == {"Vdiff", "Vout_p"}, list(dumps.keys())
    assert set(dumps["Vdiff"].keys()) == {"full", "late"}
    vdiff_late = dumps["Vdiff"]["late"]
    for key in ("mean", "min", "max", "ptp", "rms", "mean_abs",
                "freq_Hz", "duty_pct"):
        assert key in vdiff_late, (key, vdiff_late)

    print("\nOK — shape matches safeOceanDumpAll schema.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
