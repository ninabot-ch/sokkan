#!/usr/bin/env bash
# teams-register.sh — register Nina (SOKKAN) in a Microsoft 365 tenant.
#
# Two modes (docs/enterprise/TEAMS-SETUP.md):
#
#  --mode portal (DEFAULT — no Azure subscription, no Azure CLI): prints the exact steps of the
#    Teams Developer Portal (bot + its Entra app, endpoint, client secret, Teams channel) and of
#    the Entra admin center (optional Graph permissions + admin consent) with the values of
#    this instance filled in; with --app-id (the bot id the portal gave), builds the app
#    package and prints the SOKKAN_TEAMS_* lines. Changes nothing anywhere.
#
#  --mode azure (an Azure SUBSCRIPTION in the tenant + `az login`): does it with the Azure CLI —
#    multi-tenant app registration + service principal, a client secret (written to a 0600
#    file, never printed), an Azure Bot (MultiTenant, F0 — the Bot Framework issuer SOKKAN
#    verifies; the tenant is enforced by SOKKAN on every activity) with the Teams channel, optional
#    Graph permissions (--calendar / --presence) + admin consent, the package.
#    --dry-run prints every command and changes nothing.
#
# Usage:
#   scripts/teams-register.sh --public-url https://sokkan.example.ch [--app-id <bot id>] \
#       [--tenant-id <tenant>] [--calendar] [--presence] [--out ./teams-app]
#   scripts/teams-register.sh --mode azure --public-url https://sokkan.example.ch \
#       --resource-group rg-sokkan [--name nina-sokkan] [--calendar] [--presence] \
#       [--secret-file ./teams-app-secret] [--no-bot] [--dry-run]
set -euo pipefail

MODE=portal
APP_ID_IN="${SOKKAN_TEAMS_APP_ID:-}"
TENANT_IN="${SOKKAN_TEAMS_TENANT_ID:-}"
PUBLIC_URL="${SOKKAN_TEAMS_PUBLIC_URL:-${SOKKAN_PUBLIC_URL:-}}"
RG=""
NAME="nina-sokkan"
DISPLAY="Nina (SOKKAN)"
CALENDAR=0
PRESENCE=0
BOT=1
DRY=0
SECRET_FILE="./teams-app-secret"
OUT="./teams-app"
GRAPH_APP="00000003-0000-0000-c000-000000000000"   # Microsoft Graph (well-known app id)

usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --app-id) APP_ID_IN="$2"; shift 2 ;;
    --tenant-id) TENANT_IN="$2"; shift 2 ;;
    --public-url) PUBLIC_URL="$2"; shift 2 ;;
    --resource-group|-g) RG="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --display-name) DISPLAY="$2"; shift 2 ;;
    --calendar) CALENDAR=1; shift ;;
    --presence) PRESENCE=1; shift ;;
    --no-bot) BOT=0; shift ;;
    --secret-file) SECRET_FILE="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 2 ;;
  esac
done

die() { echo "teams-register: $*" >&2; exit 2; }
case "$PUBLIC_URL" in https://*) ;; *) die "--public-url must be the public https address of the instance" ;; esac
HOST="${PUBLIC_URL#https://}"; HOST="${HOST%%/*}"
ENDPOINT="https://${HOST}/api/teams/messages"
HERE=$(cd "$(dirname "$0")" && pwd)

if [ "$MODE" = portal ]; then
  PERMS=""
  [ "$CALENDAR" = 1 ] && PERMS="Calendars.Read"
  [ "$PRESENCE" = 1 ] && PERMS="${PERMS:+$PERMS, }Presence.Read.All"
  cat >&2 <<STEPS
== Teams Developer Portal (no Azure subscription needed) — a tenant admin, in a browser
 1. https://dev.teams.microsoft.com → Tools → Bot management → « + New Bot » → name: $DISPLAY
    (it creates the bot AND its Entra app registration in your tenant; the bot id = that
    app's Application (client) ID).
 2. The bot → Configure → Endpoint address: $ENDPOINT → Save.
 3. The bot → Client secrets → « Add a client secret for your bot » → copy it ONCE into your
    secret store (never into a chat, a ticket or this shell's history).
 4. The bot → Channels: Microsoft Teams must be listed (it is by default).
 5. Entra admin center → App registrations → All applications → the bot's app:
    - Authentication → Supported account types: « Multiple organizations » — LEAVE IT
      (SOKKAN verifies the Bot Framework issuer and enforces the tenant itself);
    - Overview: note the Directory (tenant) ID.
STEPS
  if [ -n "$PERMS" ]; then
    cat >&2 <<STEPS
 6. Same app → API permissions → Add → Microsoft Graph → APPLICATION permissions: $PERMS
    → « Grant admin consent for <tenant> ».
STEPS
    [ "$CALENDAR" = 1 ] && echo "    Restrict Calendars.Read to the POC mailboxes (New-ApplicationAccessPolicy, TEAMS.md § 3.2 step 3)." >&2
  else
    echo " 6. (no Graph permission: Nina answers and posts approvals with Bot Framework only;" >&2
    echo "    --calendar / --presence for the brief's Graph reads)" >&2
  fi
  if [ -z "$APP_ID_IN" ]; then
    echo " 7. Run again with --app-id <bot id> [--tenant-id <tenant id>] to build the app package." >&2
    exit 0
  fi
  echo "== 7. app package → $OUT" >&2
  SOKKAN_TEAMS_APP_ID="$APP_ID_IN" python3 "$HERE/teams-manifest.py" --public-url "$PUBLIC_URL" --out "$OUT" >&2
  cat >&2 <<STEPS
 8. Teams admin center → Teams apps → Manage apps → « Upload new app » → $OUT/sokkan-teams-app.zip
    (or Developer Portal → Apps → Import app). TEAMS-SETUP.md § 5.
STEPS
  cat <<ENV
# --- env of the SOKKAN instance (docs/enterprise/TEAMS.md § 4) ---
SOKKAN_FEATURE_TEAMS=1
SOKKAN_TEAMS_APP_ID=$APP_ID_IN
SOKKAN_TEAMS_TENANT_ID=${TENANT_IN:-<Directory (tenant) ID>}
SOKKAN_TEAMS_PUBLIC_URL=$PUBLIC_URL
SOKKAN_TEAMS_APP_PASSWORD_FILE=<0600 file holding the client secret of step 3>
ENV
  exit 0
fi
[ "$MODE" = azure ] || die "--mode portal|azure"
[ "$BOT" = 0 ] || [ -n "$RG" ] || die "--resource-group is required for the Azure Bot (or --no-bot)"
[[ "$NAME" =~ ^[A-Za-z0-9_-]{4,42}$ ]] || die "--name: 4-42 characters among a-z A-Z 0-9 - _"

# run CMD… : executes, or prints in --dry-run (the output then is a placeholder)
run() {
  if [ "$DRY" = 1 ]; then
    { printf '+'; printf ' %q' "$@"; printf '\n'; } >&2
    echo "<${PLACEHOLDER:-value}>"
  else
    "$@"
  fi
}

[ "$DRY" = 1 ] || command -v az >/dev/null || die "az (Azure CLI) not found — https://aka.ms/azcli, or use --dry-run and the portal"

echo "== tenant" >&2
TENANT=$(PLACEHOLDER=tenant-id run az account show --query tenantId -o tsv)

echo "== 1. app registration (multi-tenant: the Bot Framework issuer SOKKAN expects)" >&2
APP_ID=$(PLACEHOLDER=app-id run az ad app create --display-name "$DISPLAY" \
          --sign-in-audience AzureADMultipleOrgs --query appId -o tsv)
PLACEHOLDER=sp run az ad sp create --id "$APP_ID" --query id -o tsv >/dev/null

echo "== 2. client secret → $SECRET_FILE (0600, never printed)" >&2
if [ "$DRY" = 1 ]; then
  printf '+ %s\n' "az ad app credential reset --id $APP_ID --display-name sokkan-teams --years 2 --append --query password -o tsv > $SECRET_FILE" >&2
else
  ( umask 077
    az ad app credential reset --id "$APP_ID" --display-name sokkan-teams --years 2 --append \
      --query password -o tsv > "$SECRET_FILE" )
  chmod 600 "$SECRET_FILE"
fi

if [ "$CALENDAR" = 1 ] || [ "$PRESENCE" = 1 ]; then
  echo "== 4. Microsoft Graph application permissions + admin consent" >&2
  for perm in $([ "$CALENDAR" = 1 ] && echo Calendars.Read) $([ "$PRESENCE" = 1 ] && echo Presence.Read.All); do
    # the role id is looked up by name (never hard-coded)
    RID=$(PLACEHOLDER="role-id-$perm" run az ad sp show --id "$GRAPH_APP" \
           --query "appRoles[?value=='$perm'].id | [0]" -o tsv)
    run az ad app permission add --id "$APP_ID" --api "$GRAPH_APP" --api-permissions "${RID}=Role" >/dev/null
  done
  run az ad app permission admin-consent --id "$APP_ID" >/dev/null
  echo "   limit Calendars.Read to the POC mailboxes: New-ApplicationAccessPolicy (TEAMS.md § 3)" >&2
fi

if [ "$BOT" = 1 ]; then
  echo "== 3. Azure Bot (MultiTenant) → $ENDPOINT, channel Microsoft Teams" >&2
  run az bot create --resource-group "$RG" --name "$NAME" --app-type MultiTenant \
      --appid "$APP_ID" --endpoint "$ENDPOINT" --sku F0 \
      --display-name "$DISPLAY" -o none >/dev/null
  run az bot msteams create --resource-group "$RG" --name "$NAME" -o none >/dev/null
else
  echo "== 3. skipped (--no-bot): register the bot in the Teams Developer Portal, endpoint $ENDPOINT" >&2
fi

echo "== 5. app package → $OUT" >&2
if [ "$DRY" = 1 ]; then
  printf '+ %s\n' "SOKKAN_TEAMS_APP_ID=$APP_ID python3 $HERE/teams-manifest.py --public-url $PUBLIC_URL --out $OUT" >&2
else
  SOKKAN_TEAMS_APP_ID="$APP_ID" python3 "$HERE/teams-manifest.py" --public-url "$PUBLIC_URL" --out "$OUT" >&2
fi

cat <<ENV
# --- env of the SOKKAN instance (docs/enterprise/TEAMS.md § 4) ---
SOKKAN_FEATURE_TEAMS=1
SOKKAN_TEAMS_APP_ID=$APP_ID
SOKKAN_TEAMS_TENANT_ID=$TENANT
SOKKAN_TEAMS_PUBLIC_URL=$PUBLIC_URL
SOKKAN_TEAMS_APP_PASSWORD_FILE=<0600 file with the content of $SECRET_FILE; then delete $SECRET_FILE>
ENV
