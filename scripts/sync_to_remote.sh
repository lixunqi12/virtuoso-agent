#!/usr/bin/env bash
# One-click sync: PC working tree -> remote host deploy.
#
# remote host sits behind a firewall that blocks outbound github.com, so the
# standard "PC push; remote host pull" flow does not work. Instead we bundle
# the local git history, scp it to remote host, and hard-reset the remote host work
# tree to match. Untracked / gitignored files on remote host (stage0_*.il
# probes, .venv, wheels, ~/.virtuoso/pdk_map_private.il, etc.) are
# preserved.
#
# Usage:
#   bash scripts/sync_to_remote.sh           # sync current HEAD
#   bash scripts/sync_to_remote.sh --push    # also `git push` first
#
# Configuration (the script needs to know which SSH host and which
# remote path to sync to). Real values MUST NOT live in this file —
# it's tracked in a public repo. Create a gitignored override at
# config/.sync.local (see config/.sync.local.template) containing:
#
#   REMOTE_HOST=user@your-remote-host.example.edu
#   REMOTE_REPO_DIR=/remote/path/to/virtuoso-agent
#   REMOTE_BUNDLE_PATH=/tmp/virtuoso-agent.bundle   # optional
#
# Any of those can also be passed via environment instead of the file.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# Load user-local overrides (gitignored). Must come before the
# ${VAR:-default} expansions below.
if [ -f "$REPO_ROOT/config/.sync.local" ]; then
  # shellcheck source=/dev/null
  source "$REPO_ROOT/config/.sync.local"
fi

REMOTE_HOST="${REMOTE_HOST:-user@your-remote-host.example.edu}"
REMOTE_REPO_DIR="${REMOTE_REPO_DIR:-/remote/path/to/virtuoso-agent}"
REMOTE_BUNDLE_PATH="${REMOTE_BUNDLE_PATH:-/tmp/virtuoso-agent.bundle}"

# Refuse to run with placeholder values — they'd produce confusing
# "Name or service not known" / "No such file" errors from ssh/scp.
if [[ "$REMOTE_HOST" == *"your-remote-host.example.edu"* ]] \
   || [[ "$REMOTE_REPO_DIR" == "/remote/path/to/"* ]]; then
  cat >&2 <<'MSG'
ERROR: sync config is still at placeholder values.

Create config/.sync.local (gitignored) from the template:

    cp config/.sync.local.template config/.sync.local
    # edit with your real remote host + repo path

or export REMOTE_HOST / REMOTE_REPO_DIR in your shell before running.
MSG
  exit 2
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "WARNING: working tree is dirty. Uncommitted changes will NOT be" >&2
  echo "         synced (bundle only carries committed history)." >&2
  git status --short | head -10 >&2
  echo >&2
fi

if [[ "${1:-}" == "--push" ]]; then
  echo "[1/4] git push"
  git push
fi

LOCAL_BUNDLE="$(mktemp --suffix=.bundle 2>/dev/null || mktemp)"
trap 'rm -f "$LOCAL_BUNDLE"' EXIT

echo "[2/4] bundle HEAD + all refs -> $LOCAL_BUNDLE"
git bundle create "$LOCAL_BUNDLE" --all
echo "      $(du -h "$LOCAL_BUNDLE" | cut -f1) bundle ready"

echo "[3/4] scp -> remote host:$REMOTE_BUNDLE_PATH"
scp -q "$LOCAL_BUNDLE" "$REMOTE_HOST:$REMOTE_BUNDLE_PATH"

echo "[4/4] remote fetch + reset"
ssh "$REMOTE_HOST" "bash -s" <<REMOTE
set -euo pipefail
cd "$REMOTE_REPO_DIR"

git fetch "$REMOTE_BUNDLE_PATH" main:refs/remotes/bundle/main 2>&1 | tail -3

BEFORE=\$(git rev-parse HEAD)
git reset --hard bundle/main
AFTER=\$(git rev-parse HEAD)

if [ "\$BEFORE" = "\$AFTER" ]; then
  echo "      remote host already at \${AFTER:0:8} (no change)"
else
  echo "      \${BEFORE:0:8} -> \${AFTER:0:8}"
  git log --oneline "\$BEFORE..\$AFTER" | head -10
fi
REMOTE

echo
echo "sync complete. local HEAD: $(git rev-parse --short HEAD)"
