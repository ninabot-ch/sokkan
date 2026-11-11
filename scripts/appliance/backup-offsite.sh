#!/bin/sh
# sokkan appliance — encrypted off-site backup, every night, restorable on a new machine.
#
#   backup-offsite.sh init                 create the encryption key (once) and the repository
#   backup-offsite.sh run                  one backup now (what the nightly timer calls)
#   backup-offsite.sh check                verify the repository, list the snapshots
#   backup-offsite.sh restore [snap] --yes fetch a snapshot (default: latest) and restore it
#   backup-offsite.sh status               last result, as the cockpit and the heartbeat see it
#
# How: scripts/backup.sh writes a consistent set (Postgres dump, data volume, key files
# encrypted) in a local folder; restic sends it, encrypted and deduplicated, to an
# S3-compatible bucket; old snapshots are thinned (7 daily, 4 weekly, 6 monthly).
#
# The key is generated ON THIS MACHINE (`init`) and never leaves it by itself:
#   /etc/sokkan/backup.key   (0600 root) — WITHOUT IT THE BACKUPS CANNOT BE READ, by anyone,
#   the supplier included. `init` prints it once: the client stores a copy away from the box
#   (password manager, sealed envelope). Losing the box AND the key = losing the data.
# Where: /etc/sokkan/backup.env (0600 root) —
#   RESTIC_REPOSITORY=s3:https://sos-ch-gva-2.exo.io/<bucket>/<instance>   (any S3 endpoint)
#   AWS_ACCESS_KEY_ID=…  AWS_SECRET_ACCESS_KEY=…     (a key limited to that bucket)
#   optional: SOKKAN_BACKUP_KEEP_DAILY=7 _WEEKLY=4 _MONTHLY=6, SOKKAN_BACKUP_LOCAL=/var/backups/sokkan
# Needs restic (0.16+) on the host; SOKKAN_RESTIC overrides the binary.
# Exit: 0 done · 1 failed (the status file says why) · 2 usage / not configured.
set -eu
umask 077

HERE="$(cd "$(dirname "$0")/../.." && pwd)"
CONF="${SOKKAN_BACKUP_ENV:-/etc/sokkan/backup.env}"
KEY="${SOKKAN_BACKUP_KEYFILE:-/etc/sokkan/backup.key}"
[ -r /etc/sokkan/appliance.env ] && { set -a; . /etc/sokkan/appliance.env; set +a; }
[ -r "$CONF" ] && { set -a; . "$CONF"; set +a; }
LOCAL="${SOKKAN_BACKUP_LOCAL:-/var/backups/sokkan}"
ADIR="${SOKKAN_APPLIANCE_DIR:-/var/lib/docker/volumes/sokkan_sokkan-data/_data/appliance}"
STATUS="$ADIR/backup-status.json"
RESTIC="${SOKKAN_RESTIC:-restic}"
NOW() { date +%s; }
log() { echo "backup-offsite: $*" >&2; }

jesc() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g' | cut -c1-400; }

# write_status <ok 0|1> <error text> [snapshot] [size_bytes]
write_status() {
  mkdir -p "$ADIR" 2>/dev/null || return 0
  last_ok="$(sed -n 's/.*"last_success_ts": *\([0-9][0-9]*\).*/\1/p' "$STATUS" 2>/dev/null | head -n 1)"
  [ "$1" = 1 ] && last_ok="$(NOW)"
  tmp="$STATUS.tmp.$$"
  printf '{"last_success_ts": %s, "last_attempt_ts": %s, "last_error": "%s", "snapshot": "%s", "size_bytes": %s}\n' \
    "${last_ok:-null}" "$(NOW)" "$(jesc "${2:-}")" "$(jesc "${3:-}")" "${4:-0}" > "$tmp"
  chmod 644 "$tmp" && mv "$tmp" "$STATUS"
}

need_conf() {
  command -v "$RESTIC" >/dev/null 2>&1 || { log "restic is not installed (apt install restic)"; exit 2; }
  [ -n "${RESTIC_REPOSITORY:-}" ] || { log "no RESTIC_REPOSITORY — fill $CONF"; exit 2; }
  [ -s "$KEY" ] || { log "no encryption key at $KEY — run: $0 init"; exit 2; }
  export RESTIC_REPOSITORY RESTIC_PASSWORD_FILE="$KEY"
  export RESTIC_CACHE_DIR="${RESTIC_CACHE_DIR:-/var/cache/sokkan-restic}"
}

cmd="${1:-}"; [ $# -gt 0 ] && shift
case "$cmd" in
  init)
    command -v "$RESTIC" >/dev/null 2>&1 || { log "restic is not installed (apt install restic)"; exit 2; }
    [ -n "${RESTIC_REPOSITORY:-}" ] || { log "no RESTIC_REPOSITORY — fill $CONF first"; exit 2; }
    mkdir -p "$(dirname "$KEY")"
    if [ -s "$KEY" ]; then
      log "key already present at $KEY (kept — never regenerated over an existing one)"
    else
      # 256 bits from the kernel, on this machine; base64 so that a person can copy it
      head -c 32 /dev/urandom | base64 | tr -d '\n' > "$KEY"; chmod 600 "$KEY"
      echo ""
      echo "  ┌─ BACKUP KEY — shown once. Store a copy AWAY from this machine. ─────────────"
      echo "  │  $(cat "$KEY")"
      echo "  └─ Without it nobody can read the backups — not the supplier, not the host. ──"
      echo ""
    fi
    need_conf
    if "$RESTIC" cat config >/dev/null 2>&1; then log "repository already initialised"
    else "$RESTIC" init >/dev/null && log "repository initialised: $RESTIC_REPOSITORY"; fi
    ;;
  run)
    need_conf
    mkdir -p "$LOCAL"
    trap 'write_status 0 "interrupted"' INT TERM
    # 1. a consistent local set; the key files of the instance go in it ENCRYPTED with the
    #    same client key (restore.sh decrypts them with it)
    if ! out="$(cd "$HERE" && SOKKAN_BACKUP_KEY_PASSFILE="$KEY" SOKKAN_BACKUP_KEEP="${SOKKAN_BACKUP_LOCAL_KEEP:-3}" \
                sh scripts/backup.sh "$LOCAL" 2>&1)"; then
      write_status 0 "local backup failed: $(printf '%s' "$out" | tail -n 1)"; log "$out"; exit 1
    fi
    set_dir="$(ls -1d "$LOCAL"/sokkan-backup-* 2>/dev/null | LC_ALL=C sort | tail -n 1)"
    [ -d "$set_dir" ] || { write_status 0 "local backup wrote no set"; exit 1; }
    # 2. off-site, encrypted client-side by restic
    if ! out="$("$RESTIC" backup --host sokkan-appliance --tag sokkan --tag "set:$(basename "$set_dir")" "$set_dir" 2>&1)"; then
      write_status 0 "upload failed: $(printf '%s' "$out" | tail -n 1)"; log "$out"; exit 1
    fi
    snap="$(printf '%s\n' "$out" | sed -n 's/^snapshot \([0-9a-f]*\) saved.*/\1/p' | tail -n 1)"
    # 3. thin the old ones (never fails the backup that just succeeded)
    "$RESTIC" forget --host sokkan-appliance --tag sokkan --prune \
      --keep-daily "${SOKKAN_BACKUP_KEEP_DAILY:-7}" --keep-weekly "${SOKKAN_BACKUP_KEEP_WEEKLY:-4}" \
      --keep-monthly "${SOKKAN_BACKUP_KEEP_MONTHLY:-6}" >/dev/null 2>&1 || log "WARNING: thinning of old snapshots failed"
    size="$(du -sb "$set_dir" 2>/dev/null | cut -f1)"
    write_status 1 "" "$snap" "${size:-0}"
    log "done: snapshot ${snap:-?} ($(basename "$set_dir"), ${size:-?} bytes) → $RESTIC_REPOSITORY"
    ;;
  check)
    need_conf
    "$RESTIC" check && "$RESTIC" snapshots --host sokkan-appliance --tag sokkan
    ;;
  restore)
    need_conf
    snap="latest"; yes=0
    for a in "$@"; do case "$a" in --yes) yes=1 ;; -*) log "unknown option $a"; exit 2 ;; *) snap="$a" ;; esac; done
    [ "$yes" = 1 ] || { log "restore REPLACES the data of this instance — add --yes"; exit 2; }
    work="$(mktemp -d "${TMPDIR:-/var/tmp}/sokkan-restore.XXXXXX")"
    trap 'rm -rf "$work"' EXIT
    "$RESTIC" restore "$snap" --host sokkan-appliance --tag sokkan --target "$work" >/dev/null
    set_dir="$(find "$work" -type d -name 'sokkan-backup-*' | LC_ALL=C sort | tail -n 1)"
    [ -f "$set_dir/MANIFEST" ] || { log "snapshot $snap holds no backup set"; exit 1; }
    log "snapshot $snap fetched: $(basename "$set_dir") — restoring"
    cd "$HERE" && SOKKAN_BACKUP_KEY_PASSFILE="$KEY" sh scripts/restore.sh "$set_dir" --yes ${SOKKAN_RESTORE_FLAGS:-}
    ;;
  status)
    [ -f "$STATUS" ] && cat "$STATUS" || echo '{"last_success_ts": null, "last_attempt_ts": null, "last_error": "never run"}'
    ;;
  *) sed -n '2,24p' "$0"; exit 2 ;;
esac
