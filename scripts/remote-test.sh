#!/usr/bin/env bash
# Run the Linux-marked tests on a remote test host: rsync this checkout to the
# login user's ~/ams-src/<subdir>, then run scripts/linux-test.sh there (which
# copies it to /home/harness/<subdir> and runs pytest as `harness` inside
# `systemd-run -p Delegate=yes`). The login user needs passwordless sudo; the
# host needs deploy/install-host.sh once (see scripts/linux-test.sh).
#
# Usage: AMS_HOST=<ssh host> scripts/remote-test.sh [subdir] [pytest args...]
#   Agents working in parallel pass distinct subdirs.
set -euo pipefail
HOST=${AMS_HOST:?set AMS_HOST to an ssh host alias}
SUBDIR=${1:-ams-test}; shift || true
LOCAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_SRC="ams-src/${SUBDIR}"

ssh -o BatchMode=yes "$HOST" "mkdir -p '$REMOTE_SRC'"
rsync -az --delete --exclude .git --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
  --exclude .ruff_cache "$LOCAL_ROOT/" "$HOST:$REMOTE_SRC/"
args=$(printf ' %q' "$@")
# shellcheck disable=SC2029
ssh -o BatchMode=yes "$HOST" "bash '$REMOTE_SRC/scripts/linux-test.sh' '$SUBDIR'$args"
