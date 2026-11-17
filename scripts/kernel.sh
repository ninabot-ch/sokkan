#!/bin/sh
# sokkan kernel — switch the 4.0 kernel engine (docker-compose.kernel.yml, docs/KERNEL.md) on and
# off on THIS install. Run from the sokkan folder; it edits .env only, then says what to run.
#
#   sh scripts/kernel.sh enable --image <kernel image, pinned>   [options below]
#   sh scripts/kernel.sh refresh      host paths again (upgrade.sh runs it when the kernel is on)
#   sh scripts/kernel.sh disable      back to the legacy engine (volumes and schema `kernel` kept)
#   sh scripts/kernel.sh status
#
# enable options (each also as the .env variable named):
#   --image <ref>          SOKKAN_KERNEL_IMAGE            the kernel (required the first time)
#   --sandbox-image <ref>  SOKKAN_KERNEL_SANDBOX_IMAGE    the sandboxes' image
#   --sandboxd-image <ref> SOKKAN_KERNEL_SANDBOXD_IMAGE   socket proxy + egress image
#   --net none|registries|open  SOKKAN_KERNEL_SANDBOX_NET what a sandbox reaches (default none)
#   --memory <2g>  --cpus <2>   SOKKAN_KERNEL_SANDBOX_MEMORY / _CPUS, caps of each sandbox
#   --keep-engine          do not set SOKKAN_ENGINE_RUNTIME=kernel (the service runs, the
#                          sessions stay on the legacy engine)
#
# What enable writes: this file in COMPOSE_FILE, `kernel` in COMPOSE_PROFILES, the two database
# passwords of the kernel (generated once), the sandbox network's name (one per install, so two
# installs on one docker daemon never share it), and the two HOST paths the kernel's sandboxes
# mount: SOKKAN_KERNEL_DATA_HOST (where docker keeps the volume sokkan-data) and
# SOKKAN_KERNEL_WORKSPACE_HOST (the default project folder). Requires docker >= 26.
set -eu
cd "$(dirname "$0")/.." || exit 1
[ -f .env ] && [ -f docker-compose.yml ] || { echo "kernel: run it from the sokkan folder (no .env here)" >&2; exit 1; }

say() { printf '%s\n' "$*"; }
die() { printf '✗ kernel: %s\n' "$*" >&2; exit 1; }
getenv() { sed -n "s/^$1=//p" .env | tail -n 1; }
set_env() { grep -v "^$1=" .env > .env.tmp || true; printf '%s=%s\n' "$1" "$2" >> .env.tmp; cat .env.tmp > .env; rm -f .env.tmp; chmod 600 .env; }
unset_env() { grep -v "^$1=" .env > .env.tmp || true; cat .env.tmp > .env; rm -f .env.tmp; }
rand_hex() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
DC="docker"; docker info >/dev/null 2>&1 || DC="sudo docker"
KFILE=docker-compose.kernel.yml

enabled() { case ",$(getenv COMPOSE_PROFILES)," in *,kernel,*) return 0 ;; esac; return 1; }

project() {   # the compose project of this install (its volumes are <project>_<name>)
  p="${COMPOSE_PROJECT_NAME:-$(getenv COMPOSE_PROJECT_NAME)}"
  [ -n "$p" ] || p="$(COMPOSE_FILE=docker-compose.yml COMPOSE_PROFILES= $DC compose config 2>/dev/null | sed -n 's/^name: //p' | head -n 1)"
  [ -n "$p" ] || p="$(basename "$(pwd -P)" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9_-')"
  [ -n "$p" ] || die "cannot tell the compose project name: set COMPOSE_PROJECT_NAME in .env"
  printf '%s' "$p"
}

docker_ok() {
  v="$($DC version --format '{{.Server.Version}}' 2>/dev/null || true)"
  [ -n "$v" ] || die "docker does not answer"
  major="${v%%.*}"
  case "$major" in ''|*[!0-9]*) die "unknown docker version $v" ;; esac
  [ "$major" -ge 26 ] || die "docker $v: the kernel engine needs docker >= 26 (volume sub-paths)"
}

host_paths() {   # SOKKAN_KERNEL_DATA_HOST, SOKKAN_KERNEL_WORKSPACE_HOST, the sandbox network
  p="$(project)"
  vol="${p}_sokkan-data"
  if ! $DC volume inspect "$vol" >/dev/null 2>&1; then
    # a new install: the volume compose would create, with compose's own labels
    $DC volume create --label "com.docker.compose.project=$p" \
      --label "com.docker.compose.volume=sokkan-data" "$vol" >/dev/null || die "cannot create the volume $vol"
  fi
  mp="$($DC volume inspect -f '{{.Mountpoint}}' "$vol" 2>/dev/null || true)"
  case "$mp" in /?*) ;; *) die "the volume $vol has no host path (driver $($DC volume inspect -f '{{.Driver}}' "$vol" 2>/dev/null)): the kernel's sandboxes need a local volume" ;; esac
  ws="$(getenv SOKKAN_WORKSPACE)"; ws="${ws:-./workspace}"
  mkdir -p "$ws" 2>/dev/null || true
  wsabs="$(cd "$ws" 2>/dev/null && pwd -P)" || die "the workspace folder $ws does not exist"
  [ "$wsabs" != / ] || die "SOKKAN_WORKSPACE cannot be /"
  set_env SOKKAN_KERNEL_DATA_HOST "$mp"
  set_env SOKKAN_KERNEL_WORKSPACE_HOST "$wsabs"
  [ -n "$(getenv SOKKAN_KERNEL_SANDBOX_NETWORK)" ] || set_env SOKKAN_KERNEL_SANDBOX_NETWORK "${p}-kernel-sandbox"
}

compose_file_add() {
  cf="$(getenv COMPOSE_FILE)"
  case ":$cf:" in *":$KFILE:"*) return 0 ;; esac
  set_env COMPOSE_FILE "${cf:-docker-compose.yml}:$KFILE"
  [ -n "$(getenv COMPOSE_PATH_SEPARATOR)" ] || set_env COMPOSE_PATH_SEPARATOR ":"
}

compose_file_remove() {
  cf="$(getenv COMPOSE_FILE)"
  new="$(printf '%s' "$cf" | tr ':' '\n' | grep -vx "$KFILE" | paste -sd: -)"
  if [ -z "$new" ] || [ "$new" = docker-compose.yml ]; then unset_env COMPOSE_FILE
  else set_env COMPOSE_FILE "$new"; fi
}

profiles_set() {   # profiles_set add|remove
  pr="$(getenv COMPOSE_PROFILES)"
  rest="$(printf '%s' "$pr" | tr ',' '\n' | grep -vx kernel | grep -v '^$' | paste -sd, -)"
  if [ "$1" = add ]; then set_env COMPOSE_PROFILES "${rest:+$rest,}kernel"
  elif [ -n "$rest" ]; then set_env COMPOSE_PROFILES "$rest"
  else unset_env COMPOSE_PROFILES; fi
}

cmd="${1:-status}"; [ $# -gt 0 ] && shift
IMAGE=""; SBI=""; SBD=""; NET=""; MEM=""; CPUS=""; KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE="${2:?--image <ref>}"; shift ;;
    --sandbox-image) SBI="${2:?--sandbox-image <ref>}"; shift ;;
    --sandboxd-image) SBD="${2:?--sandboxd-image <ref>}"; shift ;;
    --net) NET="${2:?--net none|registries|open}"; shift ;;
    --memory) MEM="${2:?--memory <2g>}"; shift ;;
    --cpus) CPUS="${2:?--cpus <2>}"; shift ;;
    --keep-engine) KEEP=1 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) die "unknown option $1" ;;
  esac
  shift
done

case "$cmd" in
  enable)
    docker_ok
    [ -n "$IMAGE" ] || IMAGE="${SOKKAN_KERNEL_IMAGE:-$(getenv SOKKAN_KERNEL_IMAGE)}"
    [ -n "$IMAGE" ] || die "--image <kernel image> is required (a pinned tag or digest, never latest)"
    case "$IMAGE" in *:latest|*:latest@*) die "$IMAGE: pin the kernel image, never latest" ;; esac
    case "$NET" in ''|none|registries|open) ;; *) die "--net none|registries|open" ;; esac
    set_env SOKKAN_KERNEL_IMAGE "$IMAGE"
    [ -n "$SBI" ] && set_env SOKKAN_KERNEL_SANDBOX_IMAGE "$SBI"
    [ -n "$SBD" ] && set_env SOKKAN_KERNEL_SANDBOXD_IMAGE "$SBD"
    [ -n "$NET" ] && set_env SOKKAN_KERNEL_SANDBOX_NET "$NET"
    [ -n "$MEM" ] && set_env SOKKAN_KERNEL_SANDBOX_MEMORY "$MEM"
    [ -n "$CPUS" ] && set_env SOKKAN_KERNEL_SANDBOX_CPUS "$CPUS"
    [ -n "$(getenv SOKKAN_KERNEL_DB_OWNER_PASSWORD)" ] || set_env SOKKAN_KERNEL_DB_OWNER_PASSWORD "$(rand_hex 18)"
    [ -n "$(getenv SOKKAN_KERNEL_DB_APP_PASSWORD)" ] || set_env SOKKAN_KERNEL_DB_APP_PASSWORD "$(rand_hex 18)"
    host_paths
    compose_file_add
    profiles_set add
    [ "$KEEP" = 1 ] || set_env SOKKAN_ENGINE_RUNTIME kernel
    net="$(getenv SOKKAN_KERNEL_SANDBOX_NET)"
    say "✔ kernel engine enabled ($IMAGE) — sandbox network: ${net:-none}"
    say "  start it:  $DC compose up -d --remove-orphans"
    ;;
  refresh)
    enabled || { say "kernel engine not enabled: nothing to refresh"; exit 0; }
    docker_ok
    host_paths
    compose_file_add
    say "✔ kernel engine: host paths refreshed"
    ;;
  disable)
    compose_file_remove
    profiles_set remove
    [ "$(getenv SOKKAN_ENGINE_RUNTIME)" = kernel ] && unset_env SOKKAN_ENGINE_RUNTIME
    say "✔ kernel engine disabled: new sessions run on the legacy engine. Its volumes, its schema"
    say "  \`kernel\` and the settings in .env are kept (enable it again with: sh scripts/kernel.sh enable)."
    say "  apply:  $DC compose up -d --remove-orphans"
    ;;
  status)
    if enabled; then
      say "kernel engine: ENABLED"
      for v in SOKKAN_KERNEL_IMAGE SOKKAN_KERNEL_SANDBOX_IMAGE SOKKAN_KERNEL_SANDBOX_NET \
               SOKKAN_KERNEL_SANDBOX_MEMORY SOKKAN_KERNEL_SANDBOX_CPUS SOKKAN_KERNEL_SANDBOX_NETWORK \
               SOKKAN_KERNEL_DATA_HOST SOKKAN_KERNEL_WORKSPACE_HOST SOKKAN_ENGINE_RUNTIME; do
        say "  $v=$(getenv "$v")"
      done
    else
      say "kernel engine: disabled"
    fi
    ;;
  *) die "usage: kernel.sh enable|refresh|disable|status (--help)" ;;
esac
