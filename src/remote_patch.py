"""Remote in-place ``.PARAM`` patcher for HSpice ``.sp`` files.

T8.3-fix (2026-04-26): the previous push-full-file path
(:meth:`HspiceAgent._push_target_to_remote`, removed) shipped a
LOCAL-side scrubbed .sp body back to cobi, overwriting the original
file with ``<redacted>`` placeholders. That corrupted both the
running HSpice deck AND the operator's original PDK-bearing .sp on
the remote. This module replaces that flow: the rewrite executes
**on the remote host**, with the local agent shipping only the
proposed key/value pairs and the embedded rewriter source.

PRIVACY POSTURE (constraint 1 + 2)
-----------------------------------
* ``argv`` carries only ``python3 -`` -- ps + auth.log do not see
  any path, key, value, or netlist content.
* The transmitted stdin is the rewriter source (a verbatim copy of
  :mod:`src.sp_rewrite` shipped over base64) plus a JSON config
  blob baked into the script as a literal. **No netlist body
  ever crosses ssh outward.**
* Remote stdout follows a fixed shape (``OK_BACKUP: <path>`` /
  ``OK: N keys patched`` [``(noop)``]). Remote stderr on failure
  carries only a category label + sanitized rewriter message
  (line numbers + key names; never values, never file content).
* Every byte of remote stdout/stderr passes through
  :func:`_sanitize_remote_stderr` -- the SINGLE chokepoint that
  masks foundry tokens before any logging or exception re-raise.

ATOMIC REMOTE WRITE (constraint 3)
-----------------------------------
The remote script writes to ``<path>.tmp.<pid>`` then ``os.rename``
to ``<path>``. ``rename(2)`` is atomic on POSIX, so an ssh
interrupt between the write and the rename leaves only a stray
``.tmp.<pid>`` -- the original remote .sp is untouched.

ONE-TIME BACKUP PER AGENT RUN (constraint 4)
---------------------------------------------
:class:`RemotePatcher` carries a ``set[str]`` of remote paths
already backed up. The first patch of each path triggers
``<path>.orig_<YYYYMMDD_HHMMSS>``. Subsequent patches of the
same path from the SAME ``RemotePatcher`` instance skip the
backup attempt entirely. The timestamp is generated once per
:class:`RemotePatcher` instance.

R2 race-fix (codex 2026-04-26): the original ``if not os.path.exists
+ shutil.copy2`` pattern was non-atomic. Two concurrent
:class:`RemotePatcher` instances against the same remote path
within the same second computed identical backup filenames; both
could pass the existence check before either copy started, racing
on the destination and producing a backup whose contents were
already-mutated bytes from the OTHER instance's patch.

The remote script now uses ``os.open(O_CREAT | O_EXCL | O_WRONLY)``
for the backup file — a single atomic syscall that either creates
the destination or raises ``FileExistsError``. The losing caller
emits ``OK_BACKUP_EXISTS: <path>`` instead of ``OK_BACKUP: <path>``,
and the local side treats both as "backup is in place." Earliest
caller wins — its bytes are the pristine pre-patch original. This
is the safest semantics for the operator's PDK preservation goal.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "RemotePatchError",
    "RemotePatchResult",
    "RemotePatcher",
]


# Banned-token regex (mirrors ``$Banned`` in scripts/check_p0_gate.ps1).
# Single source of truth on the local side -- the remote protocol is
# already structured to never echo file content, so any leak that
# survives is going to be in a Python traceback or an ad-hoc print
# statement the operator added during debugging. Mask those.
_FOUNDRY_TOKEN_RE = re.compile(
    r"\b(?:nch_\w*|pch_\w*|cfmom\w*|rppoly\w*|rm1_\w*|tsmc\w*|tcbn\w*)\b",
    re.IGNORECASE,
)


class RemotePatchError(RuntimeError):
    """Raised when the remote patch fails for any reason.

    The ``str()`` of the exception carries ONLY:
      * The remote-reported FAIL category and exit code, and
      * A sanitized tail of remote stderr (foundry tokens redacted,
        key names + line numbers preserved).
    """


@dataclass(frozen=True)
class RemotePatchResult:
    """Outcome of a single remote patch call."""

    keys_patched: int
    backup_path: str | None  # absolute remote path of backup, or None if skipped
    noop: bool                # True iff remote file was already in proposed state
    backup_already_existed: bool = False  # True iff this caller LOST the O_EXCL race
    #                                       (i.e. ``OK_BACKUP_EXISTS`` was emitted
    #                                       instead of ``OK_BACKUP``). The backup
    #                                       still exists on the remote -- another
    #                                       caller (or a previous run with the same
    #                                       timestamp) created it first.


# --------------------------------------------------------------------- #
#  Public API
# --------------------------------------------------------------------- #

class RemotePatcher:
    """Remote in-place .PARAM rewriter.

    Holds the per-run backup state so :meth:`patch` can issue the
    one-time backup on the first call for each remote path.
    """

    # Sentinel used to bake the JSON config into the remote script.
    # Picked to be impossible to occur in valid JSON output (JSON
    # strings escape ``<`` only when configured to; even unescaped
    # ``<<<CONFIG_JSON>>>`` cannot appear in a numeric/short value
    # the LLM would emit). A defensive ``assert`` before substitution
    # catches the contrived case anyway.
    _CONFIG_PLACEHOLDER = "<<<__VIRTUOSO_AGENT_CONFIG_JSON_PLACEHOLDER__>>>"
    _SP_PLACEHOLDER = "<<<__VIRTUOSO_AGENT_SP_REWRITE_SRC_B64__>>>"

    def __init__(
        self,
        ssh_args: Iterable[str],
        timeout_s: int = 60,
    ) -> None:
        self.ssh_args: list[str] = list(ssh_args)
        if not self.ssh_args:
            raise ValueError(
                "ssh_args must include at least the ssh binary + host"
            )
        self.timeout_s = int(timeout_s)
        self._backed_up_remote_paths: set[str] = set()
        self._backup_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    def patch(
        self,
        remote_path: str,
        new_params: dict[str, Any],
        whitelist: Iterable[str],
    ) -> RemotePatchResult:
        """Patch ``remote_path`` in place on the remote host."""
        if not new_params:
            raise ValueError(
                "patch() called with empty new_params; a noop must not "
                "round-trip to the remote host"
            )
        if not isinstance(remote_path, str) or not remote_path.strip():
            raise ValueError("remote_path must be a non-empty string")

        do_backup = remote_path not in self._backed_up_remote_paths
        script = self._render_remote_script(
            remote_path=remote_path,
            new_params=new_params,
            whitelist=list(whitelist),
            do_backup=do_backup,
            backup_ts=self._backup_ts,
        )
        cmd = self.ssh_args + ["python3", "-"]
        try:
            proc = subprocess.run(
                cmd,
                input=script,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise RemotePatchError(
                f"remote patch timed out after {self.timeout_s}s"
            ) from exc

        # Sanitize EVERY byte of remote stdout/stderr through the
        # single chokepoint before anything else looks at it. Failures
        # below this line operate on already-redacted text.
        clean_stderr = _sanitize_remote_stderr(proc.stderr or "")
        clean_stdout = _sanitize_remote_stderr(proc.stdout or "")

        if proc.returncode != 0:
            tail_lines = clean_stderr.strip().splitlines()
            tail = tail_lines[-1] if tail_lines else "(no stderr)"
            raise RemotePatchError(
                f"remote patch rc={proc.returncode}: {tail}"
            )

        result = _parse_status_lines(clean_stdout)

        if do_backup and result.backup_path:
            self._backed_up_remote_paths.add(remote_path)
            if result.backup_already_existed:
                logger.info(
                    "remote_patch backup already in place "
                    "(O_EXCL race lost; earliest snapshot wins): %s",
                    result.backup_path,
                )
            else:
                logger.info(
                    "remote_patch backup created: %s", result.backup_path,
                )
        elif do_backup and not result.backup_path:
            logger.warning(
                "remote_patch first-touch backup expected but not "
                "reported for %s; not marking backed-up.", remote_path,
            )
        return result

    # ------------------------------------------------------------------ #
    #  Script rendering (visible at instance level so tests can
    #  introspect the exact bytes sent to ssh).
    # ------------------------------------------------------------------ #

    @classmethod
    def _render_remote_script(
        cls,
        *,
        remote_path: str,
        new_params: dict[str, Any],
        whitelist: list[str],
        do_backup: bool,
        backup_ts: str,
    ) -> str:
        """Build the self-contained Python script sent over ssh stdin.

        The script body is:
          1. A base64-embedded copy of :mod:`src.sp_rewrite` source --
             same parser the local-side tests cover.
          2. A JSON config blob (path + new_params + whitelist + flags)
             baked in as a Python ``json.loads`` literal.
          3. ~50 lines of glue: backup, read, rewrite, atomic write,
             status emit.
        """
        sp_rewrite_path = Path(__file__).resolve().parent / "sp_rewrite.py"
        sp_src = sp_rewrite_path.read_text(encoding="utf-8")
        sp_b64 = base64.b64encode(sp_src.encode("utf-8")).decode("ascii")

        config_json = json.dumps({
            "remote_path": remote_path,
            "new_params": new_params,
            "whitelist": list(whitelist),
            "do_backup": bool(do_backup),
            "backup_ts": backup_ts,
        })
        # Defensive: the placeholder must not occur in either substitution
        # payload, otherwise the .replace() would corrupt the script.
        if cls._CONFIG_PLACEHOLDER in config_json:
            raise RuntimeError("config payload collided with placeholder")
        if cls._SP_PLACEHOLDER in sp_b64:
            raise RuntimeError("sp_rewrite source collided with placeholder")

        return (
            _REMOTE_TEMPLATE
            .replace(cls._SP_PLACEHOLDER, sp_b64)
            .replace(cls._CONFIG_PLACEHOLDER, config_json)
        )


# --------------------------------------------------------------------- #
#  Helpers (module-level, unit-testable in isolation).
# --------------------------------------------------------------------- #

def _sanitize_remote_stderr(text: str) -> str:
    """Mask foundry tokens that may have leaked through a remote
    Python traceback. Single chokepoint -- every byte of remote
    stdout/stderr that the local side touches goes through here.
    """
    return _FOUNDRY_TOKEN_RE.sub("<REDACTED>", text)


_OK_KEYS_RE = re.compile(r"^OK:\s+(?P<n>\d+)\s+keys\s+patched(?P<noop>\s+\(noop\))?\s*$")


def _parse_status_lines(stdout: str) -> RemotePatchResult:
    """Parse the controlled stdout protocol from the remote patcher.

    Recognised lines:
      * ``OK_BACKUP: <path>``         -- this caller WON the O_EXCL race
                                         and created the backup.
      * ``OK_BACKUP_EXISTS: <path>``  -- this caller LOST the O_EXCL race
                                         (or the backup was already in
                                         place from a prior run with the
                                         same timestamp). Backup is still
                                         valid; another caller's bytes.
      * ``OK: <N> keys patched``      -- patch applied, N keys touched.
      * ``OK: 0 keys patched (noop)`` -- file already in proposed state.
    """
    backup_path: str | None = None
    backup_already_existed = False
    keys_patched: int | None = None
    noop = False
    for raw in stdout.splitlines():
        line = raw.strip()
        if line.startswith("OK_BACKUP: "):
            backup_path = line[len("OK_BACKUP: "):].strip() or None
            backup_already_existed = False
            continue
        if line.startswith("OK_BACKUP_EXISTS: "):
            backup_path = line[len("OK_BACKUP_EXISTS: "):].strip() or None
            backup_already_existed = True
            continue
        m = _OK_KEYS_RE.match(line)
        if m:
            keys_patched = int(m.group("n"))
            noop = bool(m.group("noop"))
    if keys_patched is None:
        raise RemotePatchError(
            "remote patcher returned rc=0 but no recognisable status "
            "line ('OK: <N> keys patched' missing from stdout)"
        )
    return RemotePatchResult(
        keys_patched=keys_patched,
        backup_path=backup_path,
        noop=noop,
        backup_already_existed=backup_already_existed,
    )


# --------------------------------------------------------------------- #
#  Remote script template.
#
#  The rewriter source is loaded from src/sp_rewrite.py at render time
#  (single source of truth; a parity test in tests/test_remote_patch.py
#  exec's this template and asserts identical output to the local
#  rewrite_params on a shared fixture). The main glue lives here:
#
#    * read REMOTE_PATH, refuse if not a regular file
#    * cp -n equivalent backup (only on first touch from this run)
#    * rewrite via embedded src.sp_rewrite.rewrite_params
#    * write tmp.<pid> + os.rename (atomic on POSIX)
#    * print exactly one OK: line on success
#    * print FAIL: <category> ... on stderr otherwise (exit non-zero)
#
#  No print(file) of file CONTENT anywhere -- only categories + key
#  names. Exception messages from ParamRewriteError are pre-cleaned by
#  the rewriter contract (never include values, only key names).
# --------------------------------------------------------------------- #

_REMOTE_TEMPLATE = '''\
import base64, json, os, sys, shutil

# ---- embedded rewriter (verbatim copy of src/sp_rewrite.py) ---- #
_SP_SRC = base64.b64decode(b"<<<__VIRTUOSO_AGENT_SP_REWRITE_SRC_B64__>>>").decode("utf-8")
_ns = {"__name__": "_embedded_sp_rewrite"}
exec(compile(_SP_SRC, "<sp_rewrite>", "exec"), _ns)
rewrite_params = _ns["rewrite_params"]
ParamRewriteError = _ns["ParamRewriteError"]

# ---- baked-in config (no argv, no separate stdin stream) ---- #
_CFG = json.loads("""<<<__VIRTUOSO_AGENT_CONFIG_JSON_PLACEHOLDER__>>>""")
REMOTE_PATH = _CFG["remote_path"]
NEW_PARAMS = _CFG["new_params"]
WHITELIST = _CFG["whitelist"]
DO_BACKUP = _CFG["do_backup"]
BACKUP_TS = _CFG["backup_ts"]


def _emit(line):
    sys.stdout.write(line + "\\n")
    sys.stdout.flush()


try:
    if not os.path.isfile(REMOTE_PATH):
        sys.stderr.write("FAIL: not_found\\n")
        sys.exit(2)

    if DO_BACKUP:
        backup_path = REMOTE_PATH + ".orig_" + BACKUP_TS
        # R2 race-fix: atomic O_CREAT|O_EXCL create. Two parallel
        # RemotePatcher instances against the same remote_path within
        # the same second compute identical backup_path; the previous
        # `if not exists + copy2` pattern raced (both could pass the
        # check simultaneously). O_EXCL serialises the create at the
        # kernel: exactly one caller's open() succeeds, all others
        # see FileExistsError. Earliest caller wins -- their bytes
        # are the pristine pre-patch original. Losers emit
        # OK_BACKUP_EXISTS so the local side knows the backup is in
        # place even though THIS caller did not create it.
        try:
            _bfd = os.open(
                backup_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
            try:
                with os.fdopen(_bfd, "wb") as _bf:
                    with open(REMOTE_PATH, "rb") as _src:
                        shutil.copyfileobj(_src, _bf)
            except Exception:
                # Backup write failed mid-stream -- unlink the partial
                # so a future caller's O_EXCL succeeds and produces a
                # complete snapshot. Never leave a truncated backup.
                try:
                    os.unlink(backup_path)
                except Exception:
                    pass
                raise
            _emit("OK_BACKUP: " + backup_path)
        except FileExistsError:
            _emit("OK_BACKUP_EXISTS: " + backup_path)

    with open(REMOTE_PATH, "r") as f:
        original = f.read()

    new_text = rewrite_params(original, NEW_PARAMS, WHITELIST)

    if new_text == original:
        _emit("OK: 0 keys patched (noop)")
        sys.exit(0)

    tmp_path = REMOTE_PATH + ".tmp." + str(os.getpid())
    try:
        with open(tmp_path, "w") as f:
            f.write(new_text)
        # POSIX rename(2): atomic over the destination. ssh interrupt
        # between write() and rename() leaves only the .tmp.<pid> stub.
        os.rename(tmp_path, REMOTE_PATH)
    except Exception:
        # Best-effort cleanup so a partial write does not pile up
        # stale .tmp.<pid> siblings on the remote.
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:
            pass
        raise

    _emit("OK: " + str(len(NEW_PARAMS)) + " keys patched")
    sys.exit(0)

except ParamRewriteError as e:
    # rewrite_params() error messages carry only key names + categories
    # by contract -- never values, never file body. Safe to surface.
    sys.stderr.write("FAIL: rewrite_error " + str(e) + "\\n")
    sys.exit(3)

except Exception as e:
    # Surface only the exception TYPE NAME -- args[] may carry
    # filesystem paths or netlist snippets we cannot vouch for.
    sys.stderr.write("FAIL: unexpected " + type(e).__name__ + "\\n")
    sys.exit(4)
'''
