"""LLM-backed adversary planner (review P2b).

llm_client only exposes chat/ask -- there is no provider tool-calling schema.
So the planner asks an LLM to PROPOSE a malicious action (as JSON) and the
runner applies that proposal through the LOCAL bridge. These tests cover the
deterministic parse/apply helpers and the planner wiring with a MOCK client;
the real LLM call is exercised live, not here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.redteam.attacker_llm import (  # noqa: E402
    AttackAction,
    apply_action,
    build_llm_planner,
    parse_action,
)
from src.redteam.runner import offline_context  # noqa: E402


def test_parse_action_plain_json():
    a = parse_action('{"surface": "design_var", "payload": "foo"}')
    assert a == AttackAction(surface="design_var", payload="foo")


def test_parse_action_extracts_json_from_fenced_text():
    raw = 'Sure, here is my attack:\n```json\n{"surface":"raw","payload":"x"}\n```\n'
    a = parse_action(raw)
    assert a is not None and a.surface == "raw" and a.payload == "x"


def test_parse_action_garbage_returns_none():
    assert parse_action("no json here") is None
    assert parse_action('{"surface": "only"}') is None


def test_apply_action_design_var_canary_is_rejected():
    with offline_context() as ctx:
        action = AttackAction(surface="design_var", payload=ctx.cval("model"))
        text = apply_action(ctx, action)
    assert ctx.cval("model") not in text  # whitelist rejected -> nothing leaks


def test_apply_action_raw_returns_payload_verbatim():
    # a model that simply echoes a canary must be catchable downstream.
    with offline_context() as ctx:
        action = AttackAction(surface="raw", payload="hello world")
        assert apply_action(ctx, action) == "hello world"


def test_build_llm_planner_calls_client_and_returns_attack():
    client = mock.Mock()
    client.chat.return_value = '{"surface": "raw", "payload": "ping"}'
    with offline_context() as ctx:
        planner = build_llm_planner(client, ctx)
        attack = planner(0, "")
        assert attack is not None
        assert attack() == "ping"
    client.chat.assert_called_once()
