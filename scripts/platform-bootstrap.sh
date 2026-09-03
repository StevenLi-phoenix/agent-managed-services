#!/usr/bin/env bash
# Bring Layer 0 (registry, auth) and the Caddy gateway up on racknerd as ams
# services, then re-point the two pilot Layer-1 services at the replica.
#
# Usage: scripts/platform-bootstrap.sh [--no-deploy] [--ref <branch>] [--no-layer1]
#   --no-deploy   skip scripts/deploy-racknerd.sh (code already on the box)
#   --ref BRANCH  branch to bring up (default: main)
#   --no-layer1   Layer 0 only: no pilot services re-pointed. Use on a FRESH host
#                 (nothing to stop, and kvservice/timeservice are pool members now,
#                 so the sync timer must be the one that declares them).
#
# This is a thin driver. Everything it does on the box is one call into
# `python -m ams.platform.layer0`, whose ordering constraints are documented in
# src/ams/platform/layer0.py. The script's own job is the two things that
# module cannot do for itself: get the code onto the host, and get the *source
# repository* onto the host.
#
# On the source: the upstream `StevenLi-phoenix/api` repository is PRIVATE, and
# the harness holds no GitHub credential (deliberately: a token in a fetch URL
# would reach a log line, and D16's store is for values services need, not for
# widening the harness's reach). `git ls-remote https://github.com/...` from
# racknerd prompts for a username and fails. So the source is delivered as a
# bare mirror pushed from this workstation to <store>/upstream/api.git, and
# `SourceMirror` is pointed at that local path -- a shape `validate_url`
# already accepts for exactly this reason. Everything downstream (clone
# --mirror, fetch, archive|tar, reflink stage) is the real code path.
#
# Phase A runs upstream `main`, which does NOT carry the SDK root-logger patch
# that lives on the local `ams-platform` branch (T1.4, never pushed). The
# consequence is recorded in .claude/state/platform-layer0.md: SDK
# `logger.warning` lines arrive without a level token and are classified INFO.
set -euo pipefail

HOST=${AMS_HOST:-racknerd}
LOCAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API_REPO=${AMS_API_REPO:-$LOCAL_ROOT/api}

STATE=/home/harness/store/state
STORE=/home/harness/store
UPSTREAM=$STORE/upstream/api.git
# Must match the AppArmor profile glob (DECISIONS D3): only this interpreter may
# create user namespaces, and every stage below needs run_admin.
REMOTE_PY=/home/harness/venv/bin/python3
REMOTE_ROOT=/home/harness/ams

DEPLOY=1
REF=main
LAYER1_FLAG=
while [ $# -gt 0 ]; do
  case "$1" in
    --no-deploy) DEPLOY=0 ;;
    --ref) REF=$2; shift ;;
    --no-layer1) LAYER1_FLAG=--no-layer1 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

ssh_() { ssh -o BatchMode=yes "$HOST" "$@"; }
# Every ams invocation on the box runs as harness, under the confined
# interpreter, with the live state and store.
as_harness() {
  # shellcheck disable=SC2029 - the command is built here on purpose
  ssh_ "su -l harness -c \"cd $REMOTE_ROOT && PYTHONPATH=src AMS_STATE_DIR=$STATE AMS_STORE_DIR=$STORE $REMOTE_PY $*\""
}

[ -d "$API_REPO/.git" ] || { echo "not a git checkout: $API_REPO" >&2; exit 1; }

# ------------------------------------------------------------------- deploy

if [ "$DEPLOY" = 1 ]; then
  echo "==> deploying the harness code"
  "$LOCAL_ROOT/scripts/deploy-racknerd.sh"
else
  echo "==> skipping deploy (--no-deploy)"
fi

# ------------------------------------------------------------------- source

echo "==> mirroring $API_REPO -> $HOST:$UPSTREAM (ref $REF)"
MIRROR=$(mktemp -d "${TMPDIR:-/tmp}/ams-api-mirror.XXXXXX")
trap 'rm -rf "$MIRROR"' EXIT
git clone --mirror --quiet "$API_REPO" "$MIRROR/api.git"
# `git archive` on the box reads this; nothing writes it.
ssh_ "mkdir -p '$STORE/upstream' && chown harness:harness '$STORE/upstream'"
rsync -az --delete "$MIRROR/api.git/" "$HOST:$UPSTREAM/"
ssh_ "chown -R harness:harness '$UPSTREAM'"
# shellcheck disable=SC2029
ssh_ "su -l harness -c 'git --git-dir $UPSTREAM rev-parse refs/heads/$REF'" \
  || { echo "no branch $REF in the pushed mirror" >&2; exit 1; }

# --------------------------------------------------------------- caddy binary

echo "==> checking the pinned Caddy binary"
if ! ssh_ "test -x '$STORE/bin/caddy'"; then
  # install-host.sh is a whole-host script with no per-section flag; running it
  # blind from here would re-do the venv, the loop file and the AppArmor
  # profile. Section 4c is the part that matters and it is idempotent.
  echo "    absent. Run on $HOST as root:  bash $REMOTE_ROOT/deploy/install-host.sh" >&2
  exit 1
fi
ssh_ "'$STORE/bin/caddy' version"

# ------------------------------------------------------------------ bring-up

echo "==> layer-0 bring-up (ref $REF)"
as_harness "-m ams.platform.layer0 --repo-url $UPSTREAM --ref $REF $LAYER1_FLAG -v"

# --------------------------------------------------------------- verification

echo
echo "==> ams ctl status"
as_harness "-m ams ctl status"

echo
echo "==> health through the loopback ports and through Caddy"
ssh_ "set -e
  echo -n 'registry /health      : '; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:20100/health
  echo -n 'auth   :20101 tcp     : '; (exec 3<>/dev/tcp/127.0.0.1/20101 && echo open) || echo closed
  echo -n 'caddy  /ams-health    : '; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:20180/ams-health
  echo -n 'caddy  /kv/health     : '; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:20180/kv/health
  echo -n 'caddy  /time/now      : '; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:20180/time/now
"

echo
echo "==> memory"
ssh_ "free -m"
