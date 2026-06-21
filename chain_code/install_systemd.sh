#!/bin/bash
# 安裝/啟用 Edit12_Krank56002 dispatcher + hill_watcher 的 systemd user services。
# 根治: 取代會被別專案(Edit6/Edit7/2.Re1400 等)搶寫清掉的 cron-watchdog → systemd Restart=on-failure 守護,
# enable-linger 讓其登出/reboot 也活, 且完全不碰 user crontab(無 lost-update race)。
set -e
ROOT="/home/s8313697/5.Re10595/Edit12_Krank56002"; UDIR="$HOME/.config/systemd/user"
mkdir -p "$UDIR"
cp -f "$ROOT/chain_code/systemd/edit12-dispatcher.service" "$UDIR/"
cp -f "$ROOT/chain_code/systemd/edit12-watcher.service"    "$UDIR/"
cp -f "$ROOT/chain_code/systemd/edit12-watchdog.service"   "$UDIR/"
cp -f "$ROOT/chain_code/systemd/edit12-watchdog.timer"     "$UDIR/"
loginctl enable-linger "$USER" 2>/dev/null || echo "(enable-linger 失敗, 無 linger 仍可運行於登入期間)"
systemctl --user daemon-reload
systemctl --user enable --now edit12-dispatcher.service edit12-watcher.service
# Route B: 健康守護 timer (語意層健康 + 切換稽核, 超出 systemd 純 process-liveness restart)
systemctl --user enable --now edit12-watchdog.timer
echo "active: $(systemctl --user is-active edit12-dispatcher.service edit12-watcher.service | tr '\n' ' ')"
echo "timer : $(systemctl --user is-active edit12-watchdog.timer)"
echo "管理: systemctl --user {status|restart|stop} edit12-dispatcher.service"
echo "      systemctl --user list-timers edit12-watchdog.timer   # 看下次健康巡檢時間"
