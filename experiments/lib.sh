# lib.sh -- shared helpers for all experiment scripts (Group G18)
# Usage: source experiments/lib.sh   (from the repository root)

set -euo pipefail
source experiments/config.env

log()  { echo "[$(date +%H:%M:%S)] $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }
now_ms() { python3 -c 'import time; print(int(time.time()*1000))'; }

# render TEMPLATE  (variables come from the environment)
render() { python3 experiments/render.py "$1"; }

# liveness_block ARM  -> prints an indented livenessProbe block (or nothing)
#   NONE  no liveness probe (control arm)
#   DP    Roberts "default probes":  period 3, timeout 1, threshold 3
#   FP    Roberts "fast probes":     period 1, timeout 1, threshold 1   (BASELINE)
#   ISO   FP timing, isolated always-200 endpoint (/livez-naive)
#   A     FP timing, progress-based endpoint (/livez)            (COMPONENT A)
#   B,AB  no kubelet liveness: the governor decides              (COMPONENT B)
#   SWEEP uses PROBE_T / PROBE_F / PROBE_P from the environment
liveness_block() {
  local path period timeout thr
  case "$1" in
    NONE|B|AB) return 0 ;;
    DP)   path=/healthz;     period=3; timeout=1; thr=3 ;;
    FP)   path=/healthz;     period=1; timeout=1; thr=1 ;;
    ISO)  path=/livez-naive; period=1; timeout=1; thr=1 ;;
    A)    path=/livez;       period=1; timeout=1; thr=1 ;;
    SWEEP) path=/healthz;    period=${PROBE_P}; timeout=${PROBE_T}; thr=${PROBE_F} ;;
    *) die "unknown arm $1" ;;
  esac
  cat <<EOF
          livenessProbe:
            httpGet: { path: ${path}, port: 8080 }
            initialDelaySeconds: 5
            periodSeconds: ${period}
            timeoutSeconds: ${timeout}
            failureThreshold: ${thr}
EOF
}

# deploy_sut NS ARM   -> deploys dep + sut (+ governor for B/AB) and waits
deploy_sut() {
  local ns=$1 arm=$2
  export NS=$ns LIVENESS="$(liveness_block "$arm")"
  export REPLICAS WORKERS WORK_UNITS WINDOW_MS CPU
  render manifests/overload/sut.yaml | kubectl apply -f - >/dev/null
  if [[ $arm == B || $arm == AB ]]; then
    export PROBE_PATH=/healthz
    [[ $arm == AB ]] && PROBE_PATH=/livez
    export THRESHOLD=$GOV_THRESHOLD CORR_FRAC=$GOV_CORR_FRAC BUDGET_FRAC=$GOV_BUDGET_FRAC HOLD_S=$GOV_HOLD_S
    render manifests/overload/governor.yaml | kubectl apply -f - >/dev/null
  fi
  kubectl -n "$ns" rollout status deploy/dep --timeout=180s >/dev/null
  kubectl -n "$ns" rollout status deploy/sut --timeout=300s >/dev/null
  if [[ $arm == B || $arm == AB ]]; then
    kubectl -n "$ns" rollout status deploy/governor --timeout=120s >/dev/null
  fi
}

# start_sampler NS SELECTOR CONTAINER OUTFILE  -> sets SAMPLER_PID
start_sampler() {
  python3 experiments/sampler.py "$1" "$2" "$3" "$4" 1 &
  SAMPLER_PID=$!
}
stop_sampler() { kill "${SAMPLER_PID}" 2>/dev/null || true; wait "${SAMPLER_PID}" 2>/dev/null || true; }

# start_loadgen NS NAME  (uses TARGET MODE RATE USERS PACE_MS DURATION_S TIMEOUT_MS SEED)
start_loadgen() {
  export NS=$1 LG_NAME=$2
  export BURST_AT_S=${BURST_AT_S:--1} BURST_LEN_S=${BURST_LEN_S:-0} BURST_RATE=${BURST_RATE:-0}
  render manifests/loadgen.yaml | kubectl apply -f - >/dev/null
  kubectl -n "$1" wait --for=condition=Ready "pod/$2" --timeout=120s >/dev/null 2>&1 || true
}

# wait_load_start NS NAME -> prints START unix ms of the load
wait_load_start() {
  local t
  for _ in $(seq 1 240); do
    t=$(kubectl -n "$1" logs "$2" 2>/dev/null | awk '/^START /{print $2; exit}')
    [[ -n "$t" ]] && { echo "$t"; return 0; }
    sleep 0.5
  done
  die "load generator $2 never started"
}

# wait_loadgen_done NS NAME DURATION_S
wait_loadgen_done() {
  local ns=$1 name=$2 dur=$3 phase
  for _ in $(seq 1 $(( (dur + 180) * 2 ))); do
    phase=$(kubectl -n "$ns" get pod "$name" -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
    [[ "$phase" == "Succeeded" || "$phase" == "Failed" ]] && return 0
    sleep 0.5
  done
  log "WARNING: load generator did not finish in time"
}

# sleep_until UNIX_MS
sleep_until() {
  python3 - "$1" <<'EOF'
import sys, time
t = int(sys.argv[1]) / 1000.0
d = t - time.time()
if d > 0: time.sleep(d)
EOF
}

# cgroup_cpu_mem NS POD CONTAINER -> "cpu_usec mem_bytes" (cgroup v2, falls back to v1)
cgroup_cpu_mem() {
  kubectl -n "$1" exec "$2" -c "$3" -- sh -c '
    if [ -f /sys/fs/cgroup/cpu.stat ]; then
      c=$(awk "/^usage_usec/{print \$2}" /sys/fs/cgroup/cpu.stat); m=$(cat /sys/fs/cgroup/memory.current)
    else
      c=$(( $(cat /sys/fs/cgroup/cpuacct/cpuacct.usage) / 1000 )); m=$(cat /sys/fs/cgroup/memory/memory.usage_in_bytes)
    fi; echo "$c $m"' 2>/dev/null || echo "NA NA"
}

# collect NS DIR  -> saves everything needed for analysis
collect() {
  local ns=$1 dir=$2
  kubectl -n "$ns" get events -o json > "$dir/events.json" 2>/dev/null || echo '{}' > "$dir/events.json"
  kubectl -n "$ns" get pods -o json   > "$dir/pods.json"   2>/dev/null || echo '{}' > "$dir/pods.json"
  if kubectl -n "$ns" get deploy governor >/dev/null 2>&1; then
    kubectl -n "$ns" logs deploy/governor > "$dir/governor.log" 2>&1 || true
  fi
}

# delete_ns NS
delete_ns() { kubectl delete ns "$1" --wait=true --timeout=180s >/dev/null 2>&1 || true; }

# write_meta FILE key=value ...   (numbers stay numbers)
write_meta() {
  local f=$1; shift
  python3 - "$f" "$@" <<'EOF'
import json, sys
d = {}
for kv in sys.argv[2:]:
    k, v = kv.split("=", 1)
    try: v = int(v)
    except ValueError:
        try: v = float(v)
        except ValueError: pass
    d[k] = v
json.dump(d, open(sys.argv[1], "w"), indent=2)
EOF
}

# capacity -> per-replica capacity measured by 02_calibrate.sh
capacity() {
  local f="$RESULTS_DIR/calibration/CAPACITY"
  [[ -f $f ]] || die "run experiments/02_calibrate.sh first ($f missing)"
  cat "$f"
}

check_cluster() {
  kubectl cluster-info >/dev/null 2>&1 || die "no cluster. Run: bash experiments/00_setup.sh"
}

# run_sut_case SCENARIO ARM LOAD_PCT REP OUTDIR
#   SCENARIO: O (sustained load, no fault), P (load spike: base LOAD_PCT,
#             PEAK_PCT for P_LEN_S from INJECT_AT_S), H (hang one pod at INJECT_AT_S),
#             D (stall the dependency for D_STALL_S at INJECT_AT_S),
#             S (boundary sweep, uses PROBE_T/PROBE_F/PROBE_P)
run_sut_case() {
  local sc=$1 arm=$2 load=$3 rep=$4 out=$5
  local id="${sc}_${arm}_l${load}_r${rep}"
  [[ $sc == S ]] && id="S_t${PROBE_T}_f${PROBE_F}_p${PROBE_P}_l${load}_r${rep}"
  local dir="$out/$id"
  if [[ -f "$dir/meta.json" ]]; then log "    $id exists, skipping"; return 0; fi
  mkdir -p "$dir"
  local ns="g18-$(echo "$id" | tr 'A-Z_' 'a-z-')"
  local cap rate
  cap=$(capacity)
  rate=$(python3 -c "print(round($REPLICAS*$cap*$load/100.0, 1))")
  log "    $id  rate=${rate}/s  ns=$ns"
  delete_ns "$ns"
  deploy_sut "$ns" "$arm"
  sleep "$SETTLE_S"

  start_sampler "$ns" app=sut sut "$dir/timeseries.csv"
  export TARGET="http://sut/work" MODE=open RATE=$rate USERS=0 PACE_MS=0 \
         DURATION_S=$LOAD_DURATION_S TIMEOUT_MS=$CLIENT_TIMEOUT_MS SEED=$rep
  local peak_rate=0
  if [[ $sc == P ]]; then
    peak_rate=$(python3 -c "print(round($REPLICAS*$cap*$P_PEAK_PCT/100.0, 1))")
    export BURST_AT_S=$INJECT_AT_S BURST_LEN_S=$P_LEN_S BURST_RATE=$peak_rate
  else
    export BURST_AT_S=-1 BURST_LEN_S=0 BURST_RATE=0
  fi
  start_loadgen "$ns" loadgen
  local t0 t_inj=0 victim=""
  t0=$(wait_load_start "$ns" loadgen)

  if [[ $sc == P ]]; then
    t_inj=$(( t0 + INJECT_AT_S * 1000 ))
    log "    load spike to ${peak_rate}/s for ${P_LEN_S}s at +${INJECT_AT_S}s (base ${rate}/s)"
  elif [[ $sc == H ]]; then
    victim=$(kubectl -n "$ns" get pod -l app=sut -o jsonpath='{.items[0].metadata.name}')
    sleep_until $(( t0 + INJECT_AT_S * 1000 ))
    t_inj=$(now_ms)
    kubectl -n "$ns" exec "$victim" -c sut -- wget -q -T 3 -O- http://127.0.0.1:8080/admin/hang >/dev/null 2>&1 \
      || log "    WARNING: hang injection failed"
    log "    hang injected into $victim at +$(( (t_inj - t0)/1000 ))s"
  elif [[ $sc == D ]]; then
    sleep_until $(( t0 + INJECT_AT_S * 1000 ))
    t_inj=$(now_ms)
    kubectl -n "$ns" exec deploy/dep -- wget -q -T 3 -O- "http://127.0.0.1:8080/admin/stall?seconds=$D_STALL_S" >/dev/null 2>&1 \
      || log "    WARNING: stall injection failed"
    log "    dependency stalled for ${D_STALL_S}s at +$(( (t_inj - t0)/1000 ))s"
  fi

  wait_loadgen_done "$ns" loadgen "$LOAD_DURATION_S"
  sleep 2
  stop_sampler
  kubectl -n "$ns" logs loadgen > "$dir/loadgen.log" 2>&1 || true
  collect "$ns" "$dir"
  local gcpu="NA NA"
  if [[ $arm == B || $arm == AB ]]; then
    local gp; gp=$(kubectl -n "$ns" get pod -l app=governor -o jsonpath='{.items[0].metadata.name}')
    gcpu=$(cgroup_cpu_mem "$ns" "$gp" governor)
  fi
  write_meta "$dir/meta.json" scenario="$sc" arm="$arm" load_pct="$load" repeat="$rep" \
    replicas="$REPLICAS" capacity_per_replica="$cap" offered_rate="$rate" \
    t0_ms="$t0" t_inject_ms="$t_inj" victim="$victim" duration_s="$LOAD_DURATION_S" \
    inject_at_s="$INJECT_AT_S" stall_s="$D_STALL_S" window_ms="$WINDOW_MS" cpu="$CPU" \
    workers="$WORKERS" work_units="$WORK_UNITS" governor_cg="$gcpu" \
    probe_t="${PROBE_T:-}" probe_f="${PROBE_F:-}" probe_p="${PROBE_P:-}" \
    peak_pct="${P_PEAK_PCT}" peak_rate="$peak_rate" burst_len_s="${P_LEN_S}"
  delete_ns "$ns"
}
