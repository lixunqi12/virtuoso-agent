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
from src.project import resolve_project
from src.safe_bridge import SafeBridge
from src import spec_evaluator, spec_validator


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safe Analog Design Agent - LLM-driven circuit optimization"
    )
    parser.add_argument(
        "--sim-backend",
        choices=["spectre", "hspice"],
        default="spectre",
        help=(
            "Simulation backend. 'spectre' (default) drives OCEAN / Maestro "
            "via SafeBridge + SKILL and runs the CircuitAgent LLM loop. "
            "'hspice' runs HSpice directly over ssh against a remote .sp "
            "netlist, parses the .mt<k> tables through parse_mt0, and reports "
            "pass/fail via evaluate_hspice — single-shot (no LLM iteration), "
            "intended for regression runs and smoke coverage on circuits "
            "that have an HSpice testbench authored alongside the Maestro one."
        ),
    )
    parser.add_argument(
        "--testbench",
        default=None,
        help=(
            "Remote POSIX path (or filename relative to --remote-spec-root "
            "in --hspice-loop mode) of the HSpice testbench .sp -- the "
            "ENTRY file HSpice executes (`hspice ./<basename>.sp`). REQUIRED "
            "when --sim-backend=hspice. NOTE: this is the RUN target. In "
            "--hspice-loop mode the file that gets REWRITTEN with new "
            "design vars each iter is determined by the spec's "
            "`hspice.param_rewrite_target` field, NOT this flag."
        ),
    )
    parser.add_argument(
        "--netlist",
        default=None,
        help=(
            "DEPRECATED alias for --testbench (same semantic, kept for "
            "backward compat). Use --testbench instead -- the name "
            "`--netlist` mis-suggests this is the rewrite target, but "
            "it is the RUN target (the .sp HSpice executes)."
        ),
    )
    parser.add_argument(
        "--lib",
        required=False,
        default=None,
        help="Virtuoso library name (required for --sim-backend=spectre)",
    )
    parser.add_argument(
        "--cell",
        required=False,
        default=None,
        help="Cell name to optimize (required for --sim-backend=spectre)",
    )
    parser.add_argument(
        "--tb-cell",
        required=False,
        default=None,
        help=(
            "Maestro testbench cell name (e.g. LC_VCO_tb) — required for "
            "--sim-backend=spectre. Must be a cell in the same library as "
            "--cell with a pre-configured Maestro session; OCEAN drives it "
            "via safeOceanRun."
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
        "--hspice-loop",
        action="store_true",
        help=(
            "T8.3 (2026-04-25): switch the HSpice backend from single-shot "
            "to closed-loop LLM-driven optimization (mirrors the OCEAN "
            "agent flow). Requires the spec to carry both a `metrics:` "
            "yaml fence AND an `hspice:` yaml fence with a "
            "`param_rewrite_target` field; requires --spec-root, "
            "--remote-spec-root, and --testbench (the .sp HSpice "
            "executes each iteration; `--netlist` is a deprecated "
            "alias). The REWRITE target -- the file mutated with new "
            "design vars before each push -- is independent: it comes "
            "from the spec's `hspice.param_rewrite_target` field "
            "(value `netlist` or `testbench`, resolved against "
            "`hspice.netlist:` / `hspice.testbench:` filenames in the "
            "same fence), pushed to <remote-spec-root>/<filename> via "
            "ssh each iter. Default off so existing single-shot "
            "regression callers are untouched."
        ),
    )
    parser.add_argument(
        "--spec-root",
        default=None,
        help=(
            "Local directory containing the .sp files referenced by "
            "spec.hspice.{netlist,testbench}. Required for --hspice-loop "
            "(the rewriter mutates the local copy of the rewrite target "
            "before each push). Defaults to the directory containing "
            "--spec when omitted."
        ),
    )
    parser.add_argument(
        "--remote-spec-root",
        default=None,
        help=(
            "POSIX directory on the remote host where the spec's .sp "
            "files live. Required for --hspice-loop. The push step "
            "writes <remote-spec-root>/<param_rewrite_target_filename>."
        ),
    )
    parser.add_argument(
        "--project",
        default=None,
        help=(
            "Project name (lowercase letters/digits/underscore; e.g. "
            "lc_vco_base, cobi_delay). When given, agent run logs and "
            "the hspice transcript are written under "
            "projects/<name>/logs/{agent,hspice}/. When omitted, the "
            "project is inferred from --spec if it lives under "
            "projects/<name>/constraints/; otherwise falls back to the "
            "reserved _scratch project. The DEFAULT_PROJECT environment "
            "variable acts as a tiebreaker before _scratch."
        ),
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )
    args = parser.parse_args()

    # --testbench is the canonical run-target flag; --netlist is kept as
    # a deprecated alias because the latter name misleadingly suggests
    # it controls the rewrite target (which actually comes from the
    # spec's hspice.param_rewrite_target). Merge into args.netlist so
    # downstream code stays untouched.
    if args.testbench and args.netlist:
        parser.error(
            "specify either --testbench or --netlist (deprecated alias), "
            "not both"
        )
    if args.testbench:
        args.netlist = args.testbench
    elif args.netlist:
        print(
            "WARNING: --netlist is deprecated; use --testbench instead. "
            "Same semantic: the .sp HSpice executes as entry point. The "
            "REWRITE target is independent and comes from the spec's "
            "hspice.param_rewrite_target field.",
            file=sys.stderr,
        )

    # MSYS / Git Bash rewrites POSIX roots like `/home/...` and
    # `/project/...` into `C:/msys64/home/...` before Python sees argv,
    # which then gets shipped to the SKILL side on remote host (Linux) where it
    # cannot be opened. Undo that here so the user doesn't have to remember
    # `MSYS_NO_PATHCONV=1` every invocation. Only activates on the known
    # mangled prefix; legit Windows paths are untouched.
    _MSYS_PREFIX = "C:/msys64"
    for _attr in ("scs_path", "remote_skill_dir", "netlist", "remote_spec_root"):
        _val = getattr(args, _attr, None)
        if isinstance(_val, str) and _val.startswith(_MSYS_PREFIX + "/"):
            setattr(args, _attr, _val[len(_MSYS_PREFIX):])

    # Backend-gated required-args validation. We can't use argparse's
    # ``required=True`` because the requirement depends on another flag.
    if args.sim_backend == "spectre":
        missing = [
            n for n, v in (
                ("--lib", args.lib),
                ("--cell", args.cell),
                ("--tb-cell", args.tb_cell),
            ) if not v
        ]
        if missing:
            parser.error(
                f"--sim-backend=spectre requires: {', '.join(missing)}"
            )
    elif args.sim_backend == "hspice":
        if not args.netlist:
            parser.error(
                "--sim-backend=hspice requires --testbench (the entry .sp "
                "HSpice executes); `--netlist` is accepted as a deprecated "
                "alias"
            )
    return args


def _run_hspice(
    args: argparse.Namespace,
    spec_block: dict | None,
    logger: logging.Logger,
) -> int:
    """Single-shot HSpice flow.

    Returns:
        0  — every metric PASS
        1  — spec / config error (no YAML block, missing metrics)
        2  — HspiceWorker transport or script error
        3  — metric-name lookup failed (spec author or netlist bug)
        4  — simulation ran and parsed but at least one metric FAIL /
             UNMEASURABLE
    """
    # Imported lazily so the spectre code path (which dominates CI)
    # doesn't pay the cost of importing the HSpice modules on every
    # invocation, and so tests can monkey-patch ``worker_from_env`` on
    # the ``src.hspice_worker`` module namespace.
    from src import hspice_resolver, hspice_worker

    if spec_block is None:
        logger.error(
            "--sim-backend=hspice requires a ```yaml signals/windows/"
            "metrics``` block in the spec; none was found. JSON specs "
            "are not supported for the HSpice backend."
        )
        return 1

    metrics = spec_block.get("metrics") or []
    if not metrics:
        logger.error("Spec YAML block has no 'metrics' entries; nothing to check.")
        return 1

    try:
        worker = hspice_worker.worker_from_env()
    except hspice_worker.HspiceWorkerError as exc:
        logger.error("HspiceWorker config error: %s", exc)
        return 1
    logger.info(
        "HspiceWorker ready (host=%s, hspice_bin=%s, "
        "hard_ceiling=%.0fs idle_timeout=%.0fs)",
        worker.cfg.remote_host,
        worker.cfg.hspice_bin,
        worker.cfg.hard_ceiling_s,
        worker.cfg.idle_timeout_s,
    )
    # Log only the basename — the full path may contain injection-probe
    # strings (leading-dash options, shell metachars) that T3's path
    # validation in ``HspiceWorker.run`` will reject. We don't want to
    # echo those verbatim into run logs before the ValueError fires.
    logger.info(
        "Running HSpice on remote netlist (basename=%s)",
        Path(args.netlist).name,
    )

    try:
        run_result = worker.run(args.netlist)
    except ValueError:
        # T3 path defense: shlex-unsafe or shape-invalid ``--netlist``
        # raises ``ValueError`` *before* any remote spawn. Treat as a
        # user-input config error (rc=1), not a worker transport issue.
        # Do NOT re-raise — that would leak a bare traceback to the
        # top-level run_agent caller and confuse the operator.
        #
        # Privacy: ``HspiceWorker`` embeds the offending path into the
        # exception message (e.g. ``...got '/tmp/secret/-evil.sp'``),
        # which defeats the basename-only log policy we apply on the
        # happy path. Drop the payload and log a category-only message
        # — the operator still knows *what* went wrong; the raw path
        # (which may itself be the injection probe) does not hit logs.
        logger.error(
            "Invalid --netlist argument: must be an absolute POSIX .sp "
            "path accepted by HspiceWorker (see T3 path validation rules)."
        )
        return 1
    except hspice_worker.HspiceWorkerTimeout as exc:
        logger.error("HSpice timeout: %s", exc)
        return 2
    except hspice_worker.HspiceWorkerSpawnError as exc:
        logger.error("HSpice spawn/transport error: %s", exc)
        return 2
    except hspice_worker.HspiceWorkerScriptError as exc:
        logger.error("HSpice script error: %s", exc)
        return 2

    logger.info(
        "HSpice rc=%d; parsed %d .mt<k> table(s) from %s",
        run_result.returncode,
        len(run_result.mt_files),
        run_result.run_dir_remote,
    )

    try:
        evaluation = hspice_resolver.evaluate_hspice(
            run_result.mt_files, metrics,
        )
    except hspice_resolver.HspiceMetricNotFoundError as exc:
        # Privacy: T4 designed ``HspiceMetricNotFoundError.__str__`` to
        # report only the count of available columns, not their names
        # (column names can leak node identifiers from a customer's
        # netlist). Keep that contract here — log the metric name and
        # the count, never ``exc.available``.
        logger.error(
            "Metric %r not found in any .mt<k> column (%d distinct "
            "columns seen across tables). Fix the spec metric name "
            "or the netlist .measure directive.",
            exc.metric_name,
            len(exc.available),
        )
        return 3

    print("\n" + "=" * 60)
    print("HSPICE RESULTS")
    print("=" * 60)
    print(f"  netlist          : {args.netlist}")
    print(f"  run_dir          : {run_result.run_dir_remote}")
    print(f"  hspice_rc        : {run_result.returncode}")
    print(f"  mt_tables        : {sorted(run_result.mt_files.keys())}")
    print()
    print("Measurements (list per metric across alters/rows):")
    for name, values in evaluation.measurements.items():
        print(f"  {name}: {values}")
    print()
    print("Pass/Fail:")
    for name, verdict in evaluation.pass_fail.items():
        print(f"  {name}: {verdict}")
    all_pass = bool(evaluation.pass_fail) and all(
        v == "PASS" for v in evaluation.pass_fail.values()
    )
    print()
    print(f"  overall          : {'PASS' if all_pass else 'FAIL'}")
    return 0 if all_pass else 4


def _run_hspice_loop(
    args: argparse.Namespace,
    spec_text: str,
    spec_path: Path,
    logger: logging.Logger,
) -> int:
    """Closed-loop HSpice flow (T8.3).

    Returns:
        0 — converged (every metric PASS).
        1 — spec / config error caught before any remote round-trip.
        2 — HspiceWorker transport / script failure during the loop.
        4 — loop ran to ``--max-iter`` without converging, or aborted
            on a contract / rewrite failure.
    """
    from src.agent import (
        HspiceAgent,
        _load_allowed_design_vars,
        extract_hspice_spec_blocks,
    )
    from src import hspice_worker

    if not isinstance(spec_text, str):
        logger.error("--hspice-loop requires a Markdown spec; got JSON.")
        return 1
    if not args.remote_spec_root:
        logger.error("--hspice-loop requires --remote-spec-root.")
        return 1
    if not args.netlist:
        logger.error(
            "--hspice-loop requires --testbench (the .sp HSpice executes; "
            "`--netlist` is accepted as a deprecated alias)."
        )
        return 1
    spec_root = Path(args.spec_root) if args.spec_root else spec_path.parent
    if not spec_root.is_dir():
        logger.error("--spec-root not a directory: %s", spec_root)
        return 1

    try:
        metrics, hspice_cfg = extract_hspice_spec_blocks(spec_text)
    except ValueError as exc:
        logger.error("HSpice spec parse error: %s", exc)
        return 1

    try:
        whitelist = _load_allowed_design_vars(spec_path)
    except RuntimeError as exc:
        logger.error("Design-var whitelist parse error: %s", exc)
        return 1

    target_kind = hspice_cfg["param_rewrite_target"]
    target_filename = hspice_cfg.get(target_kind)
    if not isinstance(target_filename, str) or not target_filename:
        logger.error(
            "spec.hspice.%s is empty -- cannot resolve rewrite target.",
            target_kind,
        )
        return 1
    remote_target_path = (
        args.remote_spec_root.rstrip("/") + "/" + target_filename
    )
    # T8.3-fix: local_target_path is no longer passed into HspiceAgent
    # (the rewrite executes on the remote box; the local scrubbed
    # copy never goes back). Computed here only for the operator log
    # line that records what would be patched.
    local_target_path = spec_root / target_filename
    logger.info(
        "HSpice loop: rewrite target=%s (kind=%s); local=%s remote=%s; "
        "run target=%s",
        target_filename, target_kind,
        local_target_path, remote_target_path, args.netlist,
    )

    try:
        worker = hspice_worker.worker_from_env()
    except hspice_worker.HspiceWorkerError as exc:
        logger.error("HspiceWorker config error: %s", exc)
        return 1

    llm = create_llm_client(provider=args.llm, model=args.model)
    agent = HspiceAgent(
        llm=llm,
        worker=worker,
        spec_text=spec_text,
        spec_metrics=metrics,
        whitelist=whitelist,
        remote_target_path=remote_target_path,
        remote_run_path=args.remote_spec_root.rstrip("/") + "/" + args.netlist,
    )

    project = resolve_project(
        args.project,
        spec_path=args.spec,
        default=os.environ.get("DEFAULT_PROJECT") or "_scratch",
        repo_root=PROJECT_ROOT,
    )
    project.ensure()
    transcript_path = project.logs_hspice_dir / (
        f"hspice_transcript_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    )
    logger.info("HSpice transcript: %s", transcript_path)
    result = agent.run(
        max_iter=args.max_iter,
        transcript_path=transcript_path,
    )

    print("\n" + "=" * 60)
    print("HSPICE LOOP RESULTS")
    print("=" * 60)
    print(f"  iterations       : {len(agent.history)}")
    print(f"  converged        : {result['converged']}")
    print(f"  abort_reason     : {result['abort_reason']}")
    print(f"  final design_vars: {result['design_vars']}")
    print()
    print("Pass/Fail:")
    for name, verdict in result["pass_fail"].items():
        print(f"  {name}: {verdict}")
    if result["converged"]:
        return 0
    if result["abort_reason"] == "hspice_failure":
        return 2
    return 4


def main() -> int:
    args = parse_args()

    project = resolve_project(
        args.project,
        spec_path=args.spec,
        default=os.environ.get("DEFAULT_PROJECT") or "_scratch",
        repo_root=PROJECT_ROOT,
    )
    project.ensure()
    log_dir = project.logs_agent_dir
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
    logger.info("Project: %s (root=%s)", project.name, project.root)
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
    _spec_block = None
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

    # Backend dispatch. Two HSpice modes:
    #   --hspice-loop OFF (default): single-shot regression flow --
    #     simulates the supplied .sp once, parses .mt<k>, prints
    #     pass/fail. No LLM iteration, no Virtuoso plumbing.
    #   --hspice-loop ON  (T8.3): closed-loop optimization via
    #     HspiceAgent -- LLM proposes design_vars, sp_rewrite mutates
    #     the spec's param_rewrite_target .sp, the rewritten file is
    #     pushed to the remote, HSpice runs, metrics resolve, repeat.
    if args.sim_backend == "hspice":
        if args.hspice_loop:
            return _run_hspice_loop(args, spec, spec_path, logger)
        return _run_hspice(args, _spec_block, logger)

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
