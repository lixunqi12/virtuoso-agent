"""Unit tests for src/hspice_worker.py.

Pure-Python tests — subprocess.run / subprocess.Popen are mocked so no
real ssh / hspice is touched. T8.4 introduced a Popen + liveness-poll
loop in `_spawn_and_wait`; helpers below build a `_FakePopen` that the
spawn site uses, while every other ssh subcommand (ls, cat, kill,
cleanup, .lis stat probe) still runs through `subprocess.run`.

Pattern mirrors tests/test_ocean_worker_spec.py for the pure-Python
side; the live-SSH `_smoke` / `_timeout` siblings are run via
`__main__` and are NOT collected by pytest.
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


def _cfg(**overrides) -> HspiceWorkerConfig:
    base = dict(
        remote_host="remotehost",
        remote_user="alice",
        hspice_bin="/apps/hspice/bin/hspice",
        remote_tmp_dir="/tmp",
        hard_ceiling_s=30.0,
        idle_timeout_s=20.0,
        liveness_poll_s=2.0,
        heartbeat_s=4.0,
        ssh_connect_timeout_s=5,
    )
    base.update(overrides)
    return HspiceWorkerConfig(**base)


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
# Fake Popen + virtual clock for the spawn site
# --------------------------------------------------------------------------


class _FakeStream:
    """Stand-in for proc.stderr — a closeable PIPE-like reader."""

    def __init__(self, body: str) -> None:
        self._body = body
        self._read = False
        self.closed = False

    def read(self) -> str:
        if self._read:
            return ""
        self._read = True
        return self._body

    def close(self) -> None:
        self.closed = True


class _FakePopen:
    """Minimal Popen stand-in used by _spawn_and_wait tests.

    `wait_outcomes`: list of values consumed by successive `.wait(timeout)`
    calls. ``None`` means "still running this iteration" — wait raises
    ``subprocess.TimeoutExpired`` and (if `vclock` is set) advances the
    virtual clock by `timeout`. An int means "process exited with rc";
    wait returns that int. Once the list is exhausted wait returns 0
    (defensive default for follow-up `proc.wait(timeout=5)` calls
    inside `_kill_local_proc`).

    Post-T8.4-R2 the production code no longer calls `.poll()`; the
    method is preserved as a no-op that always reports "running" so any
    legacy or accidental usage doesn't silently confuse the test.

    `stderr_body`: content `proc.stderr.read()` returns once reaped.
    """

    def __init__(
        self,
        cmd,
        *,
        wait_outcomes: list,
        stderr_body: str = "",
        track: list | None = None,
        vclock: "_VClock | None" = None,
        **_kwargs,
    ) -> None:
        self.cmd = cmd
        self._wait_iter = iter(wait_outcomes)
        self.stderr = _FakeStream(stderr_body)
        self.killed = False
        self.kill_count = 0
        self._vclock = vclock
        if track is not None:
            track.append(self)

    def poll(self):
        # Production code post-R2 does not call poll(); keep a safe
        # default so a stray call doesn't masquerade as "exited".
        return None

    def kill(self):
        self.killed = True
        self.kill_count += 1

    def wait(self, timeout=None):
        try:
            outcome = next(self._wait_iter)
        except StopIteration:
            # Default: process has been reaped (e.g. post-kill wait).
            return 0
        if outcome is None:
            # Simulate the timeout window elapsing on the virtual clock
            # so liveness/idle/heartbeat math in production sees real
            # elapsed time without `time.sleep` in the new R2 loop.
            if self._vclock is not None and timeout is not None:
                self._vclock.t += float(timeout)
            raise subprocess.TimeoutExpired(cmd=str(self.cmd), timeout=timeout)
        return int(outcome)


class _VClock:
    """Virtual monotonic clock.

    Pre-R2 the production polling loop called `time.sleep(poll_s)` and
    the clock advanced via that path. Post-R2 the loop is driven by
    `proc.wait(timeout=poll_s)`, so the clock is advanced inside
    `_FakePopen.wait` whenever it raises TimeoutExpired. `sleep()`
    is preserved as a no-op fallback so the patch is safe even if
    production gains an unrelated sleep later.
    """

    def __init__(self, t0: float = 1000.0) -> None:
        self.t = t0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, secs: float) -> None:
        # Defensive: if production ever calls time.sleep again, advance
        # the clock so tests don't silently freeze elapsed time.
        self.t += float(secs)


def _patch_clock(monkeypatch, vclock: _VClock) -> None:
    monkeypatch.setattr(hspice_worker.time, "monotonic", vclock.monotonic)
    monkeypatch.setattr(hspice_worker.time, "sleep", vclock.sleep)


def _popen_factory(
    *,
    wait_outcomes: list,
    stderr_body: str = "",
    track: list | None = None,
    vclock: _VClock | None = None,
):
    """Build a subprocess.Popen replacement that yields a canned _FakePopen."""

    def factory(cmd, *args, **kwargs):
        return _FakePopen(
            cmd,
            wait_outcomes=wait_outcomes,
            stderr_body=stderr_body,
            track=track,
            vclock=vclock,
        )

    return factory


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

    def test_t84_default_budgets(self):
        # Defaults document the new two-tier model:
        # 4 h hard ceiling, 10 min idle.
        cfg = HspiceWorkerConfig(remote_host="h", remote_user="u")
        assert cfg.hard_ceiling_s == 14400.0
        assert cfg.idle_timeout_s == 600.0
        assert cfg.liveness_poll_s == 30.0
        assert cfg.heartbeat_s == 60.0


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
        # T8.4: VB_HSPICE_TIMEOUT_S maps to hard_ceiling_s for backwards
        # compatibility with pre-rework deployments.
        assert w.cfg.hard_ceiling_s == 120.0

    def test_t84_legacy_env_maps_to_hard_ceiling(self, monkeypatch):
        # VB_HSPICE_TIMEOUT_S (legacy single-budget knob) → hard_ceiling_s.
        monkeypatch.setenv("VB_REMOTE_HOST", "h")
        monkeypatch.setenv("VB_REMOTE_USER", "u")
        monkeypatch.setenv("VB_HSPICE_TIMEOUT_S", "7200")
        w = worker_from_env()
        assert w.cfg.hard_ceiling_s == 7200.0
        # Idle/poll/heartbeat fall back to module defaults.
        assert w.cfg.idle_timeout_s == hspice_worker.DEFAULT_IDLE_TIMEOUT_S

    def test_t84_explicit_hard_ceiling_overrides_legacy(self, monkeypatch):
        # If both VB_HSPICE_TIMEOUT_S and VB_HSPICE_HARD_CEILING_S are
        # set, the explicit one wins.
        monkeypatch.setenv("VB_REMOTE_HOST", "h")
        monkeypatch.setenv("VB_REMOTE_USER", "u")
        monkeypatch.setenv("VB_HSPICE_TIMEOUT_S", "1234")
        monkeypatch.setenv("VB_HSPICE_HARD_CEILING_S", "9876")
        w = worker_from_env()
        assert w.cfg.hard_ceiling_s == 9876.0

    def test_t84_idle_and_poll_env_overrides(self, monkeypatch):
        monkeypatch.setenv("VB_REMOTE_HOST", "h")
        monkeypatch.setenv("VB_REMOTE_USER", "u")
        monkeypatch.setenv("VB_HSPICE_IDLE_TIMEOUT_S", "300")
        monkeypatch.setenv("VB_HSPICE_LIVENESS_POLL_S", "15")
        monkeypatch.setenv("VB_HSPICE_HEARTBEAT_S", "45")
        w = worker_from_env()
        assert w.cfg.idle_timeout_s == 300.0
        assert w.cfg.liveness_poll_s == 15.0
        assert w.cfg.heartbeat_s == 45.0


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
    def _run_with(
        self,
        run_side_effect,
        *,
        spawn_rc: int = 0,
        spawn_stderr: str = "",
        worker: HspiceWorker | None = None,
        track: list | None = None,
        monkeypatch=None,
    ):
        worker = worker or HspiceWorker(_cfg())
        # spawn finishes immediately on first poll — no polling-loop
        # bookkeeping fires for the success path.
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_side_effect,
        ) as mr:
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd,
                    wait_outcomes=[spawn_rc],
                    stderr_body=spawn_stderr,
                    track=track,
                ),
            ):
                result = worker.run("/tmp/hsp/sim.sp", timeout_sec=10.0)
        return result, mr

    def test_single_mt0_parsed_and_returned(self):
        # subprocess.run sequence: stdout-fetch, stderr-fetch, list, cat-mt0, cleanup.
        side_effect = [
            _ok(stdout="hspice banner"),  # fetch remote stdout file
            _ok(stdout=""),               # fetch remote stderr file
            _ok(stdout="sim.mt0\n"),      # list
            _ok(stdout=_MT0_BODY),        # cat mt0
            _ok(),                        # cleanup
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
            _ok(),                                                   # stdout fetch
            _ok(),                                                   # stderr fetch
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
            _ok(),                                  # stdout fetch
            _ok(),                                  # stderr fetch
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
        # T8.4 path: hspice stdout/stderr come from remote temp file
        # cats, not Popen pipes. The first two `subprocess.run` calls
        # in _spawn_and_wait deliver them.
        side_effect = [
            _ok(stdout="stdout banner\n"),     # fetch remote stdout file
            _ok(stdout="stderr line\n"),       # fetch remote stderr file
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
            _ok(stdout="hspice warning\n"),  # remote stdout
            _ok(),                            # remote stderr
            _ok(stdout="sim.mt0\n"),
            _ok(stdout=_MT0_BODY),
            _ok(),
        ]
        result, _m = self._run_with(side_effect, spawn_rc=1)
        assert result.returncode == 1
        assert "sim.mt0" in result.mt_files

    def test_spawn_command_runs_hspice_with_basename(self):
        side_effect = [
            _ok(),                  # stdout fetch
            _ok(),                  # stderr fetch
            _ok(stdout=""),         # list (nothing)
            _ok(),                  # cleanup
        ]
        track: list = []
        _result, _m = self._run_with(side_effect, track=track)
        assert track, "Popen was never called"
        spawn_cmd = track[0].cmd
        remote = spawn_cmd[-1]
        assert "hspice" in remote
        assert "sim.sp" in remote
        assert "cd /tmp/hsp" in remote
        assert "echo $$ >" in remote  # pidfile is written before exec

    def test_t84_spawn_redirects_remote_stdout_stderr_to_temp_files(self):
        # T8.4 critical: Popen(stdout=PIPE, stderr=PIPE) deadlocks once
        # hspice exceeds the OS pipe buffer. The wrapper must redirect
        # remote stdout/stderr to remote temp files, and the local
        # Popen must NOT capture hspice output via PIPE.
        side_effect = [
            _ok(),
            _ok(),
            _ok(stdout=""),
            _ok(),
        ]
        track: list = []
        worker = HspiceWorker(_cfg())
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[0], track=track,
                ) or kw,  # capture kwargs separately below
            ):
                # The above lambda only returns the FakePopen; capture
                # actual kwargs via a second patch.
                pass
        # Re-run with a kwargs-capturing wrapper to assert PIPE was not
        # used for hspice's own stdout/stderr.
        captured: dict = {}

        def _capture(cmd, *a, **kw):
            captured.update(kw)
            return _FakePopen(cmd, wait_outcomes=[0])

        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=[_ok(), _ok(), _ok(stdout=""), _ok()],
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=_capture,
            ):
                worker.run("/tmp/hsp/sim.sp", timeout_sec=10.0)
        # stdout MUST be DEVNULL (or anything that is NOT PIPE).
        assert captured.get("stdout") == subprocess.DEVNULL, (
            f"Popen used stdout={captured.get('stdout')!r}; "
            "must be DEVNULL to avoid pipe-buffer deadlock"
        )
        # stderr can be PIPE (small ssh transport messages), but must
        # not be inherited (which would scribble onto our own console).
        assert captured.get("stderr") != subprocess.STDOUT
        # stdin should be DEVNULL — hspice on remote has no stdin.
        assert captured.get("stdin") == subprocess.DEVNULL
        # And the wrapper itself must redirect on the remote side.
        # We check this via a second invocation that captures the cmd.
        track2: list = []
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=[_ok(), _ok(), _ok(stdout=""), _ok()],
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[0], track=track2,
                ),
            ):
                worker.run("/tmp/hsp/sim.sp", timeout_sec=10.0)
        wrapper = track2[0].cmd[-1]
        # The wrapper must redirect into temp files in /tmp.
        assert "hspice_stdout_" in wrapper
        assert "hspice_stderr_" in wrapper
        assert "> " in wrapper and "2> " in wrapper

    def test_empty_output_list_returns_result_with_empty_mt_files(self):
        side_effect = [
            _ok(),                   # stdout fetch
            _ok(),                   # stderr fetch
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
            _ok(),                                    # stdout fetch
            _ok(),                                    # stderr fetch
            _ok(stdout="sim.mt0\nsim.tr0\nsim.lis\n"),  # list includes tr0
            _ok(stdout=_MT0_BODY),                    # cat mt0
            _ok(stdout="lis body"),                   # cat lis
            _ok(),                                    # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ) as m:
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[0],
                ),
            ):
                result = worker.run("/tmp/hsp/sim.sp")
        # Make sure none of the subprocess.run calls ever cat'd sim.tr0.
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
            _ok(),  # stdout
            _ok(),  # stderr
            _ok(stdout=""),  # list
            _ok(),  # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=side_effect,
        ) as m:
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[0],
                ),
            ):
                worker.run("/tmp/hsp/sim.sp")
        # Index 2 = list (after stdout + stderr fetches).
        ls_cmd = m.call_args_list[2].args[0][-1]
        assert ".tr" not in ls_cmd
        assert ".mt[0-9]" in ls_cmd
        assert ".lis" in ls_cmd


# --------------------------------------------------------------------------
# T8.4 liveness-based timeout: hard ceiling + idle + heartbeat
# --------------------------------------------------------------------------


class TestT84HardCeiling:
    def test_hard_ceiling_kills_remote_and_raises(self, monkeypatch):
        # Hard ceiling = 10s, idle = 100s (won't trip), poll = 2s.
        # .lis size always grows, so liveness keeps the idle counter
        # reset — but the hard ceiling still fires.
        cfg = _cfg(hard_ceiling_s=10.0, idle_timeout_s=100.0, liveness_poll_s=2.0)
        worker = HspiceWorker(cfg)
        vclock = _VClock()
        _patch_clock(monkeypatch, vclock)

        # subprocess.run sequence: each iteration the polling loop
        # issues a .lis stat probe. We model 6 probes (each at +2s)
        # before hard-ceiling trips at elapsed=12s. After raise:
        # _kill_remote (1 run), then run() finally _cleanup_remote (1 run).
        run_calls = [
            _ok(stdout=str(100 * i) + "\n") for i in range(1, 7)
        ] + [
            _ok(stdout="killed 12345\n"),  # _kill_remote
            _ok(),                           # cleanup
        ]
        track: list = []

        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ) as mr:
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[None] * 20, track=track, vclock=vclock,
                ),
            ):
                with pytest.raises(HspiceWorkerTimeout, match="hard ceiling"):
                    worker.run("/tmp/hsp/sim.sp", timeout_sec=10.0)
        # _kill_local_proc was invoked.
        assert track[0].killed is True
        # Find the remote kill call — it has "kill -9".
        kill_cmd = None
        for c in mr.call_args_list:
            argv = c.args[0]
            if argv and "kill -9" in argv[-1]:
                kill_cmd = argv[-1]
                break
        assert kill_cmd is not None, "remote kill ssh was not issued"
        assert "pkill -9 -P" in kill_cmd

    def test_hard_ceiling_raises_with_proper_message(self, monkeypatch):
        cfg = _cfg(hard_ceiling_s=5.0, idle_timeout_s=100.0, liveness_poll_s=2.0)
        worker = HspiceWorker(cfg)
        vclock = _VClock()
        _patch_clock(monkeypatch, vclock)

        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=[
                _ok(stdout="100\n"),  # probe @ t=2
                _ok(stdout="200\n"),  # probe @ t=4
                _ok(stdout="300\n"),  # probe @ t=6 → over ceiling
                _ok(stdout="killed\n"),
                _ok(),
            ],
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[None] * 10, vclock=vclock,
                ),
            ):
                with pytest.raises(HspiceWorkerTimeout) as excinfo:
                    worker.run("/tmp/hsp/sim.sp", timeout_sec=5.0)
        msg = str(excinfo.value)
        assert "hard ceiling" in msg
        assert "killed" in msg


class TestT84IdleTimeout:
    def test_idle_timeout_fires_when_lis_stops_growing(self, monkeypatch):
        # idle = 6s, hard = 1000s. .lis grows once then stalls. After
        # 6s of stall the worker kills.
        cfg = _cfg(hard_ceiling_s=1000.0, idle_timeout_s=6.0, liveness_poll_s=2.0)
        worker = HspiceWorker(cfg)
        vclock = _VClock()
        _patch_clock(monkeypatch, vclock)

        # Probe sequence: 100, 100, 100, 100 → at t=8 (4 probes @ 2s
        # cadence), idle_for = t - last_growth_time. Last growth was
        # at t0 (before first sleep) since the first probe just
        # records the size. We want idle to clearly fire — emit same
        # size for several probes.
        run_calls = [
            _ok(stdout="100\n"),   # probe @ t=2 → records 100, growth
            _ok(stdout="100\n"),   # probe @ t=4 → no growth
            _ok(stdout="100\n"),   # probe @ t=6 → no growth
            _ok(stdout="100\n"),   # probe @ t=8 → no growth, idle_for=6s → fires
            _ok(stdout="killed\n"),
            _ok(),
        ]
        track: list = []
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[None] * 20, track=track, vclock=vclock,
                ),
            ):
                with pytest.raises(HspiceWorkerTimeout, match="idle"):
                    worker.run("/tmp/hsp/sim.sp", timeout_sec=1000.0)
        assert track[0].killed is True

    def test_idle_does_not_fire_if_lis_keeps_growing(self, monkeypatch):
        # idle = 4s, hard = 1000s, poll = 2s. .lis grows every probe
        # so idle is repeatedly reset. The proc finishes naturally
        # after a few polls.
        cfg = _cfg(hard_ceiling_s=1000.0, idle_timeout_s=4.0, liveness_poll_s=2.0)
        worker = HspiceWorker(cfg)
        vclock = _VClock()
        _patch_clock(monkeypatch, vclock)

        run_calls = [
            _ok(stdout="100\n"),     # probe @ t=2
            _ok(stdout="200\n"),     # probe @ t=4
            _ok(stdout="300\n"),     # probe @ t=6
            # Then proc.poll() returns 0 — proc finished.
            _ok(stdout="banner\n"),  # remote stdout fetch
            _ok(stdout=""),           # remote stderr fetch
            _ok(stdout=""),           # ls (no outputs)
            _ok(),                    # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[None, None, None, 0], vclock=vclock,
                ),
            ):
                result = worker.run("/tmp/hsp/sim.sp", timeout_sec=1000.0)
        assert result.returncode == 0
        assert "banner" in result.stdout_scrubbed


class TestT84Heartbeat:
    def test_heartbeat_log_emitted_at_cadence(self, monkeypatch, caplog):
        # heartbeat = 4s, poll = 2s, idle = 100s, hard = 100s.
        # Polls happen at t=2, t=4, t=6, t=8. Heartbeat fires at t≥4
        # and t≥8 → at least 2 heartbeat lines.
        cfg = _cfg(
            hard_ceiling_s=100.0, idle_timeout_s=100.0,
            liveness_poll_s=2.0, heartbeat_s=4.0,
        )
        worker = HspiceWorker(cfg)
        vclock = _VClock()
        _patch_clock(monkeypatch, vclock)

        run_calls = [
            _ok(stdout="100\n"),     # probe t=2
            _ok(stdout="200\n"),     # probe t=4 (heartbeat #1)
            _ok(stdout="300\n"),     # probe t=6
            _ok(stdout="400\n"),     # probe t=8 (heartbeat #2)
            _ok(stdout=""),          # remote stdout fetch
            _ok(stdout=""),          # remote stderr fetch
            _ok(stdout=""),          # ls
            _ok(),                   # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[None, None, None, None, 0], vclock=vclock,
                ),
            ):
                with caplog.at_level("INFO", logger="src.hspice_worker"):
                    worker.run("/tmp/hsp/sim.sp", timeout_sec=100.0)
        heartbeats = [r for r in caplog.records if "heartbeat" in r.getMessage()]
        assert len(heartbeats) >= 2, (
            f"Expected at least 2 heartbeat log lines; got {len(heartbeats)}"
        )


class TestT84LisProbeTolerance:
    def test_lis_probe_transport_failure_is_inconclusive_not_fatal(
        self, monkeypatch
    ):
        # Single probe failure (rc=1, missing file) should NOT cause
        # the worker to crash; idle counter just doesn't reset that
        # tick. Once the proc finishes naturally, run completes.
        cfg = _cfg(hard_ceiling_s=100.0, idle_timeout_s=100.0, liveness_poll_s=2.0)
        worker = HspiceWorker(cfg)
        vclock = _VClock()
        _patch_clock(monkeypatch, vclock)

        run_calls = [
            _ok(rc=1, stdout=""),       # probe fails (e.g. .lis not yet created)
            _ok(stdout="100\n"),        # probe ok
            _ok(stdout=""),              # remote stdout fetch
            _ok(stdout=""),              # remote stderr fetch
            _ok(stdout=""),              # ls
            _ok(),                       # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[None, None, 0], vclock=vclock,
                ),
            ):
                result = worker.run("/tmp/hsp/sim.sp", timeout_sec=100.0)
        assert result.returncode == 0


class TestT84RemoteLogFetch:
    def test_stdout_stderr_come_from_remote_temp_files(self, monkeypatch):
        # On success the worker `cat`s remote /tmp/hspice_stdout_<id>
        # and /tmp/hspice_stderr_<id> — those are the calls indexed 0
        # and 1 in the subprocess.run sequence after Popen reaps.
        worker = HspiceWorker(_cfg())
        run_calls = [
            _ok(stdout="REMOTE_STDOUT_PAYLOAD\n"),  # fetch hspice_stdout
            _ok(stdout="REMOTE_STDERR_PAYLOAD\n"),  # fetch hspice_stderr
            _ok(stdout=""),                          # ls (no outputs)
            _ok(),                                    # cleanup
        ]
        track_run: list = []

        def _capture_run(*args, **kwargs):
            track_run.append(args[0])
            return run_calls.pop(0)

        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=_capture_run,
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[0],
                ),
            ):
                result = worker.run("/tmp/hsp/sim.sp", timeout_sec=10.0)
        # First two run() calls must be cats of the remote stdout/stderr
        # temp files — by basename pattern.
        assert "hspice_stdout_" in track_run[0][-1]
        assert "hspice_stderr_" in track_run[1][-1]
        assert "REMOTE_STDOUT_PAYLOAD" in result.stdout_scrubbed
        assert "REMOTE_STDERR_PAYLOAD" in result.stderr_scrubbed

    def test_cleanup_removes_stdout_stderr_temp_files(self, monkeypatch):
        worker = HspiceWorker(_cfg())
        run_calls = [
            _ok(),                       # remote stdout fetch
            _ok(),                       # remote stderr fetch
            _ok(stdout=""),              # ls
            _ok(),                       # cleanup
        ]
        track_run: list = []

        def _capture_run(*args, **kwargs):
            track_run.append(args[0])
            return run_calls.pop(0)

        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=_capture_run,
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[0],
                ),
            ):
                worker.run("/tmp/hsp/sim.sp", timeout_sec=10.0)
        cleanup_cmd = track_run[-1][-1]
        assert "rm -f" in cleanup_cmd
        assert "hspice_pid_" in cleanup_cmd
        assert "hspice_stdout_" in cleanup_cmd
        assert "hspice_stderr_" in cleanup_cmd


# --------------------------------------------------------------------------
# Transport / script errors
# --------------------------------------------------------------------------


class TestErrors:
    def test_ssh_rc_255_raises_spawn_error(self):
        worker = HspiceWorker(_cfg())
        # rc=255 from spawn → kill remote (1 run), cleanup (1 run).
        # No stdout/stderr fetch occurs because we raise first.
        run_calls = [
            _ok(stdout="killed\n"),                       # best-effort kill
            _ok(),                                        # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[255],
                    stderr_body="Connection refused\n",
                ),
            ):
                with pytest.raises(HspiceWorkerSpawnError):
                    worker.run("/tmp/hsp/sim.sp", timeout_sec=10.0)

    def test_fetch_nonzero_rc_raises_script_error(self):
        worker = HspiceWorker(_cfg())
        run_calls = [
            _ok(),                                 # remote stdout fetch
            _ok(),                                 # remote stderr fetch
            _ok(stdout="sim.mt0\n"),               # list
            _ok(rc=1, stderr="cat: nope"),         # fetch fails
            _ok(),                                 # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[0],
                ),
            ):
                with pytest.raises(HspiceWorkerScriptError, match="fetch"):
                    worker.run("/tmp/hsp/sim.sp", timeout_sec=10.0)

    def test_list_ssh_rc_nonzero_raises_spawn_error(self):
        worker = HspiceWorker(_cfg())
        run_calls = [
            _ok(),                             # remote stdout fetch
            _ok(),                             # remote stderr fetch
            _ok(rc=1, stderr="ssh died"),      # list fails at ssh level
            _ok(),                             # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[0],
                ),
            ):
                with pytest.raises(HspiceWorkerSpawnError, match="list outputs"):
                    worker.run("/tmp/hsp/sim.sp", timeout_sec=10.0)

    def test_mt0_parse_error_wraps_into_script_error(self):
        worker = HspiceWorker(_cfg())
        # Malformed .mt0 (missing $DATA1 header) — parse_mt0 raises,
        # worker wraps into HspiceWorkerScriptError.
        bad_mt0 = "garbage line\n.TITLE 'x'\ndelay temper alter#\n1.0 25.0 1.0\n"
        run_calls = [
            _ok(),                           # remote stdout fetch
            _ok(),                           # remote stderr fetch
            _ok(stdout="sim.mt0\n"),         # list
            _ok(stdout=bad_mt0),             # cat (parses bad)
            _ok(),                           # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[0],
                ),
            ):
                with pytest.raises(HspiceWorkerScriptError, match="failed to parse"):
                    worker.run("/tmp/hsp/sim.sp", timeout_sec=10.0)

    def test_scrub_error_wraps_into_script_error(self):
        worker = HspiceWorker(_cfg())
        run_calls = [
            _ok(),                           # remote stdout fetch
            _ok(),                           # remote stderr fetch
            _ok(stdout="sim.mt0\n"),         # list
            _ok(stdout=_MT0_BODY),           # cat
            _ok(),                           # cleanup
        ]
        fake_scrub_err = ScrubError(
            ["nch_lvt_foo"], stage="mt0", counts={"foundry_seed": 1},
        )
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[0],
                ),
            ):
                with mock.patch(
                    "src.hspice_worker.scrub_mt0",
                    side_effect=fake_scrub_err,
                ):
                    with pytest.raises(HspiceWorkerScriptError, match="scrub failed"):
                        worker.run("/tmp/hsp/sim.sp", timeout_sec=10.0)


# --------------------------------------------------------------------------
# Cleanup always runs
# --------------------------------------------------------------------------


class TestCleanup:
    def test_cleanup_fires_on_success(self):
        worker = HspiceWorker(_cfg())
        run_calls = [
            _ok(),  # stdout fetch
            _ok(),  # stderr fetch
            _ok(stdout=""),  # list (empty)
            _ok(),  # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ) as m:
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[0],
                ),
            ):
                worker.run("/tmp/hsp/sim.sp", timeout_sec=10.0)
        last = m.call_args_list[-1].args[0][-1]
        assert "rm -f" in last

    def test_cleanup_fires_on_hard_ceiling_timeout(self, monkeypatch):
        cfg = _cfg(hard_ceiling_s=4.0, idle_timeout_s=100.0, liveness_poll_s=2.0)
        worker = HspiceWorker(cfg)
        vclock = _VClock()
        _patch_clock(monkeypatch, vclock)

        run_calls = [
            _ok(stdout="100\n"),  # probe @ t=2
            _ok(stdout="200\n"),  # probe @ t=4
            _ok(stdout="300\n"),  # probe @ t=6 (over ceiling)
            _ok(stdout="killed\n"),
            _ok(),                # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ) as m:
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[None] * 10, vclock=vclock,
                ),
            ):
                with pytest.raises(HspiceWorkerTimeout):
                    worker.run("/tmp/hsp/sim.sp", timeout_sec=4.0)
        last = m.call_args_list[-1].args[0][-1]
        assert "rm -f" in last

    def test_cleanup_fires_on_script_error(self):
        worker = HspiceWorker(_cfg())
        run_calls = [
            _ok(stdout="killed\n"),       # best-effort kill on rc=255
            _ok(),                        # cleanup still runs
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ) as m:
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[255],
                    stderr_body="boom",
                ),
            ):
                with pytest.raises(HspiceWorkerSpawnError):
                    worker.run("/tmp/hsp/sim.sp", timeout_sec=10.0)
        last = m.call_args_list[-1].args[0][-1]
        assert "rm -f" in last


# --------------------------------------------------------------------------
# R2 rework blockers: option-injection defense + rc=255 zombie kill
# --------------------------------------------------------------------------


class TestR2Blockers:
    """Codex T3 R2 blockers (still load-bearing post-T8.4):

    R1 sp_base leading-dash option injection — three layers of defense:
      - a. basename regex rejects leading '-'
      - b. spawn command prefixes the basename with './'
      - c. ls command uses '--' to terminate flag parsing
    R2 ssh rc=255 zombie window — kill remote pid before raising, since
        the wrapper may have already written the pidfile and exec'd
        hspice before the transport dropped.
    """

    def test_r1a_regex_rejects_leading_dash_basename(self):
        worker = HspiceWorker(_cfg())
        with pytest.raises(ValueError, match="leading dash"):
            worker.run("/tmp/foo/-evil.sp")

    def test_r1b_spawn_prefixes_basename_with_dot_slash(self):
        worker = HspiceWorker(_cfg())
        run_calls = [
            _ok(),                    # stdout
            _ok(),                    # stderr
            _ok(stdout=""),           # list (empty)
            _ok(),                    # cleanup
        ]
        track: list = []
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[0], track=track,
                ),
            ):
                worker.run("/tmp/hsp/sim.sp", timeout_sec=10.0)
        spawn_cmd = track[0].cmd[-1]
        assert "./sim.sp" in spawn_cmd
        cd_ix = spawn_cmd.index("cd /tmp/hsp")
        exec_ix = spawn_cmd.index("./sim.sp")
        assert cd_ix < exec_ix

    def test_r1c_ls_uses_double_dash_terminator(self):
        worker = HspiceWorker(_cfg())
        run_calls = [
            _ok(),                    # stdout
            _ok(),                    # stderr
            _ok(stdout=""),           # list (empty)
            _ok(),                    # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ) as m:
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[0],
                ),
            ):
                worker.run("/tmp/hsp/sim.sp", timeout_sec=10.0)
        # Index 2 = list (after stdout + stderr fetches).
        ls_cmd = m.call_args_list[2].args[0][-1]
        assert "ls -1 --" in ls_cmd

    def test_r2_rc255_kills_remote_before_raising(self):
        # rc=255 means ssh transport dropped — but the wrapper may
        # already have written the pidfile and exec'd hspice. The
        # worker must call _kill_remote *before* raising, using the
        # same pidfile the spawn wrapper wrote.
        worker = HspiceWorker(_cfg())
        run_calls = [
            _ok(stdout="killed 4321\n"),           # best-effort kill
            _ok(),                                  # cleanup
        ]
        track: list = []
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ) as m:
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[255],
                    stderr_body="Broken pipe\n",
                    track=track,
                ),
            ):
                with pytest.raises(HspiceWorkerSpawnError):
                    worker.run("/tmp/hsp/sim.sp", timeout_sec=10.0)
        assert m.call_count == 2
        spawn_cmd = track[0].cmd[-1]
        kill_cmd = m.call_args_list[0].args[0][-1]
        cleanup_cmd = m.call_args_list[1].args[0][-1]
        assert "kill -9" in kill_cmd
        assert "pkill -9 -P" in kill_cmd
        assert "rm -f" in cleanup_cmd
        # Kill must target the same pidfile the spawn wrapper wrote.
        import re as _re
        spawn_pid = _re.search(r"hspice_pid_[a-f0-9]+", spawn_cmd)
        kill_pid = _re.search(r"hspice_pid_[a-f0-9]+", kill_cmd)
        assert spawn_pid is not None and kill_pid is not None
        assert spawn_pid.group(0) == kill_pid.group(0)


# --------------------------------------------------------------------------
# T8.4 R2 blockers: proc.wait()-driven loop closes the sleep-window race
# --------------------------------------------------------------------------


class TestT84R2Blocker:
    """Codex T8.4 R2 blocker — the old `poll() → sleep(poll_s) → checks`
    shape ran post-sleep hard/idle/probe branches even when the proc had
    legitimately exited during the sleep, killing healthy short runs and
    swallowing rc=255 transport drops into a timeout. The R2 fix drives
    the loop off `proc.wait(timeout=poll_s)` so an in-window exit is
    reaped atomically and the post-loop rc dispatch handles it.

    These tests pin the in-window-exit semantics by setting `poll_s`
    *larger* than the timeout budget — under the old shape, every
    scenario below would have raised HspiceWorkerTimeout (or eaten an
    rc=255 as a timeout). Under R2 they all complete correctly.
    """

    def test_short_hard_ceiling_with_in_window_exit_succeeds(self, monkeypatch):
        # hard_ceiling=1s, poll=2s. The proc exits within the first
        # wait window (rc=0). Old shape: poll → still running → sleep
        # 2s (proc exits in here) → hard check at elapsed=2s > 1s →
        # KILL + raise HspiceWorkerTimeout. R2: wait(timeout=2s) reaps
        # the rc=0 atomically → break → no checks run.
        cfg = _cfg(
            hard_ceiling_s=1.0, idle_timeout_s=100.0, liveness_poll_s=2.0,
        )
        worker = HspiceWorker(cfg)
        vclock = _VClock()
        _patch_clock(monkeypatch, vclock)

        run_calls = [
            _ok(stdout=""),  # remote stdout fetch
            _ok(stdout=""),  # remote stderr fetch
            _ok(stdout=""),  # ls (no outputs)
            _ok(),           # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[0], vclock=vclock,
                ),
            ):
                result = worker.run("/tmp/hsp/sim.sp", timeout_sec=1.0)
        assert result.returncode == 0

    def test_short_idle_with_in_window_exit_succeeds(self, monkeypatch):
        # idle=1s, poll=2s. Proc exits within the first wait window.
        # Old shape: poll → running → sleep 2s → probe (inconclusive;
        # rc=1) → idle check fires (idle_for=2s > 1s) → KILL + raise.
        # R2: wait(timeout=2s) reaps rc=0 → break, no probe/idle check.
        cfg = _cfg(
            hard_ceiling_s=1000.0, idle_timeout_s=1.0, liveness_poll_s=2.0,
        )
        worker = HspiceWorker(cfg)
        vclock = _VClock()
        _patch_clock(monkeypatch, vclock)

        # No probe call appears here because R2 reaps before the probe
        # branch is reached. Sequence is just the post-loop fetch+ls+cleanup.
        run_calls = [
            _ok(stdout=""),  # remote stdout fetch
            _ok(stdout=""),  # remote stderr fetch
            _ok(stdout=""),  # ls (no outputs)
            _ok(),           # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[0], vclock=vclock,
                ),
            ):
                result = worker.run("/tmp/hsp/sim.sp", timeout_sec=1000.0)
        assert result.returncode == 0

    def test_rc255_during_wait_window_routes_to_spawn_error(self, monkeypatch):
        # ssh transport drops mid-wait → wait returns 255. Old shape:
        # if hard_ceiling < poll_s, the post-sleep elapsed check would
        # fire FIRST and raise HspiceWorkerTimeout, masking the actual
        # transport error. R2: wait reaps rc=255 atomically → break →
        # post-loop rc=255 dispatch raises HspiceWorkerSpawnError.
        cfg = _cfg(
            hard_ceiling_s=1.0, idle_timeout_s=1.0, liveness_poll_s=2.0,
        )
        worker = HspiceWorker(cfg)
        vclock = _VClock()
        _patch_clock(monkeypatch, vclock)

        run_calls = [
            _ok(stdout="killed 9999\n"),  # _kill_remote on rc=255
            _ok(),                         # cleanup
        ]
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=run_calls,
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[255],
                    stderr_body="Connection closed by remote host\n",
                    vclock=vclock,
                ),
            ):
                with pytest.raises(HspiceWorkerSpawnError):
                    worker.run("/tmp/hsp/sim.sp", timeout_sec=1.0)


# --------------------------------------------------------------------------
# T8.5: liveness probe widened beyond .lis to all hspice output families
# --------------------------------------------------------------------------


class TestT85OutputsProbe:
    """T8.5 — probe must glob ``.lis``/``.mt[0-9]*``/``.st[0-9]*``/
    ``.ic[0-9]*``/``.pa[0-9]*``/``.su[0-9]*`` and sum their sizes.

    Bug being fixed: the previous probe `stat -c %s <sp>.lis` always
    returned None when a testbench's ``.OPTION`` config skipped writing
    the listing file — even when hspice was healthily writing measure
    data. Idle-timeout then misfired and killed a healthy run.

    Constraints preserved:
    * None on transport failure / no files yet (inconclusive — caller
      keeps idle counter ticking).
    * ``.tr*`` (waveforms) MUST be excluded from the probe (privacy +
      file-size).
    """

    def test_probe_command_globs_all_output_families_excluding_tr(
        self, monkeypatch
    ):
        # Inspect the actual ssh probe command emitted by the worker —
        # it must mention all six output families and NOT mention .tr.
        cfg = _cfg(
            hard_ceiling_s=100.0, idle_timeout_s=100.0, liveness_poll_s=2.0,
        )
        worker = HspiceWorker(cfg)
        vclock = _VClock()
        _patch_clock(monkeypatch, vclock)

        captured: list = []

        def _capture_run(*args, **kwargs):
            captured.append(args[0])
            # First call is the probe at t=2; subsequent calls are
            # post-loop fetches.
            return _ok(stdout="100\n") if len(captured) == 1 else _ok(stdout="")

        with mock.patch(
            "src.hspice_worker.subprocess.run",
            side_effect=_capture_run,
        ):
            with mock.patch(
                "src.hspice_worker.subprocess.Popen",
                side_effect=lambda cmd, *a, **kw: _FakePopen(
                    cmd, wait_outcomes=[None, 0], vclock=vclock,
                ),
            ):
                worker.run("/tmp/hsp/sim.sp", timeout_sec=100.0)
        # First captured call = probe (idx 0).
        probe_shell = captured[0][-1]
        assert ".lis" in probe_shell, "probe must include .lis"
        assert ".mt[0-9]" in probe_shell, "probe must include .mt[0-9]*"
        assert ".st[0-9]" in probe_shell, "probe must include .st[0-9]*"
        assert ".ic[0-9]" in probe_shell, "probe must include .ic[0-9]*"
        assert ".pa[0-9]" in probe_shell, "probe must include .pa[0-9]*"
        assert ".su[0-9]" in probe_shell, "probe must include .su[0-9]*"
        # Defense-in-depth: never probe waveform files.
        assert ".tr" not in probe_shell, (
            f"probe must NOT include .tr* (waveforms): {probe_shell!r}"
        )

    def test_probe_returns_sum_for_numeric_awk_output(self):
        # Direct unit test on _probe_outputs_size_safe: stat+awk emits
        # a single integer total → probe returns int(out).
        worker = HspiceWorker(_cfg())
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            return_value=_ok(stdout="48576\n"),
        ):
            size = worker._probe_outputs_size_safe(
                run_dir="/tmp/hsp", sp_base="sim", run_id="testrun",
            )
        assert size == 48576

    def test_probe_returns_none_when_no_outputs_yet(self):
        # Empty awk stdout (no output files yet → NR=0 → no print) maps
        # to None — preserves the early-run inconclusive semantic from
        # the .lis-only probe so the idle counter doesn't reset before
        # hspice has flushed its first byte.
        worker = HspiceWorker(_cfg())
        with mock.patch(
            "src.hspice_worker.subprocess.run",
            return_value=_ok(stdout=""),
        ):
            size = worker._probe_outputs_size_safe(
                run_dir="/tmp/hsp", sp_base="sim", run_id="testrun",
            )
        assert size is None
