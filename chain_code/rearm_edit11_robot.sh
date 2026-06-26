#!/bin/bash
# ============================================================================
# rearm_edit11_robot.sh — 登入時(或手動)重新武裝 Edit11 session-independent 機器人
# ----------------------------------------------------------------------------
# 動機:機器人的 unit / script / enabled-symlink / 狀態檔都在「共享 home(weka)」→ NCHC 停機
#   不會刪。但讓它「免登入自啟」的 linger 旗標在 node-local `/var/lib/systemd/linger/<user>`,
#   若 NCHC reimage login node 會被抹掉 → 機器人就不會在無人登入時自動跑。
# 本腳本在每次互動登入(由 ~/.bashrc 背景呼叫)idempotent 重建 linger + 確保 grab/watchdog
#   timer enabled+started → reimage 後「第一次登入」即自我修復。亦可手動執行。
#
# 安全:只碰 edit11-* unit + 全域 linger(benign,惠及所有 systemd --user 服務);
#   grab.timer 已成功搶到並自動解除(restart/.grab_disarmed 存在)→ 尊重之、不硬起;
#   不直接起 dispatcher/watcher(交 watchdog 以 heartbeat-based liveness 處理,跨節點不重複);
#   冪等、快、全程 fail-safe(任何 systemctl 失敗都 || true,絕不中斷登入)。
# ============================================================================
ROOT="/home/s8313697/5.Re10595/Edit11_Krank5600"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u 2>/dev/null)}"

# 1. 重建 linger(reimage 後關鍵;已啟用則 no-op)
loginctl enable-linger >/dev/null 2>&1 || true

# 2. watchdog timer:永遠確保 enabled+started(它會自我修復 dispatcher/watcher)
systemctl --user reset-failed edit11-watchdog.timer >/dev/null 2>&1 || true
systemctl --user enable --now edit11-watchdog.timer >/dev/null 2>&1 || true

# 3. grab timer:除非已成功搶到並自動解除,否則確保 enabled+started
if [ ! -f "$ROOT/restart/.grab_disarmed" ]; then
  systemctl --user reset-failed edit11-grab.timer >/dev/null 2>&1 || true
  systemctl --user enable --now edit11-grab.timer >/dev/null 2>&1 || true
fi
exit 0
