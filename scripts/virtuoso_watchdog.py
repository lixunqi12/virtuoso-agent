#!/usr/bin/env python3
"""Virtuoso Bridge Watchdog — auto-reconnect on VPN/SSH drops.

Runs as a long-lived background process. Periodically pings the RAMIC daemon
through the existing SSH tunnel. If the ping fails, tears the tunnel down and
restarts it until the daemon responds again.

Typical failure modes handled:
  * Cisco VPN session expired (hard 12-hour cap) -> tunnel dies -> restart
    after user manually re-dials Cisco
  * Network flap or laptop sleep -> tunnel dies briefly -> reconnect
  * Windows-side bridge process crashed -> restart cleanly

Not handled (requires manual intervention):
  * RAMIC daemon on remote host died (Virtuoso crash, user closed CIW) -> must
    reload the setup .il inside Virtuoso

Usage
-----
    # From project root, in Git Bash:
    .venv/Scripts/python.exe scripts/virtuoso_watchdog.py

    # Options:
    --interval 30      Seconds between health pings (default 30)
    --log FILE         Log file path (default logs/watchdog.log)
    --verbose          Print to stdout in addition to log file
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_EXE = PROJECT_ROOT / ".venv" / "Scripts" / "virtuoso-bridge.exe"

# Exponential backoff caps
MIN_BACKOFF = 10
MAX_BACKOFF = 300
PING_TIMEOUT = 8


def _setup_logging(log_path: Path, verbose: bool) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("vb_watchdog")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if verbose:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger


def _ping_bridge(logger: logging.Logger) -> bool:
    """Return True if the bridge can execute a trivial SKILL end-to-end."""
    try:
        from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient
    except Exception as exc:  # noqa: BLE001
        logger.error("virtuoso_bridge import failed: %s", exc)
        return False

    try:
        vc = VirtuosoClient.from_env(timeout=PING_TIMEOUT, log_to_ciw=False)
        r = vc.execute_skill("1+1", timeout=PING_TIMEOUT)
        if str(r.status).endswith("SUCCESS") and r.output.strip() == "2":
            return True
        logger.warning("Ping bad reply: status=%s output=%r errors=%s",
                       r.status, r.output, r.errors)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ping raised: %s", exc)
        return False


def _run_bridge(cmd: str, logger: logging.Logger, timeout: int = 60) -> int:
    """Invoke virtuoso-bridge.exe <cmd> and log stdout/stderr."""
    if not BRIDGE_EXE.exists():
        logger.error("Bridge exe not found: %s", BRIDGE_EXE)
        return -1
    try:
        proc = subprocess.run(
            [str(BRIDGE_EXE), cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(PROJECT_ROOT),
        )
    except subprocess.TimeoutExpired:
        logger.error("'%s %s' timed out after %ds", BRIDGE_EXE.name, cmd, timeout)
        return -1
    if proc.stdout.strip():
        logger.info("[%s stdout] %s", cmd, proc.stdout.strip().replace("\n", " | "))
    if proc.stderr.strip():
        logger.warning("[%s stderr] %s", cmd, proc.stderr.strip().replace("\n", " | "))
    return proc.returncode


def _restart_tunnel(logger: logging.Logger) -> bool:
    """Tear down then bring up the tunnel. Return True if daemon responds."""
    logger.info("Restarting tunnel...")
    _run_bridge("stop", logger, timeout=30)
    time.sleep(2)
    rc = _run_bridge("start", logger, timeout=60)
    if rc != 0:
        logger.warning("'start' exit=%s (VPN probably still down)", rc)
        return False
    # Give daemon a moment to re-register
    time.sleep(3)
    return _ping_bridge(logger)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=30,
                        help="Seconds between health pings (default: 30)")
    parser.add_argument("--log", type=Path,
                        default=PROJECT_ROOT / "logs" / "watchdog.log",
                        help="Log file path")
    parser.add_argument("--verbose", action="store_true",
                        help="Also print to stdout")
    args = parser.parse_args()

    logger = _setup_logging(args.log, args.verbose)
    logger.info("=" * 60)
    logger.info("Watchdog started (pid=%d interval=%ds log=%s)",
                os.getpid(), args.interval, args.log)
    logger.info("=" * 60)

    stop_requested = {"flag": False}

    def _handle_sigterm(signum, frame):  # noqa: ARG001
        stop_requested["flag"] = True
        logger.info("Signal %d received, stopping watchdog", signum)

    signal.signal(signal.SIGINT, _handle_sigterm)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_sigterm)

    backoff = MIN_BACKOFF
    consecutive_failures = 0
    consecutive_successes = 0
    HEARTBEAT_EVERY = 20  # log a heartbeat every N successful pings

    while not stop_requested["flag"]:
        if _ping_bridge(logger):
            if consecutive_failures > 0:
                logger.info("Tunnel recovered after %d failure(s)",
                            consecutive_failures)
            consecutive_failures = 0
            consecutive_successes += 1
            if consecutive_successes == 1 or consecutive_successes % HEARTBEAT_EVERY == 0:
                logger.info("Tunnel healthy (ping #%d)", consecutive_successes)
            backoff = MIN_BACKOFF
            _sleep_interruptible(args.interval, stop_requested)
            continue

        consecutive_failures += 1
        consecutive_successes = 0
        logger.warning("Ping failed (#%d). Attempting reconnect...",
                       consecutive_failures)

        if _restart_tunnel(logger):
            logger.info("Reconnect successful")
            consecutive_failures = 0
            backoff = MIN_BACKOFF
            _sleep_interruptible(args.interval, stop_requested)
        else:
            logger.warning("Reconnect failed, backing off %ds "
                           "(likely VPN disconnected, waiting for user to "
                           "re-dial Cisco)", backoff)
            _sleep_interruptible(backoff, stop_requested)
            backoff = min(backoff * 2, MAX_BACKOFF)

    logger.info("Watchdog exiting.")
    return 0


def _sleep_interruptible(seconds: int, stop_flag: dict) -> None:
    # Sleep in small chunks so Ctrl+C is responsive.
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if stop_flag["flag"]:
            return
        time.sleep(min(1.0, max(0.0, end - time.monotonic())))


if __name__ == "__main__":
    sys.exit(main())
