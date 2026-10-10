#!/usr/bin/env bash
# calibrate.sh — Group G18
#
# RUN THIS BEFORE THE SWEEP. Everything downstream depends on it.
#
# Purpose: find the request rate at which ONE replica saturates. The three
# load levels in the main experiment (50% / 100% / 150%) are defined
# relative to this number. Reporting a MEASURED saturation point rather
# than an assumed one is what makes the single-laptop setup defensible.
#
# Method: deploy 1 replica with NO liveness probe, ramp the offered rate,
# and find the knee — the rate beyond which P95 latency climbs sharply
# while throughput stops climbing.

set -euo pipefail
cd "$(dirname "$0")/.."

RESULTS="results/calibration"
mkdir -p "$RESULTS"

echo "==> Deploying single replica, no liveness probe"
sed 's/__PROBE__//' manifests/deployment.template.yaml \
  | sed 's/replicas: 6/replicas: 1/' \
  | kubectl apply -f -
kubectl apply -f manifests/service.yaml
kubectl rollout status deploy/sut --timeout=120s

echo "==> Port-forwarding"
kubectl port-forward svc/sut 8080:80 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
sleep 3

for RATE in 10 20 30 40 50 60 80 100; do
  echo "==> Probing at ${RATE} req/s"
  RATE=$RATE DURATION=60s TARGET=http://localhost:8080 \
    OUT="${RESULTS}/rate-${RATE}.json" \
    k6 run load/loadtest.js >/dev/null 2>&1 || true

  P95=$(python3 -c "
import json,sys
d=json.load(open('${RESULTS}/rate-${RATE}.json'))
m=d['metrics']['sut_latency']['values']
print(round(m.get('p(95)',0),1))
" 2>/dev/null || echo "n/a")
  echo "    rate=${RATE}  p95=${P95}ms"
done

echo
echo "==> Done. Inspect the table above."
echo "    The knee is where p95 starts climbing steeply while rate keeps rising."
echo "    Record that rate in docs/CALIBRATION.md as SATURATION_RATE,"
echo "    then set it in experiments/config.env before running the sweep."
