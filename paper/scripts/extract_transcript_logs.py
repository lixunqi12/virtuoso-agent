"""Mine virtuoso-agent project transcripts -> per-iteration CSV for paper cost analysis.

Inputs:
  - projects/{lc_vco_base,cobi_delay,cobi_matching}/logs/**/*.jsonl
  - projects/*/logs/agent/benchmark/*.jsonl  (MLCAD 2026 grid sweeps)
  - projects/*/logs/agent/run_*.log  (for LLM model lookup; optional)
  - <transcript>.usage.jsonl  (sidecar emitted by scripts/run_benchmark.py;
                               fallback when transcript lacks usage)

Outputs:
  - paper/data/extracted_logs.csv

Schema per row (one row per LLM iteration that produced an accepted design):
  project, run_id, timestamp, llm_model, iter_index,
  prompt_tokens, completion_tokens, reasoning_tokens, total_tokens,
  sim_count_this_iter, sim_count_cumulative, spec_pass_flag, fail_reason_if_any

D3 (2026-05-12): tokens are now populated from the `usage` field that
src.agent._append_transcript started recording per assistant turn (the
field exists for new runs; legacy transcripts without it still get
blank tokens). Provider fallback URL patterns extended to cover
DeepSeek / OpenAI / MiMo / Gemini for the MLCAD 2026 benchmark sweep.
See extraction_notes.md for the original empty-tokens rationale.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re
from pathlib import Path

ROOT = Path(os.environ.get(
    "VA_REPO_ROOT",
    Path(__file__).resolve().parents[2],
)).resolve()
PROJECTS = ["lc_vco_base", "cobi_delay", "cobi_matching"]
OUT_CSV = ROOT / "paper" / "data" / "extracted_logs.csv"

# ---- transcript discovery -------------------------------------------------

def find_transcripts():
    """Return list of (project, transcript_path) tuples."""
    out = []
    for proj in PROJECTS:
        for pat in (
            f"projects/{proj}/logs/agent/transcript_*.jsonl",
            # MLCAD 2026 grid sweeps land in a benchmark/ subdir with
            # filenames like run_<ckpt>_seed<N>_<ts>.jsonl. Picked up
            # so the cost-quality analysis in §5 can be regenerated
            # from a single extractor invocation.
            f"projects/{proj}/logs/agent/benchmark/run_*.jsonl",
            f"projects/{proj}/logs/hspice/hspice_transcript_*.jsonl",
        ):
            for p in sorted(glob.glob(str(ROOT / pat))):
                # Skip sidecars (the .usage.jsonl files live next to
                # the transcripts and would otherwise double-count).
                if p.endswith(".usage.jsonl"):
                    continue
                out.append((proj, Path(p)))
    return out


def run_id_for(path: Path) -> str:
    """transcript filename stem -> 'YYYYMMDD_HHMMSS' run id."""
    m = re.search(r"(\d{8}_\d{6})", path.name)
    return m.group(1) if m else path.stem


# ---- llm_model lookup from run_*.log -------------------------------------

MODEL_LOG_PATTERN = re.compile(r"'model':\s*'([^']+)'")


def _read_log_head(log_path: Path, max_lines: int = 200) -> str:
    """Read up to N lines of a log file with permissive decoding."""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            chunks = []
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                chunks.append(line)
            return "".join(chunks)
    except OSError:
        return ""


def build_model_index() -> dict:
    """Map run_id -> model string by inspecting projects/*/logs/agent/run_*.log."""
    idx = {}
    for proj in PROJECTS:
        log_glob = ROOT / "projects" / proj / "logs" / "agent" / "run_*.log"
        for logp in glob.glob(str(log_glob)):
            logp = Path(logp)
            run_id = run_id_for(logp)
            head = _read_log_head(logp)
            m = MODEL_LOG_PATTERN.search(head)
            if m:
                idx[run_id] = m.group(1)
                continue
            # Fallback: hostname inference from httpx URL. D3 (2026-05-12):
            # extended to cover the four providers added for MLCAD 2026
            # grid (OpenAI / MiMo / DeepSeek / Gemini). Ordered most-
            # specific first to avoid e.g. an "api.openai.com" reverse
            # proxy used by a third-party endpoint being mis-attributed.
            if "api.minimaxi.com" in head:
                idx[run_id] = "MiniMax (host=api.minimaxi.com, exact ver unknown)"
            elif "api.anthropic.com" in head:
                idx[run_id] = "Claude (host=api.anthropic.com, exact ver unknown)"
            elif "api.moonshot" in head or "kimi" in head.lower():
                idx[run_id] = "Kimi (host=moonshot, exact ver unknown)"
            elif "api.deepseek.com" in head:
                idx[run_id] = "DeepSeek (host=api.deepseek.com, exact ver unknown)"
            elif "xiaomimimo.com" in head:
                # Match BOTH the legacy ``api.xiaomimimo.com`` baked into
                # D4 round-1 transcripts AND the corrected token-plan
                # host ``token-plan-sgp.xiaomimimo.com`` used from
                # 2026-05-13 onward. Vendor stays MiMo either way.
                idx[run_id] = "MiMo (host=xiaomimimo.com, exact ver unknown)"
            elif "generativelanguage.googleapis.com" in head:
                idx[run_id] = "Gemini (host=generativelanguage, exact ver unknown)"
            elif "api.openai.com" in head:
                idx[run_id] = "OpenAI (host=api.openai.com, exact ver unknown)"
    return idx


# Map a transcript run_id to the closest matching agent log run_id. The
# agent log timestamp is typically 1s earlier than the transcript (the
# log boots, then opens the transcript file). We accept a +/- 5 second
# match window on the HHMMSS portion of the same date.
def _ts_to_seconds(run_id: str) -> int:
    # 'YYYYMMDD_HHMMSS' -> epoch-ish seconds (relative within a day)
    try:
        date, hms = run_id.split("_")
        h, m, s = int(hms[:2]), int(hms[2:4]), int(hms[4:6])
        return int(date) * 86400 + h * 3600 + m * 60 + s
    except Exception:
        return -1


def lookup_model(transcript_run_id: str, model_idx: dict) -> str:
    if transcript_run_id in model_idx:
        return model_idx[transcript_run_id]
    t_secs = _ts_to_seconds(transcript_run_id)
    if t_secs < 0:
        return ""
    best = None
    best_dt = 999
    for log_run_id, model in model_idx.items():
        l_secs = _ts_to_seconds(log_run_id)
        if l_secs < 0:
            continue
        dt = abs(t_secs - l_secs)
        if dt < best_dt and dt <= 5:
            best_dt = dt
            best = model
    return best or ""


# ---- per-iteration parsing -----------------------------------------------

REPAIR_HEAD = "Your previous response violated HARD CONSTRAINTS"
METRICS_HDR = re.compile(r"^##+\s*Metrics\s*$", re.MULTILINE)
HSPICE_RESULTS_HDR = re.compile(r"^## Iteration \d+ HSpice results", re.MULTILINE)

# Metric line: "- metric_name: <value> <STATUS ...>" OR "- metric_name: STATUS ..."
# STATUS is PASS, FAIL (...), or UNMEASURABLE (...).
METRIC_LINE = re.compile(
    r"^- (\S+): (?:[-+0-9.eE]+\s+)?(PASS|FAIL|UNMEASURABLE)([^\n]*)$",
    re.MULTILINE,
)


def parse_metrics_block(content: str):
    """Return (spec_pass_flag, fail_reasons) from a user-message 'Metrics' block.

    spec_pass_flag: True if every metric PASSes; False if any FAIL; None if
    UNMEASURABLE-only or no metrics line found.
    fail_reasons: '; '-joined "<metric>: FAIL(...)" snippets, or '' for pass.
    """
    # Slice to the metrics section if present
    m = METRICS_HDR.search(content)
    if m:
        section = content[m.end(): m.end() + 4000]
    else:
        # Fall back to scanning whole content
        section = content

    matches = METRIC_LINE.findall(section)
    if not matches:
        return None, ""
    fails = []
    has_fail = False
    has_unmeas = False
    has_pass = False
    for name, status, tail in matches:
        if status == "PASS":
            has_pass = True
        elif status == "FAIL":
            has_fail = True
            fails.append(f"{name}:FAIL{tail.strip()}")
        elif status == "UNMEASURABLE":
            has_unmeas = True
            fails.append(f"{name}:UNMEASURABLE")
    if has_fail:
        return False, "; ".join(fails)
    if has_pass and not has_unmeas:
        return True, ""
    return None, "; ".join(fails)


def is_results_user(content: str) -> bool:
    return bool(METRICS_HDR.search(content) or HSPICE_RESULTS_HDR.search(content))


def is_repair_user(content: str) -> bool:
    return content.startswith(REPAIR_HEAD)


# ---- usage extraction (D3 addition) --------------------------------------

def _usage_from_entry(entry: dict) -> dict:
    """Pull token counts from a transcript entry's embedded `usage` block.

    Returns a dict with prompt/completion/reasoning/total keys, all
    either int or "" (the latter when the field is missing or None).
    The CSV writer expects strings/numbers; coerce None -> "" here so
    downstream `csv.DictWriter` doesn't emit the literal "None".
    """
    usage = entry.get("usage")
    if not isinstance(usage, dict):
        return {
            "prompt_tokens": "",
            "completion_tokens": "",
            "reasoning_tokens": "",
            "total_tokens": "",
        }
    return {
        k: ("" if usage.get(k) is None else usage.get(k))
        for k in (
            "prompt_tokens", "completion_tokens",
            "reasoning_tokens", "total_tokens",
        )
    }


def _load_usage_sidecar(transcript_path: Path) -> dict:
    """Return {iteration: usage_row} parsed from a `.usage.jsonl` sidecar.

    Used as a fallback when the transcript itself lacks embedded usage
    (legacy runs predating the agent.py rev that wired client.last_usage
    into _append_transcript). Empty dict if no sidecar exists.
    """
    sidecar = transcript_path.with_suffix(".usage.jsonl")
    if not sidecar.exists():
        return {}
    out: dict = {}
    try:
        with open(sidecar, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                it = obj.get("iteration")
                if it is None:
                    continue
                out[it] = {
                    k: ("" if obj.get(k) is None else obj.get(k))
                    for k in (
                        "prompt_tokens", "completion_tokens",
                        "reasoning_tokens", "total_tokens",
                    )
                }
    except OSError:
        return {}
    return out


# ---- transcript -> rows --------------------------------------------------

def extract_rows(project: str, path: Path, model_idx: dict):
    """Yield dict rows for one transcript file.

    Convention:
        Row "iter_index = K" represents the LLM's accepted assistant turn
        at iteration K (the design_vars it proposed). The simulation
        result associated with that design_vars appears in the NEXT
        user message that contains a "## Metrics" / "## Iteration N
        HSpice results" block (typically the iter K+1 user message).
        The final iteration has no following result block -> sim_count
        is 0 and spec_pass_flag is blank.
    """
    rid = run_id_for(path)
    model = lookup_model(rid, model_idx)

    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            entries.append(obj)

    # Walk in order. Each accepted assistant turn at iter K is the LAST
    # assistant entry with iteration == K (repair retries are earlier
    # assistant turns at the same iter that were rejected).
    # The "next results user message" is the FIRST subsequent user entry
    # whose content begins with "## Iteration" or contains "## Metrics".

    accepted_assist_idx = {}  # iter_index -> entry index of accepted assistant
    for i, e in enumerate(entries):
        if e.get("role") == "assistant":
            accepted_assist_idx[e["iteration"]] = i  # last wins

    # D3: pull usage from the transcript first (embedded by agent.py),
    # fall back to a `.usage.jsonl` sidecar if the transcript predates
    # the embedded-usage rev. Legacy transcripts without either still
    # get blank token columns — that's the original T3 contract.
    sidecar_idx = _load_usage_sidecar(path)

    sim_count_cum = 0
    rows = []
    for iter_k in sorted(accepted_assist_idx):
        assist_i = accepted_assist_idx[iter_k]
        assist = entries[assist_i]
        usage_row = _usage_from_entry(assist)
        # If transcript-embedded usage is blank, try the sidecar.
        if usage_row["total_tokens"] == "" and iter_k in sidecar_idx:
            usage_row = sidecar_idx[iter_k]
        # find next results user message AFTER this assistant entry
        sim_user = None
        for j in range(assist_i + 1, len(entries)):
            e = entries[j]
            if e.get("role") != "user":
                continue
            c = e.get("content", "") or ""
            if is_repair_user(c):
                continue
            if is_results_user(c):
                sim_user = e
                break
            # User message that isn't repair and isn't results -> could be
            # an initial prompt re-injection; skip
        sim_count_this = 1 if sim_user is not None else 0
        sim_count_cum += sim_count_this
        if sim_user is not None:
            spec_pass, fail_reason = parse_metrics_block(sim_user.get("content", ""))
        else:
            spec_pass, fail_reason = None, ""

        rows.append({
            "project": project,
            "run_id": rid,
            "timestamp": assist.get("timestamp", ""),
            "llm_model": model,
            "iter_index": iter_k,
            "prompt_tokens": usage_row["prompt_tokens"],
            "completion_tokens": usage_row["completion_tokens"],
            "reasoning_tokens": usage_row["reasoning_tokens"],
            "total_tokens": usage_row["total_tokens"],
            "sim_count_this_iter": sim_count_this,
            "sim_count_cumulative": sim_count_cum,
            "spec_pass_flag": "" if spec_pass is None else ("TRUE" if spec_pass else "FALSE"),
            "fail_reason_if_any": fail_reason,
        })
    return rows


# ---- main ----------------------------------------------------------------

def main():
    model_idx = build_model_index()
    print(f"[info] discovered {len(model_idx)} agent .log files w/ resolvable model")
    for k, v in sorted(model_idx.items()):
        print(f"  {k} -> {v}")

    transcripts = find_transcripts()
    print(f"[info] discovered {len(transcripts)} transcripts across {len(PROJECTS)} projects")

    fieldnames = [
        "project", "run_id", "timestamp", "llm_model", "iter_index",
        "prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens",
        "sim_count_this_iter", "sim_count_cumulative",
        "spec_pass_flag", "fail_reason_if_any",
    ]

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    n_rows = 0
    per_project = {}
    per_run = {}
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for proj, path in transcripts:
            rows = extract_rows(proj, path, model_idx)
            for r in rows:
                w.writerow(r)
                n_rows += 1
                per_project[proj] = per_project.get(proj, 0) + 1
                per_run[(proj, r["run_id"])] = per_run.get((proj, r["run_id"]), 0) + 1
    print(f"[info] wrote {n_rows} rows -> {OUT_CSV}")
    print("[info] rows per project:")
    for p, c in sorted(per_project.items()):
        print(f"  {p}: {c}")
    print("[info] rows per run:")
    for (p, r), c in sorted(per_run.items()):
        print(f"  {p}/{r}: {c}")


if __name__ == "__main__":
    main()
