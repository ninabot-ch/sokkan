#!/bin/sh
# One-shot of the parity lab: the kernel's schema and roles (deploy/kernel/db-init.sql) in the
# database of each kernel. Passwords are read from files, never passed on a command line.
set -eu
PGPASSWORD=$(cat /run/parity/pg_admin)
export PGPASSWORD
if ! psql -q -h pg -U parity -d sokkan -tAc "SELECT 1 FROM pg_database WHERE datname='sokkan_conf'" | grep -q 1; then
    psql -q -h pg -U parity -d sokkan -c 'CREATE DATABASE sokkan_conf'
fi
OWNER=$(cat /run/parity/kernel_owner)
APP=$(cat /run/parity/kernel_app)
for db in sokkan sokkan_conf; do
    # psql variables are read from its standard input, not from its arguments
    { printf '\\set owner_password %s\n\\set app_password %s\n' "'$OWNER'" "'$APP'"; cat /sql/db-init.sql; } \
        | psql -q -h pg -U parity -d "$db" -v ON_ERROR_STOP=1
done
