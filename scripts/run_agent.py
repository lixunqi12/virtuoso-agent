#!/usr/bin/env python3
"""Main CLI entry point for Safe Analog Design Agent.

Stage 1 rev 2 (2026-04-18): reworked per LC_VCO_spec.md §6 to drive OCEAN
over SKILL, not direct Spectre. Testbench is identified by its cell name
(e.g. ``LC_VCO_tb``) that Maestro already has a session for; no local
netlist path is needed.

Usage:
    python run_agent.py --lib pll --cell LC_VCO --tb-cell LC_VCO_tb \
                        --spec spec.json --llm claude \
                        --remote-skill-dir /proj/.../skill
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Force UTF-8 on stdout/stderr so LLM-produced Unicode (e.g. "≈", "±",
# CJK) doesn't crash Windows cp1252 when the final report is printed.
# errors='replace' as a last-resort safety net for unknown code points.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from dotenv import load_dotenv
from virtuoso_bridge import VirtuosoClient

from src.agent import CircuitAgent
from src.llm_client import create_llm_client
from src.ocean_worker import worker_from_env
from src.plan_auto import PlanAuto, parse_startup_from_spec
from src.safe_bridge import SafeBridge
from src import spec_evaluator, spec_validator


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safe Analog Design Agent - LLM-driven circuit optimization"
    )
    parser.add_argument("--lib", required=True, help="Virtuoso library name")
    parser.add_argument("--cell", required=True, help="Cell name to optimize")
    parser.add_argument(
        "--tb-cell",
        required=True,
        help=(
            "Maestro testbench cell name (e.g. LC_VCO_tb). Must be a cell "
            "in the same library as --cell with a pre-configured Maestro "
            "session; OCEAN drives it via safeOceanRun."
        ),
    )
    parser.add_argument(
        "--spec",
        required=True,
        help=(
            "Path to spec file. .md is preferred (the file you author in "
            "aionui gets embedded verbatim into the LLM prompt so §3 "
            "tables and §4 ranges reach the model cleanly); .json is "
            "still accepted for legacy callers."
        ),
    )
    parser.add_argument(
        "--llm",
        choices=["claude", "gemini", "kimi", "minimax", "ollama"],
        default=None,
        help=(
            "LLM provider. If omitted, falls back to DEFAULT_LLM in .env "
            "(which defaults to 'claude' if unset)."
        ),
    )
    parser.add_argument("--model", default=None, help="LLM model name override")
    parser.add_argument(
        "--max-iter",
        type=int,
        default=20,
        help="Maximum optimization iterations (default: 20)",
    )
    parser.add_argument(
        "--analysis",
        choices=["ac", "dc", "tran", "noise", "xf", "stb"],
        default="tran",
        help="OCEAN analysis type (default: tran — required by LC_VCO spec)",
    )
    parser.add_argument(
        "--pdk-map",
        default=str(PROJECT_ROOT / "config" / "pdk_map.yaml"),
        help="Path to PDK map YAML",
    )
    parser.add_argument(
        "--remote-skill-dir",
        default=None,
        help=(
            "POSIX path on the remote host where safe_*.il SKILL helpers live "
            "(e.g. /project/<user>/tool/virtuoso_bridge_lite/skill). "
            "REQUIRED for OCEAN + Maestro writeback (Stage 1 rev 2 made "
            "Direction C the only simulation path)."
        ),
    )
    parser.add_argument(
        "--scs-path",
        default=None,
        help=(
            "POSIX path on the remote host to the Maestro ExplorerRun "
            "input.scs for this testbench. When OMITTED (the default), "
            "the agent calls safeMaeFindInputScs on remote host to pick the "
            "newest input.scs under $HOME/simulation that matches "
            "(--lib, --tb-cell); Plan Auto + design-variable discovery "
            "both benefit. Pass this flag only to override auto-discovery "
            "(e.g. to point at a non-standard Maestro output dir)."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to .env file (default: config/.env)",
    )
    parser.add_argument(
        "--auto-bias-ic",
        action="store_true",
        help=(
            "Stage 1 rev 10 (2026-04-19): enable Plan Auto. After each "
            "iteration's transient, the agent calls safePatchNetlistIC to "
            "rewrite input.scs's `ic` line from spectre.fc so the next "
            "skipdc=yes tran starts from a valid bias snapshot (every "
            "non-IC'd node would otherwise be zeroed). Requires the "
            "spec to declare a `startup:` yaml block (see §9 of "
            "LC_VCO_spec.md) and --scs-path. Default off."
        ),
    )
    parser.add_argument(
        "--strict-spec",
        action="store_true",
        help=(
            "Stage 1 rev 5 (2026-04-19): treat spec static-feasibility "
            "warnings as hard errors. Without this flag the validator "
            "prints WARNINGs and the agent runs anyway; with it, any "
            "infeasible metric (e.g. frac*ptp unreachable) aborts at "
            "startup before any OCEAN round-trip."
        ),
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )
    args = parser.parse_args()

    # MSYS / Git Bash rewrites POSIX roots like `/home/...` and
    # `/project/...` into `C:/msys64/home/...` before Python sees argv,
    # which then gets shipped to the SKILL side on remote host (Linux) where it
    # cannot be opened. Undo that here so the user doesn't have to remember
    # `MSYS_NO_PATHCONV=1` every invocation. Only activates on the known
    # mangled prefix; legit Windows paths are untouched.
    _MSYS_PREFIX = "C:/msys64"
    for _attr in ("scs_path", "remote_skill_dir"):
        _val = getattr(args, _attr, None)
        if isinstance(_val, str) and _val.startswith(_MSYS_PREFIX + "/"):
            setattr(args, _attr, _val[len(_MSYS_PREFIX):])
    return args


def main() -> int:
    args = parse_args()

    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_path = log_dir / f"run_{_ts}.log"
    transcript_path = log_dir / f"transcript_{_ts}.jsonl"

    level = logging.DEBUG if args.verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )
    logger = logging.getLogger("run_agent")
    logger.info("Run log: %s", log_path)

    env_file = args.env_file or str(PROJECT_ROOT / "config" / ".env")
    env_path = Path(env_file)
    if env_path.exists():
        load_dotenv(env_path)
        logger.info("Loaded env from %s", env_path)
    else:
        logger.info("No .env file found at %s, using environment", env_path)

    if args.llm is None:
        args.llm = os.environ.get("DEFAULT_LLM", "claude").lower()
        logger.info("--llm not given, using DEFAULT_LLM=%s from env", args.llm)

    spec_path = Path(args.spec)
    if not spec_path.exists():
        logger.error("Spec file not found: %s", spec_path)
        return 1
    spec_text = spec_path.read_text(encoding="utf-8")
    # Stage 1 rev 3 (2026-04-18): accept either Markdown or JSON. The MD
    # path is the preferred one — user authors the spec as LC_VCO_spec.md
    # and we embed the full text verbatim into the LLM prompt (§3 tables
    # / §4 ranges / §1 topology narrative flow through unmodified). JSON
    # is still accepted for legacy or programmatic callers.
    if spec_path.suffix.lower() == ".json":
        spec: dict | str = json.loads(spec_text)
        logger.info("Loaded JSON spec: %s", spec)
    else:
        spec = spec_text
        logger.info(
            "Loaded Markdown spec: %s (%d chars)", spec_path, len(spec_text)
        )

    # Stage 1 rev 5 (2026-04-19): static feasibility check on the spec
    # *before* any Virtuoso connection or OCEAN round-trip. Catches the
    # ptp-vs-amplitude / unreachable-threshold class of bugs that used
    # to only manifest as "No crossing found" during the loop.
    if isinstance(spec, str):
        try:
            _spec_block = spec_evaluator.extract_eval_block(spec)
        except Exception as _exc:  # noqa: BLE001
            logger.error(
                "Spec YAML block failed validation: %s", _exc,
            )
            return 1
        if _spec_block is not None:
            _n_issues = spec_validator.log_feasibility_report(
                _spec_block, strict=args.strict_spec,
            )
            if args.strict_spec and _n_issues > 0:
                logger.error(
                    "--strict-spec: %d feasibility issue(s); aborting "
                    "before any OCEAN round-trip.", _n_issues,
                )
                return 1

    pdk_map_path = Path(args.pdk_map)
    if not pdk_map_path.exists():
        logger.error("PDK map file not found: %s", pdk_map_path)
        return 1

    logger.info("Connecting to Virtuoso bridge...")
    client = VirtuosoClient.from_env()

    bridge = SafeBridge(
        client,
        str(pdk_map_path),
        remote_skill_dir=args.remote_skill_dir,
    )
    logger.info(
        "SafeBridge initialized with PDK map: %s (remote_skill_dir=%s)",
        pdk_map_path,
        args.remote_skill_dir or "<PC fallback>",
    )

    # P1.3 scope binding: restrict all bridge operations to the single
    # (lib, cell) pair supplied on the CLI. Any subsequent read/write
    # targeting a different library/cell will be rejected at the bridge.
    bridge.set_scope(args.lib, args.cell, tb_cell=args.tb_cell)

    # 2026-04-22: auto-discover --scs-path from $HOME/simulation on remote host
    # when the user didn't pass one. Saves the user from having to hunt
    # down the Maestro ExplorerRun dir every time (and from passing a
    # non-existent path, which was the root cause of the waveform-doesn't-
    # display regression that otherwise looked like a MSYS env bug).
    if args.scs_path is None:
        try:
            found = bridge.find_input_scs(args.lib, args.tb_cell)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Auto-discovery of input.scs failed (%s: %s); "
                "proceeding without --scs-path.",
                type(exc).__name__, exc,
            )
            found = None
        if found is not None:
            args.scs_path = found["path"]
            logger.info(
                "Auto-discovered input.scs: %s (tier=%s, %d candidate%s)",
                args.scs_path, found["tier"], found["num_candidates"],
                "" if found["num_candidates"] == 1 else "s",
            )
        else:
            logger.info(
                "No input.scs found under $HOME/simulation for %s/%s; "
                "agent will use bare [tran] fallback. Open Maestro for "
                "the testbench and run once, or pass --scs-path.",
                args.lib, args.tb_cell,
            )

    llm_kwargs = {}
    if args.model:
        llm_kwargs["model"] = args.model
    llm = create_llm_client(args.llm, **llm_kwargs)
    logger.info("LLM client: %s", args.llm)

    # Stage 1 rev 12 (2026-04-20): OceanWorker spawns a throwaway
    # virtuoso -ocean subprocess per PSF dump so a degenerate PSF can
    # be kill -9'd without wedging the long-running RAMIC SKILL daemon.
    # Env vars come from config/.env (VB_REMOTE_HOST / VB_REMOTE_USER,
    # optional VB_REMOTE_SKILL_DIR / VB_OCEAN_TIMEOUT_S overrides).
    ocean_worker = worker_from_env()
    logger.info(
        "OceanWorker ready (host=%s, skill_dir=%s, budget=%.0fs)",
        ocean_worker.cfg.remote_host,
        ocean_worker.cfg.remote_skill_dir,
        ocean_worker.cfg.wall_timeout_s,
    )

    agent = CircuitAgent(
        bridge=bridge,
        llm=llm,
        spec=spec,
        analysis_type=args.analysis,
        ocean_worker=ocean_worker,
    )

    # Stage 1 rev 10 (2026-04-19): Plan Auto construction. Both the
    # spec-side `startup:` block AND --auto-bias-ic must be present to
    # activate; otherwise PlanAuto.active==False and the per-iter patch
    # call is a no-op. Keeping the parser + orchestrator outside the
    # agent loop keeps the agent generic — no circuit-specific knobs.
    startup_cfg = parse_startup_from_spec(spec if isinstance(spec, str) else "")
    plan_auto = PlanAuto(
        config=startup_cfg,
        scs_path=args.scs_path,
        enabled_flag=args.auto_bias_ic,
    )

    logger.info(
        "Starting optimization: %s/%s (tb_cell: %s, max_iter: %d)",
        args.lib,
        args.cell,
        args.tb_cell,
        args.max_iter,
    )
    result = agent.run(
        lib=args.lib,
        cell=args.cell,
        tb_cell=args.tb_cell,
        max_iter=args.max_iter,
        scs_path=args.scs_path,
        transcript_path=transcript_path,
        plan_auto=plan_auto,
    )

    print("\n" + "=" * 60)
    print("FINAL RESULTS")
    print("=" * 60)
    measurements = result.get("measurements", {})
    for key, value in measurements.items():
        print(f"  {key}: {value}")
    print(f"\n  converged        : {result.get('converged')}")
    print(f"  abort_reason     : {result.get('abort_reason') or '-'}")
    print(f"  writeback_status : {result.get('writeback_status') or '-'}")

    print("\n" + agent.get_optimization_report())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logging.getLogger("run_agent").exception("Agent crashed")
        raise
