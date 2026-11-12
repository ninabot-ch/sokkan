#!/bin/sh
set -e

# Démarré root (cf. api.Dockerfile) : on rend /etc/hosts éditable par le groupe
# sokkan — le backend y maintient les entrées `<name>.fleet` en mode managé —
# puis on DROP vers l'utilisateur non-privilégié avant de lancer quoi que ce soit.
if [ "$(id -u)" = "0" ]; then
  chgrp sokkan /etc/hosts 2>/dev/null && chmod 664 /etc/hosts 2>/dev/null || true
  # /workspace créé par l'installeur (souvent root) → l'agent uid 1000 ne peut
  # pas écrire (Write → EACCES, constaté e2e 2026-09-10). On ne chown QUE le
  # dossier top-level ET seulement s'il appartient à root : le projet d'un
  # utilisateur (son uid) n'est jamais touché, et jamais récursivement.
  if [ -d /workspace ] && [ "$(stat -c %u /workspace 2>/dev/null)" = "0" ]; then
    chown sokkan:sokkan /workspace 2>/dev/null || true
  fi
  # /data created by SOKKAN <= 0.1.0 (it ran as root): handed over to uid 1000 once, so
  # that an update from 0.1.0 starts without a manual chown. Only SOKKAN's own volume.
  D="${SOKKAN_DATA_DIR:-/data}"
  if [ -d "$D" ] && { [ "$(stat -c %u "$D" 2>/dev/null)" = "0" ] \
       || [ -n "$(find "$D" -maxdepth 3 -xdev -uid 0 -print -quit 2>/dev/null)" ]; }; then
    echo "[sokkan] $D has files owned by root (volume of SOKKAN <= 0.1.0): chown to uid 1000" >&2
    chown -R sokkan:sokkan "$D" 2>/dev/null || true
  fi
  # 3.6.6: the secret the local router presents to the api (« Write with Claude »), created
  # once in the volume the two share — by the code of the release that runs, whatever script
  # did the upgrade (a 3.6.4 or 3.6.5 upgrade.sh never wrote SOKKAN_ROUTER_SECRET to .env).
  # Never printed; readable by uid 1000 only (the api and the router).
  RS=/run/sokkan-router
  if [ -d "$RS" ]; then
    if [ ! -s "$RS/secret" ]; then
      ( umask 077; head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$RS/secret.tmp" && mv "$RS/secret.tmp" "$RS/secret" ) || true
    fi
    chown sokkan:sokkan "$RS" "$RS/secret" 2>/dev/null || true
    chmod 700 "$RS" 2>/dev/null || true; chmod 600 "$RS/secret" 2>/dev/null || true
  fi
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
