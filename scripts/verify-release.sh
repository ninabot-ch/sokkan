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
# Fail closed (4.0.2): every release since 3.6.1 (FIRST_SIGNED) is published with both files. For
# those, a .sha256 or .sig that is missing, empty, unreadable or that cannot be checked (no key, no
# cosign/openssl) is a REFUSAL; a WRONG digest or signature always is, for any release.
# Settings (environment, or /etc/sokkan/appliance.env):
#   SOKKAN_ALLOW_UNSIGNED=1      accept a release whose .sha256/.sig is missing or cannot be checked,
#                                with a warning on stderr, also appended to .sokkan-install.log of
#                                the install (SOKKAN_INSTALL_LOG). Never a WRONG one.
#   SOKKAN_REQUIRE_SIGNATURE=1   strict: the above for EVERY release, even before 3.6.1, and
#                                SOKKAN_ALLOW_UNSIGNED is ignored (the appliance profile sets it).
#                                0 (the default) no longer lets an unsigned 3.6.1+ release through.
#   SOKKAN_COSIGN_PUB            public key (default /etc/sokkan/cosign.pub, then the key of the
#                                INSTALLED release, deploy/appliance/cosign.pub next to this script)
#   SOKKAN_COSIGN                cosign binary (default: `cosign` in PATH, then
#                                /opt/sokkan/libexec/cosign); pinned build: cosign v2.4.1,
#                                sha256 in deploy/appliance/versions.env. Without cosign, a tarball
#                                signature is checked with openssl (same ECDSA P-256 signature).
# Exit: 0 verified (or accepted: see above) · 3 refused · 2 usage.
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
ALLOW_UNSIGNED="${SOKKAN_ALLOW_UNSIGNED:-0}"
FIRST_SIGNED=3.6.1          # the first release published with .sha256 + .sig
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -n "${SOKKAN_COSIGN_PUB:-}" ]; then PUB="$SOKKAN_COSIGN_PUB"           # explicit: no fallback
elif [ -r /etc/sokkan/cosign.pub ]; then PUB=/etc/sokkan/cosign.pub
else PUB="$HERE/../deploy/appliance/cosign.pub"; fi
say() { echo "verify: $*" >&2; }
refuse() { echo "✗ verify: $* — REFUSED, nothing was changed." >&2; exit 3; }
red() { if [ -t 2 ] && [ -z "${NO_COLOR:-}" ]; then printf '\033[1;31m%s\033[0m\n' "$*" >&2; else printf '%s\n' "$*" >&2; fi; }

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
[ -n "$F" ] && [ -f "$F" ] || { sed -n '2,32p' "$0"; exit 2; }
if command -v sha256sum >/dev/null 2>&1; then sha() { sha256sum "$1" | cut -d' ' -f1; }
else sha() { shasum -a 256 "$1" | cut -d' ' -f1; }; fi
NAME="$(basename "$F")"
VER="${NAME%.tar.gz}"; VER="${VER#sokkan-}"

# signed_era <version> → 0 when that release was published signed (>= FIRST_SIGNED, or unknown)
signed_era() {
  v="${1#v}"; v="${v%%+*}"
  case "$v" in ''|*[!0-9.]*|.*|*.) return 0 ;; esac
  awk -v v="$v" -v f="$FIRST_SIGNED" 'BEGIN { n = split(v, a, "."); split(f, b, ".")
    for (i = 1; i <= 3; i++) { x = (i <= n) ? a[i] + 0 : 0; y = b[i] + 0
      if (x > y) exit 0; if (x < y) exit 1 }
    exit 0 }'
}

# sig_check <file> <signature> → 0 good · 1 WRONG signature · 2 cannot check (WHY says why)
sig_check() {
  if [ ! -r "$PUB" ] || ! grep -q 'BEGIN PUBLIC KEY' "$PUB" 2>/dev/null; then WHY="no readable public key at $PUB (SOKKAN_COSIGN_PUB)"; return 2; fi
  if [ -n "${SOKKAN_COSIGN:-}" ]; then
    [ -x "$SOKKAN_COSIGN" ] || { WHY="SOKKAN_COSIGN=$SOKKAN_COSIGN is not an executable"; return 2; }
    COSIGN="$SOKKAN_COSIGN"
  else
    COSIGN="$(cosign_bin || true)"
  fi
  if [ -n "$COSIGN" ]; then
    "$COSIGN" verify-blob --key "$PUB" --signature "$2" --insecure-ignore-tlog=true "$1" >/dev/null 2>&1 && return 0
    return 1
  fi
  command -v openssl >/dev/null 2>&1 || { WHY="neither cosign nor openssl is installed (SOKKAN_COSIGN)"; return 2; }
  openssl pkey -pubin -in "$PUB" -noout >/dev/null 2>&1 || { WHY="the public key $PUB cannot be read"; return 2; }
  der="$(mktemp "${TMPDIR:-/tmp}/sokkan-sig.XXXXXX")"
  rc=1
  if tr -d ' \r\n' < "$2" | openssl base64 -d -A > "$der" 2>/dev/null && [ -s "$der" ] \
     && openssl dgst -sha256 -verify "$PUB" -signature "$der" "$1" >/dev/null 2>&1; then rc=0; fi
  rm -f "$der"
  return "$rc"
}

MISSING=""
want=""
[ -s "$F.sha256" ] && want="$(tr -d '\r' < "$F.sha256" | awk 'NR==1{print $1}')"
if [ -z "$want" ]; then MISSING="$MISSING
    - $NAME.sha256: missing or empty"
elif [ "${#want}" != 64 ] || [ -n "$(printf '%s' "$want" | tr -d '0-9a-f')" ]; then MISSING="$MISSING
    - $NAME.sha256: unreadable (not a sha256)"
else
  got="$(sha "$F")"
  [ "$want" = "$got" ] || refuse "sha256 of $NAME is $got, the release says $want (never bypassed)"
  say "sha256 OK"
fi
if [ ! -s "$F.sig" ]; then MISSING="$MISSING
    - $NAME.sig: missing or empty"
else
  rc=0; WHY=""; sig_check "$F" "$F.sig" || rc=$?
  case "$rc" in
    0) say "signature OK ($NAME, key $PUB)" ;;
    1) refuse "the signature of $NAME does not match the key $PUB (never bypassed)" ;;
    *) MISSING="$MISSING
    - signature of $NAME cannot be checked: $WHY" ;;
  esac
fi
[ -n "$MISSING" ] || exit 0

if [ "$REQUIRE" != 1 ] && ! signed_era "$VER"; then
  say "WARNING $NAME predates signed releases ($FIRST_SIGNED): accepted NOT verified:$MISSING"
  exit 0
fi
if [ "$ALLOW_UNSIGNED" = 1 ] && [ "$REQUIRE" != 1 ]; then
  red "!! WARNING — SOKKAN_ALLOW_UNSIGNED=1: $NAME accepted WITHOUT a complete verification.
   What is missing:$MISSING
   Risk: nothing proves this release is the one the SOKKAN publishers signed; a modified or corrupted
   release would be put in place. Remove SOKKAN_ALLOW_UNSIGNED as soon as the files are served again.
!! AVERTISSEMENT — SOKKAN_ALLOW_UNSIGNED=1 : $NAME acceptée SANS vérification complète."
  if [ -n "${SOKKAN_INSTALL_LOG:-}" ]; then
    printf '%s verify-release.sh WARNING SOKKAN_ALLOW_UNSIGNED=1: %s accepted WITHOUT complete verification;%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$NAME" "$(printf '%s' "$MISSING" | tr '\n' ' ' | tr -s ' ')" >> "$SOKKAN_INSTALL_LOG" 2>/dev/null \
      && chmod 600 "$SOKKAN_INSTALL_LOG" 2>/dev/null || true
  fi
  exit 0
fi
[ "$ALLOW_UNSIGNED" = 1 ] && echo "verify: SOKKAN_ALLOW_UNSIGNED=1 is ignored: SOKKAN_REQUIRE_SIGNATURE=1 is stricter" >&2
cat >&2 <<EOM
✗ verify: $NAME cannot be verified — REFUSED, nothing was changed.$MISSING
  What to do: retry in a few minutes (a release may be being published); check that
  https://sokkan.ch/dist/ lists $NAME with its .sha256 and .sig; or, knowing the risk,
  SOKKAN_ALLOW_UNSIGNED=1 (docs/UPGRADE.md, « Verification »).
  (FR) Release non vérifiable : REFUSÉE, rien n'a été modifié. Réessayer dans quelques minutes ;
  vérifier que https://sokkan.ch/dist/ liste $NAME avec son .sha256 et son .sig ; ou, en
  connaissance du risque, SOKKAN_ALLOW_UNSIGNED=1.
EOM
exit 3
