#!/bin/sh
# sokkan restore — put a set written by scripts/backup.sh back. DESTRUCTIVE.
#
#   ./scripts/restore.sh <backup-set-dir> --yes [--vault-key FILE] [--keys-dir DIR]
#                        [--import-secrets [--overwrite-secrets]] [--force-version]
#
# Checks first, touches nothing until all pass: every sha256 of the MANIFEST, the SOKKAN
# version (VERSION here must equal the set's, unless --force-version), and the keys:
#   file provider: vault.key / forge.key / teams.key (<key>.enc decrypted with
#     SOKKAN_BACKUP_KEY_PASSPHRASE / _PASSFILE, or clear in the set, or --vault-key FILE,
#     or --keys-dir DIR holding them);
#   openbao: every *.key.wrapped of data.tgz must unwrap with the OpenBao configured here
#     (SOKKAN_OPENBAO_*; compose: the api service's) — the set holds no key;
#     --import-secrets puts secrets.openbao.json back in KV (missing names; with
#     --overwrite-secrets, every name). Then:
#   compose: `docker compose stop api web`, /data emptied and replaced by data.tgz,
#            vault.key written (0600), pg_restore --clean --if-exists in `db`, `up -d`
#   local:   SOKKAN_DATA_DIR emptied and replaced, vault.key, pg_restore to
#            SOKKAN_BACKUP_PG_DSN (or CORTHEXIS_DATABASE_URL / SOKKAN_DATABASE_URL);
#            stop the API yourself first
# --yes (or SOKKAN_RESTORE_YES=1) is required. Without any vault key the current
# vault.key is kept (if any) and a warning says the secrets may be unreadable.
# Mode: SOKKAN_BACKUP_MODE (compose | local), same detection as backup.sh.
set -eu
umask 077

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SETDIR=""
YES="${SOKKAN_RESTORE_YES:-0}"
FORCE_VERSION=0
KEYFILE=""
KEYSDIR=""
IMPORT_SECRETS=0
OVERWRITE_SECRETS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES=1 ;;
    --force-version) FORCE_VERSION=1 ;;
    --vault-key) [ $# -ge 2 ] || { echo "restore: --vault-key needs a file" >&2; exit 2; }; KEYFILE="$2"; shift ;;
    --keys-dir) [ $# -ge 2 ] || { echo "restore: --keys-dir needs a directory" >&2; exit 2; }; KEYSDIR="$2"; shift ;;
    --import-secrets) IMPORT_SECRETS=1 ;;
    --overwrite-secrets) OVERWRITE_SECRETS=1 ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    -*) echo "restore: unknown option $1" >&2; exit 2 ;;
    *) SETDIR="$1" ;;
  esac
  shift
done
log() { echo "restore: $*" >&2; }
die() { echo "restore: ERROR: $*" >&2; exit 1; }
[ -n "$SETDIR" ] || { sed -n '2,26p' "$0"; exit 2; }
[ -d "$SETDIR" ] || die "$SETDIR is not a directory"
SETDIR="$(cd "$SETDIR" && pwd)"
M="$SETDIR/MANIFEST"
[ -f "$M" ] || die "no MANIFEST in $SETDIR"
field() { sed -n "s/^$1: *//p" "$M" | head -n 1; }
[ "$(field format)" = "sokkan-backup/1" ] || die "MANIFEST: unknown format"

if command -v sha256sum >/dev/null 2>&1; then sha() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
else die "sha256sum (or shasum) is required"; fi

# --- 1. integrity: every listed file matches, every file is listed --------------------
sed -n '/^sha256:$/,$p' "$M" | sed 1d > "${TMPDIR:-/tmp}/.sokkan-restore-sums.$$"
SUMS="${TMPDIR:-/tmp}/.sokkan-restore-sums.$$"
trap 'rm -f "$SUMS"' EXIT INT TERM
[ -s "$SUMS" ] || die "MANIFEST lists no file"
BAD=0
while read -r h f; do
  case "$f" in ''|*/*|.*) log "MANIFEST: invalid entry '$f'"; BAD=1; continue ;; esac
  if [ ! -f "$SETDIR/$f" ]; then log "missing: $f"; BAD=1
  elif [ "$(sha "$SETDIR/$f")" != "$h" ]; then log "checksum mismatch: $f"; BAD=1; fi
done < "$SUMS"
for p in "$SETDIR"/*; do
  b="$(basename "$p")"
  [ "$b" = MANIFEST ] && continue
  grep -q "  $b\$" "$SUMS" || { log "not in MANIFEST: $b"; BAD=1; }
done
[ "$BAD" = 0 ] || die "integrity check FAILED — nothing was changed"
grep -q '  data\.tgz$' "$SUMS" || die "the set has no data.tgz"
log "integrity OK ($(wc -l < "$SUMS" | tr -d ' ') files)"

# --- 2. version ------------------------------------------------------------------------
BVER="$(field sokkan_version)"
HVER="$(cat "$HERE/VERSION" 2>/dev/null || echo unknown)"
if [ "$BVER" != "$HVER" ]; then
  [ "$FORCE_VERSION" = 1 ] || die "backup is SOKKAN $BVER, this folder is $HVER — install $BVER first (or --force-version)"
  log "WARNING: version $BVER restored on $HVER (--force-version)"
fi

# --- mode ------------------------------------------------------------------------------
COMPOSE_DIR=""
if [ -f ./docker-compose.yml ]; then COMPOSE_DIR="$(pwd)"; elif [ -f "$HERE/docker-compose.yml" ]; then COMPOSE_DIR="$HERE"; fi
MODE="${SOKKAN_BACKUP_MODE:-}"
if [ -z "$MODE" ]; then
  MODE=local
  [ -n "$COMPOSE_DIR" ] && command -v docker >/dev/null 2>&1 &&
    (cd "$COMPOSE_DIR" && docker compose ps --services 2>/dev/null) | grep -qxE 'api|db' && MODE=compose
fi
dc() { (cd "$COMPOSE_DIR" && docker compose "$@"); }
DSN=""
case "$MODE" in
  compose) [ -n "$COMPOSE_DIR" ] || die "compose mode: no docker-compose.yml here or in $HERE" ;;
  local)
    D="${SOKKAN_DATA_DIR:-}"
    [ -n "$D" ] || die "local mode: set SOKKAN_DATA_DIR"
    mkdir -p "$D"
    D="$(cd "$D" && pwd)"
    case "$D" in /|"$HOME"|/root|/home|/usr|/etc|/var) die "refusing to empty SOKKAN_DATA_DIR=$D" ;; esac
    DSN="${SOKKAN_BACKUP_PG_DSN:-${CORTHEXIS_DATABASE_URL:-${SOKKAN_DATABASE_URL:-}}}"
    DSN="$(printf '%s' "$DSN" | sed 's#^postgres[a-z]*+[a-z0-9]*://#postgresql://#')" ;;
  *) die "SOKKAN_BACKUP_MODE must be compose or local" ;;
esac
HAS_PG=0
grep -q '  pg\.dump$' "$SUMS" && HAS_PG=1
if [ "$HAS_PG" = 1 ] && [ "$MODE" = local ]; then
  [ -n "$DSN" ] || die "the set has pg.dump: set SOKKAN_BACKUP_PG_DSN (target database)"
  command -v pg_restore >/dev/null 2>&1 || die "pg_restore is required"
fi

# --- 3. keys (decrypted / checked in a private temp dir before anything is touched) ------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; rm -f "$SUMS"' EXIT INT TERM
SECRETS="$(field secrets_provider)"
SECRETS="${SECRETS:-file}"
PY="${SOKKAN_PYTHON:-python3}"
KEYFILES="vault.key forge.key teams.key"
KEYS_OK=""
mkdir "$WORK/keys" "$WORK/wrapped"
passarg() {
  if [ -n "${SOKKAN_BACKUP_KEY_PASSFILE:-}" ]; then echo "file:$SOKKAN_BACKUP_KEY_PASSFILE"
  elif [ -n "${SOKKAN_BACKUP_KEY_PASSPHRASE:-}" ]; then echo "env:SOKKAN_BACKUP_KEY_PASSPHRASE"
  fi
}
case "$SECRETS" in
  file)
    for k in $KEYFILES; do
      if [ "$k" = vault.key ] && [ -n "$KEYFILE" ]; then
        [ -s "$KEYFILE" ] || die "--vault-key $KEYFILE: missing or empty"
        cp "$KEYFILE" "$WORK/keys/$k"
      elif [ -n "$KEYSDIR" ] && [ -s "$KEYSDIR/$k" ]; then
        cp "$KEYSDIR/$k" "$WORK/keys/$k"
      elif [ -f "$SETDIR/$k.enc" ]; then
        PASSARG="$(passarg)"
        [ -n "$PASSARG" ] || die "the set has $k.enc: set SOKKAN_BACKUP_KEY_PASSPHRASE (or _PASSFILE), or give --vault-key / --keys-dir"
        openssl enc -d -aes-256-cbc -pbkdf2 -in "$SETDIR/$k.enc" -out "$WORK/keys/$k" -pass "$PASSARG" 2>/dev/null ||
          die "cannot decrypt $k.enc (wrong passphrase?) — nothing was changed"
      elif [ -f "$SETDIR/$k" ]; then
        cp "$SETDIR/$k" "$WORK/keys/$k"
      else
        continue
      fi
      KEYS_OK="$KEYS_OK $k"
    done
    case " $KEYS_OK " in
      *" vault.key "*) ;;
      *) log "WARNING: no vault key in the set and no --vault-key: the current vault.key (if any) is kept; secrets in vault.json are unreadable unless it is the original key" ;;
    esac ;;
  openbao)
    [ -z "$KEYFILE$KEYSDIR" ] || die "the set is from the openbao provider: it has no key file to replace (--vault-key / --keys-dir refused)"
    tar xzf "$SETDIR/data.tgz" -C "$WORK/wrapped" --wildcards '*.key.wrapped' 2>/dev/null ||
      die "the set says openbao but data.tgz holds no *.key.wrapped"
    if [ "$MODE" = compose ]; then
      # shellcheck disable=SC2016
      (cd "$WORK/wrapped" && tar czf - .) | dc run --rm -T --no-deps --entrypoint sh api -c \
        'mkdir -p /tmp/w && tar xzf - -C /tmp/w && cd /app/backend && python3 -m secrets_provider.cli unwrap-check --dir /tmp/w' ||
        die "the wrapped keys do not unwrap with the OpenBao of the api service — nothing was changed (OPENBAO_REQUIRED.txt)"
    else
      "$PY" "$HERE/backend/secrets_provider/cli.py" unwrap-check --dir "$WORK/wrapped" ||
        die "the wrapped keys do not unwrap with this OpenBao (SOKKAN_OPENBAO_*) — nothing was changed (OPENBAO_REQUIRED.txt)"
    fi
    if [ "$IMPORT_SECRETS" = 1 ] && [ ! -f "$SETDIR/secrets.openbao.json" ]; then
      die "--import-secrets: the set has no secrets.openbao.json (backup taken with --no-secrets-export)"
    fi ;;
  kubernetes)
    log "the set is from the kubernetes provider: keys and secrets are Secrets of the cluster (restore them with the cluster backup)" ;;
  *) die "MANIFEST: unknown secrets_provider $SECRETS" ;;
esac
KEYS_OK="${KEYS_OK# }"

# --- 4. confirmation --------------------------------------------------------------------
if [ "$YES" != 1 ]; then
  die "restoring REPLACES the data$( [ "$HAS_PG" = 1 ] && echo ' and the memory database') of this instance — re-run with --yes"
fi
log "restoring $SETDIR (SOKKAN $BVER, mode $MODE)"

# --- 5. data, keys, Postgres -------------------------------------------------------------
# file provider: a key the set does not bring is KEPT from the current data (as before);
# openbao / kubernetes: no clear key is kept or written (the data dir is emptied)
KEEP=""
if [ "$SECRETS" = file ]; then
  for k in $KEYFILES; do case " $KEYS_OK " in *" $k "*) ;; *) KEEP="$KEEP $k" ;; esac; done
fi
if [ "$MODE" = compose ]; then
  dc stop api web
  # shellcheck disable=SC2016  # expanded by the container's sh
  dc run --rm -T --no-deps --entrypoint sh api -c '
    set -e
    mkdir -p /tmp/kept
    for k in $1; do [ -f "/data/$k" ] && cp -p "/data/$k" "/tmp/kept/$k"; done
    find /data -mindepth 1 -delete
    cd /data && tar xzf -
    for k in $1; do [ -f "/tmp/kept/$k" ] && cp -p "/tmp/kept/$k" "/data/$k"; done
    chown 1000:1000 /data' sh "$KEEP" < "$SETDIR/data.tgz"
  for k in $KEYS_OK; do
    dc run --rm -T --no-deps --entrypoint sh api -c \
      "cat > /data/$k && chmod 600 /data/$k && chown 1000:1000 /data/$k" < "$WORK/keys/$k"
  done
  if [ "$HAS_PG" = 1 ]; then
    dc up -d db
    i=0; until dc exec -T db pg_isready -U "${POSTGRES_USER:-sokkan}" >/dev/null 2>&1; do
      i=$((i + 1)); [ "$i" -lt 60 ] || die "db not ready"; sleep 1; done
    dc exec -T db pg_restore -U "${POSTGRES_USER:-sokkan}" -d "${POSTGRES_DB:-sokkan}" \
      --clean --if-exists --no-owner < "$SETDIR/pg.dump" || die "pg_restore failed"
  fi
  dc up -d
  if [ "$SECRETS" = openbao ] && [ "$IMPORT_SECRETS" = 1 ]; then
    dc exec -T -w /app/backend api python3 -m secrets_provider.cli backup-import --in - \
      $( [ "$OVERWRITE_SECRETS" = 1 ] && echo --overwrite ) < "$SETDIR/secrets.openbao.json" ||
      die "secrets re-import into OpenBao failed (data restored; re-run: secrets_provider.cli backup-import)"
  fi
else
  mkdir -p "$WORK/kept"
  for k in $KEEP; do [ -f "$D/$k" ] && cp -p "$D/$k" "$WORK/kept/$k"; done
  find "$D" -mindepth 1 -delete
  tar xzf "$SETDIR/data.tgz" -C "$D"
  for k in $KEEP; do [ -f "$WORK/kept/$k" ] && cp -p "$WORK/kept/$k" "$D/$k"; done
  for k in $KEYS_OK; do cp "$WORK/keys/$k" "$D/$k"; done
  for k in $KEYFILES; do [ -f "$D/$k" ] && chmod 600 "$D/$k"; done
  if [ "$HAS_PG" = 1 ]; then
    pg_restore --clean --if-exists --no-owner -d "$DSN" "$SETDIR/pg.dump" || die "pg_restore failed"
  fi
  if [ "$SECRETS" = openbao ] && [ "$IMPORT_SECRETS" = 1 ]; then
    if [ "$OVERWRITE_SECRETS" = 1 ]; then
      "$PY" "$HERE/backend/secrets_provider/cli.py" backup-import --in "$SETDIR/secrets.openbao.json" --overwrite
    else
      "$PY" "$HERE/backend/secrets_provider/cli.py" backup-import --in "$SETDIR/secrets.openbao.json"
    fi || die "secrets re-import into OpenBao failed (data restored; re-run: secrets_provider.cli backup-import)"
  fi
fi
if [ "$SECRETS" = openbao ] && [ "$IMPORT_SECRETS" != 1 ] && [ -f "$SETDIR/secrets.openbao.json" ]; then
  log "project secrets NOT re-imported (OpenBao still holds them); --import-secrets puts the set's copy back"
fi
[ "$HAS_PG" = 1 ] || log "the set has no pg.dump (SQLite-only install): memory database not touched"
log "done. Check: ./scripts/doctor.sh, then log in and open a secret, a board card and a memory note"
