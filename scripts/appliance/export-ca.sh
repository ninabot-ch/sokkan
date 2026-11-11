#!/bin/sh
# sokkan appliance — the certificate authority of this box's HTTPS (`tls` service, Caddy local
# CA), to install ONCE on each workstation so that the browser trusts https://<box>.
#
#   ./scripts/appliance/export-ca.sh [output.crt]        default: ./sokkan-local-ca.crt
#
# Only the PUBLIC certificate leaves the container; the CA's private key stays in the
# caddy-data volume. Prints the SHA-256 fingerprint to read out when handing the file over.
# Install: Windows — double-click → « Trusted Root Certification Authorities » ;
# macOS — Keychain Access → System → trust « Always » ; Linux — /usr/local/share/ca-certificates
# + update-ca-certificates ; Firefox — Settings → Certificates → Authorities → Import.
set -eu
cd "$(dirname "$0")/../.."
OUT="${1:-sokkan-local-ca.crt}"
DC="docker"; docker info >/dev/null 2>&1 || DC="sudo docker"
$DC compose exec -T tls cat /data/caddy/pki/authorities/local/root.crt > "$OUT" \
  || { echo "export-ca: the tls service is not running (COMPOSE_PROFILES=appliance) or has not issued a certificate yet" >&2; rm -f "$OUT"; exit 1; }
grep -q "BEGIN CERTIFICATE" "$OUT" || { echo "export-ca: no certificate found" >&2; rm -f "$OUT"; exit 1; }
echo "wrote $OUT"
command -v openssl >/dev/null 2>&1 && openssl x509 -in "$OUT" -noout -fingerprint -sha256 -enddate
