#!/bin/sh
set -e
mkdir -p "$SOKKAN_DATA_DIR" "$SOKKAN_MEMORY_DIR" "$CLAUDE_CONFIG_DIR"

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
