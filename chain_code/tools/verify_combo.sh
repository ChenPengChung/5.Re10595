#!/bin/bash
# ============================================================================
# verify_combo.sh — 靜態驗證「鎖定組合 (partition && jp) 是否正確生效」
# ----------------------------------------------------------------------------
# 用途: 在 restart/LOCK_COMBO 鎖定狀態下, 純靜態(唯讀)檢查 jp / partition / jobscript
#       是否前後一致、組合確實會被 dispatcher 套用。供本專案 /loop 心跳定期呼叫。
#
# 嚴格唯讀: 不投遞、不取消、不改任何檔、不跑 dispatcher 主迴圈、不呼叫 sbatch。
#           (sbatch --test-only 也不跑 — 那屬「動態」驗證; 鎖定態只需靜態檢查。)
#
# 退出碼: 0 = 全部 PASS (WARN 不算失敗); 1 = 有 FAIL; 2 = 用法/環境錯誤。
# 用法:   bash chain_code/tools/verify_combo.sh            # 讀 restart/LOCK_COMBO
#         EXPECT_COMBO="64 H200@64gpus" bash .../verify_combo.sh  # 額外比對期望值
# ============================================================================
set -u

# ── 定位專案根 (本檔在 <root>/chain_code/tools/) ──────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT" || { echo "FATAL: 無法 cd 到 PROJECT_ROOT=$PROJECT_ROOT"; exit 2; }

LOCK_FILE="restart/LOCK_COMBO"
VARS="variables.h"

# ── 計數 + 輸出小工具 ────────────────────────────────────────────────────────
N_PASS=0; N_FAIL=0; N_WARN=0
pass() { printf '  [ \033[32mPASS\033[0m ] %s\n' "$*"; N_PASS=$((N_PASS+1)); }
fail() { printf '  [ \033[31mFAIL\033[0m ] %s\n' "$*"; N_FAIL=$((N_FAIL+1)); }
warn() { printf '  [ \033[33mWARN\033[0m ] %s\n' "$*"; N_WARN=$((N_WARN+1)); }
info() { printf '  [ INFO ] %s\n' "$*"; }

# 依 partition 名靜態推 cap (對齊 tools/partition_lib.sh 的 fallback, 不查 sacctmgr → 快且離線可用)
cap_for_part() {
  case "$1" in
    8gpus|16gpus|32gpus) echo 32 ;;      # MaxTRESPA=gres/gpu=32 (非名稱數字; 對齊 partition_lib.sh fallback)
    64gpus)     echo 64 ;;               # p_64gpus MaxTRESPA=gres/gpu=64
    [0-9]*gpus) echo "${1%%gpus}" ;;     # 其他未列 Ngpus → 名稱數字 (保底)
    dev)        echo 4 ;;
    normal)     echo 16 ;;
    4nodes)     echo 32 ;;
    gb200*)     echo 100000 ;;           # GB200 partitions 無 per-account GPU cap
    *)          echo 100000 ;;
  esac
}
# 每節點 GPU 數 (H200=8, GB200=4)
gpus_per_node() { case "$1" in H200) echo 8 ;; GB200) echo 4 ;; *) echo 8 ;; esac; }

ts="$(date '+%Y-%m-%d %H:%M:%S')"
echo "=== verify_combo.sh — 鎖定組合靜態驗證 @ $ts ==="
echo "PROJECT_ROOT=$PROJECT_ROOT"

# ── [1] LOCK_COMBO 存在且可解析 ──────────────────────────────────────────────
if [ ! -f "$LOCK_FILE" ]; then
  fail "找不到 $LOCK_FILE → 目前非鎖定態 (自由跳轉)。本靜態驗證僅適用鎖定態, 中止。"
  echo "--- 結論: FAIL (無 LOCK_COMBO) ---"; exit 1
fi
LC="$(tr -d '\r\n' < "$LOCK_FILE")"
L_JP="${LC%% *}"
L_TGT="${LC#* }"          # ARCH@partition
L_ARCH="${L_TGT%@*}"
L_PART="${L_TGT#*@}"
if ! [[ "$L_JP" =~ ^[0-9]+$ ]] || [ "$L_TGT" = "$LC" ] || [ -z "$L_ARCH" ] || [ -z "$L_PART" ] \
   || { [ "$L_ARCH" != "H200" ] && [ "$L_ARCH" != "GB200" ]; }; then
  fail "LOCK_COMBO 格式錯誤: '$LC' (應為 '<jp> <ARCH@partition>', 例 '64 H200@64gpus')"
  echo "--- 結論: FAIL (LOCK_COMBO 格式) ---"; exit 1
fi
pass "LOCK_COMBO = '$LC' → jp=$L_JP, arch=$L_ARCH, partition=$L_PART"

# 可選: 與外部期望值比對 (供 /loop 帶 EXPECT_COMBO 防漂移)
if [ -n "${EXPECT_COMBO:-}" ]; then
  if [ "$LC" = "$EXPECT_COMBO" ]; then pass "符合期望組合 EXPECT_COMBO='$EXPECT_COMBO'"
  else fail "LOCK_COMBO='$LC' ≠ 期望 EXPECT_COMBO='$EXPECT_COMBO' (組合被改動?)"; fi
fi

# ── [2] variables.h jp == 鎖定 jp ────────────────────────────────────────────
V_JP="$(awk '/^#define[[:space:]]+jp[[:space:]]/{print $3; exit}' "$VARS" 2>/dev/null)"
if [ -z "$V_JP" ]; then fail "讀不到 variables.h 的 jp"
elif [ "$V_JP" = "$L_JP" ]; then pass "variables.h jp=$V_JP 與鎖定 jp 一致"
else fail "variables.h jp=$V_JP ≠ 鎖定 jp=$L_JP (binary 編譯期 rank 數會不符)"; fi

# ── [3] 鎖定 partition 的 GPU cap 容得下鎖定 jp ───────────────────────────────
CAP="$(cap_for_part "$L_PART")"
if [ "$L_JP" -le "$CAP" ]; then pass "partition $L_PART 每帳號 GPU cap=$CAP ≥ jp=$L_JP (不會永久 PENDING)"
else fail "partition $L_PART cap=$CAP < jp=$L_JP → 此組合會永久 PENDING (MaxGRESPerAccount)"; fi

# ── [4] jobscript 存在 ───────────────────────────────────────────────────────
JS="chain_code/jobscript_chain.slurm.${L_ARCH}"
if [ ! -f "$JS" ]; then
  fail "找不到 jobscript $JS"
  echo; echo "--- 結論: FAIL (jobscript 缺) — PASS=$N_PASS FAIL=$N_FAIL WARN=$N_WARN ---"; exit 1
fi
pass "jobscript 存在: $JS"

# ── [5] header --nodes × --ntasks-per-node == jp; --gres 與每節點 task 一致 ───
H_NODES="$(grep -m1 -E '^#SBATCH --nodes='          "$JS" | sed -E 's/.*--nodes=([0-9]+).*/\1/')"
H_TPN="$(  grep -m1 -E '^#SBATCH --ntasks-per-node=' "$JS" | sed -E 's/.*--ntasks-per-node=([0-9]+).*/\1/')"
H_GRES="$( grep -m1 -E '^#SBATCH --gres=gpu:'        "$JS" | sed -E 's/.*--gres=gpu:([0-9]+).*/\1/')"
PERNODE="$(gpus_per_node "$L_ARCH")"
if [[ "$H_NODES" =~ ^[0-9]+$ ]] && [[ "$H_TPN" =~ ^[0-9]+$ ]]; then
  TOTAL=$(( H_NODES * H_TPN ))
  if [ "$TOTAL" = "$L_JP" ]; then pass "header --nodes=$H_NODES × --ntasks-per-node=$H_TPN = $TOTAL = jp"
  else fail "header --nodes=$H_NODES × --ntasks-per-node=$H_TPN = $TOTAL ≠ jp=$L_JP"; fi
  if [ "$H_TPN" = "$PERNODE" ]; then pass "--ntasks-per-node=$H_TPN 符合 $L_ARCH 每節點 $PERNODE GPU"
  else warn "--ntasks-per-node=$H_TPN ≠ $L_ARCH 預期每節點 $PERNODE (請確認 ppr 對映)"; fi
else
  fail "header 解析失敗 (--nodes='$H_NODES' --ntasks-per-node='$H_TPN')"
fi
if [[ "$H_GRES" =~ ^[0-9]+$ ]]; then
  if [ "$H_GRES" = "${H_TPN:-x}" ]; then pass "--gres=gpu:$H_GRES 與每節點 task 數一致"
  else warn "--gres=gpu:$H_GRES ≠ --ntasks-per-node=$H_TPN (每節點 GPU 與 rank 不符?)"; fi
else info "header 無 --gres=gpu:N (略過)"; fi

# ── [6] mpirun -np 必須 == jp (字面) 或動態 (${NP}/$NP/${SLURM_NTASKS}) ───────
MPI_NP="$(grep -m1 -E '^[[:space:]]*mpirun -np ' "$JS" | sed -E 's/.*mpirun -np ([^ ]+).*/\1/')"
case "$MPI_NP" in
  '${NP}'|'$NP'|'${SLURM_NTASKS}'|'$SLURM_NTASKS'|'${SLURM_NTASKS:'*)
    pass "mpirun -np = '$MPI_NP' (動態追蹤 header → 隨 --nodes 自動跟 jp, 永不 stale)"
    # 動態引用 NP 時, 順帶查 NP fallback
    NP_FB="$(grep -m1 -E '^NP=\$\{SLURM_NTASKS:-[0-9]+\}' "$JS" | sed -E 's/.*:-([0-9]+).*/\1/')"
    if [ -z "$NP_FB" ]; then info "NP fallback 非 \${SLURM_NTASKS:-N} 形式 (略過)"
    elif [ "$NP_FB" = "$L_JP" ]; then pass "NP fallback \${SLURM_NTASKS:-$NP_FB} = jp"
    else warn "NP fallback \${SLURM_NTASKS:-$NP_FB} ≠ jp=$L_JP (SLURM_NTASKS 會覆寫故非致命, 但建議同步)"; fi
    ;;
  '')
    fail "找不到 'mpirun -np ...' 行"
    ;;
  *[!0-9]*)
    fail "mpirun -np = '$MPI_NP' 既非數字也非已知動態變數 → 無法驗證"
    ;;
  *)
    if [ "$MPI_NP" = "$L_JP" ]; then pass "mpirun -np = $MPI_NP = jp (字面值相符)"
    else fail "mpirun -np = $MPI_NP ≠ jp=$L_JP → 會啟 $MPI_NP ranks 撞 $L_JP-rank binary (rank 檢查失敗)"; fi
    ;;
esac

# ── [7] grid_provenance (若有) new_jp 一致 ──────────────────────────────────
if [ -f restart/grid_provenance ]; then
  GP_JP="$(grep -m1 -E '^new_jp=' restart/grid_provenance | cut -d= -f2 | tr -dc 0-9)"
  if [ -z "$GP_JP" ]; then info "grid_provenance 無 new_jp 欄位 (略過)"
  elif [ "$GP_JP" = "$L_JP" ]; then pass "grid_provenance new_jp=$GP_JP = jp"
  else fail "grid_provenance new_jp=$GP_JP ≠ jp=$L_JP → run.sh Preflight C 會 FATAL 擋下續跑"; fi
else
  info "無 restart/grid_provenance (cold/無 checkpoint 態 — 無續跑一致性可違反, OK)"
fi

# ── [8] dispatcher 程式碼確實會套用 LOCK_COMBO (兩條 pick 路徑都有短路) ───────
SD="chain_code/submit_dispatcher.sh"
if [ -f "$SD" ]; then
  L_IN_CLUSTER="$(awk '/^pick_cluster\(\)/{f=1} f&&/LOCK_COMBO/{print; exit}' "$SD")"
  L_IN_JP="$(awk '/^pick_jp_and_partition\(\)/{f=1} f&&/LOCK_COMBO/{print; exit}' "$SD")"
  if [ -n "$L_IN_CLUSTER" ] && [ -n "$L_IN_JP" ]; then
    pass "dispatcher pick_cluster + pick_jp_and_partition 皆有 LOCK_COMBO 短路 (鎖定會被套用)"
  else
    fail "dispatcher 缺 LOCK_COMBO 短路 (pick_cluster:$([ -n "$L_IN_CLUSTER" ] && echo ✓||echo ✗) pick_jp:$([ -n "$L_IN_JP" ] && echo ✓||echo ✗)) → 鎖定可能不生效"
  fi
else
  fail "找不到 $SD"
fi

# ── [9] 衝突哨兵 (唯讀提示, 不改任何檔) ──────────────────────────────────────
[ -f restart/STOP_CHAIN ]       && warn "存在 restart/STOP_CHAIN → chain 不會續投 (鎖定組合不會被實際使用)"
[ -f restart/STOP_DISPATCHER ]  && warn "存在 restart/STOP_DISPATCHER → dispatcher 會停 (不會評估/套用鎖定)"
[ -f restart/STOP_NOCAPACITY ]  && warn "存在 restart/STOP_NOCAPACITY → dispatcher 因長期無容量已收工"
[ -f restart/STOP_JPSWITCH ]    && info "存在 restart/STOP_JPSWITCH (僅凍 jp 自動切換; 與 LOCK_COMBO 不衝突)"

# ── 結論 ────────────────────────────────────────────────────────────────────
echo
if [ "$N_FAIL" -eq 0 ]; then
  echo "--- 結論: ✅ PASS  (PASS=$N_PASS  WARN=$N_WARN  FAIL=0) — 鎖定組合 $LC 靜態一致, 已正確生效 ---"
  exit 0
else
  echo "--- 結論: ❌ FAIL  (PASS=$N_PASS  WARN=$N_WARN  FAIL=$N_FAIL) — 鎖定組合 $LC 有不一致項, 見上方 [FAIL] ---"
  exit 1
fi
