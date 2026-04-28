"""CLI dispatch tests for ``scripts/run_agent.py --sim-backend hspice`` (T5).

Pure Python — HspiceWorker is mocked so no real ssh / hspice is touched.
Exercises:

  - ``parse_args`` backend-gated argument validation
  - ``_run_hspice`` exit codes:
      0 all metrics PASS
      1 config / spec error (missing YAML block, missing env, missing file)
      2 HspiceWorker Timeout / Spawn / Script error
      3 HspiceMetricNotFoundError (metric absent from every .mt<k>)
      4 simulation ran + parsed, at least one metric FAIL / UNMEASURABLE
  - Default backend is ``spectre`` when ``--sim-backend`` is omitted
  - The hspice branch never reaches the spectre Virtuoso/OCEAN plumbing

Tests write a transient spec fixture under ``tmp_path`` and pass
``--env-file`` at a nonexistent path so no stray ``config/.env`` bleeds
into the test process environment.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from src.hspice_worker import (  # noqa: E402
    HspiceRunResult,
    HspiceWorkerError,
    HspiceWorkerScriptError,
    HspiceWorkerSpawnError,
    HspiceWorkerTimeout,
)
from src.parse_mt0 import Mt0Result  # noqa: E402

import run_agent  # noqa: E402


# ---------------------------------------------------------------------- #
# Fixtures
# ---------------------------------------------------------------------- #


_SPEC_WITH_YAML = """\
# Test spec

Some prose.

```yaml
signals:
  - {name: Vdiff, kind: Vdiff, paths: ["/Vout_p", "/Vout_n"]}
windows:
  late: [1.5e-7, 2.0e-7]
metrics:
  - {name: my_measure, signal: Vdiff, window: late, stat: rms,
     pass: [0.0, 10.0]}
  - {name: other_measure, signal: Vdiff, window: late, stat: rms,
     pass: [0.0, 5.0]}
```
"""


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    p = tmp_path / "spec.md"
    p.write_text(_SPEC_WITH_YAML, encoding="utf-8")
    return p


@pytest.fixture
def env_stub(tmp_path: Path) -> str:
    # Point --env-file at a path that doesn't exist so load_dotenv is
    # a no-op. Prevents the test-runner environment from being
    # contaminated by a stray config/.env.
    return str(tmp_path / "nothing.env")


def _mt0(columns: list[str], rows: list[list[float]], alter: int = 1) -> Mt0Result:
    return Mt0Result(
        header={"source": "HSPICE", "version": "V1"},
        title="t",
        columns=columns,
        rows=rows,
        alter_number=alter,
    )


def _run_result(mt_files: dict[str, Mt0Result]) -> HspiceRunResult:
    return HspiceRunResult(
        returncode=0,
        stdout_scrubbed="",
        stderr_scrubbed="",
        mt_files=mt_files,
        lis_scrubbed=None,
        run_dir_remote="/tmp/hsp",
        sp_base="sim",
    )


def _mock_worker(
    run_return: HspiceRunResult | None = None,
    run_raises: Exception | None = None,
) -> mock.Mock:
    w = mock.Mock()
    w.cfg = mock.Mock(
        remote_host="testhost",
        hspice_bin="/apps/hspice",
        hard_ceiling_s=14400.0,
        idle_timeout_s=600.0,
    )
    if run_raises is not None:
        w.run.side_effect = run_raises
    else:
        w.run.return_value = run_return if run_return is not None else _run_result({})
    return w


def _hspice_argv(spec_file: Path, env_stub: str, netlist: str = "/tmp/sim.sp") -> list[str]:
    return [
        "run_agent",
        "--sim-backend", "hspice",
        "--netlist", netlist,
        "--spec", str(spec_file),
        "--env-file", env_stub,
    ]


# ---------------------------------------------------------------------- #
# parse_args — backend-gated arg validation
# ---------------------------------------------------------------------- #


class TestParseArgs:
    def test_spectre_requires_lib_cell_tb(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "argv", [
            "run_agent", "--sim-backend", "spectre",
            "--spec", str(tmp_path / "x"),
        ])
        with pytest.raises(SystemExit):
            run_agent.parse_args()

    def test_hspice_requires_netlist(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "argv", [
            "run_agent", "--sim-backend", "hspice",
            "--spec", str(tmp_path / "x"),
        ])
        with pytest.raises(SystemExit):
            run_agent.parse_args()

    def test_hspice_happy_parse(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "argv", [
            "run_agent", "--sim-backend", "hspice",
            "--netlist", "/tmp/s.sp",
            "--spec", str(tmp_path / "x"),
        ])
        args = run_agent.parse_args()
        assert args.sim_backend == "hspice"
        assert args.netlist == "/tmp/s.sp"
        # --lib / --cell / --tb-cell are optional in hspice mode.
        assert args.lib is None

    def test_default_backend_is_spectre(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "argv", [
            "run_agent",
            "--lib", "L", "--cell", "C", "--tb-cell", "T",
            "--spec", str(tmp_path / "x"),
        ])
        args = run_agent.parse_args()
        assert args.sim_backend == "spectre"
        assert args.netlist is None


# ---------------------------------------------------------------------- #
# main() — hspice dispatch: happy / FAIL / not-found paths
# ---------------------------------------------------------------------- #


class TestHspiceDispatchHappy:
    def test_all_pass_returns_rc_0_and_prints_pass(
        self, monkeypatch, capsys, spec_file, env_stub,
    ):
        mt_files = {"sim.mt0": _mt0(
            ["my_measure", "other_measure", "temper", "alter#"],
            [[5.0, 3.0, 25.0, 1.0]],
        )}
        worker = _mock_worker(_run_result(mt_files))
        monkeypatch.setattr(sys, "argv", _hspice_argv(spec_file, env_stub))

        with mock.patch("src.hspice_worker.worker_from_env", return_value=worker):
            rc = run_agent.main()

        assert rc == 0
        out = capsys.readouterr().out
        assert "HSPICE RESULTS" in out
        assert "my_measure" in out
        assert "other_measure" in out
        assert "PASS" in out

    def test_worker_run_called_with_exact_netlist_arg(
        self, monkeypatch, spec_file, env_stub,
    ):
        mt_files = {"sim.mt0": _mt0(
            ["my_measure", "other_measure", "temper", "alter#"],
            [[5.0, 3.0, 25.0, 1.0]],
        )}
        worker = _mock_worker(_run_result(mt_files))
        monkeypatch.setattr(sys, "argv", _hspice_argv(
            spec_file, env_stub, netlist="/proj/x/sim.sp",
        ))
        with mock.patch("src.hspice_worker.worker_from_env", return_value=worker):
            run_agent.main()
        worker.run.assert_called_once_with("/proj/x/sim.sp")


class TestHspiceDispatchFail:
    def test_metric_above_pass_hi_returns_rc_4(
        self, monkeypatch, capsys, spec_file, env_stub,
    ):
        mt_files = {"sim.mt0": _mt0(
            ["my_measure", "other_measure", "temper", "alter#"],
            [[99.0, 3.0, 25.0, 1.0]],   # 99 > pass hi 10 for my_measure
        )}
        worker = _mock_worker(_run_result(mt_files))
        monkeypatch.setattr(sys, "argv", _hspice_argv(spec_file, env_stub))
        with mock.patch("src.hspice_worker.worker_from_env", return_value=worker):
            rc = run_agent.main()
        assert rc == 4
        out = capsys.readouterr().out
        assert "FAIL" in out

    def test_metric_missing_from_every_mt_returns_rc_3(
        self, monkeypatch, spec_file, env_stub, caplog,
    ):
        mt_files = {"sim.mt0": _mt0(
            ["delay", "temper", "alter#"],  # spec expects my_measure
            [[1.0e-9, 25.0, 1.0]],
        )}
        worker = _mock_worker(_run_result(mt_files))
        monkeypatch.setattr(sys, "argv", _hspice_argv(spec_file, env_stub))
        with mock.patch("src.hspice_worker.worker_from_env", return_value=worker):
            with caplog.at_level("ERROR"):
                rc = run_agent.main()
        assert rc == 3


# ---------------------------------------------------------------------- #
# main() — hspice dispatch: worker errors map to rc=2
# ---------------------------------------------------------------------- #


class TestHspiceDispatchWorkerErrors:
    def test_timeout_returns_rc_2(
        self, monkeypatch, spec_file, env_stub,
    ):
        worker = _mock_worker(run_raises=HspiceWorkerTimeout("hit budget"))
        monkeypatch.setattr(sys, "argv", _hspice_argv(spec_file, env_stub))
        with mock.patch("src.hspice_worker.worker_from_env", return_value=worker):
            assert run_agent.main() == 2

    def test_spawn_error_returns_rc_2(
        self, monkeypatch, spec_file, env_stub,
    ):
        worker = _mock_worker(run_raises=HspiceWorkerSpawnError("ssh dead"))
        monkeypatch.setattr(sys, "argv", _hspice_argv(spec_file, env_stub))
        with mock.patch("src.hspice_worker.worker_from_env", return_value=worker):
            assert run_agent.main() == 2

    def test_script_error_returns_rc_2(
        self, monkeypatch, spec_file, env_stub,
    ):
        worker = _mock_worker(run_raises=HspiceWorkerScriptError("bad mt0"))
        monkeypatch.setattr(sys, "argv", _hspice_argv(spec_file, env_stub))
        with mock.patch("src.hspice_worker.worker_from_env", return_value=worker):
            assert run_agent.main() == 2

    def test_env_config_error_returns_rc_1(
        self, monkeypatch, spec_file, env_stub,
    ):
        monkeypatch.setattr(sys, "argv", _hspice_argv(spec_file, env_stub))
        with mock.patch(
            "src.hspice_worker.worker_from_env",
            side_effect=HspiceWorkerError("VB_REMOTE_HOST missing"),
        ):
            assert run_agent.main() == 1


# ---------------------------------------------------------------------- #
# main() — hspice dispatch: spec-side errors
# ---------------------------------------------------------------------- #


class TestHspiceDispatchSpecErrors:
    def test_spec_without_yaml_block_returns_rc_1_without_touching_worker(
        self, monkeypatch, tmp_path, env_stub,
    ):
        plain_spec = tmp_path / "plain.md"
        plain_spec.write_text("# just prose, no yaml block here")
        monkeypatch.setattr(sys, "argv", [
            "run_agent", "--sim-backend", "hspice",
            "--netlist", "/tmp/sim.sp",
            "--spec", str(plain_spec),
            "--env-file", env_stub,
        ])
        with mock.patch("src.hspice_worker.worker_from_env") as m:
            rc = run_agent.main()
        assert rc == 1
        m.assert_not_called()

    def test_missing_spec_file_returns_rc_1(self, monkeypatch, tmp_path, env_stub):
        monkeypatch.setattr(sys, "argv", [
            "run_agent", "--sim-backend", "hspice",
            "--netlist", "/tmp/sim.sp",
            "--spec", str(tmp_path / "absent.md"),
            "--env-file", env_stub,
        ])
        assert run_agent.main() == 1


# ---------------------------------------------------------------------- #
# main() — isolation: hspice branch does NOT hit spectre plumbing
# ---------------------------------------------------------------------- #


class TestHspiceDispatchIsolation:
    def test_hspice_branch_does_not_call_ocean_worker(
        self, monkeypatch, spec_file, env_stub,
    ):
        mt_files = {"sim.mt0": _mt0(
            ["my_measure", "other_measure", "temper", "alter#"],
            [[5.0, 3.0, 25.0, 1.0]],
        )}
        worker = _mock_worker(_run_result(mt_files))
        monkeypatch.setattr(sys, "argv", _hspice_argv(spec_file, env_stub))
        # If the hspice branch leaks into the spectre path, these mocks
        # would fire — they must NOT be called.
        with mock.patch("src.hspice_worker.worker_from_env", return_value=worker), \
             mock.patch("run_agent.worker_from_env") as ocean_m, \
             mock.patch("run_agent.VirtuosoClient") as vc_m:
            rc = run_agent.main()
        assert rc == 0
        ocean_m.assert_not_called()
        vc_m.from_env.assert_not_called()


# ---------------------------------------------------------------------- #
# main() — T5 R2 codex blockers
# ---------------------------------------------------------------------- #


class TestHspiceDispatchR2Blockers:
    """Regressions for the two codex blockers on T5 R1 review.

    B1 — ``HspiceWorker.run`` raises ``ValueError`` when T3's path
         defense rejects a shlex-unsafe / shape-invalid ``--netlist``
         (leading dash, shell metachars, empty, etc.). Before this
         rework ``_run_hspice`` had no ``except ValueError`` clause,
         so the exception propagated all the way to run_agent's
         caller with a bare traceback. R2 routes it to ``rc=1`` and
         keeps the log line human-friendly.

    B2 — ``HspiceMetricNotFoundError.__str__`` is deliberately
         privacy-safe in T4: it reports the *count* of available
         columns, not their names (column names can leak node labels
         from a customer's netlist). The R1 rc=3 log branch dumped
         ``exc.available[:20]`` which voids that contract. R2 keeps
         the log to ``metric_name`` + count only.
    """

    def test_invalid_netlist_shape_returns_rc_1_no_traceback(
        self, monkeypatch, spec_file, env_stub, caplog,
    ):
        worker = _mock_worker(
            run_raises=ValueError(
                "netlist path refuses leading '-' (shlex injection guard)"
            ),
        )
        monkeypatch.setattr(sys, "argv", _hspice_argv(spec_file, env_stub))
        caplog.set_level("DEBUG")
        with mock.patch("src.hspice_worker.worker_from_env", return_value=worker):
            rc = run_agent.main()
        # Converted to a config error, not a transport error.
        assert rc == 1
        joined = "\n".join(r.getMessage() for r in caplog.records)
        # No bare traceback leaked through run_agent's logging channel.
        assert "Traceback" not in joined
        # The user-facing error identifies the offending input class.
        assert any(
            "netlist" in r.getMessage().lower() for r in caplog.records
        )

    def test_invalid_netlist_raw_path_not_leaked_in_logs(
        self, monkeypatch, spec_file, env_stub, caplog,
    ):
        # R2 left ``logger.error("Invalid --netlist argument: %s", exc)``
        # which re-inserts the offending path into the log because
        # ``HspiceWorker`` shapes its ValueErrors like
        # ``...got '/tmp/customer_secret/-evil.sp'``. R3 drops the
        # payload and emits a category-only message. This test pins
        # that behaviour so a future refactor can't re-introduce the
        # leak by ``% exc``-ing the exception back in.
        raw_path = "/tmp/customer_secret/-evil.sp"
        worker = _mock_worker(
            run_raises=ValueError(
                "netlist basename refuses leading '-' "
                f"(option injection guard); got {raw_path!r}"
            ),
        )
        monkeypatch.setattr(sys, "argv", _hspice_argv(spec_file, env_stub))
        caplog.set_level("DEBUG")
        with mock.patch("src.hspice_worker.worker_from_env", return_value=worker):
            rc = run_agent.main()
        assert rc == 1
        text = caplog.text
        # None of the injection-probe fragments reach the log channel.
        for fragment in (raw_path, "-evil.sp", "customer_secret"):
            assert fragment not in text, (
                f"raw netlist fragment {fragment!r} leaked to logs — "
                f"R3 contract says category-only, never exc payload"
            )
        # The operator still sees a useful, human-shaped error.
        assert "Invalid --netlist argument" in text

    def test_rc_3_does_not_leak_available_columns(
        self, monkeypatch, spec_file, env_stub, caplog,
    ):
        from src.hspice_resolver import HspiceMetricNotFoundError

        # Realistic node-flavoured column names that must NOT surface
        # in logs per T4's privacy contract.
        leaky_cols = [
            "customer_secret_node",
            "internal_tap_42",
            "confidential_rail",
        ]
        worker = _mock_worker(_run_result({
            "sim.mt0": _mt0(leaky_cols, [[1.0, 2.0, 3.0]]),
        }))
        monkeypatch.setattr(sys, "argv", _hspice_argv(spec_file, env_stub))
        caplog.set_level("DEBUG")
        with mock.patch(
            "src.hspice_worker.worker_from_env", return_value=worker,
        ), mock.patch(
            "src.hspice_resolver.evaluate_hspice",
            side_effect=HspiceMetricNotFoundError(
                "my_measure", leaky_cols,
            ),
        ):
            rc = run_agent.main()
        assert rc == 3
        joined = "\n".join(r.getMessage() for r in caplog.records)
        for col in leaky_cols:
            assert col not in joined, (
                f"column name {col!r} leaked to logs — T4 privacy "
                f"contract says count only, never names"
            )
        # The log is still useful: the metric name and the count
        # of hidden columns are both present.
        assert "my_measure" in joined
        assert str(len(leaky_cols)) in joined
