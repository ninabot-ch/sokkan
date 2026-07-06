#!/bin/sh
set -e
if ! mkdir -p "$SOKKAN_DATA_DIR" "$SOKKAN_MEMORY_DIR" "$CLAUDE_CONFIG_DIR" 2>/dev/null; then
  echo "[sokkan] ERROR: $SOKKAN_DATA_DIR is not writable by uid $(id -u)." >&2
  echo "[sokkan] Data volumes created by SOKKAN <= v0.1.0 are owned by root; fix once with:" >&2
  echo "[sokkan]   docker compose run --rm --user root api chown -R 1000:1000 /data" >&2
  exit 1
fi

# l'index mémoire (boot + réindexation périodique) tourne DANS le backend :
# thread daemon lancé au startup FastAPI, modèle d'embeddings gardé chaud,
# réindex seulement quand le corpus change (SOKKAN_REINDEX_S, défaut 120 s).
exec uvicorn app:app --host 0.0.0.0 --port 8097 --app-dir /app/backend
