#!/usr/bin/env python3
"""Run the offline red-team extraction experiment and report per-tier ASR.

    python scripts/run_redteam.py [--json paper/data/redteam_asr.json]

Offline mode drives the fixed probe suite through a real SafeBridge against a
mock EDA host (no Spectre / cobi needed). Only the aggregated summary
(counts + ASR, no raw sentinel substrings) is written to JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.redteam.runner import run_offline  # noqa: E402
from src.redteam.schema import summarize  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=str, default=None,
                    help="write the aggregated summary (counts/ASR only) here")
    args = ap.parse_args()

    trials = run_offline()
    summary = summarize(trials)

    print("Red-team extraction -- offline fixed-probe ASR")
    print(f"{'Tier':<5} {'Probes':>7} {'Leaks':>6} {'ASR':>7}")
    print("-" * 28)
    out: dict[str, dict] = {}
    for tier in sorted(summary):
        f = summary[tier].fixed
        print(f"{tier:<5} {f.trials:>7} {f.leaks:>6} {f.asr*100:>6.1f}%")
        out[tier] = {
            "fixed": {"probes": f.trials, "leaks": f.leaks, "asr": f.asr},
            "adaptive": {"sessions": summary[tier].adaptive.trials,
                         "successes": summary[tier].adaptive.leaks,
                         "asr": summary[tier].adaptive.asr},
        }

    leaked = [t.trial_id for t in trials if t.leaked]
    print(f"\nleaked probes: {leaked or 'none'}")

    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"mode": "offline", "summary": out, "leaked_probe_ids": leaked},
            indent=2), encoding="utf-8")
        print(f"wrote {p}")

    # Non-zero exit if any canary survived -- the experiment found a leak.
    return 1 if leaked else 0


if __name__ == "__main__":
    raise SystemExit(main())
