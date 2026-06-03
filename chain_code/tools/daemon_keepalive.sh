#!/usr/bin/env bash
# ==============================================================================
# chain_code/tools/daemon_keepalive.sh — Edit7 daemon keep-alive watchdog (dispatcher + watcher)
# ------------------------------------------------------------------------------
# 由 crontab 每 5 分鐘呼叫 (layer 3)。自癒「本專案」兩個 login-node daemon:
#   - watcher (hill_watcher): 死了 → 經 hill_watcher_start.sh 重啟 (chain 在跑就維持, 與 dispatcher 模式無關)。
#   - dispatcher: 啟用 (DISPATCHER_INTENT 在) 但死/hung (heartbeat 過期) → 清殘留 + 重啟,
#     把 net-best 自動切換救回 (不必等下次 job 結束)。
# → watcher 與 dispatcher 皆「不靠運氣」, 死了 5 分內自動回來。
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

# ── 守門: 整條 chain 已被使用者停 → 兩個 daemon 都不重啟 ──
[ -f restart/STOP_CHAIN ] && exit 0

# ════ watcher auto-heal (與 dispatcher 模式無關; chain 在跑就維持 watcher 存活) ════
# 用專案啟動器 hill_watcher_start.sh (自帶 heartbeat dup-guard + 跨專案安全孤兒清理)。
# [跨節點判活] 以 live/watcher.heartbeat mtime 新鮮度判活 (watcher 每 ~POLL_SEC=30s touch),
# 不靠 kill -0 — cron 可能在與 watcher 不同的 login node, kill -0 會誤判死 → 反覆誤殺重啟 (churn)。
if [ -x watcher/hill_watcher_start.sh ]; then
    _whb_stale="${WATCHER_HB_STALE:-180}"
    _wage=999999
    [ -f live/watcher.heartbeat ] && _wage=$(( $(date +%s) - $(stat -c %Y live/watcher.heartbeat 2>/dev/null || echo 0) ))
    if [ "$_wage" -gt "$_whb_stale" ]; then
        _log "WATCHDOG: watcher heartbeat stale ${_wage}s (>$_whb_stale) → 經 hill_watcher_start.sh 重啟 (auto-heal)"
        bash watcher/hill_watcher_start.sh >> "$LOG" 2>&1 || true
        _log "  watcher 重啟 → pid=$(cat live/watcher.pid 2>/dev/null || echo '?')"
    else
        # [code-refresh 自動換新碼] watcher 活著但跑的是舊版 hill_watcher.sh
        # (process 啟動時間早於腳本 mtime) → 殺掉換新碼, 讓修好的 watcher 程式碼自動上線。
        # 只在 keepalive 與 watcher 同 node 時 /proc/PID 可讀(也才殺得到)→ 自洽;
        # cron 與 watcher 本就同 node(watcher 由 keepalive 在該 node 啟動)。
        # 跨專案安全: 殺前驗 /proc/PID/cwd == 本專案 PROJECT_ROOT, 絕不碰別專案 watcher。
        _wp="$(tr -dc 0-9 < live/watcher.pid 2>/dev/null)"
        if [ -n "$_wp" ] && [ -e "/proc/$_wp" ]; then
            _wcwd="$(readlink "/proc/$_wp/cwd" 2>/dev/null)"
            _wstart=$(stat -c %Y "/proc/$_wp" 2>/dev/null || echo 0)
            _smt=$(stat -c %Y watcher/hill_watcher.sh 2>/dev/null || echo 0)
            if [ "$_wcwd" = "$PROJECT_ROOT" ] && [ "$_wstart" -lt "$_smt" ]; then
                _log "WATCHDOG: watcher pid=$_wp 跑舊碼 (start $_wstart < hill_watcher.sh mtime $_smt, cwd=$_wcwd 本專案) → 殺掉換新碼"
                kill "$_wp" 2>/dev/null || true; sleep 2; kill -9 "$_wp" 2>/dev/null || true
                rm -f live/watcher.heartbeat 2>/dev/null || true   # 清心跳, 避免 start 誤判仍活
                bash watcher/hill_watcher_start.sh >> "$LOG" 2>&1 || true
                _log "  watcher 換新碼重啟 → pid=$(cat live/watcher.pid 2>/dev/null || echo '?')"
            fi
        fi
    fi
fi

# ════ dispatcher auto-heal (僅 dispatcher 模式 = INTENT 在) ════
[ -f STOP_DISPATCHER ]           && exit 0   # dispatcher 正被優雅停止 (dispatcher_stop)
[ -f restart/DISPATCHER_INTENT ] || exit 0   # 未啟用 dispatcher 模式 → 到此為止 (watcher 已處理)

# heartbeat 新鮮度判活 (daemon 每 ~POLL_INTERVAL=30s touch)
HB_STALE="${KEEPALIVE_HB_STALE:-300}"        # >此秒數視為 daemon 死/hung (300s=10 漏; 遠大於 ~35s 重啟空窗)
age=999999
[ -f restart/dispatcher.heartbeat ] && age=$(( $(date +%s) - $(stat -c %Y restart/dispatcher.heartbeat 2>/dev/null || echo 0) ))
[ "$age" -le "$HB_STALE" ] && exit 0          # dispatcher 健康 → 無事

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
