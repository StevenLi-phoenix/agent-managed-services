#!/usr/bin/env bash
# Deploy the harness to a host and (re)start it under systemd.
#
# Usage: AMS_HOST=<ssh host> scripts/deploy.sh [--no-restart]
#   The ssh user is root or has passwordless sudo.
#
# Unlike scripts/remote-test.sh (which rsyncs to a throwaway subdir and runs
# pytest in a transient unit), this writes the PRODUCTION code location
# /home/harness/ams that deploy/ams-harness.service points ExecStart at, and
# installs/reloads the unit itself. Host prerequisites are deploy/install-host.sh's
# job and are not re-done here.
set -euo pipefail

HOST=${AMS_HOST:?set AMS_HOST to an ssh host alias}
LOCAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_ROOT=/home/harness/ams
UNIT=ams-harness.service
RESTART=1
[ "${1:-}" = "--no-restart" ] && RESTART=0

STAGE="ams-src/deploy"
echo "==> rsync $LOCAL_ROOT -> $HOST:$REMOTE_ROOT (via ~/$STAGE)"
ssh -o BatchMode=yes "$HOST" "mkdir -p '$STAGE'"
rsync -az --delete --exclude .git --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
  --exclude .ruff_cache "$LOCAL_ROOT/" "$HOST:$STAGE/"
# shellcheck disable=SC2029
ssh -o BatchMode=yes "$HOST" "sudo -n mkdir -p '$REMOTE_ROOT' \
  && sudo -n rsync -a --delete '$STAGE/' '$REMOTE_ROOT/' \
  && sudo -n chown -R harness:harness '$REMOTE_ROOT'"

echo "==> install $UNIT"
# shellcheck disable=SC2029
ssh -o BatchMode=yes "$HOST" "sudo -n install -m 0644 '$REMOTE_ROOT/deploy/$UNIT' /etc/systemd/system/$UNIT \
  && sudo -n systemctl daemon-reload"

if [ "$RESTART" = 1 ]; then
  echo "==> restart $UNIT"
  # shellcheck disable=SC2029
  ssh -o BatchMode=yes "$HOST" "sudo -n systemctl restart $UNIT && sleep 2 && systemctl is-active $UNIT"
  ssh -o BatchMode=yes "$HOST" "sudo -n journalctl -u $UNIT -n 30 --no-pager"
else
  echo "==> skipped restart (--no-restart)"
fi
