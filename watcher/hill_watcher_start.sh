#!/usr/bin/env bash
# hill_watcher_start.sh — daemon launcher for the Periodic Hill watcher

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LIVE_DIR="$PROJECT_DIR/live"
LOG_FILE="$LIVE_DIR/watcher.log"
PID_FILE="$LIVE_DIR/watcher.pid"
HEARTBEAT="$LIVE_DIR/watcher.heartbeat"
HB_STALE="${WATCHER_HB_STALE:-180}"   # 心跳超過此秒數才視為 watcher 真死 (watcher 每 ~30s touch)

WATCHER="$SCRIPT_DIR/hill_watcher.sh"

[[ -x "$WATCHER" ]] || chmod +x "$WATCHER" 2>/dev/null || true
[[ -f "$WATCHER" ]] || { echo "ERROR: watcher script not found: $WATCHER" >&2; exit 1; }

mkdir -p "$LIVE_DIR"

# ── [跨節點判活] 先看 heartbeat 新鮮度 (走共享 FS, 跨 login node 可靠); 不靠 kill -0。
#    心跳新鮮 = 已有 watcher 活著(可能在別 login node) → 不重複啟動, 根除 churn。
if [[ -f "$HEARTBEAT" ]]; then
    hb_age=$(( $(date +%s) - $(stat -c %Y "$HEARTBEAT" 2>/dev/null || echo 0) ))
    if (( hb_age <= HB_STALE )); then
        echo "watcher alive (heartbeat age ${hb_age}s <= ${HB_STALE}s); not starting a duplicate"
        exit 0
    fi
    echo "watcher heartbeat stale (${hb_age}s > ${HB_STALE}s) -> treating as dead, (re)starting"
fi

# ── [跨專案安全清理] heartbeat 過期 = 真死 → 清掉「本專案、本機」殘留孤兒 watcher,
#    避免同機堆積多個實例。判定本專案的唯一依據: cmdline 含本專案 hill_watcher.sh 的
#    **絕對路徑** ($WATCHER); 別專案 (Edit6/Edit8/…) 絕對路徑不同 → case 不命中 → 絕不誤殺。
#    再以 cwd==$PROJECT_DIR 二次確認。僅清本機可見者 (跨 node 者由其自身 heartbeat 失效後各自處理)。
if command -v pgrep >/dev/null 2>&1; then
    for _pid in $(pgrep -f "hill_watcher\.sh" 2>/dev/null || true); do
        [[ "$_pid" == "$$" || "$_pid" == "${PPID:-0}" ]] && continue
        _cmd=$(tr '\0' ' ' < "/proc/$_pid/cmdline" 2>/dev/null || true)
        case "$_cmd" in
            *"$WATCHER"*)
                _cwd=$(readlink "/proc/$_pid/cwd" 2>/dev/null || true)
                if [[ "$_cwd" == "$PROJECT_DIR" || -z "$_cwd" ]]; then
                    echo "cleaning this-project orphan watcher pid=$_pid"
                    kill "$_pid" 2>/dev/null || true
                fi
                ;;
            *) : ;;   # 別專案 watcher → 絕不動
        esac
    done
fi

if [[ -f "$PID_FILE" ]]; then
    old_pid=$(cat "$PID_FILE" 2>/dev/null || true)
    [[ -n "${old_pid:-}" ]] && echo "removing stale PID file (was $old_pid)"
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
=== Periodic Hill Watcher started ===
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
