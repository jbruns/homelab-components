#!/bin/sh
# Ordered stop/start of Docker containers by a level label, across Compose projects.
# See README.md for configuration and installation.
#
#   stop   record running containers, then stop them from the highest level down to 0
#   start  detach and run `run` in the background (keeps a boot hook short)
#   run    wait for Docker, then bring targets up level 0 upwards, waiting for health
#   plan   print what `run` would do without changing anything
#
# Containers without a valid level are treated as the default level; a level of `skip` is never touched.
set -u

BASE=${STARTUP_ORDER_DIR:-/var/lib/startup-order}
STATE=$BASE/state
LOG=$BASE/last.log
LOCK=$BASE/lock
LABEL=${STARTUP_ORDER_LABEL:-startup-order}
MAX_LEVEL=${STARTUP_ORDER_MAX_LEVEL:-3}
DEFAULT_LEVEL=${STARTUP_ORDER_DEFAULT_LEVEL:-2}
REORDER_FROM=${STARTUP_ORDER_REORDER_FROM:-2}
LEVEL_TIMEOUT=${STARTUP_ORDER_LEVEL_TIMEOUT:-420}
DOCKER_WAIT=${STARTUP_ORDER_DOCKER_WAIT:-900}
SETTLE=${STARTUP_ORDER_SETTLE:-20}
SETTLE_MAX=${STARTUP_ORDER_SETTLE_MAX:-180}
DRY=0

invalid() { echo "startup-order: invalid $1: $2" >&2; exit 2; }

whole() {
    case $2 in
        '' | *[!0-9]*) invalid "STARTUP_ORDER_$1" "'$2' is not a whole number" ;;
    esac
}
whole MAX_LEVEL "$MAX_LEVEL"
whole DEFAULT_LEVEL "$DEFAULT_LEVEL"
whole REORDER_FROM "$REORDER_FROM"
whole LEVEL_TIMEOUT "$LEVEL_TIMEOUT"
whole DOCKER_WAIT "$DOCKER_WAIT"
whole SETTLE "$SETTLE"
whole SETTLE_MAX "$SETTLE_MAX"
[ "$DEFAULT_LEVEL" -le "$MAX_LEVEL" ] ||
    invalid STARTUP_ORDER_DEFAULT_LEVEL "$DEFAULT_LEVEL is above STARTUP_ORDER_MAX_LEVEL ($MAX_LEVEL)"

# Levels from $1 down to $2, or up from $1 to $2; nothing when the range is empty.
descending() { awk -v hi="$1" -v lo="$2" 'BEGIN { for (i = hi; i >= lo; i--) print i }'; }
ascending() { awk -v lo="$1" -v hi="$2" 'BEGIN { for (i = lo; i <= hi; i++) print i }'; }

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S%z') $*"; }

# name<TAB>level for every container, running or not.
inventory() {
    docker ps -a --format "{{.Names}}	{{.Label \"$LABEL\"}}" 2>/dev/null |
        awk -F'\t' -v d="$DEFAULT_LEVEL" -v max="$MAX_LEVEL" '
            $2 == "skip" { next }
            { l = $2; if (l !~ /^(0|[1-9][0-9]*)$/ || l + 0 > max + 0) l = d; print $1 "\t" l + 0 }'
}

running() { docker ps --format '{{.Names}}' 2>/dev/null | sort; }

# Names from $1 (newline list) whose level is $2.
at_level() {
    inventory | WANT=$1 awk -F'\t' -v lvl="$2" '
        BEGIN { n = split(ENVIRON["WANT"], w, "\n"); for (i = 1; i <= n; i++) if (w[i] != "") t[w[i]] = 1 }
        $2 == lvl && ($1 in t) { print $1 }'
}

# "ready" once running and (if it has a healthcheck) healthy.
ready() {
    docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null |
        awk '$1 == "running" && ($2 == "healthy" || $2 == "none") { ok = 1 } END { exit !ok }'
}

cmd_stop() {
    mkdir -p "$BASE"
    # Record first: dockerd may be stopping in parallel with this task. Merge with any
    # state left by a boot run that never finished, so a second shutdown loses nothing.
    { running; cat "$STATE" 2>/dev/null; } | sed '/^$/d' | sort -u > "$STATE.tmp"
    if [ -s "$STATE.tmp" ]; then mv "$STATE.tmp" "$STATE"; else rm -f "$STATE.tmp"; fi
    recorded=$(wc -l 2>/dev/null < "$STATE") || recorded=0
    {
        log "stop: recorded $recorded containers"
        for lvl in $(descending "$MAX_LEVEL" 0); do
            names=$(at_level "$(running)" "$lvl" | tr '\n' ' ')
            [ -n "$names" ] || continue
            log "stop: level $lvl: $names"
            # shellcheck disable=SC2086
            docker stop $names >/dev/null 2>&1 || log "stop: level $lvl: docker stop reported errors (daemon may be stopping)"
        done
        log "stop: done"
    } >> "$BASE/shutdown.log" 2>&1
}

wait_docker() {
    i=0
    until docker info >/dev/null 2>&1; do
        i=$((i + 5)); [ "$i" -ge "$DOCKER_WAIT" ] && return 1; sleep 5
    done
}

# Wait until dockerd's own restart-policy starts stop changing the running set.
wait_settle() {
    prev=$(running); quiet=0; total=0
    while [ "$quiet" -lt "$SETTLE" ] && [ "$total" -lt "$SETTLE_MAX" ]; do
        sleep 5; quiet=$((quiet + 5)); total=$((total + 5))
        cur=$(running)
        [ "$cur" = "$prev" ] || { quiet=0; prev=$cur; }
    done
}

boot_id() { cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo -; }

# The lock directory records "<pid> <boot id>" so a lock left by a crash or an earlier boot is taken over.
take_lock() {
    if ! mkdir "$LOCK" 2>/dev/null; then
        read -r pid boot < "$LOCK/owner" 2>/dev/null || return 1
        if [ "$boot" = "$(boot_id)" ] && kill -0 "$pid" 2>/dev/null; then return 1; fi
        log "run: removing stale lock $LOCK (pid $pid, boot $boot)"
        rm -rf "$LOCK"
        mkdir "$LOCK" 2>/dev/null || return 1
    fi
    echo "$$ $(boot_id)" > "$LOCK/owner"
    trap 'rm -rf "$LOCK"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
}

cmd_run() {
    mkdir -p "$BASE"
    take_lock || { log "run: another run holds $LOCK"; return 1; }
    log "run: waiting for Docker"
    wait_docker || { log "run: Docker not ready after ${DOCKER_WAIT}s, giving up"; return 1; }
    [ "$DRY" = 1 ] || wait_settle
    now=$(running)
    saved=$(cat "$STATE" 2>/dev/null)
    # Names recorded at shutdown that no longer exist (e.g. --rm containers) are dropped by at_level.
    targets=$(printf '%s\n%s\n' "$now" "$saved" | sed '/^$/d' | sort -u)
    log "run: $(printf '%s\n' "$targets" | grep -c .) targets ($(printf '%s\n' "$now" | grep -c .) already running, state file: $([ -n "$saved" ] && echo yes || echo no))"

    # dockerd's restart policy started containers in no particular order; restart those at
    # STARTUP_ORDER_REORDER_FROM and above so they come up after what they depend on.
    for lvl in $(descending "$MAX_LEVEL" "$REORDER_FROM"); do
        names=$(at_level "$now" "$lvl" | tr '\n' ' ')
        [ -n "$names" ] || continue
        log "run: stopping level $lvl for reorder: $names"
        # shellcheck disable=SC2086
        [ "$DRY" = 1 ] || docker stop $names >/dev/null 2>&1 || log "run: stop errors at level $lvl"
    done

    for lvl in $(ascending 0 "$MAX_LEVEL"); do
        names=$(at_level "$targets" "$lvl" | tr '\n' ' ')
        [ -n "$names" ] || continue
        log "run: starting level $lvl: $names"
        [ "$DRY" = 1 ] && continue
        # shellcheck disable=SC2086
        docker start $names >/dev/null 2>&1 || log "run: start errors at level $lvl"
        waited=0
        while :; do
            pending=""
            for n in $names; do ready "$n" || pending="$pending $n"; done
            [ -z "$pending" ] && { log "run: level $lvl ready after ${waited}s"; break; }
            if [ "$waited" -ge "$LEVEL_TIMEOUT" ]; then
                log "run: WARNING level $lvl not ready after ${LEVEL_TIMEOUT}s:$pending; continuing"
                break
            fi
            sleep 5; waited=$((waited + 5))
        done
    done

    [ "$DRY" = 1 ] || rm -f "$STATE"
    log "run: done"
}

case "${1:-}" in
    stop) cmd_stop ;;
    start)
        mkdir -p "$BASE"
        setsid sh "$0" run > "$LOG" 2>&1 < /dev/null &
        ;;
    run) cmd_run ;;
    plan) DRY=1; LOCK=$BASE/lock.plan; cmd_run ;;
    *) echo "usage: $0 stop|start|run|plan" >&2; exit 2 ;;
esac
