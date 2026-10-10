#!/usr/bin/env bash
# 05_stall.sh -- Experiment D: the shared dependency stalls for D_STALL_S
# seconds. EVERY replica stops making progress at the same time. Restarting
# them cannot help (the cause is outside the pods). This is where the
# cluster-aware governor (Component B) matters.
# Usage: bash experiments/05_stall.sh
cd "$(dirname "$0")/.."
source experiments/lib.sh
check_cluster
OUT="$RESULTS_DIR/D"; mkdir -p "$OUT"
for rep in $(seq 1 "$D_REPEATS"); do
  for arm in $D_ARMS; do
    run_sut_case D "$arm" "$D_LOAD" "$rep" "$OUT"
  done
done
log "Experiment D finished -> $OUT"
