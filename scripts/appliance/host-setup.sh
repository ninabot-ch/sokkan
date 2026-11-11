#!/bin/sh
# sokkan appliance — host side of the box, as root, idempotent (the installer calls it; run it
# again after an upgrade that changes a unit):
#
#   /etc/sokkan/appliance.env        where the shared folder of the data volume is, tunnel limits
#   the shared folder                $DATA/appliance, writable by the app (uid 1000)
#   systemd units                    support tunnel (request watcher, deadline timer, closed at
#                                    boot), nightly backup timer, the local model when
#                                    /etc/sokkan/ship.env exists
#   /opt/sokkan                      a link to this install when it lives elsewhere (the units
#                                    call /opt/sokkan/…)
# Never starts or stops the model by itself and never opens the tunnel.
set -eu
[ "$(id -u)" = 0 ] || { echo "host-setup: run as root" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$HERE"
command -v systemctl >/dev/null 2>&1 || { echo "host-setup: systemd is required" >&2; exit 1; }

# the data volume of THIS compose project (its name follows the folder name)
PROJECT="$(docker compose config 2>/dev/null | sed -n 's/^name: *//p' | head -n 1)"
[ -n "$PROJECT" ] || PROJECT="$(basename "$HERE" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9_-')"
VOL="${PROJECT}_sokkan-data"
MP="$(docker volume inspect -f '{{.Mountpoint}}' "$VOL" 2>/dev/null || true)"
[ -n "$MP" ] && [ -d "$MP" ] || { echo "host-setup: data volume $VOL not found — start SOKKAN first" >&2; exit 1; }
# nothing is written on the host before this point
[ "$HERE" = /opt/sokkan ] || [ -e /opt/sokkan ] || ln -s "$HERE" /opt/sokkan
[ "$(readlink -f /opt/sokkan)" = "$HERE" ] || { echo "host-setup: /opt/sokkan exists and is not this install ($(readlink -f /opt/sokkan)) — the units call /opt/sokkan/…" >&2; exit 1; }
ADIR="$MP/appliance"
install -d -m 0775 -o 1000 -g 1000 "$ADIR"

install -d -m 0755 /etc/sokkan
CONF=/etc/sokkan/appliance.env
touch "$CONF"; chmod 600 "$CONF"
put() { grep -v "^$1=" "$CONF" > "$CONF.tmp" || true; printf '%s=%s\n' "$1" "$2" >> "$CONF.tmp"; cat "$CONF.tmp" > "$CONF"; rm -f "$CONF.tmp"; }
put SOKKAN_APPLIANCE_DIR "$ADIR"
for k in SOKKAN_SUPPORT_MAX_HOURS SOKKAN_REQUIRE_SIGNATURE; do
  v="$(sed -n "s/^$k=//p" .env | tail -n 1)"
  [ -n "$v" ] && put "$k" "$v"
done
[ -f /etc/sokkan/backup.env ] || { printf '# off-site backup — see scripts/appliance/backup-offsite.sh\n#RESTIC_REPOSITORY=s3:https://<endpoint>/<bucket>/<instance>\n#AWS_ACCESS_KEY_ID=\n#AWS_SECRET_ACCESS_KEY=\n' > /etc/sokkan/backup.env; chmod 600 /etc/sokkan/backup.env; }

U=/etc/systemd/system
for f in deploy/appliance/systemd/*.service deploy/appliance/systemd/*.timer deploy/appliance/systemd/*.path; do
  # the request watcher watches the REAL path of this install's volume
  sed "s|/var/lib/docker/volumes/sokkan_sokkan-data/_data/appliance|$ADIR|g" "$f" > "$U/$(basename "$f")"
done
systemctl daemon-reload
systemctl enable --now sokkan-support-request.path sokkan-support-expire.timer >/dev/null
systemctl enable sokkan-support-boot.service >/dev/null
# first run: writes the state file (« agent installed, tunnel closed ») the cockpit reads
[ -f "$ADIR/support-state.json" ] || SOKKAN_APPLIANCE_DIR="$ADIR" sh scripts/appliance/support-tunnel.sh close --by system:install >/dev/null 2>&1 || true
systemctl enable --now sokkan-backup.timer >/dev/null
if [ -f /etc/sokkan/ship.env ]; then
  systemctl enable sokkan-ship.service >/dev/null
  echo "host-setup: local model unit enabled (start it: systemctl start sokkan-ship)"
else
  echo "host-setup: no /etc/sokkan/ship.env — local model unit installed, not enabled (deploy/appliance/ship/ship.env.example)"
fi
echo "host-setup: done — shared folder $ADIR, units in $U"
