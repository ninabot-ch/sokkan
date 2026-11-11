#!/bin/sh
# sokkan appliance — the people of the box (local auth: a personal access code each).
#
#   accounts.sh add <e-mail> --name "Ana Example" --project <slug>   account + own project → prints the code ONCE
#   accounts.sh add <e-mail> --name … --role admin                    an instance administrator
#   accounts.sh code <e-mail>      a new code (the old one stops working)
#   accounts.sh revoke <e-mail>    no more access (open cockpit closed at once)
#   accounts.sh list               who has a code, last sign-in — never a code
#
# Runs backend/localaccounts.py in the api container, as the app's user. Each person appears
# under their own name in Operate › Journal.
set -eu
cd "$(dirname "$0")/../.."
DC="docker"; docker info >/dev/null 2>&1 || DC="sudo docker"
[ $# -gt 0 ] || { sed -n '2,12p' "$0"; exit 2; }
exec $DC compose exec -T -u 1000 -w /app/backend api python3 -m localaccounts "$@"
