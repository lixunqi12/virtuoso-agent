#!/usr/bin/env python3
"""One-time D4 retry tool — clear argparse-FAILed cells from benchmark state.

CONTEXT (2026-05-12):
    The D4 run of scripts/run_benchmark.py --execute fired 33 cells. 15
    of them (5 ckpts x 3 seeds: gpt-5.5, gpt-5.4-mini, mimo-v2.5-pro,
    deepseek-v4-pro, deepseek-v4-flash) FAILed instantly at run_agent.py
    argparse because the --llm choices list was missing openai/mimo/
    deepseek. That bug is now fixed (Phase 2 of task fe4fe496;
    scripts/run_agent.py:125 + LLM_CHOICES module constant).

    But FAIL is a TERMINAL outcome in run_benchmark.py's resume state
    machine — so the next `--execute` invocation would silently skip
    those 15 cells. This script surgically demotes the affected cells
    from "completed" back to "needs re-run" by REMOVING them from
    paper/data/benchmark_state.json.

    Cells matching ANY of (a) llm in --llm-families AND (b) outcome
    matches --outcome-filter are removed. Default outcome filter = FAIL
    only (not PASS/STUCK/TIMEOUT — those have their own semantics).

USAGE:
    # Dry-run preview (default — prints what WOULD be removed):
    .venv/Scripts/python.exe scripts/clear_failed_argparse_cells.py

    # Actually apply the surgery (atomic save):
    .venv/Scripts/python.exe scripts/clear_failed_argparse_cells.py --apply

    # Custom family/outcome filters:
    .venv/Scripts/python.exe scripts/clear_failed_argparse_cells.py \\
        --llm-families openai,mimo,deepseek \\
        --outcome-filter FAIL \\
        --apply

DESIGN:
    - Reads paper/data/benchmark_state.json (or --state path).
    - Filters out matching cells; writes atomically via .tmp + rename.
    - Prints a before/after summary AND lists the specific cells touched.
    - --apply gate prevents accidental state mutation. Default = dry-run.
    - Backs up the prior state file to paper/data/benchmark_state.json.<ts>.bak
      before overwriting.

    THIS SCRIPT TOUCHES PERSISTENT STATE. Run dry-run first, eyeball the
    list, then run with --apply. After running, the cleared cells will
    be re-fired on the next `--execute` invocation of run_benchmark.py.

NOT FOR GENERAL USE — narrowly scoped to the 2026-05-12 argparse bug.
Future state-surgery patterns should be implemented as flags on
run_benchmark.py itself, not as one-off scripts.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_benchmark import (  # noqa: E402
    CHECKPOINTS,
    CellResult,
    STATE_PATH,
    load_state,
    save_state,
)

# The 5 families that died at argparse on 2026-05-12 D4. Default scope
# is exactly these; user can override via --llm-families.
DEFAULT_AFFECTED_LLMS = ("openai", "mimo", "deepseek")
DEFAULT_OUTCOME_FILTER = ("FAIL",)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "One-time tool: surgically remove argparse-FAILed cells from "
            "paper/data/benchmark_state.json so they re-fire on the next "
            "run_benchmark.py --execute pass."
        ),
    )
    p.add_argument(
        "--state",
        default=str(STATE_PATH),
        help=f"State file path (default: {STATE_PATH}).",
    )
    p.add_argument(
        "--llm-families",
        default=",".join(DEFAULT_AFFECTED_LLMS),
        help=(
            "Comma-separated llm keys to target. Default targets the "
            f"D4-2026-05-12 affected set: {','.join(DEFAULT_AFFECTED_LLMS)}."
        ),
    )
    p.add_argument(
        "--outcome-filter",
        default=",".join(DEFAULT_OUTCOME_FILTER),
        help=(
            "Comma-separated outcomes to remove. Default 'FAIL' only. "
            "Other valid values: TIMEOUT, ERROR, STUCK. PASS is not "
            "allowed (would discard real results)."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Actually mutate the state file. Default is dry-run: list "
            "what would be removed and exit without writing."
        ),
    )
    return p.parse_args(argv)


def _ckpt_to_llm() -> dict[str, str]:
    """{ckpt_name: llm_family} so we can map state entries (which carry
    ckpt_name) back to the llm family flag."""
    return {c["name"]: c["llm"] for c in CHECKPOINTS}


def select_cells_to_clear(
    state: dict[str, CellResult],
    llm_families: set[str],
    outcomes: set[str],
) -> list[CellResult]:
    """Return the CellResults that match BOTH the llm family AND outcome
    filters. Empty list if nothing matches.
    """
    ckpt_to_llm = _ckpt_to_llm()
    matched: list[CellResult] = []
    for cell in state.values():
        llm = ckpt_to_llm.get(cell.ckpt_name)
        if llm is None:
            # Cell references a ckpt not in current CHECKPOINTS — leave
            # alone, surgery is conservative.
            continue
        if llm not in llm_families:
            continue
        if cell.outcome not in outcomes:
            continue
        matched.append(cell)
    return matched


def backup_state_file(state_path: Path) -> Path | None:
    """Copy the current state file to a .bak alongside it. Returns the
    backup path, or None if the source didn't exist.
    """
    if not state_path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = state_path.with_suffix(state_path.suffix + f".{ts}.bak")
    shutil.copy2(state_path, backup)
    return backup


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    state_path = Path(args.state)
    llm_families = {s.strip() for s in args.llm_families.split(",") if s.strip()}
    outcomes = {s.strip() for s in args.outcome_filter.split(",") if s.strip()}

    if "PASS" in outcomes:
        print(
            "[clear_failed] REFUSED: outcome filter includes PASS — that "
            "would discard real grid results. Aborting.",
            file=sys.stderr,
        )
        return 2

    state = load_state(state_path)
    if not state:
        print(
            f"[clear_failed] State file empty or missing: {state_path}",
            file=sys.stderr,
        )
        return 1

    to_clear = select_cells_to_clear(state, llm_families, outcomes)

    print(f"[clear_failed] State file: {state_path}")
    print(f"[clear_failed] Total cells in state: {len(state)}")
    print(
        f"[clear_failed] Target llm families: {sorted(llm_families)}, "
        f"outcomes: {sorted(outcomes)}"
    )
    print(f"[clear_failed] Cells matching filter: {len(to_clear)}")
    print()

    if not to_clear:
        print("[clear_failed] Nothing to do.")
        return 0

    print("[clear_failed] Cells that would be REMOVED:")
    for cell in sorted(to_clear, key=lambda c: c.cell_key):
        reason = (cell.fail_reason or "").strip()
        # Truncate to keep output readable
        if len(reason) > 80:
            reason = reason[:77] + "..."
        print(
            f"  - {cell.cell_key:40s} outcome={cell.outcome:8s} "
            f"retries={cell.timeout_retry_count} "
            f"reason={reason!r}"
        )
    print()

    if not args.apply:
        print(
            "[clear_failed] DRY-RUN (default). Re-run with --apply to "
            "actually mutate the state file."
        )
        return 0

    # Apply: back up, then save the filtered state.
    backup = backup_state_file(state_path)
    if backup:
        print(f"[clear_failed] Backed up prior state to: {backup}")

    keys_to_drop = {c.cell_key for c in to_clear}
    new_state = {k: v for k, v in state.items() if k not in keys_to_drop}
    save_state(state_path, new_state)

    print(
        f"[clear_failed] APPLIED. Removed {len(to_clear)} cell(s); "
        f"state now contains {len(new_state)} entries."
    )
    print(
        "[clear_failed] Next `run_benchmark.py --execute` will re-fire "
        "the cleared cells."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
