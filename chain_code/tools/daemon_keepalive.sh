#!/usr/bin/env bash
# ==============================================================================
# chain_code/tools/daemon_keepalive.sh — Edit7 dispatcher keep-alive watchdog
# ------------------------------------------------------------------------------
# 由 crontab 每 5 分鐘呼叫 (layer 3)。若「本專案」的 dispatcher 應在跑
# (restart/DISPATCHER_INTENT 在) 但已死 / hung (heartbeat 過期), 則清殘留 + 重啟,
# 把 net-best 自動切換的最佳化救回來 (不必等下次 job 結束)。
#
# 跨節點安全: 用「共享 FS 的 restart/dispatcher.heartbeat mtime」判活, 不靠 kill -0
#   (cron 可能跑在與 daemon 不同的 login node, kill -0 會誤判)。
# 跨專案安全: PROJECT_ROOT 由腳本路徑推導, 全程只操作本專案; kill 前驗 cmdline+cwd。
#
# 不重啟的情況: STOP_CHAIN / STOP_DISPATCHER 在 (使用者刻意停), 或 DISPATCHER_INTENT 不在
#   (未啟用 dispatcher 模式 → 由 jobscript legacy 自我續投, 不需 daemon)。
# ==============================================================================
set -u

_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
PROJECT_ROOT="$(cd "$(dirname "$_SELF")/../.." && pwd)" || exit 1   # tools/ → chain_code/ → PROJECT_ROOT
cd "$PROJECT_ROOT" || exit 1

LOG="restart/dispatcher.log"
_log() { echo "[$(date '+%F %T')] [keepalive] $*" >> "$LOG" 2>/dev/null; }

# ── 守門: 不該重啟的情況 ──
[ -f restart/STOP_CHAIN ]        && exit 0   # 整條 chain 已被使用者停
[ -f STOP_DISPATCHER ]           && exit 0   # dispatcher 正被優雅停止 (dispatcher_stop)
[ -f restart/DISPATCHER_INTENT ] || exit 0   # 未啟用 dispatcher 模式 → 不介入

# ── 用 heartbeat 新鮮度判活 (daemon 每 ~POLL_INTERVAL=30s touch) ──
HB_STALE="${KEEPALIVE_HB_STALE:-300}"        # >此秒數視為 daemon 死/hung (300s=10 漏; 遠大於 ~35s 重啟空窗)
age=999999
[ -f restart/dispatcher.heartbeat ] && age=$(( $(date +%s) - $(stat -c %Y restart/dispatcher.heartbeat 2>/dev/null || echo 0) ))
[ "$age" -le "$HB_STALE" ] && exit 0          # 健康 → 無事

# ── dispatcher 死 / hung → 清殘留 + 重啟本專案 dispatcher ──
_log "WATCHDOG: DISPATCHER_INTENT 在但 heartbeat stale ${age}s (>$HB_STALE) → dispatcher 死/hung, 救回中"

# 若同 login node 且 PID 還活著但 hung (heartbeat 不動), 先殺掉本專案的它, 否則 dispatcher_start dup-guard 會擋。
_pid=""; [ -f DISPATCHER_ACTIVE ] && _pid="$(tr -dc 0-9 < DISPATCHER_ACTIVE 2>/dev/null)"
if [ -n "$_pid" ] && kill -0 "$_pid" 2>/dev/null; then
    _cmd="$(tr '\0' ' ' < "/proc/$_pid/cmdline" 2>/dev/null)"
    _cwd="$(readlink "/proc/$_pid/cwd" 2>/dev/null)"
    if printf '%s' "$_cmd" | grep -q 'submit_dispatcher' && [ "$_cwd" = "$PROJECT_ROOT" ]; then
        _log "  hung dispatcher PID=$_pid (本專案, cwd=$_cwd) → kill"
        kill "$_pid" 2>/dev/null || true; sleep 2; kill -9 "$_pid" 2>/dev/null || true
    else
        _log "  DISPATCHER_ACTIVE PID=$_pid 非本專案 dispatcher (cmd='$_cmd' cwd='$_cwd') → 不殺, 僅清殘留檔"
    fi
fi
rm -f DISPATCHER_ACTIVE 2>/dev/null || true   # 清掉殘留 sentinel (本專案的; dispatcher_start 會寫新的)

# dispatcher_start.sh 自帶 dup-guard + 重建 INTENT/heartbeat
bash chain_code/dispatcher_start.sh >> "$LOG" 2>&1
_log "WATCHDOG: dispatcher_start 已呼叫 → DISPATCHER_ACTIVE=$(cat DISPATCHER_ACTIVE 2>/dev/null || echo '?')"
