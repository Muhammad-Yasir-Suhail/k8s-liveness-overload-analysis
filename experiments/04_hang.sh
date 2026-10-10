#!/usr/bin/env bash
# 04_hang.sh -- Experiment H: ONE replica hangs silently (a real failure)
# at moderate load. A good liveness mechanism must still catch it fast.
# Usage: bash experiments/04_hang.sh
cd "$(dirname "$0")/.."
source experiments/lib.sh
check_cluster
OUT="$RESULTS_DIR/H"; mkdir -p "$OUT"
for rep in $(seq 1 "$H_REPEATS"); do
  for arm in $H_ARMS; do
    run_sut_case H "$arm" "$H_LOAD" "$rep" "$OUT"
  done
done
log "Experiment H finished -> $OUT"
