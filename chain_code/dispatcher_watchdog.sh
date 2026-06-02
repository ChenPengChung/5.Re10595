#!/bin/bash
# =============================================================================
# dispatcher_watchdog.sh — 讓 Edit6_5600DNS 的 dispatcher 不靠運氣地活著。
#
# 設計給 login-node crontab 每 ~5 分鐘跑一次。若 dispatcher 程序已死(crash/被殺/
# node-reboot)但使用者沒有要求停鏈(無 STOP_CHAIN),就「清掉殘留 sentinel + 重啟
# 本專案自己的 dispatcher」。這樣即使 job 硬當機(沒跑到 exit handler 的自我續投),
# 重新活過來的 dispatcher 也會偵測到 job 結束並續投。
#
# 安全: 只操作「當前專案(Edit6_5600DNS)」自己的 dispatcher 與 sentinel; 絕不碰
# 別專案(遵守跨專案 Job 隔離)。read-mostly: 唯一寫入是重啟自己的 daemon。
# 安裝:  crontab -e  ->  */5 * * * * /home/s8313697/5.Re10595/Edit6_5600DNS/chain_code/dispatcher_watchdog.sh
# 停用:  從 crontab 移除該行 (或 ./run dispatcher stop 後留 STOP_CHAIN 讓 watchdog 不重啟)
# =============================================================================
ROOT="/home/s8313697/5.Re10595/Edit6_5600DNS"
cd "$ROOT" || exit 1
LOG="restart/dispatcher_watchdog.log"

# 使用者明確要求停鏈 -> 不重啟(尊重 STOP_CHAIN)
[ -f restart/STOP_CHAIN ] && exit 0

p=$(cat restart/dispatcher.pid 2>/dev/null | tr -d '[:space:]')
if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
    exit 0   # dispatcher 活著, 無事
fi

# dispatcher 已死 -> 清殘留 sentinel(避免下次啟動/jobscript 誤判)+ 重啟本專案自己的 daemon
{
    echo "[$(date '+%F %T')] watchdog: dispatcher 已死 (pid='${p:-none}') -> 清 stale sentinel + 重啟"
    rm -f DISPATCHER_ACTIVE restart/DISPATCHER_ACTIVE STOP_DISPATCHER restart/STOP_DISPATCHER 2>/dev/null
    ./run dispatcher start
    np=$(cat restart/dispatcher.pid 2>/dev/null | tr -d '[:space:]')
    if [ -n "$np" ] && kill -0 "$np" 2>/dev/null; then
        echo "[$(date '+%F %T')] watchdog: 重啟成功 新 PID=$np"
    else
        echo "[$(date '+%F %T')] watchdog: 重啟失敗 (下次 cron 再試); jobscript 自我續投仍會保鏈"
    fi
} >> "$LOG" 2>&1
