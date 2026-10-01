#!/usr/bin/env bash
# Run the Linux-marked tests ON THIS HOST, the only correct way: as the
# unprivileged `harness` user, inside a transient systemd unit with a delegated
# cgroup (Delegate=yes). An interactive shell's cgroup is not delegated, and the
# AppArmor userns grant covers only the harness's private interpreter.
#
# Prerequisite (once, as root):
#   AMS_STORE_FS=plain AMS_WITH_TOOLS=0 AMS_TEST_DEPS=1 AMS_INSTALL_UNIT=0 deploy/install-host.sh
#
# Usage (as root or a passwordless sudoer):
#   scripts/linux-test.sh [subdir] [pytest args...]
#   subdir defaults to "ams-test"; the repo is copied to /home/harness/<subdir>
#   and AMS_STATE_DIR is /home/harness/state/<subdir>. Parallel runs need
#   distinct subdirs. With no pytest args, runs `tests/linux` plus the
#   portable suite's isolation-adjacent modules.
# Used by .github/workflows/ci.yml and by scripts/remote-test.sh.
set -euo pipefail

SUBDIR=${1:-ams-test}; shift || true
case "$SUBDIR" in
  "" | */* | .*) echo "linux-test: bad subdir '$SUBDIR'" >&2; exit 2 ;;
esac
SRC_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HARNESS_USER=${HARNESS_USER:-harness}
HARNESS_HOME=/home/$HARNESS_USER
DEST="$HARNESS_HOME/$SUBDIR"
PY="$HARNESS_HOME/venv/bin/python3"   # path MUST match the AppArmor profile glob

SUDO=()
[ "$(id -u)" -eq 0 ] || SUDO=(sudo -n)

if ! id "$HARNESS_USER" >/dev/null 2>&1 || [ ! -x "$PY" ]; then
  echo "linux-test: no $HARNESS_USER user or $PY; run deploy/install-host.sh first (see header)" >&2
  exit 2
fi

echo "linux-test: $SRC_ROOT -> $DEST (kernel $(uname -r))"
"${SUDO[@]}" mkdir -p "$DEST"
"${SUDO[@]}" rsync -a --delete --exclude .git --exclude .venv --exclude __pycache__ \
  --exclude .pytest_cache --exclude .ruff_cache "$SRC_ROOT/" "$DEST/"
"${SUDO[@]}" chown -R "$HARNESS_USER:$HARNESS_USER" "$DEST"

[ $# -gt 0 ] || set -- tests/linux
"${SUDO[@]}" systemd-run --uid="$HARNESS_USER" --gid="$HARNESS_USER" -p Delegate=yes \
  --wait --pipe --collect -q \
  --working-directory="$DEST" \
  -E HOME="$HARNESS_HOME" -E AMS_STATE_DIR="$HARNESS_HOME/state/$SUBDIR" -E PYTHONPATH=src \
  -E AMS_STORE_DIR="$HARNESS_HOME/store" \
  -E PATH="$HARNESS_HOME/venv/bin:$HARNESS_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  -- "$PY" -m pytest -q -p no:cacheprovider -o log_cli=false -rs "$@"
