#!/usr/bin/env bash
# Sync this repo to the racknerd harness user and run pytest inside a transient
# systemd unit with a delegated cgroup (Delegate=yes) as the unprivileged
# `harness` user. This is the ONLY correct way to run the Linux-marked tests:
# an interactive ssh session's cgroup is not delegated.
#
# Usage: scripts/remote-test.sh [remote-subdir] [pytest args...]
#   remote-subdir defaults to "ams"; agents working in parallel should pass a
#   distinct subdir (e.g. "ams-spawn") so rsyncs do not clobber each other.
set -euo pipefail
HOST=${AMS_HOST:-racknerd}
SUBDIR=${1:-ams}; shift || true
LOCAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_ROOT="/home/harness/${SUBDIR}"
REMOTE_PY="/home/harness/venv/bin/python3"   # path MUST match the AppArmor profile glob

ssh -o BatchMode=yes "$HOST" "mkdir -p '$REMOTE_ROOT' && chown harness:harness '$REMOTE_ROOT'"
# macOS ships openrsync (no --chown); fix ownership remotely instead.
rsync -az --delete --exclude .git --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
  "$LOCAL_ROOT/" "$HOST:$REMOTE_ROOT/"
ssh -o BatchMode=yes "$HOST" "chown -R harness:harness '$REMOTE_ROOT'"

# shellcheck disable=SC2029
ssh -o BatchMode=yes "$HOST" systemd-run --uid=harness --gid=harness -p Delegate=yes \
  --wait --pipe --collect -q \
  --working-directory="$REMOTE_ROOT" \
  -E HOME=/home/harness -E AMS_STATE_DIR=/home/harness/state/"$SUBDIR" -E PYTHONPATH=src \
  -E PATH=/home/harness/venv/bin:/home/harness/.local/bin:/usr/local/bin:/usr/bin:/bin \
  -- "$REMOTE_PY" -m pytest -q -p no:cacheprovider "$@"
