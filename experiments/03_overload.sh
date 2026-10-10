#!/usr/bin/env bash
# 03_overload.sh -- Experiment O: sustained overload, NO fault injected.
# Does the liveness mechanism manufacture failures (the research gap), and
# do Components A / B / A+B stop it?   Arms: see O_ARMS in config.env.
# Usage: bash experiments/03_overload.sh
cd "$(dirname "$0")/.."
source experiments/lib.sh
check_cluster
OUT="$RESULTS_DIR/O"; mkdir -p "$OUT"
log "Capacity per replica: $(capacity) req/s, replicas: $REPLICAS"
for rep in $(seq 1 "$O_REPEATS"); do
  for load in $O_LOADS; do
    for arm in $O_ARMS; do   # arms interleaved so machine drift hits all equally
      run_sut_case O "$arm" "$load" "$rep" "$OUT"
    done
  done
done
log "Experiment O finished -> $OUT"
