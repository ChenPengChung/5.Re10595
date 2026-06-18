#!/bin/bash
# ============================================================================
# jp_lock_selfcheck.sh — Edit14_2800GILBM「16gpus@32jp 鎖」一致性深檢 (純唯讀)
# ----------------------------------------------------------------------------
# Edit6 沒有 Edit7 那套 jp-lock 哨兵 (LOCK_JP_PARTITION / jp_lock_status /
# jp_lock_DRIFT.alert)。Edit6 的「鎖」是底下這幾個檔同時成立:
#   jp 鎖定 32 (variables.h #define jp 32 + select_combo_lib SC_VALID_JP 預設 32)
#   partition 自由集 = {8gpus,16gpus,32gpus}@jp32 (SC_PARTITIONS 預設)
#   暫時 pin = restart/h200_partition = 16gpus
#   Preflight C 閘門: grid_provenance variables_h_mtime == stat variables.h
#   binary 防蓋回: binary_manifest jp32 md5 == 現役 a.out md5
#   STOP_CHAIN absent (未要求停鏈)
# 全部一致 → exit 0 (OK)。任一漂移 → exit 1 並印 DRIFT 行 (供心跳立即回報)。
# 純唯讀: 只 read/stat/md5sum/grep, 不寫/不改/不投/不取消任何東西。
# 用法: bash chain_code/tools/jp_lock_selfcheck.sh   (-q 只印單行結論)
# ============================================================================
set -uo pipefail
ROOT="/home/chenpengchung/5.Re10595/Edit14_2800GILBM"
cd "$ROOT" 2>/dev/null || { echo "FATAL: 無法進入 $ROOT"; exit 2; }
LIB="chain_code/tools/select_combo_lib.sh"
QUIET=0; [ "${1:-}" = "-q" ] && QUIET=1

EXP_JP=32
EXP_PIN="16gpus"
EXP_VALID_JP="32"
EXP_PARTS="8gpus 16gpus 32gpus"

drift=()   # 漂移項
ok=()      # 通過項

# 1. variables.h jp == 32
vjp=$(grep -oE '#define[[:space:]]+jp[[:space:]]+[0-9]+' variables.h 2>/dev/null | grep -oE '[0-9]+$' | head -1)
if [ "${vjp:-}" = "$EXP_JP" ]; then ok+=("variables.h jp=$vjp"); else drift+=("variables.h jp=${vjp:-?} (期望 $EXP_JP)"); fi

# 2. SC_VALID_JP 預設 == 32 (jp 鎖定)
svj=$(grep -oE 'SC_VALID_JP:-[0-9 ]+' "$LIB" 2>/dev/null | head -1 | sed 's/SC_VALID_JP:-//' | xargs)
if [ "${svj:-}" = "$EXP_VALID_JP" ]; then ok+=("SC_VALID_JP=$svj"); else drift+=("SC_VALID_JP=\"${svj:-?}\" (期望 \"$EXP_VALID_JP\"; 多出值=未鎖 jp32)"); fi

# 3. SC_PARTITIONS 預設 == {8gpus,16gpus,32gpus} (自由集, 不含 64gpus)
scp=$(grep -oE 'SC_PARTITIONS:-[^}"]+' "$LIB" 2>/dev/null | head -1 | sed 's/SC_PARTITIONS:-//' | xargs)
scp_sorted=$(echo "$scp" | tr ' ' '\n' | sort | xargs)
exp_sorted=$(echo "$EXP_PARTS" | tr ' ' '\n' | sort | xargs)
if [ "$scp_sorted" = "$exp_sorted" ]; then ok+=("SC_PARTITIONS={$scp}"); else drift+=("SC_PARTITIONS=\"${scp:-?}\" (期望 \"$EXP_PARTS\")"); fi

# 4. restart/h200_partition pin == 16gpus
pin=$(cat restart/h200_partition 2>/dev/null | xargs)
if [ "${pin:-}" = "$EXP_PIN" ]; then ok+=("h200_partition=$pin"); else drift+=("h200_partition=${pin:-?} (期望 $EXP_PIN)"); fi

# 5. Preflight C: grid_provenance variables_h_mtime == stat variables.h
gpm=$(grep -oE 'variables_h_mtime=[0-9]+' restart/grid_provenance 2>/dev/null | grep -oE '[0-9]+$' | head -1)
vhm=$(stat -c %Y variables.h 2>/dev/null)
if [ -n "${gpm:-}" ] && [ "${gpm:-}" = "${vhm:-}" ]; then ok+=("provenance mtime=match"); else drift+=("grid_provenance variables_h_mtime=${gpm:-?} != variables.h mtime=${vhm:-?} (Preflight C 會 FATAL!)"); fi

# 6. binary_manifest jp32 md5 == 現役 a.out md5
mman=$(grep -oE 'jp32=[0-9a-f]+' restart/binary_manifest.dat 2>/dev/null | sed 's/jp32=//' | head -1)
maout=$(md5sum a.out 2>/dev/null | cut -d' ' -f1)
if [ -n "${mman:-}" ] && [ "${mman:-}" = "${maout:-}" ]; then ok+=("a.out=jp32 binary"); else drift+=("a.out md5=${maout:-?} != manifest jp32=${mman:-?} (現役 binary 非 jp32!)"); fi

# 7. STOP_CHAIN absent
if [ -f restart/STOP_CHAIN ]; then drift+=("restart/STOP_CHAIN PRESENT (已要求停鏈)"); else ok+=("STOP_CHAIN absent"); fi

# ---- 結論 ----
if [ ${#drift[@]} -eq 0 ]; then
    [ "$QUIET" = 1 ] && echo "jp_lock OK: 16gpus@jp32 鎖一致 (${#ok[@]}/7 項通過)" \
                     || { echo "=== jp_lock_selfcheck: OK (16gpus@jp32 鎖一致) ==="; printf '  [✓] %s\n' "${ok[@]}"; }
    exit 0
fi
echo "=== jp_lock_selfcheck: DRIFT (${#drift[@]} 項漂移) ==="
printf '  [✗] %s\n' "${drift[@]}"
[ "$QUIET" = 0 ] && [ ${#ok[@]} -gt 0 ] && printf '  [✓] %s\n' "${ok[@]}"
exit 1
