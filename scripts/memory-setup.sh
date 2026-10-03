#!/bin/sh
# sokkan memory setup — pick the memory profile (leger | standard | gpu) and
# install the memory model (CortHeXis engine). Run from the sokkan directory;
# re-runnable. Called by the installer; safe to run again after a hardware change.
#
#   ./scripts/memory-setup.sh                    # recommend, write .env, ask about the model licence
#   ./scripts/memory-setup.sh --profile standard # force a profile
#   ./scripts/memory-setup.sh --accept | --decline   # answer the Gemma Terms of Use without asking
#   ./scripts/memory-setup.sh --non-interactive  # .env only; the model is set up by `docker compose up`
#   ./scripts/memory-setup.sh --force            # re-apply the recommendation over an existing profile
#
# Writes to .env: SOKKAN_MEMORY_PROFILE, SOKKAN_EMBED_ACCEL (cpu | sycl | cuda) and
# the `rerank` entry of COMPOSE_PROFILES (Standard, GPU). Everything else is kept.
set -eu

cd "$(dirname "$0")/.." || exit 1
[ -f .env ] || { echo "memory-setup: .env missing — cp .env.example .env first" >&2; exit 1; }

PROFILE=""; ANSWER=""; INTERACTIVE=1; FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="${2:-}"; shift ;;
    --profile=*) PROFILE="${1#*=}" ;;
    --accept) ANSWER="--accept" ;;
    --decline) ANSWER="--decline" ;;
    --non-interactive) INTERACTIVE=0 ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "memory-setup: unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
case "$PROFILE" in ""|leger|standard|gpu) ;; *) echo "memory-setup: profile must be leger, standard or gpu" >&2; exit 2 ;; esac

env_get() { sed -n "s/^$1=//p" .env | tail -n 1; }
env_set() {  # env_set KEY VALUE — replace or append, keep the rest of .env
  grep -v "^$1=" .env > .env.tmp || true
  printf '%s=%s\n' "$1" "$2" >> .env.tmp
  mv .env.tmp .env
}

# 1. recommendation (stdlib Python; without python3 the machine gets Léger)
REC="leger"; ACCEL="cpu"; REASON="python3 not found — default"
if command -v python3 >/dev/null 2>&1; then
  OUT="$(python3 -m magnitude --memory-profile 2>/dev/null | python3 -c 'import json,sys
d = json.load(sys.stdin); print(d["recommended"], d["accel"], d["reason"])' 2>/dev/null || true)"
  if [ -n "$OUT" ]; then
    REC="${OUT%% *}"; REST="${OUT#* }"; ACCEL="${REST%% *}"; REASON="${REST#* }"
  fi
fi
CURRENT="$(env_get SOKKAN_MEMORY_PROFILE)"
echo "memory profile — recommended: $REC ($REASON); configured: ${CURRENT:-none}"

if [ -n "$PROFILE" ]; then CHOSEN="$PROFILE"
elif [ -n "$CURRENT" ] && [ "$FORCE" = 0 ]; then CHOSEN="$CURRENT"
else CHOSEN="$REC"; fi

# 2. accelerator: only for the GPU profile, and only what Docker can drive
if [ "$CHOSEN" = "gpu" ]; then
  case "$ACCEL" in
    sycl|cuda) ;;
    metal) echo "  ! Apple GPU: Docker has no Metal — run docker/embed/run.sh natively and set SOKKAN_EMBED_URLS; using the CPU container meanwhile"; ACCEL="cpu" ;;
    *) echo "  ! no GPU the containers can use (accelerator: $ACCEL) — the GPU profile will run on CPU"; ACCEL="cpu" ;;
  esac
else
  ACCEL="cpu"
fi

# 3. .env
env_set SOKKAN_MEMORY_PROFILE "$CHOSEN"
env_set SOKKAN_EMBED_ACCEL "$ACCEL"
CP="$(env_get COMPOSE_PROFILES | tr ',' '\n' | grep -v '^rerank$' | grep -v '^$' | paste -sd, - || true)"
if [ "$CHOSEN" != "leger" ]; then CP="${CP:+$CP,}rerank"; fi
if [ -n "$CP" ]; then env_set COMPOSE_PROFILES "$CP"
else grep -v '^COMPOSE_PROFILES=' .env > .env.tmp || true; mv .env.tmp .env; fi
echo "  ✔ .env: SOKKAN_MEMORY_PROFILE=$CHOSEN SOKKAN_EMBED_ACCEL=$ACCEL${CP:+ COMPOSE_PROFILES=$CP}"

# 4. licence decision + model download (in the one-shot fetch container)
if ! command -v docker >/dev/null 2>&1; then
  echo "  docker not found — the model is set up at the first \`docker compose up\`"; exit 0
fi
DC="docker"; docker info >/dev/null 2>&1 || DC="sudo docker"
if [ -n "$ANSWER" ]; then
  $DC compose run --rm -T corthexis-embed-fetch python /opt/corthexis/models.py setup $ANSWER --profile "$CHOSEN"
elif [ "$INTERACTIVE" = 1 ] && [ -t 0 ]; then
  $DC compose run --rm corthexis-embed-fetch python /opt/corthexis/models.py setup --interactive --profile "$CHOSEN"
else
  echo "  model: set up at \`docker compose up\` (MIT model until the Gemma terms are accepted;"
  echo "  accept later with ./scripts/memory-setup.sh --accept)"
fi
