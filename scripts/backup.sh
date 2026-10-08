#!/bin/sh
# sokkan backup — one timestamped, verifiable backup set, without stopping the API.
#
#   ./scripts/backup.sh [OUTPUT_DIR] [--include-plain-key] [--no-secrets-export]
#
# Writes OUTPUT_DIR/sokkan-backup-<UTC stamp>/ (OUTPUT_DIR: argument, else
# $SOKKAN_BACKUP_DIR, else ./backups), directory 0700, files 0600:
#   pg.dump        pg_dump -Fc of the CortHeXis memory store (skipped when no Postgres)
#   data.tgz       the whole data directory (/data = SOKKAN_DATA_DIR) WITHOUT any key file
#                  (vault.key, forge.key, teams.key); SQLite databases are copied with the
#                  online backup API (consistent while the API runs)
#   MANIFEST       SOKKAN version, date, mode, secrets provider, sha256 of every file
# Keys, by secrets provider (docs/enterprise/SECRETS.md):
#   file (default) <key>.enc for each key file, AES-256-CBC + PBKDF2 (openssl), when a
#                  passphrase is given: SOKKAN_BACKUP_KEY_PASSPHRASE or _PASSFILE;
#                  VAULT_KEY_NOT_INCLUDED.txt otherwise, unless --include-plain-key copies
#                  them in clear (0600)
#   openbao        (detected: *.key.wrapped in the data directory) NO key at all: data.tgz
#                  holds the transit-wrapped data keys, secrets.openbao.json the project
#                  secrets each encrypted by transit (--no-secrets-export: skipped);
#                  OPENBAO_REQUIRED.txt says what the restore needs (the same OpenBao, or
#                  its snapshot, with the transit key). --include-plain-key is refused.
#   kubernetes     (SOKKAN_SECRETS_PROVIDER=kubernetes) no key: the Secrets live in the
#                  cluster — KUBERNETES_SECRETS_NOT_INCLUDED.txt says which to back up
#
# Modes (SOKKAN_BACKUP_MODE):
#   compose (default when docker-compose.yml is here and the stack runs): pg_dump in the
#           `db` service, data read from the `api` service's /data volume
#   local   SOKKAN_DATA_DIR on this host; Postgres from SOKKAN_BACKUP_PG_DSN (or
#           CORTHEXIS_DATABASE_URL / SOKKAN_DATABASE_URL); none = SQLite-only install
# Retention: SOKKAN_BACKUP_KEEP sets (default 14) are kept in OUTPUT_DIR; older
# sokkan-backup-* sets are deleted (nothing else is ever touched). 0 = keep all.
# Restore: ./scripts/restore.sh <set>. Docs: docs/enterprise/OPERATIONS.md § 5.
set -eu
umask 077

HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT=""
PLAIN_KEY=0
SECRETS_EXPORT=1
for a in "$@"; do
  case "$a" in
    --include-plain-key) PLAIN_KEY=1 ;;
    --no-secrets-export) SECRETS_EXPORT=0 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    -*) echo "backup: unknown option $a" >&2; exit 2 ;;
    *) OUT="$a" ;;
  esac
done
OUT="${OUT:-${SOKKAN_BACKUP_DIR:-./backups}}"
KEEP="${SOKKAN_BACKUP_KEEP:-14}"
case "$KEEP" in ''|*[!0-9]*) echo "backup: SOKKAN_BACKUP_KEEP must be a number" >&2; exit 2 ;; esac

log() { echo "backup: $*" >&2; }
die() { echo "backup: ERROR: $*" >&2; exit 1; }

if command -v sha256sum >/dev/null 2>&1; then sha() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
else die "sha256sum (or shasum) is required"; fi

# --- mode -------------------------------------------------------------------------
COMPOSE_DIR=""
if [ -f ./docker-compose.yml ]; then COMPOSE_DIR="$(pwd)"; elif [ -f "$HERE/docker-compose.yml" ]; then COMPOSE_DIR="$HERE"; fi
MODE="${SOKKAN_BACKUP_MODE:-}"
if [ -z "$MODE" ]; then
  MODE=local
  if [ -n "$COMPOSE_DIR" ] && command -v docker >/dev/null 2>&1 &&
     (cd "$COMPOSE_DIR" && docker compose ps --services --status running 2>/dev/null) | grep -qxE 'api|db'; then
    MODE=compose
  fi
fi
dc() { (cd "$COMPOSE_DIR" && docker compose "$@"); }
case "$MODE" in
  compose)
    [ -n "$COMPOSE_DIR" ] || die "compose mode: no docker-compose.yml here or in $HERE"
    RUNNING="$(dc ps --services --status running 2>/dev/null || true)" ;;
  local)
    [ -n "${SOKKAN_DATA_DIR:-}" ] || die "local mode: set SOKKAN_DATA_DIR (the instance's data directory)"
    [ -d "$SOKKAN_DATA_DIR" ] || die "SOKKAN_DATA_DIR=$SOKKAN_DATA_DIR is not a directory" ;;
  *) die "SOKKAN_BACKUP_MODE must be compose or local" ;;
esac
command -v python3 >/dev/null 2>&1 || [ "$MODE" = compose ] || die "python3 is required"

# --- secrets provider (decides what happens to the keys) ----------------------------------
KEYFILES="vault.key forge.key teams.key"
if [ "$MODE" = compose ]; then
  PROBE="$(dc exec -T api sh -c 'ls /data/*.key.wrapped 2>/dev/null; echo "env=${SOKKAN_SECRETS_PROVIDER:-}"' 2>/dev/null ||
           dc run --rm -T --no-deps --entrypoint sh api -c 'ls /data/*.key.wrapped 2>/dev/null; echo "env=${SOKKAN_SECRETS_PROVIDER:-}"' 2>/dev/null || true)"
else
  PROBE="$(ls "$SOKKAN_DATA_DIR"/*.key.wrapped 2>/dev/null || true)
env=${SOKKAN_SECRETS_PROVIDER:-}"
fi
SECRETS=file
if printf '%s\n' "$PROBE" | grep -q '\.key\.wrapped$'; then SECRETS=openbao
elif printf '%s\n' "$PROBE" | grep -qx 'env=kubernetes'; then SECRETS=kubernetes; fi
if [ "$SECRETS" != file ] && [ "$PLAIN_KEY" = 1 ]; then
  die "--include-plain-key: the $SECRETS provider keeps no key in the data directory"
fi
PY="${SOKKAN_PYTHON:-python3}"

PASSARG=""
if [ -n "${SOKKAN_BACKUP_KEY_PASSFILE:-}" ]; then
  [ -r "$SOKKAN_BACKUP_KEY_PASSFILE" ] || die "SOKKAN_BACKUP_KEY_PASSFILE is not readable"
  PASSARG="file:$SOKKAN_BACKUP_KEY_PASSFILE"
elif [ -n "${SOKKAN_BACKUP_KEY_PASSPHRASE:-}" ]; then
  PASSARG="env:SOKKAN_BACKUP_KEY_PASSPHRASE"
fi
[ -z "$PASSARG" ] || command -v openssl >/dev/null 2>&1 || die "openssl is required to encrypt vault.key"

# --- output set (written as .partial, renamed at the end) ---------------------------
mkdir -p "$OUT"; chmod 700 "$OUT"
OUT="$(cd "$OUT" && pwd)"
while :; do
  STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  [ -e "$OUT/sokkan-backup-$STAMP" ] || [ -e "$OUT/.sokkan-backup-$STAMP.partial" ] || break
  sleep 1
done
NAME="sokkan-backup-$STAMP"
SET="$OUT/.$NAME.partial"
mkdir -m 700 "$SET"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP" "$SET"' EXIT INT TERM
log "mode $MODE → $OUT/$NAME"

# --- stager: consistent copy of the data dir, streamed as tar.gz on stdout -----------
# SQLite files (detected by header) go through the online backup API (sqlite3 CLI
# `.backup` when present, else Python's sqlite3); their -wal/-shm/-journal companions are
# skipped; vault.key at the root is never included. Lines "SQLITE <path>" on stderr.
cat > "$TMP/stage.py" <<'PY'
import os, shutil, sqlite3, subprocess, sys, tarfile, tempfile
root = sys.argv[1]
# key files are never in data.tgz (file provider: backed up apart, encrypted; openbao: only
# their transit-wrapped form, *.key.wrapped, which is no key)
KEYS = {k + s for k in ("vault.key", "forge.key", "teams.key") for s in ("", ".next", ".tmp")}
tmp = tempfile.mkdtemp(prefix="sokkan-stage-")
try:
    def is_sqlite(p):
        try:
            with open(p, "rb") as f:
                return f.read(16) == b"SQLite format 3\x00"
        except OSError:
            return False
    dbs = set()
    for d, _dirs, files in os.walk(root):
        for n in files:
            p = os.path.join(d, n)
            if not os.path.islink(p) and os.path.isfile(p) and is_sqlite(p):
                dbs.add(p)
    companions = {p + s for p in dbs for s in ("-wal", "-shm", "-journal")}
    cli = shutil.which("sqlite3")
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as tar:
        for d, dirs, files in os.walk(root):
            dirs.sort()
            rel_d = os.path.relpath(d, root)
            if rel_d != ".":
                tar.add(d, arcname=rel_d, recursive=False)
            for n in sorted(files):
                p = os.path.join(d, n)
                rel = os.path.relpath(p, root)
                if rel in KEYS or p in companions:
                    continue
                if not (os.path.islink(p) or os.path.isfile(p)):
                    continue  # sockets, fifos
                if p in dbs:
                    copy = os.path.join(tmp, "db")
                    if cli:
                        subprocess.run([cli, p, ".backup '%s'" % copy], check=True)
                    else:
                        src = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
                        dst = sqlite3.connect(copy)
                        with dst:
                            src.backup(dst)
                        src.close()
                        dst.close()
                    st = os.stat(p)
                    info = tar.gettarinfo(copy, arcname=rel)
                    info.uid, info.gid, info.mode, info.mtime = st.st_uid, st.st_gid, st.st_mode & 0o7777, st.st_mtime
                    with open(copy, "rb") as f:
                        tar.addfile(info, f)
                    os.unlink(copy)
                    print("SQLITE " + rel, file=sys.stderr)
                else:
                    tar.add(p, arcname=rel, recursive=False)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
PY

# --- data ------------------------------------------------------------------------
if [ "$MODE" = compose ]; then
  if echo "$RUNNING" | grep -qx api; then
    dc exec -T api python3 - /data < "$TMP/stage.py" > "$SET/data.tgz" 2> "$TMP/stage.err" ||
      { cat "$TMP/stage.err" >&2; die "data copy failed"; }
    for k in $KEYFILES; do dc exec -T api sh -c "cat /data/$k 2>/dev/null || true" > "$TMP/$k"; done
  else
    dc run --rm -T --no-deps --entrypoint python3 api - /data < "$TMP/stage.py" > "$SET/data.tgz" 2> "$TMP/stage.err" ||
      { cat "$TMP/stage.err" >&2; die "data copy failed"; }
    for k in $KEYFILES; do dc run --rm -T --no-deps --entrypoint sh api -c "cat /data/$k 2>/dev/null || true" > "$TMP/$k"; done
  fi
else
  python3 "$TMP/stage.py" "$SOKKAN_DATA_DIR" > "$SET/data.tgz" 2> "$TMP/stage.err" ||
    { cat "$TMP/stage.err" >&2; die "data copy failed"; }
  for k in $KEYFILES; do
    if [ -f "$SOKKAN_DATA_DIR/$k" ]; then cp "$SOKKAN_DATA_DIR/$k" "$TMP/$k"; else : > "$TMP/$k"; fi
  done
fi
grep -v '^SQLITE ' "$TMP/stage.err" >&2 || true
SQLITE_LIST="$(sed -n 's/^SQLITE //p' "$TMP/stage.err" | tr '\n' ' ' | sed 's/ $//')"
log "data.tgz written (SQLite: ${SQLITE_LIST:-none})"

# --- Postgres (CortHeXis) ----------------------------------------------------------
PG=skipped
if [ "$MODE" = compose ]; then
  if echo "$RUNNING" | grep -qx db; then
    dc exec -T db pg_dump -U "${POSTGRES_USER:-sokkan}" -Fc "${POSTGRES_DB:-sokkan}" > "$SET/pg.dump" ||
      die "pg_dump failed"
    PG=included
  else
    log "service db is not running: Postgres dump SKIPPED"
  fi
else
  DSN="${SOKKAN_BACKUP_PG_DSN:-${CORTHEXIS_DATABASE_URL:-${SOKKAN_DATABASE_URL:-}}}"
  if [ -n "$DSN" ]; then
    command -v pg_dump >/dev/null 2>&1 || die "pg_dump is required (Postgres DSN configured)"
    DSN="$(printf '%s' "$DSN" | sed 's#^postgres[a-z]*+[a-z0-9]*://#postgresql://#')"
    pg_dump -Fc -d "$DSN" -f "$SET/pg.dump" || die "pg_dump failed"
    PG=included
  else
    log "no Postgres configured (SQLite-only install): dump SKIPPED"
  fi
fi

# --- keys ---------------------------------------------------------------------------
KEYS_IN=""
if [ "$SECRETS" != file ]; then
  KEY=$SECRETS
  for k in $KEYFILES; do
    if [ -s "$TMP/$k" ]; then
      log "WARNING: clear $k in the data directory although the provider is $SECRETS — NOT backed up (finish the migration: secrets-migrate.py --purge)"
    fi
  done
  if [ "$SECRETS" = openbao ]; then
    cat > "$SET/OPENBAO_REQUIRED.txt" <<EOF
This set holds NO key. The data keys in data.tgz (*.key.wrapped) and the project secrets in
secrets.openbao.json are encrypted by OpenBao transit: restoring requires the SAME OpenBao
(or a restore of its snapshot: bao operator raft snapshot save/restore) with the instance's
transit key and a token/AppRole allowed to decrypt with it.
At restore: ./scripts/restore.sh <set> --yes [--import-secrets]
  (restore.sh checks first that every wrapped key unwraps; nothing is touched otherwise)
EOF
    if [ "$SECRETS_EXPORT" = 1 ]; then
      if [ "$MODE" = compose ]; then
        dc exec -T -w /app/backend api python3 -m secrets_provider.cli backup-export --out - > "$SET/secrets.openbao.json" ||
          die "secrets export from OpenBao failed (--no-secrets-export to skip)"
      else
        "$PY" "$HERE/backend/secrets_provider/cli.py" backup-export --out "$SET/secrets.openbao.json" ||
          die "secrets export from OpenBao failed (--no-secrets-export to skip; SOKKAN_PYTHON = a python with the backend's packages)"
      fi
    fi
  else
    cat > "$SET/KUBERNETES_SECRETS_NOT_INCLUDED.txt" <<EOF
The kubernetes secrets provider keeps the data keys and the project secrets in Secrets of
the namespace: <prefix>-data-keys and <prefix>-vault-<project> (prefix SOKKAN_K8S_SECRETS_PREFIX,
default sokkan). Back them up with the cluster (Velero, etcd snapshot) — this set has no key.
EOF
  fi
else
  KEY=absent
  for k in $KEYFILES; do
    [ -s "$TMP/$k" ] || continue
    KEYS_IN="$KEYS_IN $k"
    if [ -n "$PASSARG" ]; then
      openssl enc -aes-256-cbc -pbkdf2 -salt -in "$TMP/$k" -out "$SET/$k.enc" -pass "$PASSARG" ||
        die "$k encryption failed"
      [ "$k" = vault.key ] && KEY=encrypted
    elif [ "$PLAIN_KEY" = 1 ]; then
      cp "$TMP/$k" "$SET/$k"
      [ "$k" = vault.key ] && KEY=plain
    else
      [ "$k" = vault.key ] && KEY=not-included
    fi
  done
  KEYS_IN="${KEYS_IN# }"
  if [ -z "$KEYS_IN" ]; then
    log "no key file in the data directory (empty vault)"
  elif [ -n "$PASSARG" ]; then
    :
  elif [ "$PLAIN_KEY" = 1 ]; then
    log "WARNING: $KEYS_IN copied IN CLEAR (--include-plain-key): whoever reads this set reads every secret"
  else
    [ "$KEY" = absent ] && KEY=not-included
    cat > "$SET/VAULT_KEY_NOT_INCLUDED.txt" <<EOF
The key files ($KEYS_IN) are NOT in this backup set: vault.key is the only key to vault.json
(the secrets) and modelkeys.json, forge.key to the forge tokens, teams.key to the Teams tokens.
Back them up separately (secrets manager, offline copy), or re-run the backup with
SOKKAN_BACKUP_KEY_PASSPHRASE / SOKKAN_BACKUP_KEY_PASSFILE to store them encrypted.
At restore: ./scripts/restore.sh <set> --vault-key <file> [--keys-dir <dir>] --yes
EOF
    log "keys ($KEYS_IN) NOT included — back them up separately (or set SOKKAN_BACKUP_KEY_PASSPHRASE)"
  fi
fi
# key material truncated now; the temp directory itself goes with the EXIT trap
for k in $KEYFILES; do : > "$TMP/$k"; done

# --- MANIFEST ------------------------------------------------------------------------
VERSION="$(cat "$HERE/VERSION" 2>/dev/null || echo unknown)"
{
  echo "format: sokkan-backup/1"
  echo "sokkan_version: $VERSION"
  echo "created_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "mode: $MODE"
  echo "postgres: $PG"
  echo "vault_key: $KEY"
  echo "secrets_provider: $SECRETS"
  echo "keys: $KEYS_IN"
  echo "sqlite: $SQLITE_LIST"
  echo "sha256:"
  for f in "$SET"/*; do
    b="$(basename "$f")"
    echo "$(sha "$f")  $b"
  done
} > "$TMP/MANIFEST"
mv "$TMP/MANIFEST" "$SET/MANIFEST"
chmod 600 "$SET"/*
mv "$SET" "$OUT/$NAME"
trap 'rm -rf "$TMP"' EXIT INT TERM
log "done: $OUT/$NAME"

# --- retention -----------------------------------------------------------------------
if [ "$KEEP" -gt 0 ]; then
  n="$(find "$OUT" -mindepth 1 -maxdepth 1 -type d -name 'sokkan-backup-*' | wc -l)"
  if [ "$n" -gt "$KEEP" ]; then
    find "$OUT" -mindepth 1 -maxdepth 1 -type d -name 'sokkan-backup-*' | sort |
      head -n "$((n - KEEP))" | while IFS= read -r old; do
        log "retention ($KEEP): removing $(basename "$old")"
        rm -rf "$old"
      done
  fi
fi
echo "$OUT/$NAME"
