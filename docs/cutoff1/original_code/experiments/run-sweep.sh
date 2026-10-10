#!/usr/bin/env bash
# run-sweep.sh — Group G18
#
# The main experiment. 27 treatment configurations + 3 controls, each
# repeated REPEATS times. Runs unattended; expect ~26 hours at defaults.
#
# Usage:
#   ./experiments/run-sweep.sh            # full sweep
#   ./experiments/run-sweep.sh --pilot    # 1 repeat, short runs, smoke test
#
# Prerequisites: calibrate.sh has been run and SATURATION_RATE is set in
# experiments/config.env.

set -euo pipefail
cd "$(dirname "$0")/.."

source experiments/config.env

PILOT=0
[[ "${1:-}" == "--pilot" ]] && PILOT=1

TIMEOUTS=(1 2 5)
THRESHOLDS=(1 3 10)
LOAD_PCTS=(50 100 150)

if [[ $PILOT -eq 1 ]]; then
  REPEATS=1
  RUN_DURATION="3m"
  echo "### PILOT MODE: 1 repeat, ${RUN_DURATION} runs"
fi

STAMP=$(date +%Y%m%d-%H%M%S)
RESULTS="results/sweep-${STAMP}"
mkdir -p "$RESULTS"
echo "==> Results: $RESULTS"

# ---------------------------------------------------------------------
run_one () {
  local arm="$1" timeout="$2" threshold="$3" load_pct="$4" rep="$5"
  local rate=$(( SATURATION_RATE * load_pct / 100 ))
  local id="${arm}_t${timeout}_f${threshold}_l${load_pct}_r${rep}"
  local dir="${RESULTS}/${id}"
  mkdir -p "$dir"

  echo "--> ${id}  (rate=${rate}/s)"

  # Build manifest for this arm -------------------------------------
  if [[ "$arm" == "control" ]]; then
    PROBE=""
  else
    PROBE=$(cat <<EOF
          livenessProbe:
            httpGet: { path: /healthz, port: 8080 }
            periodSeconds: 5
            timeoutSeconds: ${timeout}
            failureThreshold: ${threshold}
EOF
)
  fi

  awk -v probe="$PROBE" '{ if ($0 == "__PROBE__") print probe; else print }' \
      manifests/deployment.template.yaml > "${dir}/deployment.yaml"

  # Fresh deployment each run so restart counters start at zero -------
  kubectl delete deploy sut --ignore-not-found --wait=true >/dev/null 2>&1
  sleep 5
  kubectl apply -f "${dir}/deployment.yaml" >/dev/null
  kubectl apply -f manifests/service.yaml   >/dev/null
  kubectl rollout status deploy/sut --timeout=180s >/dev/null

  # Let the cluster settle before measuring
  sleep 20

  # Port-forward ------------------------------------------------------
  kubectl port-forward svc/sut 8080:80 >/dev/null 2>&1 &
  local pf=$!
  sleep 3

  # Background metric sampler: 1 Hz snapshot of ready/restart state ----
  ( while true; do
      ts=$(date +%s)
      ready=$(kubectl get deploy sut -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
      restarts=$(kubectl get pods -l app=sut \
                 -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{"\n"}{end}' 2>/dev/null \
                 | awk '{s+=$1} END {print s+0}')
      echo "${ts},${ready:-0},${restarts:-0}" >> "${dir}/timeseries.csv"
      sleep 1
    done ) &
  local sampler=$!

  # Load ---------------------------------------------------------------
  RATE=$rate DURATION=$RUN_DURATION TARGET=http://localhost:8080 \
    OUT="${dir}/k6.json" \
    k6 run load/loadtest.js > "${dir}/k6.log" 2>&1 || true

  kill $sampler $pf 2>/dev/null || true

  # Post-run capture ----------------------------------------------------
  kubectl get events --field-selector reason=Unhealthy \
    -o json > "${dir}/events.json" 2>/dev/null || echo '{}' > "${dir}/events.json"
  kubectl get pods -l app=sut -o json > "${dir}/pods.json" 2>/dev/null || true

  # Metadata for the analysis stage
  cat > "${dir}/meta.json" <<EOF
{
  "id": "${id}",
  "arm": "${arm}",
  "timeoutSeconds": ${timeout},
  "failureThreshold": ${threshold},
  "load_pct": ${load_pct},
  "offered_rate": ${rate},
  "repeat": ${rep},
  "duration": "${RUN_DURATION}",
  "saturation_rate": ${SATURATION_RATE}
}
EOF
  echo "    done"
}

# ---------------------------------------------------------------------
TOTAL=$(( ${#TIMEOUTS[@]} * ${#THRESHOLDS[@]} * ${#LOAD_PCTS[@]} * REPEATS \
         + ${#LOAD_PCTS[@]} * REPEATS ))
echo "==> ${TOTAL} runs planned"
N=0

for rep in $(seq 1 $REPEATS); do
  # Control arm first each repeat, so drift in machine state affects
  # control and treatment roughly equally.
  for load in "${LOAD_PCTS[@]}"; do
    N=$((N+1)); echo "[${N}/${TOTAL}]"
    run_one control 0 0 "$load" "$rep"
  done

  for t in "${TIMEOUTS[@]}"; do
    for f in "${THRESHOLDS[@]}"; do
      for load in "${LOAD_PCTS[@]}"; do
        N=$((N+1)); echo "[${N}/${TOTAL}]"
        run_one treat "$t" "$f" "$load" "$rep"
      done
    done
  done
done

echo
echo "==> Sweep complete: ${RESULTS}"
echo "==> Next: python3 analysis/analyze.py ${RESULTS}"
