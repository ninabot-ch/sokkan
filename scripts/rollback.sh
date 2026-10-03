#!/bin/sh
# sokkan rollback — go back to an earlier release (or forward to a pinned one). Run from
# the sokkan folder:
#
#   ./scripts/rollback.sh <hash>          e.g. 2.3.0+1a2b3c4 — the tarball sokkan.ch/dist/sokkan-<hash>.tar.gz
#   ./scripts/rollback.sh ./sokkan-x.tar.gz   a tarball you already have
#
# The code is REPLACED, not overwritten: extracting an older tarball over a newer folder
# leaves the newer files behind, and the older build trips on them (a 2.3 web build
# compiles the 3.0 CortHeXis components and fails). Kept: .env, your workspace folder,
# anything that is not part of a release, and the data (Docker volumes) — untouched.
# Then: SOKKAN_VERSION pinned in .env, COMPOSE_FILE dropped (a 3.0 GPU override an older
# compose file does not know), `docker compose up -d --build --remove-orphans`.
set -eu
cd "$(dirname "$0")/.." || exit 1
[ -f .env ] && [ -f docker-compose.yml ] || { echo "rollback: run it from the sokkan folder" >&2; exit 1; }
REF="${1:-}"
[ -n "$REF" ] || { sed -n '2,13p' "$0"; exit 2; }
BASE="${SOKKAN_BASE:-https://sokkan.ch}"
NEW=".sokkan-rollback.$$"
rm -rf "$NEW"; mkdir "$NEW"
trap 'rm -rf "$NEW"' EXIT
if [ -f "$REF" ]; then
  tar xzf "$REF" -C "$NEW" --strip-components=1
  VER="$(basename "$REF" .tar.gz)"; VER="${VER#sokkan-}"
else
  curl -fsSL "$BASE/dist/sokkan-$REF.tar.gz" | tar xz -C "$NEW" --strip-components=1
  VER="$REF"
fi
[ -f "$NEW/docker-compose.yml" ] || { echo "rollback: not a SOKKAN release tarball" >&2; exit 1; }

# release folders of any version (0.1 - 3.x) + every entry of the target release
WS="$(sed -n 's/^SOKKAN_WORKSPACE=//p' .env | tail -n 1)"
for e in backend frontend memory magnitude cli docker scripts examples docs tests \
         $(ls -A "$NEW"); do
  case "$e" in .env|workspace|"$NEW"|.git) continue ;; esac
  [ -n "$WS" ] && [ "./$e" = "$WS" -o "$e" = "$WS" ] && continue
  rm -rf "./$e"
done
for e in $(ls -A "$NEW"); do mv "$NEW/$e" "./$e"; done
echo "→ code of $VER in place (.env, workspace and data volumes kept)"

grep -v '^SOKKAN_VERSION=' .env | grep -v '^COMPOSE_FILE=' > .env.tmp || true
printf 'SOKKAN_VERSION=%s\n' "$VER" >> .env.tmp && mv .env.tmp .env
DC="docker"; docker info >/dev/null 2>&1 || DC="sudo docker"
$DC compose up -d --build --remove-orphans
echo "✔ SOKKAN $VER is running — docs/UPGRADE.md (Roll back) for the memory archive"
