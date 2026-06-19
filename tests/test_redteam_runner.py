"""Offline runner: drive every fixed probe through the real SafeBridge
sanitizers, scan the LLM-facing text with the oracle, and aggregate ASR.

The expected result is zero canary survival (the by-construction defense
holds); a non-zero leak here is a real finding, not a passing test.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.redteam.probes import all_probes  # noqa: E402
from src.redteam.runner import run_offline  # noqa: E402
from src.redteam.schema import summarize  # noqa: E402


def test_offline_run_covers_all_fixed_probes():
    trials = run_offline()
    assert len(trials) == len(all_probes())
    assert {t.tier for t in trials} >= {"1", "2"}
    assert all(t.attacker == "fixed" for t in trials)


def test_offline_fixed_probes_have_zero_canary_survival():
    trials = run_offline()
    leaked = [(t.trial_id, t.matched) for t in trials if t.leaked]
    assert leaked == [], f"canary survived the scrubbing path: {leaked}"


def test_summary_reports_per_tier_fixed_denominator():
    summary = summarize(run_offline())
    assert "1" in summary and "2" in summary
    # every trial counted under the fixed (per-probe) denominator, none adaptive
    assert summary["1"].fixed.trials >= 1
    assert summary["1"].adaptive.trials == 0
