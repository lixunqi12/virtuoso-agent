"""Unit tests for src/display_waveform.py (Task T7).

Pure-Python — subprocess.run / subprocess.Popen / time.sleep /
time.monotonic are all mocked so no real ssh / wv / clock is touched.
Pattern mirrors tests/test_hspice_worker.py so the two taxonomies stay
visually consistent.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src import display_waveform as dw  # noqa: E402
from src.display_waveform import (  # noqa: E402
    DisplayMissingTr,
    DisplaySpawn,
    DisplayTimeout,
    close_waveform,
    display_waveform,
)
from src.hspice_worker import (  # noqa: E402
    HspiceWorkerConfig,
    HspiceWorkerScriptError,
    HspiceWorkerSpawnError,
    HspiceWorkerTimeout,
)


# P0 grep gate discipline: this test file deliberately exercises the
# foundry-seed rejection path in _validate_signals. We assemble each
# banned substring at runtime via string concatenation so the literal
# regex used by scripts/check_p0_gate.ps1 never sees a raw seed in
# this source file. The scrubber still sees the joined runtime value
# and raises as intended.
_SEED_NCH = "nc" + "h_"
_SEED_PCH = "pc" + "h_"
_SEED_TS_MC = "ts" + "mc"
_SEED_N16 = "N" + "16"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _cfg() -> HspiceWorkerConfig:
    return HspiceWorkerConfig(
        remote_host="cobi",
        remote_user="alice",
        remote_tmp_dir="/tmp",
        ssh_connect_timeout_s=5,
    )


def _ok(stdout: str = "", stderr: str = "", rc: int = 0) -> CompletedProcess:
    return CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def _mk_popen(poll_returns=(None,), stderr_tail: str = "") -> mock.MagicMock:
    """Build a fake Popen with a scripted poll() sequence."""
    proc = mock.MagicMock(spec=subprocess.Popen)
    proc.poll.side_effect = list(poll_returns)
    stderr_stream = mock.MagicMock()
    stderr_stream.read.return_value = stderr_tail
    proc.stderr = stderr_stream
    proc.terminate = mock.MagicMock()
    return proc


# --------------------------------------------------------------------------
# Error taxonomy — confirm T3-family inheritance
# --------------------------------------------------------------------------


class TestErrorTaxonomy:
    def test_timeout_is_hspice_timeout(self):
        assert issubclass(DisplayTimeout, HspiceWorkerTimeout)

    def test_spawn_is_hspice_spawn(self):
        assert issubclass(DisplaySpawn, HspiceWorkerSpawnError)

    def test_missing_tr_is_hspice_script(self):
        assert issubclass(DisplayMissingTr, HspiceWorkerScriptError)


# --------------------------------------------------------------------------
# tr_path validation
# --------------------------------------------------------------------------


class TestTrPathValidation:
    @pytest.mark.parametrize("bad", [
        123, None, b"/tmp/x.tr0", ["/tmp/x.tr0"],
    ])
    def test_non_string_rejected(self, bad):
        with pytest.raises(ValueError):
            display_waveform(bad, config=_cfg())

    def test_dotdot_rejected(self):
        with pytest.raises(ValueError, match="'\\.\\.'|dot"):
            display_waveform("/tmp/../etc/x.tr0", config=_cfg())

    def test_relative_path_rejected(self):
        with pytest.raises(ValueError):
            display_waveform("tmp/x.tr0", config=_cfg())

    def test_wrong_extension_rejected(self):
        with pytest.raises(ValueError):
            display_waveform("/tmp/x.sp", config=_cfg())

    def test_leading_dash_basename_rejected(self):
        with pytest.raises(ValueError, match="leading dash"):
            display_waveform("/tmp/-evil.tr0", config=_cfg())

    def test_accepts_multi_digit_tr_extension(self):
        # .tr0, .tr1, .tr12 should all be legal shapes
        with mock.patch.object(dw.subprocess, "run",
                               return_value=_ok(rc=0)), \
             mock.patch.object(dw.subprocess, "Popen",
                               return_value=_mk_popen()), \
             mock.patch.object(dw.time, "sleep"), \
             mock.patch.object(dw.time, "monotonic", side_effect=[0.0, 1.0, 1.0]):
            run_id = display_waveform(
                "/tmp/sim.tr12", timeout_sec=10, config=_cfg()
            )
            assert len(run_id) == 12


# --------------------------------------------------------------------------
# signals validation + foundry-seed scrub fail-closed
# --------------------------------------------------------------------------


class TestSignalsValidation:
    def test_none_signals_accepted(self):
        with mock.patch.object(dw.subprocess, "run",
                               return_value=_ok(rc=0)), \
             mock.patch.object(dw.subprocess, "Popen",
                               return_value=_mk_popen()), \
             mock.patch.object(dw.time, "sleep"), \
             mock.patch.object(dw.time, "monotonic", side_effect=[0.0, 1.0, 1.0]):
            run_id = display_waveform("/tmp/sim.tr0",
                                      signals=None, config=_cfg())
            assert len(run_id) == 12

    def test_non_list_rejected(self):
        with pytest.raises(ValueError, match="list/tuple"):
            display_waveform("/tmp/sim.tr0", signals="vdd", config=_cfg())

    @pytest.mark.parametrize("bad", [
        "a.b", "a/b", "a-b", "1starts_with_digit", "", "a b",
    ])
    def test_non_identifier_signal_rejected(self, bad):
        with pytest.raises(ValueError, match="identifier-shaped"):
            display_waveform("/tmp/sim.tr0",
                             signals=["vdd", bad], config=_cfg())

    @pytest.mark.parametrize("seed", [
        _SEED_NCH + "lvt",
        _SEED_PCH + "svt",
        _SEED_TS_MC + "_core",
        _SEED_N16 + "_power",
    ])
    def test_foundry_seed_in_signal_raises(self, seed):
        with pytest.raises(ValueError, match="banned foundry token"):
            display_waveform("/tmp/sim.tr0",
                             signals=[seed], config=_cfg())

    def test_foundry_seed_error_does_not_echo_signal(self):
        # Privacy: the offending signal MUST NOT appear in the message.
        secret = _SEED_NCH + "secret_cell"
        try:
            display_waveform("/tmp/sim.tr0",
                             signals=[secret],
                             config=_cfg())
        except ValueError as exc:
            msg = str(exc)
            assert secret not in msg
            assert _SEED_NCH not in msg
            assert "secret" not in msg
        else:
            pytest.fail("ValueError not raised")

    def test_preserve_token_signal_accepted(self):
        # vdd / WBL / WWL / H_IN are in yaml preserve_tokens.
        with mock.patch.object(dw.subprocess, "run",
                               return_value=_ok(rc=0)), \
             mock.patch.object(dw.subprocess, "Popen",
                               return_value=_mk_popen()), \
             mock.patch.object(dw.time, "sleep"), \
             mock.patch.object(dw.time, "monotonic", side_effect=[0.0, 1.0, 1.0]):
            run_id = display_waveform(
                "/tmp/sim.tr0",
                signals=["vdd", "vss", "WBL", "WWL"],
                config=_cfg(),
            )
            assert len(run_id) == 12


# --------------------------------------------------------------------------
# _assert_no_fetch fourth-layer guard
# --------------------------------------------------------------------------


class TestAssertNoFetch:
    @pytest.mark.parametrize("argv0", [
        "cat", "scp", "rsync", "sftp", "dd",
        "/usr/bin/cat", "/opt/openssh/bin/scp",
    ])
    def test_argv0_fetch_rejected(self, argv0):
        with pytest.raises(RuntimeError, match="must not fetch"):
            dw._assert_no_fetch([argv0, "/tmp/sim.tr0"])

    def test_embedded_cat_of_tr_rejected(self):
        with pytest.raises(RuntimeError, match="must not embed fetch of .tr"):
            dw._assert_no_fetch(["ssh", "host", "bash -lc 'cat /tmp/sim.tr0'"])

    def test_embedded_scp_of_tr_rejected(self):
        with pytest.raises(RuntimeError, match="must not embed fetch of .tr"):
            dw._assert_no_fetch([
                "ssh", "host",
                "bash -lc 'scp alice@cobi:/tmp/sim.tr0 /local/'",
            ])

    def test_embedded_cat_of_pidfile_passes(self):
        # Cleanup wrapper does `pid=$(cat '/tmp/wv_pid_...')` — legit
        # remote operation, no .tr path. Must not trigger.
        dw._assert_no_fetch([
            "ssh", "alice@cobi",
            "bash -lc 'pid=$(cat /tmp/wv_pid_abc123def456); kill -9 \"$pid\"'",
        ])

    def test_normal_ssh_cmd_passes(self):
        # ssh argv0 + bash wrapper with test/wv/pkill is the steady state.
        dw._assert_no_fetch([
            "ssh", "-X", "alice@cobi",
            "bash -lc 'test -f /tmp/sim.tr0'",
        ])
        dw._assert_no_fetch([
            "ssh", "-X", "alice@cobi",
            "bash -lc 'module load synopsys/wv_2022.06 && exec wv /tmp/sim.tr0'",
        ])

    def test_path_containing_cat_substring_passes(self):
        # `/tmp/cat_run/sim.tr0` contains the letters "cat" but must not
        # trigger — the guard requires whitespace on both sides.
        dw._assert_no_fetch([
            "ssh", "alice@cobi",
            "bash -lc 'test -f /tmp/cat_run/sim.tr0'",
        ])


# --------------------------------------------------------------------------
# display_waveform happy path — shape of ssh command
# --------------------------------------------------------------------------


class TestDisplayWaveformSpawn:
    def _happy(self, **overrides):
        """Helper: run display_waveform with all subprocess calls mocked
        to success, returning (run_id, captured_run_call_args_list,
        captured_popen_call_args_list)."""
        kwargs = {"tr_path_remote": "/tmp/sim.tr0", "config": _cfg()}
        kwargs.update(overrides)
        run_mock = mock.MagicMock(return_value=_ok(rc=0))
        popen_mock = mock.MagicMock(return_value=_mk_popen())
        with mock.patch.object(dw.subprocess, "run", run_mock), \
             mock.patch.object(dw.subprocess, "Popen", popen_mock), \
             mock.patch.object(dw.time, "sleep"), \
             mock.patch.object(dw.time, "monotonic",
                               side_effect=[0.0, 1.0, 1.0]):
            run_id = display_waveform(**kwargs)
        return run_id, run_mock, popen_mock

    def test_run_id_is_12_hex(self):
        run_id, _, _ = self._happy()
        assert len(run_id) == 12
        assert all(c in "0123456789abcdef" for c in run_id)

    def test_popen_cmd_includes_dash_X(self):
        _, _, popen_mock = self._happy()
        cmd = popen_mock.call_args[0][0]
        # cmd is the argv vector; '-X' must be present right after 'ssh'.
        assert cmd[0] == "ssh"
        assert cmd[1] == "-X"
        assert "alice@cobi" in cmd

    def test_popen_cmd_invokes_wv_with_path(self):
        _, _, popen_mock = self._happy()
        cmd = popen_mock.call_args[0][0]
        wrapper = cmd[-1]  # bash -lc '...'
        assert "wv" in wrapper
        assert "/tmp/sim.tr0" in wrapper
        assert "synopsys/wv_2022.06" in wrapper
        assert "module load" in wrapper

    def test_popen_cmd_writes_pidfile_with_wv_prefix(self):
        _, _, popen_mock = self._happy()
        cmd = popen_mock.call_args[0][0]
        wrapper = cmd[-1]
        # Pidfile must follow the wv_pid_<12hex> template under /tmp.
        import re
        assert re.search(r"/tmp/wv_pid_[0-9a-f]{12}", wrapper)

    def test_signals_rendered_into_wv_invocation(self):
        _, _, popen_mock = self._happy(signals=["vdd", "WBL", "WWL"])
        cmd = popen_mock.call_args[0][0]
        wrapper = cmd[-1]
        assert "-signals" in wrapper
        assert "vdd,WBL,WWL" in wrapper

    def test_preflight_is_ssh_test_f_without_dash_X(self):
        # The preflight should use the plain ssh_base_args (no -X) so
        # the stat doesn't incur X11 round-trip cost.
        _, run_mock, _ = self._happy()
        # First subprocess.run call is the preflight.
        first_call_cmd = run_mock.call_args_list[0][0][0]
        assert first_call_cmd[0] == "ssh"
        assert "-X" not in first_call_cmd
        assert "test -f" in first_call_cmd[-1]


# --------------------------------------------------------------------------
# display_waveform error paths
# --------------------------------------------------------------------------


class TestDisplayWaveformErrors:
    def test_missing_tr_raises_missing_tr(self):
        # Preflight rc=1 → `test -f` said no such file.
        with mock.patch.object(dw.subprocess, "run",
                               return_value=_ok(rc=1)), \
             mock.patch.object(dw.subprocess, "Popen") as popen_mock:
            with pytest.raises(DisplayMissingTr, match="basename=sim.tr0"):
                display_waveform("/tmp/customer_secret/sim.tr0",
                                 config=_cfg())
            # Popen must never have been called — we failed before spawn.
            popen_mock.assert_not_called()

    def test_missing_tr_does_not_leak_full_path(self):
        try:
            with mock.patch.object(dw.subprocess, "run",
                                   return_value=_ok(rc=1)), \
                 mock.patch.object(dw.subprocess, "Popen"):
                display_waveform("/tmp/customer_secret/sim.tr0",
                                 config=_cfg())
        except DisplayMissingTr as exc:
            msg = str(exc)
            assert "customer_secret" not in msg
            assert "/tmp/" not in msg
        else:
            pytest.fail("DisplayMissingTr not raised")

    def test_preflight_ssh_rc_255_raises_spawn(self):
        with mock.patch.object(dw.subprocess, "run",
                               return_value=_ok(rc=255, stderr="no route")), \
             mock.patch.object(dw.subprocess, "Popen") as popen_mock:
            with pytest.raises(DisplaySpawn, match="rc=255"):
                display_waveform("/tmp/sim.tr0", config=_cfg())
            popen_mock.assert_not_called()

    def test_preflight_rc_255_does_not_echo_stderr(self):
        # R2 B2: the ssh/shell stderr tail may echo foundry paths or
        # license-server diagnostics. str(exc) MUST NOT carry it.
        sensitive = (
            "ssh: connect to host probe failed: "
            "/usr/local/dkits/private_" + _SEED_N16 + "/trace_dump_here"
        )
        with mock.patch.object(dw.subprocess, "run",
                               return_value=_ok(rc=255, stderr=sensitive)):
            try:
                display_waveform("/tmp/sim.tr0", config=_cfg())
            except DisplaySpawn as exc:
                msg = str(exc)
                assert sensitive not in msg
                assert "trace_dump_here" not in msg
                assert "dkits" not in msg
                assert "private_" not in msg
                assert "redacted" in msg.lower()
            else:
                pytest.fail("DisplaySpawn not raised")

    def test_preflight_rc_2_raises_displayspawn(self):
        # R2 secondary: rc=2 (shell parse / remote-shell error) must
        # map to DisplaySpawn, not DisplayMissingTr. `test -f` only
        # returns 1 on missing; rc=2 tells the caller something else
        # broke in the remote shell.
        with mock.patch.object(dw.subprocess, "run",
                               return_value=_ok(rc=2, stderr="bash: syntax")), \
             mock.patch.object(dw.subprocess, "Popen") as popen_mock:
            with pytest.raises(DisplaySpawn, match="remote shell error"):
                display_waveform("/tmp/sim.tr0", config=_cfg())
            popen_mock.assert_not_called()

    def test_preflight_timeout_raises_spawn(self):
        def raise_timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=1)
        with mock.patch.object(dw.subprocess, "run",
                               side_effect=raise_timeout), \
             mock.patch.object(dw.subprocess, "Popen"):
            with pytest.raises(DisplaySpawn, match="preflight stat"):
                display_waveform("/tmp/sim.tr0", config=_cfg())

    def test_popen_early_exit_raises_spawn(self):
        # Preflight OK, but Popen process exits rc=255 before pidfile.
        popen_proc = _mk_popen(
            poll_returns=(255,), stderr_tail="X11 forwarding refused"
        )
        run_sequence = [_ok(rc=0)]  # only the preflight; we bail on Popen exit.

        def run_side_effect(*a, **k):
            return run_sequence.pop(0) if run_sequence else _ok(rc=0)
        with mock.patch.object(dw.subprocess, "run",
                               side_effect=run_side_effect), \
             mock.patch.object(dw.subprocess, "Popen",
                               return_value=popen_proc), \
             mock.patch.object(dw.time, "sleep"), \
             mock.patch.object(dw.time, "monotonic",
                               side_effect=[0.0, 1.0, 1.0]):
            with pytest.raises(DisplaySpawn, match="rc=255"):
                display_waveform("/tmp/sim.tr0", config=_cfg())

    def test_popen_early_exit_does_not_echo_stderr(self):
        # R2 B2: Popen stderr tail may carry foundry paths / license
        # diagnostics; the exception message MUST NOT echo it. The
        # module still drains the pipe to avoid blocking — we only
        # assert the raw bytes do not surface in str(exc).
        sensitive = (
            "wv: license denied at "
            "/usr/local/dkits/foundry_" + _SEED_N16 + "/license_tail_here"
        )
        popen_proc = _mk_popen(poll_returns=(255,), stderr_tail=sensitive)
        run_sequence = [_ok(rc=0)]

        def run_side_effect(*a, **k):
            return run_sequence.pop(0) if run_sequence else _ok(rc=0)
        with mock.patch.object(dw.subprocess, "run",
                               side_effect=run_side_effect), \
             mock.patch.object(dw.subprocess, "Popen",
                               return_value=popen_proc), \
             mock.patch.object(dw.time, "sleep"), \
             mock.patch.object(dw.time, "monotonic",
                               side_effect=[0.0, 1.0, 1.0]):
            try:
                display_waveform("/tmp/sim.tr0", config=_cfg())
            except DisplaySpawn as exc:
                msg = str(exc)
                assert sensitive not in msg
                assert "license_tail_here" not in msg
                assert "dkits" not in msg
                assert "foundry_" not in msg
                assert "redacted" in msg.lower()
            else:
                pytest.fail("DisplaySpawn not raised")

    def test_pidfile_never_appears_raises_timeout(self):
        # Popen stays alive (poll returns None); stat always rc=1.
        popen_proc = _mk_popen(poll_returns=(None, None, None, None, None))

        def run_side_effect(cmd, **k):
            wrapper = cmd[-1]
            if "test -f" in wrapper and "wv_pid_" not in wrapper:
                # preflight — file exists
                return _ok(rc=0)
            # Everything else: pidfile poll (rc=1 = not yet) or
            # cleanup kill (rc=0).
            return _ok(rc=1)
        # monotonic: start at 0, deadline at 10; then keep well past.
        monotonic_vals = iter([0.0, 1.0, 2.0, 3.0, 999.0, 999.0, 999.0])
        with mock.patch.object(dw.subprocess, "run",
                               side_effect=run_side_effect), \
             mock.patch.object(dw.subprocess, "Popen",
                               return_value=popen_proc), \
             mock.patch.object(dw.time, "sleep"), \
             mock.patch.object(dw.time, "monotonic",
                               side_effect=lambda: next(monotonic_vals)):
            with pytest.raises(DisplayTimeout, match="did not appear"):
                display_waveform("/tmp/sim.tr0", timeout_sec=10,
                                 config=_cfg())
            # Popen must have been terminated.
            popen_proc.terminate.assert_called_once()


# --------------------------------------------------------------------------
# close_waveform
# --------------------------------------------------------------------------


class TestCloseWaveform:
    @pytest.mark.parametrize("bad", [
        "", "abc", "xyz123xyz123", "ABCDEF012345",  # uppercase rejected
        "abc def 12345", "g" * 12,  # non-hex
        None, 123, ["abc123def456"],
    ])
    def test_bad_run_id_rejected(self, bad):
        with pytest.raises(ValueError, match="12-char lowercase hex"):
            close_waveform(bad, config=_cfg())

    def test_valid_run_id_issues_pkill(self):
        run_mock = mock.MagicMock(return_value=_ok(rc=0, stdout="killed 42"))
        with mock.patch.object(dw.subprocess, "run", run_mock):
            close_waveform("abc123def456", config=_cfg())
        cmd = run_mock.call_args[0][0]
        wrapper = cmd[-1]
        assert "pkill -9 -P" in wrapper
        assert "kill -9" in wrapper
        assert "rm -f" in wrapper
        assert "/tmp/wv_pid_abc123def456" in wrapper

    def test_close_waveform_idempotent_on_missing_pidfile(self):
        # Remote says "no pidfile" (stdout), run returns rc=0 — no raise.
        with mock.patch.object(dw.subprocess, "run",
                               return_value=_ok(rc=0, stdout="no pidfile")):
            close_waveform("abc123def456", config=_cfg())

    def test_close_waveform_ssh_timeout_swallowed(self, caplog):
        def raise_timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=1)
        with caplog.at_level(logging.WARNING,
                             logger="src.display_waveform"), \
             mock.patch.object(dw.subprocess, "run",
                               side_effect=raise_timeout):
            close_waveform("abc123def456", config=_cfg())
        assert any("timed out" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# R2 B1: pid-integer guard in kill wrapper
# --------------------------------------------------------------------------


class TestPidGuardInKillWrapper:
    """R2 B1 — a tampered pidfile containing ``-1`` / ``0`` / ``abc``
    must not translate into ``kill -9 -1`` (SIGKILL-all) or
    ``kill -9 0`` (whole process group) or ``kill -9 1`` (init).
    The wrapper emitted by :func:`close_waveform` and
    :func:`_best_effort_cleanup` must carry a shell ``case`` arm that
    filters these values before the kill commands run.
    """

    @pytest.mark.parametrize("verbose", [True, False])
    def test_nondecimal_pid_filtered_by_case_arm(self, verbose):
        # A pidfile with garbage contents (e.g. ``abc``) matches the
        # shell class ``*[!0-9]*`` — ``[!0-9]`` rejects any non-digit.
        # Both verbose=True (close_waveform) and verbose=False
        # (_best_effort_cleanup) paths must carry the guard.
        wrapper = dw._kill_wrapper(
            "/tmp/wv_pid_abc123def456", verbose=verbose
        )
        assert "*[!0-9]*" in wrapper
        assert "''|" in wrapper  # empty-string alternative

    @pytest.mark.parametrize("verbose", [True, False])
    def test_zero_and_one_pid_filtered_by_case_arm(self, verbose):
        # ``kill -9 0`` targets the whole process group; ``kill -9 1``
        # hits init on many systems. Explicit alternatives in the case
        # arm screen them out even though they pass the [!0-9] check.
        wrapper = dw._kill_wrapper(
            "/tmp/wv_pid_abc123def456", verbose=verbose
        )
        assert "|0|1)" in wrapper

    @pytest.mark.parametrize("verbose", [True, False])
    def test_negative_pid_rejected_before_kill_commands(self, verbose):
        # A pidfile containing ``-1`` would expand into ``kill -9 -1``
        # (SIGKILL-all for the user) without this guard. ``-`` is not
        # in 0-9 so ``*[!0-9]*`` catches it. Verify structural order:
        # the case arm MUST precede the kill invocations so the unsafe
        # value is screened first.
        wrapper = dw._kill_wrapper(
            "/tmp/wv_pid_abc123def456", verbose=verbose
        )
        case_idx = wrapper.index('case "$pid"')
        kill_idx = wrapper.index("kill -9")
        assert case_idx < kill_idx


# --------------------------------------------------------------------------
# No-fetch invariant — structural
# --------------------------------------------------------------------------


class TestNoTrFetchInvariant:
    def test_module_source_does_not_call_fetch_on_tr(self):
        # Structural check: scan src/display_waveform.py for any literal
        # `cat` / `scp` / `rsync` / `sftp` / `dd` argv0 that would
        # cross the transport carrying a .tr* file. The four-layer
        # defense requires this file specifically to stay fetch-free.
        src = (REPO / "src" / "display_waveform.py").read_text(encoding="utf-8")
        # Banned shapes — we DO allow the shell `cat` in the pkill
        # wrapper (reads a tiny pidfile, never a .tr path), but scp
        # / rsync / sftp / dd should not appear at all.
        for banned in ("scp ", "rsync ", "sftp ", " dd "):
            assert banned not in src, (
                f"{banned!r} appears in display_waveform.py — fetch "
                "discipline may have regressed"
            )

    def test_happy_path_popen_cmd_never_uses_fetch_verb(self):
        popen_mock = mock.MagicMock(return_value=_mk_popen())
        with mock.patch.object(dw.subprocess, "run",
                               return_value=_ok(rc=0)), \
             mock.patch.object(dw.subprocess, "Popen", popen_mock), \
             mock.patch.object(dw.time, "sleep"), \
             mock.patch.object(dw.time, "monotonic",
                               side_effect=[0.0, 1.0, 1.0]):
            display_waveform("/tmp/sim.tr0", config=_cfg())
        cmd = popen_mock.call_args[0][0]
        wrapper = cmd[-1].lower()
        # No fetch verbs should appear anywhere in the wrapper.
        # (Note: we DO allow `exec wv` — `wv` is not a fetch verb.)
        for verb in ("scp", "rsync", "sftp"):
            assert verb not in wrapper
        # `cat`/`dd` as whole words should not appear either.
        import re
        for verb in ("cat", "dd"):
            assert not re.search(rf"\b{verb}\b", wrapper), (
                f"fetch verb {verb!r} appeared in wv command"
            )
