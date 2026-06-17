#!/bin/bash
# ============================================================================
# verify_seed_integrity.sh — Edit11 warm-start 種子 checkpoint 整性閘 (純唯讀)
# ----------------------------------------------------------------------------
# 驗 restart/checkpoint/step_00000001 是否為有效的 jp=N 種子場 (interp 產物)。
# 期望值全部從 variables.h 推導 (NX/NY/NZ/jp) → 隨網格變更自動正確、不寫死。
# 用途: chain jobscript 在「一條龍」生成種子場後、啟 solver 之前的硬閘門;
#       Claude 也可獨立呼叫做生成後即時整性檢驗。
# 通過 → exit 0 並印摘要; 任一不過 → exit 1 並印 FAIL 原因。
# 純唯讀: 只 read/stat/find/awk, 不寫/不改/不投/不取消任何東西。
# 用法: bash chain_code/tools/verify_seed_integrity.sh [STEP_DIR]
#       (預設 STEP_DIR = restart/checkpoint/step_00000001)
# ============================================================================
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
cd "$ROOT" || { echo "[seed-gate] FATAL: 無法 cd 到 project root"; exit 1; }

CKPT="${1:-restart/checkpoint/step_00000001}"
M="$CKPT/metadata.dat"
fail() { echo "[seed-gate] FAIL: $*"; exit 1; }

[ -s "$M" ] || fail "metadata.dat 不存在或為空: $M"

# ── 從 variables.h 推導期望值 ──
_def() { awk -v k="$1" '$1=="#define" && $2==k {v=$3; gsub(/[()"]/,"",v); print v; exit}' variables.h; }
NX=$(_def NX); NY=$(_def NY); NZ=$(_def NZ); JP=$(_def jp)
[ -n "$NX" ] && [ -n "$NY" ] && [ -n "$NZ" ] && [ -n "$JP" ] || fail "無法從 variables.h 讀 NX/NY/NZ/jp"
EXP_NX6=$((NX + 6)); EXP_NYD6=$(( (NY - 1) / JP + 7 )); EXP_NZ6=$((NZ + 6))
EXP_DIMS="${EXP_NX6},${EXP_NYD6},${EXP_NZ6}"

# ── 讀 metadata 欄位 ──
_meta() { awk -F= -v k="$1" '$1==k{print $2; exit}' "$M" | tr -d '[:space:]'; }
MRC=$(_meta mpi_rank_count); DIMS=$(_meta grid_dims); STEP=$(_meta step)
ACCU=$(_meta accu_count);    FTT=$(_meta FTT);        DTG=$(_meta dt_global)

[ "$MRC"  = "$JP" ]       || fail "mpi_rank_count=$MRC != jp=$JP"
[ "$DIMS" = "$EXP_DIMS" ] || fail "grid_dims=$DIMS != 期望 $EXP_DIMS (NX6,NYD6,NZ6 of $NX/$NY/$NZ @jp$JP)"
[ "$STEP" = "1" ]         || fail "step=$STEP != 1 (種子應為 step 1)"
[ "${ACCU:-x}" = "0" ]    || fail "accu_count=$ACCU != 0 (種子統計應歸零)"
case "$DTG" in ""|"-1"|"-1.0"|"-1.000000000000000") fail "dt_global=$DTG 無效 (應為新網格實值)" ;; esac

# ── 檔案數: 19*jp 個 f + jp 個 rho ──
_cnt() { find "$CKPT" -maxdepth 1 -type f -regextype posix-extended -regex "$1" 2>/dev/null | wc -l; }
NF=$(_cnt ".*/f[01][0-9]_[0-9]+\.bin")
NR=$(_cnt ".*/rho_[0-9]+\.bin")
EXP_F=$((19 * JP))
[ "$NF" = "$EXP_F" ] || fail "f files=$NF != $EXP_F (19*jp)"
[ "$NR" = "$JP" ]    || fail "rho files=$NR != jp=$JP"

# ── grid_provenance 必須存在 (solver Preflight C 依賴) ──
[ -s "restart/grid_provenance" ] || fail "restart/grid_provenance 不存在或為空"

echo "[seed-gate] OK: jp=$JP grid_dims=$DIMS step=1 accu_count=0 f=$NF rho=$NR provenance✓ FTT=$FTT dt_global=$DTG"
exit 0
