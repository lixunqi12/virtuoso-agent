#!/usr/bin/env python3
"""Multi-LLM benchmark grid runner for MLCAD 2026 paper §5 (SP2/SP3/SP4).

Scaffold-only: by default this prints the planned execution grid and
exits WITHOUT firing any LLM call. Live execution requires --execute
AND a non-empty .env with the relevant *_API_KEY values populated.

Grid (11 checkpoints x 3 seeds x 1 spec = 33 runs):
    11 LLM checkpoints     -- see CHECKPOINTS below
    3  seed replicates     -- seeds [1, 2, 3]; treated as replicate
                              labels, NOT determinism guarantees (most
                              reasoning-LLM vendor APIs don't honor
                              `seed=` even when documented; we report
                              median+IQR across replicates per §4
                              methodology disclosure).
    1  spec               -- projects/lc_vco_base/constraints/spec.md

Lock-ins (claude_reviewer_v2 D3-pre, approved by Claude Code 2026-05-12;
codex_reviewer_v2 + claude_reviewer_v2 D3 verdict bundled-fix 2026-05-12):
    1. PER-RUN wall-clock ceiling = MAX_ITER * 600s + 60s margin.
       Stuck reasoning blocks killed; bucket = "wall-clock timeout".
       (Per-iter granularity is post-hoc: derived from transcript
       timestamps when bucketing failures for paper §6.)
    2. Seeds = replicate labels. NOT passed to vendor API. See §4
       methodology disclosure block in the paper draft.
    3. Per-run usage JSONL sidecar at `<transcript>.usage.jsonl`.
       Sourced from the embedded `usage` field already present in the
       transcript schema (e750189c-frozen; agent.py:362-372 already
       records `client.last_usage` per assistant turn). Sidecar is
       derived post-run; no agent.py / transcript-schema touch.
    4. Resume state at `paper/data/benchmark_state.json`; granularity
       = (checkpoint_name, seed) cell. Completed cells skipped at
       startup. PASS / FAIL / STUCK are terminal; TIMEOUT / ERROR
       are transient and retried (capped at MAX_TIMEOUT_RETRIES).
    5. (D3-bundle) MAX_TIMEOUT_RETRIES = 3 consecutive TIMEOUT/ERROR
       promotes the cell to STUCK terminal — prevents a deterministically
       broken endpoint from burning the resume budget forever. STUCK
       cells appear in §6 as "infrastructure-bound failure".
    6. (D3-bundle) Pre-flight env-var sanity-check before --execute:
       map each enabled checkpoint to its API_KEY env var (note Gemini
       uses GOOGLE_API_KEY, not GEMINI_API_KEY — see src/llm_client.py:218)
       and fail fast if any required key is empty.
    7. (D3-bundle) Subprocess .stdout is scrubbed through
       src.safe_bridge.scrub() after subprocess return. Defense in depth
       against the e750189c-class PDK leak path: run_agent.py can log
       operational paths (e.g. auto-discovered input.scs at :717-720)
       and safe_bridge has full-response diagnostic logging (:1885-1890)
       before its own return-value scrub. The .stdout file is internal
       debug — DO NOT feed to paper artifacts unscrubbed.

Output file layout:
    projects/lc_vco_base/logs/agent/benchmark/
        run_<ckpt>_seed<N>_<YYYYMMDD_HHMMSS>.jsonl        (transcript)
        run_<ckpt>_seed<N>_<YYYYMMDD_HHMMSS>.usage.jsonl  (sidecar)
        run_<ckpt>_seed<N>_<YYYYMMDD_HHMMSS>.stdout       (subprocess output)
    paper/data/benchmark_state.json                         (resume state)

Usage:
    # default = dry-run (print plan, no execution)
    .venv/Scripts/python.exe scripts/run_benchmark.py

    # filter to one checkpoint or seed
    .venv/Scripts/python.exe scripts/run_benchmark.py \\
        --ckpt deepseek-v4-pro --seed 1

    # actually fire the grid (gated; reviews + key fill required first)
    .venv/Scripts/python.exe scripts/run_benchmark.py --execute
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict, fields
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------- #
# Grid configuration                                                     #
# ---------------------------------------------------------------------- #

# 11 LLM checkpoints. `name` is the filename slug (no spaces/colons).
# `llm` is the factory key (matches src.llm_client.create_llm_client).
# `model` is the vendor model_id string (overrides the env default).
CHECKPOINTS: list[dict[str, str]] = [
    # Anthropic — 3 tiers
    {"name": "claude-opus-4-7",   "llm": "claude",   "model": "claude-opus-4-7"},
    {"name": "claude-sonnet-4-6", "llm": "claude",   "model": "claude-sonnet-4-6"},
    {"name": "claude-haiku-4-5",  "llm": "claude",   "model": "claude-haiku-4-5-20251001"},
    # OpenAI — 2 tiers
    {"name": "gpt-5.5",           "llm": "openai",   "model": "gpt-5.5"},
    {"name": "gpt-5.4-mini",      "llm": "openai",   "model": "gpt-5.4-mini"},
    # China-domestic — 4 tiers (SP4)
    {"name": "kimi-k2.5",         "llm": "kimi",     "model": "kimi-k2.5"},
    {"name": "minimax-m2.7",      "llm": "minimax",  "model": "MiniMax-M2.7"},
    {"name": "mimo-v2.5-pro",     "llm": "mimo",     "model": "mimo-v2.5-pro"},
    {"name": "deepseek-v4-pro",   "llm": "deepseek", "model": "deepseek-v4-pro"},
    {"name": "deepseek-v4-flash", "llm": "deepseek", "model": "deepseek-v4-flash"},
    # Google — flagship (vendor docs at ai.google.dev confirm canonical
    # API model name `gemini-2.5-pro`; flash variant available as
    # `gemini-2.5-flash` if a future Pareto sweep wants the cheaper tier)
    {"name": "gemini-2.5-pro",    "llm": "gemini",   "model": "gemini-2.5-pro"},
]

SEEDS: list[int] = [1, 2, 3]
MAX_ITER: int = 10
PER_ITER_TIMEOUT_S: int = 600
PER_RUN_TIMEOUT_S: int = MAX_ITER * PER_ITER_TIMEOUT_S + 60  # 6060s = 101 min

# After this many consecutive TIMEOUT/ERROR outcomes the cell is promoted
# to STUCK (terminal). Prevents a deterministically broken endpoint from
# burning the resume-loop wall-clock budget forever. claude_reviewer_v2
# D3 verdict P1 fix.
MAX_TIMEOUT_RETRIES: int = 3

# Map each LLM factory key to the env var its client actually reads
# (src/llm_client.py). NOTE Gemini uses GOOGLE_API_KEY, not the
# intuitive GEMINI_API_KEY (`src/llm_client.py:218` — genai.configure
# pulls `os.environ["GOOGLE_API_KEY"]`).
ENV_VAR_BY_LLM: dict[str, str] = {
    "claude":   "ANTHROPIC_API_KEY",
    "gemini":   "GOOGLE_API_KEY",
    "kimi":     "KIMI_API_KEY",
    "minimax":  "MINIMAX_API_KEY",
    "openai":   "OPENAI_API_KEY",
    "mimo":     "MIMO_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

# Spec is the same across all cells — LC_VCO_base is the single test bed
# locked for MLCAD 2026 per Claude Code's D1 ruling.
SPEC_PATH = PROJECT_ROOT / "projects" / "lc_vco_base" / "constraints" / "spec.md"
SIM_LIB = "pll"
SIM_CELL = "LC_VCO"
SIM_TB_CELL = "LC_VCO_tb"

BENCHMARK_LOG_DIR = (
    PROJECT_ROOT / "projects" / "lc_vco_base" / "logs" / "agent" / "benchmark"
)
STATE_PATH = PROJECT_ROOT / "paper" / "data" / "benchmark_state.json"


# ---------------------------------------------------------------------- #
# Cell + state                                                           #
# ---------------------------------------------------------------------- #

@dataclass(frozen=True)
class Cell:
    """One (checkpoint, seed) grid cell — the resume-granularity unit."""
    ckpt_name: str
    seed: int

    @property
    def key(self) -> str:
        return f"{self.ckpt_name}::seed{self.seed}"


@dataclass
class CellResult:
    """Outcome of running one cell."""
    cell_key: str
    ckpt_name: str
    seed: int
    timestamp: str
    transcript_path: str | None
    usage_sidecar_path: str | None
    stdout_path: str | None
    outcome: str  # PASS | FAIL | TIMEOUT | ERROR | DRY_RUN | STUCK
    wall_clock_s: float
    exit_code: int | None
    fail_reason: str | None
    # Number of consecutive TIMEOUT/ERROR attempts including this one.
    # 0 for fresh / PASS / FAIL / DRY_RUN. Promoted to STUCK once it
    # hits MAX_TIMEOUT_RETRIES — see `run_grid`.
    timeout_retry_count: int = 0


def load_state(state_path: Path) -> dict[str, CellResult]:
    """Return {cell.key: CellResult} from disk; empty dict if no state.

    Tolerates schema drift: keys in the persisted JSON that aren't fields
    on the current CellResult are dropped silently so an older state file
    still loads (defaults fill in any missing new fields).
    """
    if not state_path.exists():
        return {}
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    known = {f.name for f in fields(CellResult)}
    return {
        item["cell_key"]: CellResult(
            **{k: v for k, v in item.items() if k in known}
        )
        for item in raw.get("completed", [])
    }


def save_state(state_path: Path, completed: dict[str, CellResult]) -> None:
    """Atomic-ish write of state; sorted for diff-friendliness."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "completed": [
            asdict(r) for r in sorted(
                completed.values(), key=lambda c: c.cell_key,
            )
        ],
    }
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    tmp.replace(state_path)


# ---------------------------------------------------------------------- #
# Cell execution                                                         #
# ---------------------------------------------------------------------- #

def cell_transcript_path(cell: Cell, ts: str) -> Path:
    """Per-spec layout: run_<ckpt>_seed<N>_<YYYYMMDD_HHMMSS>.jsonl."""
    return BENCHMARK_LOG_DIR / (
        f"run_{cell.ckpt_name}_seed{cell.seed}_{ts}.jsonl"
    )


def build_subprocess_cmd(
    cell: Cell,
    ckpt: dict[str, str],
    transcript_path: Path,
    max_iter: int,
) -> list[str]:
    """Construct the run_agent.py argv for one cell.

    NOTE: run_agent.py currently picks its own transcript_path from
    project + timestamp. The benchmark runner can't directly pin the
    filename via CLI; it relies on the post-run discovery step in
    `discover_run_artifacts` to map the cell back to whichever
    transcript_<ts>.jsonl run_agent.py actually wrote. The unused
    `transcript_path` arg here is the intended target name — it
    becomes the post-rename destination so the benchmark layout is
    deterministic regardless of run_agent.py's internal timestamping.
    """
    python = sys.executable
    return [
        python,
        str(PROJECT_ROOT / "scripts" / "run_agent.py"),
        "--project", "lc_vco_base",
        "--lib", SIM_LIB,
        "--cell", SIM_CELL,
        "--tb-cell", SIM_TB_CELL,
        "--spec", str(SPEC_PATH),
        "--max-iter", str(max_iter),
        "--sim-backend", "spectre",
        "--llm", ckpt["llm"],
        "--model", ckpt["model"],
    ]


def emit_usage_sidecar(
    transcript_path: Path,
    sidecar_path: Path,
) -> int:
    """Derive the per-iter usage sidecar from the transcript.

    Reads `transcript_path` (JSONL: one entry per LLM/user turn) and
    writes `sidecar_path` (JSONL: one entry per assistant turn that
    carried usage info). Source of truth = `entry["usage"]` populated
    by `src.agent._append_transcript` from `client.last_usage`.

    Returns the count of sidecar rows written (= # assistant turns
    that had non-None usage).

    Schema mirrors the embedded usage dict so downstream
    `paper/scripts/extract_transcript_logs.py` reads either side
    interchangeably:
        {iteration, timestamp, prompt_tokens, completion_tokens,
         reasoning_tokens, total_tokens, provider, model}
    """
    if not transcript_path.exists():
        return 0
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    n_written = 0
    with transcript_path.open("r", encoding="utf-8") as fin, \
            sidecar_path.open("w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("role") != "assistant":
                continue
            usage = entry.get("usage")
            if not isinstance(usage, dict):
                continue
            row = {
                "iteration": entry.get("iteration"),
                "timestamp": entry.get("timestamp"),
                **usage,
            }
            fout.write(json.dumps(row, ensure_ascii=False) + "\n")
            n_written += 1
    return n_written


def discover_run_artifacts(
    log_dir: Path,
    started_after: float,
) -> Path | None:
    """Return the path of the run_agent.py transcript written this run.

    run_agent.py writes `transcript_<YYYYMMDD_HHMMSS>.jsonl` in the
    project's agent log dir, NOT in the benchmark subdir. We pick the
    newest transcript whose mtime is >= started_after (the wall-clock
    epoch when we kicked off the subprocess). Returns None if no
    matching file appeared.

    Mtime heuristic is SERIAL-ONLY. codex_reviewer_v2 D3 P1: if a
    parallel run (or manual run_agent.py invocation) creates another
    transcript after `started_after`, this could misattribute. Before
    parallelizing the grid (D5+ if needed), plumb a real --transcript-path
    flag through run_agent.py or parse the exact path from subprocess
    output. For the D4 serial fire, the heuristic is sufficient.
    """
    if not log_dir.exists():
        return None
    candidates = [
        p for p in log_dir.glob("transcript_*.jsonl")
        if p.stat().st_mtime >= started_after - 1.0  # 1s clock-skew slack
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def scrub_stdout_file(stdout_path: Path) -> bool:
    """Post-process the subprocess .stdout file through safe_bridge.scrub.

    Defense in depth against the e750189c-class PDK leak: run_agent.py
    can log operational paths (e.g. auto-discovered input.scs) and
    safe_bridge has full-response diagnostic logging before its own
    return-value scrub. None of those are scrubbed in stdout. Returns
    True on success. On failure, deletes the stdout file rather than
    retaining an unscrubbed copy (fail-closed).
    """
    if not stdout_path.exists():
        return False
    try:
        from src.safe_bridge import scrub as _scrub
        content = stdout_path.read_text(encoding="utf-8", errors="replace")
        stdout_path.write_text(_scrub(content), encoding="utf-8")
        return True
    except Exception:
        # Fail-closed: better to lose debug visibility than leak PDK
        # tokens into a retained artifact.
        try:
            stdout_path.unlink()
        except OSError:
            pass
        return False


def preflight_env_check(
    cells: list[tuple[Cell, dict[str, str]]],
) -> list[str]:
    """Return the list of API_KEY env vars that are missing/empty for
    the enabled grid. Loads config/.env first so dotenv-managed keys are
    visible. Empty list = OK to proceed.
    """
    env_file = PROJECT_ROOT / "config" / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file)
        except ImportError:
            pass

    needed_llms = {ckpt["llm"] for _, ckpt in cells}
    missing: list[str] = []
    for llm in sorted(needed_llms):
        env_var = ENV_VAR_BY_LLM.get(llm)
        if env_var is None:
            missing.append(f"<unknown llm: {llm}>")
            continue
        if not os.environ.get(env_var):
            missing.append(env_var)
    return missing


_FAIL_PATTERN_RE = re.compile(
    r"\b(AuthenticationError|RateLimitError|APIConnectionError"
    r"|APITimeoutError|ConnectionError|BadRequestError"
    r"|PermissionDeniedError|InternalServerError)"
    r"\b[^\n]{0,200}"
)


def classify_subprocess_failure(stdout_path: Path) -> str | None:
    """Best-effort: scan the tail of a non-zero-exit subprocess's
    merged stdout/stderr for known LLM-client failure classes (auth,
    quota, connectivity) so the operator gets a one-line hint in
    ``CellResult.fail_reason`` instead of an empty string. Returns
    ``None`` if no pattern matches; caller may leave fail_reason unset.
    """
    if not stdout_path.exists():
        return None
    try:
        with stdout_path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 8192))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    m = _FAIL_PATTERN_RE.search(tail)
    if not m:
        return None
    return m.group(0).strip().splitlines()[0][:160]


def execute_cell(
    cell: Cell,
    ckpt: dict[str, str],
    max_iter: int,
    timeout_s: int,
    dry_run: bool,
) -> CellResult:
    """Run one (ckpt, seed) cell and return a CellResult.

    On dry_run: returns a DRY_RUN marker without subprocess invocation.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_transcript = cell_transcript_path(cell, ts)
    target_sidecar = target_transcript.with_suffix(".usage.jsonl")
    target_stdout = target_transcript.with_suffix(".stdout")

    cmd = build_subprocess_cmd(cell, ckpt, target_transcript, max_iter)

    if dry_run:
        return CellResult(
            cell_key=cell.key,
            ckpt_name=cell.ckpt_name,
            seed=cell.seed,
            timestamp=ts,
            transcript_path=str(target_transcript),
            usage_sidecar_path=str(target_sidecar),
            stdout_path=str(target_stdout),
            outcome="DRY_RUN",
            wall_clock_s=0.0,
            exit_code=None,
            fail_reason=" ".join(cmd),
        )

    BENCHMARK_LOG_DIR.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    outcome = "ERROR"
    exit_code: int | None = None
    fail_reason: str | None = None
    actual_transcript: Path | None = None

    try:
        with target_stdout.open("w", encoding="utf-8") as fh:
            result = subprocess.run(
                cmd,
                cwd=str(PROJECT_ROOT),
                stdout=fh,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
                check=False,
            )
        exit_code = result.returncode
        outcome = "PASS" if exit_code == 0 else "FAIL"
    except subprocess.TimeoutExpired as exc:
        outcome = "TIMEOUT"
        fail_reason = (
            f"wall-clock timeout at {timeout_s}s "
            f"(per-iter ceiling {PER_ITER_TIMEOUT_S}s x max_iter {max_iter})"
        )
        exit_code = None
        _ = exc
    except OSError as exc:
        outcome = "ERROR"
        fail_reason = f"OSError: {type(exc).__name__}: {exc}"

    wall_clock = time.time() - started_at

    # Locate the transcript that run_agent.py actually wrote and rename
    # into the benchmark layout. Skip on failure so we don't bury an
    # error case under a misleading "we got a transcript" log line.
    agent_log_dir = (
        PROJECT_ROOT / "projects" / "lc_vco_base" / "logs" / "agent"
    )
    actual_transcript = discover_run_artifacts(agent_log_dir, started_at)
    if actual_transcript is not None:
        try:
            actual_transcript.rename(target_transcript)
            actual_transcript = target_transcript
        except OSError:
            # If rename fails (cross-device, locked file), leave the
            # original in place and point the state file at it.
            target_transcript = actual_transcript

    n_usage_rows = 0
    if actual_transcript is not None and actual_transcript.exists():
        n_usage_rows = emit_usage_sidecar(actual_transcript, target_sidecar)

    # Scrub retained .stdout through safe_bridge before letting it live
    # on disk. codex_reviewer_v2 D3 P0/P1: stdout is not a scrubbed
    # boundary; operational paths and safe_bridge diagnostic logging
    # can land here unredacted.
    scrub_stdout_file(target_stdout)

    if outcome == "FAIL" and fail_reason is None:
        # Best-effort classification of the subprocess's exit so an
        # operator skimming state.json sees "AuthenticationError: ..."
        # instead of an empty reason — the most common cause of a fast
        # FAIL (HTTP 401/403/429 from the LLM endpoint).
        fail_reason = classify_subprocess_failure(target_stdout)

    if outcome == "PASS" and n_usage_rows == 0:
        # Subprocess returned 0 but no usage was recorded — likely a
        # path where the agent never reached the LLM call (e.g. spec
        # validation aborted). Demote to FAIL so the bucketing in §6
        # doesn't double-count this as a real convergence.
        outcome = "FAIL"
        fail_reason = "PASS exit but zero LLM-usage rows in transcript"

    return CellResult(
        cell_key=cell.key,
        ckpt_name=cell.ckpt_name,
        seed=cell.seed,
        timestamp=ts,
        transcript_path=(
            str(actual_transcript) if actual_transcript else None
        ),
        usage_sidecar_path=(
            str(target_sidecar) if n_usage_rows > 0 else None
        ),
        stdout_path=str(target_stdout) if target_stdout.exists() else None,
        outcome=outcome,
        wall_clock_s=round(wall_clock, 1),
        exit_code=exit_code,
        fail_reason=fail_reason,
    )


# ---------------------------------------------------------------------- #
# Grid orchestration                                                     #
# ---------------------------------------------------------------------- #

def enumerate_cells(
    checkpoints: list[dict[str, str]],
    seeds: list[int],
    ckpt_filter: str | None,
    seed_filter: int | None,
) -> list[tuple[Cell, dict[str, str]]]:
    """Return ordered list of (Cell, checkpoint_config) tuples to run."""
    out: list[tuple[Cell, dict[str, str]]] = []
    for ckpt in checkpoints:
        if ckpt_filter is not None and ckpt["name"] != ckpt_filter:
            continue
        for seed in seeds:
            if seed_filter is not None and seed != seed_filter:
                continue
            out.append((Cell(ckpt_name=ckpt["name"], seed=seed), ckpt))
    return out


def run_grid(
    checkpoints: list[dict[str, str]],
    seeds: list[int],
    state_path: Path,
    max_iter: int,
    timeout_s: int,
    dry_run: bool,
    resume: bool,
    ckpt_filter: str | None,
    seed_filter: int | None,
) -> dict[str, Any]:
    """Drive the full grid; return summary stats."""
    cells = enumerate_cells(checkpoints, seeds, ckpt_filter, seed_filter)
    completed = load_state(state_path) if resume else {}

    skipped = 0
    fresh: list[CellResult] = []
    for cell, ckpt in cells:
        prior: CellResult | None = None
        if resume and cell.key in completed:
            prior = completed[cell.key]
            if prior.outcome in ("PASS", "FAIL", "STUCK"):
                # PASS / FAIL / STUCK are terminal — don't replay.
                # PASS  = real outcome we want in §6 stats.
                # FAIL  = real non-convergence (also a §6 datapoint).
                # STUCK = infrastructure-bound (was retried MAX times).
                # TIMEOUT and ERROR are NOT terminal: a transient infra
                # blip shouldn't lock the cell out forever, so they get
                # retried (capped at MAX_TIMEOUT_RETRIES below).
                skipped += 1
                continue
        result = execute_cell(cell, ckpt, max_iter, timeout_s, dry_run)

        # STUCK promotion: if the prior attempt was also TIMEOUT/ERROR
        # and this one is too, bump the consecutive-failure counter. If
        # we hit MAX_TIMEOUT_RETRIES, promote the outcome to STUCK so
        # the next resume pass skips it terminally.
        if (
            prior is not None
            and prior.outcome in ("TIMEOUT", "ERROR")
            and result.outcome in ("TIMEOUT", "ERROR")
        ):
            result.timeout_retry_count = prior.timeout_retry_count + 1
            if result.timeout_retry_count >= MAX_TIMEOUT_RETRIES:
                result.outcome = "STUCK"
                stuck_msg = (
                    f"promoted to STUCK after {result.timeout_retry_count} "
                    f"consecutive TIMEOUT/ERROR attempts"
                )
                result.fail_reason = (
                    f"{result.fail_reason} | {stuck_msg}"
                    if result.fail_reason else stuck_msg
                )

        fresh.append(result)
        completed[cell.key] = result
        if not dry_run:
            save_state(state_path, completed)

    return {
        "total_cells": len(cells),
        "skipped_resumed": skipped,
        "executed_this_run": len(fresh),
        "outcomes": _tally(fresh),
        "state_path": str(state_path),
    }


def _tally(results: list[CellResult]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in results:
        out[r.outcome] = out.get(r.outcome, 0) + 1
    return out


# ---------------------------------------------------------------------- #
# CLI                                                                    #
# ---------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Multi-LLM benchmark grid runner (MLCAD 2026 §5)"
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Actually invoke the subprocesses. Default is dry-run: print "
            "the grid plan and exit without firing any LLM call. Gated "
            "behind this flag so a stray invocation can't burn API "
            "credits — required ONLY after dual review APPROVE and "
            "Claude Code's D4 go-signal."
        ),
    )
    p.add_argument(
        "--ckpt",
        default=None,
        help=(
            "Filter: only run cells whose checkpoint name matches. "
            "Use the `name` field from CHECKPOINTS (e.g. 'deepseek-v4-pro')."
        ),
    )
    p.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Filter: only run cells with this seed replicate label.",
    )
    p.add_argument(
        "--max-iter",
        type=int,
        default=MAX_ITER,
        help=f"Max iterations per cell (default: {MAX_ITER}).",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=PER_RUN_TIMEOUT_S,
        help=(
            f"Per-cell wall-clock ceiling in seconds "
            f"(default: {PER_RUN_TIMEOUT_S}s = max-iter x per-iter 600s "
            f"+ 60s margin)."
        ),
    )
    p.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help=(
            "Disable resume; replay every cell. Default is resume ON "
            "(skip cells with terminal outcome PASS/FAIL in state file). "
            "TIMEOUT/ERROR cells are always retried — they may be "
            "transient infra blips."
        ),
    )
    p.set_defaults(resume=True)
    p.add_argument(
        "--state",
        default=str(STATE_PATH),
        help=f"Resume state JSON path (default: {STATE_PATH}).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dry_run = not args.execute

    if dry_run:
        print(
            "[run_benchmark] DRY-RUN mode — listing planned cells only.\n"
            "[run_benchmark] Pass --execute to actually fire subprocesses.\n"
        )
    else:
        # Pre-flight env-var check before burning any API credits.
        cells_for_check = enumerate_cells(
            CHECKPOINTS, SEEDS, args.ckpt, args.seed,
        )
        missing = preflight_env_check(cells_for_check)
        if missing:
            print(
                "[run_benchmark] FATAL: missing API_KEY env vars for the "
                "enabled grid:",
                file=sys.stderr,
            )
            for var in missing:
                print(f"  - {var}", file=sys.stderr)
            print(
                "[run_benchmark] Populate them in config/.env and re-run. "
                "(Note: Gemini reads GOOGLE_API_KEY, not GEMINI_API_KEY.)",
                file=sys.stderr,
            )
            return 2

    summary = run_grid(
        checkpoints=CHECKPOINTS,
        seeds=SEEDS,
        state_path=Path(args.state),
        max_iter=args.max_iter,
        timeout_s=args.timeout,
        dry_run=dry_run,
        resume=args.resume,
        ckpt_filter=args.ckpt,
        seed_filter=args.seed,
    )

    print()
    print("=" * 60)
    print(" BENCHMARK SUMMARY ")
    print("=" * 60)
    for key, value in summary.items():
        print(f"  {key:24s} : {value}")
    print()
    if dry_run:
        print(
            "Re-run with --execute to fire the grid for real "
            "(requires populated API keys in config/.env)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
