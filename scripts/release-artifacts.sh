#!/bin/sh
# sokkan release-artifacts — what a release publishes NEXT TO its tarball so that an install
# can verify it (scripts/verify-release.sh) and run prebuilt images (upgrade.sh --images).
# Run by the maintainers' release script after `git archive` wrote the tarball:
#
#   scripts/release-artifacts.sh <dist-dir>/sokkan-<ver+hash>.tar.gz [--images <registry/prefix>]
#
# Writes, in the same folder:
#   <tarball>.sha256   hex digest
#   <tarball>.sig      cosign signature of the tarball (key pair, --tlog-upload=false: an
#                      on-prem box verifies offline)
#   <tarball>.images   with --images: `api=<repo>@sha256:…` and `web=…` — the images built
#                      FROM THIS TARBALL (not from the working tree), pushed and signed by digest
# Needs: SOKKAN_COSIGN_KEY (private key file) and COSIGN_PASSWORD (or SOKKAN_COSIGN_PASSWORD_FILE);
# cosign in PATH or SOKKAN_COSIGN; docker (buildx) for --images. The private key never goes in
# the repository nor on an appliance. Nothing is published by this script: it only writes
# files and pushes to the registry you name.
set -eu
TB="${1:-}"; [ -f "$TB" ] || { sed -n '2,18p' "$0"; exit 2; }
shift
REG=""
while [ $# -gt 0 ]; do case "$1" in --images) REG="${2:?registry/prefix}"; shift ;; *) echo "unknown option $1" >&2; exit 2 ;; esac; shift; done
COSIGN="${SOKKAN_COSIGN:-cosign}"
KEY="${SOKKAN_COSIGN_KEY:?SOKKAN_COSIGN_KEY = the release private key file}"
[ -n "${COSIGN_PASSWORD:-}" ] || { [ -r "${SOKKAN_COSIGN_PASSWORD_FILE:-}" ] && COSIGN_PASSWORD="$(cat "$SOKKAN_COSIGN_PASSWORD_FILE")"; }
export COSIGN_PASSWORD="${COSIGN_PASSWORD:-}"

sha256sum "$TB" | cut -d' ' -f1 > "$TB.sha256"
"$COSIGN" sign-blob --yes --key "$KEY" --tlog-upload=false --output-signature "$TB.sig" "$TB" >/dev/null 2>&1
echo "signed $(basename "$TB") (sha256 $(cut -c1-12 "$TB.sha256")…)"

[ -n "$REG" ] || exit 0
VER="$(basename "$TB" .tar.gz)"; VER="${VER#sokkan-}"
TAG="$(printf '%s' "$VER" | tr '+' '-')"            # a docker tag cannot hold `+`
SRC="$(mktemp -d)"; trap 'rm -rf "$SRC"' EXIT
tar xzf "$TB" -C "$SRC" --strip-components=1        # build what the tarball holds, nothing else
COMMIT="unknown"; case "$VER" in *+*) COMMIT="${VER#*+}" ;; esac
: > "$TB.images"
for name in api web; do
  ref="$REG/sokkan-$name:$TAG"
  docker build -q -f "$SRC/docker/$name.Dockerfile" --build-arg "SOKKAN_COMMIT=$COMMIT" -t "$ref" "$SRC" >/dev/null
  docker push -q "$ref" >/dev/null
  digest="$(docker inspect --format '{{index .RepoDigests 0}}' "$ref")"
  "$COSIGN" sign --yes --key "$KEY" --tlog-upload=false ${SOKKAN_COSIGN_IMAGE_FLAGS:-} "$digest" >/dev/null 2>&1
  printf '%s=%s\n' "$name" "$digest" >> "$TB.images"
  echo "image $name: $digest (signed)"
done
