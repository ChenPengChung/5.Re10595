#!/bin/bash
# 安裝/啟用 Edit13 dispatcher + hill_watcher 的 systemd user services。
# 根治: 取代會被 Edit7/2.Re1400 搶寫清掉的 cron-watchdog → systemd Restart=on-failure 守護,
# enable-linger 讓其登出/reboot 也活, 且完全不碰 user crontab(無 lost-update race)。
set -e
ROOT="/home/chenpengchung/5.Re10595/Edit13_2800ITBLBM"
CHAIN_DIR="$ROOT/chain_code_nchc"
UDIR="$HOME/.config/systemd/user"
mkdir -p "$UDIR"

OLD_UNITS=(edit6-dispatcher.service edit6-watcher.service edit6-watchdog.service edit6-watchdog.timer)
NEW_UNITS=(edit13-dispatcher.service edit13-watcher.service edit13-watchdog.service edit13-watchdog.timer)

for u in "${OLD_UNITS[@]}"; do
    systemctl --user disable --now "$u" 2>/dev/null || true
    rm -f "$UDIR/$u"
done

for u in "${NEW_UNITS[@]}"; do
    cp -f "$CHAIN_DIR/systemd/$u" "$UDIR/"
done

mkdir -p "$ROOT/restart"
printf '32gpus\n' > "$ROOT/restart/h200_partition"

loginctl enable-linger "$USER" 2>/dev/null || echo "(enable-linger 失敗, 無 linger 仍可運行於登入期間)"
systemctl --user daemon-reload
systemctl --user enable --now edit13-dispatcher.service edit13-watcher.service
# Route B: 健康守護 timer (語意層健康 + 切換稽核, 超出 systemd 純 process-liveness restart)
systemctl --user enable --now edit13-watchdog.timer
echo "active: $(systemctl --user is-active edit13-dispatcher.service edit13-watcher.service | tr '\n' ' ')"
echo "timer : $(systemctl --user is-active edit13-watchdog.timer)"
echo "lock  : restart/h200_partition=$(cat "$ROOT/restart/h200_partition")"
echo "管理: systemctl --user {status|restart|stop} edit13-dispatcher.service"
echo "      systemctl --user list-timers edit13-watchdog.timer   # 看下次健康巡檢時間"
