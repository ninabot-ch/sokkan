#!/usr/bin/env bash
# Real test of the chart + the kubernetes runner on a throw-away k3d cluster (k3s in Docker).
# Needs: docker, k3d, kubectl, helm; local images sokkan-api:local, sokkan-session:local
# (sokkan-web:local optional). Nothing is installed on the host; the cluster is deleted at the
# end (KEEP=1 keeps it). Usage: deploy/helm/sokkan/ci/k3d-test.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../../.." && pwd)"
CLUSTER="${CLUSTER:-sokkan-ci}"
NS=sokkan
export KUBECONFIG="${KUBECONFIG_OUT:-$(mktemp)}"
log() { echo "[k3d-test] $*" >&2; }
cleanup() {
  if [ "${KEEP:-0}" != 1 ]; then
    log "deleting cluster $CLUSTER"
    k3d cluster delete "$CLUSTER" >/dev/null 2>&1 || true
    docker network rm "k3d-$CLUSTER" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

log "cluster"
# explicit subnet: hosts with many compose projects exhaust Docker's default address pools
docker network create --subnet "${K3D_SUBNET:-10.251.0.0/24}" "k3d-$CLUSTER" >/dev/null
k3d cluster create "$CLUSTER" --network "k3d-$CLUSTER" --agents 0 --no-lb --wait --timeout 180s \
  --k3s-arg "--disable=traefik@server:0" \
  --k3s-arg "--kubelet-arg=eviction-hard=nodefs.available<1%,imagefs.available<1%@server:0" \
  --k3s-arg "--kubelet-arg=image-gc-high-threshold=100@server:0" \
  --k3s-arg "--kubelet-arg=image-gc-low-threshold=99@server:0" \
  --kubeconfig-update-default=false \
  --kubeconfig-switch-context=false >/dev/null
k3d kubeconfig get "$CLUSTER" > "$KUBECONFIG"
for img in sokkan-api:local sokkan-session:local sokkan-web:local; do
  docker image inspect "$img" >/dev/null 2>&1 && k3d image import -c "$CLUSTER" "$img" >/dev/null && log "imported $img"
done
kubectl create namespace $NS >/dev/null
kubectl -n $NS create configmap mock-anthropic --from-file="$ROOT/tests/mock_anthropic.py" >/dev/null
kubectl -n $NS apply -f "$HERE/mock-anthropic.yaml" >/dev/null

log "helm install"
helm install sokkan "$ROOT/deploy/helm/sokkan" -n $NS -f "$HERE/k3d-values.yaml" \
  --set ingress.enabled=false \
  --set-string api.env.ANTHROPIC_BASE_URL=http://mock-anthropic.$NS.svc:8080 \
  --set-string api.env.ANTHROPIC_API_KEY=sk-test \
  --set-string api.env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
  --set-string api.env.SOKKAN_SESSION_NO_PROXY=mock-anthropic.$NS.svc \
  ${HELM_EXTRA:-} --wait --timeout 10m
kubectl -n $NS get pods -o wide

log "api health"
API=$(kubectl -n $NS get pod -l app.kubernetes.io/component=api -o name | head -1)
kubectl -n $NS exec "$API" -- python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8097/api/health').read().decode())"

log "session phase 1: pod created, one turn, api process dies without closing"
kubectl -n $NS cp "$HERE/session-smoke.py" "${API#pod/}:/tmp/session-smoke.py"
kubectl -n $NS exec "$API" -- python /tmp/session-smoke.py start
kubectl -n $NS get pods -l app.kubernetes.io/component=session -o wide
SPOD=$(kubectl -n $NS get pod -l app.kubernetes.io/component=session -o name | head -1)
log "session pod security context"
kubectl -n $NS get "$SPOD" -o jsonpath='{.spec.securityContext}{"\n"}{.spec.containers[0].securityContext}{"\n"}{.spec.containers[0].resources}{"\n"}'
log "measure (cgroup of the session pod; metrics-server up to 90 s)"
kubectl -n $NS exec "${SPOD#pod/}" -- sh -c 'echo "memory.current=$(cat /sys/fs/cgroup/memory.current) memory.peak=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null)"' >&2 || true
for i in $(seq 1 18); do kubectl -n $NS top pod "${SPOD#pod/}" >&2 2>/dev/null && break; sleep 5; done || true
log "session phase 2: a new api process reattaches to the live pod"
kubectl -n $NS exec "$API" -- python /tmp/session-smoke.py resume
log "cleanup check"
for i in $(seq 1 30); do
  n=$(kubectl -n $NS get pods,secrets -l app.kubernetes.io/component=session --no-headers 2>/dev/null | wc -l)
  [ "$n" = 0 ] && break; sleep 2
done
kubectl -n $NS get pods,secrets -l app.kubernetes.io/component=session
[ "$n" = 0 ] && log "OK: session pod and Secret removed" || { log "FAIL: leftovers"; exit 1; }
