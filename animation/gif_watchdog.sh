#!/bin/bash
# =============================================================================
# gif_watchdog.sh — Edit11_Krank5600 全場 GIF 累積器看門狗 (對話獨立 / session-free)
# =============================================================================
# 目的: 讓 1000 幀 GIF 製作「完全不倚賴 Claude 對話」。
#   - gif_render_loop.py driver 以 setsid nohup 脫離 session 跑。
#   - 本 watchdog 也以 setsid nohup 脫離 session 跑, 每 INTERVAL 秒檢查 driver:
#       * driver 存活        → 不動。
#       * driver crash 未完成 → 自動重啟 (driver 冪等, 掃 png_frames/ 已渲幀續抓)。
#       * driver 乾淨完成 (log "ALL DONE") → watchdog 自行退出。
#   → 對話中斷 / session 關閉都不影響 driver 或 watchdog。
#
# 跨專案安全 (CRITICAL):
#   - 只認 cwd==本專案 ROOT 且 comm==python3 且 cmdline 含 gif_render_loop.py 的 driver。
#   - 絕不碰 Edit7 (或任何其他專案) 的 driver。
#   - 絕不使用 pkill -f / killall (會誤殺別專案); 只用經 is_my_driver 驗證過的明確 PID。
#
# 注意: nohup daemon 不耐 login node 重開機 (與本專案 dispatcher/watcher 同限制)。
#       對話中斷無妨; 若整台 login node 重啟, 需手動再跑一次本腳本。
# =============================================================================
set -u

ROOT="/home/s8313697/5.Re10595/Edit11_Krank5600"
cd "$ROOT" || { echo "cannot cd $ROOT"; exit 1; }

DRIVER_PY="animation/gif_render_loop.py"
DRIVER_PID_FILE="animation/gif_render_loop.pid"
DRIVER_LOG="animation/gif_render_loop.log"
WD_PID_FILE="animation/gif_watchdog.pid"
WD_LOG="animation/gif_watchdog.log"
PYTHON="python3"
INTERVAL="${GIF_WD_INTERVAL:-45}"          # 檢查間隔 (s)
HEARTBEAT_EVERY=40                         # 每 N 次迴圈記一次心跳 (~每 30 分)

# driver 重啟時沿用的環境 (與初次啟動一致)
export GIF_TARGET="${GIF_TARGET:-1000}"
export GIF_MAX_WALL="${GIF_MAX_WALL:-700000}"

log(){ echo "[$(date '+%F %T')] $*" >> "$WD_LOG"; }

# 是否為「本專案 driver」: 存活 + cwd==ROOT + comm==python3 + cmdline 含 gif_render_loop.py
is_my_driver(){
  local pid="${1:-}"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  [ "$(readlink /proc/$pid/cwd 2>/dev/null)" = "$ROOT" ] || return 1
  [ "$(cat /proc/$pid/comm 2>/dev/null)" = "$PYTHON" ] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "gif_render_loop.py" || return 1
  return 0
}

# 掃描所有「本專案」driver python pid (升冪); 自動排除 Edit7 等其他 cwd
scan_drivers(){
  local q
  for q in $(pgrep -f "gif_render_loop.py" 2>/dev/null); do
    is_my_driver "$q" && echo "$q"
  done | sort -n
}

driver_done(){ tail -n 8 "$DRIVER_LOG" 2>/dev/null | grep -q "ALL DONE"; }

launch_driver(){
  setsid nohup env GIF_TARGET="$GIF_TARGET" GIF_MAX_WALL="$GIF_MAX_WALL" \
    "$PYTHON" "$DRIVER_PY" >/dev/null 2>&1 &
  sleep 3
  local p
  p="$(scan_drivers | tail -1)"
  if [ -n "$p" ]; then
    echo "$p" > "$DRIVER_PID_FILE"
    log "RELAUNCHED driver PID=$p"
  else
    log "RELAUNCH FAILED: launch 後找不到 python driver"
  fi
}

# 收斂到剛好一個本專案 driver: 採用既有(留最舊), 殺多餘(防雙開 race), 沒有則啟動
ensure_single_driver(){
  local pids keep extra
  pids="$(scan_drivers)"
  if [ -z "$pids" ]; then
    driver_done && return 0          # 已完成就不重啟
    log "no Edit11 driver alive → launching"
    launch_driver
    return 0
  fi
  keep="$(echo "$pids" | head -1)"
  echo "$keep" > "$DRIVER_PID_FILE"
  for extra in $(echo "$pids" | tail -n +2); do
    log "extra Edit11 driver PID=$extra → kill (keep $keep, 防雙開)"
    kill "$extra" 2>/dev/null
  done
}

echo $$ > "$WD_PID_FILE"
log "WATCHDOG START pid=$$ interval=${INTERVAL}s target=$GIF_TARGET max_wall=$GIF_MAX_WALL"
ensure_single_driver                       # 啟動即收斂: 採用現有 driver / 清雙開
log "adopted driver PID=$(cat "$DRIVER_PID_FILE" 2>/dev/null)"

iter=0
while true; do
  if driver_done; then
    log "driver ALL DONE → GIF 製作完成, watchdog 退出。"
    rm -f "$WD_PID_FILE"
    exit 0
  fi
  pid="$(cat "$DRIVER_PID_FILE" 2>/dev/null)"
  if ! is_my_driver "${pid:-}"; then
    log "driver pid=${pid:-none} 不存活 → ensure_single_driver"
    ensure_single_driver
  fi
  iter=$((iter + 1))
  if [ $((iter % HEARTBEAT_EVERY)) -eq 0 ]; then
    nf=$(ls -1 animation/png_frames/frame_*_cont.png 2>/dev/null | grep -vc _RD_cont)
    log "heartbeat: driver=$(cat "$DRIVER_PID_FILE" 2>/dev/null) frames~${nf}/$GIF_TARGET"
  fi
  sleep "$INTERVAL"
done
