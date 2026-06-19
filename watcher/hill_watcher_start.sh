#!/usr/bin/env bash
# hill_watcher_start.sh — daemon launcher for Periodic Hill Re5600 watcher

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LIVE_DIR="$PROJECT_DIR/live"
LOG_FILE="$LIVE_DIR/watcher.log"
PID_FILE="$LIVE_DIR/watcher.pid"

WATCHER="$SCRIPT_DIR/hill_watcher.sh"

[[ -x "$WATCHER" ]] || chmod +x "$WATCHER" 2>/dev/null || true
[[ -f "$WATCHER" ]] || { echo "ERROR: watcher script not found: $WATCHER" >&2; exit 1; }

mkdir -p "$LIVE_DIR"

# [硬化] systemd 是 watcher 的唯一真相來源 — service 若 active 就不再手動啟動重複實例。
# (歷史故障: 恢復時重複跑本腳本 + 下面的 PID_FILE-only dup-guard 被 stale watcher.pid 打敗
#  → 累積多隻 watcher 同時 racing 狂出圖、灌爆 login node。)
# [修正 2026-06-19] 服務名必須是「本專案」edit11-watcher.service — 原本誤抄 Edit6 的
# edit6-watcher.service(跨專案污染): Edit6 服務一直 active → Edit11 launcher 永遠誤判
# 「已被 systemd 管理」+ 把 Edit6 的 MainPID 寫進 Edit11 watcher.pid → 害 hill_watcher.sh
# 的 node-local dup-guard(kill -0 那個 Edit6 PID 成功)拒絕啟動。Edit11 目前無 systemd
# 服務 → 此檢查 inert,正確落到下方 cwd-based dup-guard + setsid 啟動。
if systemctl --user is-active --quiet edit11-watcher.service 2>/dev/null; then
    SYS_PID=$(systemctl --user show -p MainPID --value edit11-watcher.service 2>/dev/null)
    echo "watcher 已由 systemd 管理 (edit11-watcher.service, MainPID=$SYS_PID), 不重複啟動"
    [[ -n "${SYS_PID:-}" ]] && echo "$SYS_PID" > "$PID_FILE" 2>/dev/null || true
    exit 0
fi

# [硬化] cwd-based dup-guard (跨專案安全): 掃所有 hill_watcher.sh, 用 /proc/PID/cwd 判本專案歸屬。
# 涵蓋絕對+相對路徑啟動 — 純 PID_FILE 比對會被 stale pid 打敗而漏判 → 啟出重複實例。
# 別專案 (Edit7/Edit8/...) 的 watcher cwd 不在本專案 → 絕不誤判、絕不誤殺。
for _p in $(pgrep -f 'hill_watcher\.sh' 2>/dev/null); do
    [[ "$_p" = "$$" ]] && continue
    _last=$(tr '\0' '\n' </proc/"$_p"/cmdline 2>/dev/null | tail -1)
    case "$_last" in *hill_watcher.sh) : ;; *) continue ;; esac
    _cwd=$(readlink /proc/"$_p"/cwd 2>/dev/null)
    case "$_cwd" in
        "$PROJECT_DIR"|"$PROJECT_DIR"/*)
            echo "本專案 watcher 已在執行 (PID=$_p, cwd 判定), 不重複啟動"
            echo "$_p" > "$PID_FILE" 2>/dev/null || true
            exit 1 ;;
    esac
done

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
