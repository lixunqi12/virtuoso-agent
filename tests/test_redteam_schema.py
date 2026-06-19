"""Trial schema for the red-team adversarial-extraction experiment (M0).

Pins the one statistic the paper review (P2a) insisted on: fixed-probe
trials are counted PER-PROBE and adaptive trials PER-SESSION, and the two
denominators are never merged into a single ASR.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.redteam.schema import Trial, summarize  # noqa: E402


def _t(tier: str, attacker: str, leaked: bool) -> Trial:
    return Trial(tier=tier, attacker=attacker, trial_id="x", leaked=leaked)


def test_summarize_keeps_fixed_and_adaptive_denominators_separate():
    trials = [
        _t("1", "fixed", False),
        _t("1", "fixed", False),
        _t("1", "fixed", True),     # 1 leak / 3 probes
        _t("1", "adaptive", False),
        _t("1", "adaptive", False),  # 0 successes / 2 sessions
    ]
    s = summarize(trials)
    assert "1" in s
    tier1 = s["1"]
    assert (tier1.fixed.trials, tier1.fixed.leaks) == (3, 1)
    assert (tier1.adaptive.trials, tier1.adaptive.leaks) == (2, 0)
    assert tier1.fixed.asr == 1 / 3
    assert tier1.adaptive.asr == 0.0


def test_empty_mode_has_zero_asr_not_div_by_zero():
    s = summarize([_t("2", "fixed", True)])
    assert "2" in s
    assert s["2"].fixed.asr == 1.0
    assert s["2"].adaptive.trials == 0
    assert s["2"].adaptive.asr == 0.0


def test_tiers_are_aggregated_independently():
    s = summarize([_t("1", "fixed", True), _t("3a", "fixed", False)])
    assert "1" in s and "3a" in s
    assert s["1"].fixed.leaks == 1
    assert s["3a"].fixed.leaks == 0
