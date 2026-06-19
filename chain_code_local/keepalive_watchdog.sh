#!/bin/bash
# ==============================================================================
# keepalive_watchdog.sh — 本地生產「二次守衛」(secondary guard) for dispatcher
# ------------------------------------------------------------------------------
# 全程守護三個常駐, 死了→自動重啟 (避免誤殺/意外 kill 導致生產中斷):
#   (1) cfdq daemon (dispatcher) — 放置/續鏈 job 0003 到 V100
#   (2) hill_watcher.sh (watcher) — 產 live/monitor_latest.png 等圖
#   (3) flow_render_loop.sh — 產 live/flowfield_latest.png (paraview 流場圖)
# dispatcher 死亡判定 (與 cfdq 權威一致): daemon.lock/alive age > INTERVAL*3+30=90s,
#   連續 2 輪才重啟 (debounce 防瞬時誤判)。
# 跨專案安全 (CLAUDE.md): /proc/PID/cwd 判歸屬;絕不 pkill -f;cfdq daemon 是全域單例。
# 自我單例: flock (程序死即釋鎖, 無 stale-pid 競態)。
# 用法: nohup bash chain_code_local/keepalive_watchdog.sh >/dev/null 2>&1 &
# ==============================================================================
set -uo pipefail
PROJ=/home/chenpengchung/5.Re10595/Edit13_2800ITBLBM
cd "$PROJ"
LOG="$PROJ/live/keepalive_watchdog.log"
ALIVE="$HOME/.cfdq/daemon.lock/alive"
CFDQ=/home/chenpengchung/bin/cfdq
INTERVAL=60
log(){ echo "[$(date '+%F %T')] $*" >> "$LOG"; }

# 自我單例 (flock): 同時只允許一個本專案 watchdog。
# ⚠ fd 8 必須在所有被 spawn 的子程序中關閉 (各 nohup ... 8>&- &),否則子程序(watcher 等)
#   會繼承並持有此鎖 → 本 watchdog 死後鎖仍被子程序卡住,systemd 接管實例搶不到鎖而空轉退出。
exec 8>"$PROJ/live/.watchdog.lock"
flock -n 8 || { log "另一 watchdog 持鎖, 本實例退出"; exit 0; }
echo $$ > "$PROJ/live/watchdog.pid"
log "===== watchdog 啟動 pid=$$ (守 dispatcher + watcher + flow_render) ====="

ensure_proc(){ # $1=pidfile $2=human $3=launch-cmd
  local pf="$1" name="$2" cmd="$3" p alive=0
  p=$(cat "$pf" 2>/dev/null || true)
  if [ -n "${p:-}" ] && kill -0 "$p" 2>/dev/null && [ "$(readlink "/proc/$p/cwd" 2>/dev/null)" = "$PROJ" ]; then alive=1; fi
  if [ "$alive" -eq 0 ]; then
    log "ALARM: $name 死亡 (pid=${p:-none}) → 重啟"
    eval "$cmd"
    sleep 3
    log "已重啟 $name (new pid=$(cat "$pf" 2>/dev/null))"
  fi
}

stale=0
while true; do
  # ---- (1) cfdq daemon (dispatcher) ----
  if [ -e "$ALIVE" ]; then age=$(( $(date +%s) - $(stat -c %Y "$ALIVE") )); else age=99999; fi
  if [ "$age" -gt 90 ]; then
    stale=$((stale+1)); log "WARN: cfdq daemon age=${age}s (>90s) streak=$stale"
    if [ "$stale" -ge 2 ]; then
      log "ALARM: dispatcher 死亡 → 重啟 cfdq daemon"
      nohup "$CFDQ" daemon >> "$HOME/.cfdq/daemon.log" 2>&1 8>&- &
      log "已重啟 cfdq daemon (new pid=$!)"; stale=0; sleep 10
    fi
  else [ "$stale" -ne 0 ] && log "INFO: cfdq daemon 恢復 (age=${age}s)"; stale=0; fi

  # ---- (2) hill_watcher ----
  ensure_proc "$PROJ/live/watcher.pid" "hill_watcher" \
    "nohup bash '$PROJ/watcher_nchc/hill_watcher.sh' >> '$PROJ/live/hill_watcher_console.log' 2>&1 8>&- &"

  # ---- (3) flow_render_loop ----
  ensure_proc "$PROJ/live/flow_render.pid" "flow_render_loop" \
    "nohup bash '$PROJ/chain_code_local/flow_render_loop.sh' >/dev/null 2>&1 8>&- &"

  # ---- 心跳 (每 ~10 分) ----
  now=$(date +%s)
  if [ $(( now % 600 )) -lt "$INTERVAL" ]; then
    log "heartbeat: daemon_age=${age}s job0003=$(cat "$HOME/.cfdq/jobs/0003/status" 2>/dev/null||echo '?') watcher=$(cat "$PROJ/live/watcher.pid" 2>/dev/null) flow_render=$(cat "$PROJ/live/flow_render.pid" 2>/dev/null)"
  fi
  sleep "$INTERVAL"
done
