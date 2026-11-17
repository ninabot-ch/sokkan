#!/bin/bash
# The kernel-compose battery: TWO SOKKAN instances on ONE docker daemon, each with the kernel
# engine switched on by scripts/kernel.sh, run from docker-compose.kernel.yml as shipped (plus a
# test override: prebuilt api image, explicit subnets, memory caps). Proves, on a real daemon:
#   K1  the kernel starts with the docker sandbox (proxy, no raw socket), its config rendered
#       by the api (instance label from the instance key, caps, flags)
#   K2  conformance C1-C9 of the kernel adapter against that kernel (tests/test_harness_kernel.py,
#       scripted model, sandboxes through the proxy, restart of the kernel included)
#   K3  an Atelier work through the api on a real model: cards approved, a deliverable saved
#   K4  the sandbox of that work: capped (memory, CPU), no network, one bind mount, labelled
#       with instance A only; the proxy refuses a privileged container
#   K5  instance B's kernel restarting (its start-up reaping) leaves A's sandbox alone
#   K6  DELETE of the work: no sandbox of A survives
#
#   KC_KERNEL_IMAGE   the kernel image (pinned)         KC_MODEL_URL / KC_MODEL  the real model
#   KC_HOME           work folder (default /srv/sokkan-kc)   KC_NET_A / KC_NET_B   /24 prefixes,
#   KC_SANDBOX_IMAGE  the sandboxes' image                    e.g. 10.99.1 and 10.99.2
#   KC_KEEP=1         leave everything up at the end
# Run from the sokkan source tree: bash tests/e2e_kernel_compose/run.sh
set -uo pipefail
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
: "${KC_KERNEL_IMAGE:?pinned kernel image}" "${KC_MODEL_URL:?model endpoint}" "${KC_MODEL:?model name}"
: "${KC_NET_A:?/24 prefix for instance A}" "${KC_NET_B:?/24 prefix for instance B}"
KC_HOME="${KC_HOME:-/srv/sokkan-kc}"
SBI="${KC_SANDBOX_IMAGE:-ghcr.io/nokker-ai/sandbox:0.1.0-rc.9.32}"
SHA="$(cd "$SRC" && git rev-parse --short HEAD 2>/dev/null || date +%s)"
API_IMAGE="kc-api:$SHA"
RES="$KC_HOME/results/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RES"
N=(nice -n 10)
PASS=0; FAIL=0
ok() { echo "PASS $*" | tee -a "$RES/summary.txt"; PASS=$((PASS+1)); }
ko() { echo "FAIL $*" | tee -a "$RES/summary.txt"; FAIL=$((FAIL+1)); }
check() { local name="$1"; shift; if "$@"; then ok "$name"; else ko "$name"; fi; }

# ---- the api image, once ----------------------------------------------------------------
if ! docker image inspect "$API_IMAGE" >/dev/null 2>&1; then
  echo "→ building $API_IMAGE"
  (cd "$SRC" && "${N[@]}" docker build -q -f docker/api.Dockerfile --build-arg SOKKAN_COMMIT="$SHA" -t "$API_IMAGE" .) \
    > "$RES/build.log" 2>&1 || { echo "build failed"; tail -20 "$RES/build.log"; exit 1; }
fi

inst() {   # inst <a|b> <net prefix> <model url> — a fresh instance folder, kernel switched on
  local x=$1 net=$2 url=$3 d="$KC_HOME/inst-$1"
  mkdir -p "$d"
  rsync -a --delete --exclude .git --exclude node_modules --exclude .next --exclude workspace \
    --exclude .env --exclude compose.kc.yml "$SRC/" "$d/"
  mkdir -p "$d/workspace/conf" && chown -R 1000:1000 "$d/workspace"
  if [ ! -f "$d/.env" ]; then
    umask 077
    cat > "$d/.env" <<EOF
COMPOSE_PROJECT_NAME=kc-$x
SOKKAN_LOCAL_TOKEN=$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')
SOKKAN_DB_PASSWORD=$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')
SOKKAN_FEATURE_DOCUMENTS=1
SOKKAN_FEATURE_SIMPLE_PROFILE=1
SOKKAN_KERNEL_SANDBOX_MEMORY=1g
SOKKAN_KERNEL_SANDBOX_CPUS=1
EOF
    umask 022
  fi
  model_conf "$d" "$url"
  cat > "$d/compose.kc.yml" <<EOF
# test override (tests/e2e_kernel_compose/run.sh): prebuilt api, explicit subnets, caps
services:
  api:
    image: $API_IMAGE
    pull_policy: never
    mem_limit: 2g
    volumes:
      - ./tests/e2e_kernel_compose:/opt/kc:ro
  db:
    mem_limit: 1g
networks:
  default:
    ipam: {config: [{subnet: $net.0/28}]}
  kernel-dockerapi:
    ipam: {config: [{subnet: $net.16/28}]}
  kernel-sandbox:
    ipam: {config: [{subnet: $net.32/28}]}
  kernel-egress:
    ipam: {config: [{subnet: $net.48/28}]}
  kernel-outbound:
    ipam: {config: [{subnet: $net.64/28}]}
EOF
  (cd "$d" && sh scripts/kernel.sh enable --image "$KC_KERNEL_IMAGE" --sandbox-image "$SBI") >> "$RES/enable-$x.log" 2>&1 \
    || { cat "$RES/enable-$x.log"; return 1; }
  # the test override on top of what kernel.sh wrote
  local cf; cf="$(sed -n 's/^COMPOSE_FILE=//p' "$d/.env" | tail -n 1)"
  case ":$cf:" in *:compose.kc.yml:*) ;; *) sed -i '/^COMPOSE_FILE=/d' "$d/.env"; echo "COMPOSE_FILE=$cf:compose.kc.yml" >> "$d/.env" ;; esac
}

model_conf() {   # model_conf <dir> <url> — the operator's file: the model tiers only
  sed -e "s#base_url: http://router:8096/v1#base_url: $2#" -e "s#model: ship#model: $KC_MODEL#" \
      -e "s#model: deep#model: $KC_MODEL#" "$1/deploy/kernel/config.example.yaml" > "$1/deploy/kernel/config.yaml"
}

dc() { local x=$1; shift; (cd "$KC_HOME/inst-$x" && "${N[@]}" docker compose "$@"); }
TOOLBOX="${KC_TOOLBOX_IMAGE:-ghcr.io/nokker-ai/sandboxd:0.1.0-rc.9.32}"   # any image with sed
label() { docker run --rm -v "kc-$1_kernel-config:/c:ro" "$TOOLBOX" sed -n 's/^  instance: //p' /c/config.yaml 2>/dev/null; }
sandboxes() { docker ps -aq --filter "label=nokker.instance=$1" | sort; }
wait_healthy() {   # wait_healthy <x> <service> <seconds>
  local t=0 cid; cid="$(dc "$1" ps -q "$2")"
  while [ $t -lt "$3" ]; do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null)" = healthy ] && return 0
    sleep 5; t=$((t+5))
  done
  return 1
}

# ---- K1: both instances up, conformance model for A ------------------------------------
inst a "$KC_NET_A" "http://kc-a-conf:18099/v1" || exit 1
inst b "$KC_NET_B" "$KC_MODEL_URL" || exit 1
for x in a b; do
  dc $x up -d api kernel > "$RES/up-$x.log" 2>&1 || { tail -30 "$RES/up-$x.log"; ko "K1 $x up"; }
done
for x in a b; do
  check "K1 kernel $x healthy" wait_healthy $x kernel 300
done
LA="$(label a)"; LB="$(label b)"
echo "labels: A=$LA B=$LB" | tee -a "$RES/summary.txt"
check "K1 labels set, distinct, from the key" bash -c "[[ '$LA' =~ ^sokkan-[0-9a-f]{16}$ && '$LB' =~ ^sokkan-[0-9a-f]{16}$ && '$LA' != '$LB' ]]"
KA="$(dc a ps -q kernel)"
check "K1 no docker socket in the kernel" bash -c "! docker inspect -f '{{range .Mounts}}{{.Source}} {{end}}' $KA | grep -q docker.sock"
check "K1 kernel read-only, no capability" bash -c "[ \"\$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}}' $KA)\" = 'true [ALL]' ]"
docker run --rm -v kc-a_kernel-config:/c:ro "$TOOLBOX" cat /c/config.yaml > "$RES/kernel-config-a.yaml"
check "K1 rendered: docker, require_sandbox, caps" bash -c "grep -q 'backend: docker' $RES/kernel-config-a.yaml && grep -q 'require_sandbox: true' $RES/kernel-config-a.yaml && grep -q 'memory: 1g' $RES/kernel-config-a.yaml && ! grep -q memory_direct $RES/kernel-config-a.yaml"
# the label survives a restart of the api (re-render): stable
dc a restart api >/dev/null 2>&1; wait_healthy a api 180 >/dev/null
check "K1 label stable across a re-render" test "$(label a)" = "$LA"

# ---- K2: conformance against A's kernel ---------------------------------------------------
WSA="$(sed -n 's/^SOKKAN_KERNEL_WORKSPACE_HOST=//p' "$KC_HOME/inst-a/.env")"
docker rm -f kc-a-conf >/dev/null 2>&1
"${N[@]}" docker run --name kc-a-conf --network kc-a_default --user 1000:1000 \
  --group-add "$(getent group docker | cut -d: -f3)" --memory 2g --cpus 2 \
  -v "$KC_HOME/inst-a:/src:ro" -v "$WSA:/workspace" -v kc-a_kernel-token:/run/kernel:ro \
  -v /usr/bin/docker:/usr/bin/docker:ro -v /var/run/docker.sock:/var/run/docker.sock \
  -w /src -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
  -e SOKKAN_KERNEL_TEST_URL=http://kernel:8742 -e SOKKAN_KERNEL_TEST_TOKEN_FILE=/run/kernel/local-token \
  -e SOKKAN_KERNEL_TEST_ROOT=/workspace/conf -e SOKKAN_KERNEL_TEST_UPSTREAM_PORT=18099 \
  -e SOKKAN_KERNEL_TEST_UPSTREAM_HOST=0.0.0.0 -e SOKKAN_KERNEL_SANDBOX=docker \
  -e SOKKAN_KERNEL_TEST_RESTART_CMD="docker restart $KA" \
  ssint-test:full python -m pytest -q -rs -p no:cacheprovider tests/test_harness_kernel.py \
  > "$RES/conformance.log" 2>&1
docker rm -f kc-a-conf >/dev/null 2>&1
tail -5 "$RES/conformance.log"
check "K2 conformance C1-C9 on the compose kernel" bash -c "grep -Eq '[0-9]+ passed' $RES/conformance.log && ! grep -Eq '[0-9]+ (failed|error)' $RES/conformance.log && ! grep -q skipped $RES/conformance.log"
wait_healthy a kernel 180 >/dev/null
check "K2 no sandbox of A left after the conformance" test -z "$(sandboxes "$LA")"

# ---- K3: an Atelier work on the real model -----------------------------------------------
model_conf "$KC_HOME/inst-a" "$KC_MODEL_URL"
dc a restart api >/dev/null 2>&1; wait_healthy a api 180 >/dev/null
dc a up -d --force-recreate kernel >/dev/null 2>&1; wait_healthy a kernel 240 >/dev/null
dc a exec -T api python3 /opt/kc/atelier.py start --timeout 480 > "$RES/atelier.json" 2> "$RES/atelier.err"
cat "$RES/atelier.json"
SID="$(python3 -c "import json,sys; print(json.load(open('$RES/atelier.json'))['sid'])" 2>/dev/null)"
check "K3 Atelier: a card approved and a deliverable saved" python3 -c "
import json; o = json.load(open('$RES/atelier.json'))
assert o['deliverables'] and o['cards'] >= 1, o
assert not [e for e in o['errors'] if e['code'] in ('sandbox_unavailable', 'engine_unavailable')], o"

# ---- K4: the work's sandbox ----------------------------------------------------------------
SBX="$(sandboxes "$LA" | head -n 1)"
echo "sandbox of A: ${SBX:-none}" | tee -a "$RES/summary.txt"
[ -n "$SBX" ] && docker inspect "$SBX" > "$RES/sandbox-a.json"
check "K4 a sandbox of A exists while the work is open" test -n "$SBX"
check "K4 sandbox capped 1g / 1 cpu, no network, no privilege" python3 -c "
import json; c = json.load(open('$RES/sandbox-a.json'))[0]; h = c['HostConfig']
assert h['Memory'] == 1 << 30 and h['NanoCpus'] == 10**9, (h['Memory'], h['NanoCpus'])
assert h['NetworkMode'] == 'none' and not h['Privileged'], h['NetworkMode']
binds = [m for m in c['Mounts'] if m['Type'] == 'bind']
assert len(binds) == 1 and binds[0]['Destination'] == '/workspace', c['Mounts']
assert not any('docker.sock' in m['Source'] for m in c['Mounts'])
assert c['Config']['Labels'].get('nokker.instance') == '$LA'"
dc a exec -T kernel python3 - < "$SRC/tests/e2e_kernel_compose/proxy_probe.py" > "$RES/proxy-probe.json" 2>&1
check "K4 the proxy refuses privileged, host root, docker socket, named volume" grep -q '"refused_all": true' "$RES/proxy-probe.json"

# ---- K5: B's kernel restarts (its reaping) — A's sandbox stays ----------------------------
dc b restart kernel >/dev/null 2>&1; wait_healthy b kernel 240 >/dev/null
check "K5 A's sandbox survives B's start-up reaping" bash -c "[ -n '$SBX' ] && docker inspect '$SBX' >/dev/null 2>&1"
check "K5 B lists none of A's sandboxes" test -z "$(sandboxes "$LB" | grep -x "${SBX:-none}")"

# ---- K6: DELETE of the work ----------------------------------------------------------------
if [ -n "$SID" ]; then
  dc a exec -T api python3 /opt/kc/atelier.py close "$SID" >> "$RES/atelier.json" 2>&1
fi
t=0; while [ -n "$(sandboxes "$LA")" ] && [ $t -lt 60 ]; do sleep 3; t=$((t+3)); done
check "K6 no sandbox of A after DELETE (${t}s)" test -z "$(sandboxes "$LA")"

for x in a b; do dc $x logs --no-color kernel kernel-dockerproxy api > "$RES/logs-$x.txt" 2>&1; done
echo "== $PASS passed, $FAIL failed — $RES" | tee -a "$RES/summary.txt"

# ---- cleanup ---------------------------------------------------------------------------------
if [ "${KC_KEEP:-0}" != 1 ]; then
  for x in a b; do
    for c in $(docker ps -aq --filter "label=nokker.instance=$(label $x)"); do docker rm -f "$c" >/dev/null; done
    dc $x --profile kernel down -v --remove-orphans > /dev/null 2>&1
  done
fi
[ "$FAIL" = 0 ]
