#!/bin/sh
# SOKKAN Magnitude — one-line host installer.
#
# Lays down a working Python if the machine has none (no system changes, no
# sudo — a standalone build under ~/.sokkan), fetches the host agent, and pairs
# this machine with the cockpit. Re-runnable; everything lives under ~/.sokkan.
#
# The cockpit serves this with the cockpit URL and pairing token already filled
# in — just copy the one-liner from the Magnitude tab. Values can be overridden
# with MAGNITUDE_COCKPIT / MAGNITUDE_TOKEN / MAGNITUDE_REF / MAGNITUDE_HOME.
set -eu

COCKPIT="${MAGNITUDE_COCKPIT:-@COCKPIT@}"
TOKEN="${MAGNITUDE_TOKEN:-@TOKEN@}"
REF="${MAGNITUDE_REF:-@REF@}"
PYVER="@PYVER@"
PYDATE="@PYDATE@"
ROOT="${MAGNITUDE_HOME:-$HOME/.sokkan}"
PY_DIR="$ROOT/py"
AGENT_DIR="$ROOT/agent"

log()  { printf '\033[1;33m[magnitude]\033[0m %s\n' "$1" >&2; }
die()  { printf '\033[1;31m[magnitude] %s\033[0m\n' "$1" >&2; exit 1; }

[ -n "$TOKEN" ] && [ "$TOKEN" != "@TOKEN@" ] \
  || die "no pairing token — copy the command from the cockpit's Magnitude tab"
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v tar  >/dev/null 2>&1 || die "tar is required"

# --- 1. a usable Python 3.9+ (without tripping the macOS CLT popup) ----------
# On macOS with no Command Line Tools, `python3`/`python` are stubs that pop a
# GUI install dialog on *any* invocation — skip them and go straight to the
# standalone build, so the whole thing stays non-interactive.
HAVE_CLT=1
[ "$(uname -s)" = "Darwin" ] && { xcode-select -p >/dev/null 2>&1 || HAVE_CLT=0; }

PY=""
for cand in "$PY_DIR/bin/python3" python3 python; do
  case "$cand" in
    python3|python) [ "$HAVE_CLT" = 0 ] && continue ;;
  esac
  if "$cand" -c 'import sys,urllib.request; sys.exit(0 if sys.version_info>=(3,9) else 1)' \
      >/dev/null 2>&1; then
    PY="$cand"; break
  fi
done

if [ -z "$PY" ]; then
  log "no usable Python found — installing a standalone one under $PY_DIR (no system changes)"
  os="$(uname -s)"; arch="$(uname -m)"
  case "$os-$arch" in
    Darwin-arm64)              triple="aarch64-apple-darwin" ;;
    Darwin-x86_64)             triple="x86_64-apple-darwin" ;;
    Linux-x86_64)              triple="x86_64-unknown-linux-gnu" ;;
    Linux-aarch64|Linux-arm64) triple="aarch64-unknown-linux-gnu" ;;
    *) die "unsupported platform $os-$arch — install Python 3.9+ and re-run" ;;
  esac
  url="https://github.com/astral-sh/python-build-standalone/releases/download/${PYDATE}/cpython-${PYVER}%2B${PYDATE}-${triple}-install_only.tar.gz"
  mkdir -p "$ROOT"
  log "downloading Python ${PYVER}…"
  curl -fsSL "$url" -o "$ROOT/py.tar.gz" || die "Python download failed ($url)"
  rm -rf "$PY_DIR"; mkdir -p "$PY_DIR"
  tar xzf "$ROOT/py.tar.gz" -C "$PY_DIR" --strip-components=1 || die "Python extract failed"
  rm -f "$ROOT/py.tar.gz"
  PY="$PY_DIR/bin/python3"
  "$PY" -c 'import urllib.request' >/dev/null 2>&1 || die "standalone Python is not functional"
fi
log "python: $("$PY" --version 2>&1)"

# --- 2. the agent code (pure stdlib package) --------------------------------
log "fetching the Magnitude agent (ref ${REF})…"
rm -rf "$AGENT_DIR"; mkdir -p "$AGENT_DIR"
curl -fsSL "https://github.com/ninabot-ch/sokkan/archive/${REF}.tar.gz" \
  | tar xz -C "$AGENT_DIR" --strip-components=1 \
  || die "agent download failed"
[ -d "$AGENT_DIR/magnitude" ] || die "agent package not found in the tarball"

# --- 3. pair & run (foreground; Ctrl-C stops it) ----------------------------
# For a permanent node, wrap this in a launchd/systemd unit — see magnitude/README.md.
log "pairing with $COCKPIT — leave this running; watch the Magnitude tab"
cd "$AGENT_DIR"
exec "$PY" -m magnitude --cockpit="$COCKPIT" --token="$TOKEN"
