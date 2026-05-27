"""Compute LC_VCO tuning-curve metrics from existing Maestro sweep PSFs.

Workflow:
1. Copy _ocean_tuning_extract.ocn to the remote host's /tmp/.
2. Run virtuoso -ocean -nograph -restore <that file> over SSH.
   The OCEAN script loops 9 sweep points, opens each PSF, evaluates
   ``frequency(clip(VT("/Vout_p") - VT("/Vout_n") 1e-7 2e-7))`` and
   writes JSON to /tmp.
3. scp the JSON back and run ``src/spec_evaluator.evaluate_swept`` on
   the resulting (Vctrl, f_osc_GHz) table — same code path the agent's
   sweep phase uses, so pass/fail bands stay in lockstep with
   ``projects/lc_vco_base/constraints/spec.md`` §6.
4. Render a human-readable report and exit non-zero if any tuning
   metric is non-PASS.

Notes:
- The 9-point sweep against Vctrl in [0, 0.8] V must already be present
  under ``VB_TUNING_ROOT`` on the remote host; no re-run is performed.
- This script is read-only on the remote host (no new sims). It does NOT
  go through SafeBridge because it reads already-computed results from an
  operator-provided PSF root.
- Path-2 (2026-05-19): kept as a no-bridge debug entrypoint. The same
  ``evaluate_swept`` is consumed by ``CircuitAgent._run_sweep_phase`` for
  the in-loop tuning gate; this script is the offline twin.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src import spec_evaluator  # noqa: E402 — path setup above

REMOTE_HOST = os.environ.get("VB_REMOTE_HOST", "").strip()
REMOTE_USER = os.environ.get("VB_REMOTE_USER", "").strip()
REMOTE_ROOT = os.environ.get("VB_TUNING_ROOT", "").strip()
VIRTUOSO_BIN = os.environ.get("VB_VIRTUOSO_BIN", "virtuoso").strip()

SPEC_PATH = _REPO_ROOT / "projects" / "lc_vco_base" / "constraints" / "spec.md"

OCN_LOCAL = Path(__file__).with_name("_ocean_tuning_extract.ocn")


def _load_eval_block() -> dict:
    """Pull the spec.md eval block so this script's pass bands match
    whatever the agent will actually gate convergence on. Raises if the
    spec is missing or malformed — keeps the script as a faithful
    offline twin instead of a stale copy."""
    text = SPEC_PATH.read_text(encoding="utf-8")
    block = spec_evaluator.extract_eval_block(text)
    if block is None:
        raise RuntimeError(f"No eval block found in {SPEC_PATH}")
    if not block.get("tuning_metrics"):
        raise RuntimeError(
            f"{SPEC_PATH} has no `tuning_metrics:` — nothing to evaluate."
        )
    return block


def ssh_args() -> list[str]:
    if not REMOTE_HOST or not REMOTE_USER:
        raise RuntimeError("Set VB_REMOTE_HOST and VB_REMOTE_USER first.")
    return [
        "ssh",
        "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval=15",
        f"{REMOTE_USER}@{REMOTE_HOST}",
    ]


def upload(local: Path, remote: str) -> None:
    cmd = ssh_args() + [f"cat > {remote}"]
    with local.open("rb") as fh:
        proc = subprocess.run(cmd, stdin=fh, capture_output=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(
            f"scp upload failed rc={proc.returncode}: "
            f"{proc.stderr.decode(errors='replace')[-400:]}"
        )


def run_ocean(remote_ocn: str, remote_out: str, timeout_s: float = 180.0) -> str:
    cmd = ssh_args() + [
        f"env VB_TUNING_ROOT={remote_root_q()} "
        f"VB_TUNING_OUT={remote_out} "
        f"{VIRTUOSO_BIN} -ocean -nograph -restore {remote_ocn}"
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise RuntimeError(f"virtuoso -ocean rc={proc.returncode}")
    return proc.stdout


def remote_root_q() -> str:
    if not REMOTE_ROOT:
        raise RuntimeError("Set VB_TUNING_ROOT to the remote PSF sweep root.")
    return REMOTE_ROOT


def fetch(remote: str) -> str:
    cmd = ssh_args() + [f"cat {remote}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(
            f"fetch failed rc={proc.returncode}: {proc.stderr[-400:]}"
        )
    return proc.stdout


def cleanup(*paths: str) -> None:
    if not paths:
        return
    cmd = ssh_args() + ["rm", "-f", *paths]
    subprocess.run(cmd, capture_output=True, timeout=30)


def evaluate_points(block: dict, points: list[dict]) -> tuple[
    list[dict], list[float], dict, dict,
]:
    """Sort by Vctrl, hand to ``spec_evaluator.evaluate_swept``.

    Each `points` entry is ``{"vctrl": <V>, "f_osc_GHz": <GHz>|None}``.
    Returns ``(sorted_points, vctrls, tuning_measurements, tuning_pass_fail)``.
    """
    valid = [p for p in points if p.get("f_osc_GHz") is not None]
    valid.sort(key=lambda p: p["vctrl"])
    if len(valid) < 2:
        raise RuntimeError(f"only {len(valid)} valid points")

    vctrls = [float(p["vctrl"]) for p in valid]
    base_per_point = [{"f_osc_GHz": float(p["f_osc_GHz"])} for p in valid]
    tuning_meas, tuning_pf = spec_evaluator.evaluate_swept(
        block, base_per_point, vctrls,
    )
    return valid, vctrls, tuning_meas, tuning_pf


def report(
    sorted_points: list[dict],
    tuning_meas: dict,
    tuning_pf: dict,
) -> int:
    print("=== tuning curve ===")
    for p in sorted_points:
        print(f"  Vctrl={p['vctrl']:.2f} V  →  f_osc={p['f_osc_GHz']:.3f} GHz")
    print()
    print("=== tuning metrics (spec_evaluator.evaluate_swept) ===")
    for name, verdict in tuning_pf.items():
        value = tuning_meas.get(name)
        if isinstance(value, list):
            value_str = "[" + ", ".join(f"{v:+.1f}" for v in value) + "]"
        elif isinstance(value, float):
            value_str = f"{value:.3f}"
        else:
            value_str = str(value)
        print(f"  {name:24s} = {value_str}  → {verdict}")
    overall = all(
        str(v).strip().upper().startswith("PASS")
        for v in tuning_pf.values()
    )
    print()
    print(f"=== OVERALL: {'PASS' if overall else 'FAIL'} ===")
    return 0 if overall else 1


def main() -> int:
    block = _load_eval_block()

    run_id = uuid.uuid4().hex[:8]
    remote_ocn = f"/tmp/vb_tuning_extract_{run_id}.ocn"
    remote_out = f"/tmp/vb_tuning_curve_{run_id}.json"

    print(f"[1/4] uploading OCEAN script to {remote_ocn} ...")
    upload(OCN_LOCAL, remote_ocn)

    print(f"[2/4] running virtuoso -ocean (eval 9 PSFs) ...")
    stdout = run_ocean(remote_ocn, remote_out)
    last_line = stdout.strip().splitlines()[-1] if stdout.strip() else ""
    print(f"[2/4] worker status: {last_line}")

    print(f"[3/4] fetching JSON from {remote_out} ...")
    try:
        body = fetch(remote_out)
    finally:
        cleanup(remote_ocn, remote_out)

    data = json.loads(body)
    pts = data["points"]
    print(f"[4/4] computing tuning metrics ({len(pts)} points) ...")
    try:
        sorted_points, _vctrls, tuning_meas, tuning_pf = evaluate_points(
            block, pts,
        )
    except RuntimeError as exc:
        print(f"FAIL: {exc}")
        return 1
    return report(sorted_points, tuning_meas, tuning_pf)


if __name__ == "__main__":
    sys.exit(main())
