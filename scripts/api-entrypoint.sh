#!/bin/sh
set -e
if ! mkdir -p "$SOKKAN_DATA_DIR" "$SOKKAN_MEMORY_DIR" "$CLAUDE_CONFIG_DIR" 2>/dev/null; then
  echo "[sokkan] ERROR: $SOKKAN_DATA_DIR is not writable by uid $(id -u)." >&2
  echo "[sokkan] Data volumes created by SOKKAN <= v0.1.0 are owned by root; fix once with:" >&2
  echo "[sokkan]   docker compose run --rm --user root api chown -R 1000:1000 /data" >&2
  exit 1
fi

# index mémoire au boot (1er run : télécharge le modèle d'embeddings ~120 Mo)
python /app/memory/index_memory.py || echo "[sokkan] memory index skipped (see above)"

# réindexation périodique (les notes écrites par les sessions deviennent cherchables)
(
  while true; do
    sleep "${SOKKAN_REINDEX_S:-120}"
    python /app/memory/index_memory.py >/dev/null 2>&1 || true
  done
) &

exec uvicorn app:app --host 0.0.0.0 --port 8097 --app-dir /app/backend
