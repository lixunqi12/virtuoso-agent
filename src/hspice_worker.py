"""HSpice remote-exec worker (Task T3).

Spawns a fresh HSpice subprocess on the remote compute host for a
given netlist, waits for it to finish (with wall-clock kill on
timeout), and fetches back the sanitised measurement / listing
artifacts:

- Every ``<base>.mt[0-9]`` is scrubbed via :func:`hspice_scrub.scrub_mt0`
  and parsed into :class:`parse_mt0.Mt0Result`.
- ``<base>.lis`` is scrubbed via :func:`hspice_scrub.scrub_lis`.
- Captured stdout / stderr are scrubbed via :func:`hspice_scrub.scrub_lis`
  (they're log-shaped text from the same HSpice run).

``<base>.tr[0-9]`` waveform files are **NEVER fetched** — they're
routinely 500 MB+ and would saturate the SSH channel. Waveform
inspection lives in T7's X11 WaveView plumbing.

Transport posture mirrors :mod:`src.ocean_worker`: plain ``subprocess`` +
the ``ssh`` / ``cat`` / ``rm`` primitives, no ``paramiko`` / ``fabric``.
``BatchMode=yes`` + ``ConnectTimeout`` + ``ServerAliveInterval`` guard
against password prompts, dead-VPN hangs, and long-run connection
drops.

Timeout discipline: before ``exec``-ing HSpice the wrapper writes
``$$`` to a pidfile. On :class:`subprocess.TimeoutExpired` the worker
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


DEFAULT_HSPICE_BIN = "/apps/synopsys/hspice/bin/hspice"
DEFAULT_REMOTE_TMP_DIR = "/tmp"
DEFAULT_WALL_TIMEOUT_S = 600.0
DEFAULT_SSH_CONNECT_TIMEOUT_S = 15


class HspiceWorkerError(Exception):
    """Base class for all worker failures."""


class HspiceWorkerTimeout(HspiceWorkerError):
    """Remote HSpice exceeded wall-clock budget and was SIGKILLed."""


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
    wall_timeout_s: float = DEFAULT_WALL_TIMEOUT_S
    ssh_connect_timeout_s: int = DEFAULT_SSH_CONNECT_TIMEOUT_S

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

        Raises:
            :class:`HspiceWorkerTimeout`: wall-clock budget exceeded.
            :class:`HspiceWorkerSpawnError`: ssh transport / file listing
                could not reach the remote host.
            :class:`HspiceWorkerScriptError`: HSpice produced invalid /
                unscrubbable output, or a fetched file failed to parse.
            :class:`ValueError`: ``sp_path_remote`` fails validation.
        """
        self._validate_sp_path(sp_path_remote)

        run_dir = str(PurePosixPath(sp_path_remote).parent)
        sp_base = PurePosixPath(sp_path_remote).stem  # "sim" or "sim.nominal"

        budget = float(
            timeout_sec if timeout_sec is not None else self.cfg.wall_timeout_s
        )
        run_id = uuid.uuid4().hex[:12]
        remote_pidfile = f"{self.cfg.remote_tmp_dir}/hspice_pid_{run_id}"

        try:
            rc, stdout, stderr = self._spawn_and_wait(
                sp_path_remote=sp_path_remote,
                run_dir=run_dir,
                sp_base=sp_base,
                remote_pidfile=remote_pidfile,
                budget_s=budget,
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
            self._cleanup_remote(remote_pidfile)

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
        budget_s: float,
        run_id: str,
    ) -> tuple[int, str, str]:
        # Prefix the .sp filename with `./` after cd-ing into the run
        # dir so hspice cannot parse a basename starting with '-' as a
        # flag. `_validate_sp_path` already rejects that case, but
        # leading-dash defense lives at multiple layers.
        wrapper = (
            f"echo $$ > {shlex.quote(remote_pidfile)}; "
            f"cd {shlex.quote(run_dir)}; "
            f"exec {shlex.quote(self.cfg.hspice_bin)} "
            f"{shlex.quote('./' + sp_base + '.sp')}"
        )
        cmd = self.cfg.ssh_base_args() + [f"bash -lc {shlex.quote(wrapper)}"]

        logger.info(
            "HspiceWorker[%s]: spawning hspice sp=%s budget=%.0fs",
            run_id, sp_path_remote, budget_s,
        )
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True, text=True,
                timeout=budget_s,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - t0
            logger.warning(
                "HspiceWorker[%s]: wall-clock timeout after %.1fs; "
                "sending kill -9 to remote pid",
                run_id, elapsed,
            )
            self._kill_remote(remote_pidfile, run_id)
            raise HspiceWorkerTimeout(
                f"hspice subprocess exceeded {budget_s:.0f}s; killed"
            ) from None

        elapsed = time.monotonic() - t0
        logger.info(
            "HspiceWorker[%s]: hspice finished rc=%d in %.1fs",
            run_id, proc.returncode, elapsed,
        )
        # hspice itself may exit nonzero on simulation failure — we still
        # want to return what output we have, and let the caller decide.
        # An ssh-transport failure (rc 255, "Connection refused", etc.)
        # is distinguished by returncode 255 / missing output.
        if proc.returncode == 255:
            # R2 (codex): ssh transport may have dropped *after* the
            # wrapper wrote the pidfile and started hspice — leaving a
            # zombie simulation burning compute / license. Best-effort
            # kill before raising so the remote side cannot orphan us.
            self._kill_remote(remote_pidfile, run_id)
            raise HspiceWorkerSpawnError(
                f"ssh transport error rc=255: "
                f"{(proc.stderr or '')[-200:]}"
            )
        return proc.returncode, proc.stdout or "", proc.stderr or ""

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
        cmd = self.cfg.ssh_base_args() + [f"bash -lc {shlex.quote(ls_cmd)}"]
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
        cmd = self.cfg.ssh_base_args() + [f"bash -lc {shlex.quote(kill_cmd)}"]
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
            f"bash -lc {shlex.quote('rm -f ' + rm_list)}"
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
      - ``VB_HSPICE_TIMEOUT_S``: optional wall-clock override
      - ``VB_REMOTE_TMP_DIR``: optional override for pidfile directory
    """
    host = os.environ.get("VB_REMOTE_HOST")
    user = os.environ.get("VB_REMOTE_USER")
    if not host or not user:
        raise HspiceWorkerError(
            "VB_REMOTE_HOST / VB_REMOTE_USER must be set "
            "(load config/.env first)"
        )
    cfg = HspiceWorkerConfig(
        remote_host=host,
        remote_user=user,
        hspice_bin=os.environ.get("VB_HSPICE_BIN", DEFAULT_HSPICE_BIN),
        remote_tmp_dir=os.environ.get("VB_REMOTE_TMP_DIR", DEFAULT_REMOTE_TMP_DIR),
        wall_timeout_s=float(
            os.environ.get("VB_HSPICE_TIMEOUT_S", DEFAULT_WALL_TIMEOUT_S)
        ),
    )
    return HspiceWorker(cfg)
