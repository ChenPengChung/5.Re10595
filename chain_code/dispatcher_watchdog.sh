#!/bin/bash
# =============================================================================
# dispatcher_watchdog.sh — 讓 Edit12_Krank56002 的兩個 daemon 不靠運氣地活著:
#   (A) dispatcher (submit_dispatcher.sh) — 自動續投/切換的大腦
#   (B) hill_watcher (watcher/hill_watcher.sh) — 產生 live/monitor_latest.png 收斂圖
#
# 設計給 login-node crontab 每 ~5 分鐘跑一次。任一 daemon 程序死掉(crash/被殺/
# node-reboot)且使用者沒有要求停鏈(無 STOP_CHAIN),就清掉殘留 + 重啟「本專案自己的」
# 該 daemon。兩者各自獨立檢查(不會因一個活著就略過另一個)。
#
# 安全: 只操作當前專案(Edit12_Krank56002)自己的 daemon 與 sentinel; 絕不碰別專案。
# 安裝:  crontab -e  ->  */5 * * * * /home/s8313697/5.Re10595/Edit12_Krank56002/chain_code/dispatcher_watchdog.sh
#        (亦會由 `./run dispatcher start` 自動確保此 crontab 存在)
# =============================================================================
ROOT="/home/s8313697/5.Re10595/Edit12_Krank56002"
cd "$ROOT" || exit 1
LOG="restart/dispatcher_watchdog.log"

# 使用者明確要求停鏈 -> 兩個都不重啟(尊重 STOP_CHAIN)
[ -f restart/STOP_CHAIN ] && exit 0

# ── (A) dispatcher ──
dp=$(cat restart/dispatcher.pid 2>/dev/null | tr -d '[:space:]')
if [ -z "$dp" ] || ! kill -0 "$dp" 2>/dev/null; then
    {
        echo "[$(date '+%F %T')] watchdog: dispatcher 已死 (pid='${dp:-none}') -> 清殘留 sentinel + 重啟"
        rm -f DISPATCHER_ACTIVE restart/DISPATCHER_ACTIVE STOP_DISPATCHER restart/STOP_DISPATCHER 2>/dev/null
        ./run dispatcher start
        np=$(cat restart/dispatcher.pid 2>/dev/null | tr -d '[:space:]')
        if [ -n "$np" ] && kill -0 "$np" 2>/dev/null; then
            echo "[$(date '+%F %T')] watchdog: dispatcher 重啟成功 新 PID=$np"
        else
            echo "[$(date '+%F %T')] watchdog: dispatcher 重啟失敗 (下次再試); jobscript 自我續投仍保鏈"
        fi
    } >> "$LOG" 2>&1
fi

# ── (B) hill_watcher (出圖到 live/) ──
wp=$(cat live/watcher.pid 2>/dev/null | tr -d '[:space:]')
if [ -z "$wp" ] || ! kill -0 "$wp" 2>/dev/null; then
    {
        echo "[$(date '+%F %T')] watchdog: hill_watcher 已死 (pid='${wp:-none}') -> 重啟 (hill_watcher_start.sh 自帶 dup-guard)"
        bash watcher/hill_watcher_start.sh
        nwp=$(cat live/watcher.pid 2>/dev/null | tr -d '[:space:]')
        if [ -n "$nwp" ] && kill -0 "$nwp" 2>/dev/null; then
            echo "[$(date '+%F %T')] watchdog: hill_watcher 重啟成功 新 PID=$nwp"
        else
            echo "[$(date '+%F %T')] watchdog: hill_watcher 重啟失敗 (下次再試)"
        fi
    } >> "$LOG" 2>&1
fi

exit 0
