#!/usr/bin/env bash
# 06_sweep.sh -- OPTIONAL: the Cutoff-1 boundary sweep on the real cluster
# (timeoutSeconds x failureThreshold at 150% load, periodSeconds 5).
# The full 27-cell sweep is done in the simulator (sim/); this script
# checks a few cells on the testbed. ~1 h.
# Usage: bash experiments/06_sweep.sh
cd "$(dirname "$0")/.."
source experiments/lib.sh
check_cluster
OUT="$RESULTS_DIR/S"; mkdir -p "$OUT"
export PROBE_P=5
for load in 150; do
  for PROBE_T in 1 2 5; do
    for PROBE_F in 1 3 10; do
      export PROBE_T PROBE_F
      run_sut_case S SWEEP "$load" 1 "$OUT"
    done
  done
done
log "Sweep finished -> $OUT"
