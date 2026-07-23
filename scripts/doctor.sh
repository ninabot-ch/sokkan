#!/bin/sh
# sokkan doctor — sanity-check an install (prereqs, config, running stack).
# Run from the sokkan directory:  ./scripts/doctor.sh
# Safe to run anytime; read-only.

OK=0; WARN=0; FAIL=0
ok()   { printf '  ✔ %s\n' "$*"; OK=$((OK+1)); }
warn() { printf '  ⚠ %s\n' "$*"; WARN=$((WARN+1)); }
fail() { printf '  ✗ %s\n' "$*"; FAIL=$((FAIL+1)); }

cd "$(dirname "$0")/.." || exit 1
printf 'sokkan doctor — %s\n\n' "$(pwd)"

# --- host ---------------------------------------------------------------
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64|aarch64|arm64) ok "arch $ARCH (supported)" ;;
  *) fail "arch $ARCH — untested; expect x86_64 or arm64" ;;
esac

if command -v docker >/dev/null 2>&1; then
  DC="docker"; docker info >/dev/null 2>&1 || DC="sudo docker"
  DV="$($DC version --format '{{.Server.Version}}' 2>/dev/null)"
  if [ -n "$DV" ]; then
    MAJ="${DV%%.*}"
    [ "$MAJ" -ge 24 ] 2>/dev/null && ok "Docker Engine $DV" || warn "Docker Engine $DV — 24+ recommended"
  else
    fail "docker present but the daemon is unreachable (permissions? service down?)"
  fi
  $DC compose version >/dev/null 2>&1 && ok "Compose v2 plugin" \
    || fail "no Compose v2 plugin — distro/snap docker? use get.docker.com"
else
  fail "docker not installed — the installer can do it: curl -fsSL https://sokkan.ch/install.sh | sh"
  DC=""
fi

# RAM / disk (~4 GB RAM, ~3 GB disk for model + build)
if [ -r /proc/meminfo ]; then
  MEM_GB=$(awk '/MemTotal/ {printf "%d", $2/1048576}' /proc/meminfo)
  [ "$MEM_GB" -ge 4 ] && ok "RAM ${MEM_GB} GB" || warn "RAM ${MEM_GB} GB — 4 GB recommended (embedding model + build)"
fi
DISK_GB=$(df -Pk . 2>/dev/null | awk 'NR==2 {printf "%d", $4/1048576}')
[ -n "$DISK_GB" ] && { [ "$DISK_GB" -ge 3 ] && ok "free disk ${DISK_GB} GB" || warn "free disk ${DISK_GB} GB — 3 GB recommended"; }

# --- config -------------------------------------------------------------
if [ -f .env ]; then
  ok ".env present"
  # shellcheck disable=SC1091
  . ./.env 2>/dev/null
  if [ -n "$ANTHROPIC_API_KEY" ] || [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
    ok "model credential set ($([ -n "$ANTHROPIC_API_KEY" ] && echo ANTHROPIC_API_KEY || echo CLAUDE_CODE_OAUTH_TOKEN))"
  else
    warn "no ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN in .env — set one, or configure a provider in Profile → Model"
  fi
  [ -n "$SOKKAN_LOCAL_TOKEN" ] && ok "SOKKAN_LOCAL_TOKEN set" \
    || warn "SOKKAN_LOCAL_TOKEN empty — open access, trusted networks only"
  WS="${SOKKAN_WORKSPACE:-./workspace}"
  [ -d "$WS" ] && ok "workspace exists: $WS" || fail "SOKKAN_WORKSPACE does not exist: $WS"
else
  fail ".env missing — cp .env.example .env"
fi

# --- running stack ------------------------------------------------------
PORT="${SOKKAN_PORT:-3009}"
if [ -n "$DC" ]; then
  UP="$($DC compose ps --services --status running 2>/dev/null | xargs 2>/dev/null)"
  if [ -n "$UP" ]; then
    ok "containers running: $UP"
    if command -v curl >/dev/null 2>&1; then
      if curl -sf -m 5 "http://localhost:$PORT/api/health" >/dev/null 2>&1; then
        ok "API healthy on :$PORT"
      else
        fail "API not answering on http://localhost:$PORT/api/health — docker compose logs api"
      fi
    fi
  else
    warn "stack not running — docker compose up -d --build"
  fi
fi

printf '\n%d ok · %d warning(s) · %d failure(s)\n' "$OK" "$WARN" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
