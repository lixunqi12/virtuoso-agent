#!/usr/bin/env python3
"""Live probe for the Spectre OP scalar name that carries true MOS vdsat.

The report is intentionally bounded: it prints only allowlisted candidate
names, aggregate hit counts, requested DUT instance paths, and numeric values.
It does not print raw PSF paths, PDK model names, or unfiltered OP key lists.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from virtuoso_bridge import VirtuosoClient

from scripts.check_op_point_save_effectiveness import (
    _analysis_specs_from_input_scs,
    _parse_design_var,
)
from src.agent import _op_point_probe_paths
from src.safe_bridge import SafeBridge, _scrub


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a bounded OCEAN DC/AC smoke and probe the true Spectre OP "
            "name used for MOS vdsat."
        )
    )
    parser.add_argument("--lib", required=True)
    parser.add_argument("--cell", required=True)
    parser.add_argument("--tb-cell", required=True)
    parser.add_argument("--dut-path", default="/I0")
    parser.add_argument(
        "--design-var",
        action="append",
        default=[],
        type=_parse_design_var,
        metavar="NAME=VALUE",
        help="Optional design variable override; may be repeated.",
    )
    parser.add_argument(
        "--pdk-map",
        default=str(PROJECT_ROOT / "config" / "pdk_map.yaml"),
    )
    parser.add_argument(
        "--env-file",
        default=str(PROJECT_ROOT / "config" / ".env"),
    )
    parser.add_argument("--remote-skill-dir", default=None)
    parser.add_argument("--report-json", default="")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if not args.verbose:
        for name in ("virtuoso_bridge", "src.safe_bridge"):
            logging.getLogger(name).setLevel(logging.WARNING)

    env_path = Path(args.env_file)
    if env_path.exists():
        load_dotenv(env_path)

    try:
        client = VirtuosoClient.from_env()
        bridge = SafeBridge(
            client,
            args.pdk_map,
            remote_skill_dir=args.remote_skill_dir,
        )
        bridge.set_scope(args.lib, args.cell, tb_cell=args.tb_cell)
        schematic = bridge.read_circuit(args.lib, args.cell)
        instances = schematic.get("instances")
        if not isinstance(instances, list):
            instances = []
        nets, inst_paths = _op_point_probe_paths(instances)
        analyses = _analysis_specs_from_input_scs(bridge, args.lib, args.tb_cell)
        sim_result = bridge.run_ocean_sim(
            args.lib,
            args.cell,
            args.tb_cell,
            design_vars=dict(args.design_var),
            analyses=analyses,
            dut_path=args.dut_path,
        )
        probe = bridge.probe_vdsat_aliases_from_results(instances=inst_paths)
        op_point_error = None
        try:
            op_point = bridge.read_dc_op_point_from_results(
                nets=nets,
                instances=inst_paths,
            )
        except Exception as exc:  # noqa: BLE001 - probe result is primary
            op_point = {}
            op_point_error = _scrub(f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - live probe should report cleanly
        report = {
            "ok": False,
            "error": _scrub(f"{type(exc).__name__}: {exc}"),
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1

    canonical_vdsat_devices = 0
    for params in (op_point.get("instances") or {}).values():
        if isinstance(params, dict) and isinstance(params.get("vdsat"), (int, float)):
            canonical_vdsat_devices += 1

    report: dict[str, Any] = {
        "ok": probe.get("actualName") is not None,
        "vdsatProbe": probe,
        "opPoint": {
            "nodesReturned": len(op_point.get("nodes") or {}),
            "instancesReturned": len(op_point.get("instances") or {}),
            "canonicalVdsatDevices": canonical_vdsat_devices,
            "resultKinds": op_point.get("resultKinds") or [],
            "issues": op_point.get("issues") or [],
            "error": op_point_error,
        },
        "sim": {
            "ok": bool(sim_result.get("ok")),
            "varsApplied": sim_result.get("varsApplied"),
            "analyses": sim_result.get("analyses") or [],
            "opPointsRequested": sim_result.get("opPointsRequested"),
        },
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.report_json:
        Path(args.report_json).write_text(text + "\n", encoding="utf-8")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
