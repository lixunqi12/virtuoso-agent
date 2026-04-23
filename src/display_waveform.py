"""Remote Synopsys WaveView launcher (Task T7).

X11-forwards a Synopsys WaveView session from the remote compute host
(cobi) to the operator's display, letting them inspect a
``.tr[0-9]*`` transient-waveform file without ever copying it to the
local machine. ``.tr*`` artifacts are routinely 500 MB+; pulling them
back would saturate the SSH channel — which is exactly why T3's
:class:`src.hspice_worker.HspiceWorker` already refuses to fetch them.
This module adds the viewer-side complement: open the waveform on the
remote side and render it over X11.

Public surface:

- :func:`display_waveform` — spawn ``wv <tr_path>`` on the remote side
  under ``ssh -X``, poll for the remote pidfile to appear, and return
  a 12-hex ``run_id``. Same run_id / pidfile naming convention as
  :mod:`src.hspice_worker` so operator cleanup scripts are shared.
- :func:`close_waveform` — pair to the above: read the pidfile, send
  ``pkill -9 -P <pid>`` + ``kill -9 <pid>``, then ``rm -f`` the pidfile.
  Mirrors :meth:`HspiceWorker._kill_remote`.

Error taxonomy (inherits T3's hierarchy so ``except HspiceWorkerError``
at a caller catches both families):

- :class:`DisplayTimeout`    (subclass of :class:`HspiceWorkerTimeout`)
- :class:`DisplaySpawn`      (subclass of :class:`HspiceWorkerSpawnError`)
- :class:`DisplayMissingTr`  (subclass of :class:`HspiceWorkerScriptError`)

``.tr*`` fetch is structurally forbidden here:

- T3 already enforces a three-layer defense on the HSpice side
  (``_validate_sp_path`` rejects .tr shape, ``_fetch_file`` rejects .tr
  basenames, ``_list_outputs`` filters .tr from the ls result). Per the
  T7 brief, those are not to be weakened. This module adds a fourth
  layer: :func:`_assert_no_fetch` is called on every outbound
  subprocess command vector to refuse any ``cat`` / ``scp`` / ``rsync``
  / ``sftp`` / ``dd`` launch, argv0 or embedded. The only remote
  commands this module ever issues are ``test -f`` (preflight),
  ``wv <path>`` (the viewer itself), and ``pkill`` / ``kill`` /
  ``rm -f`` (cleanup).
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import time
import uuid
from pathlib import PurePosixPath

from src.hspice_scrub import scrub_lis
from src.hspice_worker import (
    HspiceWorkerConfig,
    HspiceWorkerScriptError,
    HspiceWorkerSpawnError,
    HspiceWorkerTimeout,
    worker_from_env,
)

logger = logging.getLogger(__name__)


__all__ = [
    "display_waveform",
    "close_waveform",
    "DisplayTimeout",
    "DisplaySpawn",
    "DisplayMissingTr",
]


# Accept only absolute POSIX paths ending in ``.tr<digits>``. Character
# set mirrors T3's ``_SP_PATH_RE`` so the two validators stay visually
# consistent; shlex.quote + ``./`` prefix + ``--`` terminators still
# apply at call sites as defense in depth.
_TR_PATH_RE = re.compile(r"^/[A-Za-z0-9_./\-]+\.tr[0-9]*$")
_TR_PATH_NO_DOTDOT_RE = re.compile(r"(^|/)\.\.(/|$)")
# Basename must not start with ``-`` — wv / bash would otherwise parse
# it as a flag once any cd-to-dir step strips the leading path.
_TR_BASENAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*\.tr[0-9]*$")
# Signals are plain identifiers — no dots, slashes, or dashes. Keeps
# the attack surface of the wv CLI arg small and matches the HDL
# conventions used elsewhere in this repo.
_SIGNAL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# close_waveform's run_id must be exactly what display_waveform emitted.
_RUN_ID_RE = re.compile(r"^[0-9a-f]{12}$")

_PIDFILE_TEMPLATE = "{tmp_dir}/wv_pid_{run_id}"
_WV_MODULE = "synopsys/wv_2022.06"

# Fourth-layer fetch guard: any argv0 or embedded shell token that
# reads a remote file and streams it back to the local side. This
# module never needs any of these — wv opens the file in place.
_FETCH_ARGV0 = frozenset({"cat", "scp", "rsync", "sftp", "dd"})


class DisplayTimeout(HspiceWorkerTimeout):
    """WaveView failed to report its pid within ``timeout_sec``.

    Includes the case where ssh connected but wv crashed before
    writing the pidfile (e.g., ``module load`` failure) — we can't
    tell that apart from a slow wv from the local side.
    """


class DisplaySpawn(HspiceWorkerSpawnError):
    """ssh transport / X11 forward could not be established."""


class DisplayMissingTr(HspiceWorkerScriptError):
    """The remote ``.tr*`` file does not exist on the compute host."""


def _validate_tr_path(tr_path_remote: str) -> None:
    if not isinstance(tr_path_remote, str):
        raise ValueError(
            f"tr_path_remote must be str; got {type(tr_path_remote)!r}"
        )
    if _TR_PATH_NO_DOTDOT_RE.search(tr_path_remote):
        raise ValueError("tr_path_remote must not contain '..'")
    if not _TR_PATH_RE.match(tr_path_remote):
        raise ValueError(
            "tr_path_remote must be an absolute POSIX path ending in "
            ".tr[0-9]* and containing only [A-Za-z0-9_./-]"
        )
    basename = PurePosixPath(tr_path_remote).name
    if not _TR_BASENAME_RE.match(basename):
        raise ValueError(
            "tr_path_remote basename must start with alphanumeric/"
            "underscore (rejecting option-like leading dash)"
        )


def _validate_signals(signals: list[str] | tuple[str, ...] | None) -> list[str]:
    """Identifier + foundry-seed validation for the ``signals`` arg.

    Runs each entry through :func:`scrub_lis` and raises if the output
    differs from the input — the T1 scrubber replaces banned foundry
    tokens with ``<redacted>`` and absolute paths with ``<path>``, so a
    delta between input and output is a direct signal that the user
    handed us a sensitive token. Preserve tokens (``vdd`` / ``WBL``
    etc.) pass through unchanged and are accepted.

    Privacy: the exception message DOES NOT echo the offending entry —
    it may BE the sensitive payload. Caller already has the input and
    does not need it repeated in the error.
    """
    if signals is None:
        return []
    if not isinstance(signals, (list, tuple)):
        raise ValueError(
            f"signals must be a list/tuple; got {type(signals)!r}"
        )
    out: list[str] = []
    for s in signals:
        if not isinstance(s, str) or not _SIGNAL_RE.match(s):
            raise ValueError(
                "signals entries must be identifier-shaped "
                "[A-Za-z_][A-Za-z0-9_]*"
            )
        if scrub_lis(s) != s:
            raise ValueError(
                "signals argument contained a banned foundry token "
                "(post-scrub delta detected)"
            )
        out.append(s)
    return out


def _assert_no_fetch(cmd: list[str]) -> None:
    """Fourth-layer guard: refuse to launch a fetch-shaped subprocess.

    Checks two shapes:

    - ``argv0`` (``cmd[0]`` basename) being in :data:`_FETCH_ARGV0`.
    - Any ``bash -lc <script>`` token whose shell script contains a
      fetch verb surrounded by whitespace (catching an embedded
      ``cat <path>`` inside a wrapper).

    Raises:
        RuntimeError: unconditional — any match means the module logic
            was wrongly modified. Caller should treat this as a bug,
            not a runtime condition to handle.
    """
    if not cmd:
        return
    argv0 = cmd[0].rsplit("/", 1)[-1]
    if argv0 in _FETCH_ARGV0:
        raise RuntimeError(
            f"display_waveform must not fetch: argv0={argv0!r}"
        )
    for token in cmd[1:]:
        if not isinstance(token, str):
            continue
        lowered = token.lower()
        for fc in _FETCH_ARGV0:
            # Match ``<verb> ... .tr<digits>`` within a single shell
            # statement (no ``;`` / ``&`` / ``|`` separator in between).
            # This blocks the intended threat — ``cat .../sim.tr0`` —
            # without tripping the legitimate ``pid=$(cat <pidfile>)``
            # pattern in cleanup wrappers, where no .tr path appears.
            # Word boundaries on both sides avoid false positives on
            # substrings like ``cat_run`` (``_`` is a word char so the
            # trailing ``\b`` doesn't match inside the identifier).
            pattern = rf"\b{re.escape(fc)}\b[^;&|]*\.tr[0-9]*\b"
            if re.search(pattern, lowered):
                raise RuntimeError(
                    f"display_waveform must not embed fetch of .tr*: "
                    f"verb={fc!r}"
                )


def _ssh_x_args(cfg: HspiceWorkerConfig) -> list[str]:
    """SSH base args with ``-X`` inserted for X11 forwarding.

    ``-X`` lives immediately after ``ssh`` so the remaining options
    (``BatchMode=yes`` / ``ConnectTimeout`` / ``ServerAliveInterval``)
    still apply — the session remains non-interactive and bounded.
    """
    base = cfg.ssh_base_args()
    return [base[0], "-X"] + base[1:]


def _kill_wrapper(pidfile: str, *, verbose: bool) -> str:
    """Build the remote bash snippet that validates and kills a pid.

    R2 B1 (codex): the earlier form passed the cat'd pidfile contents
    straight into ``pkill -9 -P "$pid"`` / ``kill -9 "$pid"``. A
    corrupt or tampered pidfile containing ``-1`` would expand into
    ``kill -9 -1`` — SIGKILL to every process signalable by the user,
    including the ssh session itself. Same hazard for ``0`` (whole
    process group) and ``1`` (init on many systems). The shell
    ``case`` arm screens these AND any non-decimal content (captured
    by ``*[!0-9]*`` — the ``[!0-9]`` is a shell character class, not
    regex, matching any single non-digit character) before the kill
    commands run. Invalid pids still unlink the pidfile so a bad
    write doesn't jam future close attempts.
    """
    quoted = shlex.quote(pidfile)
    valid_branch = (
        f"pkill -9 -P \"$pid\" 2>/dev/null; "
        f"kill -9 \"$pid\" 2>/dev/null; "
        f"rm -f {quoted}"
    )
    invalid_branch = f"rm -f {quoted}"
    if verbose:
        valid_branch += "; echo killed $pid"
        invalid_branch += "; echo invalid_pid"
    miss_branch = "echo no pidfile" if verbose else ":"
    return (
        f"if [ -f {quoted} ]; then "
        f"pid=$(cat {quoted}); "
        f"case \"$pid\" in "
        f"''|*[!0-9]*|0|1) {invalid_branch};; "
        f"*) {valid_branch};; "
        f"esac; "
        f"else {miss_branch}; fi"
    )


def _best_effort_cleanup(cfg: HspiceWorkerConfig, pidfile: str) -> None:
    """Remote pkill + rm, silent on failure. Used when a spawn fails
    mid-way and we want to make sure no orphan wv is left running."""
    cmd = cfg.ssh_base_args() + [
        f"bash -lc {shlex.quote(_kill_wrapper(pidfile, verbose=False))}"
    ]
    _assert_no_fetch(cmd)
    try:
        subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=cfg.ssh_connect_timeout_s + 10,
        )
    except Exception:  # noqa: BLE001
        pass


def display_waveform(
    tr_path_remote: str,
    signals: list[str] | tuple[str, ...] | None = None,
    timeout_sec: float = 120.0,
    config: HspiceWorkerConfig | None = None,
) -> str:
    """Launch Synopsys WaveView on the remote compute host via ``ssh -X``.

    The ``.tr*`` file is NEVER fetched to the local host. WaveView is
    invoked on the remote side with the file path as an argument; the
    GUI is rendered over X11. Returns the 12-hex ``run_id`` so the
    caller can later close the session via :func:`close_waveform`.

    Args:
        tr_path_remote: absolute POSIX path to a ``.tr[0-9]*`` file on
            the remote host. Must match
            ``^/[A-Za-z0-9_./-]+\\.tr[0-9]*$``.
        signals: optional list of signal identifiers to preselect in
            WaveView. Each entry must be ``[A-Za-z_][A-Za-z0-9_]*``
            AND pass the T1 foundry-seed scrub unchanged.
        timeout_sec: how long to wait for the remote wv process to
            write its pidfile before declaring a spawn failure.
        config: optional :class:`HspiceWorkerConfig`. If omitted,
            built from the project's usual env vars via
            :func:`src.hspice_worker.worker_from_env`.

    Returns:
        12-char lowercase hex ``run_id`` that names the remote
        pidfile at ``<remote_tmp_dir>/wv_pid_<run_id>``.

    Raises:
        DisplayTimeout: pidfile did not appear within ``timeout_sec``.
        DisplaySpawn: ssh transport / X11 forward error, or preflight
            stat could not reach the remote host.
        DisplayMissingTr: the ``.tr*`` file does not exist on remote.
        ValueError: ``tr_path_remote`` or ``signals`` failed validation.
    """
    _validate_tr_path(tr_path_remote)
    sig_list = _validate_signals(signals)

    cfg = config if config is not None else worker_from_env().cfg

    run_id = uuid.uuid4().hex[:12]
    pidfile = _PIDFILE_TEMPLATE.format(
        tmp_dir=cfg.remote_tmp_dir, run_id=run_id
    )

    # Preflight: remote stat. If we spawn blindly and the .tr doesn't
    # exist, wv would fail inside X11 land, give no useful error back
    # to us, and the caller would only see DisplayTimeout after the
    # full budget. Cheaper to fail fast here.
    test_wrapper = f"test -f {shlex.quote(tr_path_remote)}"
    test_cmd = cfg.ssh_base_args() + [f"bash -lc {shlex.quote(test_wrapper)}"]
    _assert_no_fetch(test_cmd)
    try:
        stat = subprocess.run(
            test_cmd, capture_output=True, text=True,
            timeout=cfg.ssh_connect_timeout_s + 10,
        )
    except subprocess.TimeoutExpired:
        raise DisplaySpawn(
            f"preflight stat of tr_path_remote timed out on "
            f"{cfg.ssh_target()}"
        ) from None
    # R2 secondary (codex): trichotomise preflight rc instead of
    # collapsing all non-zero / non-255 into "missing file". `test -f`
    # returns 1 only on missing; rc=2 (shell parse error) or rc=127
    # (test binary not found) are remote-shell failures, not the
    # file-absent case. Mapping them to DisplayMissingTr would mis-tell
    # the caller their file is gone when really their remote shell is
    # broken.
    #
    # R2 B2 (codex): stderr from the remote ssh/shell can echo foundry
    # paths, license-server diagnostics, or kernel-log fragments. We
    # drop the tail from the exception message entirely — category +
    # rc is enough for the caller to distinguish transport vs shell.
    if stat.returncode == 255:
        raise DisplaySpawn(
            "ssh transport error during preflight rc=255 "
            "(remote stderr redacted)"
        )
    if stat.returncode == 1:
        # `test -f` returns 1 on missing; don't echo the full path in
        # the error — it may be sensitive. Basename is enough to
        # triage and is already known to the caller.
        raise DisplayMissingTr(
            "remote .tr* file not found "
            f"(basename={PurePosixPath(tr_path_remote).name})"
        )
    if stat.returncode != 0:
        raise DisplaySpawn(
            f"remote shell error during preflight rc={stat.returncode} "
            "(remote stderr redacted)"
        )

    # Build the wv invocation. We always pass the .tr path; signals
    # become a comma-joined ``-signals`` argument if provided.
    wv_bits = [f"wv {shlex.quote(tr_path_remote)}"]
    if sig_list:
        wv_bits.append("-signals " + shlex.quote(",".join(sig_list)))
    wv_invocation = " ".join(wv_bits)

    wrapper = (
        f"echo $$ > {shlex.quote(pidfile)}; "
        f"module load {shlex.quote(_WV_MODULE)} && "
        f"exec {wv_invocation}"
    )
    cmd = _ssh_x_args(cfg) + [f"bash -lc {shlex.quote(wrapper)}"]
    _assert_no_fetch(cmd)

    logger.info(
        "display_waveform[%s]: spawning wv on %s tr_base=%s signals=%d",
        run_id,
        cfg.ssh_target(),
        PurePosixPath(tr_path_remote).name,
        len(sig_list),
    )

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Poll the remote side for the pidfile. We cannot trust Popen's
    # local return — ssh reports success on handshake, not on wv
    # actually coming up. Polling the remote stat is the earliest
    # reliable signal.
    deadline = time.monotonic() + float(timeout_sec)
    pidfile_seen = False
    while time.monotonic() < deadline:
        local_rc = proc.poll()
        if local_rc is not None and local_rc != 0:
            # R2 B2 (codex): the stderr tail from ssh/wv/X11 can echo
            # foundry paths or license-server diagnostics. Drain it to
            # unblock the pipe buffer, then discard. The exception
            # message carries rc + category only.
            if proc.stderr is not None:
                try:
                    proc.stderr.read()
                except Exception:  # noqa: BLE001
                    pass
            _best_effort_cleanup(cfg, pidfile)
            raise DisplaySpawn(
                f"ssh -X / wv spawn exited rc={local_rc} "
                "(remote stderr redacted)"
            )
        stat_wrapper = f"test -f {shlex.quote(pidfile)}"
        stat_cmd = cfg.ssh_base_args() + [
            f"bash -lc {shlex.quote(stat_wrapper)}"
        ]
        _assert_no_fetch(stat_cmd)
        try:
            stat_probe = subprocess.run(
                stat_cmd, capture_output=True, text=True,
                timeout=cfg.ssh_connect_timeout_s + 5,
            )
        except subprocess.TimeoutExpired:
            time.sleep(1.0)
            continue
        if stat_probe.returncode == 0:
            pidfile_seen = True
            break
        time.sleep(1.0)

    if not pidfile_seen:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        _best_effort_cleanup(cfg, pidfile)
        raise DisplayTimeout(
            f"wv pidfile did not appear within {timeout_sec:.0f}s"
        )

    logger.info(
        "display_waveform[%s]: wv pidfile detected; returning run_id",
        run_id,
    )
    return run_id


def close_waveform(
    run_id: str,
    config: HspiceWorkerConfig | None = None,
) -> None:
    """Pair to :func:`display_waveform`: pkill the remote wv session.

    Idempotent — a missing pidfile is not an error (the user may have
    closed WaveView from the GUI already). Mirrors
    :meth:`HspiceWorker._kill_remote` exactly so ops tooling that
    already knows the T3 shape can re-use it.

    Args:
        run_id: the 12-char lowercase hex string returned by
            :func:`display_waveform`.
        config: optional :class:`HspiceWorkerConfig`. If omitted,
            built from env via :func:`worker_from_env`.

    Raises:
        ValueError: ``run_id`` is not 12 lowercase-hex chars.
    """
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise ValueError(
            "run_id must be a 12-char lowercase hex string"
        )
    cfg = config if config is not None else worker_from_env().cfg
    pidfile = _PIDFILE_TEMPLATE.format(
        tmp_dir=cfg.remote_tmp_dir, run_id=run_id
    )

    # R2 B1: shared wrapper carries the pid-validation guard so the
    # same ``kill -9 -1`` hazard cannot be reintroduced in one branch
    # while being fixed in the other.
    cmd = cfg.ssh_base_args() + [
        f"bash -lc {shlex.quote(_kill_wrapper(pidfile, verbose=True))}"
    ]
    _assert_no_fetch(cmd)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=cfg.ssh_connect_timeout_s + 10,
        )
        logger.info(
            "close_waveform[%s]: remote kill result=%s",
            run_id,
            (proc.stdout or proc.stderr).strip()[-200:],
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "close_waveform[%s]: remote kill ssh call itself timed out",
            run_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "close_waveform[%s]: remote kill raised %s",
            run_id, type(exc).__name__,
        )
