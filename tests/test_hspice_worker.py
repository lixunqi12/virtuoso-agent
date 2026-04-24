"""Unit tests for src/hspice_worker.py.

Pure-Python tests — subprocess.run is mocked so no real ssh / hspice is
touched. Pattern mirrors tests/test_ocean_worker_spec.py (the only
pytest-collected module for ocean_worker; the `_smoke` / `_timeout`
siblings are live-SSH scripts run via `__main__`).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src import hspice_worker  # noqa: E402
from src.hspice_scrub import ScrubError  # noqa: E402
from src.hspice_worker import (  # noqa: E402
    HspiceRunResult,
    HspiceWorker,
    HspiceWorkerConfig,
    HspiceWorkerScriptError,
    HspiceWorkerSpawnError,
    HspiceWorkerTimeout,
    worker_from_env,
)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _cfg() -> HspiceWorkerConfig:
    return HspiceWorkerConfig(
        remote_host="remotehost",
        remote_user="alice",
        hspice_bin="/apps/hspice/bin/hspice",
        remote_tmp_dir="/tmp",
        wall_timeout_s=30.0,
        ssh_connect_timeout_s=5,
    )


# Minimal valid .mt0 content (parseable, no PDK tokens).
_MT0_BODY = (
    "$DATA1 SOURCE='HSPICE' VERSION='V1' PARAM_COUNT=0\n"
    ".TITLE 'clean'\n"
    "delay temper alter#\n"
    "1.0e-9 25.0 1.0\n"
)

# Second-alter .mt1
_MT1_BODY = (
    "$DATA1 SOURCE='HSPICE' VERSION='V1' PARAM_COUNT=0\n"
    ".TITLE 'clean_alter2'\n"
    "delay temper alter#\n"
    "1.2e-9 25.0 2.0\n"
)

# Third-alter .mt2
_MT2_BODY = (
    "$DATA1 SOURCE='HSPICE' VERSION='V1' PARAM_COUNT=0\n"
    ".TITLE 'clean_alter3'\n"
    "delay temper alter#\n"
    "1.4e-9 25.0 3.0\n"
)


def _ok(stdout: str = "", stderr: str = "", rc: int = 0) -> CompletedProcess:
    return CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


# --------------------------------------------------------------------------
# Config + env
# --------------------------------------------------------------------------


class TestConfig:
    def test_ssh_base_args_shape(self):
        cfg = _cfg()
        args = cfg.ssh_base_args()
        assert args[0] == "ssh"
        assert "BatchMode=yes" in args
        assert "ConnectTimeout=5" in args
        assert args[-1] == "alice@remotehost"

    def test_ssh_target(self):
        assert _cfg().ssh_target() == "alice@remotehost"


class TestWorkerFromEnv:
    def test_requires_host_and_user(self, monkeypatch):
        monkeypatch.delenv("VB_REMOTE_HOST", raising=False)
        monkeypatch.delenv("VB_REMOTE_USER", raising=False)
        with pytest.raises(hspice_worker.HspiceWorkerError):
            worker_from_env()

    def test_happy(self, monkeypatch):
        monkeypatch.setenv("VB_REMOTE_HOST", "somehost")
        monkeypatch.setenv("VB_REMOTE_USER", "bob")
        monkeypatch.setenv("VB_HSPICE_BIN", "/opt/hspice")
        monkeypatch.setenv("VB_HSPICE_TIMEOUT_S", "120")
        w = worker_from_env()
        assert isinstance(w, HspiceWorker)
        assert w.cfg.remote_host == "somehost"
        assert w.cfg.remote_user == "bob"
        assert w.cfg.hspice_bin == "/opt/hspice"
        assert w.cfg.wall_timeout_s == 120.0


# --------------------------------------------------------------------------
# sp_path validation
# --------------------------------------------------------------------------


class TestSpPathValidation:
    @pytest.fixture
    def worker(self):
        return HspiceWorker(_cfg())

    def test_rejects_relative_path(self, worker):
        with pytest.raises(ValueError):
            worker.run("sim.sp")

    def test_rejects_non_sp_extension(self, worker):
        with pytest.raises(ValueError):
            worker.run("/tmp/sim.txt")

    def test_rejects_dotdot(self, worker):
        with pytest.raises(ValueError, match=r"\.\."):
            worker.run("/tmp/../etc/sim.sp")

    def test_rejects_shell_meta_semicolon(self, worker):
        with pytest.raises(ValueError):
            worker.run("/tmp/sim;rm.sp")

    def test_rejects_shell_meta_space(self, worker):
        with pytest.raises(ValueError):
            worker.run("/tmp/sim file.sp")

    def test_rejects_non_str(self, worker):
        with pytest.raises(ValueError):
            worker.run(None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Success paths
# --------------------------------------------------------------------------


class TestRunSuccess:
    def _run_with(self, side_effect):
        worker = HspiceWorker(_cfg())
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ) as m:
            return worker.run("/tmp/hsp/sim.sp", timeout_sec=10.0), m

    def test_single_mt0_parsed_and_returned(self):
        side_effect = [
            _ok(stdout="hspice banner", stderr=""),  # spawn
            _ok(stdout="sim.mt0\n"),                 # list
            _ok(stdout=_MT0_BODY),                   # cat mt0
            _ok(),                                   # cleanup
        ]
        result, _m = self._run_with(side_effect)
        assert isinstance(result, HspiceRunResult)
        assert result.returncode == 0
        assert set(result.mt_files.keys()) == {"sim.mt0"}
        mt = result.mt_files["sim.mt0"]
        assert mt.title == "clean"
        assert mt.alter_number == 1
        assert mt.rows[0][0] == pytest.approx(1.0e-9)
        assert result.run_dir_remote == "/tmp/hsp"
        assert result.sp_base == "sim"

    def test_multiple_mtN_all_parsed(self):
        side_effect = [
            _ok(),                                                   # spawn
            _ok(stdout="sim.mt0\nsim.mt1\nsim.mt2\n"),                # list
            _ok(stdout=_MT0_BODY),                                   # cat mt0
            _ok(stdout=_MT1_BODY),                                   # cat mt1
            _ok(stdout=_MT2_BODY),                                   # cat mt2
            _ok(),                                                   # cleanup
        ]
        result, _m = self._run_with(side_effect)
        assert set(result.mt_files.keys()) == {"sim.mt0", "sim.mt1", "sim.mt2"}
        assert result.mt_files["sim.mt0"].alter_number == 1
        assert result.mt_files["sim.mt1"].alter_number == 2
        assert result.mt_files["sim.mt2"].alter_number == 3

    def test_lis_fetched_and_scrubbed(self):
        lis_raw = "hspice listing\n.include 'safe_model.lib'\n"
        side_effect = [
            _ok(),                                  # spawn
            _ok(stdout="sim.mt0\nsim.lis\n"),       # list
            _ok(stdout=_MT0_BODY),                  # cat mt0
            _ok(stdout=lis_raw),                    # cat lis
            _ok(),                                  # cleanup
        ]
        result, _m = self._run_with(side_effect)
        assert result.lis_scrubbed is not None
        # scrub_lis must have been applied — "hspice listing" is a safe
        # token so it survives.
        assert "hspice listing" in result.lis_scrubbed

    def test_stdout_and_stderr_are_scrubbed_not_empty(self):
        side_effect = [
            _ok(stdout="stdout banner\n", stderr="stderr line\n"),
            _ok(stdout="sim.mt0\n"),
            _ok(stdout=_MT0_BODY),
            _ok(),
        ]
        result, _m = self._run_with(side_effect)
        assert "stdout banner" in result.stdout_scrubbed
        assert "stderr line" in result.stderr_scrubbed

    def test_run_with_nonzero_hspice_rc_still_returns_result(self):
        # hspice rc=1 (simulation failed) is NOT a transport error. The
        # worker returns what output is available and the caller checks
        # returncode. Only rc=255 (ssh transport) raises.
        side_effect = [
            _ok(rc=1, stdout="hspice warning\n"),
            _ok(stdout="sim.mt0\n"),
            _ok(stdout=_MT0_BODY),
            _ok(),
        ]
        result, _m = self._run_with(side_effect)
        assert result.returncode == 1
        assert "sim.mt0" in result.mt_files

    def test_spawn_command_runs_hspice_with_basename(self):
        side_effect = [
            _ok(),                  # spawn
            _ok(stdout=""),         # list (nothing)
            _ok(),                  # cleanup
        ]
        _result, m = self._run_with(side_effect)
        # First call = spawn. Last arg to ssh is the remote bash command.
        spawn_cmd = m.call_args_list[0].args[0]
        remote = spawn_cmd[-1]
        assert "hspice" in remote
        # shlex.quote drops the quotes when the arg has no shell meta,
        # so "sim.sp" comes through bare — still correct because it's
        # passed to the shell as a single whitespace-separated token.
        assert "sim.sp" in remote
        assert "cd /tmp/hsp" in remote
        assert "echo $$ >" in remote  # pidfile is written before exec

    def test_empty_output_list_returns_result_with_empty_mt_files(self):
        side_effect = [
            _ok(),                   # spawn
            _ok(stdout=""),          # list returns nothing
            _ok(),                   # cleanup
        ]
        result, _m = self._run_with(side_effect)
        assert result.mt_files == {}
        assert result.lis_scrubbed is None


# --------------------------------------------------------------------------
# .tr* exclusion
# --------------------------------------------------------------------------


class TestTrExclusion:
    def test_list_filters_out_tr_files_even_if_shell_returned_them(self):
        # Defense-in-depth: if ls somehow leaks a .tr0, the worker must
        # still not fetch it. Simulate a bogus ls that includes sim.tr0.
        worker = HspiceWorker(_cfg())
        side_effect = [
            _ok(),                                    # spawn
            _ok(stdout="sim.mt0\nsim.tr0\nsim.lis\n"),  # list includes tr0
            _ok(stdout=_MT0_BODY),                    # cat mt0
            _ok(stdout="lis body"),                   # cat lis
            _ok(),                                    # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ) as m:
            result = worker.run("/tmp/hsp/sim.sp")
        # Make sure none of the 5 subprocess calls ever cat'd sim.tr0.
        for call in m.call_args_list:
            remote = call.args[0][-1]
            assert "sim.tr0" not in remote, f"tr0 leaked into: {remote!r}"
        assert "sim.tr0" not in result.mt_files

    def test_fetch_file_refuses_tr_path_directly(self):
        worker = HspiceWorker(_cfg())
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            return_value=_ok(stdout="never read"),
        ):
            with pytest.raises(HspiceWorkerScriptError, match="waveform"):
                worker._fetch_file("/tmp/hsp/sim.tr0")

    def test_list_glob_does_not_contain_tr(self):
        # Inspect the ls command to confirm the glob itself never
        # matches .tr files.
        worker = HspiceWorker(_cfg())
        side_effect = [
            _ok(),
            _ok(stdout=""),
            _ok(),
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ) as m:
            worker.run("/tmp/hsp/sim.sp")
        ls_cmd = m.call_args_list[1].args[0][-1]
        assert ".tr" not in ls_cmd
        assert ".mt[0-9]" in ls_cmd
        assert ".lis" in ls_cmd


# --------------------------------------------------------------------------
# Timeout + kill
# --------------------------------------------------------------------------


class TestTimeout:
    def test_timeout_raises_and_issues_kill_and_cleanup(self):
        worker = HspiceWorker(_cfg())
        spawn_to = subprocess.TimeoutExpired(cmd=["ssh"], timeout=3.0)
        side_effect = [
            spawn_to,                             # spawn raises
            _ok(stdout="killed 12345\n"),         # remote kill
            _ok(),                                # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ) as m:
            with pytest.raises(HspiceWorkerTimeout):
                worker.run("/tmp/hsp/sim.sp", timeout_sec=3.0)
        # 3 subprocess calls: spawn, kill, cleanup
        assert m.call_count == 3
        kill_cmd = m.call_args_list[1].args[0][-1]
        assert "kill -9" in kill_cmd
        assert "pkill -9 -P" in kill_cmd
        # cleanup still fires in `finally`.
        cleanup_cmd = m.call_args_list[2].args[0][-1]
        assert "rm -f" in cleanup_cmd
        assert "hspice_pid_" in cleanup_cmd

    def test_timeout_preserves_pidfile_in_kill_command(self):
        worker = HspiceWorker(_cfg())
        spawn_to = subprocess.TimeoutExpired(cmd=["ssh"], timeout=3.0)
        side_effect = [spawn_to, _ok(stdout="killed\n"), _ok()]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ) as m:
            with pytest.raises(HspiceWorkerTimeout):
                worker.run("/tmp/hsp/sim.sp", timeout_sec=3.0)
        spawn_cmd = m.call_args_list[0].args[0][-1]
        kill_cmd = m.call_args_list[1].args[0][-1]
        # Both reference the same run_id-keyed pidfile.
        import re as _re
        spawn_pid = _re.search(r"hspice_pid_[a-f0-9]+", spawn_cmd)
        kill_pid = _re.search(r"hspice_pid_[a-f0-9]+", kill_cmd)
        assert spawn_pid is not None
        assert kill_pid is not None
        assert spawn_pid.group(0) == kill_pid.group(0)


# --------------------------------------------------------------------------
# Transport / script errors
# --------------------------------------------------------------------------


class TestErrors:
    def test_ssh_rc_255_raises_spawn_error(self):
        worker = HspiceWorker(_cfg())
        side_effect = [
            _ok(rc=255, stderr="Connection refused\n"),  # spawn ssh-level error
            _ok(stdout="killed\n"),                       # best-effort kill
            _ok(),                                        # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ):
            with pytest.raises(HspiceWorkerSpawnError):
                worker.run("/tmp/hsp/sim.sp")

    def test_fetch_nonzero_rc_raises_script_error(self):
        worker = HspiceWorker(_cfg())
        side_effect = [
            _ok(),                                 # spawn
            _ok(stdout="sim.mt0\n"),               # list
            _ok(rc=1, stderr="cat: nope"),         # fetch fails
            _ok(),                                 # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ):
            with pytest.raises(HspiceWorkerScriptError, match="fetch"):
                worker.run("/tmp/hsp/sim.sp")

    def test_list_ssh_rc_nonzero_raises_spawn_error(self):
        worker = HspiceWorker(_cfg())
        side_effect = [
            _ok(),                             # spawn
            _ok(rc=1, stderr="ssh died"),      # list fails at ssh level
            _ok(),                             # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ):
            with pytest.raises(HspiceWorkerSpawnError, match="list outputs"):
                worker.run("/tmp/hsp/sim.sp")

    def test_mt0_parse_error_wraps_into_script_error(self):
        worker = HspiceWorker(_cfg())
        # Malformed .mt0 (missing $DATA1 header) — parse_mt0 raises,
        # worker wraps into HspiceWorkerScriptError.
        bad_mt0 = "garbage line\n.TITLE 'x'\ndelay temper alter#\n1.0 25.0 1.0\n"
        side_effect = [
            _ok(),                           # spawn
            _ok(stdout="sim.mt0\n"),         # list
            _ok(stdout=bad_mt0),             # cat (parses bad)
            _ok(),                           # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ):
            with pytest.raises(HspiceWorkerScriptError, match="failed to parse"):
                worker.run("/tmp/hsp/sim.sp")

    def test_scrub_error_wraps_into_script_error(self):
        worker = HspiceWorker(_cfg())
        # Patch scrub_mt0 to raise ScrubError — simulates a foundry
        # token surviving the scrub (shouldn't happen in practice but
        # we must handle it).
        side_effect = [
            _ok(),                           # spawn
            _ok(stdout="sim.mt0\n"),         # list
            _ok(stdout=_MT0_BODY),           # cat
            _ok(),                           # cleanup
        ]
        fake_scrub_err = ScrubError(
            ["nch_lvt_foo"], stage="mt0", counts={"foundry_seed": 1},
        )
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ):
            with mock.patch(
                "src.hspice_worker.scrub_mt0",
                side_effect=fake_scrub_err,
            ):
                with pytest.raises(HspiceWorkerScriptError, match="scrub failed"):
                    worker.run("/tmp/hsp/sim.sp")


# --------------------------------------------------------------------------
# Cleanup always runs
# --------------------------------------------------------------------------


class TestCleanup:
    def test_cleanup_fires_on_success(self):
        worker = HspiceWorker(_cfg())
        side_effect = [
            _ok(), _ok(stdout=""), _ok(),  # spawn, list(empty), cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ) as m:
            worker.run("/tmp/hsp/sim.sp")
        assert m.call_count == 3
        last = m.call_args_list[-1].args[0][-1]
        assert "rm -f" in last

    def test_cleanup_fires_on_timeout(self):
        worker = HspiceWorker(_cfg())
        side_effect = [
            subprocess.TimeoutExpired(cmd=["ssh"], timeout=3.0),
            _ok(stdout="killed\n"),
            _ok(),  # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ) as m:
            with pytest.raises(HspiceWorkerTimeout):
                worker.run("/tmp/hsp/sim.sp", timeout_sec=3.0)
        last = m.call_args_list[-1].args[0][-1]
        assert "rm -f" in last

    def test_cleanup_fires_on_script_error(self):
        worker = HspiceWorker(_cfg())
        side_effect = [
            _ok(rc=255, stderr="boom"),  # spawn transport error
            _ok(stdout="killed\n"),       # best-effort kill on rc=255
            _ok(),                        # cleanup still runs
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ) as m:
            with pytest.raises(HspiceWorkerSpawnError):
                worker.run("/tmp/hsp/sim.sp")
        last = m.call_args_list[-1].args[0][-1]
        assert "rm -f" in last


# --------------------------------------------------------------------------
# R2 rework blockers: option-injection defense + rc=255 zombie kill
# --------------------------------------------------------------------------


class TestR2Blockers:
    """Codex T3 R2 blockers:

    R1 sp_base leading-dash option injection — three layers of defense:
      - a. basename regex rejects leading '-'
      - b. spawn command prefixes the basename with './'
      - c. ls command uses '--' to terminate flag parsing
    R2 ssh rc=255 zombie window — kill remote pid before raising, since
        the wrapper may have already written the pidfile and exec'd
        hspice before the transport dropped.
    """

    def test_r1a_regex_rejects_leading_dash_basename(self):
        # `/tmp/foo/-evil.sp` survives the old path regex but the
        # basename still starts with '-'. Must be rejected up-front —
        # this is the first of three defense layers.
        worker = HspiceWorker(_cfg())
        with pytest.raises(ValueError, match="leading dash"):
            worker.run("/tmp/foo/-evil.sp")

    def test_r1b_spawn_prefixes_basename_with_dot_slash(self):
        # Even with the regex blocking `-evil.sp`, the spawn command
        # must still prefix the filename with `./` so a future regex
        # bypass cannot feed a dash-leading name to hspice.
        worker = HspiceWorker(_cfg())
        side_effect = [
            _ok(),                    # spawn
            _ok(stdout=""),           # list (empty)
            _ok(),                    # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ) as m:
            worker.run("/tmp/hsp/sim.sp")
        spawn_cmd = m.call_args_list[0].args[0][-1]
        # The hspice argument must be './sim.sp', not bare 'sim.sp'.
        assert "./sim.sp" in spawn_cmd
        # And the ordering must be: cd into run_dir FIRST, then exec.
        cd_ix = spawn_cmd.index("cd /tmp/hsp")
        exec_ix = spawn_cmd.index("./sim.sp")
        assert cd_ix < exec_ix

    def test_r1c_ls_uses_double_dash_terminator(self):
        # ls must use `--` to terminate flag parsing, so a stray
        # leading-dash basename cannot be misread as a flag.
        worker = HspiceWorker(_cfg())
        side_effect = [
            _ok(),                    # spawn
            _ok(stdout=""),           # list (empty)
            _ok(),                    # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ) as m:
            worker.run("/tmp/hsp/sim.sp")
        ls_cmd = m.call_args_list[1].args[0][-1]
        assert "ls -1 --" in ls_cmd

    def test_r2_rc255_kills_remote_before_raising(self):
        # rc=255 means ssh transport dropped — but the wrapper may
        # already have written the pidfile and exec'd hspice. The
        # worker must call _kill_remote *before* raising, using the
        # same pidfile the spawn wrapper wrote.
        worker = HspiceWorker(_cfg())
        side_effect = [
            _ok(rc=255, stderr="Broken pipe\n"),   # spawn ssh drop
            _ok(stdout="killed 4321\n"),           # best-effort kill
            _ok(),                                  # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ) as m:
            with pytest.raises(HspiceWorkerSpawnError):
                worker.run("/tmp/hsp/sim.sp")
        # Exactly 3 subprocess calls: spawn, kill, cleanup.
        assert m.call_count == 3
        spawn_cmd = m.call_args_list[0].args[0][-1]
        kill_cmd = m.call_args_list[1].args[0][-1]
        cleanup_cmd = m.call_args_list[2].args[0][-1]
        # Kill must actually be the kill command, not cleanup.
        assert "kill -9" in kill_cmd
        assert "pkill -9 -P" in kill_cmd
        assert "rm -f" in cleanup_cmd
        # Kill must target the same pidfile the spawn wrapper wrote.
        import re as _re
        spawn_pid = _re.search(r"hspice_pid_[a-f0-9]+", spawn_cmd)
        kill_pid = _re.search(r"hspice_pid_[a-f0-9]+", kill_cmd)
        assert spawn_pid is not None and kill_pid is not None
        assert spawn_pid.group(0) == kill_pid.group(0)
