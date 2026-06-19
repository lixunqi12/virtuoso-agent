"""Regression: the HSpice next-turn LLM prompt must not leak host paths.

The HSpice closed loop assembles the next LLM prompt in
``HspiceAgent._next_prompt()``. An earlier version embedded
``run_result.run_dir_remote`` -- a raw absolute remote path such as
``/project/<user>/work/...`` -- verbatim, bypassing both the SafeBridge
sanitizer and the ``assert_llm_feedback_safe`` final gate that the Spectre
loop applies (``src/agent.py`` Spectre branch). That is a Tier-1 host-path
leak under the paper's threat model (absolute paths / user names are
protected assets).

These tests pin two things:
  1. the assembled prompt carries no absolute host path / run dir; and
  2. the fix removes only the path -- the metric feedback is preserved
     (the whole prompt is NOT withheld).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.agent import HspiceAgent  # noqa: E402
from src.hspice_worker import HspiceRunResult  # noqa: E402


SENSITIVE_DIR = "/project/alice/work/lc_vco/run_20260616_001"


def _make_agent() -> HspiceAgent:
    llm = mock.Mock()
    worker = mock.Mock()
    worker.cfg.ssh_base_args.return_value = []
    worker.cfg.ssh_connect_timeout_s = 10
    with mock.patch("src.agent.RemotePatcher"):
        return HspiceAgent(
            llm=llm,
            worker=worker,
            spec_text="spec",
            spec_metrics=[{"name": "m"}],
            whitelist=["r"],
            remote_target_path="target.sp",
            remote_run_path="run.sp",
        )


def _run_result(run_dir_remote: str) -> HspiceRunResult:
    return HspiceRunResult(
        returncode=0,
        stdout_scrubbed="",
        stderr_scrubbed="",
        mt_files={},
        lis_scrubbed=None,
        run_dir_remote=run_dir_remote,
        sp_base="sim",
    )


def _evaluation():
    return types.SimpleNamespace(
        pass_fail={"my_measure": "FAIL (1.0)"},
        measurements={"my_measure": 1.0},
    )


def test_next_prompt_does_not_embed_raw_remote_run_dir():
    agent = _make_agent()
    prompt = agent._next_prompt(1, _evaluation(), _run_result(SENSITIVE_DIR))
    assert SENSITIVE_DIR not in prompt
    assert "/project/alice" not in prompt


def test_next_prompt_preserves_metric_feedback():
    # The fix must strip the path, not withhold the whole prompt: the
    # model still needs the per-metric verdict to make progress.
    agent = _make_agent()
    prompt = agent._next_prompt(1, _evaluation(), _run_result(SENSITIVE_DIR))
    assert "## Metrics" in prompt
    assert "my_measure" in prompt
