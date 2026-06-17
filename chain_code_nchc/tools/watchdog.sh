#!/usr/bin/env bash
# ==============================================================================
# chain_code/tools/watchdog.sh — solver hang detector
# ==============================================================================
# Runs in background alongside mpirun. Monitors output file freshness.
# If no watched file is updated for TIMEOUT seconds (after GRACE period),
# kills the solver process tree and writes a sentinel file so the jobscript
# can blacklist the node and resubmit on a healthy one.
#
# Usage (from jobscript):
#   bash watchdog.sh <solver_pid> <sentinel_file> [timeout_sec] [grace_sec] &
#
# Environment:
#   WATCHDOG_FILES  space-separated watch files (default: gilbm_metrics_full.dat
#                   Ustar_Force_record.dat)
#
# Exit: 0 = solver exited on its own (no hang)
#       1 = hang detected, solver killed
# ==============================================================================

set -u

SOLVER_PID="$1"
SENTINEL="$2"
TIMEOUT_SEC="${3:-600}"
GRACE_SEC="${4:-180}"
CHECK_SEC=30

: "${WATCHDOG_FILES:=gilbm_metrics_full.dat Ustar_Force_record.dat}"

_log() { printf '[%s] [watchdog] %s\n' "$(date '+%F %T')" "$*"; }
_solver_alive() { kill -0 "$SOLVER_PID" 2>/dev/null; }

rm -f "$SENTINEL"
_log "started: pid=$$ solver=$SOLVER_PID timeout=${TIMEOUT_SEC}s grace=${GRACE_SEC}s"
_log "  files: $WATCHDOG_FILES"

_latest_mtime() {
    local best=0
    for f in $WATCHDOG_FILES; do
        [ -f "$f" ] || continue
        local m
        m=$(stat -c %Y "$f" 2>/dev/null || echo 0)
        [ "$m" -gt "$best" ] && best=$m
    done
    echo "$best"
}

_collect_descendants() {
    local pid="$1" kids
    kids=$(pgrep -P "$pid" 2>/dev/null || true)
    for k in $kids; do
        echo "$k"
        _collect_descendants "$k"
    done
}

# ── Phase 1: grace period (cold start / initialization) ──
END_GRACE=$(( $(date +%s) + GRACE_SEC ))
while [ "$(date +%s)" -lt "$END_GRACE" ]; do
    _solver_alive || { _log "solver exited during grace period"; exit 0; }
    sleep "$CHECK_SEC"
done
_log "grace period ended, active monitoring begins"

# ── Phase 2: active monitoring ──
LAST_MT=$(_latest_mtime)
LAST_CHANGE=$(date +%s)

while true; do
    _solver_alive || { _log "solver exited normally"; exit 0; }

    MT=$(_latest_mtime)
    if [ "$MT" -gt "$LAST_MT" ]; then
        LAST_MT=$MT
        LAST_CHANGE=$(date +%s)
    fi

    NOW=$(date +%s)
    STALE=$(( NOW - LAST_CHANGE ))

    if [ "$STALE" -ge "$TIMEOUT_SEC" ]; then
        NODE=$(hostname 2>/dev/null || echo unknown)
        _log "HANG DETECTED on $NODE: no output update for ${STALE}s (threshold=${TIMEOUT_SEC}s)"

        # Write sentinel (line 1 = node name, used by jobscript)
        {
            printf '%s\n' "$NODE"
            printf 'detected=%s\nstale_sec=%d\nlast_mtime=%d\n' \
                   "$(date '+%F %T')" "$STALE" "$LAST_MT"
        } > "$SENTINEL"

        # Collect full process tree BEFORE killing (avoid orphan PIDs)
        ALL_PIDS="$SOLVER_PID"
        DESC=$(_collect_descendants "$SOLVER_PID")
        [ -n "$DESC" ] && ALL_PIDS="$ALL_PIDS $DESC"

        _log "SIGTERM -> PIDs: $ALL_PIDS"
        kill -TERM $ALL_PIDS 2>/dev/null || true

        # Wait up to 30s for graceful exit
        for _ in 1 2 3 4 5 6; do
            sleep 5
            _solver_alive || { _log "solver exited after SIGTERM"; exit 1; }
        done

        # Escalate to SIGKILL
        _log "solver still alive after 30s, SIGKILL -> PIDs: $ALL_PIDS"
        kill -9 $ALL_PIDS 2>/dev/null || true
        sleep 2
        _log "SIGKILL sent, watchdog exiting"
        exit 1
    fi

    sleep "$CHECK_SEC"
done
