from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_paper_benchmark import (  # noqa: E402
    CHECKPOINTS,
    CIRCUITS,
    Cell,
    build_agent_cmd,
    build_cells,
    parse_run_stdout,
    preflight,
    run_cell,
    summarize_transcript,
)


def test_default_checkpoints_are_current_provider_set():
    assert list(CHECKPOINTS) == [
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "gpt-5.5",
        "gpt-5.4-mini",
        "kimi-k2.5",
        "minimax-m2.7",
        "minimax-m3",
        "mimo-v2.5-pro",
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "gemini-2.5-pro",
    ]


def test_build_cells_crosses_circuits_and_models():
    cells = build_cells(["opamp", "lc_vco"], ["mimo-v2.5-pro"], seed=1)
    assert [c.key for c in cells] == [
        "opamp::mimo-v2.5-pro::full::seed1",
        "lc_vco::mimo-v2.5-pro::full::seed1",
    ]


def test_opamp_command_keeps_stimulus_fixed_and_ignores_setup():
    cmd = build_agent_cmd(
        CIRCUITS["opamp"],
        CHECKPOINTS["mimo-v2.5-pro"],
        max_iter=15,
        sweep_results_root=None,
    )
    assert "--ignore-llm-maestro-setup" in cmd
    assert "--analysis" in cmd and "ac" in cmd
    fixed = [
        cmd[idx + 1]
        for idx, item in enumerate(cmd)
        if item == "--fixed-design-var"
    ]
    assert fixed == ["ac_magnitude", "Vicm", "p_phase", "n_phase"]


def test_lc_vco_command_uses_curve_searcher_and_sweep_root():
    cmd = build_agent_cmd(
        CIRCUITS["lc_vco"],
        CHECKPOINTS["mimo-v2.5-pro"],
        max_iter=15,
        sweep_results_root="/remote/Interactive.1",
    )
    assert "--auto-bias-ic" in cmd
    assert "--enable-curve-searcher" in cmd
    assert "--sweep-results-root" in cmd
    assert "/remote/Interactive.1" in cmd


def test_dry_run_does_not_call_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "scripts.run_paper_benchmark.PROJECT_ROOT", PROJECT_ROOT,
    )
    with patch("scripts.run_paper_benchmark.subprocess.run") as mock_run:
        rec = run_cell(
            Cell("opamp", "mimo-v2.5-pro"),
            max_iter=15,
            timeout_s=10,
            reset_timeout_s=10,
            dry_run=True,
            no_reset=False,
            sweep_results_root=None,
        )
    mock_run.assert_not_called()
    assert rec.outcome == "DRY_RUN"
    assert rec.reset_status == "planned"


def test_lc_vco_without_sweep_root_is_skipped_before_subprocess():
    with patch("scripts.run_paper_benchmark.subprocess.run") as mock_run:
        rec = run_cell(
            Cell("lc_vco", "mimo-v2.5-pro"),
            max_iter=15,
            timeout_s=10,
            reset_timeout_s=10,
            dry_run=False,
            no_reset=False,
            sweep_results_root=None,
        )
    mock_run.assert_not_called()
    assert rec.outcome == "SKIPPED"
    assert "sweep" in (rec.fail_reason or "")


def test_lc_vco_clears_sweep_root_after_reset_before_agent(monkeypatch):
    events: list[str] = []
    completed = MagicMock(spec=subprocess.CompletedProcess)
    completed.returncode = 0

    def fake_reset(*args, **kwargs):
        events.append("reset")
        return "ok", None

    def fake_clear(root):
        events.append(f"clear:{root}")
        return {"ok": True, "cleared": True, "archive": "/home/u/Interactive.0.old"}

    def fake_run(cmd, cwd, stdout, stderr, timeout, check):
        events.append("agent")
        stdout.write(
            "FINAL RESULTS\n"
            "  f_osc_GHz: 20.0\n"
            "  converged        : True\n"
            "  writeback_status : ok\n"
        )
        return completed

    with patch("scripts.run_paper_benchmark.run_reset", fake_reset), patch(
        "scripts.run_paper_benchmark.clear_sweep_results_root", fake_clear,
    ), patch(
        "scripts.run_paper_benchmark.subprocess.run", fake_run,
    ), patch(
        "scripts.run_paper_benchmark.discover_transcript", return_value=None,
    ):
        rec = run_cell(
            Cell("lc_vco", "mimo-v2.5-pro"),
            max_iter=1,
            timeout_s=10,
            reset_timeout_s=10,
            dry_run=False,
            no_reset=False,
            sweep_results_root="/home/u/sim/Interactive.0",
        )

    assert events == ["reset", "clear:/home/u/sim/Interactive.0", "agent"]
    assert rec.outcome == "PASS"
    assert rec.writeback_status == "ok"


def test_lc_vco_cleanup_failure_does_not_start_agent():
    with patch("scripts.run_paper_benchmark.run_reset", return_value=("ok", None)), patch(
        "scripts.run_paper_benchmark.clear_sweep_results_root",
        side_effect=RuntimeError("cleanup failed"),
    ), patch("scripts.run_paper_benchmark.subprocess.run") as mock_run:
        rec = run_cell(
            Cell("lc_vco", "mimo-v2.5-pro"),
            max_iter=1,
            timeout_s=10,
            reset_timeout_s=10,
            dry_run=False,
            no_reset=False,
            sweep_results_root="/home/u/sim/Interactive.0",
        )

    mock_run.assert_not_called()
    assert rec.outcome == "RESET_FAIL"
    assert rec.reset_status == "sweep_cleanup_failed"
    assert "sweep cleanup failed" in (rec.fail_reason or "")


def test_preflight_reports_only_enabled_missing_env(monkeypatch):
    for ckpt in CHECKPOINTS.values():
        monkeypatch.delenv(ckpt.env_var, raising=False)
    monkeypatch.setenv("MIMO_API_KEY", "ok")
    cells = [Cell("opamp", "mimo-v2.5-pro")]
    assert preflight(cells, sweep_results_root=None) == []


def test_preflight_lc_vco_requires_sweep_root(monkeypatch):
    monkeypatch.setenv("MIMO_API_KEY", "ok")
    cells = [Cell("lc_vco", "mimo-v2.5-pro")]
    missing = preflight(cells, sweep_results_root=None)
    assert "LC_VCO_SWEEP_RESULTS_ROOT or --sweep-results-root" in missing


def test_parse_run_stdout_extracts_final_metrics(tmp_path):
    stdout = tmp_path / "run.stdout.log"
    stdout.write_text(
        """
FINAL RESULTS
  A0_diff_db: 52.101
  UGB_Hz: 195361896.128

  converged        : True
  abort_reason     : -
  writeback_status : ok

# Optimization Report
## Iteration 1
## Iteration 2
""",
        encoding="utf-8",
    )
    parsed = parse_run_stdout(stdout)
    assert parsed["measurements"]["A0_diff_db"] == 52.101
    assert parsed["measurements"]["UGB_Hz"] == 195361896.128
    assert parsed["converged"] is True
    assert parsed["abort_reason"] is None
    assert parsed["writeback_status"] == "ok"
    assert parsed["n_iter"] == 2


def test_summarize_transcript_counts_telemetry(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    rows = [
        {"role": "assistant", "llm_telemetry": {
            "status": "ok", "finish_reason": "stop",
            "transport_mode": "streaming", "duration_s": 1.2,
            "event_count": 10, "visible_chars": 50,
            "reasoning_chars": 100,
        }},
        {"role": "assistant", "llm_telemetry": {
            "status": "ok", "finish_reason": "length",
            "transport_mode": "streaming", "duration_s": 2.0,
            "event_count": 11, "visible_chars": 60,
            "reasoning_chars": 110,
        }},
    ]
    transcript.write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8",
    )
    summary = summarize_transcript(transcript)
    assert summary["assistant_turns"] == 2
    assert summary["telemetry_rows"] == 2
    assert summary["finish_reasons"] == {"stop": 1, "length": 1}
    assert summary["last_finish_reason"] == "length"


def test_execute_path_maps_converged_false_to_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "scripts.run_paper_benchmark.PROJECT_ROOT", tmp_path,
    )

    def fake_reset(*args, **kwargs):
        return "ok", None

    completed = MagicMock(spec=subprocess.CompletedProcess)
    completed.returncode = 0

    def fake_run(cmd, cwd, stdout, stderr, timeout, check):
        stdout.write("FINAL RESULTS\n  A0_diff_db: 1\n  converged        : False\n")
        return completed

    with patch("scripts.run_paper_benchmark.run_reset", fake_reset), patch(
        "scripts.run_paper_benchmark.subprocess.run", fake_run,
    ), patch(
        "scripts.run_paper_benchmark.discover_transcript", return_value=None,
    ):
        rec = run_cell(
            Cell("opamp", "mimo-v2.5-pro"),
            max_iter=1,
            timeout_s=10,
            reset_timeout_s=10,
            dry_run=False,
            no_reset=False,
            sweep_results_root=None,
        )
    assert rec.outcome == "FAIL"
    assert rec.converged is False
