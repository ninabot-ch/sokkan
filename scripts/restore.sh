#!/bin/sh
# sokkan restore — put a set written by scripts/backup.sh back. DESTRUCTIVE.
#
#   ./scripts/restore.sh <backup-set-dir> --yes [--vault-key FILE] [--force-version]
#
# Checks first, touches nothing until all pass: every sha256 of the MANIFEST, the SOKKAN
# version (VERSION here must equal the set's, unless --force-version), and the vault key
# (vault.key.enc decrypted with SOKKAN_BACKUP_KEY_PASSPHRASE / SOKKAN_BACKUP_KEY_PASSFILE,
# or a clear vault.key in the set, or --vault-key FILE). Then:
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
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES=1 ;;
    --force-version) FORCE_VERSION=1 ;;
    --vault-key) [ $# -ge 2 ] || { echo "restore: --vault-key needs a file" >&2; exit 2; }; KEYFILE="$2"; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    -*) echo "restore: unknown option $1" >&2; exit 2 ;;
    *) SETDIR="$1" ;;
  esac
  shift
done
log() { echo "restore: $*" >&2; }
die() { echo "restore: ERROR: $*" >&2; exit 1; }
[ -n "$SETDIR" ] || { sed -n '2,18p' "$0"; exit 2; }
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

# --- 3. vault key (decrypted into a private temp file before anything is touched) ------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; rm -f "$SUMS"' EXIT INT TERM
KEY=""
if [ -n "$KEYFILE" ]; then
  [ -s "$KEYFILE" ] || die "--vault-key $KEYFILE: missing or empty"
  cp "$KEYFILE" "$WORK/vault.key"; KEY="$WORK/vault.key"
elif [ -f "$SETDIR/vault.key.enc" ]; then
  if [ -n "${SOKKAN_BACKUP_KEY_PASSFILE:-}" ]; then PASSARG="file:$SOKKAN_BACKUP_KEY_PASSFILE"
  elif [ -n "${SOKKAN_BACKUP_KEY_PASSPHRASE:-}" ]; then PASSARG="env:SOKKAN_BACKUP_KEY_PASSPHRASE"
  else die "the set has vault.key.enc: set SOKKAN_BACKUP_KEY_PASSPHRASE (or _PASSFILE), or give --vault-key"; fi
  openssl enc -d -aes-256-cbc -pbkdf2 -in "$SETDIR/vault.key.enc" -out "$WORK/vault.key" -pass "$PASSARG" 2>/dev/null ||
    die "cannot decrypt vault.key.enc (wrong passphrase?) — nothing was changed"
  KEY="$WORK/vault.key"
elif [ -f "$SETDIR/vault.key" ]; then
  KEY="$SETDIR/vault.key"
fi
[ -n "$KEY" ] || log "WARNING: no vault key in the set and no --vault-key: the current vault.key (if any) is kept; secrets in vault.json are unreadable unless it is the original key"

# --- 4. confirmation --------------------------------------------------------------------
if [ "$YES" != 1 ]; then
  die "restoring REPLACES the data$( [ "$HAS_PG" = 1 ] && echo ' and the memory database') of this instance — re-run with --yes"
fi
log "restoring $SETDIR (SOKKAN $BVER, mode $MODE)"

# --- 5. data, key, Postgres -------------------------------------------------------------
if [ "$MODE" = compose ]; then
  dc stop api web
  # shellcheck disable=SC2016  # expanded by the container's sh
  dc run --rm -T --no-deps --entrypoint sh api -c '
    set -e
    [ -f /data/vault.key ] && cp -p /data/vault.key /tmp/kept.key
    find /data -mindepth 1 -delete
    cd /data && tar xzf -
    if [ "$1" = keep ] && [ -f /tmp/kept.key ]; then cp -p /tmp/kept.key /data/vault.key; fi
    chown 1000:1000 /data' sh "$( [ -n "$KEY" ] && echo replace || echo keep )" < "$SETDIR/data.tgz"
  if [ -n "$KEY" ]; then
    dc run --rm -T --no-deps --entrypoint sh api -c \
      'cat > /data/vault.key && chmod 600 /data/vault.key && chown 1000:1000 /data/vault.key' < "$KEY"
  fi
  if [ "$HAS_PG" = 1 ]; then
    dc up -d db
    i=0; until dc exec -T db pg_isready -U "${POSTGRES_USER:-sokkan}" >/dev/null 2>&1; do
      i=$((i + 1)); [ "$i" -lt 60 ] || die "db not ready"; sleep 1; done
    dc exec -T db pg_restore -U "${POSTGRES_USER:-sokkan}" -d "${POSTGRES_DB:-sokkan}" \
      --clean --if-exists --no-owner < "$SETDIR/pg.dump" || die "pg_restore failed"
  fi
  dc up -d
else
  [ -f "$D/vault.key" ] && [ -z "$KEY" ] && cp -p "$D/vault.key" "$WORK/kept.key"
  find "$D" -mindepth 1 -delete
  tar xzf "$SETDIR/data.tgz" -C "$D"
  if [ -n "$KEY" ]; then cp "$KEY" "$D/vault.key"
  elif [ -f "$WORK/kept.key" ]; then cp "$WORK/kept.key" "$D/vault.key"; fi
  [ -f "$D/vault.key" ] && chmod 600 "$D/vault.key"
  if [ "$HAS_PG" = 1 ]; then
    pg_restore --clean --if-exists --no-owner -d "$DSN" "$SETDIR/pg.dump" || die "pg_restore failed"
  fi
fi
[ "$HAS_PG" = 1 ] || log "the set has no pg.dump (SQLite-only install): memory database not touched"
log "done. Check: ./scripts/doctor.sh, then log in and open a secret, a board card and a memory note"
