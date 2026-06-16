#!/usr/bin/env python3
"""Paper-grade model benchmark runner for live Virtuoso-agent circuits.

This runner is intentionally serial: one Maestro session is mutable state, so
each cell resets the setup before launching ``run_agent.py``. Dry-run is the
default. Pass ``--execute`` to run live LLM + ADE/Spectre jobs.
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
from dataclasses import asdict, dataclass, field
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

from dotenv import load_dotenv  # noqa: E402

from src.safe_bridge import SafeBridge, scrub as safe_scrub  # noqa: E402


PAPER_DATA_DIR = PROJECT_ROOT / "paper" / "data"
STATE_JSONL = PAPER_DATA_DIR / "model_benchmark_runs.jsonl"
SUMMARY_CSV = PAPER_DATA_DIR / "model_benchmark_summary.csv"


@dataclass(frozen=True)
class Checkpoint:
    name: str
    llm: str
    model: str
    env_var: str


CHECKPOINTS: dict[str, Checkpoint] = {
    "claude-opus-4-8": Checkpoint(
        "claude-opus-4-8", "claude", "claude-opus-4-8",
        "ANTHROPIC_API_KEY",
    ),
    "claude-sonnet-4-6": Checkpoint(
        "claude-sonnet-4-6", "claude", "claude-sonnet-4-6",
        "ANTHROPIC_API_KEY",
    ),
    "claude-haiku-4-5": Checkpoint(
        "claude-haiku-4-5", "claude", "claude-haiku-4-5-20251001",
        "ANTHROPIC_API_KEY",
    ),
    "gpt-5.5": Checkpoint(
        "gpt-5.5", "openai", "gpt-5.5", "OPENAI_API_KEY",
    ),
    "gpt-5.4-mini": Checkpoint(
        "gpt-5.4-mini", "openai", "gpt-5.4-mini", "OPENAI_API_KEY",
    ),
    "kimi-k2.5": Checkpoint(
        "kimi-k2.5", "kimi", "kimi-k2.5", "KIMI_API_KEY",
    ),
    "minimax-m2.7": Checkpoint(
        "minimax-m2.7", "minimax", "MiniMax-M2.7", "MINIMAX_API_KEY",
    ),
    "minimax-m3": Checkpoint(
        "minimax-m3", "minimax", "MiniMax-M3", "MINIMAX_API_KEY",
    ),
    "mimo-v2.5-pro": Checkpoint(
        "mimo-v2.5-pro", "mimo", "mimo-v2.5-pro", "MIMO_API_KEY",
    ),
    "deepseek-v4-pro": Checkpoint(
        "deepseek-v4-pro", "deepseek", "deepseek-v4-pro", "DEEPSEEK_API_KEY",
    ),
    "deepseek-v4-flash": Checkpoint(
        "deepseek-v4-flash", "deepseek", "deepseek-v4-flash",
        "DEEPSEEK_API_KEY",
    ),
    "gemini-2.5-pro": Checkpoint(
        "gemini-2.5-pro", "gemini", "gemini-2.5-pro", "GOOGLE_API_KEY",
    ),
}

DEFAULT_MODELS = tuple(CHECKPOINTS)


@dataclass(frozen=True)
class Circuit:
    name: str
    project: str
    lib: str
    cell: str
    tb_cell: str
    maestro_test: str
    spec_path: Path
    reset_yaml: Path
    analysis: str
    run_agent_extra: tuple[str, ...] = ()
    fixed_design_vars: tuple[str, ...] = ()
    needs_sweep_root: bool = False

    @property
    def agent_log_dir(self) -> Path:
        return PROJECT_ROOT / "projects" / self.project / "logs" / "agent"

    @property
    def benchmark_log_dir(self) -> Path:
        return PROJECT_ROOT / "projects" / self.project / "logs" / "paper_benchmark"


CIRCUITS: dict[str, Circuit] = {
    "opamp": Circuit(
        name="opamp",
        project="opamp_pll",
        lib="pll",
        cell="opamp",
        tb_cell="opamp_test",
        maestro_test="pll_opamp_test_1",
        spec_path=PROJECT_ROOT / "projects" / "opamp_pll" / "constraints" / "spec.md",
        reset_yaml=PROJECT_ROOT
        / "projects" / "opamp_pll" / "maestro_setup" / "ac_dc_setup_allones.yaml",
        analysis="ac",
        run_agent_extra=("--ignore-llm-maestro-setup",),
        fixed_design_vars=("ac_magnitude", "Vicm", "p_phase", "n_phase"),
    ),
    "lc_vco": Circuit(
        name="lc_vco",
        project="lc_vco_base",
        lib="pll",
        cell="LC_VCO",
        tb_cell="LC_VCO_tb",
        maestro_test="pll_LC_VCO_tb_1",
        spec_path=PROJECT_ROOT
        / "projects" / "lc_vco_base" / "constraints" / "spec.md",
        reset_yaml=PROJECT_ROOT
        / "projects" / "lc_vco_base" / "maestro_setup" / "tran_tuning_allones.yaml",
        analysis="tran",
        run_agent_extra=(
            "--ignore-llm-maestro-setup",
            "--auto-bias-ic",
            "--enable-curve-searcher",
            "--curve-searcher-max-candidates",
            "6",
        ),
        needs_sweep_root=True,
    ),
}


@dataclass(frozen=True)
class Cell:
    circuit: str
    model_name: str
    seed: int = 1
    variant: str = "full"

    @property
    def key(self) -> str:
        return (
            f"{self.circuit}::{self.model_name}::"
            f"{self.variant}::seed{self.seed}"
        )


@dataclass
class RunRecord:
    cell_key: str
    circuit: str
    model_name: str
    llm: str
    model: str
    variant: str
    seed: int
    timestamp: str
    outcome: str
    exit_code: int | None
    wall_clock_s: float
    reset_status: str | None
    reset_report_path: str | None
    stdout_path: str | None
    stderr_path: str | None
    transcript_path: str | None
    command: list[str]
    measurements: dict[str, Any] = field(default_factory=dict)
    tuning_measurements: dict[str, Any] = field(default_factory=dict)
    tuning_pass_fail: dict[str, Any] = field(default_factory=dict)
    final_design_vars: dict[str, Any] = field(default_factory=dict)
    converged: bool | None = None
    abort_reason: str | None = None
    writeback_status: str | None = None
    n_iter: int | None = None
    telemetry: dict[str, Any] = field(default_factory=dict)
    fail_reason: str | None = None


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _rel(path: Path | str | None) -> str | None:
    if path is None:
        return None
    p = Path(path)
    try:
        return p.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(safe_scrub(str(p)))


def _scrub_file(path: Path) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    path.write_text(str(safe_scrub(text)), encoding="utf-8")


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"", "-"}:
        return None
    if value in {"True", "False"}:
        return value == "True"
    if value.startswith("[") or value.startswith("{"):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            pass
    try:
        return float(value)
    except ValueError:
        return value


def parse_run_stdout(stdout_path: Path) -> dict[str, Any]:
    text = stdout_path.read_text(encoding="utf-8", errors="replace")
    measurements: dict[str, Any] = {}
    tuning_measurements: dict[str, Any] = {}
    tuning_pass_fail: dict[str, Any] = {}

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
        if in_final and "--- Tuning curve" in line:
            in_tuning = True
            continue
        if not in_final:
            continue
        m = re.match(r"\s*([A-Za-z0-9_]+):\s*(.*?)\s*$", line)
        if not m:
            continue
        key, raw_value = m.group(1), m.group(2)
        if key in {"converged", "abort_reason", "writeback_status"}:
            continue
        if in_tuning:
            value_text = raw_value
            verdict = None
            if "  " in raw_value:
                value_text, verdict = raw_value.split("  ", 1)
            elif "->" in raw_value:
                value_text, verdict = raw_value.split("->", 1)
            tuning_measurements[key] = _parse_scalar(value_text)
            tuning_pass_fail[key] = verdict.strip() if verdict else None
        else:
            measurements[key] = _parse_scalar(raw_value)

    converged: bool | None = None
    abort_reason: str | None = None
    writeback_status: str | None = None
    m = re.search(r"converged\s*:\s*(True|False)", text)
    if m:
        converged = m.group(1) == "True"
    m = re.search(r"abort_reason\s*:\s*([^\n]+)", text)
    if m:
        abort_reason = m.group(1).strip()
        if abort_reason == "-":
            abort_reason = None
    m = re.search(r"writeback_status\s*:\s*([^\n]+)", text)
    if m:
        writeback_status = m.group(1).strip()

    final_design_vars: dict[str, Any] = {}
    block = re.search(
        r"FINAL CONVERGED VALUES.*?Variable\s+Value\s+-+\s+-+\s*(.*?)Scope:",
        text,
        flags=re.DOTALL,
    )
    if block:
        for row in block.group(1).splitlines():
            parts = row.split()
            if len(parts) >= 2:
                final_design_vars[parts[0]] = parts[1]
    if not final_design_vars:
        matches = re.findall(
            r"(?:design_vars|vars)=?\s*:\s*(\{.*?\})",
            text,
            flags=re.MULTILINE,
        )
        if not matches:
            matches = re.findall(r"vars=(\{.*?\})", text, flags=re.MULTILINE)
        if matches:
            try:
                parsed = ast.literal_eval(matches[-1])
            except (SyntaxError, ValueError):
                parsed = {}
            if isinstance(parsed, dict):
                final_design_vars = {
                    str(k): str(v) for k, v in parsed.items()
                }

    n_iter = len(re.findall(r"^## Iteration \d+", text, flags=re.MULTILINE))
    return {
        "measurements": measurements,
        "tuning_measurements": tuning_measurements,
        "tuning_pass_fail": tuning_pass_fail,
        "final_design_vars": final_design_vars,
        "converged": converged,
        "abort_reason": abort_reason,
        "writeback_status": writeback_status,
        "n_iter": n_iter or None,
    }


def summarize_transcript(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    turns = 0
    telemetry_rows = 0
    finish_reasons: dict[str, int] = {}
    statuses: dict[str, int] = {}
    last: dict[str, Any] | None = None
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("role") == "assistant":
                turns += 1
            telemetry = row.get("llm_telemetry")
            if isinstance(telemetry, dict):
                telemetry_rows += 1
                last = telemetry
                finish = str(telemetry.get("finish_reason") or "unknown")
                status = str(telemetry.get("status") or "unknown")
                finish_reasons[finish] = finish_reasons.get(finish, 0) + 1
                statuses[status] = statuses.get(status, 0) + 1
    out: dict[str, Any] = {
        "assistant_turns": turns,
        "telemetry_rows": telemetry_rows,
        "finish_reasons": finish_reasons,
        "statuses": statuses,
    }
    if last:
        out["last_transport_mode"] = last.get("transport_mode")
        out["last_finish_reason"] = last.get("finish_reason")
        out["last_status"] = last.get("status")
        out["last_duration_s"] = last.get("duration_s")
        out["last_event_count"] = last.get("event_count")
        out["last_visible_chars"] = last.get("visible_chars")
        out["last_reasoning_chars"] = last.get("reasoning_chars")
    return out


def discover_transcript(circuit: Circuit, started_at: float) -> Path | None:
    if not circuit.agent_log_dir.exists():
        return None
    candidates = [
        p for p in circuit.agent_log_dir.glob("transcript_*.jsonl")
        if p.stat().st_mtime >= started_at - 1.0
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def build_reset_cmd(circuit: Circuit, report_path: Path) -> list[str]:
    return [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "configure_maestro_setup.py"),
        "--lib",
        circuit.lib,
        "--cell",
        circuit.cell,
        "--tb-cell",
        circuit.tb_cell,
        "--yaml",
        str(circuit.reset_yaml),
        "--verify",
        "--report-json",
        str(report_path),
    ]


def build_agent_cmd(
    circuit: Circuit,
    ckpt: Checkpoint,
    max_iter: int,
    sweep_results_root: str | None,
) -> list[str]:
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "run_agent.py"),
        "--project",
        circuit.project,
        "--lib",
        circuit.lib,
        "--cell",
        circuit.cell,
        "--tb-cell",
        circuit.tb_cell,
        "--maestro-test",
        circuit.maestro_test,
        "--spec",
        str(circuit.spec_path),
        "--analysis",
        circuit.analysis,
        "--sim-backend",
        "spectre",
        "--llm",
        ckpt.llm,
        "--model",
        ckpt.model,
        "--max-iter",
        str(max_iter),
    ]
    for name in circuit.fixed_design_vars:
        cmd += ["--fixed-design-var", name]
    cmd += list(circuit.run_agent_extra)
    if circuit.needs_sweep_root and sweep_results_root:
        cmd += ["--sweep-results-root", sweep_results_root]
    return cmd


def load_terminal_records(state_path: Path) -> set[str]:
    if not state_path.exists():
        return set()
    out: set[str] = set()
    for line in state_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("outcome") in {"PASS", "FAIL", "RESET_FAIL", "ENV_FAIL", "SKIPPED"}:
            out.add(str(row.get("cell_key")))
    return out


def run_reset(circuit: Circuit, report_path: Path, timeout_s: int) -> tuple[str, str | None]:
    cmd = build_reset_cmd(circuit, report_path)
    # A reset that times out (e.g. the Maestro GUI momentarily blocked by
    # modal dialogs) must fail the CELL, not crash the whole campaign —
    # an uncaught TimeoutExpired here killed a 22-cell run on 2026-06-11.
    # Reset is idempotent, so retry once before giving up.
    last_err: str | None = None
    for attempt in range(2):
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            last_err = (
                f"reset timed out after {timeout_s}s "
                f"(attempt {attempt + 1}/2)"
            )
            print(f"[reset-retry] {circuit.name}: {last_err}", file=sys.stderr)
            continue
        except OSError as exc:
            return "failed", f"reset OSError: {type(exc).__name__}: {exc}"
        break
    else:
        return "failed", last_err
    report_path.parent.mkdir(parents=True, exist_ok=True)
    reset_log = report_path.with_suffix(".reset.log")
    reset_log.write_text(
        str(safe_scrub((proc.stdout or "") + "\n" + (proc.stderr or ""))),
        encoding="utf-8",
    )
    if proc.returncode != 0:
        return "failed", f"reset exit_code={proc.returncode}; log={_rel(reset_log)}"
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            report = {}
        apply = report.get("apply") if isinstance(report, dict) else {}
        verify = report.get("verify") if isinstance(report, dict) else {}
        saved = apply.get("saved") if isinstance(apply, dict) else None
        test_written = apply.get("testScopedWritten") if isinstance(apply, dict) else None
        verify_ok = verify.get("ok") if isinstance(verify, dict) else None
        if saved is False or test_written == 0 or verify_ok is False:
            return (
                "failed",
                f"reset verification failed: saved={saved} "
                f"testScopedWritten={test_written} verify_ok={verify_ok}",
            )
    return "ok", None


def clear_sweep_results_root(sweep_results_root: str) -> dict[str, Any]:
    """Clear stale remote swept-results under a validated Interactive root."""
    from virtuoso_bridge import VirtuosoClient

    bridge = SafeBridge(
        VirtuosoClient.from_env(),
        str(PROJECT_ROOT / "config" / "pdk_map.yaml"),
        remote_skill_dir=os.environ.get("VB_REMOTE_SKILL_DIR"),
    )
    return bridge.clear_sweep_results(sweep_results_root)


def run_cell(
    cell: Cell,
    *,
    max_iter: int,
    timeout_s: int,
    reset_timeout_s: int,
    dry_run: bool,
    no_reset: bool,
    sweep_results_root: str | None,
) -> RunRecord:
    circuit = CIRCUITS[cell.circuit]
    ckpt = CHECKPOINTS[cell.model_name]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    circuit.benchmark_log_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{cell.circuit}_{cell.model_name}_{cell.variant}_seed{cell.seed}_{ts}"
    stdout_path = circuit.benchmark_log_dir / f"{stem}.stdout.log"
    stderr_path = circuit.benchmark_log_dir / f"{stem}.stderr.log"
    reset_report_path = circuit.benchmark_log_dir / f"{stem}.reset.json"
    command = build_agent_cmd(circuit, ckpt, max_iter, sweep_results_root)

    if circuit.needs_sweep_root and not sweep_results_root:
        return RunRecord(
            cell_key=cell.key,
            circuit=cell.circuit,
            model_name=ckpt.name,
            llm=ckpt.llm,
            model=ckpt.model,
            variant=cell.variant,
            seed=cell.seed,
            timestamp=ts,
            outcome="SKIPPED",
            exit_code=None,
            wall_clock_s=0.0,
            reset_status=None,
            reset_report_path=None,
            stdout_path=None,
            stderr_path=None,
            transcript_path=None,
            command=command,
            fail_reason=(
                "LC_VCO tuning benchmark requires --sweep-results-root "
                "or LC_VCO_SWEEP_RESULTS_ROOT"
            ),
        )

    if dry_run:
        return RunRecord(
            cell_key=cell.key,
            circuit=cell.circuit,
            model_name=ckpt.name,
            llm=ckpt.llm,
            model=ckpt.model,
            variant=cell.variant,
            seed=cell.seed,
            timestamp=ts,
            outcome="DRY_RUN",
            exit_code=None,
            wall_clock_s=0.0,
            reset_status=None if no_reset else "planned",
            reset_report_path=_rel(reset_report_path),
            stdout_path=_rel(stdout_path),
            stderr_path=_rel(stderr_path),
            transcript_path=None,
            command=command,
            fail_reason=" ".join(command),
        )

    reset_status: str | None = "skipped"
    reset_reason: str | None = None
    if not no_reset:
        reset_status, reset_reason = run_reset(circuit, reset_report_path, reset_timeout_s)
        if reset_status != "ok":
            return RunRecord(
                cell_key=cell.key,
                circuit=cell.circuit,
                model_name=ckpt.name,
                llm=ckpt.llm,
                model=ckpt.model,
                variant=cell.variant,
                seed=cell.seed,
                timestamp=ts,
                outcome="RESET_FAIL",
                exit_code=None,
                wall_clock_s=0.0,
                reset_status=reset_status,
                reset_report_path=_rel(reset_report_path) if reset_report_path.exists() else None,
                stdout_path=None,
                stderr_path=None,
                transcript_path=None,
                command=command,
                fail_reason=reset_reason,
            )

    if circuit.needs_sweep_root and sweep_results_root:
        try:
            cleanup = clear_sweep_results_root(sweep_results_root)
            archive = cleanup.get("archive") if isinstance(cleanup, dict) else None
            print(
                f"[sweep-clean] {cell.key}: cleared={cleanup.get('cleared') if isinstance(cleanup, dict) else '?'} "
                f"archive={safe_scrub(str(archive or '-'))}"
            )
        except Exception as exc:  # noqa: BLE001 - shared env failure
            return RunRecord(
                cell_key=cell.key,
                circuit=cell.circuit,
                model_name=ckpt.name,
                llm=ckpt.llm,
                model=ckpt.model,
                variant=cell.variant,
                seed=cell.seed,
                timestamp=ts,
                outcome="RESET_FAIL",
                exit_code=None,
                wall_clock_s=0.0,
                reset_status="sweep_cleanup_failed",
                reset_report_path=_rel(reset_report_path) if reset_report_path.exists() else None,
                stdout_path=None,
                stderr_path=None,
                transcript_path=None,
                command=command,
                fail_reason=(
                    "sweep cleanup failed: "
                    f"{safe_scrub(type(exc).__name__ + ': ' + str(exc))}"
                ),
            )

    started_at = time.time()
    exit_code: int | None = None
    fail_reason: str | None = None
    try:
        with stdout_path.open("w", encoding="utf-8") as fout, stderr_path.open(
            "w", encoding="utf-8"
        ) as ferr:
            proc = subprocess.run(
                command,
                cwd=str(PROJECT_ROOT),
                stdout=fout,
                stderr=ferr,
                timeout=timeout_s,
                check=False,
            )
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        fail_reason = f"timeout after {timeout_s}s"
    except OSError as exc:
        fail_reason = f"OSError: {type(exc).__name__}: {safe_scrub(str(exc))}"

    wall_s = round(time.time() - started_at, 1)
    _scrub_file(stdout_path)
    _scrub_file(stderr_path)

    parsed = parse_run_stdout(stdout_path) if stdout_path.exists() else {}
    transcript = discover_transcript(circuit, started_at)
    telemetry = summarize_transcript(transcript)
    converged = parsed.get("converged")
    if exit_code not in (0, None) and fail_reason is None:
        fail_reason = f"run_agent exit_code={exit_code}"
    if fail_reason is None and converged is not True:
        fail_reason = parsed.get("abort_reason") or "not converged"
    outcome = "PASS" if exit_code == 0 and converged is True else "FAIL"
    if exit_code is None and fail_reason and fail_reason.startswith("timeout"):
        outcome = "TIMEOUT"

    return RunRecord(
        cell_key=cell.key,
        circuit=cell.circuit,
        model_name=ckpt.name,
        llm=ckpt.llm,
        model=ckpt.model,
        variant=cell.variant,
        seed=cell.seed,
        timestamp=ts,
        outcome=outcome,
        exit_code=exit_code,
        wall_clock_s=wall_s,
        reset_status=reset_status,
        reset_report_path=_rel(reset_report_path) if reset_report_path.exists() else None,
        stdout_path=_rel(stdout_path) if stdout_path.exists() else None,
        stderr_path=_rel(stderr_path) if stderr_path.exists() else None,
        transcript_path=_rel(transcript) if transcript else None,
        command=command,
        measurements=parsed.get("measurements") or {},
        tuning_measurements=parsed.get("tuning_measurements") or {},
        tuning_pass_fail=parsed.get("tuning_pass_fail") or {},
        final_design_vars=parsed.get("final_design_vars") or {},
        converged=converged,
        abort_reason=parsed.get("abort_reason"),
        writeback_status=parsed.get("writeback_status"),
        n_iter=parsed.get("n_iter"),
        telemetry=telemetry,
        fail_reason=fail_reason,
    )


def append_record(record: RunRecord, state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(record)
    if payload.get("command"):
        # The recorded command can carry absolute local paths and remote
        # roots (usernames) — e.g. a --sweep-results-root argument. Scrub
        # the archived copy; the executed command is unaffected. Without
        # this the state file fails the P0 leak gate.
        payload["command"] = [
            str(safe_scrub(str(part))) for part in payload["command"]
        ]
    with state_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def rebuild_summary_csv(state_path: Path, csv_path: Path) -> None:
    records: list[dict[str, Any]] = []
    if state_path.exists():
        for line in state_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    fields = [
        "cell_key",
        "circuit",
        "model_name",
        "llm",
        "model",
        "variant",
        "seed",
        "timestamp",
        "outcome",
        "fail_reason",
        "wall_clock_s",
        "exit_code",
        "reset_status",
        "converged",
        "abort_reason",
        "writeback_status",
        "n_iter",
        "A0_diff_db",
        "UGB_Hz",
        "f_osc_GHz",
        "V_diff_pp_V",
        "V_cm_V",
        "duty_cycle_pct",
        "amp_hold_ratio",
        "t_startup_ns",
        "I_core_uA",
        "tuning_range_GHz",
        "Kvco_MHz_per_V",
        "Kvco_linearity",
        "monotonic",
        "assistant_turns",
        "telemetry_rows",
        "last_transport_mode",
        "last_finish_reason",
        "last_status",
        "last_duration_s",
        "last_event_count",
        "stdout_path",
        "stderr_path",
        "transcript_path",
        "reset_report_path",
    ]
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for rec in records:
            row = {name: rec.get(name) for name in fields}
            for src_name in ("measurements", "tuning_measurements", "telemetry"):
                src = rec.get(src_name) or {}
                for key, value in src.items():
                    if key in fields:
                        row[key] = (
                            json.dumps(value, ensure_ascii=False)
                            if isinstance(value, (dict, list))
                            else value
                        )
            writer.writerow(row)


def build_cells(circuits: list[str], models: list[str], seed: int) -> list[Cell]:
    return [Cell(circuit=c, model_name=m, seed=seed) for c in circuits for m in models]


def preflight(cells: list[Cell], sweep_results_root: str | None) -> list[str]:
    missing: list[str] = []
    for cell in cells:
        if cell.circuit not in CIRCUITS:
            missing.append(f"unknown circuit: {cell.circuit}")
        if cell.model_name not in CHECKPOINTS:
            missing.append(f"unknown model: {cell.model_name}")
            continue
        env_var = CHECKPOINTS[cell.model_name].env_var
        if not os.environ.get(env_var):
            missing.append(env_var)
    if any(CIRCUITS.get(c.circuit, CIRCUITS["opamp"]).needs_sweep_root for c in cells):
        if not sweep_results_root:
            missing.append("LC_VCO_SWEEP_RESULTS_ROOT or --sweep-results-root")
    return sorted(set(missing))


def llm_endpoint_preflight(
    cells: list[Cell],
    *,
    timeout_s: int,
) -> dict[str, str]:
    """Live one-prompt reachability probe per unique checkpoint.

    Catches dead endpoints and broken client stacks before any Maestro
    reset or Spectre time is spent; previously a dead endpoint cost a
    full reset plus a run_agent launch per cell. Returns
    ``{model_name: scrubbed_error}`` for failed endpoints; their cells
    are recorded as FAIL without being run. Costs one tiny completion
    per healthy model.
    """
    failures: dict[str, str] = {}
    probe_code = (
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(PROJECT_ROOT)!r})\n"
        "from dotenv import load_dotenv\n"
        f"load_dotenv({str(PROJECT_ROOT / 'config' / '.env')!r})\n"
        "from src.llm_client import create_llm_client\n"
        "client = create_llm_client(sys.argv[1], model=sys.argv[2])\n"
        "reply = client.ask('Reply with exactly: OK')\n"
        "if not (reply or '').strip():\n"
        "    raise RuntimeError('empty preflight reply')\n"
        "print('ok')\n"
    )
    for name in dict.fromkeys(cell.model_name for cell in cells):
        ckpt = CHECKPOINTS.get(name)
        if ckpt is None:
            continue
        try:
            subprocess.run(
                [sys.executable, "-c", probe_code, ckpt.llm, ckpt.model],
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                check=True,
            )
        except subprocess.TimeoutExpired:
            failures[name] = f"endpoint probe timed out after {timeout_s}s"
            print(
                f"[preflight] {name}: FAIL {failures[name]}",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001 - any failure means skip
            failures[name] = str(
                safe_scrub(f"{type(exc).__name__}: {exc}")
            )[:200]
            print(
                f"[preflight] {name}: FAIL {failures[name]}",
                file=sys.stderr,
            )
        else:
            print(f"[preflight] {name}: ok")
    return failures


def _endpoint_failure_record(cell: Cell, err: str) -> RunRecord:
    ckpt = CHECKPOINTS[cell.model_name]
    return RunRecord(
        cell_key=cell.key,
        circuit=cell.circuit,
        model_name=ckpt.name,
        llm=ckpt.llm,
        model=ckpt.model,
        variant=cell.variant,
        seed=cell.seed,
        timestamp=time.strftime("%Y%m%d_%H%M%S"),
        outcome="FAIL",
        exit_code=None,
        wall_clock_s=0.0,
        reset_status=None,
        reset_report_path=None,
        stdout_path=None,
        stderr_path=None,
        transcript_path=None,
        command=[],
        converged=False,
        abort_reason="llm_error",
        fail_reason=f"endpoint_preflight: {err}",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--circuits",
        default="opamp,lc_vco",
        help="Comma-separated circuit names: opamp, lc_vco.",
    )
    parser.add_argument(
        "--models",
        default=",".join(DEFAULT_MODELS),
        help=f"Comma-separated model names. Default: {','.join(DEFAULT_MODELS)}",
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--max-iter", type=int, default=15)
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--reset-timeout", type=int, default=180)
    parser.add_argument("--no-reset", action="store_true")
    parser.add_argument(
        "--llm-endpoint-preflight",
        action="store_true",
        help=(
            "Probe each requested LLM endpoint before running cells. Disabled "
            "by default because some providers can stream slowly enough to "
            "look like a hung benchmark."
        ),
    )
    parser.add_argument(
        "--llm-endpoint-timeout",
        type=int,
        default=60,
        help="Per-model timeout in seconds for --llm-endpoint-preflight.",
    )
    parser.add_argument(
        "--continue-on-reset-fail",
        action="store_true",
        help=(
            "Continue remaining cells after a Maestro reset/writeback failure. "
            "Default is fail-fast because reset failures usually mean shared "
            "Virtuoso/Maestro state is unhealthy, not a model-specific result."
        ),
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--env-file", default=str(PROJECT_ROOT / "config" / ".env"))
    parser.add_argument("--state", default=str(STATE_JSONL))
    parser.add_argument("--summary-csv", default=str(SUMMARY_CSV))
    parser.add_argument(
        "--sweep-results-root",
        default=None,
        help="Remote LC_VCO Interactive.<N> root; default from LC_VCO_SWEEP_RESULTS_ROOT.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(args.env_file)
    circuits = _split_csv(args.circuits)
    models = _split_csv(args.models)
    sweep_root = args.sweep_results_root or os.environ.get("LC_VCO_SWEEP_RESULTS_ROOT")
    cells = build_cells(circuits, models, args.seed)
    dry_run = not args.execute

    missing = preflight(cells, sweep_root)
    if missing and not dry_run:
        print("Missing required benchmark configuration:", file=sys.stderr)
        for item in missing:
            print(f"  - {item}", file=sys.stderr)
        return 2

    state_path = Path(args.state)
    completed = load_terminal_records(state_path) if args.resume else set()
    print(f"[paper_benchmark] dry_run={dry_run} cells={len(cells)} max_iter={args.max_iter}")
    if args.resume:
        print(f"[paper_benchmark] resume terminal cells={len(completed)}")

    endpoint_failures: dict[str, str] = {}
    if not dry_run and args.llm_endpoint_preflight:
        pending = [
            c for c in cells
            if not (args.resume and c.key in completed)
        ]
        if pending:
            print("[paper_benchmark] LLM endpoint preflight ...")
            endpoint_failures = llm_endpoint_preflight(
                pending,
                timeout_s=args.llm_endpoint_timeout,
            )

    records: list[RunRecord] = []
    for cell in cells:
        if args.resume and cell.key in completed:
            print(f"[skip] {cell.key}")
            continue
        if cell.model_name in endpoint_failures:
            record = _endpoint_failure_record(
                cell, endpoint_failures[cell.model_name],
            )
            records.append(record)
            if not dry_run:
                append_record(record, state_path)
            print(
                f"[done] {record.cell_key} outcome=FAIL conv=False "
                f"iter=None wall=0.0s reason={record.fail_reason}"
            )
            continue
        print(f"[run] {cell.key}")
        record = run_cell(
            cell,
            max_iter=args.max_iter,
            timeout_s=args.timeout,
            reset_timeout_s=args.reset_timeout,
            dry_run=dry_run,
            no_reset=args.no_reset,
            sweep_results_root=sweep_root,
        )
        records.append(record)
        if not dry_run:
            append_record(record, state_path)
        print(
            f"[done] {record.cell_key} outcome={record.outcome} "
            f"conv={record.converged} iter={record.n_iter} "
            f"wall={record.wall_clock_s}s reason={record.fail_reason or '-'}"
        )
        if (
            not dry_run
            and record.outcome == "RESET_FAIL"
            and not args.continue_on_reset_fail
        ):
            print(
                "[fatal] stopping benchmark after reset/writeback failure; "
                "shared Maestro state may be unhealthy. Re-run with "
                "--continue-on-reset-fail only when you intentionally want "
                "per-cell RESET_FAIL records.",
                file=sys.stderr,
            )
            break

    if not dry_run:
        rebuild_summary_csv(state_path, Path(args.summary_csv))
        print(f"[state] {_rel(state_path)}")
        print(f"[summary] {_rel(Path(args.summary_csv))}")
    else:
        for rec in records:
            print(f"[dry] {rec.cell_key}: {' '.join(rec.command)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
