"""Adaptive (per-session) red-team runner.

A session runs an attacker planner for up to ``max_turns``; it succeeds (leaks)
if ANY turn's LLM-facing text trips the oracle, and stops early on that turn.
Planners are mocked here -- the real LLM-backed planner is exercised live, not
in unit tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.redteam.runner import offline_context, run_session  # noqa: E402


def test_session_with_blocked_attacks_yields_no_leak():
    with offline_context() as ctx:
        def canary_cell_read() -> str:
            ctx.mock_client.execute_skill.return_value = {
                "instances": [{"name": "M1", "cell": ctx.cval("cell"),
                               "lib": "GENERIC_PDK", "params": {"w": "1u"}}]
            }
            return json.dumps(ctx.bridge.read_circuit("l", "c"))

        def planner(turn, feedback):
            return canary_cell_read if turn < 3 else None

        trial = run_session(planner, ctx, tier="1", max_turns=5)
    assert trial.attacker == "adaptive"
    assert trial.leaked is False


def test_session_detects_leak_and_stops_early():
    seen: list[int] = []
    with offline_context() as ctx:
        leak_val = ctx.cval("model")

        def planner(turn, feedback):
            seen.append(turn)
            return lambda: f"oops, leaked {leak_val} into the prompt"

        trial = run_session(planner, ctx, tier="2", max_turns=5)
    assert trial.leaked is True
    assert "foundry_model" in trial.matched
    assert seen == [0]  # stopped after the first leaking turn


def test_session_respects_max_turns_when_no_leak():
    turns: list[int] = []
    with offline_context() as ctx:
        def planner(turn, feedback):
            turns.append(turn)
            return lambda: "## Metrics\n- gm: 1.0e-3 -> PASS\n"

        trial = run_session(planner, ctx, tier="2", max_turns=4)
    assert trial.leaked is False
    assert turns == [0, 1, 2, 3]
