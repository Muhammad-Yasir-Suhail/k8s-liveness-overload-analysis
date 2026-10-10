#!/usr/bin/env bash
# 00_setup.sh -- create the kind cluster, build the image, load images,
# deploy the SockShop catalogue database. Safe to run again.
# Usage:  bash experiments/00_setup.sh
cd "$(dirname "$0")/.."
source experiments/lib.sh

log "Checking tools"
for t in docker kind kubectl python3; do
  command -v "$t" >/dev/null || die "'$t' not found. See docs/GUIDE.md step 2."
done
docker info >/dev/null 2>&1 || die "Docker is not running. Start Docker Desktop / dockerd."
log "  docker $(docker version --format '{{.Server.Version}}'), kind $(kind version | awk '{print $2}'), $(kubectl version --client 2>/dev/null | head -1)"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  log "Cluster '$CLUSTER_NAME' already exists -- reusing it"
else
  log "Creating cluster from $CLUSTER_CONFIG (first time takes 2-5 min)"
  kind create cluster --config "$CLUSTER_CONFIG" --wait 180s
fi
kubectl cluster-info --context "kind-$CLUSTER_NAME" >/dev/null
kubectl config use-context "kind-$CLUSTER_NAME" >/dev/null

log "Building $TOOLS_IMAGE (Go service, load generator, SKI sidecar, governor)"
docker build -q -t "$TOOLS_IMAGE" src/ >/dev/null

log "Pulling SockShop images"
docker pull -q "$CATALOGUE_IMAGE" >/dev/null
docker pull -q "$CATALOGUE_DB_IMAGE" >/dev/null

log "Loading images into the kind node (no registry needed afterwards)"
kind load docker-image --name "$CLUSTER_NAME" "$TOOLS_IMAGE" "$CATALOGUE_IMAGE" "$CATALOGUE_DB_IMAGE" >/dev/null

log "Deploying catalogue-db (namespace g18-db)"
kubectl apply -f manifests/repro/catalogue-db.yaml >/dev/null
kubectl -n g18-db rollout status deploy/catalogue-db --timeout=300s >/dev/null

log "Recording environment"
mkdir -p "$RESULTS_DIR"
{
  echo "date: $(date -Iseconds)"
  echo "host_os: $(uname -srmo)"
  echo "cpu: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | xargs)"
  echo "cpus: $(nproc)"
  echo "mem_total_kb: $(awk '/MemTotal/{print $2}' /proc/meminfo)"
  echo "docker: $(docker version --format '{{.Server.Version}}')"
  echo "kind: $(kind version)"
  echo "kubectl: $(kubectl version --client 2>/dev/null | head -1)"
  echo "server: $(kubectl version 2>/dev/null | grep -i server)"
  echo "python: $(python3 --version)"
} > "$RESULTS_DIR/environment.txt"
cat "$RESULTS_DIR/environment.txt"

log "Setup complete. Next: bash experiments/01_repro.sh"
