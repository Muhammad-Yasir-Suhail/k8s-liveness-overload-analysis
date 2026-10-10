#!/usr/bin/env bash
# 01_repro.sh -- Experiment R: reproduction of Roberts et al. (2025)
#
# One catalogue Pod on one node, 100 req/s, 0.5 s timeout, the catalogue
# process is killed 60 s after the load starts (liveness scenario: the
# wrapper shell logs a line and hangs). Arms: DP, FP, SKI.
#
# Measured per run: time to queue restart, time to start restart, total /
# failed / HTTP 500 requests, restarts, cumulative Ready-pod seconds,
# and CPU/memory of the catalogue pod (SKI overhead).
#
# Usage: bash experiments/01_repro.sh          (PROFILE from config.env)
cd "$(dirname "$0")/.."
source experiments/lib.sh
check_cluster

OUT="$RESULTS_DIR/R"
mkdir -p "$OUT"
kubectl -n g18-db rollout status deploy/catalogue-db --timeout=120s >/dev/null || die "catalogue-db not running (run 00_setup.sh)"

total=$(( $(wc -w <<<"$R_ARMS") * R_REPEATS )); n=0
for rep in $(seq 1 "$R_REPEATS"); do
  for arm in $R_ARMS; do
    n=$((n+1))
    id="R_${arm}_r${rep}"
    dir="$OUT/$id"
    if [[ -f "$dir/meta.json" ]]; then log "[$n/$total] $id exists, skipping"; continue; fi
    mkdir -p "$dir"
    ns="g18-r-$(echo "$arm" | tr A-Z a-z)-$rep"
    log "[$n/$total] $id  (namespace $ns)"
    delete_ns "$ns"

    export NS=$ns
    render "manifests/repro/catalogue-$(echo "$arm" | tr A-Z a-z).yaml" | kubectl apply -f - >/dev/null
    for _ in $(seq 1 180); do   # wait until the pod exists and runs
      phase=$(kubectl -n "$ns" get pod -l name=catalogue -o jsonpath='{.items[0].status.phase}' 2>/dev/null || true)
      [[ "$phase" == "Running" ]] && break
      sleep 1
    done
    [[ "$phase" == "Running" ]] || die "catalogue pod did not start (kubectl -n $ns describe pod)"
    pod=$(kubectl -n "$ns" get pod -l name=catalogue -o jsonpath='{.items[0].metadata.name}')

    # sanity: the shell exists in the catalogue image
    kubectl -n "$ns" exec "$pod" -c catalogue -- sh -c 'echo ok' >/dev/null || die "catalogue image has no /bin/sh"

    warm=$R_WARMUP_FAST_S; [[ $arm == DP ]] && warm=$R_WARMUP_DP_S
    log "    warm-up ${warm}s (liveness initial delay must pass)"
    sleep "$warm"
    kubectl -n "$ns" wait --for=condition=Ready pod -l name=catalogue --timeout=300s >/dev/null \
      || log "    WARNING: catalogue pod not Ready after warm-up"
    pod=$(kubectl -n "$ns" get pod -l name=catalogue -o jsonpath='{.items[0].metadata.name}')
    warm_restarts=$(kubectl -n "$ns" get pod "$pod" -o jsonpath='{.status.containerStatuses[?(@.name=="catalogue")].restartCount}')
    log "    restarts during warm-up: ${warm_restarts:-0}"

    start_sampler "$ns" name=catalogue catalogue "$dir/timeseries.csv"
    cg0=$(cgroup_cpu_mem "$ns" "$pod" catalogue)
    cs0="NA NA"; [[ $arm == SKI ]] && cs0=$(cgroup_cpu_mem "$ns" "$pod" ski)
    t_cg0=$(now_ms)

    export TARGET="http://catalogue/catalogue" MODE=closed RATE=0 USERS=$R_USERS PACE_MS=$R_PACE_MS \
           DURATION_S=$R_DURATION_S TIMEOUT_MS=$R_TIMEOUT_MS SEED=$rep
    start_loadgen "$ns" loadgen
    t0=$(wait_load_start "$ns" loadgen)
    sleep_until $(( t0 + R_INJECT_AT_S * 1000 ))
    t_inj=$(now_ms)
    kubectl -n "$ns" exec "$pod" -c catalogue -- sh -c 'kill $(pidof app) 2>/dev/null || pkill app || killall app' >/dev/null 2>&1 \
      || log "    WARNING: kill command returned non-zero"
    log "    failure injected at +$(( (t_inj - t0) / 1000 ))s"

    # CPU/memory before the failure disturbs the numbers (first 55 s of load)
    cg1=$(cgroup_cpu_mem "$ns" "$pod" catalogue)
    cs1="NA NA"; [[ $arm == SKI ]] && cs1=$(cgroup_cpu_mem "$ns" "$pod" ski)
    t_cg1=$(now_ms)

    wait_loadgen_done "$ns" loadgen "$R_DURATION_S"
    sleep 2
    stop_sampler
    kubectl -n "$ns" logs loadgen > "$dir/loadgen.log" 2>&1 || true
    [[ $arm == SKI ]] && kubectl -n "$ns" logs "$pod" -c ski > "$dir/ski.log" 2>&1 || true
    collect "$ns" "$dir"
    write_meta "$dir/meta.json" scenario=R arm="$arm" repeat="$rep" pod="$pod" \
      t0_ms="$t0" t_inject_ms="$t_inj" duration_s="$R_DURATION_S" warmup_restarts="${warm_restarts:-0}" \
      cg_cat0="$cg0" cg_cat1="$cg1" cg_ski0="$cs0" cg_ski1="$cs1" t_cg0_ms="$t_cg0" t_cg1_ms="$t_cg1"
    delete_ns "$ns"
    log "    done"
  done
done
log "Experiment R finished -> $OUT"
