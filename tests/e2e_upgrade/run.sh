#!/bin/sh
# Self-hosted upgrade bench: real installs of past SOKKAN releases, updated to this tree
# the two documented ways, then rolled back. Each case runs on a fresh "machine": a
# Docker-in-Docker container (privileged, default bridge network, memory-capped) with
# its own Docker engine, so the user's compose file runs unmodified (its own network,
# volumes, builds) without touching the host's.
#
#   tests/e2e_upgrade/run.sh <case>…          e.g. 2.3.0-installer 2.3.0-manual
#   tests/e2e_upgrade/run.sh all
#
# Cases: <version>-installer | <version>-manual for 2.3.0 2.2.0 2.0.1 0.1.0, plus
#   old-compose   2.3.0 -> this tree with Docker Engine 20.10 + Compose 2.12.2 (installer)
#   vm4g          2.3.0 -> this tree (installer) in 4 GB without swap: RAM peak measured
#
#   installer  the installer is run again (install.sh of the 3.0 branch, tarball served
#              locally, unattended: SOKKAN_ACCEPT_GEMMA_TERMS=1 → EmbeddingGemma)
#   manual     docs/UPGRADE.md: tarball over the folder + `docker compose up -d --build`,
#              WITHOUT scripts/memory-setup.sh (→ the MIT model, Gemma terms undecided)
#
# The source versions are `git archive` of their tags, as published on sokkan.ch/dist,
# installed with the install.sh of their time: INSTALLER_REPO = a checkout of the
# repository that holds site/site/install.sh (its history gives the installer of each
# date, its working copy the 3.0 one); without it, https://sokkan.ch/install.sh is used.
# Results: $OUT/<case>/ (raw) and tests/e2e_upgrade/results/<case>.json (summary).
# KEEP_CACHE=1 keeps the engines' image/build cache volumes between invocations.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
P=sokkan-v3-upg
OUT="${OUT:-$(mktemp -d)}"
DISTS="$OUT/dists"
SOURCES="2.3.0 2.2.0 2.0.1 0.1.0"
mkdir -p "$DISTS" "$HERE/results"
say() { printf '\n######## %s\n' "$*"; }

[ "${1:-}" = all ] && set -- $(for v in $SOURCES; do echo "$v-installer $v-manual"; done) old-compose vm4g
[ $# -gt 0 ] || { sed -n '2,30p' "$0"; exit 2; }

# ---- distribution: the releases and this tree (uncommitted changes included)
for v in $SOURCES; do
  [ -f "$DISTS/sokkan-$v.tar.gz" ] || git -C "$ROOT" archive --prefix=sokkan/ -o "$DISTS/sokkan-$v.tar.gz" "v$v"
  if [ -n "${INSTALLER_REPO:-}" ]; then
    d="$(git -C "$ROOT" log -1 --format=%cI "v$v")"
    c="$(git -C "$INSTALLER_REPO" rev-list -1 --before="$d" HEAD -- site/site/install.sh)"
    [ -n "$c" ] || c="$(git -C "$INSTALLER_REPO" rev-list --reverse HEAD -- site/site/install.sh | head -n 1)"
    git -C "$INSTALLER_REPO" show "$c:site/site/install.sh" > "$DISTS/install-$v.sh"
    echo "installer for $v: $c"
  else
    curl -fsSL https://sokkan.ch/install.sh > "$DISTS/install-$v.sh"
  fi
done
ref="$(git -C "$ROOT" stash create 2>/dev/null || true)"
git -C "$ROOT" archive --prefix=sokkan/ -o "$DISTS/sokkan-3.tar.gz" "${ref:-HEAD}"
echo "3.0 tree: ${ref:-HEAD} ($(git -C "$ROOT" rev-parse --short HEAD))" > "$DISTS/tree.txt"
if [ -n "${INSTALLER_REPO:-}" ]; then cp "$INSTALLER_REPO/site/site/install.sh" "$DISTS/install-3.sh"
else curl -fsSL https://sokkan.ch/install.sh > "$DISTS/install-3.sh"; fi

host_image() {  # host_image <dind tag> <compose version or ''>
  img="$P-hostimg:$1${2:+-c$2}"
  docker image inspect "$img" >/dev/null 2>&1 && { echo "$img"; return; }
  b="$(mktemp -d)"
  if [ -n "$2" ]; then
    curl -fsSL -o "$b/docker-compose" \
      "https://github.com/docker/compose/releases/download/v$2/docker-compose-linux-x86_64"
    chmod +x "$b/docker-compose"
  else : > "$b/docker-compose"; fi
  cat > "$b/Dockerfile" <<DF
FROM docker:$1-dind
RUN apk add --no-cache python3 curl
COPY docker-compose /tmp/docker-compose
RUN if [ -s /tmp/docker-compose ]; then for d in /usr/local/libexec/docker/cli-plugins /usr/libexec/docker/cli-plugins /usr/local/lib/docker/cli-plugins; do mkdir -p \$d && cp /tmp/docker-compose \$d/docker-compose; done; fi; rm /tmp/docker-compose
DF
  docker build -q -t "$img" "$b" >/dev/null
  rm -rf "$b"
  echo "$img"
}

preload() {  # images the stacks need, from the host cache when present (saves the pulls)
  for i in pgvector/pgvector:pg16 python:3.12-slim node:22-alpine \
           ghcr.io/ggml-org/llama.cpp:server-b11347 caddy:2-alpine; do
    docker exec "$P-host" docker image inspect "$i" >/dev/null 2>&1 && continue
    if docker image inspect "$i" >/dev/null 2>&1; then
      docker save "$i" | docker exec -i "$P-host" docker load -q
    fi
  done
}

sampler() {  # memory of the whole test machine (cgroup of the dind container), 1/s
  cg="/sys/fs/cgroup/system.slice/docker-$1.scope"
  while [ -d "$cg" ]; do
    printf '%s %s %s %s %s\n' "$(date +%s.%N)" "$(cat "$cg/memory.current")" \
      "$(sed -n 's/^inactive_file //p' "$cg/memory.stat")" "$(sed -n 's/^anon //p' "$cg/memory.stat")" \
      "$(sed -n 's/^oom_kill //p' "$cg/memory.events")"
    sleep 1
  done
}

cleanup() {
  docker rm -f "$P-host" >/dev/null 2>&1 || true
}
trap cleanup EXIT

for case in "$@"; do
  TAG=29; CV=""; MEM=4g; SWAP=6g; MODE=installer; ACCEPT=1
  case "$case" in
    old-compose) SRC=2.3.0; TAG=20.10; CV=2.12.2 ;;
    vm4g) SRC=2.3.0; SWAP=4g ;;
    *-installer) SRC="${case%-installer}" ;;
    *-manual) SRC="${case%-manual}"; MODE=manual; ACCEPT="" ;;
    *) echo "unknown case $case"; exit 2 ;;
  esac
  W="$OUT/$case"; rm -rf "$W"; mkdir -p "$W"
  say "$case: $SRC -> 3.0 ($MODE), engine docker:$TAG-dind${CV:+, compose $CV}, $MEM (swap total $SWAP)"
  img="$(host_image "$TAG" "$CV")"
  cleanup
  docker run -d --name "$P-host" --privileged --network bridge --memory "$MEM" --memory-swap "$SWAP" \
    -v "$P-dind-$TAG:/var/lib/docker" -v "$W:/work" -v "$ROOT/tests:/harness:ro" -v "$DISTS:/dists:ro" \
    "$img" --default-address-pool base=10.231.0.0/16,size=24 --bip 10.230.0.1/24 >/dev/null
  n=0; until docker exec "$P-host" docker info >/dev/null 2>&1; do
    n=$((n + 1)); [ "$n" -gt 60 ] && { docker logs --tail 30 "$P-host"; exit 1; }; sleep 2; done
  preload
  sampler "$(docker inspect -f '{{.Id}}' "$P-host")" > "$W/mem.tsv" &
  SAMPLER=$!
  rc=0
  docker exec -e SRC="$SRC" -e MODE="$MODE" -e ACCEPT="$ACCEPT" "$P-host" \
    sh /harness/e2e_upgrade/inner.sh > "$W/inner.log" 2>&1 || rc=$?
  tail -n 25 "$W/inner.log"
  # the user's stack and data go; the engine's image and build cache stay for the next case
  docker exec "$P-host" sh -c 'cd /home/user/sokkan 2>/dev/null && docker compose down -v --remove-orphans -t 5 >/dev/null 2>&1;
    docker rm -f $(docker ps -aq) >/dev/null 2>&1; docker volume rm $(docker volume ls -q) >/dev/null 2>&1' || true
  cleanup
  kill "$SAMPLER" 2>/dev/null || true
  cp "$DISTS/tree.txt" "$W/"
  echo "case=$case src=$SRC mode=$MODE engine=$TAG compose=${CV:-bundled} mem=$MEM swap=$SWAP inner_rc=$rc" > "$W/case.txt"
  python3 "$HERE/report.py" "$W" "$HERE/results/$case.json" || true
done

if [ "${KEEP_CACHE:-0}" != 1 ]; then
  docker volume rm $(docker volume ls -q | grep "^$P-dind-") >/dev/null 2>&1 || true
  docker rmi $(docker images -q "$P-hostimg") >/dev/null 2>&1 || true
fi
echo "raw results: $OUT"
