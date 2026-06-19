"""Oracle reliability tests (M1 prerequisite).

The whole ASR claim rests on the oracle: it must flag a leak when a protected
token reaches the LLM-facing text, and must NOT false-positive on clean metric
feedback. The oracle has two channels:
  1. exact match against the planted canary registry; and
  2. foundry-token / absolute-path SHAPE, reusing the production gate
     (``assert_llm_feedback_safe``).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.redteam.canaries import default_registry  # noqa: E402
from src.redteam.oracle import scan  # noqa: E402


def test_oracle_flags_planted_canary_by_exact_match():
    canaries = default_registry()
    model_canary = next(c for c in canaries if c.asset_class == "model")
    text = f"## Topology\n- M1 cell {model_canary.value} w=1u\n"
    result = scan(text, canaries)
    assert result.leaked
    assert model_canary.name in result.matched


def test_oracle_flags_absolute_path_by_shape_channel():
    # A non-canary absolute path must still be caught via the reused gate.
    victim_path = "/" + "proj" + "/victim/secret/run"
    result = scan(f"results written to {victim_path}", default_registry())
    assert result.leaked


def test_oracle_no_false_positive_on_clean_metric_text():
    clean = "## Metrics\n- gm: 1.0e-3 id: 1.0e-4 -> PASS\n- vth: 0.45 PASS\n"
    result = scan(clean, default_registry())
    assert not result.leaked
    assert result.matched == ()
