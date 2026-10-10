#!/usr/bin/env bash
# 03b_spike.sh -- Experiment P: a short load spike (70 % -> 150 % for 60 s
# -> back to 70 %). Does the system come back after the spike, or does the
# restart loop keep it down (a "metastable" failure)?
# Usage: bash experiments/03b_spike.sh
cd "$(dirname "$0")/.."
source experiments/lib.sh
check_cluster
OUT="$RESULTS_DIR/P"; mkdir -p "$OUT"
for rep in $(seq 1 "$P_REPEATS"); do
  for arm in $P_ARMS; do
    run_sut_case P "$arm" "$P_LOAD" "$rep" "$OUT"
  done
done
log "Experiment P finished -> $OUT"
