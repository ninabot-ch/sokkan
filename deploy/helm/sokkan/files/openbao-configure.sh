#!/bin/sh
# Configure OpenBao (or HashiCorp Vault: same CLI verbs with `vault`) for ONE SOKKAN instance.
# Idempotent. Run with an operator token (BAO_ADDR, BAO_TOKEN [, BAO_NAMESPACE, BAO_CACERT]).
#
#   INST       instance id (KV prefix sokkan/<INST>/, transit key sokkan-<INST>)
#   KV         KV v2 mount (default secret)       TRANSIT  transit mount (default transit)
#   TKEY       transit key (default sokkan-<INST>)
#   AUTH       kubernetes | approle | none        AUTH_MOUNT (default kubernetes / approle)
#   SA, NS     ServiceAccount + namespace of the api (kubernetes auth)
#   K8S_CONFIG extra args for auth/<mount>/config (default: kubernetes_host of the pod —
#              OpenBao in the cluster uses its own ServiceAccount to call TokenReview)
# The policy below is THE policy of docs/enterprise/SECRETS.md § 3 (tests check it).
set -eu
BAO="$(command -v bao || command -v vault)"
: "${INST:?INST (instance id) is required}"
KV="${KV:-secret}"; TRANSIT="${TRANSIT:-transit}"; TKEY="${TKEY:-sokkan-$INST}"
AUTH="${AUTH:-kubernetes}"
POLICY="sokkan-$INST"

"$BAO" secrets list -format=json | grep -q "\"$TRANSIT/\"" || "$BAO" secrets enable -path="$TRANSIT" transit
"$BAO" secrets list -format=json | grep -q "\"$KV/\"" || "$BAO" secrets enable -path="$KV" kv-v2
"$BAO" read "$TRANSIT/keys/$TKEY" >/dev/null 2>&1 || "$BAO" write -f "$TRANSIT/keys/$TKEY" type=aes256-gcm96

"$BAO" policy write "$POLICY" - <<HCL
path "$KV/data/sokkan/$INST/*"     { capabilities = ["create", "read", "update", "delete"] }
path "$KV/metadata/sokkan/$INST/*" { capabilities = ["list", "read", "delete"] }
path "$TRANSIT/encrypt/$TKEY" { capabilities = ["update"] }
path "$TRANSIT/decrypt/$TKEY" { capabilities = ["update"] }
path "$TRANSIT/rewrap/$TKEY"  { capabilities = ["update"] }
path "$TRANSIT/keys/$TKEY"    { capabilities = ["read"] }
HCL
# the operator's rotation policy (secrets-rotate.py --master): the above + rotate
"$BAO" policy write "$POLICY-rotate" - <<HCL
path "$KV/data/sokkan/$INST/*"     { capabilities = ["create", "read", "update", "delete"] }
path "$KV/metadata/sokkan/$INST/*" { capabilities = ["list", "read", "delete"] }
path "$TRANSIT/encrypt/$TKEY" { capabilities = ["update"] }
path "$TRANSIT/decrypt/$TKEY" { capabilities = ["update"] }
path "$TRANSIT/rewrap/$TKEY"  { capabilities = ["update"] }
path "$TRANSIT/keys/$TKEY"    { capabilities = ["read"] }
path "$TRANSIT/keys/$TKEY/rotate" { capabilities = ["update"] }
HCL

case "$AUTH" in
  kubernetes)
    : "${SA:?SA (api ServiceAccount) is required}"; : "${NS:?NS (namespace) is required}"
    M="${AUTH_MOUNT:-kubernetes}"
    "$BAO" auth list -format=json | grep -q "\"$M/\"" || "$BAO" auth enable -path="$M" kubernetes
    # shellcheck disable=SC2086  # K8S_CONFIG is a list of key=value arguments
    "$BAO" write "auth/$M/config" ${K8S_CONFIG:-kubernetes_host=https://kubernetes.default.svc}
    "$BAO" write "auth/$M/role/${ROLE:-sokkan}" bound_service_account_names="$SA" \
      bound_service_account_namespaces="$NS" token_policies="$POLICY" token_ttl=1h token_max_ttl=24h
    ;;
  approle)
    M="${AUTH_MOUNT:-approle}"
    "$BAO" auth list -format=json | grep -q "\"$M/\"" || "$BAO" auth enable -path="$M" approle
    "$BAO" write "auth/$M/role/${ROLE:-sokkan-$INST}" token_policies="$POLICY" token_ttl=1h \
      token_max_ttl=24h secret_id_ttl=0
    echo "role-id:   $("$BAO" read -field=role_id "auth/$M/role/${ROLE:-sokkan-$INST}/role-id")"
    echo "secret-id: bao write -f -field=secret_id auth/$M/role/${ROLE:-sokkan-$INST}/secret-id"
    ;;
  none) ;;
  *) echo "AUTH must be kubernetes, approle or none" >&2; exit 2 ;;
esac
echo "configured: instance $INST, policy $POLICY, transit $TRANSIT/keys/$TKEY, KV $KV/sokkan/$INST/"
