#!/usr/bin/env python3
"""Live smoke check for safeOceanRun per-DUT saveOpPoint effectiveness.

The check intentionally reports only aggregate counts and allowlisted OP
scalar names. It does not print raw PSF paths, PDK model names, or full
operating-point dumps.
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

from src.agent import _op_point_probe_paths
from src.safe_bridge import (
    SafeBridge,
    _scrub,
    assess_op_point_save_effectiveness,
)


def _parse_design_var(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(
            "--design-var entries must use NAME=VALUE"
        )
    name, value = raw.split("=", 1)
    name = name.strip()
    value = value.strip()
    if not name or not value:
        raise argparse.ArgumentTypeError(
            "--design-var entries must use non-empty NAME=VALUE"
        )
    return name, value


def _analysis_specs_from_input_scs(
    bridge: SafeBridge,
    lib: str,
    tb_cell: str,
) -> list[Any]:
    found = bridge.find_input_scs(lib, tb_cell)
    if found is None:
        return [
            (
                "dc",
                {
                    "oppoint": "rawfile",
                    "detail": "all",
                    "maxiters": "150",
                    "maxsteps": "10000",
                },
            )
        ]
    analyses = bridge.list_analyses(found["path"])
    if not analyses:
        return ["dc"]
    return [
        (item["name"], item.get("kwargs") or [])
        for item in analyses
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a bounded OCEAN smoke and verify per-DUT saveOpPoint "
            "produced safe numeric OP scalars."
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
        op_point = bridge.read_dc_op_point_from_results(
            nets=nets,
            instances=inst_paths,
        )
        check = assess_op_point_save_effectiveness(sim_result, op_point)
    except Exception as exc:  # noqa: BLE001 - CLI smoke should report cleanly
        report = {
            "ok": False,
            "error": _scrub(f"{type(exc).__name__}: {exc}"),
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1

    report = {
        "ok": bool(check.get("ok")),
        "saveOpPoint": check,
        "opPoint": {
            "nodesReturned": len(op_point.get("nodes") or {}),
            "instancesReturned": len(op_point.get("instances") or {}),
            "resultKinds": op_point.get("resultKinds") or [],
            "issues": op_point.get("issues") or [],
        },
        "sim": {
            "ok": bool(sim_result.get("ok")),
            "varsApplied": sim_result.get("varsApplied"),
            "analyses": sim_result.get("analyses") or [],
        },
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.report_json:
        Path(args.report_json).write_text(text + "\n", encoding="utf-8")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
