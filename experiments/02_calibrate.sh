#!/usr/bin/env bash
# 02_calibrate.sh -- Experiment C: measure the capacity of ONE replica and
# the load -> health-check latency coupling (Objective O2 of Cutoff 1).
#
# One SUT replica, NO liveness probe. Open-model load at increasing rates.
# For each rate we record goodput, p95 latency, and the latency of /healthz
# (shared pool) and /livez (progress-based) measured from inside the
# cluster. Capacity = the highest goodput reached.
#
# Usage: bash experiments/02_calibrate.sh
cd "$(dirname "$0")/.."
source experiments/lib.sh
check_cluster

OUT="$RESULTS_DIR/calibration"
mkdir -p "$OUT"
ns=g18-calib
delete_ns "$ns"
saved_rep=$REPLICAS; REPLICAS=1
deploy_sut "$ns" NONE
REPLICAS=$saved_rep
sleep "$SETTLE_S"
pod=$(kubectl -n "$ns" get pod -l app=sut -o jsonpath='{.items[0].metadata.name}')
ip=$(kubectl -n "$ns" get pod "$pod" -o jsonpath='{.status.podIP}')

# probe pod: measures /healthz (shared pool) and /livez (progress-based)
# latency once per second, from inside the cluster, during every step
kubectl -n "$ns" run prober --image="$TOOLS_IMAGE" --image-pull-policy=IfNotPresent --restart=Never \
  --env=MODE=probe --env="PROBE_URLS=http://$ip:8080/healthz,http://$ip:8080/livez" \
  --env=DURATION_S=100000 --env=TIMEOUT_MS=5000 --env="TARGET=http://$ip:8080/livez" \
  --command -- /loadgen >/dev/null
kubectl -n "$ns" wait --for=condition=Ready pod/prober --timeout=60s >/dev/null

echo "rate,t0_ms,duration_s" > "$OUT/steps.csv"
i=0
for rate in $C_RATES; do
  i=$((i+1))
  log "rate ${rate} req/s for ${C_STEP_S}s"
  export TARGET="http://sut/work" MODE=open RATE=$rate USERS=0 PACE_MS=0 \
         DURATION_S=$C_STEP_S TIMEOUT_MS=$CLIENT_TIMEOUT_MS SEED=$i
  start_loadgen "$ns" "lg-$i"
  t0=$(wait_load_start "$ns" "lg-$i")
  echo "$rate,$t0,$C_STEP_S" >> "$OUT/steps.csv"
  wait_loadgen_done "$ns" "lg-$i" "$C_STEP_S"
  kubectl -n "$ns" logs "lg-$i" > "$OUT/loadgen_rate${rate}.log" 2>&1 || true
  grep '^SUMMARY' "$OUT/loadgen_rate${rate}.log" | cut -c1-200 || true
  kubectl -n "$ns" delete pod "lg-$i" --wait=false >/dev/null 2>&1 || true
  sleep 10   # let the queue drain between steps
done
kubectl -n "$ns" logs prober > "$OUT/prober.log" 2>&1 || true

python3 - "$OUT" <<'EOF'
import json, sys, glob, os, re
out = sys.argv[1]
best = 0.0
rows = []
for f in glob.glob(os.path.join(out, "loadgen_rate*.log")):
    rate = float(re.search(r"rate(\d+)", f).group(1))
    for line in open(f):
        if line.startswith("SUMMARY "):
            s = json.loads(line[8:])
            rows.append((rate, s["goodput_rps"], s["success_rate"], s["p95_ms"]))
            best = max(best, s["goodput_rps"])
rows.sort()
print("rate  goodput  success  p95_ms")
for r in rows:
    print(f"{r[0]:5.0f} {r[1]:8.1f} {r[2]:8.3f} {r[3]:8.0f}")
open(os.path.join(out, "CAPACITY"), "w").write(f"{best:.1f}\n")
print(f"\nMeasured capacity of one replica: {best:.1f} req/s -> written to {out}/CAPACITY")
EOF
delete_ns "$ns"
log "Calibration finished. Next: bash experiments/03_overload.sh"
