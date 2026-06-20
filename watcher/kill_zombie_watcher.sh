#!/usr/bin/env bash
# kill_zombie_watcher.sh — 清除「本專案(Edit11)所屬的殭屍 watcher」
# ============================================================================
# 背景:watcher 為跨節點單例(共享 home 上 atomic mkdir 鎖 live/watcher.nodelock +
#   live/watcher.heartbeat)。合法持鎖者只有一隻;若先前 takeover 沒殺乾淨, 別節點可能
#   殘留「無鎖殭屍 watcher」(偶爾蓋寫 heartbeat、與合法者搶 render)。NCHC 登入節點間
#   SSH 有 2FA/push, 無法 scripted 跨節點 kill → 故本腳本走「共享 FS, 免 SSH」兩條路:
#
#   (1) 本機殭屍   → 直接 kill(/proc/cwd 驗證屬本專案 + 明確 PID, 絕不 pkill -f)。
#   (2) 跨節點殭屍 → 靠 hill_watcher.sh 內建的 self-eviction:任何「不再持有 nodelock」的
#                    watcher 會在 ~2 個 poll 週期內自己優雅退出(免 SSH)。本腳本只負責
#                    「把合法 owner 釘穩 + 監測殭屍是否如期自滅」並回報。對「仍跑舊碼、沒有
#                    self-eviction」的歷史殭屍(罕見)→ 回報需該節點重啟/重開才清, 不臆造殺法。
#
# 守門:絕不碰 Edit6/Edit12 等別專案的 watcher(一律 /proc/PID/cwd 驗歸屬);只殺本機、
#   明確 PID、cwd==本專案根 的 hill_watcher.sh;絕不 scancel/動 job/動 checkpoint。
# 用法:bash watcher/kill_zombie_watcher.sh          (互動回報)
#       bash watcher/kill_zombie_watcher.sh --watch  (額外監測 ~90s 確認跨節點殭屍自滅)
# ============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"   # 物理路徑(與 readlink /proc/cwd 同基準, 避免 symlink 漏網)
LIVE_DIR="$PROJECT_DIR/live"
NODELOCK="$LIVE_DIR/watcher.nodelock"
HEARTBEAT="$LIVE_DIR/watcher.heartbeat"
MYHOST="$(hostname)"
WATCH=0; [[ "${1:-}" == "--watch" ]] && WATCH=1

echo "=== Edit11 殭屍 watcher 清除 @ $(date '+%F %T') (本機=$MYHOST) ==="

# ── 1. 合法持鎖者(單一真相)──────────────────────────────────────────────
owner="$(cat "$NODELOCK/owner" 2>/dev/null || true)"
hb="$(cat "$HEARTBEAT" 2>/dev/null || true)"
echo "  nodelock owner(合法 watcher)= ${owner:-（無鎖!）}"
echo "  heartbeat 現值              = ${hb:-（無）}"
if [[ -z "$owner" ]]; then
    echo "  ⚠️ 沒有 nodelock owner — 無法判定誰合法;建議先 bash watcher/hill_watcher_start.sh 起一隻再跑本腳本。"
    exit 2
fi
# owner 格式守門:必須嚴格為「非空主機名:數字pid」(單一冒號)。owner 只在 _claim_lock/_take 寫
# (rm→mkdir→echo);若此刻讀到 NFS 半寫的畸形值(無冒號/多冒號/空主機/非數字 pid)→ fail-safe
# 偏向保留:本輪不殺, 以免誤殺合法持鎖者。用 regex 一次驗證 + 萃取 host/pid(防 host:1:2、:123 漏網)。
if ! [[ "$owner" =~ ^([^:]+):([0-9]+)$ ]]; then
    echo "  ⚠️ owner 檔疑似改寫中或畸形(=$owner)— 本輪略過殺殭屍以免誤殺合法者(稍後重跑)。"
    exit 2
fi
owner_host="${BASH_REMATCH[1]}"
owner_pid="${BASH_REMATCH[2]}"

# ── 2. 本機殭屍掃描 + 直接 kill(/proc/cwd 驗證屬 Edit11)──────────────────
echo "── 本機($MYHOST)Edit11 watcher 實例掃描 ──"
killed_local=0; kept_local=0
for p in $(pgrep -f '[h]ill_watcher\.sh' 2>/dev/null || true); do
    [[ "$p" == "$$" || "$p" == "$PPID" ]] && continue
    cwd="$(readlink "/proc/$p/cwd" 2>/dev/null || true)"
    if [[ "$cwd" != "$PROJECT_DIR" ]]; then
        echo "  ↷ PID=$p cwd=$cwd —— 非本專案, 不碰"
        continue
    fi
    # 屬本專案。是合法者(== owner 且 owner 在本機)還是本機殭屍?
    if [[ "$owner_host" == "$MYHOST" && "$p" == "$owner_pid" ]]; then
        echo "  ✓ PID=$p = 合法持鎖者(保留)"
        kept_local=$((kept_local+1))
    else
        # TOCTOU 防誤殺:owner 是腳本啟動時的單次快照;殺前重讀 live owner。若此 PID 在掃描期間因遠端
        # 死亡而 _take 成為新的合法持鎖者(快照仍是舊的別節點 owner)→ 保留不殺。
        cur_owner="$(cat "$NODELOCK/owner" 2>/dev/null || true)"
        if [[ "$cur_owner" == "$MYHOST:$p" ]]; then
            echo "  ✓ PID=$p 掃描期間已接管成合法持鎖者 → 保留(避免 TOCTOU 誤殺)"
            kept_local=$((kept_local+1))
        else
            echo "  ✗ PID=$p = 本機殭屍(非持鎖者)→ kill $p"
            kill "$p" 2>/dev/null && { echo "    ✅ killed $p"; killed_local=$((killed_local+1)); } \
                || echo "    ⚠️ kill $p 失敗(可能已退出)"
        fi
    fi
done
[[ "$killed_local" == 0 && "$kept_local" == 0 ]] && echo "  (本機無 Edit11 watcher 進程;合法者可能在別節點)"

# ── 3. 跨節點殭屍偵測(heartbeat owner != nodelock owner 即代表別節點有殭屍蓋寫)──
echo "── 跨節點殭屍偵測(監測 heartbeat 是否出現非合法 owner 的 node:pid)──"
SAMPLES=$([[ "$WATCH" == 1 ]] && echo 30 || echo 6)
declare -A seen_other
for i in $(seq 1 "$SAMPLES"); do
    cur="$(cut -d: -f1,2 "$HEARTBEAT" 2>/dev/null || true)"
    if [[ -n "$cur" && "$cur" != "$owner" ]]; then seen_other["$cur"]=1; fi
    [[ "$i" -lt "$SAMPLES" ]] && sleep 3
done
if [[ -z "${seen_other[*]+set}" ]]; then   # set -u 安全:空關聯陣列不可用 ${#a[@]}(會 unbound abort)
    echo "  ✅ 監測期間 heartbeat 恆為合法 owner($owner)— 無跨節點殭屍蓋寫。"
else
    echo "  ⚠️ 偵測到別節點殭屍蓋寫 heartbeat:"
    for z in "${!seen_other[@]}"; do echo "     - $z(非合法 owner)"; done
    echo "  → 處置:hill_watcher.sh 已內建 self-eviction;若該殭屍跑「新碼」, 它會在 ~2 個 poll 週期(~60s)內"
    echo "          偵測到自己不再持鎖而自動退出 → 再跑一次本腳本(或 --watch)確認消失即可, 免 SSH。"
    echo "          若它跑「舊碼(無 self-eviction)」, 共享 FS 殺不到 → 需該登入節點重啟/重開, 或你能直連"
    echo "          該節點時:readlink /proc/<pid>/cwd 確認是 Edit11 後 kill <pid>。"
fi

# ── 4. 收斂回報 ──────────────────────────────────────────────────────────
echo "── 結果 ──"
echo "  本機殺掉殭屍: $killed_local 個 | 本機保留合法者: $kept_local 個"
final_hb="$(cut -d: -f1,2 "$HEARTBEAT" 2>/dev/null || true)"
if [[ -z "${seen_other[*]+set}" && ( "$killed_local" -gt 0 || "$kept_local" -gt 0 || "$final_hb" == "$owner" ) ]]; then
    echo "  ✅ 單一實例:合法 watcher=$owner(heartbeat 現為 $final_hb)。"
    exit 0
fi
echo "  ⏳ 仍偵測到跨節點殭屍 —— 等其 self-eviction(~60s)後再驗, 或依上述舊碼處置。"
exit 0