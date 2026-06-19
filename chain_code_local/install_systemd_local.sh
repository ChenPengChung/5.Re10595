#!/bin/bash
# ==============================================================================
# install_systemd_local.sh — 把本地 cfdq keepalive 二次守衛裝成 systemd user service,
#   讓它「開機自起、撐過登入節點 reboot」(根治: nohup 背景程序 reboot 後不會自己回來)。
#
# keepalive_watchdog.sh 本身已守 cfdq daemon + hill_watcher (+flow_render),
#   所以只需一個 service;boot→(linger)user systemd 起→起 keepalive→keepalive 拉回其餘。
#
# 冪等: 可重複執行。會「乾淨接管」現有手動 nohup keepalive(flock 單例)再交給 systemd。
# 安全: 只用 /proc/PID/cwd 判本專案歸屬才動;絕不 pkill -f;不碰別專案 (Edit14/Edit11)。
# 用法: bash chain_code_local/install_systemd_local.sh
# 解除: systemctl --user disable --now edit13-local-keepalive.service
# ==============================================================================
set -euo pipefail
ROOT="/home/chenpengchung/5.Re10595/Edit13_2800ITBLBM"
UDIR="$HOME/.config/systemd/user"
SVC="edit13-local-keepalive.service"

mkdir -p "$UDIR"
cp -f "$ROOT/chain_code_local/systemd/$SVC" "$UDIR/"
echo "[install] 已安裝 unit → $UDIR/$SVC"

# 1) 乾淨接管: 停掉現有「手動 nohup」keepalive(僅限本專案, /proc/PID/cwd + cmdline 雙重驗證),
#    釋放 flock,讓 systemd 那一份能取得鎖成為唯一守衛。
pf="$ROOT/live/watchdog.pid"
if [ -f "$pf" ]; then
  p="$(cat "$pf" 2>/dev/null || true)"
  if [ -n "${p:-}" ] && kill -0 "$p" 2>/dev/null \
     && [ "$(readlink "/proc/$p/cwd" 2>/dev/null)" = "$ROOT" ] \
     && tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q 'keepalive_watchdog.sh'; then
    # 判斷該 pid 是否「就是」本 systemd service 的 MainPID(唯一可靠依據;
    # 不能用 parent comm — setsid/nohup 程序會被 init(PID 1, 名亦為 systemd)收養而誤判)。
    svc_main="$(systemctl --user show -p MainPID --value "$SVC" 2>/dev/null || echo 0)"
    if [ -n "${svc_main:-}" ] && [ "$svc_main" != "0" ] && [ "$p" = "$svc_main" ]; then
      echo "[install] 現有 keepalive(pid=$p)即 systemd service 本身,跳過接管"
    else
      echo "[install] 停掉非 systemd 管的 keepalive pid=$p 以交棒 (釋放 flock)"
      kill "$p" 2>/dev/null || true
      for _ in 1 2 3 4 5 6 7 8; do kill -0 "$p" 2>/dev/null || break; sleep 1; done
    fi
  fi
fi

# 2) linger: 讓 user systemd 於「開機」(非僅登入期間)就拉起 service → 真正撐過 reboot
if loginctl enable-linger "$USER" 2>/dev/null; then
  echo "[install] enable-linger OK — reboot 後會自動拉起,不需登入"
else
  echo "[install] ⚠ enable-linger 需要 root: 請另外執行  sudo loginctl enable-linger $USER"
  echo "          (未開 linger 時 service 仍會在你登入時自起,但 reboot 後要等登入才觸發)"
fi

# 3) 啟用 + 立即啟動
systemctl --user daemon-reload
systemctl --user enable --now "$SVC"

# 4) 驗證
sleep 4
echo "----------------------------------------------------------------"
echo "[install] is-active : $(systemctl --user is-active "$SVC" 2>&1)"
echo "[install] is-enabled: $(systemctl --user is-enabled "$SVC" 2>&1)"
echo "[install] linger    : $(loginctl show-user "$USER" -p Linger --value 2>/dev/null)"
echo "[install] watchdog.pid → $(cat "$ROOT/live/watchdog.pid" 2>/dev/null) (應為 systemd 管的新 pid)"
echo "[install] watcher.pid  → $(cat "$ROOT/live/watcher.pid" 2>/dev/null)"
echo "管理: systemctl --user {status|restart|stop|disable} $SVC"
