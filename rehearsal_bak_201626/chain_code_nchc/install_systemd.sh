#!/bin/bash
# 安裝/啟用 Edit13 dispatcher + hill_watcher 的 systemd user services。
# 根治: 取代會被 Edit7/2.Re1400 搶寫清掉的 cron-watchdog → systemd Restart=on-failure 守護,
# enable-linger 讓其登出/reboot 也活, 且完全不碰 user crontab(無 lost-update race)。
set -e
_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || realpath "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
CHAIN_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
ROOT="$(cd "$CHAIN_DIR/.." && pwd)"
UDIR="$HOME/.config/systemd/user"
mkdir -p "$UDIR"

# 跨專案隔離: 絕不在此 disable/rm 任何 edit6-* unit —— 那是 RUNNING 中的 Edit6_5600DNS
# 專案在共用 NFS ~/.config/systemd/user/ 下的 live daemon。移除會誤殺別專案的 daemon
# (歷史 'watcher 暴增' 根因)。Edit13 只安裝/啟用自己的 edit13-* unit。
NEW_UNITS=(edit13-dispatcher.service edit13-watcher.service edit13-watchdog.service edit13-watchdog.timer)

for u in "${NEW_UNITS[@]}"; do
    cp -f "$CHAIN_DIR/systemd/$u" "$UDIR/"
done

mkdir -p "$ROOT/restart"
printf 'dev\n' > "$ROOT/restart/h200_partition"

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
