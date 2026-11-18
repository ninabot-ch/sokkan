#!/bin/sh
# Entry of the parity lab's kernel containers: the DSN is built from a password FILE (never in
# the compose file, never on a command line visible to `ps` on the host).
#   kernel.sh migrate              migrations of both databases, as the schema's owner
#   kernel.sh serve <host:port/db> the kernel, as the application role
set -eu
case "$1" in
    migrate)
        for db in sokkan sokkan_conf; do
            NOKKER_PG_DSN="postgresql://kernel_owner:$(cat /run/parity/kernel_owner)@pg:5432/$db" \
                python3 -m nokker --config /etc/kernel/config.yaml migrate
        done ;;
    serve)
        NOKKER_PG_DSN="postgresql://kernel_app:$(cat /run/parity/kernel_app)@$2"
        export NOKKER_PG_DSN
        exec python3 -m nokker --config /etc/kernel/config.yaml serve ;;
    *) echo "usage: kernel.sh migrate | serve <host:port/db>" >&2; exit 2 ;;
esac
