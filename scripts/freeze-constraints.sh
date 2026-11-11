#!/bin/sh
# Write docker/constraints.txt = the exact version of every Python package of the api image.
# Run after changing backend/requirements.txt or memory/requirements.txt, commit the result:
#
#   ./scripts/freeze-constraints.sh            resolve in a throwaway python:3.12-slim container
#   ./scripts/freeze-constraints.sh <image>    freeze an image that is already built
#
# The image build then installs with `-c docker/constraints.txt`: two builds of one release
# get the same packages (docker/api.Dockerfile).
set -eu
cd "$(dirname "$0")/.."
OUT=docker/constraints.txt
HEAD="# Exact versions of the api image (direct and transitive) — written by scripts/freeze-constraints.sh.
# Do not edit by hand: change the requirements, run the script, commit."
if [ -n "${1:-}" ]; then
  LIST="$(docker run --rm --entrypoint pip "$1" freeze --all 2>/dev/null)"
else
  LIST="$(docker run --rm -v "$PWD/backend/requirements.txt:/b.txt:ro" -v "$PWD/memory/requirements.txt:/m.txt:ro" \
          python:3.12-slim sh -c 'pip install -q --no-cache-dir -r /b.txt -r /m.txt >/dev/null 2>&1 && pip freeze --all')"
fi
[ -n "$LIST" ] || { echo "freeze-constraints: no package list — nothing written" >&2; exit 1; }
# pip itself is the base image's; setuptools/wheel are not installed by us
printf '%s\n%s\n' "$HEAD" "$(printf '%s\n' "$LIST" | grep -v -i -E '^(pip|setuptools|wheel)==' | LC_ALL=C sort -f)" > "$OUT"
echo "wrote $OUT ($(grep -c '==' "$OUT") packages)"
