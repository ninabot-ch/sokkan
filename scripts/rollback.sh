#!/bin/sh
# sokkan rollback — go back to an earlier release (or forward to a pinned one). Run from
# the sokkan folder:
#
#   ./scripts/rollback.sh <hash>          e.g. 2.3.0+1a2b3c4 — the tarball sokkan.ch/dist/sokkan-<hash>.tar.gz
#   ./scripts/rollback.sh ./sokkan-x.tar.gz   a tarball you already have
#   ./scripts/rollback.sh                 the version that ran before the last upgrade
#                                         (.sokkan-previous, written by scripts/upgrade.sh)
#
# The release is verified like an upgrade (scripts/verify-release.sh), and the script waits
# until the instance answers as the version asked for (exit 1 otherwise).
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
[ -n "$REF" ] || { [ -f .sokkan-previous ] && REF="$(cat .sokkan-previous)"; }
[ -n "$REF" ] || { sed -n '2,19p' "$0"; exit 2; }
BASE="${SOKKAN_BASE:-https://sokkan.ch}"
NEW=".sokkan-rollback.$$"
DL=".sokkan-rollback.$$.dl"
rm -rf "$NEW" "$DL"; mkdir "$NEW" "$DL"
trap 'rm -rf "$NEW" "$DL"' EXIT
if [ -f "$REF" ]; then
  TB="$REF"
  VER="$(basename "$REF" .tar.gz)"; VER="${VER#sokkan-}"
else
  TB="$DL/sokkan-$REF.tar.gz"
  curl -fsSL "$BASE/dist/sokkan-$REF.tar.gz" -o "$TB" \
    || { echo "✗ rollback: download of sokkan-$REF.tar.gz failed — nothing was changed." >&2; exit 1; }
  for sfx in sha256 sig; do curl -fsSL "$BASE/dist/sokkan-$REF.tar.gz.$sfx" -o "$TB.$sfx" 2>/dev/null || rm -f "$TB.$sfx"; done
  VER="$REF"
fi
# verified with the script of the release that is RUNNING (it carries this machine's rules)
[ "$(sed -n 's/^SOKKAN_REQUIRE_SIGNATURE=//p' .env | tail -n 1)" = 1 ] && SOKKAN_REQUIRE_SIGNATURE=1
export SOKKAN_REQUIRE_SIGNATURE="${SOKKAN_REQUIRE_SIGNATURE:-0}"
if [ -f scripts/verify-release.sh ]; then
  sh scripts/verify-release.sh "$TB" || { echo "✗ rollback: the release did not pass verification — nothing was changed." >&2; exit 3; }
fi
tar xzf "$TB" -C "$NEW" --strip-components=1
[ -f "$NEW/docker-compose.yml" ] || { echo "rollback: not a SOKKAN release tarball" >&2; exit 1; }
CUR="$(sed -n 's/^SOKKAN_VERSION=//p' .env | tail -n 1)"

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

# COMPOSE_FILE: keep only the files the target release ships (a 3.0 GPU override, the 3.6
# prebuilt-images override: an older compose setup does not know them)
CF="$(sed -n 's/^COMPOSE_FILE=//p' .env | tail -n 1)"
KEEP=""
OLDIFS="$IFS"; IFS=":"
for f in $CF; do [ -f "$f" ] && KEEP="${KEEP:+$KEEP:}$f"; done
IFS="$OLDIFS"
IMAGES=0
case ":$KEEP:" in *:docker/compose.images.yml:*) IMAGES=1 ;; esac
DC="docker"; docker info >/dev/null 2>&1 || DC="sudo docker"
ITAG="$(printf '%s' "$VER" | tr '+' '-')"          # a docker tag cannot hold `+`
if [ "$IMAGES" = 1 ] && ! $DC image inspect "sokkan-api:$ITAG" >/dev/null 2>&1; then
  # the images of that version are no longer on this machine: build it from its sources
  KEEP="$(printf '%s' "$KEEP" | sed 's|:docker/compose.images.yml||; s|docker/compose.images.yml:||')"
  IMAGES=0
  echo "  (images of $VER not on this machine: building from the sources)"
fi
grep -v -e '^SOKKAN_VERSION=' -e '^COMPOSE_FILE=' -e '^SOKKAN_COMMIT=' -e '^SOKKAN_IMAGE_TAG=' .env > .env.tmp || true
printf 'SOKKAN_VERSION=%s\n' "$VER" >> .env.tmp
case "$VER" in *+*) printf 'SOKKAN_COMMIT=%s\n' "${VER#*+}" >> .env.tmp ;; esac
[ -n "$KEEP" ] && [ "$KEEP" != "docker-compose.yml" ] && printf 'COMPOSE_FILE=%s\n' "$KEEP" >> .env.tmp
[ "$IMAGES" = 1 ] && printf 'SOKKAN_IMAGE_TAG=%s\n' "$ITAG" >> .env.tmp
cat .env.tmp > .env && rm -f .env.tmp
[ -n "$CUR" ] && [ "$CUR" != "$VER" ] && printf '%s\n' "$CUR" > .sokkan-previous
if [ "$IMAGES" = 1 ]; then $DC compose up -d --no-build --remove-orphans
else $DC compose up -d --build --remove-orphans; fi

# the instance answers, and as the version asked for (same check as scripts/upgrade.sh)
[ "${SOKKAN_ROLLBACK_NO_WAIT:-0}" = 1 ] && { echo "✔ SOKKAN $VER started (health not checked)"; exit 0; }
port="$(sed -n 's/^SOKKAN_PORT=//p' .env | tail -n 1)"; port="${port:-3009}"
url="${SOKKAN_UPGRADE_URL:-http://127.0.0.1:$port}"
limit="${SOKKAN_UPGRADE_WAIT_S:-900}"; t=0
if command -v curl >/dev/null 2>&1; then
  echo "→ waiting for SOKKAN to answer as $VER ($url, up to ${limit}s)…"
  ok=0
  while [ "$t" -lt "$limit" ]; do
    if [ "$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$url/api/health" 2>/dev/null)" = 200 ]; then
      semver="$(curl -s -m 5 "$url/api/version" 2>/dev/null | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p')"
      if [ -z "$semver" ] || [ "$semver" = "${VER%%+*}" ]; then ok=1; break; fi
    fi
    sleep 3; t=$((t + 3))
  done
  [ "$ok" = 1 ] || { echo "✗ rollback: SOKKAN did not come back as $VER — see: $DC compose ps ; $DC compose logs api" >&2; exit 1; }
fi
echo "✔ SOKKAN $VER is running — docs/UPGRADE.md (Roll back) for the memory archive"
