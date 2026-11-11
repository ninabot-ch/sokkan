#!/bin/sh
# sokkan verify-release — is this release what the publisher signed? Used by install.sh,
# upgrade.sh and rollback.sh BEFORE anything is put in place.
#
#   ./scripts/verify-release.sh <sokkan-x.tar.gz>           tarball: sha256 + signature
#   ./scripts/verify-release.sh --image <repo@sha256:…>     a prebuilt image, by digest
#
# What is checked, next to the tarball (same name + suffix, as published in dist/):
#   <tarball>.sha256   hex digest — a mismatch ALWAYS fails
#   <tarball>.sig      cosign signature of the tarball (key pair, no transparency log:
#                      the box may be offline). Verified with the public key ALREADY on this
#                      machine — never one that came with the download.
# Settings (environment, or /etc/sokkan/appliance.env):
#   SOKKAN_REQUIRE_SIGNATURE=1   fail closed: no signature, no key or no cosign = refused
#                                (the appliance profile sets it). Default 0: a release without
#                                signature is accepted with a warning, a BAD one never is.
#   SOKKAN_COSIGN_PUB            public key (default /etc/sokkan/cosign.pub)
#   SOKKAN_COSIGN                cosign binary (default: `cosign` in PATH, then
#                                /opt/sokkan/libexec/cosign); pinned build: cosign v2.4.1,
#                                sha256 in deploy/appliance/versions.env
# Exit: 0 verified (or accepted unsigned when not required) · 3 refused · 2 usage.
set -eu

[ -r /etc/sokkan/appliance.env ] && [ -z "${SOKKAN_VERIFY_NO_ENVFILE:-}" ] && {
  # only the settings of this script, without overriding the caller's environment
  for k in SOKKAN_REQUIRE_SIGNATURE SOKKAN_COSIGN_PUB SOKKAN_COSIGN; do
    eval "cur=\${$k:-}"
    if [ -z "$cur" ]; then
      v="$(sed -n "s/^$k=//p" /etc/sokkan/appliance.env | tail -n 1)"
      [ -n "$v" ] && export "$k=$v"
    fi
  done
}
REQUIRE="${SOKKAN_REQUIRE_SIGNATURE:-0}"
PUB="${SOKKAN_COSIGN_PUB:-/etc/sokkan/cosign.pub}"
say() { echo "verify: $*" >&2; }
refuse() { echo "✗ verify: $* — REFUSED, nothing was changed." >&2; exit 3; }

cosign_bin() {
  if [ -n "${SOKKAN_COSIGN:-}" ]; then [ -x "$SOKKAN_COSIGN" ] && { echo "$SOKKAN_COSIGN"; return 0; }; return 1; fi
  command -v cosign 2>/dev/null && return 0
  [ -x /opt/sokkan/libexec/cosign ] && { echo /opt/sokkan/libexec/cosign; return 0; }
  return 1
}

# can_verify: sets COSIGN; when signatures are required, a missing tool or key is a refusal
need_cosign() {
  COSIGN="$(cosign_bin || true)"
  if [ -z "$COSIGN" ]; then
    [ "$REQUIRE" = 1 ] && refuse "signature required but no cosign binary (SOKKAN_COSIGN)"
    return 1
  fi
  if [ ! -r "$PUB" ]; then
    [ "$REQUIRE" = 1 ] && refuse "signature required but no public key at $PUB (SOKKAN_COSIGN_PUB)"
    return 1
  fi
  return 0
}

if [ "${1:-}" = "--image" ]; then
  REF="${2:-}"
  case "$REF" in *@sha256:????????????????????????????????????????????????????????????????) ;;
    *) refuse "an image is verified by digest (repo@sha256:…), got '$REF'" ;; esac
  if ! need_cosign; then say "WARNING image $REF NOT verified (no cosign or no key; SOKKAN_REQUIRE_SIGNATURE=0)"; exit 0; fi
  "$COSIGN" verify --key "$PUB" --insecure-ignore-tlog=true ${SOKKAN_COSIGN_IMAGE_FLAGS:-} "$REF" >/dev/null 2>&1 \
    || refuse "image $REF is not signed by $PUB"
  say "image signature OK ($REF)"
  exit 0
fi

F="${1:-}"
[ -n "$F" ] && [ -f "$F" ] || { sed -n '2,24p' "$0"; exit 2; }
if command -v sha256sum >/dev/null 2>&1; then sha() { sha256sum "$1" | cut -d' ' -f1; }
else sha() { shasum -a 256 "$1" | cut -d' ' -f1; }; fi

if [ -f "$F.sha256" ]; then
  want="$(tr -d '\r' < "$F.sha256" | awk 'NR==1{print $1}')"
  got="$(sha "$F")"
  [ "$want" = "$got" ] || refuse "sha256 of $(basename "$F") is $got, the release says $want"
  say "sha256 OK"
elif [ "$REQUIRE" = 1 ]; then
  refuse "signature required but $(basename "$F").sha256 is missing"
fi

if [ ! -f "$F.sig" ]; then
  [ "$REQUIRE" = 1 ] && refuse "signature required but $(basename "$F").sig is missing"
  say "WARNING $(basename "$F") has no signature — accepted (SOKKAN_REQUIRE_SIGNATURE=0)"
  exit 0
fi
if ! need_cosign; then
  say "WARNING signature present but NOT verified (no cosign or no key at $PUB; SOKKAN_REQUIRE_SIGNATURE=0)"
  exit 0
fi
"$COSIGN" verify-blob --key "$PUB" --signature "$F.sig" --insecure-ignore-tlog=true "$F" >/dev/null 2>&1 \
  || refuse "the signature of $(basename "$F") does not match the key $PUB"
say "signature OK ($(basename "$F"), key $PUB)"
