"""Force a timeout to verify kill path works.

Sets wall budget to 3s — virtuoso boot alone takes 8s, so we're
guaranteed to hit the timeout branch. After the kill, a follow-up
subprocess check confirms no stray virtuoso-ocean processes linger on
the remote host.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO / "config" / ".env")

from src.ocean_worker import (  # noqa: E402
    OceanWorkerTimeout,
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

SIGNALS = [("Vdiff", "Vdiff", ["/Vout_p", "/Vout_n"])]
WINDOWS = [("late", 1.8e-7, 2e-7)]


def count_remote_virtuoso(worker) -> int:
    cmd = worker.cfg.ssh_base_args() + [
        "bash -lc 'pgrep -u $USER -f \"virtuoso -ocean\" | wc -l'"
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30,
    )
    # Output may contain module-load warnings; take last numeric line.
    for line in reversed(proc.stdout.strip().splitlines()):
        line = line.strip()
        if line.isdigit():
            return int(line)
    return -1


def main() -> int:
    worker = worker_from_env()

    before = count_remote_virtuoso(worker)
    print(f"before: {before} virtuoso-ocean process(es) on remote host")

    t0 = time.monotonic()
    try:
        worker.dump_all(
            psf_dir=PSF_DIR, signals=SIGNALS, windows=WINDOWS,
            timeout_s=3.0,
        )
    except OceanWorkerTimeout as exc:
        elapsed = time.monotonic() - t0
        print(f"OK — got OceanWorkerTimeout in {elapsed:.1f}s: {exc}")
    else:
        print("FAIL — expected OceanWorkerTimeout but dump_all returned")
        return 1

    # Give remote a moment to actually die.
    time.sleep(2.0)
    after = count_remote_virtuoso(worker)
    print(f"after:  {after} virtuoso-ocean process(es) on remote host")

    # We expect `after <= before` — our spawned process must be dead.
    if after > before:
        print("FAIL — stray virtuoso subprocess survived the kill")
        return 2

    print("\nOK — kill -9 path verified; no lingering processes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
