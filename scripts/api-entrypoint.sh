#!/bin/sh
set -e

# Démarré root (cf. api.Dockerfile) : on rend /etc/hosts éditable par le groupe
# sokkan — le backend y maintient les entrées `<name>.fleet` en mode managé —
# puis on DROP vers l'utilisateur non-privilégié avant de lancer quoi que ce soit.
if [ "$(id -u)" = "0" ]; then
  chgrp sokkan /etc/hosts 2>/dev/null && chmod 664 /etc/hosts 2>/dev/null || true
  exec setpriv --reuid=sokkan --regid=sokkan --init-groups "$0" "$@"
fi

# Guard d'écriture RÉEL : mkdir -p seul ne suffit pas — sur un vieux volume
# root-owned dont les sous-dossiers existent déjà, mkdir passe et l'app
# crash-loop plus loin (PermissionError sur /data/session-secret, constaté
# e2e 2026-07-22). On teste une écriture effective pour échouer ICI, avec le
# remède affiché.
if ! mkdir -p "$SOKKAN_DATA_DIR" "$SOKKAN_MEMORY_DIR" "$CLAUDE_CONFIG_DIR" 2>/dev/null \
   || ! touch "$SOKKAN_DATA_DIR/.writable-check" 2>/dev/null; then
  echo "[sokkan] ERROR: $SOKKAN_DATA_DIR is not writable by uid $(id -u)." >&2
  echo "[sokkan] Data volumes created by SOKKAN <= v0.1.0 are owned by root; fix once with:" >&2
  echo "[sokkan]   docker compose run --rm --user root api chown -R 1000:1000 /data" >&2
  exit 1
fi
rm -f "$SOKKAN_DATA_DIR/.writable-check"

# l'index mémoire (boot + réindexation périodique) tourne DANS le backend :
# thread daemon lancé au startup FastAPI, modèle d'embeddings gardé chaud,
# réindex seulement quand le corpus change (SOKKAN_REINDEX_S, défaut 120 s).
exec uvicorn app:app --host 0.0.0.0 --port 8097 --app-dir /app/backend
