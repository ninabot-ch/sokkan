#!/usr/bin/env bash
# Real-cluster test of the kubernetes runner and the chart's RBAC / NetworkPolicies / Pod
# Security, sized for a host WITHOUT room for the real images (~1 GB of disk): k3s in Docker,
# the namespace ENFORCES the `restricted` Pod Security Standard, a driver pod plays the api
# (chart ServiceAccount + Role), the session pod runs the real supervisor with a scripted CLI.
# The full test with the real images is k3d-test.sh. Cluster deleted at the end (KEEP=1 keeps).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../../.." && pwd)"
CLUSTER="${CLUSTER:-sokkan-smoke}"
NS=sokkan
REL=rel
export KUBECONFIG="${KUBECONFIG_OUT:-$(mktemp)}"
log() { echo "[runner-smoke] $*" >&2; }
cleanup() {
  if [ "${KEEP:-0}" != 1 ]; then
    log "deleting cluster $CLUSTER"
    k3d cluster delete "$CLUSTER" >/dev/null 2>&1 || true
    docker network rm "k3d-$CLUSTER" >/dev/null 2>&1 || true
    docker rmi sokkan-session-mini:test >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
k() { kubectl -n $NS "$@"; }

docker build -q -f "$HERE/mini-session.Dockerfile" -t sokkan-session-mini:test "$ROOT" >/dev/null
docker network create --subnet "${K3D_SUBNET:-10.251.0.0/24}" "k3d-$CLUSTER" >/dev/null
k3d cluster create "$CLUSTER" --network "k3d-$CLUSTER" --agents 0 --no-lb --wait --timeout 180s \
  --k3s-arg "--disable=traefik@server:0" \
  --k3s-arg "--kubelet-arg=eviction-hard=nodefs.available<1%,imagefs.available<1%@server:0" \
  --k3s-arg "--kubelet-arg=image-gc-high-threshold=100@server:0" \
  --k3s-arg "--kubelet-arg=image-gc-low-threshold=99@server:0" \
  --kubeconfig-update-default=false \
  --kubeconfig-switch-context=false >/dev/null
k3d kubeconfig get "$CLUSTER" > "$KUBECONFIG"
k3d image import -c "$CLUSTER" sokkan-session-mini:test >/dev/null
kubectl create namespace $NS >/dev/null
kubectl label namespace $NS pod-security.kubernetes.io/enforce=restricted \
  pod-security.kubernetes.io/enforce-version=latest >/dev/null
log "namespace $NS enforces Pod Security 'restricted'"

# the chart's RBAC, NetworkPolicies and egress gateway, rendered for this release
helm template $REL "$ROOT/deploy/helm/sokkan" -n $NS \
  --set database.url=postgresql://unused --set runner.image.repository=sokkan-session-mini \
  --set runner.image.tag=test --set runner.image.pullPolicy=Never \
  -s templates/rbac.yaml -s templates/networkpolicy.yaml -s templates/egress.yaml \
  -s templates/secrets.yaml | k apply -f - >/dev/null
k apply -f - >/dev/null <<YAML
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: $REL-sokkan-data}
spec:
  accessModes: [ReadWriteOnce]
  resources: {requests: {storage: 100Mi}}
---
apiVersion: v1
kind: Pod
metadata:
  name: fake-api
  labels: {app.kubernetes.io/name: sokkan, app.kubernetes.io/instance: $REL, app.kubernetes.io/component: api}
spec:
  serviceAccountName: $REL-sokkan-api
  restartPolicy: Never
  automountServiceAccountToken: true
  securityContext: {runAsNonRoot: true, runAsUser: 1000, fsGroup: 1000, seccompProfile: {type: RuntimeDefault}}
  containers:
    - name: api
      image: sokkan-session-mini:test
      imagePullPolicy: Never
      command: ["python3", "/opt/sokkan/driver.py"]
      env:
        - {name: SOKKAN_RUNNER_SECRET, value: smoke-key}
        - {name: SOKKAN_RUNNER_INSTANCE, value: $REL}
        - {name: SOKKAN_SESSION_IMAGE, value: sokkan-session-mini:test}
        - {name: SOKKAN_SESSION_IMAGE_PULL_POLICY, value: Never}
        - {name: SOKKAN_K8S_SESSION_SERVICE_ACCOUNT, value: $REL-sokkan-session}
        - {name: SOKKAN_RUNNER_MOUNTS, value: /data=pvc:$REL-sokkan-data}
        - {name: SOKKAN_RUNNER_RELAY_ADDR, value: "$REL-sokkan-api-relay.$NS.svc:8098"}
        - {name: SOKKAN_SESSION_HTTPS_PROXY, value: "http://$REL-sokkan-egress.$NS.svc:3128"}
        - {name: SOKKAN_K8S_RUN_AS_USER, value: "1000"}
        - {name: SOKKAN_K8S_FS_GROUP, value: "1000"}
        - {name: SOKKAN_K8S_SESSION_AFFINITY, value: api}
        - {name: SOKKAN_K8S_API_POD_LABELS, value: '{"app.kubernetes.io/instance":"$REL","app.kubernetes.io/component":"api"}'}
        - {name: SOKKAN_K8S_SESSION_LABELS, value: '{"app.kubernetes.io/instance":"$REL"}'}
        - {name: SOKKAN_SESSION_MEMORY_LIMIT, value: 256Mi}
        - {name: SOKKAN_SESSION_CPU_LIMIT, value: 500m}
        - {name: CLAUDE_CONFIG_DIR, value: /data/claude}
        - {name: SOKKAN_DATA_DIR, value: /data}
      resources: {requests: {cpu: 50m, memory: 64Mi}, limits: {cpu: 500m, memory: 256Mi}}
      securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}
      volumeMounts: [{name: data, mountPath: /data}]
  volumes: [{name: data, persistentVolumeClaim: {claimName: $REL-sokkan-data}}]
---
apiVersion: v1
kind: Service
metadata: {name: $REL-sokkan-api-relay}
spec:
  selector: {app.kubernetes.io/instance: $REL, app.kubernetes.io/component: api}
  ports: [{port: 8098, targetPort: 8098}]
YAML
k wait --for=condition=Ready pod/fake-api --timeout=180s >/dev/null || {
  k describe pod fake-api | tail -15; k logs fake-api || true; kubectl describe nodes | grep -A6 Conditions; exit 1; }
k rollout status deploy/$REL-sokkan-egress --timeout=180s >/dev/null
for i in $(seq 1 90); do k logs fake-api 2>/dev/null | grep -q '"api_gone"' && break; sleep 2; done
k logs fake-api | grep '"step"'
SPOD=$(k get pod -l app.kubernetes.io/component=session -o name | head -1)
log "session pod: $SPOD"
k get "$SPOD" -o jsonpath='{.spec.securityContext}{"\n"}{.spec.containers[0].securityContext}{"\n"}{.spec.containers[0].resources}{"\n"}'
log "a NON-restricted pod is refused by the namespace (control):"
k run rootpod --image=sokkan-session-mini:test --image-pull-policy=Never --restart=Never \
  --overrides='{"spec":{"containers":[{"name":"x","image":"sokkan-session-mini:test","securityContext":{"privileged":true}}]}}' 2>&1 | head -2 || true
log "MCP relay from the session pod (identity set by the api):"
echo ping | k exec -i "${SPOD#pod/}" -- sokkan-mcp-relay echo
log "direct egress from the session pod must fail:"
k exec "${SPOD#pod/}" -- python3 -c "import socket;socket.setdefaulttimeout(5);socket.create_connection(('1.1.1.1',443));print('EGRESS OPEN')" 2>&1 | tail -1 || true
log "egress gateway refuses a host outside the allowlist:"
k exec "${SPOD#pod/}" -- python3 -c "
import socket;s=socket.create_connection(('$REL-sokkan-egress',3128),timeout=5)
s.sendall(b'CONNECT example.com:443 HTTP/1.1\r\n\r\n');print(s.recv(64).split(b'\r\n')[0].decode())"
log "session pod cannot reach the K8s API (no token, policy):"
k exec "${SPOD#pod/}" -- sh -c 'ls /var/run/secrets/kubernetes.io/serviceaccount 2>&1 | head -1'
for i in $(seq 1 12); do k top pod "${SPOD#pod/}" 2>/dev/null && break; sleep 5; done || true
k exec fake-api -- touch /tmp/continue
for i in $(seq 1 60); do k logs fake-api 2>/dev/null | grep -q '"done"' && break; sleep 2; done
k logs fake-api | grep '"step"' | tail -3
LEFT=$(k get pods,secrets -l app.kubernetes.io/component=session --no-headers 2>/dev/null | wc -l)
log "session objects left: $LEFT"
[ "$LEFT" = 0 ]
log "OK"
