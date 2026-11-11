#!/bin/sh
# SOKKAN installer — https://sokkan.ch
#
#   curl -fsSL https://sokkan.ch/install.sh | sh                     install, or UPDATE an existing install
#   curl -fsSL https://sokkan.ch/install.sh | sh -s -- --version 3.6.0+1a2b3c4
#   sh install.sh --profile appliance --version 3.6.0+1a2b3c4        on-prem box (docs/appliance/README.md)
#
# Options (each also as an environment variable):
#   --version <ver+hash>   SOKKAN_RELEASE    a PINNED release (default: the current one, resolved
#                                            to its exact <version>+<commit>, never « latest »)
#   --profile <name>       SOKKAN_PROFILE    community (default) | appliance
#   --dir <folder>         SOKKAN_DIR        target folder (default ./sokkan; appliance: /opt/sokkan)
#   --tarball <file>                         a release tarball you already have (offline install)
#   SOKKAN_BASE=https://…                    where releases are served (default https://sokkan.ch)
#   SOKKAN_INSTALL_DOCKER=1                  install Docker without asking (Linux)
#   SOKKAN_ACCEPT_GEMMA_TERMS=1|0            memory model licence answer (1 EmbeddingGemma, 0 MIT model)
#   SOKKAN_REQUIRE_SIGNATURE=1               refuse a release without a valid signature (appliance: on)
#   SOKKAN_COSIGN_PUB, SOKKAN_COSIGN         public key / cosign binary used to verify
#
# An existing install (folder with a .env) is UPDATED by the release's own scripts/upgrade.sh —
# the one upgrade path: same verification, same steps, same health check.
# The source of this file is scripts/install.sh in the SOKKAN repository.
set -eu

BASE="${SOKKAN_BASE:-https://sokkan.ch}"
PROFILE="${SOKKAN_PROFILE:-community}"
RELEASE="${SOKKAN_RELEASE:-}"
DIR="${SOKKAN_DIR:-}"
TARBALL=""
DOCKER_FRESH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --version) RELEASE="${2:?--version <ver+hash>}"; shift ;;
    --profile) PROFILE="${2:?--profile community|appliance}"; shift ;;
    --dir) DIR="${2:?--dir <folder>}"; shift ;;
    --tarball) TARBALL="${2:?--tarball <file>}"; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "install: unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
case "$PROFILE" in community|appliance) ;; *) echo "install: --profile community|appliance" >&2; exit 2 ;; esac
[ -n "$DIR" ] || { [ "$PROFILE" = appliance ] && DIR=/opt/sokkan || DIR=sokkan; }
case "$DIR" in /*) ;; *) DIR="$(pwd)/$DIR" ;; esac     # absolute: scripts change directory
REQUIRE="${SOKKAN_REQUIRE_SIGNATURE:-}"
[ -n "$REQUIRE" ] || { [ "$PROFILE" = appliance ] && REQUIRE=1 || REQUIRE=0; }
PUB="${SOKKAN_COSIGN_PUB:-/etc/sokkan/cosign.pub}"

say() { printf '%s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }
rand_hex() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# ---------------------------------------------------------------- docker
install_docker() {
  say "✗ Docker is not installed."
  case "$(uname -s)" in
    Linux) ;;
    Darwin) say "  macOS: install Docker Desktop (https://docs.docker.com/desktop/) or OrbStack, then run this again."; exit 1 ;;
    *) say "  Install Docker Desktop (https://docs.docker.com/desktop/), then run this again."; exit 1 ;;
  esac
  if [ "${SOKKAN_INSTALL_DOCKER:-}" = "1" ]; then ans=y
  elif [ -r /dev/tty ]; then
    printf "→ Install it now with the official Docker script (get.docker.com, needs sudo)? [Y/n] "
    read -r ans < /dev/tty || ans=n
  else
    say "  To install it: curl -fsSL https://get.docker.com | sudo sh"
    say "  (or re-run this installer with SOKKAN_INSTALL_DOCKER=1)"
    exit 1
  fi
  case "$ans" in n*|N*) say "  → guide: https://docs.docker.com/engine/install/"; exit 1 ;; esac
  say "→ Installing Docker (official get.docker.com script)…"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$(id -un)" 2>/dev/null || true
  DOCKER_FRESH=1
}
command -v docker >/dev/null 2>&1 || install_docker
DC="docker"; docker info >/dev/null 2>&1 || DC="sudo docker"
$DC compose version >/dev/null 2>&1 || {
  say "✗ Docker is here but without the Compose v2 plugin (common with distro/snap packages)."
  say "  Simplest fix: curl -fsSL https://get.docker.com | sudo sh  (installs docker-ce + compose cleanly)"
  exit 1
}

# ---------------------------------------------------------------- fetch + verify
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sokkan-install.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT INT TERM

fetch() {
  if [ -n "$TARBALL" ]; then
    [ -f "$TARBALL" ] || die "no such file: $TARBALL"
    TB="$TARBALL"
    VER="$(basename "$TB" .tar.gz)"; VER="${VER#sokkan-}"; [ "$VER" = latest ] && VER=""
    return 0
  fi
  if [ -n "$RELEASE" ]; then VER="$RELEASE"
  else
    # « the current release » is resolved ONCE to its exact name: what is installed is known
    VER="$(curl -fsSL "$BASE/dist/VERSION" 2>/dev/null | tr -d ' \r\n' || true)"
    [ -n "$VER" ] || die "cannot read $BASE/dist/VERSION — nothing was changed."
  fi
  TB="$WORK/sokkan-$VER.tar.gz"
  say "→ Downloading SOKKAN $VER ($BASE/dist)…"
  if ! curl -fsSL "$BASE/dist/sokkan-$VER.tar.gz" -o "$TB"; then
    [ -z "$RELEASE" ] || die "release $RELEASE not found at $BASE/dist — nothing was changed."
    curl -fsSL "$BASE/dist/sokkan-latest.tar.gz" -o "$TB" || die "download of the release failed — nothing was changed."
  fi
  for sfx in sha256 sig images; do
    curl -fsSL "$BASE/dist/sokkan-$VER.tar.gz.$sfx" -o "$TB.$sfx" 2>/dev/null || rm -f "$TB.$sfx"
  done
}

# Same rules as scripts/verify-release.sh — written here too because the FIRST install has no
# installed script to trust, and the one inside the download cannot vouch for itself.
verify() {
  f="$1"
  if command -v sha256sum >/dev/null 2>&1; then got="$(sha256sum "$f" | cut -d' ' -f1)"
  else got="$(shasum -a 256 "$f" | cut -d' ' -f1)"; fi
  if [ -f "$f.sha256" ]; then
    want="$(tr -d '\r' < "$f.sha256" | awk 'NR==1{print $1}')"
    [ "$want" = "$got" ] || die "sha256 mismatch on $(basename "$f") — REFUSED, nothing was changed."
  elif [ "$REQUIRE" = 1 ]; then die "signature required but $(basename "$f").sha256 is missing — REFUSED."
  fi
  if [ ! -f "$f.sig" ]; then
    [ "$REQUIRE" = 1 ] && die "signature required but $(basename "$f").sig is missing — REFUSED."
    return 0
  fi
  COSIGN="${SOKKAN_COSIGN:-}"
  [ -n "$COSIGN" ] || COSIGN="$(command -v cosign 2>/dev/null || true)"
  [ -n "$COSIGN" ] || { [ -x /opt/sokkan/libexec/cosign ] && COSIGN=/opt/sokkan/libexec/cosign; }
  if [ -z "$COSIGN" ] || [ ! -x "$COSIGN" ] || [ ! -r "$PUB" ]; then
    [ "$REQUIRE" = 1 ] && die "signature required but cosign or the public key ($PUB) is missing — REFUSED."
    say "  ! signature present but not verified (no cosign / no key at $PUB)"
    return 0
  fi
  "$COSIGN" verify-blob --key "$PUB" --signature "$f.sig" --insecure-ignore-tlog=true "$f" >/dev/null 2>&1 \
    || die "the signature of $(basename "$f") does not match $PUB — REFUSED, nothing was changed."
  say "  ✔ signature verified ($(basename "$f"))"
}

fetch
verify "$TB"
mkdir "$WORK/src"
tar xzf "$TB" -C "$WORK/src" --strip-components=1 || die "cannot read the release — nothing was changed."
[ -f "$WORK/src/docker-compose.yml" ] || die "not a SOKKAN release — nothing was changed."
[ -n "$VER" ] || VER="$(tr -d ' \r\n' < "$WORK/src/VERSION" 2>/dev/null || echo '?')"

# what the release's upgrade.sh must see next to the tarball it is given
# (under its release name, so that upgrade.sh reads <version>+<commit> from it)
stage_tarball() {
  rm -rf "$1/.sokkan-stage"; mkdir -p "$1/.sokkan-stage"
  ST="$1/.sokkan-stage/sokkan-$VER.tar.gz"
  cp "$TB" "$ST"
  for sfx in sha256 sig images; do [ -f "$TB.$sfx" ] && cp "$TB.$sfx" "$ST.$sfx"; done
  echo "$ST"
}
unstage() { rm -rf "$(dirname "$1")"; }

# ---------------------------------------------------------------- existing install → UPDATE
if [ -e "$DIR" ]; then
  if [ -f "$DIR/.env" ] && [ -f "$DIR/docker-compose.yml" ]; then
    say "→ Existing install found in $DIR — updating to $VER…"
    if [ -f "$WORK/src/scripts/upgrade.sh" ]; then
      # THE upgrade path: the upgrade.sh of the release being installed, run in the install
      mkdir -p "$DIR/scripts"
      cp "$WORK/src/scripts/upgrade.sh" "$DIR/scripts/.upgrade-$VER.sh"
      ST="$(stage_tarball "$DIR")"
      rc=0
      SOKKAN_REQUIRE_SIGNATURE="$REQUIRE" SOKKAN_COSIGN_PUB="$PUB" sh "$DIR/scripts/.upgrade-$VER.sh" "$ST" || rc=$?
      rm -f "$DIR/scripts/.upgrade-$VER.sh"; unstage "$ST"
      exit "$rc"
    fi
    die "release $VER has no scripts/upgrade.sh (older than 3.4.3): use scripts/rollback.sh $VER from the install."
  fi
  # an empty folder is fine (a mount point prepared for the appliance)
  [ -d "$DIR" ] && [ -z "$(ls -A "$DIR" 2>/dev/null)" ] \
    || die "the folder $DIR exists but does not look like a SOKKAN install (no .env) — aborting."
fi

# ---------------------------------------------------------------- new install
mkdir -p "$DIR"
tar -C "$WORK/src" -cf - . | tar -xf - -C "$DIR"
cd "$DIR"

gemma_flag() {
  case "${SOKKAN_ACCEPT_GEMMA_TERMS:-}" in
    1|y|yes|true) echo "--accept"; return ;;
    0|n|no|false) echo "--decline"; return ;;
  esac
  ( exec < /dev/tty ) 2>/dev/null || return 0
  {
    say ""
    say "  Memory model — SOKKAN's memory (CortHeXis) ranks best with EmbeddingGemma"
    say "  (Google, 300M parameters, runs locally). Its licence is the Gemma Terms of Use,"
    say "  not an open-source licence: https://ai.google.dev/gemma/terms"
    say "  (and its prohibited use policy: https://ai.google.dev/gemma/prohibited_use_policy)."
    say "  Decline and SOKKAN uses multilingual-e5-base (MIT), with a slightly lower recall."
    printf "→ Accept the Gemma Terms of Use and download EmbeddingGemma? [y/n, Enter = decide later] "
  } > /dev/tty
  read -r a < /dev/tty || a=""
  case "$a" in y*|Y*) echo "--accept" ;; n*|N*) echo "--decline" ;; esac
}
memory_setup() {
  [ -f scripts/memory-setup.sh ] || return 0
  ans="$(gemma_flag)"
  # shellcheck disable=SC2086
  sh scripts/memory-setup.sh ${ans:---non-interactive} ${SOKKAN_MEMORY_PROFILE:+--profile $SOKKAN_MEMORY_PROFILE} \
    || say "  ! memory setup failed — run ./scripts/memory-setup.sh later"
}
set_env() { grep -v "^$1=" .env > .env.tmp || true; printf '%s=%s\n' "$1" "$2" >> .env.tmp; cat .env.tmp > .env; rm -f .env.tmp; }

if [ "$PROFILE" = community ]; then
  cp .env.example .env
  chmod 600 .env
  TOKEN="$(rand_hex 24)"
  set_env SOKKAN_LOCAL_TOKEN "$TOKEN"
  if [ "$VER" != "?" ]; then
    set_env SOKKAN_VERSION "$VER"
    case "$VER" in *+*) set_env SOKKAN_COMMIT "${VER#*+}" ;; esac
  fi
  grep -q 'SOKKAN_DB_PASSWORD' .env.example 2>/dev/null && set_env SOKKAN_DB_PASSWORD "$(rand_hex 18)"
  memory_setup
  chmod 600 .env
  COMPOSE_CMD="docker compose"; [ "$DC" = "sudo docker" ] && COMPOSE_CMD="sudo docker compose"
  say ""
  say "✔ SOKKAN $VER installed in $DIR"
  say ""
  say "  1. Edit $DIR/.env:"
  say "       ANTHROPIC_API_KEY=…            (Anthropic API key)"
  say "       — or, with a Claude Pro/Max subscription:"
  say "       CLAUDE_CODE_OAUTH_TOKEN=…      (generated by: claude setup-token)"
  say "       SOKKAN_WORKSPACE=/path/to/your/project"
  say ""
  say "  2. cd $DIR && $COMPOSE_CMD up -d --build     (first build ≈ 3-5 min)"
  say ""
  say "  3. http://localhost:3009 — access token (already in .env):"
  say "       $TOKEN"
  say ""
  say "  Update later: run this installer again, or ./scripts/upgrade.sh [<version>]"
  say ""
  say "  ☁ Don't want to run the ops? SOKKAN Cloud: dedicated VM + private network,"
  say "    operated from Switzerland — same code, same repo."
  say "    → https://sokkan.ch/#cloud · demo: hello@sokkan.ch"
  if [ "$DOCKER_FRESH" = "1" ]; then
    say ""
    say "  ℹ Docker was just installed: after you log out and back in,"
    say "    \"docker\" will work without sudo (docker group)."
  fi
  exit 0
fi

# ---------------------------------------------------------------- appliance profile
# Everything a non-technical site needs is decided here; nobody edits a file afterwards.
ask() {   # ask VAR "question" default — environment first, then the terminal, then the default
  eval "cur=\${$1:-}"
  if [ -z "$cur" ] && ( exec < /dev/tty ) 2>/dev/null; then
    printf '→ %s%s: ' "$2" "${3:+ [$3]}" > /dev/tty
    read -r cur < /dev/tty || cur=""
  fi
  [ -n "$cur" ] || cur="$3"
  eval "$1=\$cur"
}
BOX_IP_DEFAULT="$(hostname -I 2>/dev/null | awk '{print $1}')"
ask SOKKAN_ORG_NAME "Organisation" "Organisation"
ask SOKKAN_OWNER_EMAIL "Administrator e-mail" "admin@localhost"
ask SOKKAN_OWNER_NAME "Administrator name" "Admin"
ask SOKKAN_BOX_NAME "Name people type to reach the box" "sokkan.lan"
ask SOKKAN_BOX_IP "IP address of the box on the local network" "${BOX_IP_DEFAULT:-127.0.0.1}"
ask SOKKAN_SHIP_URL "Local model endpoint (empty = none)" "http://host.docker.internal:8004"
ask SOKKAN_SHIP_MODEL "Local model name" "qwen3.8-27b"
ask SOKKAN_DEEP_TOKEN "Deep gateway token of this instance (empty = no Deep)" ""
ask SOKKAN_INSTANCE_ID "Instance name for supervision (opaque)" "sokkan-box"
ask SOKKAN_HEARTBEAT_URL "Supervision heartbeat URL (empty = nothing is sent)" ""

OWNER_CODE="$(rand_hex 24)"
: > .env; chmod 600 .env
# the template, minus its placeholders (<…>): every one of them is written below
grep -v '=<' deploy/appliance/env.appliance.example > .env
set_env SOKKAN_VERSION "$VER"
case "$VER" in *+*) set_env SOKKAN_COMMIT "${VER#*+}" ;; *) set_env SOKKAN_COMMIT "" ;; esac
set_env SOKKAN_REQUIRE_SIGNATURE "$REQUIRE"
set_env SOKKAN_ORG_NAME "$SOKKAN_ORG_NAME"
set_env SOKKAN_OWNER_EMAIL "$SOKKAN_OWNER_EMAIL"
set_env SOKKAN_OWNER_NAME "$SOKKAN_OWNER_NAME"
set_env SOKKAN_LOCAL_TOKEN "$OWNER_CODE"
set_env SOKKAN_TLS_SITES "https://$SOKKAN_BOX_NAME, https://$SOKKAN_BOX_IP, https://localhost"
set_env SOKKAN_PUBLIC_URL "https://$SOKKAN_BOX_NAME"
set_env SOKKAN_SHIP_URL "$SOKKAN_SHIP_URL"
set_env SOKKAN_SHIP_MODEL "$SOKKAN_SHIP_MODEL"
set_env SOKKAN_ASSISTANT_LLM_MODEL "$SOKKAN_SHIP_MODEL"
if [ -n "$SOKKAN_SHIP_URL" ]; then set_env SOKKAN_ASSISTANT_LLM_URL "${SOKKAN_SHIP_URL%/}/v1"
else set_env SOKKAN_FEATURE_ASSISTANT 0; set_env SOKKAN_ASSISTANT_LLM_URL ""; fi
# document ingestion reads with the LOCAL model only: no local model = the feature stays off
# (it never falls back on the Deep gateway — the documents would leave the site)
set_env SOKKAN_DOC_INGEST_LLM_MODEL "$SOKKAN_SHIP_MODEL"
if [ -n "$SOKKAN_SHIP_URL" ]; then set_env SOKKAN_DOC_INGEST_LLM_URL "${SOKKAN_SHIP_URL%/}/v1"
else set_env SOKKAN_FEATURE_DOC_INGEST 0; set_env SOKKAN_DOC_INGEST_LLM_URL ""; fi
[ -n "${SOKKAN_STUDY_ID:-}" ] && set_env SOKKAN_STUDY_ID "$SOKKAN_STUDY_ID"
[ -n "${SOKKAN_STUDY_EXPIRES_AT:-}" ] && set_env SOKKAN_STUDY_EXPIRES_AT "$SOKKAN_STUDY_EXPIRES_AT"
set_env SOKKAN_DEEP_TOKEN "$SOKKAN_DEEP_TOKEN"
[ -n "${SOKKAN_DEEP_URL:-}" ] && set_env SOKKAN_DEEP_URL "$SOKKAN_DEEP_URL"
set_env SOKKAN_DB_PASSWORD "$(rand_hex 18)"
set_env SOKKAN_INSTANCE_ID "$SOKKAN_INSTANCE_ID"
set_env SOKKAN_HEARTBEAT_URL "$SOKKAN_HEARTBEAT_URL"
set_env SOKKAN_HEARTBEAT_TOKEN "${SOKKAN_HEARTBEAT_TOKEN:-}"
[ -n "${SOKKAN_TLS_PORT:-}" ] && set_env SOKKAN_TLS_PORT "$SOKKAN_TLS_PORT"
[ -n "${SOKKAN_PORT:-}" ] && set_env SOKKAN_PORT "$SOKKAN_PORT"
[ -n "${SOKKAN_ERROR_LANG:-}" ] && set_env SOKKAN_ERROR_LANG "$SOKKAN_ERROR_LANG"
# images: prebuilt and signed when the release publishes them, else built here from the
# verified tarball (said out loud — never a silent fallback)
if [ -f "$TB.images" ]; then
  set_env SOKKAN_IMAGE_TAG "$(printf '%s' "$VER" | tr '+' '-')"      # a docker tag cannot hold `+`
else
  say "  ! release $VER publishes no prebuilt images: they will be BUILT on this machine (≈ 5-10 min)"
  set_env SOKKAN_IMAGES 0
  grep -v -e '^SOKKAN_IMAGE_TAG=' -e '^COMPOSE_FILE=' -e '^COMPOSE_PATH_SEPARATOR=' .env > .env.tmp; cat .env.tmp > .env; rm -f .env.tmp
fi
# memory: profile and accelerator from the machine (GPU → gpu + sycl/cuda override), the
# licence answer from the client. The template's values are the target; the machine decides.
grep -v -e '^SOKKAN_MEMORY_PROFILE=' -e '^SOKKAN_EMBED_ACCEL=' -e '^SOKKAN_ACCEPT_GEMMA_TERMS=' .env > .env.tmp; cat .env.tmp > .env; rm -f .env.tmp
memory_setup
chmod 600 .env

# start through the release's own upgrade path: pull + verify the images, start, wait until
# the instance answers as $VER
ST="$(stage_tarball "$PWD")"
rc=0
SOKKAN_REQUIRE_SIGNATURE="$REQUIRE" SOKKAN_COSIGN_PUB="$PUB" sh scripts/upgrade.sh "$ST" || rc=$?
unstage "$ST"
[ "$rc" = 0 ] || die "SOKKAN $VER is installed in $DIR but did not start — see: cd $DIR && $DC compose ps"

# host side: support tunnel agent, nightly backup timer, model unit (needs root + systemd)
if [ "${SOKKAN_SKIP_HOST_SETUP:-0}" = 1 ]; then
  say "  (host setup skipped: SOKKAN_SKIP_HOST_SETUP=1)"
elif [ "$(id -u)" = 0 ] && command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  sh scripts/appliance/host-setup.sh || say "  ! host setup incomplete — run scripts/appliance/host-setup.sh as root"
else
  say "  ! not root or no systemd: run  sudo sh $DIR/scripts/appliance/host-setup.sh  to install the"
  say "    support-tunnel agent, the nightly backup timer and the model unit"
fi

# the people: SOKKAN_ACCOUNTS="ana@x.ch:Ana:projet-ana,ben@x.ch:Ben:projet-ben"
CODES="$WORK/codes.txt"; : > "$CODES"
if [ -n "${SOKKAN_ACCOUNTS:-}" ]; then
  printf '%s\n' "$SOKKAN_ACCOUNTS" | tr ',' '\n' | while IFS=: read -r a_mail a_name a_proj; do
    [ -n "$a_mail" ] || continue
    if [ -n "$a_proj" ]; then
      line="$(sh scripts/appliance/accounts.sh add "$a_mail" --name "${a_name:-$a_mail}" --project "$a_proj" < /dev/null 2>"$WORK/acc.err" | tail -n 1)" || line=""
    else
      line="$(sh scripts/appliance/accounts.sh add "$a_mail" --name "${a_name:-$a_mail}" --role dev < /dev/null 2>"$WORK/acc.err" | tail -n 1)" || line=""
    fi
    case "$line" in
      *sk-local-*) printf '       %s\n' "$line" >> "$CODES" ;;
      *) say "  ! account $a_mail was not created ($(tail -n 1 "$WORK/acc.err" 2>/dev/null)) — scripts/appliance/accounts.sh add $a_mail --project …" ;;
    esac
  done
fi

say ""
say "✔ SOKKAN $VER is running on this machine (appliance profile)"
say ""
say "  Address      https://$SOKKAN_BOX_NAME   (also https://$SOKKAN_BOX_IP)"
say "  Certificate  sh $DIR/scripts/appliance/export-ca.sh  → install sokkan-local-ca.crt once per workstation"
say "  Administrator ($SOKKAN_OWNER_EMAIL) access code — shown once, also in $DIR/.env (0600):"
say "       $OWNER_CODE"
if [ -s "$CODES" ]; then
  say "  Personal access codes — shown ONCE, hand each to its owner:"
  cat "$CODES"
else
  say "  Add a person:  sh $DIR/scripts/appliance/accounts.sh add <e-mail> --name <name> --project <slug>"
fi
say "  Backup       fill /etc/sokkan/backup.env then: sh $DIR/scripts/appliance/backup-offsite.sh init"
say "  Support      off. The client opens it: sh $DIR/scripts/appliance/support-tunnel.sh open 2"
say "  Docs         $DIR/docs/appliance/README.md"
