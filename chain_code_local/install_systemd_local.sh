#!/bin/bash
# ==============================================================================
# install_systemd_local.sh — 把本地 cfdq keepalive 二次守衛改成 systemd --user 服務,
#                            開機自起 / 登出 / 斷線 (tmux 掉) 後仍自動拉起。
# ------------------------------------------------------------------------------
# 根治 2026-06-19 16:10 事件: watcher + keepalive 同為 tmux/nohup 子程序, session 一掉
# 就雙雙死亡, 監控斷線 2h20m。改由 systemd (PID1 之子) 守護 keepalive + enable-linger →
# 登出/reboot 都活, 不再依賴互動 session。
#
# 架構 (與姊妹專案 edit13-local-keepalive.service 一致, keepalive-only):
#   edit14-local-keepalive.service : Restart=on-failure
#     → keepalive_watchdog.sh 自身 (nohup, 各子程序 8>&- 關 flock fd) 保活:
#         (1) 全域 cfdq daemon (dispatcher)   (2) hill_watcher (收斂/benchmark 圖)
#   不單獨把 cfdq daemon / watcher 變 systemd unit:
#     * cfdq daemon 是「全域單例」, Edit13/Edit14 共用; 兩專案 keepalive 都以 nohup +
#       singleton 鎖保活它, 不搶 systemd 擁有權 → 避免跨專案互打 (見 CLAUDE.md)。
#     * watcher 由 keepalive 60s 輪詢保活即可。
#
# 冪等: 可重複執行。安全: 只裝本專案 keepalive; 不碰別專案 (Edit11/Edit13) 服務/job/daemon。
# 用法:  bash chain_code_local/install_systemd_local.sh
# 首次遷移 (停舊 nohup keepalive+watcher 釋 flock 後再 enable) 見 CLAUDE.md「本地 systemd 開機自起」。
# ==============================================================================
set -euo pipefail
ROOT="/home/chenpengchung/5.Re10595/Edit14_2800GILBM"
SRC="$ROOT/chain_code_local/systemd"
UDIR="$HOME/.config/systemd/user"
UNIT="edit14-local-keepalive.service"

echo "== 1) 安裝 unit 檔 → $UDIR =="
mkdir -p "$UDIR"
cp -f "$SRC/$UNIT" "$UDIR/"

echo "== 2) enable-linger (登出/reboot 後仍由 systemd 拉起) =="
if loginctl enable-linger "$USER" 2>/dev/null; then
  echo "   linger 已啟用 ($(loginctl show-user "$USER" 2>/dev/null | grep -i '^Linger=' || echo '?'))"
else
  echo "   (enable-linger 失敗 — 可能需 root: sudo loginctl enable-linger $USER)"
  echo "   未開 linger 時, 服務僅於有登入 session 期間運作, reboot 後不自起。"
fi

echo "== 3) 接管舊實例 (釋放 flock, 否則新 systemd 實例搶不到鎖 → exit 0 inactive) =="
# 停 systemd 舊副本; 再殺任何「仍持有本專案 watchdog flock fd8」的程序
# (= 手動 keepalive, 或 pre-fix 洩漏 fd8 的舊 watcher)。一律 /proc/PID/cwd 驗本專案歸屬。
systemctl --user stop "$UNIT" 2>/dev/null || true
sleep 1
for pid in $(cat "$ROOT/live/watchdog.pid" "$ROOT/live/watcher.pid" 2>/dev/null); do
  kill -0 "$pid" 2>/dev/null || continue
  [ "$(readlink "/proc/$pid/cwd" 2>/dev/null)" = "$ROOT" ] || continue
  if [ "$(readlink "/proc/$pid/fd/8" 2>/dev/null)" = "$ROOT/live/.watchdog.lock" ]; then
    echo "   釋鎖: 停止持 flock 舊實例 pid=$pid"; kill "$pid" 2>/dev/null || true
  fi
done
sleep 2

echo "== 4) daemon-reload + enable --now =="
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"

echo "== 5) 狀態 =="
printf '   %-30s : enabled=%s active=%s\n' "$UNIT" \
  "$(systemctl --user is-enabled "$UNIT" 2>/dev/null)" "$(systemctl --user is-active "$UNIT" 2>/dev/null)"
echo "   linger : $(loginctl show-user "$USER" 2>/dev/null | grep -i '^Linger=' || echo '?')"
echo
echo "管理指令:"
echo "  systemctl --user status  $UNIT"
echo "  systemctl --user restart $UNIT     # keepalive 死/卡 → 重拉 (它再保活 watcher+daemon)"
echo "  journalctl --user -u $UNIT -n 50 --no-pager"
