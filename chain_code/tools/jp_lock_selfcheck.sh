#!/bin/bash
# jp_lock_selfcheck.sh — 靜態檢驗「鎖定組合 jp=32 | partition=16gpus」是否正確生效 (心跳腳本)。
#
# 用途: 目前處於「鎖定組合狀態 (partition && jps)」, 依使用者要求只做『靜態檢驗』(不投遞、不 SELFTEST)。
#   驗證: (A) 鎖定哨兵 LOCK_JP_PARTITION + h200_partition=16gpus + variables.h jp=32;
#         (B) 候選機制限定 jp={32} 且 pin 分區 16gpus 在 PARTITION_CANDIDATES (才能被探測進可投清單);
#         (C) 超上限處理: 先 sbatch --test-only 試過、再依 cap 跳過並警告 (不連試都沒試);
#             嚴格鎖: pin 不可投時「跳過本輪+警告、維持鎖定、不落回別分區」(非亂跳/非強投);
#         (D) live 可行性: 鎖定目標 16gpus 此刻 cap(=32)>=jp(=32) 且 state=up → 可投; 否則 WARN(執行期會跳過+警告)。
#
# READ-ONLY: 只讀檔 / 查 sacctmgr+sinfo (無 sbatch、無投遞、無取消、不改任何 source/checkpoint/job)。
#   「附加」一行心跳 → live/jp_lock_heartbeat.log; 寫狀態 → live/jp_lock_status; drift → live/jp_lock_DRIFT.alert。
#
# 用法: bash chain_code/tools/jp_lock_selfcheck.sh
# 退出碼: 0=PASS(含預期內 cap/state SKIP); 1=偵測到漂移(配置與鎖定組合不符)。

_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
CHAIN_DIR="$(cd "$(dirname "$_SELF")/.." && pwd)"
ROOT="$(cd "$CHAIN_DIR/.." && pwd)"
cd "$ROOT" || { echo "[FATAL] cannot cd to project root"; exit 1; }

DISP="$CHAIN_DIR/submit_dispatcher.sh"
LIB="$CHAIN_DIR/tools/partition_lib.sh"
HB="live/jp_lock_heartbeat.log"
STATUS_FILE="live/jp_lock_status"
ALERT_FILE="live/jp_lock_DRIFT.alert"
mkdir -p live

EXPECT_JP=32                        # 鎖定的 jp (GPU 數)
EXPECT_PIN=16gpus                   # 鎖定的 partition (h200_partition pin)
EXPECT_JPC="32"                     # JP_CANDIDATES 預期 (暫時鎖定 32; 自由切換集 {8gpus,16gpus,32gpus}@32jp; auto-controller 只跑 32)
CANDS=(8gpus 16gpus 32gpus)         # 全 H200 候選 (僅供 live 矩陣對照; 嚴格鎖下只採用 pin)

drift=0; warn=0
# 僅在 TTY 上色 (cron/redirect 時不輸出 ANSI)
if [ -t 1 ]; then C_G=$'\033[32m'; C_R=$'\033[31m'; C_Y=$'\033[33m'; C_0=$'\033[0m'; else C_G=; C_R=; C_Y=; C_0=; fi
pass(){ printf "  [%sPASS%s] %s\n" "$C_G" "$C_0" "$1"; }
fail(){ printf "  [%sFAIL%s] %s\n" "$C_R" "$C_0" "$1"; drift=1; }
note(){ printf "  [%sWARN%s] %s\n" "$C_Y" "$C_0" "$1"; warn=1; }
hdr (){ printf "\n== %s ==\n" "$1"; }

echo "================ jp-lock 靜態自檢 (鎖定組合 jp=$EXPECT_JP | $EXPECT_PIN) $(date '+%F %T') ================"

# ── A. 鎖定狀態 (partition && jps) ──
hdr "A. 鎖定哨兵 (預期: LOCK_JP_PARTITION 凍 jp + 鎖 partition; h200_partition=$EXPECT_PIN)"
if [ -f restart/LOCK_JP_PARTITION ]; then pass "restart/LOCK_JP_PARTITION 存在 → jp 凍結 + partition 釘 pin (組合鎖定生效)"
else fail "restart/LOCK_JP_PARTITION 不存在 → 組合未鎖定 (jp 會自動切 / partition 自由跳轉)"; fi
_pin="$(tr -d '[:space:]' < restart/h200_partition 2>/dev/null)"
if [ "$_pin" = "$EXPECT_PIN" ]; then pass "restart/h200_partition pin = $_pin (= 鎖定 partition)"
else fail "restart/h200_partition pin = '${_pin:-<空/不存在>}' ≠ 預期 $EXPECT_PIN"; fi
_jp="$(grep -E '^#define[[:space:]]+jp[[:space:]]+[0-9]+' variables.h 2>/dev/null | grep -oE '[0-9]+' | head -1)"
if [ "$_jp" = "$EXPECT_JP" ]; then pass "variables.h jp = $_jp (= 鎖定 jp)"
else fail "variables.h jp = ${_jp:-?} ≠ 預期 $EXPECT_JP"; fi
[ -f restart/STOP_JPSWITCH ] && note "另有 restart/STOP_JPSWITCH (LOCK_JP_PARTITION 已含凍 jp → 冗餘, 可 rm 清掉避免混淆)"

# ── B. 候選機制限定 (jp={16}; pin 分區須為候選, 才能被探測進可投清單) ──
hdr "B. 候選機制 (預期: JP_CANDIDATES='$EXPECT_JPC'; PARTITION_CANDIDATES 含 pin H200:$EXPECT_PIN)"
_jpc="$(grep -E 'JP_CANDIDATES:-' "$DISP" 2>/dev/null | grep -oE ':-[0-9 ]+' | head -1 | sed 's/^:-//' | xargs)"
if [ "$_jpc" = "$EXPECT_JPC" ]; then pass "JP_CANDIDATES 預設 = '$_jpc' (auto-controller 只跑 jp=$EXPECT_JP)"
else fail "JP_CANDIDATES 預設 = '${_jpc:-?}' ≠ 預期 '$EXPECT_JPC'"; fi
if grep -E '^PARTITION_CANDIDATES_RAW=' "$DISP" 2>/dev/null | grep -q "H200:$EXPECT_PIN"; then
  pass "PARTITION_CANDIDATES 含 pin H200:$EXPECT_PIN (pick_cluster 才能探測 pin 進 _T)"
else fail "PARTITION_CANDIDATES 缺 pin H200:$EXPECT_PIN → 鎖定的 partition 永不會進可投清單!"; fi

# ── C. 超上限處理 + 嚴格鎖路徑 (試了才跳過 + 給警告; 不亂跳別分區) ──
hdr "C. 超上限/嚴格鎖 (預期: 先 --test-only 再依 cap 跳過並警告; pin 不可投→跳過本輪不落回別分區)"
if grep -q '先「實際試一次」' "$DISP" && grep -qE '帳號GPU上限.*跳過.*永久PENDING|已試 --test-only.*帳號GPU上限' "$DISP"; then
  pass "jp_partition_eta: 先 sbatch --test-only 後依 cap 跳過(附警告) — 不是連試都沒試"
else note "jp_partition_eta try-then-skip 標記未完全比對到 (人工確認 submit_dispatcher.sh:745-759)"; fi
if grep -qE '略過: jp=.*每帳號 GPU 上限.*MaxGRESPerAccount' "$DISP"; then
  pass "pick_cluster: 超 cap 跳過時有警告 log (MaxGRESPerAccount)"
else note "pick_cluster 超 cap 警告字串未比對到 (人工確認 submit_dispatcher.sh:296-300)"; fi
if grep -qE '\[LOCK_JP_PARTITION\]\[strict\].*跳過本輪.*不落回別分區' "$DISP"; then
  pass "pick_cluster 嚴格鎖: pin 不可投 → 已試 --test-only 後跳過本輪+警告, 維持鎖定 (不落回別分區/不強投)"
else fail "pick_cluster 嚴格鎖路徑缺失 → pin 不可投時可能落回別分區 (違反「限定 $EXPECT_PIN」)"; fi
if grep -qE '網格不整除或 slab<7 .*物理不可跑.*跳過' "$DISP"; then
  pass "grid/slab 物理不可跑才「不必試」直接跳 (合理; 非 cap 情形)"
else note "grid/slab 前置過濾字串未比對到"; fi

# ── D. live 可行性 (★ = 鎖定目標 16gpus 是否此刻可投; 嚴格鎖下唯一採用此分區) ──
hdr "D. live 可行性 (cap=sacctmgr, state=sinfo; 嚴格鎖 → 唯一採用 pin=$EXPECT_PIN, 其餘僅對照)"
[ -f "$LIB" ] && . "$LIB"
NY="$(awk '/^#define[[:space:]]+NY[[:space:]]/{print $3; exit}' variables.h 2>/dev/null)"; NM1=$(( ${NY:-1} - 1 ))
state_of(){ sinfo -h -p "$1" -o '%a' 2>/dev/null | head -1 | tr -d '[:space:]'; }
jp="$EXPECT_JP"
printf "   %-8s %-6s %-6s %-5s %s\n" "PART" "cap" "stat" "grid" "→ 判定 (jp=$jp)"
pin_feasible=0
for p in "${CANDS[@]}"; do
  cap="$(partition_gpu_cap_per_account "$p" 2>/dev/null)"; cap="${cap:-?}"
  st="$(state_of "$p")"; st="${st:-?}"
  grid="ok"; { [ "$NM1" -gt 0 ] && { [ $((NM1 % jp)) -ne 0 ] || [ $((NM1 / jp)) -lt 7 ] || [ $((jp % 8)) -ne 0 ]; }; } && grid="NG"
  if   [ "$grid" = "NG" ]; then verdict="SKIP(grid:不必試)"
  elif [ "$cap" != "?" ] && [ "$jp" -gt "$cap" ]; then verdict="SKIP+警告(cap $cap<$jp)"
  elif [ "$st" != "up" ]; then verdict="SKIP(state=$st)"
  else verdict="可投(FEASIBLE)"; [ "$p" = "$EXPECT_PIN" ] && pin_feasible=1; fi
  mark=" "; [ "$p" = "$EXPECT_PIN" ] && mark="★"
  used=""; [ "$p" != "$EXPECT_PIN" ] && used="  (嚴格鎖下不採用)"
  printf " %s %-8s %-6s %-6s %-5s %s%s\n" "$mark" "$p" "$cap" "$st" "$grid" "$verdict" "$used"
done
if [ "$pin_feasible" -eq 1 ]; then
  pass "鎖定目標 $EXPECT_PIN 此刻可投 (cap>=jp=$jp, state=up) → 嚴格鎖會投此分區"
else
  note "鎖定目標 $EXPECT_PIN 此刻不可投 → 執行期嚴格鎖會『跳過本輪+警告』並等下輪 (非漂移; 連續無容量 ~4h 才 STOP_NOCAPACITY)"
fi

# ── 結論 + 心跳 + 狀態檔 ──
hdr "結論"
if   [ "$drift" -ne 0 ]; then status="DRIFT"; echo "  ✗ 偵測到配置漂移 (見上方 FAIL) — 鎖定組合未正確生效!"
elif [ "$warn"  -ne 0 ]; then status="OK(warn)"; echo "  ⚠ 鎖定組合配置正確, 但有需留意項 (見 WARN; 多為 live 容量暫態)。"
else status="OK"; echo "  ✓ 鎖定組合 (jp=$EXPECT_JP | partition=$EXPECT_PIN) + 嚴格鎖候選機制 靜態檢驗全數通過。"; fi
_hbline="$(date '+%F %T')  status=$status  jp=$EXPECT_JP  pin=$EXPECT_PIN  LOCK=$([ -f restart/LOCK_JP_PARTITION ] && echo y || echo n)  pin_feasible=$pin_feasible  drift=$drift warn=$warn"
echo "$_hbline" >> "$HB"
printf '%s\n' "$status" > "$STATUS_FILE"
if [ "$drift" -ne 0 ]; then printf '%s\n' "$_hbline" > "$ALERT_FILE"; else rm -f "$ALERT_FILE"; fi
echo "  心跳已記錄 → $HB ; 狀態 → $STATUS_FILE ($status)"
[ "$drift" -eq 0 ]
