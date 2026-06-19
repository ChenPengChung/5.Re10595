#!/bin/bash
# ==============================================================================
# keepalive_resync.sh — 一次性把本地 keepalive/watcher 收斂回「單一 systemd 守衛 + 單一 watcher」。
#
# 何時用: 監控層出現「多個 keepalive / 重複 watcher 累積」時(例如某 session 反覆啟停留下殘留)。
# ⚠ 請在「全新、乾淨的 shell」執行(先結束會製造 stray 的舊 Claude/互動 session),
#   這樣 bulk-kill 不會誤殺 session 內的行程,且不會邊殺邊有新 stray 冒出來。
#
# 安全: 只動「cwd = 本專案」的 keepalive_watchdog.sh / hill_watcher.sh;
#   絕不碰 cfdq daemon(全域共用,不在本 service cgroup)、絕不碰 Edit14/Edit11 等別專案;
#   不用 pkill -f;一律 /proc/PID/cwd 判歸屬。
# ==============================================================================
set -uo pipefail
E13="/home/chenpengchung/5.Re10595/Edit13_2800ITBLBM"
SVC=edit13-local-keepalive.service
cd "$E13"

is_edit13(){ # $1=pid → 0 if cwd under E13
  local c; c=$(readlink "/proc/$1/cwd" 2>/dev/null) || return 1
  case "$c" in "$E13"|"$E13"/*) return 0;; *) return 1;; esac
}

echo "[resync] 1) 停 systemd service(cgroup 一併清掉它名下重複 watcher;cfdq 不在此 cgroup 不受影響)"
systemctl --user stop "$SVC" 2>/dev/null || true
sleep 3

echo "[resync] 2) 掃掉殘留的 Edit13 keepalive / hill_watcher(cwd 驗證、單一 kill、最多 5 輪)"
for round in 1 2 3 4 5; do
  victims=""
  for pid in $(ps -u "$USER" -o pid= 2>/dev/null); do
    is_edit13 "$pid" || continue
    cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
    case "$cmd" in
      *keepalive_watchdog.sh*|*hill_watcher.sh*) victims="$victims $pid" ;;
    esac
  done
  [ -z "$victims" ] && { echo "  round $round: 全清 ✓"; break; }
  echo "  round $round kill:$victims"
  for p in $victims; do kill "$p" 2>/dev/null || true; done
  sleep 3
done

echo "[resync] 3) 重新啟動單一 systemd 守衛"
systemctl --user start "$SVC"
sleep 6

echo "[resync] 4) 驗證"
kc=0; wc=0
for pid in $(ps -u "$USER" -o pid= 2>/dev/null); do
  is_edit13 "$pid" || continue
  cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
  case "$cmd" in
    *keepalive_watchdog.sh*) kc=$((kc+1)) ;;
    *hill_watcher.sh*)       wc=$((wc+1)) ;;
  esac
done
echo "  keepalive=$kc (期望 1)   hill_watcher=$wc (期望 1~2:含 benchmark 子殼)"
echo "  service is-active=$(systemctl --user is-active "$SVC")  MainPID=$(systemctl --user show -p MainPID --value "$SVC")"
echo "  cfdq job0003=$(cat "$HOME/.cfdq/jobs/0003/status" 2>/dev/null || echo '?') (應 running,全程未受影響)"
echo "[resync] done。若 keepalive>1 或 watcher 持續累積,再跑一次;仍不收斂表示有外部 launcher,回報協助。"
