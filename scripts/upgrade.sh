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
  REF=""; BUILD=1; DRY=0
  for a in "$@"; do
    case "$a" in
      --no-build) BUILD=0 ;;
      --dry-run) DRY=1 ;;
      -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
      *) REF="$a" ;;
    esac
  done
  NEW=".sokkan-update.$$"
  MANIFEST=".sokkan-update.$$.manifest"
  rm -rf "$NEW" "$MANIFEST"; mkdir "$NEW"
  trap 'rm -rf "$NEW" "$MANIFEST"' EXIT INT TERM

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
  if [ -f "$SRC" ]; then
    tar xzf "$SRC" -C "$NEW" --strip-components=1 || { echo "✗ upgrade: cannot read $SRC — nothing was changed." >&2; exit 1; }
  else
    echo "→ downloading $SRC"
    curl -fsSL "$SRC" | tar xz -C "$NEW" --strip-components=1 \
      || { echo "✗ upgrade: download of the release failed — nothing was changed." >&2; exit 1; }
  fi
  [ -f "$NEW/docker-compose.yml" ] || { echo "✗ upgrade: not a SOKKAN release tarball — nothing was changed." >&2; exit 1; }
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
  tar -C "$NEW" -cf - . | tar -xf - -C .
  rm -rf "$NEW"
  echo "→ code of ${VER:-the release} in place (.env, workspace, override, data volumes kept)"

  if [ -n "$VER" ]; then
    grep -v '^SOKKAN_VERSION=' .env > .env.tmp || true
    printf 'SOKKAN_VERSION=%s\n' "$VER" >> .env.tmp && mv .env.tmp .env
  fi
  [ "$BUILD" = 1 ] || { echo "→ --no-build: run  docker compose up -d --build --remove-orphans  when ready"; exit 0; }
  if [ -f scripts/memory-setup.sh ] && ! grep -q '^SOKKAN_MEMORY_PROFILE=' .env; then
    echo "→ memory engine (new in 3.0): profile and model…"
    sh scripts/memory-setup.sh --non-interactive || echo "  ! memory setup failed — run ./scripts/memory-setup.sh later"
  fi
  DC="docker"; docker info >/dev/null 2>&1 || DC="sudo docker"
  $DC compose up -d --build --remove-orphans
  echo "✔ SOKKAN ${VER:-updated} is running — your .env and your data are untouched."
}

main "$@"
