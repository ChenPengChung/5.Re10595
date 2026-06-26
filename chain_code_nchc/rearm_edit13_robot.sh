#!/bin/bash
# rearm_edit13_robot.sh — 登入時(或手動)重建 linger + 重新武裝 Edit13 grab timer。
# 動機:robot unit/script/enabled-symlink 在共享 home(停機不刪),但 linger 在 node-local,
#   NCHC reimage 會抹掉 → 機器人不再免登入自啟。每次互動登入 idempotent 重建 → reimage 後
#   首次登入即修復。安全:只碰 edit13-grab.timer;restart/.grab_disarmed 在則尊重(不重新武裝
#   已搶過的);冪等、fail-safe(任何 systemctl 失敗都 || true,不中斷登入)。
ROOT="/home/s8313697/5.Re10595/Edit13_2800ITBLBM"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u 2>/dev/null)}"
loginctl enable-linger >/dev/null 2>&1 || true
if [ ! -f "$ROOT/restart/.grab_disarmed" ]; then
  systemctl --user reset-failed edit13-grab.timer >/dev/null 2>&1 || true
  systemctl --user enable --now edit13-grab.timer >/dev/null 2>&1 || true
fi
exit 0
