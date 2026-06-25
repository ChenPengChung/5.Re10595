#!/bin/bash
# ============================================================================
# jp_lock_selfcheck.sh — Edit13_2800ITBLBM 鎖一致性深檢 (純唯讀)
# ----------------------------------------------------------------------------
# 設計原則 (2026-06-28 起): **以「實際提交的 SLURM job」為唯一真相**, 不在本檔
# 硬寫 account/partition/jp 期望常數 (舊版硬寫 EXP_PIN=dev 卻 EXP_PARTS=32gpus,
# 互相矛盾 → 誤報). 改為:
#   (A) 若 restart/chain_jobid 對應一個線上 job (squeue/scontrol 查得到):
#       以該 job 的 Account / Partition / (NumNodes*8 = jp) 為期望真相 REFSRC=live,
#       驗證各 config 檔 (jobscript header / SC_* / dispatcher ACCOUNT / h200_partition /
#       variables.h jp) 是否都與這個線上 job 一致 → 漂移即 DRIFT.
#   (B) 無線上 job (暫停/換手中): 退回「各 config 檔互相一致」檢查 (以 jobscript
#       header + variables.h jp 為內部基準), 不引用任何硬寫常數.
# 另含與 partition/account 無關的安全閘 (mtime / binary manifest / STOP_CHAIN).
# 純唯讀: 只 read/stat/md5sum/grep/squeue/scontrol, 不寫/不改/不投/不取消.
# 用法: bash chain_code_nchc/tools/jp_lock_selfcheck.sh   (-q 只印單行結論)
#
# 註記 — Edit13 鎖定變更: 2026-06-28 以後改鎖 115 | 16gpus | 32jps
#   (MST115169 / 16gpus / jp32 / 2d); 之前為 115 | dev | 32jps. 改鎖時只改「定義處」
#   (jobscript #SBATCH / SC_* / dispatcher ACCOUNT); 本檢查自動跟著線上 job 走.
# ============================================================================
set -uo pipefail
_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || realpath "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
CHAIN_DIR="$(cd "$(dirname "$_SELF")/.." && pwd)"
ROOT="$(cd "$CHAIN_DIR/.." && pwd)"
cd "$ROOT" 2>/dev/null || { echo "FATAL: 無法進入 $ROOT"; exit 2; }
LIB="$CHAIN_DIR/tools/select_combo_lib.sh"
JS_H200="$CHAIN_DIR/jobscript_chain.slurm.H200"
DISP="$CHAIN_DIR/submit_dispatcher.sh"
QUIET=0; [ "${1:-}" = "-q" ] && QUIET=1

drift=()   # 漂移項
ok=()      # 通過項

# ---- 從 config 檔讀「定義處」實際值 (僅讀, 不當期望真相) ----
js_part=$(awk -F= '/^#SBATCH[[:space:]]+--partition=/{gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2; exit}' "$JS_H200" 2>/dev/null)
js_acct=$(awk -F= '/^#SBATCH[[:space:]]+--account=/{gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2; exit}' "$JS_H200" 2>/dev/null)
sc_parts=$(grep -oE 'SC_PARTITIONS:-[^}"]+' "$LIB" 2>/dev/null | head -1 | sed 's/SC_PARTITIONS:-//' | xargs)
sc_acct=$(grep -oE 'SC_ACCT:-[^}"]+' "$LIB" 2>/dev/null | head -1 | sed 's/SC_ACCT:-//' | xargs)
sc_valid_jp=$(grep -oE 'SC_VALID_JP:-[0-9 ]+' "$LIB" 2>/dev/null | head -1 | sed 's/SC_VALID_JP:-//' | xargs)
disp_acct=$(grep -oE 'ACCOUNT:-[^}"]+' "$DISP" 2>/dev/null | head -1 | sed 's/ACCOUNT:-//' | xargs)
h200_part=$(cat restart/h200_partition 2>/dev/null | xargs)
vjp=$(grep -oE '#define[[:space:]]+jp[[:space:]]+[0-9]+' variables.h 2>/dev/null | grep -oE '[0-9]+$' | head -1)

# ---- (A) 嘗試以線上提交的 job 為唯一真相 ----
JID=$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]')
LINFO=""
if [[ "$JID" =~ ^[0-9]+$ ]]; then
    LINFO=$(scontrol show job "$JID" 2>/dev/null)
    # 只認「活躍狀態」的 job 為真相; 終態(CANCELLED/COMPLETED/FAILED/...)不算 → 退回 config 自一致
    _st=$(echo "$LINFO" | grep -oE 'JobState=[^ ]+' | head -1 | cut -d= -f2)
    case "${_st:-}" in
        PENDING|RUNNING|CONFIGURING|COMPLETING|RESIZING|SUSPENDED) : ;;
        *) LINFO="" ;;
    esac
fi

if [ -n "$LINFO" ]; then
    EXP_ACCOUNT=$(echo "$LINFO" | grep -oE 'Account=[^ ]+'   | head -1 | cut -d= -f2)
    EXP_PART=$(   echo "$LINFO" | grep -oE 'Partition=[^ ]+' | head -1 | cut -d= -f2)
    EXP_NN=$(     echo "$LINFO" | grep -oE 'NumNodes=[0-9]+' | head -1 | cut -d= -f2)
    EXP_STATE=$(  echo "$LINFO" | grep -oE 'JobState=[^ ]+'  | head -1 | cut -d= -f2)
    EXP_JP=""; [ -n "${EXP_NN:-}" ] && EXP_JP=$(( EXP_NN * 8 ))   # H200 8 GPU/node = jp
    REFSRC="live job $JID ($EXP_STATE)"
    ok+=("真相來源 = 線上 job $JID: account=$EXP_ACCOUNT partition=$EXP_PART jp=$EXP_JP ($EXP_STATE)")
else
    # ---- (B) 無線上 job → config 互相一致 (以 jobscript header + variables.h 為內部基準) ----
    EXP_ACCOUNT="$js_acct"; EXP_PART="$js_part"; EXP_JP="$vjp"
    REFSRC="config 自一致 (無線上 job)"
    ok+=("真相來源 = config 自一致 (無線上 job): jobscript account=$js_acct partition=$js_part jp=$vjp")
fi

# 比對工具 (exact, 用於 partition/jp)
_cmp(){ # name actual expected
    if [ "${2:-}" = "${3:-}" ]; then ok+=("$1=$2"); else drift+=("$1=${2:-?} (期望 ${3:-?} <= $REFSRC)"); fi
}
# 比對工具 (case-insensitive, 用於 account: scontrol 回小寫 mst115169 / config 大寫 MST115169)
_cmpci(){ # name actual expected
    if [ "${2,,}" = "${3,,}" ]; then ok+=("$1=$2"); else drift+=("$1=${2:-?} (期望 ${3:-?} <= $REFSRC)"); fi
}

# 1. jobscript header partition/account == 真相
_cmp   "jobscript partition" "$js_part" "$EXP_PART"
_cmpci "jobscript account"   "$js_acct" "$EXP_ACCOUNT"

# 2. dispatcher ACCOUNT (實際 sbatch account 走這個, 非 SC_ACCT) == 真相 account
_cmpci "dispatcher ACCOUNT" "$disp_acct" "$EXP_ACCOUNT"

# 3. SC_PARTITIONS 必含真相 partition (selector 候選集)
if echo " $sc_parts " | grep -qw "$EXP_PART"; then ok+=("SC_PARTITIONS={$sc_parts} 含 $EXP_PART"); else drift+=("SC_PARTITIONS=\"${sc_parts:-?}\" 不含真相 partition $EXP_PART (<= $REFSRC)"); fi

# 4. SC_ACCT (selector 探測用) 與真相 account 一致性 (僅提示, 不致命: 實際走 dispatcher ACCOUNT)
if [ "${sc_acct,,}" = "${EXP_ACCOUNT,,}" ]; then ok+=("SC_ACCT=$sc_acct"); else drift+=("SC_ACCT=${sc_acct:-?} != $EXP_ACCOUNT (selector 探測用; 實際 account 走 dispatcher ACCOUNT, 不一致僅建議對齊)"); fi

# 5. jp 一致: variables.h jp == SC_VALID_JP == 真相 jp
_cmp "variables.h jp" "$vjp" "$EXP_JP"
_cmp "SC_VALID_JP" "$sc_valid_jp" "$EXP_JP"

# 6. h200_partition pin (若存在) == 真相 partition
if [ -n "${h200_part:-}" ]; then _cmp "h200_partition" "$h200_part" "$EXP_PART"; else ok+=("h200_partition absent (jobscript header 為準)"); fi

# ---- 與 partition/account 無關的固有安全閘 ----
# 7. Preflight C: grid_provenance variables_h_mtime == stat variables.h
#    檔不存在 → run.sh Preflight C 整段跳過 (`-e restart/grid_provenance` 才檢查) → 非阻塞 (不報 drift)
vhm=$(stat -c %Y variables.h 2>/dev/null)
if [ ! -e restart/grid_provenance ]; then
    ok+=("grid_provenance absent (Preflight C 跳過, 非阻塞; 重生時須令 variables_h_mtime=$vhm)")
else
    gpm=$(grep -oE 'variables_h_mtime=[0-9]+' restart/grid_provenance 2>/dev/null | grep -oE '[0-9]+$' | head -1)
    if [ "${gpm:-}" = "${vhm:-}" ]; then ok+=("provenance mtime=match"); else drift+=("grid_provenance variables_h_mtime=${gpm:-?} != variables.h mtime=${vhm:-?} (Preflight C 會 FATAL!)"); fi
fi

# 8. binary_manifest jp<jp> md5 == 現役 a.out md5 (jp 動態取自真相)
#    檔不存在 → jp-switch guard inert (僅 dispatcher jp-switch 路徑用) → 非阻塞 (不報 drift)
if [ ! -e restart/binary_manifest.dat ]; then
    ok+=("binary_manifest absent (jp-switch guard inert, 非阻塞)")
elif [ -n "${EXP_JP:-}" ]; then
    mman=$(grep -oE "jp${EXP_JP}=[0-9a-f]+" restart/binary_manifest.dat 2>/dev/null | sed "s/jp${EXP_JP}=//" | head -1)
    maout=$(md5sum a.out 2>/dev/null | cut -d' ' -f1)
    if [ -n "${mman:-}" ] && [ "${mman:-}" = "${maout:-}" ]; then ok+=("a.out=jp${EXP_JP} binary"); else drift+=("a.out md5=${maout:-?} != manifest jp${EXP_JP}=${mman:-?} (現役 binary 非 jp${EXP_JP})"); fi
fi

# 9. STOP_CHAIN absent
if [ -f restart/STOP_CHAIN ]; then drift+=("restart/STOP_CHAIN PRESENT (已要求停鏈)"); else ok+=("STOP_CHAIN absent"); fi

# ---- 結論 ----
ntot=$(( ${#ok[@]} + ${#drift[@]} ))
if [ ${#drift[@]} -eq 0 ]; then
    [ "$QUIET" = 1 ] && echo "jp_lock OK: 一致 ($REFSRC; ${#ok[@]}/$ntot 項通過)" \
                     || { echo "=== jp_lock_selfcheck: OK (一致; 真相 = $REFSRC) ==="; printf '  [✓] %s\n' "${ok[@]}"; }
    exit 0
fi
echo "=== jp_lock_selfcheck: DRIFT (${#drift[@]} 項漂移; 真相 = $REFSRC) ==="
printf '  [✗] %s\n' "${drift[@]}"
[ "$QUIET" = 0 ] && [ ${#ok[@]} -gt 0 ] && printf '  [✓] %s\n' "${ok[@]}"
exit 1
