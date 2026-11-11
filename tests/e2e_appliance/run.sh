#!/bin/sh
# tests/e2e_appliance — play the T1-T14 plan of the on-prem appliance on a BLANK machine.
#
#   tests/e2e_appliance/run.sh [all|appliance|upgrade]        (default: all)
#
# The « machine » is a Docker-in-Docker container on this host: its own engine, network and
# volumes, memory-capped, nothing published on the host. Run it on a build host with ~8 GB
# free; it creates no cloud resource. What it needs from outside: Docker Hub / ghcr / PyPI /
# npm / Hugging Face (image and model pulls), GitHub (the pinned cosign binary).
#
#   E2E_WORK=/root/sokkan-e2e   work folder — NOT under /tmp (a dind masks the host's /tmp)
#   E2E_MEM=6g                  memory cap of the machine      E2E_SWAP=7g  (memory + swap)
#   E2E_PSI_MAX=10              stop everything when the HOST's memory pressure
#                               (/proc/pressure/memory « some avg10 ») goes above this
#   E2E_KEEP=1                  keep the machine and its volume at the end (debugging)
#   INSTALLER_OLD=<file>        the installer a client of 3.4.6 used (default: sokkan.ch/install.sh)
#
# Sources: the releases are `git archive` of their tags (v3.4.6, v3.5.1) and of HEAD of this
# checkout (COMMITTED state — commit first). Results: $E2E_WORK/work/results.json (+ logs),
# summary printed at the end and copied to tests/e2e_appliance/results/.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
WHAT="${1:-all}"
W="${E2E_WORK:-/root/sokkan-e2e}"
NAME=sokkan-appliance-e2e
MEM="${E2E_MEM:-6g}"; SWAP="${E2E_SWAP:-7g}"; PSI_MAX="${E2E_PSI_MAX:-10}"
case "$W" in /tmp/*) echo "E2E_WORK must not be under /tmp" >&2; exit 2 ;; esac
say() { printf '\n######## %s\n' "$*"; }

# one environment at a time, and never on top of somebody else's ports: nothing is published
docker inspect "$NAME" >/dev/null 2>&1 && { echo "$NAME already exists (another run?) — remove it first" >&2; exit 1; }
mkdir -p "$W/dists" "$W/work" "$HERE/results"
rm -rf "$W/work"/* 2>/dev/null || true

# ---- the releases, as published ---------------------------------------------------------
if [ -d "$ROOT/.git" ] || git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  HASH="$(git -C "$ROOT" rev-parse --short HEAD)"
  NEW="$(cat "$ROOT/VERSION")+$HASH"
  git -C "$ROOT" archive --prefix=sokkan/ -o "$W/dists/sokkan-$NEW.tar.gz" HEAD
  for t in 3.4.6 3.5.1; do
    h="$(git -C "$ROOT" rev-parse --short "v$t")"
    [ -f "$W/dists/sokkan-$t+$h.tar.gz" ] || git -C "$ROOT" archive --prefix=sokkan/ -o "$W/dists/sokkan-$t+$h.tar.gz" "v$t"
  done
  echo "$NEW" > "$W/dists/NEW_VERSION"
  git -C "$ROOT" show HEAD:scripts/install.sh > "$W/dists/install-new.sh"
else
  # a copy without history (the build host): the dists were prepared where the repository is
  [ -s "$W/dists/NEW_VERSION" ] || { echo "no git here and no prepared $W/dists — see README.md" >&2; exit 2; }
  NEW="$(cat "$W/dists/NEW_VERSION")"
fi
if [ ! -s "$W/dists/install-old.sh" ]; then
  if [ -n "${INSTALLER_OLD:-}" ]; then cp "$INSTALLER_OLD" "$W/dists/install-old.sh"
  else curl -fsSL https://sokkan.ch/install.sh -o "$W/dists/install-old.sh"; fi
fi
. "$ROOT/deploy/appliance/versions.env"
cp "$ROOT/deploy/appliance/versions.env" "$W/dists/versions.env"
if [ ! -s "$W/dists/cosign" ] || ! echo "$COSIGN_SHA256  $W/dists/cosign" | sha256sum -c - >/dev/null 2>&1; then
  curl -fsSL "$COSIGN_URL" -o "$W/dists/cosign"
  echo "$COSIGN_SHA256  $W/dists/cosign" | sha256sum -c - >/dev/null || { echo "cosign: sha256 mismatch" >&2; exit 1; }
fi
say "release under test: $NEW · memory $MEM (swap total $SWAP) · work $W"

# ---- the machine ------------------------------------------------------------------------
cleanup() {
  [ -n "${WATCH:-}" ] && kill "$WATCH" 2>/dev/null || true
  [ -n "${SAMPLER:-}" ] && kill "$SAMPLER" 2>/dev/null || true
  if [ "${E2E_KEEP:-0}" != 1 ]; then
    docker rm -f -v "$NAME" >/dev/null 2>&1 || true
    docker volume rm "$NAME-docker" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM
docker run -d --name "$NAME" --privileged --network bridge --memory "$MEM" --memory-swap "$SWAP" \
  -v "$NAME-docker:/var/lib/docker" -v "$W/work:/work" -v "$W/dists:/dists:ro" -v "$ROOT/tests:/harness:ro" \
  docker:27-dind --default-address-pool base=10.241.0.0/16,size=24 --bip 10.240.0.1/24 >/dev/null
n=0; until docker exec "$NAME" docker info >/dev/null 2>&1; do
  n=$((n + 1)); [ "$n" -gt 60 ] && { docker logs --tail 30 "$NAME"; exit 1; }; sleep 2; done
# nothing of the machine listens on the host
PUBLISHED="$(docker port "$NAME" 2>/dev/null || true)"
[ -z "$PUBLISHED" ] || { echo "the test machine publishes ports on the host: $PUBLISHED" >&2; exit 1; }

# host watchdog: this host serves other things — stop when ITS memory pressure rises
( while sleep 5; do
    p="$(sed -n 's/^some avg10=\([0-9]*\).*/\1/p' /proc/pressure/memory 2>/dev/null)"
    if [ "${p:-0}" -gt "$PSI_MAX" ]; then
      echo "WATCHDOG: host memory pressure some avg10=$p > $PSI_MAX — stopping the test machine" | tee -a "$W/work/watchdog.log"
      docker rm -f -v "$NAME" >/dev/null 2>&1
      exit 0
    fi
  done ) &
WATCH=$!
( cg="/sys/fs/cgroup/system.slice/docker-$(docker inspect -f '{{.Id}}' "$NAME").scope"
  while [ -d "$cg" ]; do
    printf '%s %s %s\n' "$(date +%s)" "$(cat "$cg/memory.current" 2>/dev/null)" "$(sed -n 's/^some avg10=\([0-9.]*\).*/\1/p' /proc/pressure/memory)"
    sleep 2
  done > "$W/work/mem.tsv" ) &
SAMPLER=$!

# images the stacks need, from the host cache when present (saves pulls)
for i in pgvector/pgvector:pg16 python:3.12-slim node:22-alpine ghcr.io/ggml-org/llama.cpp:server-b11347 \
         caddy:2.10.2-alpine caddy:2-alpine registry:2 minio/minio:latest; do
  docker image inspect "$i" >/dev/null 2>&1 && docker save "$i" | docker exec -i "$NAME" docker load -q >/dev/null 2>&1 || true
done

phase() {
  say "phase: $1"
  docker inspect "$NAME" >/dev/null 2>&1 || { echo "the test machine is gone (watchdog?) — see $W/work/watchdog.log"; return 1; }
  docker exec ${2:+-e "$2"} "$NAME" sh /harness/e2e_appliance/inner.sh "$1" 2>&1 | tee -a "$W/work/$1.log" | grep -E "^RESULT|^####|WATCHDOG|Traceback|Error" || true
}
docker exec "$NAME" sh /harness/e2e_appliance/inner.sh tools
phase prepare
case "$WHAT" in all|appliance)
  phase appliance
  # T13: the machine is « rebooted » — its engine stops and starts, nobody types anything inside
  if docker inspect "$NAME" >/dev/null 2>&1; then
    say "reboot of the test machine"
    T0="$(date +%s)"
    docker restart -t 30 "$NAME" >/dev/null
    n=0; until docker exec "$NAME" docker info >/dev/null 2>&1; do n=$((n + 1)); [ "$n" -gt 90 ] && break; sleep 2; done
    phase after-reboot "E2E_REBOOT_T0=$T0"
  fi ;;
esac
case "$WHAT" in all|upgrade) phase upgrade ;; esac
docker inspect "$NAME" >/dev/null 2>&1 && docker exec "$NAME" python3 /harness/e2e_appliance/suite.py report | tee "$W/work/report.txt"
PEAK="$(awk 'BEGIN{m=0} {if ($2>m) m=$2} END{printf "%d", m/1048576}' "$W/work/mem.tsv" 2>/dev/null || echo '?')"
PSI="$(awk 'BEGIN{m=0} {if ($3>m) m=$3} END{print m}' "$W/work/mem.tsv" 2>/dev/null || echo '?')"
say "machine memory peak: ${PEAK} MiB (cap $MEM) · host pressure peak (some avg10): $PSI"
[ -f "$W/work/results.json" ] && cp "$W/work/results.json" "$HERE/results/$(echo "$NEW" | tr '+' '_').json" 2>/dev/null || true
echo "raw results and logs: $W/work"
