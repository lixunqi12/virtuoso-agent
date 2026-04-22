"""OCEAN subprocess worker for hang-safe PSF dumping.

Problem background
------------------
The main Virtuoso session (single long-running PID on remote host) runs SKILL
via a RAMIC TCP daemon. When a SKILL call such as
``selectResult('tran) + VT("/net")`` chokes on a pathological PSF, the
interpreter wedges at C-level and every subsequent SKILL call queues
behind it. Cadence does not expose any external-interrupt mechanism
(researched 2026-04-20: hiCancelScheduled, MPS control channel,
skillbridge's pyKillServer — none can preempt a busy SKILL thread).

Solution
--------
Do all hang-prone PSF dumping in a **fresh, throwaway Virtuoso/OCEAN
process** spawned over SSH for each iteration. If the subprocess hangs,
we kill it at the OS level (-9) without touching the main interactive
session. License seat (``Affirma_sim_analysis_env``, 800 available)
checks out on spawn and back in on exit.

Architecture
------------
1.  Generate a small SKILL expression file (``vbSignalList`` /
    ``vbWindowList``) locally.
2.  Upload it to remote host via scp.
3.  Run a remote wrapper shell that writes its own PID to a file, then
    ``exec``s ``virtuoso -ocean -nograph -restore psf_dump_worker.ocn``.
4.  If the local wall-clock timer expires, a second ssh issues
    ``kill -9 $(cat /tmp/.../pid)`` to terminate the subprocess.
5.  On success, ``cat`` the remote JSON result over ssh and parse it.

The worker script itself lives at
``skill/psf_dump_worker.ocn`` and is uploaded alongside the other safe_*.il
helpers during bridge init.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)


class OceanWorkerError(Exception):
    """Base class for worker failures (classified via DumpStatus)."""


class OceanWorkerTimeout(OceanWorkerError):
    """Subprocess exceeded wall-clock budget and was SIGKILLed."""


class OceanWorkerSpawnError(OceanWorkerError):
    """ssh/scp could not establish a connection."""


class OceanWorkerScriptError(OceanWorkerError):
    """Worker script returned status != ok (env missing, bad PSF, etc.)."""


# ---------------------------------------------------------------------------
# Defaults — match the paths we probed on remote host on 2026-04-20.
# ---------------------------------------------------------------------------

DEFAULT_VIRTUOSO_BIN = (
    "/apps/cadence/ic_23.1/tools.lnx86/dfII/bin/virtuoso"
)
DEFAULT_REMOTE_TMP_DIR = "/tmp"
DEFAULT_WALL_TIMEOUT_S = 60.0
DEFAULT_SSH_CONNECT_TIMEOUT_S = 15


@dataclass(frozen=True)
class OceanWorkerConfig:
    remote_host: str
    remote_user: str
    remote_skill_dir: str
    virtuoso_bin: str = DEFAULT_VIRTUOSO_BIN
    remote_tmp_dir: str = DEFAULT_REMOTE_TMP_DIR
    wall_timeout_s: float = DEFAULT_WALL_TIMEOUT_S
    ssh_connect_timeout_s: int = DEFAULT_SSH_CONNECT_TIMEOUT_S

    def ssh_target(self) -> str:
        return f"{self.remote_user}@{self.remote_host}"

    def ssh_base_args(self) -> list[str]:
        # -o BatchMode: fail fast if no key auth (avoid password prompt hang)
        # -o ConnectTimeout: avoid blocking forever when remote host drops off VPN
        # -o ServerAliveInterval: detect dead connection during long run
        return [
            "ssh",
            "-o", f"ConnectTimeout={self.ssh_connect_timeout_s}",
            "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=3",
            self.ssh_target(),
        ]


# ---------------------------------------------------------------------------
# SKILL spec file generation
# ---------------------------------------------------------------------------

# Match the regex used in safeOcean_validProbePath / safeOcean_validSigName
# so we reject paths at the Python side before they hit SKILL. Defense-in-
# depth: SKILL still re-validates, but catching garbage here produces a
# nicer error message.
import re
_SIG_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
_PROBE_PATH_RE = re.compile(r"^(/[A-Za-z_][A-Za-z0-9_]*){1,8}\Z")
_ALLOWED_KINDS = frozenset({"V", "I", "Vdiff", "Vsum_half"})


def _validate_signal(entry: Sequence) -> tuple[str, str, list[str]]:
    if len(entry) != 3:
        raise ValueError(
            f"signal entry must be (name, kind, [paths]); got {entry!r}"
        )
    name, kind, paths = entry
    if not isinstance(name, str) or not _SIG_NAME_RE.match(name):
        raise ValueError(f"bad signal name {name!r}")
    if kind not in _ALLOWED_KINDS:
        raise ValueError(f"bad signal kind {kind!r}; allowed {_ALLOWED_KINDS}")
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ValueError(f"signal {name}: paths must be non-empty list")
    clean_paths: list[str] = []
    for p in paths:
        if not isinstance(p, str) or not _PROBE_PATH_RE.match(p):
            raise ValueError(f"signal {name}: bad probe path {p!r}")
        clean_paths.append(p)
    return name, kind, clean_paths


def _validate_osc_signals(entries: Sequence | None) -> list[str]:
    """Validate the optional oscillation-gate signal list.

    Expected shape: a sequence of exactly 2 SKILL probe paths (e.g.
    ``["/Vout_p", "/Vout_n"]``). Returns ``[]`` if ``entries`` is None
    or empty, meaning "skip the gate".
    """
    if entries is None:
        return []
    if not isinstance(entries, (list, tuple)):
        raise ValueError(f"osc_signals must be a list; got {entries!r}")
    if len(entries) == 0:
        return []
    if len(entries) != 2:
        raise ValueError(
            f"osc_signals must contain exactly 2 probe paths; got {entries!r}"
        )
    clean: list[str] = []
    for p in entries:
        if not isinstance(p, str) or not _PROBE_PATH_RE.match(p):
            raise ValueError(f"osc_signals: bad probe path {p!r}")
        clean.append(p)
    return clean


def _validate_window(entry: Sequence) -> tuple[str, float, float]:
    if len(entry) != 3:
        raise ValueError(
            f"window entry must be (name, tStart, tEnd); got {entry!r}"
        )
    name, t_start, t_end = entry
    if not isinstance(name, str) or not _SIG_NAME_RE.match(name):
        raise ValueError(f"bad window name {name!r}")
    try:
        t_start = float(t_start)
        t_end = float(t_end)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"window {name}: non-numeric times") from exc
    if not (t_end > t_start):
        raise ValueError(f"window {name}: tEnd must be > tStart")
    return name, t_start, t_end


def _render_spec_il(signals: list[tuple[str, str, list[str]]],
                    windows: list[tuple[str, float, float]],
                    osc_signals: list[str] | None = None) -> str:
    """Generate a SKILL expression file the worker can ``load``."""
    lines: list[str] = [";;; auto-generated by ocean_worker.py — do not edit"]
    lines.append("vbSignalList = (list")
    for name, kind, paths in signals:
        path_fragment = " ".join(f'"{p}"' for p in paths)
        lines.append(
            f'    (list "{name}" "{kind}" (list {path_fragment}))'
        )
    lines.append(")")
    lines.append("vbWindowList = (list")
    for name, t_start, t_end in windows:
        # Use repr to preserve float precision (e.g. 1.8e-07).
        lines.append(f'    (list "{name}" {t_start!r} {t_end!r})')
    lines.append(")")
    # Oscillation-gate signal pair (optional). When non-empty, the OCEAN
    # worker pre-checks (ptp of path0 - path1) and skips dumpAll if the
    # differential swing is below threshold — prevents cross-based metrics
    # (frequency, dutyFromCross) from looping on near-DC signals.
    if osc_signals:
        paths_fragment = " ".join(f'"{p}"' for p in osc_signals)
        lines.append(f"vbOscSignals = (list {paths_fragment})")
    else:
        lines.append("vbOscSignals = nil")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class OceanWorker:
    """Spawn a fresh OCEAN subprocess per PSF dump call."""

    def __init__(self, config: OceanWorkerConfig):
        self.cfg = config

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def dump_all(
        self,
        psf_dir: str,
        signals: Sequence,
        windows: Sequence,
        osc_signals: Sequence[str] | None = None,
        timeout_s: float | None = None,
    ) -> dict:
        """Run safeOceanDumpAll in a throwaway OCEAN process.

        Returns the parsed dict ``{"ok": true, "dumps": {...}}`` on
        success. Raises one of :class:`OceanWorkerTimeout`,
        :class:`OceanWorkerScriptError`, or :class:`OceanWorkerSpawnError`
        on failure.

        If ``osc_signals`` is a 2-element list of probe paths (e.g.
        ``["/Vout_p", "/Vout_n"]``), the OCEAN worker will pre-check the
        peak-to-peak of (path0 − path1) before ``safeOceanDumpAll`` and
        short-circuit with ``{"status":"degenerate_not_oscillating"}`` if
        the swing is below the oscillation threshold.
        """
        clean_signals = [_validate_signal(s) for s in signals]
        clean_windows = [_validate_window(w) for w in windows]
        clean_osc = _validate_osc_signals(osc_signals)
        if not clean_signals:
            raise ValueError("signals list is empty")
        if not clean_windows:
            raise ValueError("windows list is empty")

        budget = float(timeout_s if timeout_s is not None
                       else self.cfg.wall_timeout_s)
        run_id = uuid.uuid4().hex[:12]

        remote_spec = (
            f"{self.cfg.remote_tmp_dir}/vb_spec_{run_id}.il"
        )
        remote_result = (
            f"{self.cfg.remote_tmp_dir}/vb_result_{run_id}.json"
        )
        remote_pidfile = (
            f"{self.cfg.remote_tmp_dir}/vb_pid_{run_id}"
        )

        try:
            self._upload_spec(clean_signals, clean_windows, clean_osc, remote_spec)
            self._spawn_and_wait(
                psf_dir=psf_dir,
                remote_spec=remote_spec,
                remote_result=remote_result,
                remote_pidfile=remote_pidfile,
                budget_s=budget,
                run_id=run_id,
            )
            return self._fetch_result(remote_result)
        finally:
            self._cleanup_remote(remote_spec, remote_result, remote_pidfile)

    # ------------------------------------------------------------------ #
    #  Implementation
    # ------------------------------------------------------------------ #

    def _upload_spec(
        self,
        signals: list[tuple[str, str, list[str]]],
        windows: list[tuple[str, float, float]],
        osc_signals: list[str],
        remote_spec: str,
    ) -> None:
        body = _render_spec_il(signals, windows, osc_signals)
        # scp from stdin via ssh cat > file; avoids touching a local tmp
        # file on Windows (mixed path separators, perms, etc.).
        cmd = self.cfg.ssh_base_args() + [f"cat > {shlex.quote(remote_spec)}"]
        logger.debug(
            "OceanWorker: uploading spec (%d bytes) to %s",
            len(body), remote_spec,
        )
        proc = subprocess.run(
            cmd, input=body, capture_output=True,
            text=True, timeout=self.cfg.ssh_connect_timeout_s + 10,
        )
        if proc.returncode != 0:
            raise OceanWorkerSpawnError(
                f"scp of spec failed rc={proc.returncode}: "
                f"{proc.stderr[-400:]}"
            )

    def _spawn_and_wait(
        self,
        psf_dir: str,
        remote_spec: str,
        remote_result: str,
        remote_pidfile: str,
        budget_s: float,
        run_id: str,
    ) -> None:
        # Wrapper shell: (a) record PID, (b) setenv, (c) exec virtuoso.
        # Using bash -lc so the env inherits module system etc. We write
        # PID *before* exec so a timeout kill can find it even if virtuoso
        # is stuck during startup.
        env_exports = {
            "VB_PSF_DIR": psf_dir,
            "VB_SPEC_FILE": remote_spec,
            "VB_RESULT_JSON": remote_result,
            "VB_SKILL_DIR": self.cfg.remote_skill_dir,
        }
        exports_line = " ".join(
            f"export {k}={shlex.quote(v)};" for k, v in env_exports.items()
        )
        worker_ocn = (
            f"{self.cfg.remote_skill_dir}/psf_dump_worker.ocn"
        )
        wrapper = (
            f"echo $$ > {shlex.quote(remote_pidfile)}; "
            f"{exports_line} "
            f"exec {shlex.quote(self.cfg.virtuoso_bin)} "
            f"-ocean -nograph -restore {shlex.quote(worker_ocn)}"
        )
        # remote host default shell is csh; ssh joins our remaining args with
        # spaces and hands them to csh. We must build a single quoted
        # "bash -lc '<wrapper>'" token so csh just forks bash without
        # touching the inner shell syntax.
        cmd = self.cfg.ssh_base_args() + [
            f"bash -lc {shlex.quote(wrapper)}"
        ]

        logger.info(
            "OceanWorker[%s]: spawning virtuoso (psf=%s, budget=%.0fs)",
            run_id, psf_dir, budget_s,
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
            # Capture whatever the subprocess emitted before the kill.
            # The OCEAN worker (skill/psf_dump_worker.ocn) prints per-step
            # timestamps to stderr (step4_openResults, step5_selectResult,
            # step5b_dc_snapshot, step6_dumpAll, ...); on a systemic hang
            # the last timestamp tells us exactly which step blocked.
            def _decode(buf):
                if buf is None:
                    return ""
                if isinstance(buf, bytes):
                    return buf.decode(errors="replace")
                return buf
            partial_stdout = _decode(exc.stdout)
            partial_stderr = _decode(exc.stderr)
            logger.warning(
                "OceanWorker[%s]: wall-clock timeout after %.1fs; "
                "sending kill -9 to remote PID", run_id, elapsed,
            )
            logger.warning(
                "OceanWorker[%s]: partial stdout tail (last 1500 chars):\n%s",
                run_id, (partial_stdout[-1500:] or "<empty>"),
            )
            logger.warning(
                "OceanWorker[%s]: partial stderr tail (last 2000 chars):\n%s",
                run_id, (partial_stderr[-2000:] or "<empty>"),
            )
            self._kill_remote(remote_pidfile, run_id)
            raise OceanWorkerTimeout(
                f"virtuoso subprocess exceeded {budget_s:.0f}s; killed"
            )

        elapsed = time.monotonic() - t0
        logger.info(
            "OceanWorker[%s]: subprocess finished rc=%d in %.1fs",
            run_id, proc.returncode, elapsed,
        )

        # Worker prints `{"status":"ok"}` or `{"status":"error","msg":"..."}`
        # somewhere in stdout (virtuoso also logs lots of cxt banner text).
        status_line = _extract_status_line(proc.stdout)
        if proc.returncode != 0 or status_line is None or \
                status_line.get("status") != "ok":
            stderr_tail = (proc.stderr or "")[-400:]
            stdout_tail = (proc.stdout or "")[-400:]
            msg = (
                status_line.get("msg") if isinstance(status_line, dict)
                else "worker did not emit ok status"
            )
            raise OceanWorkerScriptError(
                f"worker rc={proc.returncode}, msg={msg!r}; "
                f"stdout_tail={stdout_tail!r}; stderr_tail={stderr_tail!r}"
            )

    def _fetch_result(self, remote_result: str) -> dict:
        cmd = self.cfg.ssh_base_args() + [f"cat {shlex.quote(remote_result)}"]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self.cfg.ssh_connect_timeout_s + 10,
            )
        except subprocess.TimeoutExpired as exc:
            raise OceanWorkerSpawnError(
                f"cat of result file timed out: {exc}"
            ) from exc
        if proc.returncode != 0:
            raise OceanWorkerScriptError(
                f"cat result failed rc={proc.returncode}: "
                f"{proc.stderr[-400:]}"
            )
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise OceanWorkerScriptError(
                f"result JSON parse failed: {exc}; "
                f"head={proc.stdout[:200]!r}"
            ) from exc
        if not parsed.get("ok"):
            raise OceanWorkerScriptError(
                f"safeOceanDumpAll returned ok=false: "
                f"{parsed.get('error', '<no error>')}"
            )
        return parsed

    def _kill_remote(self, remote_pidfile: str, run_id: str) -> None:
        # Best-effort: read PID, kill -9 it and its children. We don't
        # raise if this fails — the main error is already the timeout.
        kill_cmd = (
            f"if [ -f {shlex.quote(remote_pidfile)} ]; then "
            f"pid=$(cat {shlex.quote(remote_pidfile)}); "
            f"pkill -9 -P \"$pid\" 2>/dev/null; "
            f"kill -9 \"$pid\" 2>/dev/null; "
            f"echo killed $pid; "
            f"else echo no pidfile; fi"
        )
        cmd = self.cfg.ssh_base_args() + [
            f"bash -lc {shlex.quote(kill_cmd)}"
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self.cfg.ssh_connect_timeout_s + 10,
            )
            logger.info(
                "OceanWorker[%s]: remote kill result: %s",
                run_id, (proc.stdout or proc.stderr).strip()[-200:],
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "OceanWorker[%s]: remote kill ssh call itself timed out",
                run_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "OceanWorker[%s]: remote kill raised %s",
                run_id, type(exc).__name__,
            )

    def _cleanup_remote(self, *paths: str) -> None:
        # Fire-and-forget: tiny temp files, ok if delete fails.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_status_line(stdout: str) -> dict | None:
    """Find the single-line JSON status the worker prints to stdout.

    Virtuoso prints ~60 lines of banner/cxt-loading noise; the worker's
    status is one line starting with ``{"status":``.
    """
    if not stdout:
        return None
    for line in stdout.splitlines():
        s = line.strip()
        if s.startswith('{"status":'):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                continue
    return None


def worker_from_env() -> OceanWorker:
    """Build an :class:`OceanWorker` from the project's usual env vars.

    Reads:
      - VB_REMOTE_HOST, VB_REMOTE_USER: ssh target (from config/.env)
      - VB_REMOTE_SKILL_DIR: remote SKILL dir (defaulted to the value
        we've been using on remote host)
      - VB_VIRTUOSO_BIN: optional override
      - VB_OCEAN_TIMEOUT_S: optional wall-clock override
    """
    host = os.environ.get("VB_REMOTE_HOST")
    user = os.environ.get("VB_REMOTE_USER")
    if not host or not user:
        raise OceanWorkerError(
            "VB_REMOTE_HOST / VB_REMOTE_USER must be set "
            "(load config/.env first)"
        )
    skill_dir = os.environ.get("VB_REMOTE_SKILL_DIR")
    if not skill_dir:
        raise OceanWorkerError(
            "VB_REMOTE_SKILL_DIR must be set (load config/.env first) — "
            "the absolute POSIX path on the remote host where safe_*.il lives."
        )
    cfg = OceanWorkerConfig(
        remote_host=host,
        remote_user=user,
        remote_skill_dir=skill_dir,
        virtuoso_bin=os.environ.get(
            "VB_VIRTUOSO_BIN", DEFAULT_VIRTUOSO_BIN,
        ),
        wall_timeout_s=float(
            os.environ.get("VB_OCEAN_TIMEOUT_S", DEFAULT_WALL_TIMEOUT_S)
        ),
    )
    return OceanWorker(cfg)
