#!/bin/sh
# The parity lab: two kernels and their Postgres, on an EXPLICIT docker subnet, memory-capped.
#
#   tests/parity/lab/lab.sh up        write configs and secrets (first time), start, wait healthy
#   tests/parity/lab/lab.sh env       the variables run.py and the conformance tests need (eval it)
#   tests/parity/lab/lab.sh restart   restart the kernel under test (what the resume journey does)
#   tests/parity/lab/lab.sh down      stop and remove the containers and the network (data kept)
#   tests/parity/lab/lab.sh purge     down + remove the volume and PARITY_HOME's state
#
# Required: PARITY_KERNEL_IMAGE (pinned), PARITY_NET_SUBNET + PARITY_NET_GW (a free /24 and its
# gateway: never a pool chosen by docker), PARITY_UPSTREAM (the model as a CONTAINER reaches it,
# e.g. a published port on the host's own address), PARITY_MODEL (its name).
# Optional: PARITY_HOME (/srv/sokkan-parity), PARITY_WIRE_PORT (18790: the meter, published on
# 127.0.0.1), PARITY_CONF_UPSTREAM_PORT (18791: the scripted model of the conformance tests),
# PARITY_KERNEL_PORT (18742), PARITY_CONF_PORT (18743), PARITY_PG_PORT (15433, 127.0.0.1),
# PARITY_KERNEL_SANDBOX (local | docker), PARITY_KERNEL_MEM (2g; at most 7g on a shared machine),
# PARITY_THINKING (0), PARITY_MAX_CONCURRENT (2), PARITY_HOST_EMBED (1: the kernel's host-embed
# flags on, as SOKKAN ships them), PARITY_SANDBOX_IMAGE (the sandbox image when
# PARITY_KERNEL_SANDBOX=docker; the kernel then reaches the host's docker socket),
# PARITY_SANDBOX_MEMORY (e.g. 4g) and PARITY_SANDBOX_CPUS (e.g. 4): the sandbox's caps (kernel
# host-2, docker sandbox only; unset = no cap), PARITY_CONF_SANDBOX (local | docker: the
# conformance kernel's sandbox), PARITY_APPROVAL_WAIT_S (8: the kernel's approval_wait_s).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
: "${PARITY_HOME:=/srv/sokkan-parity}"
: "${PARITY_PROJECT:=sokkan-parity}"
: "${PARITY_WIRE_PORT:=18790}"
: "${PARITY_CONF_UPSTREAM_PORT:=18791}"
: "${PARITY_KERNEL_PORT:=18742}"
: "${PARITY_CONF_PORT:=18743}"
: "${PARITY_KERNEL_SANDBOX:=local}"
: "${PARITY_KERNEL_MEM:=2g}"
: "${PARITY_PG_PORT:=15433}"
: "${PARITY_THINKING:=0}"
: "${PARITY_MAX_CONCURRENT:=2}"
: "${PARITY_HOST_EMBED:=1}"
: "${PARITY_SANDBOX_IMAGE:=}"
: "${PARITY_SANDBOX_MEMORY:=}"
: "${PARITY_SANDBOX_CPUS:=}"
: "${PARITY_CONF_SANDBOX:=local}"
: "${PARITY_APPROVAL_WAIT_S:=8}"
PARITY_DOCKER_SOCK=/dev/null
PARITY_CONF_DOCKER_SOCK=/dev/null
PARITY_DOCKER_GID=65534
if [ "$PARITY_KERNEL_SANDBOX" = docker ] || [ "$PARITY_CONF_SANDBOX" = docker ]; then
    PARITY_DOCKER_GID=$(stat -c %g /var/run/docker.sock 2>/dev/null || echo 65534)
fi
[ "$PARITY_KERNEL_SANDBOX" = docker ] && PARITY_DOCKER_SOCK=/var/run/docker.sock
[ "$PARITY_CONF_SANDBOX" = docker ] && PARITY_CONF_DOCKER_SOCK=/var/run/docker.sock
export PARITY_HOME PARITY_PROJECT PARITY_KERNEL_PORT PARITY_CONF_PORT PARITY_KERNEL_MEM PARITY_PG_PORT

compose() {
    docker compose --env-file "$PARITY_HOME/.env" -f "$HERE/compose.yml" "$@"
}

need() {
    for v in "$@"; do
        eval "val=\${$v:-}"
        [ -n "$val" ] || { echo "lab.sh: $v is required" >&2; exit 2; }
    done
}

secret() {
    f="$PARITY_HOME/secrets/$1"
    [ -s "$f" ] || { umask 077; od -An -N24 -tx1 /dev/urandom | tr -d ' \n' > "$f"; }
    chmod 600 "$f"
}

render() {   # template, out, upstream, model, root, context, max_calls, host, port (+ R_SANDBOX)
    if [ "$PARITY_HOST_EMBED" = 1 ]; then he=true; chain=60; else he=false; chain=0; fi
    if [ "$R_SANDBOX" = docker ]; then req=true; else req=false; fi
    sbi=${PARITY_SANDBOX_IMAGE:-nikolaik/python-nodejs:python3.14-nodejs22}
    caps=""
    if [ "$R_SANDBOX" = docker ] && [ -n "$PARITY_SANDBOX_MEMORY" ]; then
        caps="memory: \"$PARITY_SANDBOX_MEMORY\""
    fi
    if [ "$R_SANDBOX" = docker ] && [ -n "$PARITY_SANDBOX_CPUS" ]; then
        caps="${caps:+$caps\n    }cpus: $PARITY_SANDBOX_CPUS"
    fi
    sed -e "s#@UPSTREAM@#$3#" -e "s#@MODEL@#$4#" -e "s#@ROOT@#$5#g" -e "s#@CONTEXT@#$6#" \
        -e "s#@MAX_CALLS@#$7#" -e "s#@INSTANCE@#$R_INSTANCE#" -e "s#@APPROVAL_WAIT@#$PARITY_APPROVAL_WAIT_S#" -e "s#@HOST@#$8#" -e "s#@PORT@#$9#" \
        -e "s#@SANDBOX@#$R_SANDBOX#" -e "s#@HOST_EMBED@#$he#" -e "s#@CHAIN_MAX@#$chain#" \
        -e "s#@REQUIRE_SANDBOX@#$req#" -e "s#@SANDBOX_IMAGE@#$sbi#" \
        -e "s|^    # @SANDBOX_CAPS@\$|${caps:+    $caps}|" "$1" > "$2"
    chmod 644 "$2"
}

open_dir() {  # a folder the kernel (uid 1000) writes into, whoever creates what inside it
    mkdir -p "$1"
    chown 1000:1000 "$1"
    if command -v setfacl >/dev/null 2>&1; then
        setfacl -m u:1000:rwx -d -m u:1000:rwx "$1"
    fi
}

case "${1:-}" in
up)
    need PARITY_KERNEL_IMAGE PARITY_NET_SUBNET PARITY_NET_GW PARITY_MODEL PARITY_UPSTREAM
    mkdir -p "$PARITY_HOME/secrets" "$PARITY_HOME/kernel" "$PARITY_HOME/kconf"
    chmod 700 "$PARITY_HOME/secrets"
    for s in pg_admin kernel_owner kernel_app; do secret "$s"; done
    # the kernels read their own password file only (uid 1000)
    chown 1000:1000 "$PARITY_HOME/secrets/kernel_owner" "$PARITY_HOME/secrets/kernel_app"
    # the same password for the conformance kernel, which runs as root without capabilities
    install -m 600 -o 0 -g 0 "$PARITY_HOME/secrets/kernel_app" "$PARITY_HOME/secrets/kernel_app.root"
    chmod 711 "$PARITY_HOME/secrets"
    for d in kernel/state kernel/token; do
        mkdir -p "$PARITY_HOME/$d"; chown 1000:1000 "$PARITY_HOME/$d"
    done
    # the conformance kernel runs as root (compose.yml): its folders stay root's
    mkdir -p "$PARITY_HOME/kconf/state" "$PARITY_HOME/kconf/token" "$PARITY_HOME/conf-ws/root"
    open_dir "$PARITY_HOME/ws"
    mkdir -p "$PARITY_HOME/meter"; chown 1000:1000 "$PARITY_HOME/meter"
    R_SANDBOX=$PARITY_KERNEL_SANDBOX R_INSTANCE=$PARITY_PROJECT-kernel
    render "$HERE/kernel.yaml.tmpl" "$PARITY_HOME/kernel/config.yaml" \
        "http://meter:18790/v1" "$PARITY_MODEL" "$PARITY_HOME/ws" true 60 0.0.0.0 8742
    # the conformance kernel: scripted model, host network; no sandbox unless
    # PARITY_CONF_SANDBOX=docker (C1-C9 played with the kernel's docker sandbox: C3 outside_root)
    R_SANDBOX=$PARITY_CONF_SANDBOX R_INSTANCE=$PARITY_PROJECT-conf
    render "$HERE/kernel.yaml.tmpl" "$PARITY_HOME/kconf/config.yaml" \
        "http://127.0.0.1:$PARITY_CONF_UPSTREAM_PORT/v1" scripted "$PARITY_HOME/conf-ws/root" false 20 \
        127.0.0.1 "$PARITY_CONF_PORT"
    umask 077
    cat > "$PARITY_HOME/.env" <<EOF
PARITY_PROJECT=$PARITY_PROJECT
PARITY_HOME=$PARITY_HOME
PARITY_KERNEL_IMAGE=$PARITY_KERNEL_IMAGE
PARITY_NET_SUBNET=$PARITY_NET_SUBNET
PARITY_NET_GW=$PARITY_NET_GW
PARITY_KERNEL_PORT=$PARITY_KERNEL_PORT
PARITY_CONF_PORT=$PARITY_CONF_PORT
PARITY_KERNEL_MEM=$PARITY_KERNEL_MEM
PARITY_PG_PORT=$PARITY_PG_PORT
PARITY_WIRE_PORT=$PARITY_WIRE_PORT
PARITY_UPSTREAM=$PARITY_UPSTREAM
PARITY_MODEL=$PARITY_MODEL
PARITY_THINKING=$PARITY_THINKING
PARITY_MAX_CONCURRENT=$PARITY_MAX_CONCURRENT
PARITY_DOCKER_SOCK=$PARITY_DOCKER_SOCK
PARITY_DOCKER_GID=$PARITY_DOCKER_GID
PARITY_CONF_DOCKER_SOCK=$PARITY_CONF_DOCKER_SOCK
EOF
    compose up -d --wait kernel kernel-conf
    compose ps
    ;;
env)
    cat <<EOF
export PARITY_WORKDIR=$PARITY_HOME/ws
export PARITY_WIRE_URL=http://127.0.0.1:$PARITY_WIRE_PORT
export PARITY_KERNEL_URL=http://127.0.0.1:$PARITY_KERNEL_PORT
export PARITY_KERNEL_TOKEN_FILE=$PARITY_HOME/kernel/token/local-token
export PARITY_KERNEL_RESTART_CMD='docker restart $PARITY_PROJECT-kernel-1'
export PARITY_KERNEL_SANDBOX=$PARITY_KERNEL_SANDBOX
export PARITY_KERNEL_IMAGE=${PARITY_KERNEL_IMAGE:-}
export PARITY_HOST_EMBED=$PARITY_HOST_EMBED
export PARITY_KERNEL_PG_CMD='docker exec -i $PARITY_PROJECT-pg-1 psql -U parity -d sokkan -tA'
export SOKKAN_KERNEL_MCP_HEADERS=$([ "$PARITY_HOST_EMBED" = 1 ] && echo 1 || echo 0)
export SOKKAN_KERNEL_DENY_TOOLS=$([ "$PARITY_HOST_EMBED" = 1 ] && echo memory_note || echo)
export SOKKAN_KERNEL_TEST_URL=http://127.0.0.1:$PARITY_CONF_PORT
export SOKKAN_KERNEL_TEST_TOKEN_FILE=$PARITY_HOME/kconf/token/local-token
export SOKKAN_KERNEL_TEST_ROOT=$PARITY_HOME/conf-ws/root
export SOKKAN_KERNEL_TEST_UPSTREAM_PORT=$PARITY_CONF_UPSTREAM_PORT
export SOKKAN_KERNEL_TEST_SANDBOX=$PARITY_CONF_SANDBOX
export PARITY_APPROVAL_WAIT_S=$PARITY_APPROVAL_WAIT_S
export SOKKAN_KERNEL_TEST_RESTART_CMD='docker restart $PARITY_PROJECT-kernel-conf-1'
EOF
    ;;
restart)
    docker restart "$PARITY_PROJECT-kernel-1" ;;
down)
    compose down ;;
purge)
    compose down -v
    rm -rf -- "${PARITY_HOME:?}/kernel" "${PARITY_HOME:?}/kconf" "${PARITY_HOME:?}/ws" "${PARITY_HOME:?}/conf-ws"
    ;;
*)
    sed -n '2,23p' "$0"; exit 2 ;;
esac
