#!/usr/bin/env python3
"""Focused LC_VCO evidence runner for paper-grade experiments.

This is intentionally narrower than ``scripts/run_benchmark.py``.  It runs the
current live LC_VCO 7-point tuning setup, resets Maestro to the same initial
state before every cell, and records enough metadata for reproducibility.

Default matrix:
  - multi-model: full framework for selected checkpoints
  - ablation: one checkpoint across full / no_curve_searcher /
    no_swept_metric / no_writeback

Dry-run is the default. Pass ``--execute`` to run live LLM + Spectre jobs.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from dotenv import load_dotenv
from virtuoso_bridge import VirtuosoClient

from src.safe_bridge import SafeBridge, scrub as safe_scrub


SPEC_PATH = PROJECT_ROOT / "projects" / "lc_vco_base" / "constraints" / "spec.md"
AGENT_LOG_DIR = PROJECT_ROOT / "projects" / "lc_vco_base" / "logs" / "agent"
EVIDENCE_LOG_DIR = AGENT_LOG_DIR / "evidence"
STATE_JSONL = PROJECT_ROOT / "paper" / "data" / "lc_vco_evidence_runs.jsonl"
SUMMARY_CSV = PROJECT_ROOT / "paper" / "data" / "lc_vco_evidence_summary.csv"

SIM_LIB = "pll"
SIM_CELL = "LC_VCO"
SIM_TB_CELL = "LC_VCO_tb"
MAESTRO_TEST = "pll_LC_VCO_tb_1"

SPEC_BASELINE_VARS: dict[str, str] = {
    "Vctrl": "0.4",
    "C": "50f",
    "Ibias": "500u",
    "L": "506p",
    "nfin_cc": "20",
    "nfin_mirror": "16",
    "nfin_neg": "16",
    "nfin_tail": "16",
    "R": "10k",
}

# Stress-test initialization requested by the user: Vctrl stays nominal,
# every other design variable starts from a literal all-ones value. This is
# intentionally pathological for mixed-unit variables and is reported as a
# stress baseline, not as the normal physics-aware benchmark baseline.
ALL_ONES_VARS: dict[str, str] = {
    name: ("0.4" if name == "Vctrl" else "1")
    for name in SPEC_BASELINE_VARS
}

CHECKPOINTS: dict[str, dict[str, str]] = {
    "mimo-v2.5-pro": {
        "llm": "mimo",
        "model": "mimo-v2.5-pro",
        "env": "MIMO_API_KEY",
    },
    "deepseek-v4-flash": {
        "llm": "deepseek",
        "model": "deepseek-v4-flash",
        "env": "DEEPSEEK_API_KEY",
    },
    "deepseek-v4-pro": {
        "llm": "deepseek",
        "model": "deepseek-v4-pro",
        "env": "DEEPSEEK_API_KEY",
    },
    "gpt-5.4-mini": {
        "llm": "openai",
        "model": "gpt-5.4-mini",
        "env": "OPENAI_API_KEY",
    },
    "gpt-5.5": {
        "llm": "openai",
        "model": "gpt-5.5",
        "env": "OPENAI_API_KEY",
    },
    "gemini-2.5-pro": {
        "llm": "gemini",
        "model": "gemini-2.5-pro",
        "env": "GOOGLE_API_KEY",
    },
    "kimi-k2.5": {
        "llm": "kimi",
        "model": "kimi-k2.5",
        "env": "KIMI_API_KEY",
    },
    "minimax-m2.7": {
        "llm": "minimax",
        "model": "MiniMax-M2.7",
        "env": "MINIMAX_API_KEY",
    },
    "minimax-m3": {
        "llm": "minimax",
        "model": "MiniMax-M3",
        "env": "MINIMAX_API_KEY",
    },
}

DEFAULT_MODELS = ["mimo-v2.5-pro", "deepseek-v4-flash", "gpt-5.4-mini"]
DEFAULT_ABLATION_MODEL = "mimo-v2.5-pro"


@dataclass(frozen=True)
class Cell:
    model_name: str
    variant: str
    init: str = "all_ones"
    seed: int = 1

    @property
    def key(self) -> str:
        return (
            f"{self.model_name}::{self.variant}::"
            f"init{self.init}::seed{self.seed}"
        )


@dataclass
class RunRecord:
    cell_key: str
    model_name: str
    llm: str
    model: str
    variant: str
    init: str
    seed: int
    timestamp: str
    baseline_vars: dict[str, str]
    command: list[str]
    outcome: str
    exit_code: int | None
    wall_clock_s: float
    transcript_path: str | None
    stdout_path: str | None
    converged: bool | None
    writeback_status: str | None
    n_iter: int | None
    measurements: dict[str, Any]
    tuning_measurements: dict[str, Any]
    tuning_pass_fail: dict[str, str]
    final_design_vars: dict[str, str]
    fail_reason: str | None = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--execute", action="store_true")
    p.add_argument(
        "--models",
        default=",".join(DEFAULT_MODELS),
        help="Comma-separated checkpoint names for full-framework multi-model runs.",
    )
    p.add_argument(
        "--ablation-model",
        default=DEFAULT_ABLATION_MODEL,
        help="Checkpoint used for ablation variants.",
    )
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--max-iter", type=int, default=5)
    p.add_argument("--timeout", type=int, default=7200)
    p.add_argument(
        "--init",
        choices=["spec_baseline", "all_ones"],
        default="all_ones",
        help=(
            "Initial Maestro design variables before every cell. "
            "spec_baseline uses the physical LC_VCO defaults; all_ones "
            "keeps Vctrl=0.4 and writes every other var to literal 1."
        ),
    )
    p.add_argument(
        "--sweep-results-root",
        default=None,
        help=(
            "Remote POSIX Maestro Interactive.<N> sweep root. Required for "
            "variants that evaluate swept tuning metrics. May also be set "
            "via LC_VCO_SWEEP_RESULTS_ROOT."
        ),
    )
    p.add_argument(
        "--only",
        default=None,
        help=(
            "Comma-separated variants to run. Choices: full, no_curve_searcher, "
            "no_swept_metric, no_writeback. Default runs full for all models "
            "plus ablations for --ablation-model."
        ),
    )
    p.add_argument("--no-reset", action="store_true")
    p.add_argument("--env-file", default=str(PROJECT_ROOT / "config" / ".env"))
    p.add_argument("--pdk-map", default=str(PROJECT_ROOT / "config" / "pdk_map.yaml"))
    p.add_argument("--remote-skill-dir", default=None)
    p.add_argument("--resume", action="store_true")
    return p.parse_args(argv)


def initial_vars(args: argparse.Namespace) -> dict[str, str]:
    if args.init == "all_ones":
        return dict(ALL_ONES_VARS)
    return dict(SPEC_BASELINE_VARS)


def _split_csv(text: str | None) -> list[str]:
    if not text:
        return []
    return [x.strip() for x in text.split(",") if x.strip()]


def build_cells(args: argparse.Namespace) -> list[Cell]:
    models = _split_csv(args.models)
    variants = _split_csv(args.only)
    if variants:
        out: list[Cell] = []
        for variant in variants:
            for model in models:
                out.append(Cell(model, variant, args.init, args.seed))
        return out

    cells = [Cell(model, "full", args.init, args.seed) for model in models]
    for variant in ("no_curve_searcher", "no_swept_metric", "no_writeback"):
        cells.append(Cell(args.ablation_model, variant, args.init, args.seed))

    # Deduplicate while preserving order.
    seen: set[str] = set()
    out = []
    for cell in cells:
        if cell.key not in seen:
            out.append(cell)
            seen.add(cell.key)
    return out


def load_completed() -> set[str]:
    if not STATE_JSONL.exists():
        return set()
    out: set[str] = set()
    for line in STATE_JSONL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("outcome") in {"PASS", "FAIL"}:
            out.add(str(rec.get("cell_key")))
    return out


def preflight(cells: list[Cell], args: argparse.Namespace) -> list[str]:
    missing: list[str] = []
    for cell in cells:
        ckpt = CHECKPOINTS.get(cell.model_name)
        if ckpt is None:
            missing.append(f"unknown checkpoint: {cell.model_name}")
            continue
        env_name = ckpt["env"]
        if not os.environ.get(env_name):
            missing.append(env_name)
    needs_sweep = any(cell.variant != "no_swept_metric" for cell in cells)
    if (
        needs_sweep
        and not args.sweep_results_root
        and not os.environ.get("LC_VCO_SWEEP_RESULTS_ROOT")
    ):
        missing.append("LC_VCO_SWEEP_RESULTS_ROOT or --sweep-results-root")
    return sorted(set(missing))


def reset_maestro_baseline(args: argparse.Namespace) -> dict[str, Any]:
    client = VirtuosoClient.from_env()
    bridge = SafeBridge(
        client,
        args.pdk_map,
        remote_skill_dir=(
            args.remote_skill_dir
            or os.environ.get("VB_REMOTE_SKILL_DIR")
            or None
        ),
    )
    bridge.set_scope(SIM_LIB, SIM_CELL, tb_cell=SIM_TB_CELL)
    return bridge.write_and_save_maestro(initial_vars(args))


def build_run_agent_cmd(cell: Cell, args: argparse.Namespace) -> list[str]:
    ckpt = CHECKPOINTS[cell.model_name]
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "run_agent.py"),
        "--project", "lc_vco_base",
        "--lib", SIM_LIB,
        "--cell", SIM_CELL,
        "--tb-cell", SIM_TB_CELL,
        "--maestro-test", MAESTRO_TEST,
        "--spec", str(SPEC_PATH),
        "--llm", ckpt["llm"],
        "--model", ckpt["model"],
        "--max-iter", str(args.max_iter),
        "--auto-bias-ic",
    ]
    if cell.variant != "no_swept_metric":
        sweep_root = (
            args.sweep_results_root
            or os.environ.get("LC_VCO_SWEEP_RESULTS_ROOT")
            or ""
        )
        cmd += ["--sweep-results-root", sweep_root]
    if cell.variant not in {"no_curve_searcher", "no_swept_metric"}:
        cmd += ["--enable-curve-searcher", "--curve-searcher-max-candidates", "6"]
    if cell.variant == "no_writeback":
        cmd += ["--no-writeback"]
    return cmd


def discover_transcript(started_at: float) -> Path | None:
    candidates = [
        p for p in AGENT_LOG_DIR.glob("transcript_*.jsonl")
        if p.stat().st_mtime >= started_at - 1.0
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _parse_scalar(text: str) -> Any:
    text = text.strip()
    if text in {"True", "False"}:
        return text == "True"
    if text == "-":
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def parse_stdout(stdout_path: Path) -> dict[str, Any]:
    text = stdout_path.read_text(encoding="utf-8", errors="replace")
    measurements: dict[str, Any] = {}
    tuning_measurements: dict[str, Any] = {}
    tuning_pass_fail: dict[str, str] = {}
    final_design_vars: dict[str, str] = {}

    in_final = False
    in_tuning = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip() == "FINAL RESULTS":
            in_final = True
            in_tuning = False
            continue
        if in_final and line.startswith("# Optimization Report"):
            break
        if in_final and line.strip() == "--- Tuning curve (sweep) ---":
            in_tuning = True
            continue
        if not in_final:
            continue
        m = re.match(r"\s*([A-Za-z0-9_]+):\s*(.*?)\s*$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if key in {"converged", "abort_reason", "writeback_status"}:
            continue
        if in_tuning:
            if "  " in value:
                val_text, verdict = value.split("  ", 1)
            elif "->" in value:
                val_text, verdict = value.split("->", 1)
            else:
                val_text, verdict = value, ""
            tuning_measurements[key] = _parse_scalar(val_text)
            tuning_pass_fail[key] = (
                verdict.replace("鈫?", "").replace("->", "").strip()
            )
        else:
            measurements[key] = _parse_scalar(value)

    converged = None
    writeback_status = None
    m = re.search(r"converged\s*:\s*(True|False)", text)
    if m:
        converged = m.group(1) == "True"
    m = re.search(r"writeback_status\s*:\s*([^\n]+)", text)
    if m:
        writeback_status = m.group(1).strip()

    var_block = re.search(
        r"FINAL CONVERGED VALUES.*?Variable\s+Value\s+-+\s+-+\s*(.*?)Scope:",
        text,
        re.DOTALL,
    )
    if var_block:
        for line in var_block.group(1).splitlines():
            parts = line.split()
            if len(parts) >= 2:
                final_design_vars[parts[0]] = parts[1]
    if not final_design_vars:
        design_var_matches = re.findall(
            r"^design_vars:\s*(\{.*\})\s*$",
            text,
            flags=re.MULTILINE,
        )
        if design_var_matches:
            try:
                parsed_vars = ast.literal_eval(design_var_matches[-1])
            except (SyntaxError, ValueError):
                parsed_vars = {}
            if isinstance(parsed_vars, dict):
                final_design_vars = {
                    str(key): str(value) for key, value in parsed_vars.items()
                }

    n_iter = len(re.findall(r"^## Iteration \d+", text, flags=re.MULTILINE))
    return {
        "measurements": measurements,
        "tuning_measurements": tuning_measurements,
        "tuning_pass_fail": tuning_pass_fail,
        "final_design_vars": final_design_vars,
        "converged": converged,
        "writeback_status": writeback_status,
        "n_iter": n_iter or None,
    }


def run_cell(cell: Cell, args: argparse.Namespace, *, dry_run: bool) -> RunRecord:
    ckpt = CHECKPOINTS[cell.model_name]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    EVIDENCE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    stdout_path = EVIDENCE_LOG_DIR / (
        f"run_{cell.model_name}_{cell.variant}_{cell.init}_"
        f"seed{cell.seed}_{ts}.stdout.log"
    )
    cmd = build_run_agent_cmd(cell, args)

    if dry_run:
        return RunRecord(
            cell_key=cell.key,
            model_name=cell.model_name,
            llm=ckpt["llm"],
            model=ckpt["model"],
            variant=cell.variant,
            init=cell.init,
            seed=cell.seed,
            timestamp=ts,
            baseline_vars=initial_vars(args),
            command=cmd,
            outcome="DRY_RUN",
            exit_code=None,
            wall_clock_s=0.0,
            transcript_path=None,
            stdout_path=str(stdout_path),
            converged=None,
            writeback_status=None,
            n_iter=None,
            measurements={},
            tuning_measurements={},
            tuning_pass_fail={},
            final_design_vars={},
            fail_reason=" ".join(cmd),
        )

    if not args.no_reset:
        reset_result = reset_maestro_baseline(args)
        print(f"[reset] {cell.key}: {reset_result}")

    started_at = time.time()
    exit_code: int | None = None
    fail_reason: str | None = None
    try:
        with stdout_path.open("w", encoding="utf-8") as fout:
            proc = subprocess.run(
                cmd,
                cwd=str(PROJECT_ROOT),
                stdout=fout,
                stderr=subprocess.STDOUT,
                timeout=args.timeout,
                check=False,
            )
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        exit_code = None
        fail_reason = f"timeout after {args.timeout}s"
    wall_s = round(time.time() - started_at, 1)

    try:
        raw = stdout_path.read_text(encoding="utf-8", errors="replace")
        stdout_path.write_text(safe_scrub(raw), encoding="utf-8")
    except OSError:
        pass

    transcript = discover_transcript(started_at)
    parsed = parse_stdout(stdout_path) if stdout_path.exists() else {}
    converged = parsed.get("converged")
    outcome = "PASS" if exit_code == 0 and converged is True else "FAIL"
    if fail_reason is None and exit_code not in (0, None):
        fail_reason = f"run_agent exit_code={exit_code}"
    if fail_reason is None and converged is not True:
        fail_reason = "not converged"

    return RunRecord(
        cell_key=cell.key,
        model_name=cell.model_name,
        llm=ckpt["llm"],
        model=ckpt["model"],
        variant=cell.variant,
        init=cell.init,
        seed=cell.seed,
        timestamp=ts,
        baseline_vars=initial_vars(args),
        command=cmd,
        outcome=outcome,
        exit_code=exit_code,
        wall_clock_s=wall_s,
        transcript_path=str(transcript) if transcript else None,
        stdout_path=str(stdout_path) if stdout_path.exists() else None,
        converged=converged,
        writeback_status=parsed.get("writeback_status"),
        n_iter=parsed.get("n_iter"),
        measurements=parsed.get("measurements") or {},
        tuning_measurements=parsed.get("tuning_measurements") or {},
        tuning_pass_fail=parsed.get("tuning_pass_fail") or {},
        final_design_vars=parsed.get("final_design_vars") or {},
        fail_reason=fail_reason,
    )


def append_record(record: RunRecord) -> None:
    STATE_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with STATE_JSONL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def rebuild_summary_csv() -> None:
    records: list[dict[str, Any]] = []
    if STATE_JSONL.exists():
        for line in STATE_JSONL.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    SUMMARY_CSV.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "cell_key", "model_name", "llm", "model", "variant", "seed",
        "init", "timestamp", "outcome", "wall_clock_s", "exit_code", "fail_reason",
        "converged", "writeback_status", "n_iter",
        "f_osc_GHz", "V_diff_pp_V", "V_cm_V", "duty_cycle_pct",
        "amp_hold_ratio", "t_startup_ns", "I_core_uA",
        "tuning_range_GHz", "Kvco_MHz_per_V", "Kvco_linearity", "monotonic",
        "Vctrl", "C", "Ibias", "L", "nfin_cc", "nfin_mirror",
        "nfin_neg", "nfin_tail", "R",
        "transcript_path", "stdout_path",
    ]
    with SUMMARY_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for rec in records:
            row = {k: rec.get(k) for k in fields}
            meas = rec.get("measurements") or {}
            tune = rec.get("tuning_measurements") or {}
            vars_ = rec.get("final_design_vars") or {}
            for k in fields:
                if k in meas:
                    row[k] = meas[k]
                elif k in tune:
                    row[k] = json.dumps(tune[k], ensure_ascii=False)
                elif k in vars_:
                    row[k] = vars_[k]
            writer.writerow(row)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(args.env_file)
    cells = build_cells(args)
    missing = preflight(cells, args)
    if missing and args.execute:
        print("Missing required model/API configuration:", file=sys.stderr)
        for item in missing:
            print(f"  - {item}", file=sys.stderr)
        return 2

    completed = load_completed() if args.resume else set()
    dry_run = not args.execute
    print(f"[lc_vco_evidence] dry_run={dry_run} cells={len(cells)}")
    for cell in cells:
        if args.resume and cell.key in completed:
            print(f"[skip] {cell.key}")
            continue
        print(f"[run] {cell.key}")
        record = run_cell(cell, args, dry_run=dry_run)
        if not dry_run:
            append_record(record)
        print(
            f"[done] {record.cell_key} outcome={record.outcome} "
            f"iters={record.n_iter} writeback={record.writeback_status} "
            f"wall={record.wall_clock_s}s"
        )
    if not dry_run:
        rebuild_summary_csv()
        print(f"[summary] {SUMMARY_CSV}")
        print(f"[state] {STATE_JSONL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
