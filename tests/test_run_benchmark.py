"""Unit tests for scripts/run_benchmark.py.

Coverage:
  - state file load/save roundtrip + atomic write
  - cell enumeration with ckpt/seed filters
  - resume skips terminal (PASS/FAIL) outcomes, retries TIMEOUT/ERROR
  - usage sidecar emission (transcript -> derived .usage.jsonl)
  - dry-run path returns DRY_RUN markers and never invokes subprocess

NO live LLM calls. NO subprocess invocation: subprocess.run is mocked
in every test that crosses execute_cell, per Claude Code's directive
("不要 mock 整个 grid 跑一遍验脚本"). Tests stay at function level on
state I/O and grid enumeration.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_benchmark import (  # noqa: E402
    CHECKPOINTS,
    Cell,
    CellResult,
    ENV_VAR_BY_LLM,
    MAX_TIMEOUT_RETRIES,
    SEEDS,
    emit_usage_sidecar,
    enumerate_cells,
    execute_cell,
    load_state,
    preflight_env_check,
    run_grid,
    save_state,
    scrub_stdout_file,
    classify_subprocess_failure,
)


# ====================================================================== #
#  Grid config sanity                                                    #
# ====================================================================== #

class TestGridConfig:
    def test_eleven_checkpoints(self):
        """User-approved 10+1 expansion: 3 Anthropic + 2 OpenAI + 4
        China-domestic + 1 Gemini + 1 DeepSeek-flash = 11."""
        assert len(CHECKPOINTS) == 11

    def test_three_seeds(self):
        assert SEEDS == [1, 2, 3]

    def test_checkpoint_names_unique(self):
        names = [c["name"] for c in CHECKPOINTS]
        assert len(names) == len(set(names))

    def test_all_checkpoints_have_required_keys(self):
        for c in CHECKPOINTS:
            assert "name" in c and "llm" in c and "model" in c
            assert c["name"]
            assert c["llm"] in {
                "claude", "gemini", "kimi", "minimax",
                "openai", "mimo", "deepseek",
            }

    def test_deepseek_pareto_pair_present(self):
        """SP3 cost-quality Pareto needs both pro and flash for the
        domestic-China data point."""
        names = {c["name"] for c in CHECKPOINTS}
        assert "deepseek-v4-pro" in names
        assert "deepseek-v4-flash" in names


# ====================================================================== #
#  State file                                                            #
# ====================================================================== #

class TestStateFile:
    def test_load_state_missing_returns_empty(self, tmp_path):
        assert load_state(tmp_path / "nonexistent.json") == {}

    def test_load_state_malformed_returns_empty(self, tmp_path):
        p = tmp_path / "broken.json"
        p.write_text("not valid json {", encoding="utf-8")
        assert load_state(p) == {}

    def test_save_load_roundtrip(self, tmp_path):
        state_path = tmp_path / "state.json"
        results = {
            "claude-opus-4-7::seed1": CellResult(
                cell_key="claude-opus-4-7::seed1",
                ckpt_name="claude-opus-4-7",
                seed=1,
                timestamp="20260512_120000",
                transcript_path="/tmp/t.jsonl",
                usage_sidecar_path="/tmp/t.usage.jsonl",
                stdout_path="/tmp/t.stdout",
                outcome="PASS",
                wall_clock_s=42.0,
                exit_code=0,
                fail_reason=None,
            ),
        }
        save_state(state_path, results)
        loaded = load_state(state_path)
        assert "claude-opus-4-7::seed1" in loaded
        assert loaded["claude-opus-4-7::seed1"].outcome == "PASS"
        assert loaded["claude-opus-4-7::seed1"].wall_clock_s == 42.0

    def test_save_state_atomic_rename(self, tmp_path):
        """Atomic write: write to .tmp then rename. Verifies the tmp
        file is gone after save (would linger on partial-write crash)."""
        state_path = tmp_path / "state.json"
        save_state(state_path, {})
        assert state_path.exists()
        assert not (tmp_path / "state.json.tmp").exists()


# ====================================================================== #
#  Cell enumeration + filters                                            #
# ====================================================================== #

class TestEnumerateCells:
    def test_full_grid(self):
        cells = enumerate_cells(CHECKPOINTS, SEEDS, None, None)
        assert len(cells) == len(CHECKPOINTS) * len(SEEDS) == 33

    def test_ckpt_filter(self):
        cells = enumerate_cells(
            CHECKPOINTS, SEEDS, "deepseek-v4-pro", None,
        )
        assert len(cells) == 3
        assert all(c.ckpt_name == "deepseek-v4-pro" for c, _ in cells)

    def test_seed_filter(self):
        cells = enumerate_cells(CHECKPOINTS, SEEDS, None, 2)
        assert len(cells) == len(CHECKPOINTS)
        assert all(c.seed == 2 for c, _ in cells)

    def test_both_filters(self):
        cells = enumerate_cells(CHECKPOINTS, SEEDS, "gpt-5.5", 3)
        assert len(cells) == 1
        cell, ckpt = cells[0]
        assert cell.ckpt_name == "gpt-5.5"
        assert cell.seed == 3
        assert ckpt["model"] == "gpt-5.5"

    def test_unknown_ckpt_yields_empty(self):
        cells = enumerate_cells(
            CHECKPOINTS, SEEDS, "no-such-model", None,
        )
        assert cells == []


# ====================================================================== #
#  Usage sidecar emission                                                #
# ====================================================================== #

def _write_jsonl(path: Path, entries: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


class TestUsageSidecar:
    def test_emit_picks_assistant_with_usage(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        _write_jsonl(transcript, [
            {"iteration": 0, "role": "user", "content": "prompt"},
            {"iteration": 0, "role": "assistant", "content": "ok",
             "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                       "reasoning_tokens": 20, "total_tokens": 170,
                       "provider": "deepseek", "model": "deepseek-v4-pro"}},
            {"iteration": 1, "role": "user", "content": "next"},
            {"iteration": 1, "role": "assistant", "content": "ok2",
             "usage": {"prompt_tokens": 110, "completion_tokens": 55,
                       "reasoning_tokens": 25, "total_tokens": 190,
                       "provider": "deepseek", "model": "deepseek-v4-pro"}},
        ])
        sidecar = tmp_path / "t.usage.jsonl"
        n = emit_usage_sidecar(transcript, sidecar)
        assert n == 2
        lines = sidecar.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        row0 = json.loads(lines[0])
        assert row0["iteration"] == 0
        assert row0["prompt_tokens"] == 100
        assert row0["total_tokens"] == 170
        assert row0["provider"] == "deepseek"

    def test_emit_skips_user_entries(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        _write_jsonl(transcript, [
            {"iteration": 0, "role": "user", "content": "x",
             "usage": {"this": "should be ignored"}},
            {"iteration": 0, "role": "assistant", "content": "y",
             "usage": {"prompt_tokens": 5, "total_tokens": 10}},
        ])
        sidecar = tmp_path / "t.usage.jsonl"
        n = emit_usage_sidecar(transcript, sidecar)
        assert n == 1

    def test_emit_skips_assistant_without_usage(self, tmp_path):
        """Legacy transcripts predating embedded-usage rev should not
        emit blank sidecar rows."""
        transcript = tmp_path / "t.jsonl"
        _write_jsonl(transcript, [
            {"iteration": 0, "role": "assistant", "content": "no usage"},
        ])
        sidecar = tmp_path / "t.usage.jsonl"
        n = emit_usage_sidecar(transcript, sidecar)
        assert n == 0

    def test_emit_missing_transcript_returns_zero(self, tmp_path):
        sidecar = tmp_path / "missing.usage.jsonl"
        n = emit_usage_sidecar(tmp_path / "no-transcript.jsonl", sidecar)
        assert n == 0
        assert not sidecar.exists()

    def test_emit_tolerates_malformed_lines(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            "{not valid json\n"
            + json.dumps({"iteration": 1, "role": "assistant",
                          "usage": {"total_tokens": 50}}) + "\n",
            encoding="utf-8",
        )
        sidecar = tmp_path / "t.usage.jsonl"
        n = emit_usage_sidecar(transcript, sidecar)
        assert n == 1


# ====================================================================== #
#  Dry-run + resume                                                      #
# ====================================================================== #

class TestDryRun:
    def test_execute_cell_dry_run_no_subprocess(self):
        """Dry-run must NOT invoke subprocess — protects against
        accidental API spend. Patches subprocess.run with a sentinel
        that explodes if called."""
        cell = Cell(ckpt_name="claude-opus-4-7", seed=1)
        ckpt = CHECKPOINTS[0]
        with patch("scripts.run_benchmark.subprocess.run") as mock_run:
            result = execute_cell(
                cell, ckpt, max_iter=10, timeout_s=600, dry_run=True,
            )
        mock_run.assert_not_called()
        assert result.outcome == "DRY_RUN"
        assert result.cell_key == "claude-opus-4-7::seed1"

    def test_run_grid_dry_run_marks_all_dry(self, tmp_path):
        state = tmp_path / "state.json"
        with patch("scripts.run_benchmark.subprocess.run") as mock_run:
            summary = run_grid(
                checkpoints=CHECKPOINTS[:2],
                seeds=[1],
                state_path=state,
                max_iter=10,
                timeout_s=600,
                dry_run=True,
                resume=False,
                ckpt_filter=None,
                seed_filter=None,
            )
        mock_run.assert_not_called()
        assert summary["total_cells"] == 2
        assert summary["executed_this_run"] == 2
        assert summary["outcomes"].get("DRY_RUN") == 2

    def test_run_grid_dry_run_does_not_write_state(self, tmp_path):
        """Dry-run must not touch the persistent state file — otherwise
        a dry-run preview would mark cells as completed."""
        state = tmp_path / "state.json"
        with patch("scripts.run_benchmark.subprocess.run"):
            run_grid(
                checkpoints=CHECKPOINTS[:1],
                seeds=[1],
                state_path=state,
                max_iter=10,
                timeout_s=600,
                dry_run=True,
                resume=False,
                ckpt_filter=None,
                seed_filter=None,
            )
        assert not state.exists()


class TestResume:
    @staticmethod
    def _make_result(ckpt: str, seed: int, outcome: str) -> CellResult:
        return CellResult(
            cell_key=f"{ckpt}::seed{seed}",
            ckpt_name=ckpt,
            seed=seed,
            timestamp="20260512_120000",
            transcript_path=None,
            usage_sidecar_path=None,
            stdout_path=None,
            outcome=outcome,
            wall_clock_s=1.0,
            exit_code=0 if outcome == "PASS" else 1,
            fail_reason=None,
        )

    def test_resume_skips_pass(self, tmp_path):
        state = tmp_path / "state.json"
        prior = {
            "claude-opus-4-7::seed1": self._make_result(
                "claude-opus-4-7", 1, "PASS",
            ),
        }
        save_state(state, prior)
        with patch("scripts.run_benchmark.subprocess.run") as mock_run:
            summary = run_grid(
                checkpoints=CHECKPOINTS[:1],
                seeds=[1],
                state_path=state,
                max_iter=10,
                timeout_s=600,
                dry_run=True,
                resume=True,
                ckpt_filter=None,
                seed_filter=None,
            )
        mock_run.assert_not_called()
        assert summary["skipped_resumed"] == 1
        assert summary["executed_this_run"] == 0

    def test_resume_skips_fail(self, tmp_path):
        """FAIL is terminal — don't replay (might be a legitimate non-
        convergence result that paper §6 wants to bucket).
        """
        state = tmp_path / "state.json"
        prior = {
            "gpt-5.5::seed1": self._make_result("gpt-5.5", 1, "FAIL"),
        }
        save_state(state, prior)
        gpt = next(c for c in CHECKPOINTS if c["name"] == "gpt-5.5")
        summary = run_grid(
            checkpoints=[gpt],
            seeds=[1],
            state_path=state,
            max_iter=10,
            timeout_s=600,
            dry_run=True,
            resume=True,
            ckpt_filter=None,
            seed_filter=None,
        )
        assert summary["skipped_resumed"] == 1

    def test_resume_retries_timeout(self, tmp_path):
        """TIMEOUT might be a transient infra blip — retry on next
        grid invocation."""
        state = tmp_path / "state.json"
        prior = {
            "deepseek-v4-pro::seed1": self._make_result(
                "deepseek-v4-pro", 1, "TIMEOUT",
            ),
        }
        save_state(state, prior)
        ds = next(c for c in CHECKPOINTS if c["name"] == "deepseek-v4-pro")
        summary = run_grid(
            checkpoints=[ds],
            seeds=[1],
            state_path=state,
            max_iter=10,
            timeout_s=600,
            dry_run=True,
            resume=True,
            ckpt_filter=None,
            seed_filter=None,
        )
        assert summary["skipped_resumed"] == 0
        assert summary["executed_this_run"] == 1

    def test_resume_retries_error(self, tmp_path):
        """ERROR (e.g. OSError on subprocess spawn) is also transient
        and gets retried."""
        state = tmp_path / "state.json"
        prior = {
            "mimo-v2.5-pro::seed1": self._make_result(
                "mimo-v2.5-pro", 1, "ERROR",
            ),
        }
        save_state(state, prior)
        mimo = next(c for c in CHECKPOINTS if c["name"] == "mimo-v2.5-pro")
        summary = run_grid(
            checkpoints=[mimo],
            seeds=[1],
            state_path=state,
            max_iter=10,
            timeout_s=600,
            dry_run=True,
            resume=True,
            ckpt_filter=None,
            seed_filter=None,
        )
        assert summary["skipped_resumed"] == 0
        assert summary["executed_this_run"] == 1

    def test_no_resume_replays_everything(self, tmp_path):
        state = tmp_path / "state.json"
        prior = {
            "claude-opus-4-7::seed1": self._make_result(
                "claude-opus-4-7", 1, "PASS",
            ),
        }
        save_state(state, prior)
        summary = run_grid(
            checkpoints=CHECKPOINTS[:1],
            seeds=[1],
            state_path=state,
            max_iter=10,
            timeout_s=600,
            dry_run=True,
            resume=False,
            ckpt_filter=None,
            seed_filter=None,
        )
        assert summary["skipped_resumed"] == 0
        assert summary["executed_this_run"] == 1


# ====================================================================== #
#  Subprocess-level execute (mocked subprocess.run)                      #
# ====================================================================== #

class TestExecuteCellMocked:
    """Cover the live-execution code path with subprocess.run mocked.

    NOT a real subprocess invocation — these verify the outcome-mapping
    logic without burning any LLM tokens.
    """

    def _patch_artifact_discovery(self, transcript_lines: list[dict] | None):
        """Patch discover_run_artifacts to return a synthetic transcript
        path containing the given lines (or None to simulate no
        transcript)."""
        if transcript_lines is None:
            return patch(
                "scripts.run_benchmark.discover_run_artifacts",
                return_value=None,
            )
        # We can't actually create the file from the test classmethod
        # since tmp_path isn't available here — the caller wires it.
        raise NotImplementedError("Use TestExecuteCellMocked fixtures")

    def test_subprocess_timeout_maps_to_TIMEOUT(self, tmp_path):
        cell = Cell(ckpt_name="kimi-k2.5", seed=1)
        ckpt = next(c for c in CHECKPOINTS if c["name"] == "kimi-k2.5")
        with patch(
            "scripts.run_benchmark.BENCHMARK_LOG_DIR", tmp_path,
        ), patch(
            "scripts.run_benchmark.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="x", timeout=10),
        ), patch(
            "scripts.run_benchmark.discover_run_artifacts",
            return_value=None,
        ):
            result = execute_cell(
                cell, ckpt, max_iter=2, timeout_s=10, dry_run=False,
            )
        assert result.outcome == "TIMEOUT"
        assert "wall-clock timeout" in (result.fail_reason or "")

    def test_subprocess_nonzero_maps_to_FAIL(self, tmp_path):
        cell = Cell(ckpt_name="kimi-k2.5", seed=1)
        ckpt = next(c for c in CHECKPOINTS if c["name"] == "kimi-k2.5")
        completed = MagicMock(spec=subprocess.CompletedProcess)
        completed.returncode = 4
        with patch(
            "scripts.run_benchmark.BENCHMARK_LOG_DIR", tmp_path,
        ), patch(
            "scripts.run_benchmark.subprocess.run", return_value=completed,
        ), patch(
            "scripts.run_benchmark.discover_run_artifacts",
            return_value=None,
        ):
            result = execute_cell(
                cell, ckpt, max_iter=2, timeout_s=10, dry_run=False,
            )
        assert result.outcome == "FAIL"
        assert result.exit_code == 4

    def test_subprocess_oserror_maps_to_ERROR(self, tmp_path):
        cell = Cell(ckpt_name="kimi-k2.5", seed=1)
        ckpt = next(c for c in CHECKPOINTS if c["name"] == "kimi-k2.5")
        with patch(
            "scripts.run_benchmark.BENCHMARK_LOG_DIR", tmp_path,
        ), patch(
            "scripts.run_benchmark.subprocess.run",
            side_effect=OSError("no such executable"),
        ), patch(
            "scripts.run_benchmark.discover_run_artifacts",
            return_value=None,
        ):
            result = execute_cell(
                cell, ckpt, max_iter=2, timeout_s=10, dry_run=False,
            )
        assert result.outcome == "ERROR"
        assert "OSError" in (result.fail_reason or "")

    def test_pass_with_zero_usage_demoted_to_fail(self, tmp_path):
        """Subprocess returns 0 but emits no LLM usage rows — likely a
        spec-validation abort before any LLM call. Don't count this as
        a real convergence in the §6 bucketing.
        """
        cell = Cell(ckpt_name="kimi-k2.5", seed=1)
        ckpt = next(c for c in CHECKPOINTS if c["name"] == "kimi-k2.5")
        completed = MagicMock(spec=subprocess.CompletedProcess)
        completed.returncode = 0

        # Synthetic transcript with no assistant entries
        transcript = tmp_path / "transcript_20260512_120000.jsonl"
        _write_jsonl(transcript, [
            {"iteration": 0, "role": "user", "content": "x"},
        ])
        with patch(
            "scripts.run_benchmark.BENCHMARK_LOG_DIR", tmp_path,
        ), patch(
            "scripts.run_benchmark.subprocess.run", return_value=completed,
        ), patch(
            "scripts.run_benchmark.discover_run_artifacts",
            return_value=transcript,
        ):
            result = execute_cell(
                cell, ckpt, max_iter=2, timeout_s=10, dry_run=False,
            )
        assert result.outcome == "FAIL"
        assert "zero LLM-usage" in (result.fail_reason or "")

    def test_pass_with_usage_stays_pass(self, tmp_path):
        cell = Cell(ckpt_name="kimi-k2.5", seed=1)
        ckpt = next(c for c in CHECKPOINTS if c["name"] == "kimi-k2.5")
        completed = MagicMock(spec=subprocess.CompletedProcess)
        completed.returncode = 0

        transcript = tmp_path / "transcript_20260512_120000.jsonl"
        _write_jsonl(transcript, [
            {"iteration": 0, "role": "user", "content": "x"},
            {"iteration": 0, "role": "assistant", "content": "y",
             "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                       "reasoning_tokens": 0, "total_tokens": 15,
                       "provider": "kimi", "model": "kimi-k2.5"}},
        ])
        with patch(
            "scripts.run_benchmark.BENCHMARK_LOG_DIR", tmp_path,
        ), patch(
            "scripts.run_benchmark.subprocess.run", return_value=completed,
        ), patch(
            "scripts.run_benchmark.discover_run_artifacts",
            return_value=transcript,
        ):
            result = execute_cell(
                cell, ckpt, max_iter=2, timeout_s=10, dry_run=False,
            )
        assert result.outcome == "PASS"
        assert result.exit_code == 0
        assert result.usage_sidecar_path is not None


# ====================================================================== #
#  STUCK promotion (claude_reviewer_v2 D3 P1)                            #
# ====================================================================== #

class TestStuckPromotion:
    """After MAX_TIMEOUT_RETRIES consecutive TIMEOUT/ERROR, the cell is
    promoted to STUCK terminal — prevents a deterministically broken
    endpoint from re-burning the resume budget every grid invocation.
    """

    @staticmethod
    def _make_prior(
        ckpt: str, seed: int, outcome: str, retry_count: int,
    ) -> CellResult:
        return CellResult(
            cell_key=f"{ckpt}::seed{seed}",
            ckpt_name=ckpt, seed=seed,
            timestamp="20260512_120000",
            transcript_path=None, usage_sidecar_path=None,
            stdout_path=None,
            outcome=outcome,
            wall_clock_s=1.0, exit_code=None, fail_reason=None,
            timeout_retry_count=retry_count,
        )

    def test_max_retries_constant_is_three(self):
        assert MAX_TIMEOUT_RETRIES == 3

    def test_consecutive_timeout_promotes_to_stuck(self, tmp_path):
        """Prior 2 consecutive TIMEOUTs + this attempt also times out
        → outcome promoted to STUCK on the new result.
        """
        state = tmp_path / "state.json"
        ckpt_name = "deepseek-v4-pro"
        prior = {
            f"{ckpt_name}::seed1": self._make_prior(
                ckpt_name, 1, "TIMEOUT", retry_count=2,
            ),
        }
        save_state(state, prior)
        ds = next(c for c in CHECKPOINTS if c["name"] == ckpt_name)
        with patch(
            "scripts.run_benchmark.BENCHMARK_LOG_DIR", tmp_path,
        ), patch(
            "scripts.run_benchmark.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="x", timeout=10),
        ), patch(
            "scripts.run_benchmark.discover_run_artifacts",
            return_value=None,
        ):
            summary = run_grid(
                checkpoints=[ds],
                seeds=[1],
                state_path=state,
                max_iter=2,
                timeout_s=10,
                dry_run=False,
                resume=True,
                ckpt_filter=None,
                seed_filter=None,
            )
        assert summary["executed_this_run"] == 1
        loaded = load_state(state)
        result = loaded[f"{ckpt_name}::seed1"]
        assert result.outcome == "STUCK"
        assert result.timeout_retry_count == 3
        assert "STUCK" in (result.fail_reason or "")

    def test_stuck_is_terminal_skipped_on_resume(self, tmp_path):
        """A cell that was promoted to STUCK in a prior run must NOT be
        retried on subsequent invocations. STUCK joins PASS/FAIL in the
        terminal-skip set.
        """
        state = tmp_path / "state.json"
        ckpt_name = "kimi-k2.5"
        prior = {
            f"{ckpt_name}::seed1": self._make_prior(
                ckpt_name, 1, "STUCK", retry_count=3,
            ),
        }
        save_state(state, prior)
        kimi = next(c for c in CHECKPOINTS if c["name"] == ckpt_name)
        with patch("scripts.run_benchmark.subprocess.run") as mock_run:
            summary = run_grid(
                checkpoints=[kimi],
                seeds=[1],
                state_path=state,
                max_iter=10,
                timeout_s=600,
                dry_run=True,
                resume=True,
                ckpt_filter=None,
                seed_filter=None,
            )
        mock_run.assert_not_called()
        assert summary["skipped_resumed"] == 1
        assert summary["executed_this_run"] == 0

    def test_first_timeout_does_not_promote(self, tmp_path):
        """First TIMEOUT attempt = retry_count 1, NOT STUCK."""
        state = tmp_path / "state.json"
        ckpt_name = "minimax-m2.7"
        mini = next(c for c in CHECKPOINTS if c["name"] == ckpt_name)
        with patch(
            "scripts.run_benchmark.BENCHMARK_LOG_DIR", tmp_path,
        ), patch(
            "scripts.run_benchmark.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="x", timeout=10),
        ), patch(
            "scripts.run_benchmark.discover_run_artifacts",
            return_value=None,
        ):
            run_grid(
                checkpoints=[mini],
                seeds=[1],
                state_path=state,
                max_iter=2,
                timeout_s=10,
                dry_run=False,
                resume=True,
                ckpt_filter=None,
                seed_filter=None,
            )
        loaded = load_state(state)
        result = loaded[f"{ckpt_name}::seed1"]
        assert result.outcome == "TIMEOUT"
        assert result.timeout_retry_count == 0  # fresh result, no prior

    def test_recovery_resets_count(self, tmp_path):
        """A TIMEOUT followed by a PASS does NOT propagate the retry
        counter — recovered cells are clean again. (Implementation
        detail: execute_cell returns a fresh CellResult with
        timeout_retry_count=0 on success.)
        """
        state = tmp_path / "state.json"
        ckpt_name = "mimo-v2.5-pro"
        prior = {
            f"{ckpt_name}::seed1": self._make_prior(
                ckpt_name, 1, "TIMEOUT", retry_count=2,
            ),
        }
        save_state(state, prior)
        mimo = next(c for c in CHECKPOINTS if c["name"] == ckpt_name)
        completed = MagicMock(spec=subprocess.CompletedProcess)
        completed.returncode = 0
        transcript = tmp_path / "transcript_20260512_120000.jsonl"
        _write_jsonl(transcript, [
            {"iteration": 0, "role": "assistant", "content": "ok",
             "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                       "total_tokens": 15}},
        ])
        with patch(
            "scripts.run_benchmark.BENCHMARK_LOG_DIR", tmp_path,
        ), patch(
            "scripts.run_benchmark.subprocess.run", return_value=completed,
        ), patch(
            "scripts.run_benchmark.discover_run_artifacts",
            return_value=transcript,
        ):
            run_grid(
                checkpoints=[mimo],
                seeds=[1],
                state_path=state,
                max_iter=2,
                timeout_s=10,
                dry_run=False,
                resume=True,
                ckpt_filter=None,
                seed_filter=None,
            )
        loaded = load_state(state)
        result = loaded[f"{ckpt_name}::seed1"]
        assert result.outcome == "PASS"
        assert result.timeout_retry_count == 0


# ====================================================================== #
#  Pre-flight env-var sanity-check (Claude Code 2026-05-12)              #
# ====================================================================== #

class TestPreflightEnv:
    """Pre-flight check maps each enabled checkpoint to its API_KEY env
    var and bails before --execute if any are missing/empty. Critical:
    Gemini reads GOOGLE_API_KEY, NOT GEMINI_API_KEY (src/llm_client.py:218).
    """

    def test_env_var_map_correct_gemini_uses_google_key(self):
        assert ENV_VAR_BY_LLM["gemini"] == "GOOGLE_API_KEY"
        # Sanity: don't regress on the obvious-but-wrong name.
        assert "gemini" not in ENV_VAR_BY_LLM or \
            ENV_VAR_BY_LLM["gemini"] != "GEMINI_API_KEY"

    def test_env_var_map_covers_all_llms_in_checkpoints(self):
        """Every llm in CHECKPOINTS must have an entry in ENV_VAR_BY_LLM,
        else preflight will report 'unknown llm' on D4.
        """
        for c in CHECKPOINTS:
            assert c["llm"] in ENV_VAR_BY_LLM

    def test_preflight_missing_keys_returned(self, monkeypatch, tmp_path):
        """Empty env vars are reported. Patch out config/.env load so
        the test environment is the source of truth.
        """
        # Wipe all relevant keys
        for var in ENV_VAR_BY_LLM.values():
            monkeypatch.delenv(var, raising=False)
        # Block dotenv from re-populating from a real config/.env on disk
        monkeypatch.setattr(
            "scripts.run_benchmark.PROJECT_ROOT", tmp_path,
        )

        # Just one checkpoint, just one seed → only one llm needed
        kimi = next(c for c in CHECKPOINTS if c["llm"] == "kimi")
        cells = [(Cell(ckpt_name=kimi["name"], seed=1), kimi)]
        missing = preflight_env_check(cells)
        assert missing == ["KIMI_API_KEY"]

    def test_preflight_all_present_returns_empty(self, monkeypatch, tmp_path):
        for var in ENV_VAR_BY_LLM.values():
            monkeypatch.setenv(var, "test-value")
        monkeypatch.setattr(
            "scripts.run_benchmark.PROJECT_ROOT", tmp_path,
        )
        cells = [
            (Cell(ckpt_name=c["name"], seed=1), c)
            for c in CHECKPOINTS[:3]
        ]
        missing = preflight_env_check(cells)
        assert missing == []

    def test_preflight_only_checks_enabled_llms(
        self, monkeypatch, tmp_path,
    ):
        """If the grid is filtered to one checkpoint, we only check that
        checkpoint's env var — not the full ENV_VAR_BY_LLM map.
        """
        for var in ENV_VAR_BY_LLM.values():
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ok")
        monkeypatch.setattr(
            "scripts.run_benchmark.PROJECT_ROOT", tmp_path,
        )

        ds = next(c for c in CHECKPOINTS if c["name"] == "deepseek-v4-pro")
        cells = [(Cell(ckpt_name=ds["name"], seed=1), ds)]
        assert preflight_env_check(cells) == []


# ====================================================================== #
#  Stdout scrub through safe_bridge (codex_reviewer_v2 D3 P0/P1)         #
# ====================================================================== #

class TestStdoutScrub:
    def test_scrub_missing_file_returns_false(self, tmp_path):
        assert scrub_stdout_file(tmp_path / "nope.stdout") is False

    def test_scrub_present_file_returns_true(self, tmp_path):
        p = tmp_path / "x.stdout"
        p.write_text("hello world", encoding="utf-8")
        assert scrub_stdout_file(p) is True
        assert p.exists()  # not deleted on success

    def test_scrub_fail_closed_deletes_file(self, tmp_path, monkeypatch):
        """If scrub raises (e.g. import failure or scrub() blows up), we
        delete the stdout file rather than retain unscrubbed content.
        Fail-closed against the e750189c PDK leak class.
        """
        p = tmp_path / "x.stdout"
        p.write_text("unscrubbed content", encoding="utf-8")

        # Force the import inside scrub_stdout_file to raise so the
        # try/except hits the fail-closed branch.
        import builtins
        real_import = builtins.__import__

        def boom(name, *args, **kwargs):
            if name == "src.safe_bridge":
                raise ImportError("simulated")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", boom)
        assert scrub_stdout_file(p) is False
        assert not p.exists()  # fail-closed deleted


# ====================================================================== #
#  classify_subprocess_failure (D4 P3): operator-friendly fail_reason    #
# ====================================================================== #

class TestClassifySubprocessFailure:
    def test_missing_file_returns_none(self, tmp_path):
        assert classify_subprocess_failure(tmp_path / "nope.stdout") is None

    def test_no_pattern_returns_none(self, tmp_path):
        p = tmp_path / "boring.stdout"
        p.write_text("just normal logs, nothing wrong", encoding="utf-8")
        assert classify_subprocess_failure(p) is None

    def test_authentication_error_extracted(self, tmp_path):
        p = tmp_path / "auth.stdout"
        p.write_text(
            "Traceback (most recent call last):\n"
            "  ...\n"
            "openai.AuthenticationError: Error code: 401 - Invalid API Key\n",
            encoding="utf-8",
        )
        out = classify_subprocess_failure(p)
        assert out is not None
        assert "AuthenticationError" in out

    def test_rate_limit_error_extracted(self, tmp_path):
        p = tmp_path / "rl.stdout"
        p.write_text("openai.RateLimitError: 429 Too Many Requests\n", encoding="utf-8")
        out = classify_subprocess_failure(p)
        assert out is not None and "RateLimitError" in out

    def test_only_tail_is_scanned(self, tmp_path):
        """A pattern buried >8KB before EOF must NOT be matched — the
        helper reads only the tail to bound work on huge stdout files.
        """
        p = tmp_path / "long.stdout"
        # Pad with 10 KB of innocuous bytes, then end with pattern-free text.
        p.write_text("X" * 10_000 + "AuthenticationError leaked early\n"
                     + "Y" * 10_000 + "all clean\n", encoding="utf-8")
        # Only the trailing "Y" block + "all clean" is in tail → no match.
        assert classify_subprocess_failure(p) is None

    def test_truncates_to_160_chars(self, tmp_path):
        p = tmp_path / "long_reason.stdout"
        long_msg = "x" * 500
        p.write_text(f"AuthenticationError: {long_msg}\n", encoding="utf-8")
        out = classify_subprocess_failure(p)
        assert out is not None and len(out) <= 160


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
