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
LOCK=$BASE/run.lock
LABEL=${STARTUP_ORDER_LABEL:-startup-order}
MAX_LEVEL=${STARTUP_ORDER_MAX_LEVEL:-3}
DEFAULT_LEVEL=${STARTUP_ORDER_DEFAULT_LEVEL:-2}
REORDER_FROM=${STARTUP_ORDER_REORDER_FROM:-2}
LEVEL_TIMEOUT=${STARTUP_ORDER_LEVEL_TIMEOUT:-420}
DOCKER_WAIT=${STARTUP_ORDER_DOCKER_WAIT:-900}
SETTLE=${STARTUP_ORDER_SETTLE:-20}
SETTLE_MAX=${STARTUP_ORDER_SETTLE_MAX:-180}
STOP_WAIT=${STARTUP_ORDER_STOP_WAIT:-30}
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
whole STOP_WAIT "$STOP_WAIT"
[ "$DEFAULT_LEVEL" -le "$MAX_LEVEL" ] ||
    invalid STARTUP_ORDER_DEFAULT_LEVEL "$DEFAULT_LEVEL is above STARTUP_ORDER_MAX_LEVEL ($MAX_LEVEL)"

case "${1:-}" in
    stop | start | run | plan)
        command -v flock >/dev/null 2>&1 || { echo "startup-order: requires flock (util-linux) on PATH" >&2; exit 2; } ;;
esac

# Levels from $1 down to $2, or up from $1 to $2; nothing when the range is empty.
descending() { awk -v hi="$1" -v lo="$2" 'BEGIN { for (i = hi; i >= lo; i--) print i }'; }
ascending() { awk -v lo="$1" -v hi="$2" 'BEGIN { for (i = lo; i <= hi; i++) print i }'; }

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S%z') $*"; }

# name<TAB>level for every container, running or not; fails when docker does.
inventory() {
    containers=$(docker ps -a --format "{{.Names}}	{{.Label \"$LABEL\"}}" 2>/dev/null) || return 1
    printf '%s\n' "$containers" | awk -F'\t' -v d="$DEFAULT_LEVEL" -v max="$MAX_LEVEL" '
        $1 == "" || $2 == "skip" { next }
        { l = $2; if (l !~ /^(0|[1-9][0-9]*)$/ || l + 0 > max + 0) l = d; print $1 "\t" l + 0 }'
}

running() { docker ps --format '{{.Names}}' 2>/dev/null | sort; }

# Sets $saved to the recorded names, empty when there is no state file; fails when one exists but cannot be read.
read_state() {
    saved=""
    [ -e "$STATE" ] || [ -L "$STATE" ] || return 0
    saved=$(cat "$STATE" 2>/dev/null)
}

# Atomically replaces the state file with the names in $1; on failure the old state file is left as it was.
save_state() {
    { printf '%s\n' "$1" > "$STATE.tmp" && mv -f "$STATE.tmp" "$STATE"; } 2>/dev/null
}

# Names from $1 (newline list) whose level is $2, per the inventory in $3 (default: a fresh one).
at_level() {
    { if [ $# -ge 3 ]; then printf '%s\n' "$3"; else inventory; fi; } | WANT=$1 awk -F'\t' -v lvl="$2" '
        BEGIN { n = split(ENVIRON["WANT"], w, "\n"); for (i = 1; i <= n; i++) if (w[i] != "") t[w[i]] = 1 }
        $2 == lvl && ($1 in t) { print $1 }'
}

# "ready" once running and (if it has a healthcheck) healthy.
ready() {
    docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null |
        awk '$1 == "running" && ($2 == "healthy" || $2 == "none") { ok = 1 } END { exit !ok }'
}

# Shutdown wins over a boot run: ask the run holding the lock to give up, then wait for it to let go.
stop_lock() {
    exec 9>> "$LOCK" || return 1
    flock -n 9 && return 0
    pid=$(cat "$LOCK.pid" 2>/dev/null) && [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null &&
        log "stop: asked run $pid to give up"
    # busybox flock has no -w.
    waited=0
    until flock -n 9; do
        [ "$waited" -ge "$STOP_WAIT" ] && return 1
        sleep 1; waited=$((waited + 1))
    done
}

cmd_stop() {
    mkdir -p "$BASE" || return 1
    {
        stop_lock 2>/dev/null || { log "stop: $LOCK still held after ${STOP_WAIT}s; stopping nothing"; return 1; }
        echo $$ > "$LOCK.pid" 2>/dev/null
        # Record first: dockerd may be stopping in parallel with this task. Merge with any state left by a boot run
        # that never finished, so a second shutdown loses nothing. Stop nothing unless the record is saved.
        now=$(docker ps --format '{{.Names}}' 2>/dev/null) ||
            { log "stop: cannot list running containers; stopping nothing"; return 1; }
        read_state || { log "stop: cannot read $STATE; stopping nothing"; return 1; }
        keep=$(printf '%s\n%s\n' "$now" "$saved" | sed '/^$/d' | sort -u)
        if [ -n "$keep" ]; then
            save_state "$keep" || { log "stop: cannot save state to $STATE; stopping nothing"; return 1; }
        fi
        log "stop: recorded $(printf '%s\n' "$keep" | grep -c .) containers"
        for lvl in $(descending "$MAX_LEVEL" 0); do
            # Only what was recorded: anything started since is left for dockerd to stop, and to restart at boot.
            names=$(at_level "$now" "$lvl" | tr '\n' ' ')
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

# flock(1) holds the lock on an open descriptor, so the kernel releases it when this run exits, crashes or the
# Host reboots; the lock file itself is never removed. Child commands (docker, sleep) inherit fd 9, so a killed
# run's lock lasts until its last child exits. The holder's PID goes in $LOCK.pid so `stop` can end a boot run.
take_lock() {
    exec 9>> "$LOCK" && flock -n 9 || return 1
    echo $$ > "$LOCK.pid" 2>/dev/null
    trap 'exit 130' INT
    trap 'exit 143' TERM
}

# Leave the state file for the next run and fail; for errors that may leave targets stopped.
abort() { log "run: $*; keeping $STATE for the next run"; exit 1; }

cmd_run() {
    mkdir -p "$BASE"
    take_lock 2>/dev/null || { log "run: another run holds $LOCK"; return 1; }
    log "run: waiting for Docker"
    wait_docker || { log "run: Docker not ready after ${DOCKER_WAIT}s, giving up"; return 1; }
    [ "$DRY" = 1 ] || wait_settle
    now=$(docker ps --format '{{.Names}}' 2>/dev/null) || abort "cannot list running containers"
    now=$(printf '%s\n' "$now" | sort)
    inv=$(inventory) || abort "cannot list containers"
    read_state || abort "cannot read $STATE; stopping nothing"
    # Names recorded at shutdown that no longer exist (e.g. --rm containers) are dropped by at_level.
    targets=$(printf '%s\n%s\n' "$now" "$saved" | sed '/^$/d' | sort -u)
    log "run: $(printf '%s\n' "$targets" | grep -c .) targets ($(printf '%s\n' "$now" | grep -c .) already running, state file: $([ -n "$saved" ] && echo yes || echo no))"

    # Persist every target before stopping anything, so a run interrupted mid-reorder leaves the next run a full
    # recovery set. The state is removed only once every target is running.
    if [ "$DRY" != 1 ] && [ -n "$targets" ]; then
        save_state "$targets" ||
            { log "run: cannot save recovery state to $STATE; stopping nothing"; return 1; }
    fi

    # dockerd's restart policy started containers in no particular order; restart those at
    # STARTUP_ORDER_REORDER_FROM and above so they come up after what they depend on.
    for lvl in $(descending "$MAX_LEVEL" "$REORDER_FROM"); do
        names=$(at_level "$now" "$lvl" "$inv" | tr '\n' ' ')
        [ -n "$names" ] || continue
        log "run: stopping level $lvl for reorder: $names"
        [ "$DRY" = 1 ] && continue
        # shellcheck disable=SC2086
        docker stop $names >/dev/null 2>&1 || {
            docker info >/dev/null 2>&1 || abort "Docker became unavailable"
            log "run: stop errors at level $lvl"
        }
    done

    for lvl in $(ascending 0 "$MAX_LEVEL"); do
        names=$(at_level "$targets" "$lvl" "$inv" | tr '\n' ' ')
        [ -n "$names" ] || continue
        log "run: starting level $lvl: $names"
        [ "$DRY" = 1 ] && continue
        # shellcheck disable=SC2086
        docker start $names >/dev/null 2>&1 || {
            docker info >/dev/null 2>&1 || abort "Docker became unavailable"
            log "run: start errors at level $lvl"
        }
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

    [ "$DRY" = 1 ] && { log "run: done"; return 0; }
    # Unhealthy is a warning (above); a target that still exists but is not running (e.g. restarting) is a failure.
    now=$(docker ps --filter status=running --format '{{.Names}}' 2>/dev/null) || abort "cannot confirm what is running"
    inv=$(inventory) || abort "cannot confirm what is running"
    missing=$(for lvl in $(ascending 0 "$MAX_LEVEL"); do at_level "$targets" "$lvl" "$inv"; done |
        NOW=$now awk 'BEGIN { n = split(ENVIRON["NOW"], r, "\n"); for (i = 1; i <= n; i++) up[r[i]] = 1 }
            !($0 in up) { printf "%s ", $0 }')
    [ -z "$missing" ] || abort "not started: $missing"
    rm -f "$STATE"
    log "run: done"
}

case "${1:-}" in
    stop) cmd_stop ;;
    start)
        mkdir -p "$BASE"
        setsid sh "$0" run > "$LOG" 2>&1 < /dev/null &
        ;;
    run) cmd_run ;;
    plan) DRY=1; LOCK=$BASE/plan.lock; cmd_run ;;
    *) echo "usage: $0 stop|start|run|plan" >&2; exit 2 ;;
esac
