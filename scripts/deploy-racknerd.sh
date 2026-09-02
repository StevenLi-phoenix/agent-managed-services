#!/usr/bin/env bash
# Deploy the harness to the target box and (re)start it under systemd.
#
# Usage: scripts/deploy-racknerd.sh [--no-restart]
#
# Unlike scripts/remote-test.sh (which rsyncs to a throwaway subdir and runs
# pytest in a transient unit), this writes the PRODUCTION code location
# /home/harness/ams that deploy/ams-harness.service points ExecStart at, and
# installs/reloads the unit itself. Host prerequisites are deploy/install-host.sh's
# job and are not re-done here.
set -euo pipefail

HOST=${AMS_HOST:-racknerd}
LOCAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_ROOT=/home/harness/ams
UNIT=ams-harness.service
RESTART=1
[ "${1:-}" = "--no-restart" ] && RESTART=0

echo "==> rsync $LOCAL_ROOT -> $HOST:$REMOTE_ROOT"
ssh -o BatchMode=yes "$HOST" "mkdir -p '$REMOTE_ROOT'"
# macOS ships openrsync (no --chown); fix ownership remotely instead.
rsync -az --delete --exclude .git --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
  "$LOCAL_ROOT/" "$HOST:$REMOTE_ROOT/"
ssh -o BatchMode=yes "$HOST" "chown -R harness:harness '$REMOTE_ROOT'"

echo "==> install $UNIT"
# shellcheck disable=SC2029
ssh -o BatchMode=yes "$HOST" "install -m 0644 '$REMOTE_ROOT/deploy/$UNIT' /etc/systemd/system/$UNIT \
  && systemctl daemon-reload"

if [ "$RESTART" = 1 ]; then
  echo "==> restart $UNIT"
  # shellcheck disable=SC2029
  ssh -o BatchMode=yes "$HOST" "systemctl restart $UNIT && sleep 2 && systemctl is-active $UNIT"
  ssh -o BatchMode=yes "$HOST" "journalctl -u $UNIT -n 30 --no-pager"
else
  echo "==> skipped restart (--no-restart)"
fi
