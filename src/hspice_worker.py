"""HSpice remote-exec worker (Task T3 + T8.4 liveness rework).

Spawns a fresh HSpice subprocess on the remote compute host for a
given netlist, waits for it to finish (with liveness-based + hard-
ceiling timeouts), and fetches back the sanitised measurement /
listing artifacts:

- Every ``<base>.mt[0-9]`` is scrubbed via :func:`hspice_scrub.scrub_mt0`
  and parsed into :class:`parse_mt0.Mt0Result`.
- ``<base>.lis`` is scrubbed via :func:`hspice_scrub.scrub_lis`.
- HSpice stdout / stderr are scrubbed via :func:`hspice_scrub.scrub_lis`
  (they're log-shaped text from the same HSpice run).

``<base>.tr[0-9]`` waveform files are **NEVER fetched** — they're
routinely 500 MB+ and would saturate the SSH channel. Waveform
inspection lives in T7's X11 WaveView plumbing.

Transport posture mirrors :mod:`src.ocean_worker`: plain ``subprocess`` +
the ``ssh`` / ``cat`` / ``rm`` primitives, no ``paramiko`` / ``fabric``.
``BatchMode=yes`` + ``ConnectTimeout`` + ``ServerAliveInterval`` guard
against password prompts, dead-VPN hangs, and long-run connection
drops.

T8.4 liveness rework — why we don't use ``subprocess.run(timeout=...)``:

1. HSpice runs are *long* (8–14 hours of useful sim time, 4-hour worst
   case), so a single fixed wall budget either kills healthy sims or
   leaves hung ones running.
2. We need a *liveness* signal that distinguishes a productive sim from
   a stuck one. The native signal is ``<base>.lis`` size growth — if
   the listing file is still growing, hspice is still working;
   otherwise it's wedged or finished.
3. ``subprocess.run`` blocks the entire local process. Switching to
   ``subprocess.Popen`` lets us probe ``.lis`` size periodically
   (every ``liveness_poll_s`` seconds, default 30s), kill on idle
   (``idle_timeout_s``, default 600s), and still enforce a hard ceiling
   (``hard_ceiling_s``, default 14400s = 4h) as defense in depth.

Pipe-buffer deadlock note: ``Popen(stdout=PIPE, stderr=PIPE)`` deadlocks
when the remote process writes more than the OS pipe buffer (~64 KB)
worth of output and we don't drain. HSpice routinely writes MBs of
banners and progress lines. Rather than running drain threads (extra
moving parts, harder to reason about kill semantics), the spawn wrapper
redirects hspice's own stdout/stderr to remote temp files, and we fetch
them via ``cat`` after the process exits. The local Popen only carries
ssh's own (small) stderr, which is safe to PIPE.

Timeout discipline: before ``exec``-ing HSpice the wrapper writes
``$$`` to a pidfile. On either idle or hard-ceiling timeout the worker
ssh's back and sends ``pkill -9 -P <pid>`` + ``kill -9 <pid>`` so the
remote HSpice process cannot orphan past the local budget.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Mapping

from src.hspice_scrub import ScrubError, scrub_lis, scrub_mt0
from src.parse_mt0 import Mt0ParseError, Mt0Result, parse_mt0

logger = logging.getLogger(__name__)


__all__ = [
    "HspiceWorker",
    "HspiceWorkerConfig",
    "HspiceRunResult",
    "HspiceWorkerError",
    "HspiceWorkerTimeout",
    "HspiceWorkerSpawnError",
    "HspiceWorkerScriptError",
    "worker_from_env",
]


# Accept only absolute POSIX paths with a conservative character set —
# `shlex.quote` is still applied at every command construction site
# (defense in depth), but we refuse the path up-front to fail fast on
# obvious junk. No spaces, no `..`, no shell metacharacters.
#
# R2 (codex): basename must not start with '-'. A path like
# `/tmp/-evil.sp` would otherwise survive the regex and get handed to
# hspice / ls as a flag once the cd-to-dir pattern strips the leading
# path off. The `_SP_BASENAME_RE` guards the basename separately; the
# spawn command also gets a `./` prefix and `ls` gets a `--` terminator
# as defense in depth.
_SP_PATH_RE = re.compile(r"^/[A-Za-z0-9_./\-]+\.sp$")
_SP_PATH_NO_DOTDOT_RE = re.compile(r"(^|/)\.\.(/|$)")
_SP_BASENAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*\.sp$")

# Only these output file families are allowed to come back over SSH.
# .tr* waveform files are explicitly excluded — see module docstring.
_MT_BASENAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+\.mt[0-9]$")
_LIS_BASENAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+\.lis$")
_TR_BASENAME_RE = re.compile(r"\.tr[0-9]*$", re.IGNORECASE)


DEFAULT_HSPICE_BIN = "/apps/hspice/2023.03/hspice/linux64/hspice"
DEFAULT_REMOTE_TMP_DIR = "/tmp"
# Hard ceiling default: 4 hours. HSpice runs do go this long; anything
# beyond is almost certainly stuck.
DEFAULT_HARD_CEILING_S = 14400.0
# Idle timeout default: 10 minutes of no .lis growth = wedged sim.
DEFAULT_IDLE_TIMEOUT_S = 600.0
# Liveness probe cadence: every 30s the worker ssh's a `stat -c %s` of
# the .lis file. Cheap; cadence chosen so that a long .lis flush gap
# (a few seconds, common in transient .meas blocks) doesn't trip idle.
DEFAULT_LIVENESS_POLL_S = 30.0
# Heartbeat log cadence: emit one INFO line every 60s while the sim
# runs so operators see liveness without having to ssh manually.
DEFAULT_HEARTBEAT_S = 60.0
DEFAULT_SSH_CONNECT_TIMEOUT_S = 15

# Remote shell invocation. We deliberately do NOT use `bash -lc` (login
# shell) on COBI because the site's `.bash_profile` calls
# `module load hspice/2018.09` and that modulefile no longer exists in
# the bash MODULEPATH — every spawn would emit pages of error banners
# to stderr and (more importantly) leave PATH in an unpredictable state.
# Instead we pin the hspice binary path explicitly via VB_HSPICE_BIN and
# inject the license env vars from the worker config, so the simulator
# runs in a known-clean environment without depending on dotfiles.
_BASH_WRAPPER_PREFIX = "bash --noprofile --norc -c "

# Names accepted in the `extra_env` mapping for `_spawn_and_wait`. We
# keep the whitelist explicit so a future config drift cannot smuggle
# arbitrary env vars into the remote shell.
_HSPICE_ENV_WHITELIST = frozenset({
    "SNPSLMD_LICENSE_FILE",
    "LM_LICENSE_FILE",
})
_HSPICE_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_./@:,\-]+$")


class HspiceWorkerError(Exception):
    """Base class for all worker failures."""


class HspiceWorkerTimeout(HspiceWorkerError):
    """Remote HSpice exceeded idle or hard-ceiling budget and was SIGKILLed."""


class HspiceWorkerSpawnError(HspiceWorkerError):
    """ssh / cat could not establish a connection or transport failed."""


class HspiceWorkerScriptError(HspiceWorkerError):
    """HSpice returned nonzero / refused to start / output invalid."""


@dataclass(frozen=True)
class HspiceWorkerConfig:
    remote_host: str
    remote_user: str
    hspice_bin: str = DEFAULT_HSPICE_BIN
    remote_tmp_dir: str = DEFAULT_REMOTE_TMP_DIR
    # T8.4: replaced single wall_timeout_s with two-tier liveness model.
    # `hard_ceiling_s` is the absolute wall budget (was wall_timeout_s).
    hard_ceiling_s: float = DEFAULT_HARD_CEILING_S
    idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S
    liveness_poll_s: float = DEFAULT_LIVENESS_POLL_S
    heartbeat_s: float = DEFAULT_HEARTBEAT_S
    ssh_connect_timeout_s: int = DEFAULT_SSH_CONNECT_TIMEOUT_S
    snpslmd_license_file: str | None = None
    lm_license_file: str | None = None

    def ssh_target(self) -> str:
        return f"{self.remote_user}@{self.remote_host}"

    def ssh_base_args(self) -> list[str]:
        return [
            "ssh",
            "-o", f"ConnectTimeout={self.ssh_connect_timeout_s}",
            "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=3",
            self.ssh_target(),
        ]

    def license_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.snpslmd_license_file:
            env["SNPSLMD_LICENSE_FILE"] = self.snpslmd_license_file
        if self.lm_license_file:
            env["LM_LICENSE_FILE"] = self.lm_license_file
        return env


@dataclass(frozen=True)
class HspiceRunResult:
    returncode: int
    stdout_scrubbed: str
    stderr_scrubbed: str
    mt_files: Mapping[str, Mt0Result]
    lis_scrubbed: str | None
    run_dir_remote: str
    sp_base: str


class HspiceWorker:
    """Spawn a fresh HSpice subprocess per simulation call."""

    def __init__(self, config: HspiceWorkerConfig) -> None:
        self.cfg = config

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def run(
        self,
        sp_path_remote: str,
        timeout_sec: float | None = None,
    ) -> HspiceRunResult:
        """Execute HSpice on ``sp_path_remote`` and return a sanitised result.

        ``timeout_sec``, if given, overrides the configured *hard ceiling*
        for this call. The idle-timeout (no ``.lis`` growth) is governed
        by ``cfg.idle_timeout_s`` and is not affected.

        Raises:
            :class:`HspiceWorkerTimeout`: idle-timeout or hard-ceiling
                exceeded.
            :class:`HspiceWorkerSpawnError`: ssh transport / file listing
                could not reach the remote host.
            :class:`HspiceWorkerScriptError`: HSpice produced invalid /
                unscrubbable output, or a fetched file failed to parse.
            :class:`ValueError`: ``sp_path_remote`` fails validation.
        """
        self._validate_sp_path(sp_path_remote)

        run_dir = str(PurePosixPath(sp_path_remote).parent)
        sp_base = PurePosixPath(sp_path_remote).stem  # "sim" or "sim.nominal"

        hard_ceiling = float(
            timeout_sec if timeout_sec is not None else self.cfg.hard_ceiling_s
        )
        run_id = uuid.uuid4().hex[:12]
        remote_pidfile = f"{self.cfg.remote_tmp_dir}/hspice_pid_{run_id}"
        remote_stdout_file = f"{self.cfg.remote_tmp_dir}/hspice_stdout_{run_id}"
        remote_stderr_file = f"{self.cfg.remote_tmp_dir}/hspice_stderr_{run_id}"
        # Always cleaned up — stderr/stdout files contain hspice log
        # output (potentially MB) and should not accumulate in /tmp.
        cleanup_paths = (remote_pidfile, remote_stdout_file, remote_stderr_file)

        try:
            rc, stdout, stderr = self._spawn_and_wait(
                sp_path_remote=sp_path_remote,
                run_dir=run_dir,
                sp_base=sp_base,
                remote_pidfile=remote_pidfile,
                remote_stdout_file=remote_stdout_file,
                remote_stderr_file=remote_stderr_file,
                hard_ceiling_s=hard_ceiling,
                run_id=run_id,
            )
            out_files = self._list_outputs(run_dir, sp_base)
            mt_files: dict[str, Mt0Result] = {}
            lis_scrubbed: str | None = None
            for basename in out_files:
                remote_path = f"{run_dir}/{basename}"
                if _MT_BASENAME_RE.match(basename):
                    raw = self._fetch_file(remote_path)
                    scrubbed = self._scrub(scrub_mt0, raw, stage="mt0")
                    try:
                        mt_files[basename] = parse_mt0(scrubbed)
                    except Mt0ParseError as exc:
                        raise HspiceWorkerScriptError(
                            f"{basename} failed to parse: {exc}"
                        ) from None
                elif _LIS_BASENAME_RE.match(basename):
                    raw = self._fetch_file(remote_path)
                    lis_scrubbed = self._scrub(scrub_lis, raw, stage="lis")

            stdout_scrubbed = self._scrub(scrub_lis, stdout, stage="stdout")
            stderr_scrubbed = self._scrub(scrub_lis, stderr, stage="stderr")

            return HspiceRunResult(
                returncode=rc,
                stdout_scrubbed=stdout_scrubbed,
                stderr_scrubbed=stderr_scrubbed,
                mt_files=dict(mt_files),
                lis_scrubbed=lis_scrubbed,
                run_dir_remote=run_dir,
                sp_base=sp_base,
            )
        finally:
            self._cleanup_remote(*cleanup_paths)

    # ------------------------------------------------------------------ #
    #  Implementation
    # ------------------------------------------------------------------ #

    @staticmethod
    def _validate_sp_path(sp_path_remote: str) -> None:
        if not isinstance(sp_path_remote, str):
            raise ValueError(f"sp_path_remote must be str; got {type(sp_path_remote)!r}")
        if _SP_PATH_NO_DOTDOT_RE.search(sp_path_remote):
            raise ValueError(f"sp_path_remote must not contain '..': {sp_path_remote!r}")
        if not _SP_PATH_RE.match(sp_path_remote):
            raise ValueError(
                f"sp_path_remote must be an absolute POSIX path ending in .sp, "
                f"containing only [A-Za-z0-9_./-]; got {sp_path_remote!r}"
            )
        # Basename may not start with '-' — once `cd run_dir` runs, the
        # relative basename is passed to hspice / ls and would otherwise
        # be parsed as a flag.
        basename = PurePosixPath(sp_path_remote).name
        if not _SP_BASENAME_RE.match(basename):
            raise ValueError(
                f"sp_path_remote basename must start with alphanumeric/"
                f"underscore (rejecting option-like leading dash): "
                f"{basename!r}"
            )

    def _spawn_and_wait(
        self,
        *,
        sp_path_remote: str,
        run_dir: str,
        sp_base: str,
        remote_pidfile: str,
        remote_stdout_file: str,
        remote_stderr_file: str,
        hard_ceiling_s: float,
        run_id: str,
    ) -> tuple[int, str, str]:
        # Prefix the .sp filename with `./` after cd-ing into the run
        # dir so hspice cannot parse a basename starting with '-' as a
        # flag. `_validate_sp_path` already rejects that case, but
        # leading-dash defense lives at multiple layers.
        #
        # T8.4: redirect hspice's own stdout/stderr to remote temp files
        # so the local Popen does not need to drain hspice's (large)
        # output through SSH pipes — that would deadlock once the OS
        # pipe buffer fills. We `cat` the files back after exit.
        env_exports = self._license_export_prefix()
        wrapper = (
            f"{env_exports}"
            f"echo $$ > {shlex.quote(remote_pidfile)}; "
            f"cd {shlex.quote(run_dir)}; "
            f"exec {shlex.quote(self.cfg.hspice_bin)} "
            f"{shlex.quote('./' + sp_base + '.sp')} "
            f"> {shlex.quote(remote_stdout_file)} "
            f"2> {shlex.quote(remote_stderr_file)}"
        )
        cmd = self.cfg.ssh_base_args() + [
            _BASH_WRAPPER_PREFIX + shlex.quote(wrapper)
        ]

        idle_s = self.cfg.idle_timeout_s
        poll_s = self.cfg.liveness_poll_s
        heartbeat_s = self.cfg.heartbeat_s

        logger.info(
            "HspiceWorker[%s]: spawning hspice sp=%s "
            "idle_timeout=%.0fs hard_ceiling=%.0fs poll=%.0fs",
            run_id, sp_path_remote, idle_s, hard_ceiling_s, poll_s,
        )
        # stdout=DEVNULL: hspice's stdout is redirected to a remote file
        # by the wrapper; ssh has nothing useful to forward. stderr=PIPE:
        # ssh transport-level errors (auth, dropped session) come here
        # and are typically a few hundred bytes — safe to PIPE without a
        # drain thread.
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        t0 = time.monotonic()
        last_outputs_size: int = -1
        last_growth_time = t0
        last_heartbeat = t0
        rc: int | None = None

        try:
            while True:
                # T8.4 R2 (codex blocker): drive the loop off
                # `proc.wait(timeout=poll_s)` instead of `poll()` then a
                # separate `time.sleep`. The previous shape had a window
                # where hspice could exit during `time.sleep(poll_s)`
                # and the next iteration's hard-ceiling / idle / probe
                # branches would fire on an already-exited process —
                # killing healthy short runs and swallowing a sleep-window
                # rc=255 transport drop into idle/hard timeout instead of
                # the correct SpawnError branch. `proc.wait(timeout=...)`
                # reaps an in-window exit atomically: TimeoutExpired
                # means "still running, do timeout/probe checks";
                # any return value means "exited, skip checks and let
                # the post-loop rc dispatch handle it".
                try:
                    rc = proc.wait(timeout=poll_s)
                    break
                except subprocess.TimeoutExpired:
                    pass  # still running; fall through to checks

                now = time.monotonic()
                elapsed = now - t0

                # Hard ceiling — defense in depth. Even if liveness keeps
                # claiming progress, a 4h+ run is almost certainly stuck.
                if elapsed > hard_ceiling_s:
                    logger.warning(
                        "HspiceWorker[%s]: hard ceiling %.0fs exceeded "
                        "(elapsed=%.0fs); killing remote",
                        run_id, hard_ceiling_s, elapsed,
                    )
                    self._kill_remote(remote_pidfile, run_id)
                    self._kill_local_proc(proc)
                    raise HspiceWorkerTimeout(
                        f"hspice exceeded hard ceiling "
                        f"{hard_ceiling_s:.0f}s; killed"
                    )

                # Liveness probe (T8.5): sum bytes across all hspice
                # output families (.lis/.mt*/.st*/.ic*/.pa*/.su*), not
                # just .lis — some testbench .OPTION configs skip the
                # listing file entirely. A transport blip is inconclusive
                # (None); we don't kill on a single failed probe, since
                # ssh ServerAlive will surface a truly dead session.
                cur_size = self._probe_outputs_size_safe(
                    run_dir, sp_base, run_id,
                )
                if cur_size is not None and cur_size != last_outputs_size:
                    last_outputs_size = cur_size
                    last_growth_time = now

                # Idle timeout — no output growth for idle_s seconds.
                idle_for = now - last_growth_time
                if idle_for > idle_s:
                    logger.warning(
                        "HspiceWorker[%s]: no output growth for %.0fs "
                        "(idle limit %.0fs); killing remote",
                        run_id, idle_for, idle_s,
                    )
                    self._kill_remote(remote_pidfile, run_id)
                    self._kill_local_proc(proc)
                    raise HspiceWorkerTimeout(
                        f"hspice idle (no output growth) for "
                        f"{idle_s:.0f}s; killed"
                    )

                # Heartbeat log — operators tail the agent log for
                # liveness without having to ssh in.
                if (now - last_heartbeat) >= heartbeat_s:
                    last_heartbeat = now
                    size_repr = (
                        "n/a" if last_outputs_size < 0
                        else f"{last_outputs_size}B"
                    )
                    logger.info(
                        "HspiceWorker[%s]: heartbeat elapsed=%.0fs "
                        "outputs_size=%s idle_for=%.0fs",
                        run_id, elapsed, size_repr, idle_for,
                    )

            # Process exited; collect ssh stderr (small) and fetch
            # the remote stdout/stderr files (potentially large).
            elapsed = time.monotonic() - t0
            ssh_stderr = ""
            if proc.stderr is not None:
                try:
                    ssh_stderr = proc.stderr.read() or ""
                except Exception:  # noqa: BLE001
                    ssh_stderr = ""
            logger.info(
                "HspiceWorker[%s]: hspice finished rc=%d in %.1fs",
                run_id, rc, elapsed,
            )

            # rc=255 → ssh transport error. Best-effort kill the remote
            # pid (the wrapper may have raced past `exec` already) before
            # raising. ssh_stderr is small and safe to surface; it does
            # not contain hspice log content.
            if rc == 255:
                self._kill_remote(remote_pidfile, run_id)
                raise HspiceWorkerSpawnError(
                    f"ssh transport error rc=255: "
                    f"{ssh_stderr[-200:]}"
                )

            # Fetch hspice's redirected stdout/stderr from the remote
            # temp files. If hspice crashed before writing anything, the
            # files may be empty or absent — tolerate that without
            # erroring (the caller still gets returncode + .lis).
            stdout_text = self._fetch_remote_log(remote_stdout_file)
            stderr_text = self._fetch_remote_log(remote_stderr_file)
            return rc, stdout_text, stderr_text
        finally:
            # Close the local Popen handles so the fd is released even
            # on the kill paths above.
            if proc.stderr is not None:
                try:
                    proc.stderr.close()
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _kill_local_proc(proc: subprocess.Popen) -> None:
        """Kill the local ssh Popen and wait briefly for it to reap."""
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    def _probe_outputs_size_safe(
        self, run_dir: str, sp_base: str, run_id: str,
    ) -> int | None:
        """Return total bytes across hspice output families, or None on uncertainty.

        T8.5: liveness signal widened from ``.lis``-only to all standard
        hspice output families. Some testbench ``.OPTION`` configurations
        cause hspice to skip writing the listing (e.g. ``.OPTION NOPAGE
        NOLIST POST``) and only emit ``.mt``/``.st``/``.ic``/``.pa``/
        ``.su`` artifacts. Probing ``.lis`` alone misreads a healthy run
        as wedged and the idle-timeout fires after ``idle_timeout_s``
        even though hspice was producing measure data the whole time.

        The probe globs ``.lis``, ``.mt[0-9]*``, ``.st[0-9]*``,
        ``.ic[0-9]*``, ``.pa[0-9]*``, ``.su[0-9]*`` and sums their sizes
        with ``stat -c %s | awk``. ``.tr*`` (waveforms) is intentionally
        excluded — we never read those for liveness (size + privacy).

        Return semantics (preserved from T8.4 R2):
        * int — total bytes across all output families that exist; the
          caller compares to ``last_outputs_size`` to detect growth.
        * None — inconclusive: transport timeout/exception, ssh non-zero
          rc, or no output files exist yet. The caller does NOT reset
          the idle counter on None, so a sustained transport outage or
          a genuinely wedged sim still trips ``idle_timeout_s``.
        """
        # sp_base is _SP_BASENAME_RE-validated (no shell metas); shlex.quote
        # is defense in depth. The `[0-9]*` glob is intentionally NOT
        # quoted so bash expands it — quoting would turn it literal.
        # Stderr from stat (`No such file` when a glob keeps its literal
        # pattern with no match) is silenced; awk's ``NR>0`` guard makes
        # "no files yet" emit empty stdout, which caller maps to None —
        # preserving the early-run inconclusive semantic.
        sb = shlex.quote(sp_base)
        rd = shlex.quote(run_dir)
        probe_cmd = (
            f"cd {rd} 2>/dev/null && "
            f"stat -c %s "
            f"{sb}.lis {sb}.mt[0-9]* {sb}.st[0-9]* "
            f"{sb}.ic[0-9]* {sb}.pa[0-9]* {sb}.su[0-9]* "
            f"2>/dev/null | awk '{{s+=$1}} END {{if (NR>0) print s}}'"
        )
        cmd = self.cfg.ssh_base_args() + [
            _BASH_WRAPPER_PREFIX + shlex.quote(probe_cmd)
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self.cfg.ssh_connect_timeout_s + 5,
            )
        except subprocess.TimeoutExpired:
            logger.debug(
                "HspiceWorker[%s]: outputs probe ssh timed out", run_id,
            )
            return None
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "HspiceWorker[%s]: outputs probe raised %s",
                run_id, type(exc).__name__,
            )
            return None
        if proc.returncode != 0:
            logger.debug(
                "HspiceWorker[%s]: outputs probe rc=%d (transport-ish)",
                run_id, proc.returncode,
            )
            return None
        out = (proc.stdout or "").strip()
        if not out:
            return None  # No output files yet — still early in run.
        try:
            return int(out)
        except ValueError:
            return None

    def _fetch_remote_log(self, remote_path: str) -> str:
        """Fetch a remote log temp file. Tolerates missing/empty files."""
        cmd = self.cfg.ssh_base_args() + [
            f"cat {shlex.quote(remote_path)} 2>/dev/null; true"
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self.cfg.ssh_connect_timeout_s + 60,
            )
        except subprocess.TimeoutExpired:
            return ""
        except Exception:  # noqa: BLE001
            return ""
        return proc.stdout or ""

    def _license_export_prefix(self) -> str:
        env = self.cfg.license_env()
        if not env:
            return ""
        parts: list[str] = []
        for name, value in env.items():
            if name not in _HSPICE_ENV_WHITELIST:
                raise HspiceWorkerScriptError(
                    f"refusing to export non-whitelisted env var: {name!r}"
                )
            if not _HSPICE_ENV_VALUE_RE.match(value):
                raise HspiceWorkerScriptError(
                    f"license env value for {name} contains disallowed chars"
                )
            parts.append(f"export {name}={shlex.quote(value)}; ")
        return "".join(parts)

    def _list_outputs(self, run_dir: str, sp_base: str) -> list[str]:
        """Return basenames of .mt[0-9] and .lis files for this run.

        Explicitly excludes anything matching .tr* — defense-in-depth
        against a future change that might make the shell glob
        accidentally match waveform files.
        """
        # sp_base already validated via _SP_PATH_RE; the glob pattern
        # below uses it unquoted so the shell can expand the bracket
        # class. shlex.quote-ing the pattern would turn `mt[0-9]` into
        # a literal filename.
        # `-- ` after `ls -1` terminates option parsing, so even if a
        # future path somehow has a leading dash it cannot be read as a
        # flag. Regex guard + ./-prefix already block this upstream;
        # the `--` is defense in depth.
        ls_cmd = (
            f"cd {shlex.quote(run_dir)} && "
            f"ls -1 -- {shlex.quote(sp_base)}.mt[0-9] "
            f"{shlex.quote(sp_base)}.lis 2>/dev/null; "
            f"true"
        )
        cmd = self.cfg.ssh_base_args() + [
            _BASH_WRAPPER_PREFIX + shlex.quote(ls_cmd)
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self.cfg.ssh_connect_timeout_s + 10,
            )
        except subprocess.TimeoutExpired as exc:
            raise HspiceWorkerSpawnError(
                f"list outputs timed out: {exc}"
            ) from None
        # With `|| true` the command should always succeed; a nonzero
        # here indicates an ssh-level problem.
        if proc.returncode != 0:
            raise HspiceWorkerSpawnError(
                f"list outputs ssh failed rc={proc.returncode}: "
                f"{(proc.stderr or '')[-200:]}"
            )
        basenames: list[str] = []
        for line in (proc.stdout or "").splitlines():
            base = line.strip()
            if not base:
                continue
            # Guard against path injection via ls output: only accept
            # bare basenames matching the expected families.
            if _TR_BASENAME_RE.search(base):
                continue
            if _MT_BASENAME_RE.match(base) or _LIS_BASENAME_RE.match(base):
                basenames.append(base)
        return basenames

    def _fetch_file(self, remote_path: str) -> str:
        # Defense-in-depth: never ever cat a .tr* file, regardless of
        # how it ended up in a path argument.
        if _TR_BASENAME_RE.search(remote_path):
            raise HspiceWorkerScriptError(
                f"refusing to fetch waveform file: {remote_path}"
            )
        cmd = self.cfg.ssh_base_args() + [f"cat {shlex.quote(remote_path)}"]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                # .lis can be a few MB; give it more headroom than the
                # tiny control-plane calls.
                timeout=self.cfg.ssh_connect_timeout_s + 60,
            )
        except subprocess.TimeoutExpired as exc:
            raise HspiceWorkerSpawnError(
                f"fetch {remote_path} timed out"
            ) from None
        if proc.returncode != 0:
            raise HspiceWorkerScriptError(
                f"fetch {remote_path} failed rc={proc.returncode}: "
                f"{(proc.stderr or '')[-200:]}"
            )
        return proc.stdout or ""

    @staticmethod
    def _scrub(scrubber, text: str, *, stage: str) -> str:
        if not text:
            return text
        try:
            return scrubber(text)
        except ScrubError as exc:
            # Propagate via our own error type so callers can
            # distinguish transport / script / privacy-boundary failures.
            # `str(exc)` is already privacy-preserving (T1 R2).
            raise HspiceWorkerScriptError(
                f"{stage} scrub failed: {exc}"
            ) from None

    def _kill_remote(self, remote_pidfile: str, run_id: str) -> None:
        kill_cmd = (
            f"if [ -f {shlex.quote(remote_pidfile)} ]; then "
            f"pid=$(cat {shlex.quote(remote_pidfile)}); "
            f"pkill -9 -P \"$pid\" 2>/dev/null; "
            f"kill -9 \"$pid\" 2>/dev/null; "
            f"echo killed $pid; "
            f"else echo no pidfile; fi"
        )
        cmd = self.cfg.ssh_base_args() + [
            _BASH_WRAPPER_PREFIX + shlex.quote(kill_cmd)
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self.cfg.ssh_connect_timeout_s + 10,
            )
            logger.info(
                "HspiceWorker[%s]: remote kill result: %s",
                run_id, (proc.stdout or proc.stderr).strip()[-200:],
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "HspiceWorker[%s]: remote kill ssh call itself timed out",
                run_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "HspiceWorker[%s]: remote kill raised %s",
                run_id, type(exc).__name__,
            )

    def _cleanup_remote(self, *paths: str) -> None:
        rm_list = " ".join(shlex.quote(p) for p in paths)
        cmd = self.cfg.ssh_base_args() + [
            _BASH_WRAPPER_PREFIX + shlex.quote("rm -f " + rm_list)
        ]
        try:
            subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self.cfg.ssh_connect_timeout_s + 5,
            )
        except Exception:  # noqa: BLE001
            pass


def worker_from_env() -> HspiceWorker:
    """Build an :class:`HspiceWorker` from the project's usual env vars.

    Reads:
      - ``VB_REMOTE_HOST``, ``VB_REMOTE_USER``: ssh target
      - ``VB_HSPICE_BIN``: optional override for the hspice binary
      - ``VB_HSPICE_TIMEOUT_S``: legacy alias — maps to ``hard_ceiling_s``
        for backwards compatibility with pre-T8.4 deployments.
      - ``VB_HSPICE_HARD_CEILING_S``: optional override for the hard
        wall-budget (default 4 h). Wins over ``VB_HSPICE_TIMEOUT_S``
        when both are set.
      - ``VB_HSPICE_IDLE_TIMEOUT_S``: optional override for the
        no-.lis-growth idle budget (default 600 s).
      - ``VB_HSPICE_LIVENESS_POLL_S``: optional probe cadence (default 30 s).
      - ``VB_HSPICE_HEARTBEAT_S``: optional heartbeat-log cadence (default 60 s).
      - ``VB_REMOTE_TMP_DIR``: optional override for pidfile / log temps
      - ``VB_HSPICE_SNPSLMD_LICENSE_FILE``: Synopsys license server (port@host)
      - ``VB_HSPICE_LM_LICENSE_FILE``: legacy LM_LICENSE_FILE path
    """
    host = os.environ.get("VB_REMOTE_HOST")
    user = os.environ.get("VB_REMOTE_USER")
    if not host or not user:
        raise HspiceWorkerError(
            "VB_REMOTE_HOST / VB_REMOTE_USER must be set "
            "(load config/.env first)"
        )
    # VB_HSPICE_TIMEOUT_S is the legacy single-budget knob; in T8.4 it
    # maps to hard_ceiling_s. VB_HSPICE_HARD_CEILING_S is the new
    # explicit name; if both are set, the explicit one wins.
    legacy_to = os.environ.get("VB_HSPICE_TIMEOUT_S")
    explicit_hc = os.environ.get("VB_HSPICE_HARD_CEILING_S")
    if explicit_hc is not None:
        hard_ceiling_s = float(explicit_hc)
    elif legacy_to is not None:
        hard_ceiling_s = float(legacy_to)
    else:
        hard_ceiling_s = DEFAULT_HARD_CEILING_S
    cfg = HspiceWorkerConfig(
        remote_host=host,
        remote_user=user,
        hspice_bin=os.environ.get("VB_HSPICE_BIN", DEFAULT_HSPICE_BIN),
        remote_tmp_dir=os.environ.get("VB_REMOTE_TMP_DIR", DEFAULT_REMOTE_TMP_DIR),
        hard_ceiling_s=hard_ceiling_s,
        idle_timeout_s=float(
            os.environ.get("VB_HSPICE_IDLE_TIMEOUT_S", DEFAULT_IDLE_TIMEOUT_S)
        ),
        liveness_poll_s=float(
            os.environ.get("VB_HSPICE_LIVENESS_POLL_S", DEFAULT_LIVENESS_POLL_S)
        ),
        heartbeat_s=float(
            os.environ.get("VB_HSPICE_HEARTBEAT_S", DEFAULT_HEARTBEAT_S)
        ),
        snpslmd_license_file=os.environ.get("VB_HSPICE_SNPSLMD_LICENSE_FILE") or None,
        lm_license_file=os.environ.get("VB_HSPICE_LM_LICENSE_FILE") or None,
    )
    return HspiceWorker(cfg)
