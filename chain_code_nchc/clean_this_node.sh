#!/usr/bin/env bash
# clean_this_node.sh — 清掉「本登入節點」上屬於本專案的 runaway watcher / dispatcher。
#
# 背景: ~/.config/systemd/user 在共享 home → systemd enable = 全 5 個登入節點都啟用 →
#       舊碼 watcher 在 lgn02–05 各自 spin、忽略單例鎖。新碼的鎖只能讓「新啟動」者退讓,
#       管不到已在跑的舊實例 → 必須逐台登入把它們 kill 掉。
#
# 安全保證:
#   - 只動本專案: 一律以 /proc/PID/cwd == 本專案根目錄 過濾,絕不 pkill -f / 比對 cmdline 字串,
#     絕不碰 Edit7 / Edit8 / 2.Re1400 等別專案同名 daemon。
#   - 只 `stop` 不 `disable`: stop 是 per-node runtime 狀態(只影響本節點);disable 會刪掉
#     共享 home 的 enable symlink → 全節點生效(連 lgn01 合法 owner 都會被關)→ 嚴禁。
#   - 保護活躍 owner: 若本節點正是目前 watcher 鎖的擁有者(心跳 host),直接跳過不殺。
#
# 用法: 逐一登入各登入節點(過 1/2/3 選單)後,執行:
#         bash /home/chenpengchung/5.Re10595/Edit13_2800ITBLBM/chain_code_nchc/clean_this_node.sh
#       重複登入(gateway 會 load-balance 到不同節點)直到背景巡檢回報收斂、或下方
#       「已清節點」清單涵蓋 lgn02 lgn03 lgn04 lgn05。lgn01(owner)會被自動跳過。
set -u

SELF="${BASH_SOURCE[0]:-$0}"
PROJ="$(cd "$(dirname "$SELF")/.." && pwd)"
cd "$PROJ" || { echo "FATAL: 無法進入專案目錄 $PROJ"; exit 1; }
LIVE="$PROJ/live"
LOG="$LIVE/.nodes_cleaned.log"
H="$(hostname)"
OWNER="$(cut -d: -f1 "$LIVE/watcher.heartbeat" 2>/dev/null || true)"
DOWNER="$(cut -d: -f1 "$PROJ/restart/dispatcher.heartbeat" 2>/dev/null || true)"
mkdir -p "$LIVE"

_coverage() { awk '{print $1}' "$LOG" 2>/dev/null | sort -u | tr '\n' ' '; }

# ── 保護: 本節點若是目前活躍 watcher owner → 不動它 ─────────────────────────────
if [ -n "$OWNER" ] && [ "$H" = "$OWNER" ]; then
    echo "⏭  $H 是目前活躍 watcher owner(唯一合法實例)— 保留不動。"
    echo "—— 已清節點: $(_coverage)"
    echo "—— owner: watcher=$OWNER  dispatcher=$DOWNER"
    exit 0
fi

# ── 1) 停掉本節點的三個 unit(per-node runtime;不碰共享 enable symlink)──────────
systemctl --user stop edit13-watcher.service edit13-dispatcher.service edit13-watchdog.timer 2>/dev/null || true

# ── 2) 殺殘留程序 — 僅限 cwd == 本專案者(跨專案安全)────────────────────────────
killed=0
for p in $(pgrep -f 'hill_watcher\.sh' 2>/dev/null); do
    [ "$(readlink /proc/$p/cwd 2>/dev/null)" = "$PROJ" ] && kill -9 "$p" 2>/dev/null && killed=$((killed+1))
done
for p in $(pgrep -f 'submit_dispatcher\.sh' 2>/dev/null); do
    [ "$(readlink /proc/$p/cwd 2>/dev/null)" = "$PROJ" ] && kill -9 "$p" 2>/dev/null && killed=$((killed+1))
done

# ── 3) 記錄 + 回報 ─────────────────────────────────────────────────────────────
echo "$H $(date '+%F %T') killed=$killed" >> "$LOG"
echo "✅ $H 已清乾淨(殺掉 $killed 隻本專案 runaway 程序)"
echo "—— 已清節點: $(_coverage)"
echo "—— owner: watcher=$OWNER  dispatcher=$DOWNER  (應為 25a-lgn01)"
