#!/usr/bin/env bash
# ams pool migration measurement. n samples, 60 s apart, fleet idle.
set -uo pipefail
CG=/sys/fs/cgroup/system.slice/ams-harness.service
LAYER0="svc-registry svc-auth svc-caddy svc-hello svc-pyhello"
N=${1:-3}
for i in $(seq 1 "$N"); do
  echo "== sample $i $(date -u +%H:%M:%S)"
  all=0; l1=0
  for c in "$CG"/svc-*; do
    b=$(basename "$c"); m=$(cat "$c/memory.current" 2>/dev/null || echo 0)
    all=$((all+m))
    case " $LAYER0 " in *" $b "*) ;; *) l1=$((l1+m));; esac
  done
  # one process per svc cgroup = the fleet's service processes
  fleet_procs=$(cat "$CG"/svc-*/cgroup.procs 2>/dev/null | wc -l)
  # python processes: interpreters running out of a service root, + the harness
  py=$(pgrep -f 'store/state/services/.*/bin/python' | wc -l)
  py=$((py + $(pgrep -fc 'venv/bin/python3 -m ams run' || echo 0)))
  free_avail=$(free -m | awk '/^Mem:/{print $7}')
  load=$(awk '{print $1}' /proc/loadavg)
  pool_mem=n/a; pool_pids=n/a; pool_threads=n/a
  if [ -d "$CG/svc-pool-core" ]; then
    pool_mem=$(( $(cat "$CG/svc-pool-core/memory.current") / 1048576 ))
    pool_pids=$(cat "$CG/svc-pool-core/pids.current")
    pp=$(head -1 "$CG/svc-pool-core/cgroup.procs" 2>/dev/null)
    [ -n "$pp" ] && pool_threads=$(ls /proc/"$pp"/task 2>/dev/null | wc -l)
  fi
  echo "python_processes=$py fleet_service_procs=$fleet_procs layer1_sum_MiB=$((l1/1048576)) all_services_MiB=$((all/1048576)) free_available_MiB=$free_avail load=$load"
  echo "pool_memory_current_MiB=$pool_mem pool_pids_current=$pool_pids pool_threads=$pool_threads"
  t=$(curl -s -o /dev/null -w '%{time_total}' -H 'Host: api.lishuyu.app' http://127.0.0.1:20180/time/health)
  code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: api.lishuyu.app' http://127.0.0.1:20180/time/health)
  echo "time_health_via_caddy_s=$t http=$code"
  [ "$i" -lt "$N" ] && sleep 60
done
