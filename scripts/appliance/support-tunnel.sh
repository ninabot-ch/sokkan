#!/bin/sh
# support-tunnel.sh — SOKKAN appliance: client-controlled, time-bounded support tunnel.
#
# Runs as root on the HOST (systemd units in deploy/appliance/systemd/). The api container
# talks to it only through files in $SOKKAN_APPLIANCE_DIR (request in, state + events out).
#
#   open <hours> [--by <who>] [--reason <text>]   bring the tunnel up for a bounded time
#   close [--by <who>]                            bring it down (idempotent)
#   status                                        one JSON line
#   expire                                        close at the deadline; fail-closed check
#   apply                                         execute support-request.json (once)
#
# Test overrides: SOKKAN_TUNNEL_UP_CMD / _DOWN_CMD / _PROBE_CMD, SOKKAN_SUPPORT_NOW.
set -eu
umask 022

# a person typing the command (not systemd) gets the same settings as the units
if [ -z "${SOKKAN_APPLIANCE_DIR:-}" ] && [ -r /etc/sokkan/appliance.env ]; then
    set -a; . /etc/sokkan/appliance.env; set +a
fi

DIR="${SOKKAN_APPLIANCE_DIR:-/var/lib/docker/volumes/sokkan_sokkan-data/_data/appliance}"
STATE="$DIR/support-state.json"
EVENTS="$DIR/support-events.jsonl"
REQUEST="$DIR/support-request.json"
LOCK="$DIR/.support.lock"
MAX_HOURS="${SOKKAN_SUPPORT_MAX_HOURS:-8}"
UP_CMD="${SOKKAN_TUNNEL_UP_CMD:-systemctl start tailscaled && tailscale up --accept-routes=false --ssh=false}"
DOWN_CMD="${SOKKAN_TUNNEL_DOWN_CMD:-tailscale down; systemctl stop tailscaled}"
PROBE_CMD="${SOKKAN_TUNNEL_PROBE_CMD:-tailscale status --json 2>/dev/null | grep -q '\"BackendState\": *\"Running\"'}"

COUNTER=0
S_OPEN=false S_OPENED=null S_DEADLINE=null S_BY="" S_REASON="" S_LAST=""

die() { echo "support-tunnel: $*" >&2; exit "${2:-2}"; }
now() { if [ -n "${SOKKAN_SUPPORT_NOW:-}" ]; then echo "$SOKKAN_SUPPORT_NOW"; else date +%s; fi; }
is_num() { case "$1" in ''|*[!0-9.]*|.|*.*.*) return 1;; esac; return 0; }
is_int() { case "$1" in ''|*[!0-9]*) return 1;; esac; return 0; }

check_text() { # reject control characters (they would break the one-line JSON)
    n=$(printf '%s' "$1" | LC_ALL=C tr -cd '\000-\037\177' | wc -c)
    [ "$n" -eq 0 ] || die "control characters are not allowed in '$2'"
}
jstr() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

check_hours() {
    is_num "$1" || die "hours must be a number (got '$1')"
    awk -v h="$1" -v m="$MAX_HOURS" 'BEGIN{exit !(h>0 && h<=m)}' ||
        die "hours must be > 0 and <= $MAX_HOURS (SOKKAN_SUPPORT_MAX_HOURS), got $1"
}

load_state() {
    [ -f "$STATE" ] || return 0
    v=$(sed -n 's/^  "open": *\([a-z]*\),\{0,1\}$/\1/p' "$STATE" | head -n1); [ -n "$v" ] && S_OPEN=$v
    v=$(sed -n 's/^  "opened_at": *\([0-9]*\),\{0,1\}$/\1/p' "$STATE" | head -n1); [ -n "$v" ] && S_OPENED=$v
    v=$(sed -n 's/^  "deadline": *\([0-9]*\),\{0,1\}$/\1/p' "$STATE" | head -n1); [ -n "$v" ] && S_DEADLINE=$v
    S_BY=$(sed -n 's/^  "by": "\(.*\)",$/\1/p' "$STATE" | head -n1)
    S_REASON=$(sed -n 's/^  "reason": "\(.*\)",$/\1/p' "$STATE" | head -n1)
    S_LAST=$(sed -n 's/^  "last_request_id": "\(.*\)",$/\1/p' "$STATE" | head -n1)
    return 0
}

write_state() {
    tmp="$STATE.tmp.$$"
    printf '{\n  "open": %s,\n  "opened_at": %s,\n  "deadline": %s,\n  "by": "%s",\n  "reason": "%s",\n  "last_request_id": "%s",\n  "updated_at": %s\n}\n' \
        "$S_OPEN" "$S_OPENED" "$S_DEADLINE" "$S_BY" "$S_REASON" "$S_LAST" "$(now)" > "$tmp"
    chmod 644 "$tmp"
    mv -f "$tmp" "$STATE"
}

# event <event> <by(escaped)> <hours|null> <deadline|null> <reason(escaped)>
event() {
    COUNTER=$((COUNTER + 1))
    t=$(now)
    printf '{"id": "%s-%s-%s", "ts": %s, "event": "%s", "by": "%s", "hours": %s, "deadline": %s, "reason": "%s"}\n' \
        "$t" "$$" "$COUNTER" "$t" "$1" "$2" "$3" "$4" "$5" >> "$EVENTS"
    chmod 644 "$EVENTS" 2>/dev/null || true
}

probe_up() { sh -c "$PROBE_CMD" >/dev/null 2>&1; }

do_open() { # hours by(escaped) reason(escaped)
    t=$(now)
    dl=$(awk -v n="$t" -v h="$1" 'BEGIN{printf "%d", n + h*3600}')
    if sh -c "$UP_CMD" >&2; then
        S_OPEN=true S_OPENED=$t S_DEADLINE=$dl S_BY=$2 S_REASON=$3
        write_state
        event opened "$2" "$1" "$dl" "$3"
        return 0
    fi
    S_OPEN=false S_OPENED=null S_DEADLINE=null S_BY=$2 S_REASON=$3
    write_state
    event open_failed "$2" "$1" null "$3"
    echo "support-tunnel: tunnel up command failed" >&2
    return 1
}

do_close() { # by(escaped) [event]
    was=$S_OPEN dl=$S_DEADLINE
    sh -c "$DOWN_CMD" >&2 || echo "support-tunnel: warning: down command failed (expire will re-check)" >&2
    S_OPEN=false S_OPENED=null S_DEADLINE=null S_BY=$1
    write_state
    if [ "$was" = true ]; then
        event "${2:-closed}" "$1" null "$dl" ""
    fi
    return 0
}

cmd_expire() {
    load_state
    t=$(now)
    if [ "$S_OPEN" = true ]; then
        if is_int "$S_DEADLINE" && [ "$t" -ge "$S_DEADLINE" ]; then
            do_close "system:deadline" expired
        fi
    elif probe_up; then
        # state says closed but the tunnel is up: fail closed
        sh -c "$DOWN_CMD" >&2 || true
        S_BY="system:fail-closed"
        write_state
        event forced_closed "system:fail-closed" null null "tunnel was up while the state said closed"
    fi
}

cmd_status() {
    load_state
    t=$(now)
    rem=0
    if [ "$S_OPEN" = true ] && is_int "$S_DEADLINE" && [ "$S_DEADLINE" -gt "$t" ]; then rem=$((S_DEADLINE - t)); fi
    printf '{"open": %s, "opened_at": %s, "deadline": %s, "by": "%s", "remaining_s": %s}\n' \
        "$S_OPEN" "$S_OPENED" "$S_DEADLINE" "$S_BY" "$rem"
}

cmd_apply() {
    [ -f "$REQUEST" ] || { echo "support-tunnel: no request file" >&2; return 0; }
    load_state
    j=$(tr -d '\n' < "$REQUEST")
    field() { printf '%s' "$j" | sed -n "s/.*\"$1\": *\"\\([^\"]*\\)\".*/\\1/p"; }
    numfield() { printf '%s' "$j" | sed -n "s/.*\"$1\": *\\([0-9.]*\\).*/\\1/p"; }
    rid=$(field id | tr -cd 'A-Za-z0-9._-')
    action=$(field action)
    by=$(field by | tr -cd 'A-Za-z0-9@._+-')
    hours=$(numfield hours)
    rts=$(numfield ts)
    [ -n "$rid" ] || die "request without id" 1
    if [ "$rid" = "$S_LAST" ]; then echo "support-tunnel: request $rid already applied" >&2; return 0; fi
    t=$(now)
    S_LAST=$rid
    rts_i=${rts%%.*}
    if ! is_int "$rts_i" || [ $((t - rts_i)) -gt 600 ]; then
        echo "support-tunnel: request $rid too old or without ts, ignored" >&2
        write_state
        return 0
    fi
    [ -n "$by" ] || by="cockpit"
    case "$action" in
        open)
            check_hours "$hours"
            do_open "$hours" "$by" "requested from the cockpit" ;;
        close)
            do_close "$by" ;;
        *) write_state; die "unknown action '$action'" 1 ;;
    esac
}

acquire_lock() {
    mkdir -p "$DIR"
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$LOCK"
        flock 9
    else
        i=0
        until mkdir "$LOCK.d" 2>/dev/null; do
            i=$((i + 1)); [ "$i" -le 60 ] || die "cannot get the lock" 1
            sleep 1
        done
        trap 'rmdir "$LOCK.d" 2>/dev/null || true' EXIT
    fi
}

usage() { echo "usage: $0 open <hours> [--by who] [--reason text] | close [--by who] | status | expire | apply" >&2; exit 2; }

[ $# -ge 1 ] || usage
sub=$1; shift
BY="${SUDO_USER:-}"; [ -n "$BY" ] || BY="local-admin"
REASON=""
case "$sub" in
    open)
        [ $# -ge 1 ] || usage
        HOURS=$1; shift
        ;;
    close|status|expire|apply) ;;
    *) usage ;;
esac
while [ $# -gt 0 ]; do
    case "$1" in
        --by) [ $# -ge 2 ] || usage; BY=$2; shift 2 ;;
        --reason) [ $# -ge 2 ] || usage; REASON=$2; shift 2 ;;
        *) usage ;;
    esac
done
check_text "$BY" by
check_text "$REASON" reason
EBY=$(jstr "$BY")
EREASON=$(jstr "$REASON")

case "$sub" in
    status) cmd_status ;;
    open)
        check_hours "$HOURS"
        acquire_lock; load_state
        do_open "$HOURS" "$EBY" "$EREASON" ;;
    close) acquire_lock; load_state; do_close "$EBY" ;;
    expire) acquire_lock; cmd_expire ;;
    apply) acquire_lock; cmd_apply ;;
esac
