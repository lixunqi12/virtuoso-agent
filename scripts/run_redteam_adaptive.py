#!/usr/bin/env python3
"""Adaptive (LLM-backed) red-team run.

An LLM sits in the attacker seat: each turn it proposes a malicious action
(JSON), which is applied to the LOCAL SafeBridge and scanned by the oracle. A
session succeeds (leaks) if any turn trips the oracle. Reports per-session ASR.

    python scripts/run_redteam_adaptive.py --provider deepseek \\
        --model deepseek-v4-pro --sessions 10 --turns 8

Raw per-session transcripts go to a gitignored dir; only the aggregated summary
(counts/ASR, no raw sentinel substrings) is written via --out.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

from src.llm_client import create_llm_client  # noqa: E402
from src.redteam.attacker_llm import build_llm_planner  # noqa: E402
from src.redteam.runner import offline_context, run_session  # noqa: E402


class _RecordingClient:
    """Wrap an LLM client to capture each turn's prompt + response locally."""

    def __init__(self, inner, sink: list):
        self._inner = inner
        self._sink = sink

    def chat(self, messages):
        resp = self._inner.chat(messages)
        self._sink.append({
            "prompt": messages[-1]["content"] if messages else "",
            "response": resp,
        })
        return resp


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--provider", default="deepseek")
    ap.add_argument("--model", default="deepseek-v4-pro")
    ap.add_argument("--sessions", type=int, default=10)
    ap.add_argument("--turns", type=int, default=8)
    ap.add_argument("--env-file", default=str(REPO / "config" / ".env"))
    ap.add_argument("--transcript-dir",
                    default=str(REPO / "paper" / "data" / "redteam_transcripts"))
    ap.add_argument("--out", default=None,
                    help="write the counts/ASR summary JSON here")
    ap.add_argument("--date", default="unset",
                    help="run date stamp recorded in the summary metadata")
    args = ap.parse_args()

    load_dotenv(args.env_file)
    client = create_llm_client(args.provider, model=args.model)
    tdir = Path(args.transcript_dir)
    tdir.mkdir(parents=True, exist_ok=True)

    leaked = 0
    rows = []
    for s in range(args.sessions):
        sink: list = []
        rec = _RecordingClient(client, sink)
        with offline_context() as ctx:
            planner = build_llm_planner(rec, ctx)
            trial = run_session(planner, ctx, tier="adaptive", max_turns=args.turns)
        (tdir / f"adaptive_session_{s:02d}.json").write_text(
            json.dumps(sink, indent=2, ensure_ascii=False), encoding="utf-8")
        leaked += int(trial.leaked)
        rows.append({"session": s, "leaked": trial.leaked,
                     "turns_used": len(sink), "matched": list(trial.matched)})
        verdict = f"LEAK {trial.matched}" if trial.leaked else "no leak"
        print(f"session {s:02d}: {verdict}  ({len(sink)} turns)")

    asr = leaked / args.sessions if args.sessions else 0.0
    print(f"\nAdaptive per-session ASR: {leaked}/{args.sessions} = {asr*100:.1f}%"
          f"  (model={args.model}, turns={args.turns})")

    summary = {
        "mode": "adaptive", "provider": args.provider, "model": args.model,
        "sessions": args.sessions, "turns": args.turns, "date": args.date,
        "adaptive": {"sessions": args.sessions, "successes": leaked, "asr": asr},
        "per_session": [{"session": r["session"], "leaked": r["leaked"],
                         "turns_used": r["turns_used"]} for r in rows],
    }
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")

    return 1 if leaked else 0


if __name__ == "__main__":
    raise SystemExit(main())
