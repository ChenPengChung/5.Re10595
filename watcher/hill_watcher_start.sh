#!/usr/bin/env bash
# hill_watcher_start.sh — daemon launcher for Periodic Hill Re5600 watcher

set -euo pipefail

# If launched from run.sh/build_and_submit.sh, do not let watcher keep
# .run.lock busy after the submitter exits.
{ exec 200>&-; } 2>/dev/null || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LIVE_DIR="$PROJECT_DIR/live"
LOG_FILE="$LIVE_DIR/watcher.log"
PID_FILE="$LIVE_DIR/watcher.pid"

WATCHER="$SCRIPT_DIR/hill_watcher.sh"

[[ -x "$WATCHER" ]] || chmod +x "$WATCHER" 2>/dev/null || true
[[ -f "$WATCHER" ]] || { echo "ERROR: watcher script not found: $WATCHER" >&2; exit 1; }

mkdir -p "$LIVE_DIR"

if [[ -f "$PID_FILE" ]]; then
    old_pid=$(cat "$PID_FILE" 2>/dev/null || true)
    if [[ -n "${old_pid:-}" ]] && kill -0 "$old_pid" 2>/dev/null; then
        echo "watcher already running:  PID $old_pid"
        echo "    log : $LOG_FILE"
        echo "    stop: kill $old_pid"
        exit 1
    fi
    echo "stale PID file ($old_pid not running), cleaning up"
    rm -f "$PID_FILE"
fi

setsid bash "$WATCHER" </dev/null >>"$LOG_FILE" 2>&1 &
disown $! 2>/dev/null || true

for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if [[ -s "$PID_FILE" ]]; then break; fi
    sleep 0.2
done

if [[ ! -s "$PID_FILE" ]]; then
    echo "ERROR: watcher did not start within 3s; check $LOG_FILE"
    tail -n 20 "$LOG_FILE" 2>/dev/null || true
    exit 1
fi

WATCHER_PID=$(cat "$PID_FILE")
if ! kill -0 "$WATCHER_PID" 2>/dev/null; then
    echo "ERROR: watcher PID $WATCHER_PID is not alive; check $LOG_FILE"
    tail -n 20 "$LOG_FILE" 2>/dev/null || true
    exit 1
fi

cat <<EOF
=== Periodic Hill Re5600 Watcher started ===
  PID         : $WATCHER_PID
  PID file    : $PID_FILE
  Log file    : $LOG_FILE
  Output dir  : $LIVE_DIR
  Live plots  :
    $LIVE_DIR/monitor_latest.png
    $LIVE_DIR/monitor_latest.pdf

  Stop  : kill \$(cat "$PID_FILE")
  Tail  : tail -f "$LOG_FILE"
EOF

ps -o pid,ppid,sid,stat,cmd -p "$WATCHER_PID" 2>/dev/null || true
