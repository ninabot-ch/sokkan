#!/bin/sh
# sokkan doctor — sanity-check an install (prereqs, config, running stack).
# Run from the sokkan directory:  ./scripts/doctor.sh
# Safe to run anytime. Read-only, with one exception: an install without a
# memory profile (upgrade from 2.x) gets the recommended one written to .env.

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
  if CV="$($DC compose version --short 2>/dev/null)"; then
    CV="${CV#v}"; CMAJ="${CV%%.*}"; CMIN="${CV#*.}"; CMIN="${CMIN%%.*}"
    # the compose file runs on any Compose v2 from 2.12 (end-to-end tested); 2.6-2.11
    # parse it (untested end to end); older ones cannot read it
    if [ "$CMAJ" -gt 2 ] 2>/dev/null || { [ "$CMAJ" -eq 2 ] && [ "$CMIN" -ge 12 ]; } 2>/dev/null; then
      ok "Compose $CV"
    elif [ "$CMAJ" -eq 2 ] 2>/dev/null && [ "$CMIN" -ge 6 ] 2>/dev/null; then
      warn "Compose $CV — older than the oldest tested release (2.12) — update: get.docker.com"
    else
      fail "Compose $CV — too old for this compose file (2.12+) — update: get.docker.com"
    fi
  else
    fail "no Compose v2 plugin — distro/snap docker? use get.docker.com"
  fi
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

# --- memory profile (CortHeXis engine) ------------------------------------
if [ -f .env ]; then
  REC=""
  if command -v python3 >/dev/null 2>&1; then
    REC="$(python3 -m magnitude --memory-profile --env 2>/dev/null | sed -n 's/^SOKKAN_MEMORY_PROFILE=//p')"
  fi
  MP="${SOKKAN_MEMORY_PROFILE:-}"
  if [ -z "$MP" ] && [ -n "$ML_SERVICE_URL" ]; then
    warn "memory profile unset, ML_SERVICE_URL set → 2.x remote embeddings (./scripts/memory-setup.sh to move to 3.0)"
  elif [ -z "$MP" ]; then
    MP="${REC:-leger}"
    printf 'SOKKAN_MEMORY_PROFILE=%s\n' "$MP" >> .env
    warn "memory profile was unset — wrote SOKKAN_MEMORY_PROFILE=$MP to .env (./scripts/memory-setup.sh to review)"
  else
    case "$MP" in
      leger|standard|gpu|legacy|remote) ok "memory profile $MP${REC:+ (recommended: $REC)}" ;;
      *) fail "SOKKAN_MEMORY_PROFILE=$MP — expected leger, standard or gpu" ;;
    esac
    [ -n "$REC" ] && [ "$REC" != "$MP" ] && case "$MP" in leger|standard|gpu)
      warn "this machine suits the $REC profile — ./scripts/memory-setup.sh --profile $REC" ;; esac
  fi
  if [ -n "${MEM_GB:-}" ]; then
    case "$MP" in
      standard) [ "$MEM_GB" -ge 15 ] || warn "profile standard on ${MEM_GB} GB RAM — 16 GB recommended" ;;
      leger) [ "$MEM_GB" -ge 4 ] || warn "profile leger on ${MEM_GB} GB RAM — 4 GB minimum" ;;
    esac
  fi
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
    case " $UP " in
      *" corthexis-embed "*)
        if $DC compose exec -T corthexis-embed curl -sf -m 5 http://localhost:8080/health >/dev/null 2>&1; then
          ok "memory model server healthy"
        else
          warn "memory model server not ready — docker compose logs corthexis-embed"
        fi
        ACT="$($DC compose exec -T api cat /models/active.json 2>/dev/null | tr -d '\n ')"
        case "$ACT" in
          *'"embed":"embeddinggemma'*) ok "memory model: EmbeddingGemma (Gemma Terms of Use accepted)" ;;
          *'"embed":null'*|"") warn "no memory model installed — ./scripts/memory-setup.sh" ;;
          *) M="$(printf '%s' "$ACT" | sed -n 's/.*"embed":"\([^"]*\)".*/\1/p')"
             R="$(printf '%s' "$ACT" | sed -n 's/.*"reason":"\([^"]*\)".*/\1/p')"
             warn "memory model: $M ($R) — ./scripts/memory-setup.sh --accept for EmbeddingGemma (better recall)" ;;
        esac ;;
      *) case "${SOKKAN_MEMORY_PROFILE:-}" in leger|standard|gpu)
           warn "memory model server not running — docker compose up -d" ;; esac ;;
    esac
  else
    warn "stack not running — docker compose up -d --build"
  fi
fi

printf '\n%d ok · %d warning(s) · %d failure(s)\n' "$OK" "$WARN" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
