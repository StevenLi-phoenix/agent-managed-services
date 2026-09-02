#!/usr/bin/env bash
# Run two real services from the `api` monorepo as replicas under the live ams
# harness on racknerd. Pilot for .claude/state/pilot-api.md; NOT a deployer.
#
# Usage: scripts/pilot-api.sh [--skip-sync]
#   --skip-sync   reuse the monorepo copy already in each service root
#                 (skips the rsync + chown, which dominate the runtime)
#
# What it does, per service id (kvservice, timeservice):
#   1. copies the whole api monorepo to <service root>/repo
#   2. installs examples/api-pilot/<id>/service.toml as the declaration
#   3. creates <service root>/data (kvservice's SQLite lives outside the repo copy)
#   4. generates SVC_SECRET on the box and pipes it into `ams secret set` (only
#      if unset); the value never reaches this script's stdout or any log
#   5. `ams provision <id>`  -> `uv sync --frozen` in the project, venv service-owned
#   6. restarts ams-harness.service and verifies both services over HTTP
#
# Why the whole monorepo and not just the service directory: both projects
# declare `sdk = { path = "../../components/sdk", editable = true }`, which uv
# resolves relative to the project. The sibling tree has to be there.
#
# Why the harness is stopped for the middle: a service root is owned by the
# service's mapped uid once provisioned, so a re-run has to chown it back to
# harness to write into it, and provisioning rewrites the venv. Doing that under
# a running service would mutate it in place. `hello` and `pyhello` come back
# with the unit; their ports and uid blocks are persisted state.
#
# Idempotent: safe to re-run. The rsync deliberately does NOT use --delete
# (openrsync's interaction between --delete and --exclude is not something to
# bet a provisioned .venv on); a file deleted upstream therefore lingers in the
# replica. A real deployer would stage into a fresh directory and swap.
set -euo pipefail

HOST=${AMS_HOST:-racknerd}
LOCAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API_REPO=${AMS_API_REPO:-$LOCAL_ROOT/api}
UNIT=ams-harness.service

STATE=/home/harness/store/state
STORE=/home/harness/store
SERVICES=$STATE/services
# Must match the AppArmor profile glob (DECISIONS D3): only this interpreter
# may create user namespaces.
REMOTE_PY=/home/harness/venv/bin/python3

IDS=(kvservice timeservice)

SKIP_SYNC=0
[ "${1:-}" = "--skip-sync" ] && SKIP_SYNC=1

ssh_() { ssh -o BatchMode=yes "$HOST" "$@"; }

# --------------------------------------------------------------------- checks

for id in "${IDS[@]}"; do
  [ -f "$LOCAL_ROOT/examples/api-pilot/$id/service.toml" ] ||
    { echo "missing declaration for $id" >&2; exit 1; }
done
for d in services/kvservice apps/timeservice components/sdk; do
  [ -d "$API_REPO/$d" ] || { echo "not an api monorepo: $API_REPO ($d missing)" >&2; exit 1; }
done

echo "==> validating declarations locally"
LOCAL_PY="$LOCAL_ROOT/.venv/bin/python"
[ -x "$LOCAL_PY" ] || LOCAL_PY=python3
PYTHONPATH="$LOCAL_ROOT/src" "$LOCAL_PY" -m ams validate \
  "$LOCAL_ROOT"/examples/api-pilot/*/service.toml

# ------------------------------------------------------------------ install

echo "==> stopping $UNIT (hello/pyhello come back with it)"
ssh_ "systemctl stop $UNIT"

for id in "${IDS[@]}"; do
  root=$SERVICES/$id/root
  echo "==> $id: preparing $root"
  # Created by root, handed to harness: provisioning's closing chown can only
  # touch uids mapped into the admin namespace, and host uid 0 is not one.
  ssh_ "mkdir -p '$SERVICES/$id' '$root/repo' '$root/data'"

  if [ "$SKIP_SYNC" = 0 ]; then
    echo "==> $id: rsync $API_REPO -> $root/repo"
    rsync -rlptz \
      --exclude .git --exclude .venv --exclude node_modules \
      --exclude __pycache__ --exclude frontend \
      "$API_REPO/" "$HOST:$root/repo/"
  else
    echo "==> $id: skipping rsync (--skip-sync)"
  fi

  echo "==> $id: installing declaration"
  rsync -rlptz "$LOCAL_ROOT/examples/api-pilot/$id/service.toml" \
    "$HOST:$SERVICES/$id/service.toml"
  # 0751 is what ams.state/ams.cli produce for a service directory: traversable
  # by the service uid, not listable by its siblings. Root's umask 022 would
  # otherwise leave 0755 here and quietly widen the layout.
  ssh_ "chown -R harness:harness '$SERVICES/$id' && chmod 0751 '$SERVICES/$id'"
done

# ------------------------------------------------------------------ secrets

# SVC_SECRET is declared by name only (DECISIONS D16) and the value is generated
# here, on the box, per service. It goes straight from `openssl rand` into the
# store over a pipe: it is never an argument, never lands in a file other than
# <state>/secrets/<id>/SVC_SECRET (0600, harness), and is never echoed -- so it
# appears in neither this script's output nor the journal. Nobody, including
# whoever runs this script, ever sees it; that is the point of a write-only
# store, and rotating it is just re-running with `ams secret rm` first.
for id in "${IDS[@]}"; do
  echo "==> $id: SVC_SECRET in the store (value never printed)"
  # shellcheck disable=SC2087  # the heredoc is expanded locally on purpose
  ssh_ "bash -s" <<REMOTE
set -euo pipefail
AMS_ENV="PYTHONPATH=src AMS_STATE_DIR=$STATE"
if su -l harness -c "cd /home/harness/ams && \$AMS_ENV $REMOTE_PY -m ams secret check $id" \
     >/dev/null 2>&1; then
  echo "    already set; keeping the existing value"
else
  su -l harness -c "cd /home/harness/ams && openssl rand -hex 32 | \
\$AMS_ENV $REMOTE_PY -m ams secret set $id SVC_SECRET"
  echo "    generated and stored"
fi
REMOTE
done

# ---------------------------------------------------------------- provision

for id in "${IDS[@]}"; do
  echo "==> $id: ams provision (uv sync --frozen)"
  # A login shell as harness: provisioning forks an admin user namespace and
  # runs uv as inner root, which needs the AppArmor-profiled interpreter and
  # the harness's own /etc/subuid range.
  ssh_ "su -l harness -c \"cd /home/harness/ams && PYTHONPATH=src \
AMS_STATE_DIR=$STATE AMS_STORE_DIR=$STORE $REMOTE_PY -m ams provision $id\""
done

echo "==> starting $UNIT"
ssh_ "systemctl start $UNIT && sleep 3 && systemctl is-active $UNIT"

# ------------------------------------------------------------------- verify

echo
echo "===================== verification ====================="
# The journal below is the most useful output when verification fails, so a
# failing probe must not take the script down before it is printed.
set +e
ssh_ "bash -s" <<'REMOTE'
set -uo pipefail
STATE=/home/harness/store/state
CG=/sys/fs/cgroup/system.slice/ams-harness.service
port_of() {
  python3 -c "import json,sys;print(json.load(open('$STATE/state/ports.json'))['ports'][sys.argv[1]]['main'])" "$1"
}
uid_of() {
  python3 -c "import json,sys;print(json.load(open('$STATE/state/uidmap.json'))['blocks'][sys.argv[1]]['uid_start'])" "$1"
}

rc=0
for id in kvservice timeservice; do
  port=$(port_of "$id") || { echo "$id: no allocated port"; rc=1; continue; }
  uid=$(uid_of "$id")   || { echo "$id: no uid block"; rc=1; continue; }
  echo "--- $id  port=$port  uid=$uid"

  code=000
  for _ in $(seq 1 60); do
    code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:$port/health" || echo 000)
    [ "$code" = 200 ] && break
    sleep 1
  done
  echo "GET /health -> $code"
  [ "$code" = 200 ] || rc=1
  curl -s -m 3 "http://127.0.0.1:$port/health"; echo

  ps -o uid=,pid=,args= -u "$uid" || { echo "no process running as $uid"; rc=1; }

  for f in memory.max memory.swap.max pids.max memory.current; do
    printf '%s=%s\n' "$f" "$(cat "$CG/svc-$id/$f" 2>/dev/null || echo MISSING)"
  done

  # Secret delivery (D16). Every probe below prints names, modes and counts --
  # never a value; that is deliberate and must stay that way.
  echo -n "secret store names: "
  su -l harness -c "cd /home/harness/ams && PYTHONPATH=src AMS_STATE_DIR=$STATE \
/home/harness/venv/bin/python3 -m ams secret list $id" | tr '\n' ' '; echo
  stat -c 'store file: %n mode=%a owner=%U' "$STATE/secrets/$id/SVC_SECRET" || rc=1
  echo "declaration: secrets list = $(grep -c '^secrets = ' "$STATE/services/$id/service.toml"), \
[env] assignments = $(grep -c '^SVC_SECRET' "$STATE/services/$id/service.toml")"
  pid=$(ps -o pid= -u "$uid" | head -1 | tr -d ' ')
  if [ -n "$pid" ]; then
    # cut at the first '=' so only the NAME can ever reach this output.
    if tr '\0' '\n' < "/proc/$pid/environ" | cut -d= -f1 | grep -qx SVC_SECRET; then
      echo "process environ (pid $pid): SVC_SECRET= present"
    else
      echo "process environ (pid $pid): SVC_SECRET MISSING"; rc=1
    fi
  else
    echo "no pid for uid $uid; cannot inspect environ"; rc=1
  fi
done

tp=$(port_of timeservice)
echo "--- timeservice GET /now"
curl -s -m 3 "http://127.0.0.1:$tp/now"; echo

kp=$(port_of kvservice)
echo "--- kvservice anonymous surface"
echo -n "GET /  -> "; curl -s -o /dev/null -m 3 -w '%{http_code}\n' "http://127.0.0.1:$kp/"
echo -n "PUT /pilot -> "; curl -s -o /dev/null -m 3 -w '%{http_code}\n' \
  -X PUT -H 'content-type: application/json' -d '{"value":"hi"}' "http://127.0.0.1:$kp/pilot"
echo "--- kvservice sqlite file"
ls -ln "$STATE/services/kvservice/root/data/" || rc=1

echo "--- all supervised services"
python3 -c "import json;d=json.load(open('$STATE/state/ports.json'))['ports'];print(sorted(d))"
exit $rc
REMOTE
verify_rc=$?
set -e

echo
echo "==> journal (last 60 lines)"
ssh_ "journalctl -u $UNIT -n 60 --no-pager -o cat"

exit $verify_rc
