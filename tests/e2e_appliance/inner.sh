#!/bin/sh
# Runs ON the test machine (the Docker-in-Docker container): tools, then one phase of suite.py.
#   inner.sh tools | prepare | appliance | after-reboot | upgrade | report
set -eu
PHASE="${1:?phase}"
if [ "$PHASE" = tools ]; then
  apk add --no-cache python3 curl restic openssl coreutils iproute2 iptables tar bash >/dev/null
  install -m 0755 /dists/cosign /usr/local/bin/cosign
  # the pinned build (deploy/appliance/versions.env) — a different binary is not used
  . /dists/versions.env
  echo "$COSIGN_SHA256  /usr/local/bin/cosign" | sha256sum -c - >/dev/null
  cosign version 2>&1 | sed -n 's/^GitVersion: */cosign /p'
  restic version | cut -d' ' -f1-2
  docker version --format 'docker {{.Server.Version}}'; docker compose version --short | sed 's/^/compose /'
  exit 0
fi
exec python3 /harness/e2e_appliance/suite.py "$PHASE"
