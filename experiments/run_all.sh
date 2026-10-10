#!/usr/bin/env bash
# run_all.sh -- everything, in order, then the analysis.
#   PROFILE=smoke bash experiments/run_all.sh   (~35 min, check that it works)
#   PROFILE=quick bash experiments/run_all.sh   (~4 h, for the report)
#   PROFILE=full  bash experiments/run_all.sh   (~11 h, overnight)
# Every run is skipped if its result already exists, so you can stop
# (Ctrl+C) and start again later without losing finished runs.
cd "$(dirname "$0")/.."
set -euo pipefail
source experiments/config.env
echo "PROFILE=$PROFILE"
bash experiments/00_setup.sh
bash experiments/01_repro.sh
[[ -f $RESULTS_DIR/calibration/CAPACITY ]] || bash experiments/02_calibrate.sh
bash experiments/03_overload.sh
bash experiments/03b_spike.sh
bash experiments/04_hang.sh
bash experiments/05_stall.sh
python3 analysis/analyze_testbed.py "$RESULTS_DIR"
echo "All done. Tables/figures are in report/generated/. Rebuild the PDF (docs/GUIDE.md step 7)."
