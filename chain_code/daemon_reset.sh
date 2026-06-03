#!/bin/bash
# 乾淨 reset Edit6 的 dispatcher + hill_watcher 成單一實例 + 確保 watchdog cron。
# 用於「雙重故障」恢復: dispatcher 死 + cron 被 Edit7 的 daemon_keepalive 覆寫清掉時。
# (chain 本身由 jobscript 自我續投 Layer 2 保命, 不依賴本腳本; 本腳本只恢復最佳化用的 daemon。)
# 用法: bash chain_code/daemon_reset.sh
ROOT="/home/s8313697/5.Re10595/Edit6_5600DNS"
cd "$ROOT" || exit 1
DISP="Edit6_5600DNS/chain_code/submit_dispatcher.sh"
WATCH="Edit6_5600DNS/watcher/hill_watcher.sh"

# 1) 暫移除 Edit6 watchdog cron(避免 reset 期間干擾), 不碰 Edit7
crontab -l 2>/dev/null | grep -v 'Edit6_5600DNS/chain_code/dispatcher_watchdog.sh' | crontab - 2>/dev/null

# 2) 停所有 Edit6 dispatcher
touch restart/STOP_DISPATCHER 2>/dev/null; sleep 8
for p in $(pgrep -f submit_dispatcher.sh 2>/dev/null); do
  [ "$p" = "$$" ] && continue
  tr '\0' ' ' </proc/$p/cmdline 2>/dev/null | grep -q "$DISP" && kill -9 "$p" 2>/dev/null
done
# 3) 殺所有 Edit6 hill_watcher
for p in $(pgrep -f hill_watcher.sh 2>/dev/null); do
  [ "$p" = "$$" ] && continue
  tr '\0' ' ' </proc/$p/cmdline 2>/dev/null | grep -q "$WATCH" && kill -9 "$p" 2>/dev/null
done
sleep 3
# 4) 清 sentinel + stale pid, 起單一 dispatcher(會自動裝 cron)+ watcher
rm -f STOP_DISPATCHER restart/STOP_DISPATCHER DISPATCHER_ACTIVE restart/DISPATCHER_ACTIVE live/watcher.pid 2>/dev/null
./run dispatcher start >/dev/null 2>&1; sleep 3
bash watcher/hill_watcher_start.sh >/dev/null 2>&1; sleep 3
# 5) 確保 cron(dispatcher_start 也會做)
crontab -l 2>/dev/null | grep -q dispatcher_watchdog || \
  ( crontab -l 2>/dev/null; echo "*/5 * * * * $ROOT/chain_code/dispatcher_watchdog.sh" ) | crontab - 2>/dev/null
# 6) 報告
sleep 2
nd=0; for p in $(pgrep -f submit_dispatcher.sh 2>/dev/null); do tr '\0' ' ' </proc/$p/cmdline 2>/dev/null | grep -q "$DISP" && [ "$(ps -o ppid= -p $p 2>/dev/null|tr -d ' ')" = 1 ] && nd=$((nd+1)); done
echo "=== daemon_reset done $(date '+%T') ==="
echo "dispatcher daemon(PPID=1)=$nd  pid檔=$(cat restart/dispatcher.pid 2>/dev/null) alive=$(kill -0 $(cat restart/dispatcher.pid 2>/dev/null) 2>/dev/null && echo Y || echo N)"
echo "hill_watcher pid檔=$(cat live/watcher.pid 2>/dev/null) alive=$(kill -0 $(cat live/watcher.pid 2>/dev/null) 2>/dev/null && echo Y || echo N)"
echo "cron Edit6 watchdog: $(crontab -l 2>/dev/null | grep -c 'Edit6_5600DNS.*dispatcher_watchdog') 條"
echo "job: $(squeue -j $(cat restart/chain_jobid 2>/dev/null) -h -o '%i %T %M' 2>/dev/null)"
