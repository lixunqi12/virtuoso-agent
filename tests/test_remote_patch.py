"""Unit tests for src.remote_patch (T8.3-fix).

Verifies the four hard constraints:
  1. The script body that ssh transports outward contains NO
     local-side .sp file content (only the proposed key/value pairs
     and the embedded rewriter source).
  2. Any foundry-token leak in remote stderr is sanitized through
     :func:`src.remote_patch._sanitize_remote_stderr` BEFORE it is
     re-raised as :class:`RemotePatchError`.
  3. The first patch of a given remote path triggers a backup; the
     second patch of the same path skips the backup. (cp -n
     equivalent; per-instance ``_backed_up_remote_paths`` state.)
  4. The transmitted script writes to ``<path>.tmp.<pid>`` then
     ``os.rename`` to the original path -- atomic, single shell
     chain, ssh interrupt mid-write leaves the original intact.

Plus a parity test that the embedded rewriter source produces
byte-identical output to :func:`src.sp_rewrite.rewrite_params` on
the same fixture (anti-drift guard for the base64 embedding).

These tests mock ``subprocess.run`` so no actual ssh is required.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from src.remote_patch import (
    RemotePatchError,
    RemotePatchResult,
    RemotePatcher,
    _parse_status_lines,
    _sanitize_remote_stderr,
)
from src.sp_rewrite import rewrite_params


# --------------------------------------------------------------------- #
#  Fixtures
# --------------------------------------------------------------------- #

DELAY_TB = (
    ".TEMP 27\n"
    ".OPTION POST\n"
    "\n"
    ".PARAM delay = 50p\n"
    "+ SIGN = 0V\n"
    "+ LSB = 0V\n"
    "+ LSB2 = 0V\n"
    "+ MSB = 0V\n"
    "+ hinvoltage = 0\n"
    "\n"
    "V1 vdd 0 0.8V\n"
    "\n"
    ".END\n"
)

DELAY_WHITELIST = ("delay", "hinvoltage", "sign", "lsb", "lsb2", "msb")


def _ok_completed(stdout: str, stderr: str = "", returncode: int = 0):
    """Build a CompletedProcess matching subprocess.run's text=True API."""
    return subprocess.CompletedProcess(
        args=["ssh", "host", "python3", "-"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# --------------------------------------------------------------------- #
#  Constraint 1: NO file body crosses ssh outward.
# --------------------------------------------------------------------- #

class TestNoFileBodyOutward:

    def test_rendered_script_does_not_contain_netlist_body(self):
        """The script sent to ssh must NOT carry any .sp file content
        from the local side. It carries only:
          * the embedded rewriter source (src/sp_rewrite.py)
          * a JSON config blob with new_params + whitelist + path
        """
        script = RemotePatcher._render_remote_script(
            remote_path="/work/sim/sample_tb.sp",
            new_params={"num_finger_n0": 2, "num_finger_p1": 3},
            whitelist=["num_finger_n0", "num_finger_p1"],
            do_backup=True,
            backup_ts="20260426_103000",
        )
        # No local .sp body markers should appear.
        assert ".PARAM delay = 50p" not in script
        assert "+ SIGN = 0V" not in script
        assert "V1 vdd 0 0.8V" not in script
        # Script DOES carry the proposed values + path + sp_rewrite source.
        assert "num_finger_n0" in script
        assert "/work/sim/sample_tb.sp" in script

    def test_subprocess_input_carries_no_local_sp_body(self, tmp_path):
        """Mock ssh; assert the actual `input=` byte stream sent to
        subprocess.run never contains the local .sp body."""
        # Stage a local file that we are NOT going to send.
        local_sp = tmp_path / "scrubbed_local.sp"
        local_sp.write_text(
            ".PARAM delay = 50p\n"
            "* foundry-leak-canary should never appear in ssh stdin\n",
            encoding="utf-8",
        )
        captured: dict = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            captured["input"] = kw.get("input", "")
            return _ok_completed("OK_BACKUP: /work/sim/foo.sp.orig_x\nOK: 1 keys patched\n")

        patcher = RemotePatcher(ssh_args=["ssh", "user@host"])
        with patch("src.remote_patch.subprocess.run", side_effect=fake_run):
            patcher.patch(
                "/work/sim/foo.sp", {"delay": "75p"}, ["delay"],
            )

        sent = captured["input"]
        # The local .sp's body must not have crossed.
        assert "foundry-leak-canary" not in sent
        assert local_sp.read_text(encoding="utf-8") not in sent
        # argv must not carry the path or the keys (ps + auth.log safety).
        assert captured["cmd"] == ["ssh", "user@host", "python3", "-"]
        assert "/work/sim/foo.sp" not in " ".join(captured["cmd"])
        assert "delay" not in " ".join(captured["cmd"])
        assert "75p" not in " ".join(captured["cmd"])


# --------------------------------------------------------------------- #
#  Constraint 2: foundry-token leak in stderr is sanitized.
# --------------------------------------------------------------------- #

class TestStderrSanitization:

    def test_sanitize_masks_foundry_tokens(self):
        leaky = (
            "Traceback (most recent call last):\n"
            "  File \"<sp_rewrite>\", line 32, in _rewrite\n"
            "ValueError: model 'nch_25_lvt_mac' has bad fingers; "
            "also pch_18, cfmom_x, rppoly_a, rm1_top, tsmc_pdk_v2, "
            "tcbn28hpcplusbwp30p140 - all sensitive\n"
        )
        clean = _sanitize_remote_stderr(leaky)
        for token in (
            "nch_25_lvt_mac", "pch_18", "cfmom_x", "rppoly_a",
            "rm1_top", "tsmc_pdk_v2", "tcbn28hpcplusbwp30p140",
        ):
            assert token not in clean, f"token {token!r} survived sanitization"
        # Structural traceback context survives -- we want operators
        # to see WHERE the failure was, just not WHAT model leaked.
        assert "<sp_rewrite>" in clean
        assert "line 32" in clean
        assert "<REDACTED>" in clean

    def test_remote_failure_reraise_is_sanitized(self):
        """When the remote side returns a non-zero exit with stderr
        carrying a foundry token, the local RemotePatchError message
        must NOT carry that token."""
        leaky_stderr = "FAIL: unexpected RuntimeError nch_25_lvt_mac at line 12\n"

        def fake_run(cmd, **kw):
            return _ok_completed(stdout="", stderr=leaky_stderr, returncode=4)

        patcher = RemotePatcher(ssh_args=["ssh", "host"])
        with patch("src.remote_patch.subprocess.run", side_effect=fake_run):
            with pytest.raises(RemotePatchError) as exc:
                patcher.patch("/work/sim/foo.sp", {"delay": "75p"}, ["delay"])
        msg = str(exc.value)
        assert "nch_25_lvt_mac" not in msg
        assert "<REDACTED>" in msg
        assert "rc=4" in msg

    def test_stdout_is_sanitized_too(self):
        """Defense in depth: if a foundry token somehow ends up in
        the OK_BACKUP path or the OK status (e.g. operator points
        the agent at a remote path that itself contains a foundry
        token), the parsed result must not leak it either."""
        stdout = (
            "OK_BACKUP: /work/sim/tsmc_dump_v2/foo.sp.orig_20260426_103000\n"
            "OK: 1 keys patched\n"
        )

        def fake_run(cmd, **kw):
            return _ok_completed(stdout=stdout)

        patcher = RemotePatcher(ssh_args=["ssh", "host"])
        with patch("src.remote_patch.subprocess.run", side_effect=fake_run):
            res = patcher.patch("/work/sim/foo.sp", {"delay": "75p"}, ["delay"])
        assert "tsmc" not in (res.backup_path or "")
        assert "<REDACTED>" in (res.backup_path or "")


# --------------------------------------------------------------------- #
#  Constraint 3: first-touch backup; subsequent calls skip.
# --------------------------------------------------------------------- #

class TestFirstTouchBackup:

    def test_first_call_requests_backup_second_call_skips(self):
        """Inspect the rendered script's DO_BACKUP flag across two
        sequential patches of the same remote path."""
        scripts: list[str] = []

        def fake_run(cmd, **kw):
            scripts.append(kw["input"])
            ts = "20260426_103000"
            return _ok_completed(
                f"OK_BACKUP: /work/sim/foo.sp.orig_{ts}\nOK: 1 keys patched\n"
            )

        patcher = RemotePatcher(ssh_args=["ssh", "host"])
        with patch("src.remote_patch.subprocess.run", side_effect=fake_run):
            patcher.patch("/work/sim/foo.sp", {"delay": "75p"}, ["delay"])
            patcher.patch("/work/sim/foo.sp", {"delay": "80p"}, ["delay"])

        # Both scripts contain the config blob; check the JSON's
        # do_backup flag without exec'ing the script.
        assert _do_backup_flag(scripts[0]) is True, \
            "first patch must request backup"
        assert _do_backup_flag(scripts[1]) is False, \
            "second patch of same path must skip backup"

    def test_different_remote_paths_each_get_backup(self):
        scripts: list[str] = []

        def fake_run(cmd, **kw):
            scripts.append(kw["input"])
            return _ok_completed(
                "OK_BACKUP: /work/sim/x.sp.orig_x\nOK: 1 keys patched\n"
            )

        patcher = RemotePatcher(ssh_args=["ssh", "host"])
        with patch("src.remote_patch.subprocess.run", side_effect=fake_run):
            patcher.patch("/work/sim/a.sp", {"delay": "75p"}, ["delay"])
            patcher.patch("/work/sim/b.sp", {"delay": "75p"}, ["delay"])

        assert _do_backup_flag(scripts[0]) is True
        assert _do_backup_flag(scripts[1]) is True

    def test_backup_path_uses_orig_timestamp_pattern(self):
        """The remote backup path must follow ``<path>.orig_YYYYMMDD_HHMMSS``."""

        def fake_run(cmd, **kw):
            cfg = _config_from_script(kw["input"])
            backup = cfg["remote_path"] + ".orig_" + cfg["backup_ts"]
            return _ok_completed(f"OK_BACKUP: {backup}\nOK: 1 keys patched\n")

        patcher = RemotePatcher(ssh_args=["ssh", "host"])
        with patch("src.remote_patch.subprocess.run", side_effect=fake_run):
            res = patcher.patch("/work/sim/x.sp", {"delay": "75p"}, ["delay"])

        assert res.backup_path is not None
        assert re.match(
            r"^/work/sim/x\.sp\.orig_\d{8}_\d{6}$", res.backup_path,
        ), f"backup path {res.backup_path!r} does not match orig pattern"


# --------------------------------------------------------------------- #
#  R2 (codex 2026-04-26): backup race + missing-file + exception args.
# --------------------------------------------------------------------- #

class TestR2BackupRaceFix:
    """Two parallel RemotePatcher instances against the same remote
    path within the same second compute IDENTICAL backup filenames.
    The previous ``if not os.path.exists + shutil.copy2`` raced
    (both could pass the check, both copied; the loser overwrote
    already-mutated bytes). The R2 fix is ``os.open(O_CREAT|O_EXCL)``
    -- earliest caller wins, others see ``OK_BACKUP_EXISTS``.
    """

    def test_remote_script_uses_o_excl_atomic_create(self):
        """Structural assert: the rendered script must use the atomic
        O_CREAT|O_EXCL primitive, not the racy 'check then copy'."""
        script = RemotePatcher._render_remote_script(
            remote_path="/work/sim/foo.sp",
            new_params={"delay": "75p"},
            whitelist=["delay"],
            do_backup=True,
            backup_ts="20260426_103000",
        )
        assert "os.O_CREAT" in script
        assert "os.O_EXCL" in script
        assert "os.O_WRONLY" in script
        # The non-atomic predecessor must be GONE. ``os.path.exists``
        # against the backup path was the racy check; ``shutil.copy2``
        # was the pair the race straddled.
        assert "if not os.path.exists(backup_path):" not in script
        assert "shutil.copy2(REMOTE_PATH, backup_path)" not in script
        # Both branches must emit a parseable status line so the
        # local side knows the backup is in place either way.
        assert "OK_BACKUP: " in script
        assert "OK_BACKUP_EXISTS: " in script
        # FileExistsError is the kernel signal we lost the race.
        assert "FileExistsError" in script

    def test_concurrent_collision_loser_sees_exists(self):
        """Two RemotePatcher instances back up the same remote_path.
        The first wins the O_EXCL race (OK_BACKUP); the second loses
        (OK_BACKUP_EXISTS). Both report a backup_path so both local
        agents know the backup is in place; the second flags
        ``backup_already_existed=True`` for telemetry.
        """
        state = {"created": False}

        def fake_run(cmd, **kw):
            cfg = _config_from_script(kw["input"])
            bp = cfg["remote_path"] + ".orig_" + cfg["backup_ts"]
            if not state["created"]:
                state["created"] = True
                return _ok_completed(
                    f"OK_BACKUP: {bp}\nOK: 1 keys patched\n"
                )
            return _ok_completed(
                f"OK_BACKUP_EXISTS: {bp}\nOK: 1 keys patched\n"
            )

        p1 = RemotePatcher(ssh_args=["ssh", "host"])
        p2 = RemotePatcher(ssh_args=["ssh", "host"])
        with patch("src.remote_patch.subprocess.run", side_effect=fake_run):
            r1 = p1.patch("/work/sim/foo.sp", {"delay": "75p"}, ["delay"])
            r2 = p2.patch("/work/sim/foo.sp", {"delay": "80p"}, ["delay"])

        # Earliest snapshot wins: r1 created the file with the
        # pristine pre-patch bytes.
        assert r1.backup_path is not None
        assert r1.backup_already_existed is False
        # r2 did not overwrite -- it sees the same path but flags
        # that the backup was already in place.
        assert r2.backup_path == r1.backup_path
        assert r2.backup_already_existed is True
        # Both instances mark the path so subsequent same-instance
        # patches do not re-attempt the backup.
        assert "/work/sim/foo.sp" in p1._backed_up_remote_paths
        assert "/work/sim/foo.sp" in p2._backed_up_remote_paths

    def test_partial_backup_cleanup_on_copy_failure(self):
        """Belt-and-braces: if the backup body write fails mid-stream,
        the partial file must be unlinked so a future caller's O_EXCL
        succeeds and produces a complete snapshot. Never leave a
        truncated backup that future callers would treat as good.
        """
        script = RemotePatcher._render_remote_script(
            remote_path="/work/sim/foo.sp",
            new_params={"delay": "75p"},
            whitelist=["delay"],
            do_backup=True,
            backup_ts="20260426_103000",
        )
        # The script must unlink ``backup_path`` inside the backup
        # try/except so a partial write does not poison the next
        # caller's O_EXCL attempt.
        assert "os.unlink(backup_path)" in script


class TestR2RemoteFailNotFound:
    """Remote returns ``FAIL: not_found`` with rc=2 when the remote
    target file is missing (operator pointed at the wrong path, or
    the staging hasn't completed). Local side must surface this as
    a RemotePatchError with the sanitized FAIL line.
    """

    def test_remote_fail_not_found_raises_remotepatcherror(self):
        def fake_run(cmd, **kw):
            return _ok_completed(
                stdout="", stderr="FAIL: not_found\n", returncode=2,
            )

        patcher = RemotePatcher(ssh_args=["ssh", "host"])
        with patch("src.remote_patch.subprocess.run", side_effect=fake_run):
            with pytest.raises(RemotePatchError) as exc:
                patcher.patch(
                    "/work/sim/missing.sp", {"delay": "75p"}, ["delay"],
                )
        msg = str(exc.value)
        assert "rc=2" in msg
        assert "FAIL: not_found" in msg
        # Path must NOT leak into the exception (the operator already
        # knows what they passed; argv-style echo is information they
        # have, not new privacy concern, but the RemotePatcher contract
        # is "categories + line numbers + key names" only -- the path
        # is intentionally not echoed).
        assert "/work/sim/missing.sp" not in msg

    def test_remote_script_fails_fast_when_target_missing(self):
        """The remote script must check os.path.isfile(REMOTE_PATH)
        BEFORE doing the backup -- otherwise an O_EXCL on a path
        whose target is missing would still create a zero-byte
        backup file."""
        script = RemotePatcher._render_remote_script(
            remote_path="/work/sim/foo.sp",
            new_params={"delay": "75p"},
            whitelist=["delay"],
            do_backup=True,
            backup_ts="20260426_103000",
        )
        not_found_idx = script.index('FAIL: not_found')
        backup_idx = script.index('backup_path = REMOTE_PATH + ".orig_"')
        assert not_found_idx < backup_idx, \
            "not_found check must precede the backup attempt"


class TestR2GenericExceptionSanitization:
    """Constraint R2-3: the remote ``except Exception`` fallback must
    surface ONLY ``type(e).__name__`` -- never ``str(e)``,
    ``repr(e)``, or ``e.args``. An OSError raised on the remote with
    ``nch_lvt_mac`` in its args must produce a local-side exception
    string carrying ``OSError`` and nothing else.
    """

    def test_generic_except_block_uses_only_type_name(self):
        script = RemotePatcher._render_remote_script(
            remote_path="/work/sim/foo.sp",
            new_params={"delay": "75p"},
            whitelist=["delay"],
            do_backup=True,
            backup_ts="20260426_103000",
        )
        # Locate the catch-all "except Exception" branch and slice
        # to end-of-template.
        exc_idx = script.index("except Exception as e:")
        exc_block = script[exc_idx:]
        # The required form: write only the type name + newline.
        assert 'type(e).__name__' in exc_block
        # The forbidden forms must NOT appear in the catch-all branch.
        # (They are allowed -- and present -- in the
        # ``except ParamRewriteError`` branch above, which surfaces
        # ``str(e)`` because ParamRewriteError messages are
        # contractually safe: key names + categories only.)
        assert "str(e)" not in exc_block, \
            "generic exception branch must not use str(e)"
        assert "repr(e)" not in exc_block, \
            "generic exception branch must not use repr(e)"
        assert "e.args" not in exc_block, \
            "generic exception branch must not surface e.args"

    def test_generic_exception_reraise_carries_only_type_name(self):
        """Behavioural test: simulate the controlled remote stderr
        emitted by the script when an OSError fires. The remote
        protocol design (only type name) plus the local sanitizer
        give defense-in-depth -- even if a future operator-modified
        script DID leak ``str(e)``, the sanitizer catches it.
        """
        # Case A: the remote script behaves correctly -- only the
        # type name reaches stderr.
        def fake_run_clean(cmd, **kw):
            return _ok_completed(
                stdout="", stderr="FAIL: unexpected OSError\n", returncode=4,
            )

        patcher = RemotePatcher(ssh_args=["ssh", "host"])
        with patch("src.remote_patch.subprocess.run", side_effect=fake_run_clean):
            with pytest.raises(RemotePatchError) as exc:
                patcher.patch("/work/sim/foo.sp", {"delay": "75p"}, ["delay"])
        msg_a = str(exc.value)
        assert "OSError" in msg_a
        assert "nch_lvt_mac" not in msg_a

        # Case B: simulate a script that incorrectly leaked str(e)
        # carrying a foundry token. The local sanitizer (single
        # chokepoint) must still mask the token before re-raise.
        def fake_run_leaky(cmd, **kw):
            return _ok_completed(
                stdout="",
                stderr="FAIL: unexpected OSError [Errno 2] nch_lvt_mac model not loaded\n",
                returncode=4,
            )

        patcher2 = RemotePatcher(ssh_args=["ssh", "host"])
        with patch("src.remote_patch.subprocess.run", side_effect=fake_run_leaky):
            with pytest.raises(RemotePatchError) as exc:
                patcher2.patch("/work/sim/foo.sp", {"delay": "75p"}, ["delay"])
        msg_b = str(exc.value)
        assert "nch_lvt_mac" not in msg_b
        assert "<REDACTED>" in msg_b
        assert "OSError" in msg_b


# --------------------------------------------------------------------- #
#  Constraint 4: tmp + rename atomic pattern.
# --------------------------------------------------------------------- #

class TestAtomicWritePattern:

    def test_remote_script_uses_tmp_pid_then_rename(self):
        script = RemotePatcher._render_remote_script(
            remote_path="/work/sim/foo.sp",
            new_params={"delay": "75p"},
            whitelist=["delay"],
            do_backup=False,
            backup_ts="20260426_103000",
        )
        # The .tmp.<pid> intermediate file naming.
        assert "tmp_path = REMOTE_PATH + \".tmp.\" + str(os.getpid())" in script
        # The atomic rename onto the destination.
        assert "os.rename(tmp_path, REMOTE_PATH)" in script
        # The .tmp must be written BEFORE the rename, never the
        # other way around.
        write_pos = script.index("f.write(new_text)")
        rename_pos = script.index("os.rename(tmp_path, REMOTE_PATH)")
        assert write_pos < rename_pos, \
            "rename must come AFTER the tmp file is written"
        # Cleanup on failure -- a partial write should not pile up
        # stale .tmp.<pid> siblings.
        assert "os.unlink(tmp_path)" in script

    def test_no_cat_pipe_pattern_in_script(self):
        """Belt-and-braces: no ``cat ... > path`` shell redirection
        anywhere in the transmitted script (the bug we are fixing
        was exactly that pattern)."""
        script = RemotePatcher._render_remote_script(
            remote_path="/work/sim/foo.sp",
            new_params={"delay": "75p"},
            whitelist=["delay"],
            do_backup=False,
            backup_ts="20260426_103000",
        )
        assert "cat >" not in script
        assert "cat > " not in script


# --------------------------------------------------------------------- #
#  Parity: embedded rewriter == local rewriter.
# --------------------------------------------------------------------- #

class TestRewriterParity:
    """The remote script base64-embeds src/sp_rewrite.py and execs it.
    A future edit to sp_rewrite.py automatically propagates -- but
    only if the embedding scheme keeps working. This test guards
    against an embedding regression by extracting and exec'ing the
    embedded source, then comparing to the local rewrite on the same
    fixture.
    """

    def test_embedded_source_executes_and_matches_local(self):
        # Render a script for a known fixture; pull the embedded source
        # out of it; exec; compare.
        script = RemotePatcher._render_remote_script(
            remote_path="/work/sim/foo.sp",
            new_params={},  # empty so we don't trip phantom-key check
            whitelist=DELAY_WHITELIST,
            do_backup=False,
            backup_ts="20260426_103000",
        )
        # The embedded source is base64-decoded at remote runtime;
        # do the same here to get the exact source the remote uses.
        m = re.search(
            r'_SP_SRC = base64\.b64decode\(b"([^"]+)"\)\.decode\("utf-8"\)',
            script,
        )
        assert m, "embedded sp_rewrite payload missing from script"
        import base64
        embedded_src = base64.b64decode(m.group(1)).decode("utf-8")
        # Execute it in an isolated namespace.
        ns: dict = {"__name__": "_test_embedded_sp_rewrite"}
        exec(compile(embedded_src, "<embedded>", "exec"), ns)
        embedded_rewrite = ns["rewrite_params"]

        # Compare against the local function on a non-trivial fixture.
        new_params = {"delay": "75p", "hinvoltage": "0.8"}
        local_out = rewrite_params(DELAY_TB, new_params, DELAY_WHITELIST)
        embedded_out = embedded_rewrite(DELAY_TB, new_params, DELAY_WHITELIST)
        assert local_out == embedded_out


# --------------------------------------------------------------------- #
#  Status-line parser tests (pure helper).
# --------------------------------------------------------------------- #

class TestStatusLineParser:

    def test_parse_ok_with_backup(self):
        out = "OK_BACKUP: /work/sim/foo.sp.orig_20260426_103000\nOK: 4 keys patched\n"
        res = _parse_status_lines(out)
        assert res == RemotePatchResult(
            keys_patched=4,
            backup_path="/work/sim/foo.sp.orig_20260426_103000",
            noop=False,
        )

    def test_parse_ok_noop(self):
        out = "OK: 0 keys patched (noop)\n"
        res = _parse_status_lines(out)
        assert res.keys_patched == 0
        assert res.noop is True
        assert res.backup_path is None

    def test_parse_missing_ok_line_raises(self):
        with pytest.raises(RemotePatchError):
            _parse_status_lines("OK_BACKUP: /foo\n")  # no OK: line


# --------------------------------------------------------------------- #
#  Defensive: empty new_params is rejected locally without ssh.
# --------------------------------------------------------------------- #

def test_empty_new_params_does_not_round_trip():
    """An empty proposal must not waste an ssh round trip."""
    patcher = RemotePatcher(ssh_args=["ssh", "host"])
    called = {"n": 0}

    def fake_run(cmd, **kw):
        called["n"] += 1
        return _ok_completed("OK: 0 keys patched (noop)\n")

    with patch("src.remote_patch.subprocess.run", side_effect=fake_run):
        with pytest.raises(ValueError):
            patcher.patch("/work/sim/foo.sp", {}, ["delay"])
    assert called["n"] == 0


# --------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------- #

def _config_from_script(script: str) -> dict:
    """Pull the JSON config out of a rendered remote script."""
    m = re.search(
        r'_CFG = json\.loads\("""(.+?)"""\)',
        script,
        re.DOTALL,
    )
    assert m, "config JSON literal missing from script"
    return json.loads(m.group(1))


def _do_backup_flag(script: str) -> bool:
    return _config_from_script(script)["do_backup"]
