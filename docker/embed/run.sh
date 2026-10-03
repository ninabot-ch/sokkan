#!/bin/sh
# corthexis-embed — llama.cpp server for the memory engine (embedding or reranker).
#
#   run.sh embed | rerank
#
# The model is chosen by the first-run setup (memory/core/models.py), which
# writes <models>/<role>.env after the licence decision and the SHA-256 check.
# Until that file exists the container waits (unhealthy) instead of crashing in
# a loop: accepting the terms later, or copying a model by hand, starts it.
# Also runs outside Docker (Apple GPU): LLAMA_SERVER=llama-server CORTHEXIS_MODELS_DIR=…
#
# Buffers follow what runs in production on the reference instance:
#  - --cache-ram 0 is MANDATORY: the prompt cache defaults to 8 GiB and grows
#    until the OOM killer steps in;
#  - -ub must hold a whole sequence (non-causal embedding models); keep it at
#    2048 or below, -ub 4096 inflates compute buffers by gigabytes;
#  - the client truncates documents to 500 tokens (/tokenize) so every input
#    fits in a slot (-c / -np).
set -eu

ROLE="${1:-embed}"
MODELS="${CORTHEXIS_MODELS_DIR:-/models}"
ENV_FILE="$MODELS/${ROLE}.env"
PROFILE="${CORTHEXIS_MEMORY_PROFILE:-leger}"
DEVICE="${CORTHEXIS_EMBED_DEVICE:-cpu}"   # cpu | gpu (set by the compose.<accel>.yml overrides)
PORT="${CORTHEXIS_EMBED_PORT:-8080}"

while [ ! -f "$ENV_FILE" ]; do
  echo "[corthexis-embed] waiting for $ENV_FILE — run: docker compose run --rm corthexis-embed-fetch" >&2
  sleep 30
done
# shellcheck disable=SC1090
. "$ENV_FILE"
FILE="$MODELS/$CORTHEXIS_MODEL_FILE"
[ -f "$FILE" ] || { echo "[corthexis-embed] missing $FILE" >&2; exit 1; }

# threads / slots / context per profile (overridable)
case "$DEVICE:$PROFILE" in
  gpu:*)        T=2; NP=2; CTX=2048; UB=1024 ;;
  cpu:standard) T=8; NP=4; CTX=4096; UB=2048 ;;
  *)            T=4; NP=2; CTX=2048; UB=2048 ;;
esac
T="${CORTHEXIS_EMBED_THREADS:-$T}"
NP="${CORTHEXIS_EMBED_SLOTS:-$NP}"
CTX="${CORTHEXIS_EMBED_CTX:-$CTX}"
UB="${CORTHEXIS_EMBED_UBATCH:-$UB}"

set -- -m "$FILE" -t "$T" -np "$NP" -c "$CTX" -b "$UB" -ub "$UB" \
  --cache-ram 0 --host "${CORTHEXIS_EMBED_HOST:-0.0.0.0}" --port "$PORT" --no-webui
if [ "$ROLE" = "rerank" ]; then
  set -- "$@" --reranking
else
  set -- "$@" --embedding --pooling "${CORTHEXIS_MODEL_POOLING:-mean}"
fi
[ "$DEVICE" = "gpu" ] && set -- "$@" --n-gpu-layers 99

echo "[corthexis-embed] $ROLE: $CORTHEXIS_MODEL_KEY ($DEVICE, profile $PROFILE)" >&2
exec "${LLAMA_SERVER:-/app/llama-server}" "$@"
