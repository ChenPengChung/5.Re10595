#!/bin/bash
# 乾淨 reset Edit13 的 dispatcher + hill_watcher 成「單一 systemd 管理實例」。
# 現行守護: systemd user services (edit13-dispatcher / edit13-watcher,
#           Restart=on-failure + enable-linger) — 已完全脫離 crontab race。
# 本腳本用於恢復: 殺光所有殘留/重複的 Edit13 daemon (含「相對路徑」啟動的孤兒),
#               再交回 systemd 單一管理。
# 跨專案安全: 一律用 /proc/PID/cwd 判專案歸屬, 絕不碰別專案 (Edit7/Edit8/...) 的 daemon;
#            不靠 cmdline 路徑字串 (相對路徑啟動會漏判)。
# (chain 本身由 jobscript Layer 2 自我續投保命, 不依賴本腳本; 本腳本只恢復最佳化用的 daemon。)
# 用法: bash chain_code_nchc/daemon_reset.sh
_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || realpath "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
CHAIN_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
ROOT="$(cd "$CHAIN_DIR/.." && pwd)"
cd "$ROOT" || exit 1
DISPATCHER_SERVICE="edit13-dispatcher.service"
WATCHER_SERVICE="edit13-watcher.service"

# 1) 停 systemd 兩個 service (若有), 避免 reset 期間 Restart= 又把它拉起
systemctl --user stop "$DISPATCHER_SERVICE" "$WATCHER_SERVICE" 2>/dev/null
sleep 2

# 2) 殺光所有「本專案 cwd」的 dispatcher + watcher 殘留 (絕對+相對路徑都涵蓋; 別專案不碰)
kill_by_cwd() {   # $1 = 腳本基名 (submit_dispatcher.sh / hill_watcher.sh)
  for p in $(pgrep -f "$1" 2>/dev/null); do
    [ "$p" = "$$" ] && continue
    last=$(tr '\0' '\n' </proc/"$p"/cmdline 2>/dev/null | tail -1)
    case "$last" in *"$1") : ;; *) continue ;; esac          # 只認真正 daemon (末參=腳本)
    cwd=$(readlink /proc/"$p"/cwd 2>/dev/null)
    case "$cwd" in "$ROOT"|"$ROOT"/*) kill -9 "$p" 2>/dev/null ;; esac   # 僅本專案 cwd
  done
}
for r in 1 2 3; do kill_by_cwd submit_dispatcher.sh; kill_by_cwd hill_watcher.sh; sleep 2; done

# 3) 清殘留 sentinel + stale pid 檔
rm -f STOP_DISPATCHER restart/STOP_DISPATCHER DISPATCHER_ACTIVE restart/DISPATCHER_ACTIVE live/watcher.pid 2>/dev/null

# 4) 交回 systemd 單一管理 (Restart=on-failure 自動守護, 完全不碰 crontab)
systemctl --user start "$DISPATCHER_SERVICE" "$WATCHER_SERVICE" 2>/dev/null
sleep 4

# 5) 報告 (用 cwd 判定本專案實例數, 應各 = 1)
cnt_by_cwd() { c=0; for p in $(pgrep -f "$1" 2>/dev/null); do
    last=$(tr '\0' '\n' </proc/"$p"/cmdline 2>/dev/null | tail -1)
    case "$last" in *"$1") cwd=$(readlink /proc/"$p"/cwd 2>/dev/null)
        case "$cwd" in "$ROOT"|"$ROOT"/*) c=$((c+1)) ;; esac ;; esac
  done; echo "$c"; }
echo "=== daemon_reset done $(date '+%T') ==="
echo "dispatcher service=$(systemctl --user is-active "$DISPATCHER_SERVICE") MainPID=$(systemctl --user show -p MainPID --value "$DISPATCHER_SERVICE")  本專案實例(cwd)=$(cnt_by_cwd submit_dispatcher.sh)"
echo "watcher    service=$(systemctl --user is-active "$WATCHER_SERVICE") MainPID=$(systemctl --user show -p MainPID --value "$WATCHER_SERVICE")  本專案實例(cwd)=$(cnt_by_cwd hill_watcher.sh)"
echo "live/monitor_latest.png: $(( ($(date +%s)-$(stat -c %Y live/monitor_latest.png 2>/dev/null))/60 ))分前"
echo "job: $(squeue -j "$(cat restart/chain_jobid 2>/dev/null)" -h -o '%i %T %M' 2>/dev/null)"
