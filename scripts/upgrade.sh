#!/bin/sh
# sokkan upgrade — put a release in place WITHOUT leaving the files it no longer ships.
# Run from the sokkan folder (the installer and the managed fleet call it too):
#
#   ./scripts/upgrade.sh                       the current release (sokkan.ch/dist/sokkan-latest.tar.gz)
#   ./scripts/upgrade.sh <hash>                a pinned release, e.g. 3.4.3+1a2b3c4
#   ./scripts/upgrade.sh ./sokkan-x.tar.gz     a tarball you already have
#   ./scripts/upgrade.sh https://…/x.tar.gz    a tarball served elsewhere
#     --no-build   put the code in place, do not run docker compose
#     --dry-run    list what would be removed and replaced, change nothing
#     --images     run the PREBUILT, signed images of the release instead of building here
#                  (also: SOKKAN_IMAGES=1 in .env — the appliance profile)
#     --no-wait    do not wait for the new version to answer (default: wait, see below)
#
# ONE upgrade path (3.6): `curl -fsSL https://sokkan.ch/install.sh | sh` on an existing install
# downloads the release and runs THIS script from it — same steps, same checks.
#
# Before anything changes: the release is verified (scripts/verify-release.sh — sha256, and
# the cosign signature against the public key already on this machine; with
# SOKKAN_REQUIRE_SIGNATURE=1 a release without a valid signature is refused).
# After `docker compose up`: the script waits until /api/version answers the version it just
# installed (SOKKAN_UPGRADE_WAIT_S, default 900 s). If it does not, it says so, exits 1 and
# prints the rollback command (the previous version is recorded in .sokkan-previous).
#
# Why not `tar xz` over the folder: extracting a release OVER the tree keeps every file an
# earlier release had and this one dropped. Next type-checks the whole tree, so one dead
# component (`frontend/components/MemoryKB.tsx`, removed in 3.0) is enough to fail
# `npm run build` of the web image — seen on the public demo at 3.4.2.
#
# What it does: the tarball's file list is the manifest. Inside the CODE directories of the
# release (backend/, frontend/, memory/, cli/, scripts/, deploy/, docker/, magnitude/,
# examples/, docs/, tests/) every file absent from the manifest is removed, then the release
# is copied over. NEVER touched: `.env` and any `.env.*`, `docker-compose.override.yml`,
# the workspace folder (`SOKKAN_WORKSPACE`), a local data folder (`SOKKAN_DATA_DIR`),
# `frontend/node_modules`, `frontend/.next`, `.git`, and every top-level entry that is not
# part of a release (your own files). The data lives in Docker volumes: untouched.
# A failed download changes nothing.
set -eu

main() {
  cd "$(dirname "$0")/.." || exit 1
  [ -f .env ] && [ -f docker-compose.yml ] || { echo "upgrade: run it from the sokkan folder (no .env / docker-compose.yml here)" >&2; exit 1; }
  BASE="${SOKKAN_BASE:-https://sokkan.ch}"
  REF=""; BUILD=1; DRY=0; WAIT=1
  [ "${SOKKAN_UPGRADE_NO_WAIT:-0}" = 1 ] && WAIT=0
  # signature policy: the strictest of .env and the environment (a 1 is never lowered)
  [ "$(sed -n 's/^SOKKAN_REQUIRE_SIGNATURE=//p' .env | tail -n 1)" = 1 ] && SOKKAN_REQUIRE_SIGNATURE=1
  export SOKKAN_REQUIRE_SIGNATURE="${SOKKAN_REQUIRE_SIGNATURE:-0}"
  IMAGES="$(sed -n 's/^SOKKAN_IMAGES=//p' .env | tail -n 1)"
  for a in "$@"; do
    case "$a" in
      --no-build) BUILD=0 ;;
      --dry-run) DRY=1 ;;
      --images) IMAGES=1 ;;
      --no-wait) WAIT=0 ;;
      -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
      *) REF="$a" ;;
    esac
  done
  NEW=".sokkan-update.$$"
  MANIFEST=".sokkan-update.$$.manifest"
  DL=".sokkan-update.$$.dl"
  rm -rf "$NEW" "$MANIFEST" "$DL"; mkdir "$NEW" "$DL"
  trap 'rm -rf "$NEW" "$MANIFEST" "$DL"' EXIT INT TERM

  # ---- fetch ----------------------------------------------------------------------------
  VER=""
  if [ -z "$REF" ]; then
    SRC="$BASE/dist/sokkan-latest.tar.gz"
    VER="$(curl -fsSL "$BASE/dist/VERSION" 2>/dev/null || true)"
  elif [ -f "$REF" ]; then
    SRC="$REF"
    VER="$(basename "$REF" .tar.gz)"; VER="${VER#sokkan-}"; [ "$VER" = latest ] && VER=""
  else
    case "$REF" in
      http://*|https://*) SRC="$REF" ;;
      *) SRC="$BASE/dist/sokkan-$REF.tar.gz"; VER="$REF" ;;
    esac
  fi
  if [ ! -f "$SRC" ]; then
    echo "→ downloading $SRC"
    TB="$DL/$(basename "$SRC")"
    curl -fsSL "$SRC" -o "$TB" \
      || { echo "✗ upgrade: download of the release failed — nothing was changed." >&2; exit 1; }
    # what the publisher puts next to the tarball (absent on releases before 3.6)
    for sfx in sha256 sig images; do curl -fsSL "$SRC.$sfx" -o "$TB.$sfx" 2>/dev/null || rm -f "$TB.$sfx"; done
    SRC="$TB"
  fi
  # ---- verify BEFORE anything is extracted over the tree --------------------------------
  # with the script of the INSTALLED release (the one this machine already trusts)
  if [ -f scripts/verify-release.sh ]; then
    sh scripts/verify-release.sh "$SRC" || { echo "✗ upgrade: the release did not pass verification — nothing was changed." >&2; exit 3; }
  elif [ "$SOKKAN_REQUIRE_SIGNATURE" = 1 ]; then
    echo "✗ upgrade: signature required but scripts/verify-release.sh is missing — nothing was changed." >&2; exit 3
  fi
  tar xzf "$SRC" -C "$NEW" --strip-components=1 || { echo "✗ upgrade: cannot read $SRC — nothing was changed." >&2; exit 1; }
  [ -f "$NEW/docker-compose.yml" ] || { echo "✗ upgrade: not a SOKKAN release tarball — nothing was changed." >&2; exit 1; }
  # version of the release: its VERSION file wins over the file name (a renamed tarball)
  if [ -f "$NEW/VERSION" ]; then
    SEMVER="$(tr -d ' \r\n' < "$NEW/VERSION")"
    case "$VER" in "$SEMVER"+*) ;; *) VER="$SEMVER" ;; esac
  fi
  IMAGES_FILE=""
  if [ "${IMAGES:-0}" = 1 ]; then
    [ -f "$SRC.images" ] || { echo "✗ upgrade: --images asked but the release has no image list ($(basename "$SRC").images) — nothing was changed." >&2; exit 1; }
    IMAGES_FILE="$DL/images.list"; cp "$SRC.images" "$IMAGES_FILE"
    ITAG="$(printf '%s' "$VER" | tr '+' '-')"          # a docker tag cannot hold `+`
    DCK="docker"; docker info >/dev/null 2>&1 || DCK="sudo docker"
    # pull and verify EVERY image before the code is touched: a missing signature stops here
    while IFS='=' read -r name ref; do
      case "$name" in api|web) ;; *) continue ;; esac
      echo "→ pulling $name: $ref"
      $DCK pull -q "$ref" >/dev/null || { echo "✗ upgrade: cannot pull $ref — nothing was changed." >&2; exit 1; }
      if [ -f scripts/verify-release.sh ]; then
        sh scripts/verify-release.sh --image "$ref" || { echo "✗ upgrade: image $name did not pass verification — nothing was changed." >&2; exit 3; }
      else
        sh "$NEW/scripts/verify-release.sh" --image "$ref" || exit 3
      fi
      $DCK tag "$ref" "sokkan-$name:$ITAG"
    done < "$IMAGES_FILE"
  fi
  # the manifest: every file of the release, relative paths
  (cd "$NEW" && find . \( -type f -o -type l \) | sed 's|^\./||' | LC_ALL=C sort) > "$MANIFEST"

  # ---- what is never touched ------------------------------------------------------------
  WS="$(sed -n 's/^SOKKAN_WORKSPACE=//p' .env | tail -n 1 | sed 's|^\./||; s|/$||')"
  DD="$(sed -n 's/^SOKKAN_DATA_DIR=//p' .env | tail -n 1 | sed 's|^\./||; s|/$||')"
  keep() {   # keep <relative path> → 0 when the path must stay
    case "$1" in
      .env|.env.*|*/.env|*/.env.*|docker-compose.override.yml|.git|.git/*) return 0 ;;
      workspace|workspace/*) return 0 ;;
      frontend/node_modules|frontend/node_modules/*|frontend/.next|frontend/.next/*) return 0 ;;
      "$NEW"|"$NEW"/*|"$MANIFEST") return 0 ;;
    esac
    [ -n "$WS" ] && case "$1" in "$WS"|"$WS"/*) return 0 ;; esac
    [ -n "$DD" ] && case "$1" in "$DD"|"$DD"/*) return 0 ;; esac
    return 1
  }

  # ---- purge: files of the code directories that the release does not ship -------------
  for d in backend frontend memory cli scripts deploy docker magnitude examples docs tests; do
    [ -d "$d" ] || continue
    if [ ! -d "$NEW/$d" ]; then
      keep "$d" && continue
      echo "  - $d/ (no longer part of a release)"
      [ "$DRY" = 1 ] || rm -rf "./$d"
      continue
    fi
    find "$d" \( -path "frontend/node_modules" -o -path "frontend/.next" -o -name __pycache__ -o -name .venv -o -name node_modules \) -prune -o \( -type f -o -type l \) -print \
    | LC_ALL=C sort | while IFS= read -r f; do
      keep "$f" && continue
      if ! grep -qxF -- "$f" "$MANIFEST"; then
        echo "  - $f"
        [ "$DRY" = 1 ] || rm -f -- "$f"
      fi
    done
    [ "$DRY" = 1 ] || find "$d" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
    [ "$DRY" = 1 ] || find "$d" -mindepth 1 -type d \( -path "frontend/node_modules" -o -path "frontend/.next" \) -prune -o -type d -empty -print 2>/dev/null \
      | LC_ALL=C sort -r | while IFS= read -r e; do keep "$e" || rmdir -- "$e" 2>/dev/null || true; done
  done
  if [ "$DRY" = 1 ]; then
    echo "→ dry run: the lines above would be removed; then $(wc -l < "$MANIFEST" | tr -d ' ') files of the release copied over. Nothing was changed."
    exit 0
  fi

  # ---- the release over the tree (every file of the manifest, nothing else) ------------
  PREV="$(sed -n 's/^SOKKAN_VERSION=//p' .env | tail -n 1)"
  [ -n "$PREV" ] && [ "$PREV" != "$VER" ] && printf '%s\n' "$PREV" > .sokkan-previous
  tar -C "$NEW" -cf - . | tar -xf - -C .
  rm -rf "$NEW"
  echo "→ code of ${VER:-the release} in place (.env, workspace, override, data volumes kept)"

  chmod 600 .env 2>/dev/null || true      # it holds secrets (tokens, database password)
  set_env() {   # set_env KEY VALUE — replace or append, keep everything else of .env
    grep -v "^$1=" .env > .env.tmp || true
    printf '%s=%s\n' "$1" "$2" >> .env.tmp && cat .env.tmp > .env && rm -f .env.tmp
  }
  if [ -n "$VER" ]; then
    set_env SOKKAN_VERSION "$VER"
    # the commit of the release, for the image build and /api/version (3.6: it said "unknown")
    case "$VER" in *+*) set_env SOKKAN_COMMIT "${VER#*+}" ;; esac
  fi
  # 3.6.5: the secret between the router and the api (« Write with Claude »), generated once on
  # a box that has a local model and none yet — the value never shown
  if grep -q '^SOKKAN_SHIP_URL=.' .env && ! grep -q '^SOKKAN_ROUTER_SECRET=.' .env; then
    set_env SOKKAN_ROUTER_SECRET "$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    echo "→ router secret generated (SOKKAN_ROUTER_SECRET)"
  fi
  if [ "${IMAGES:-0}" = 1 ]; then
    set_env SOKKAN_IMAGES 1
    set_env SOKKAN_IMAGE_TAG "$ITAG"
    CF="$(sed -n 's/^COMPOSE_FILE=//p' .env | tail -n 1)"
    case ":$CF:" in
      *:docker/compose.images.yml:*) ;;
      *) set_env COMPOSE_FILE "${CF:-docker-compose.yml}:docker/compose.images.yml"
         grep -q '^COMPOSE_PATH_SEPARATOR=' .env || set_env COMPOSE_PATH_SEPARATOR ":" ;;
    esac
  fi
  [ "$BUILD" = 1 ] || { echo "→ --no-build: run  docker compose up -d --build --remove-orphans  when ready"; exit 0; }
  if [ -f scripts/memory-setup.sh ] && ! grep -q '^SOKKAN_MEMORY_PROFILE=' .env; then
    echo "→ memory engine (new in 3.0): profile and model…"
    sh scripts/memory-setup.sh --non-interactive || echo "  ! memory setup failed — run ./scripts/memory-setup.sh later"
  fi
  DC="docker"; docker info >/dev/null 2>&1 || DC="sudo docker"
  if [ "${IMAGES:-0}" = 1 ]; then
    $DC compose up -d --no-build --remove-orphans
  else
    $DC compose up -d --build --remove-orphans
  fi
  [ "$WAIT" = 1 ] || { echo "✔ SOKKAN ${VER:-updated} started (--no-wait: health not checked)."; exit 0; }
  if wait_version "$VER"; then
    echo "✔ SOKKAN ${VER:-updated} is running — your .env and your data are untouched."
  else
    echo "✗ upgrade: SOKKAN did not come back as ${VER:-the new version} — see: $DC compose ps ; $DC compose logs api" >&2
    [ -f .sokkan-previous ] && echo "  roll back to the version that ran before:  ./scripts/rollback.sh $(cat .sokkan-previous)" >&2
    exit 1
  fi
}

# wait_version <ver[+commit]> — the instance answers, AND it is the version just installed
# (an old container that still answers /api/health is not a successful upgrade). Releases
# before 3.2.3 have no /api/version: for those, /api/health 200 is all there is to check.
wait_version() {
  want="$1"
  port="$(sed -n 's/^SOKKAN_PORT=//p' .env | tail -n 1)"; port="${port:-3009}"
  url="${SOKKAN_UPGRADE_URL:-http://127.0.0.1:$port}"
  limit="${SOKKAN_UPGRADE_WAIT_S:-900}"
  command -v curl >/dev/null 2>&1 || { echo "  (no curl: health not checked)"; return 0; }
  echo "→ waiting for SOKKAN to answer as ${want:-the new version} ($url, up to ${limit}s)…"
  t=0
  while [ "$t" -lt "$limit" ]; do
    if [ "$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$url/api/health" 2>/dev/null)" = 200 ]; then
      body="$(curl -s -m 5 "$url/api/version" 2>/dev/null || true)"
      got="$(printf '%s' "$body" | sed -n 's/.*"dist" *: *"\([^"]*\)".*/\1/p')"
      semver="$(printf '%s' "$body" | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p')"
      if [ -z "$want" ] || [ -z "$semver" ] || [ "$got" = "$want" ] || [ "$semver" = "$want" ] \
         || { [ "$semver" = "${want%%+*}" ] && [ "$got" = "$semver" ]; }; then
        return 0
      fi
    fi
    sleep 3; t=$((t + 3))
  done
  return 1
}

main "$@"
