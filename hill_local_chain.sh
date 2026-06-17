#!/bin/bash
# ==============================================================================
# hill_local_chain.sh — 本地 V100 單節點 8-GPU「一段」執行
# ------------------------------------------------------------------------------
# 取代 NCHC 的 chain_code/jobscript_chain.slurm.*(那靠 SLURM walltime + sbatch
# resubmit)。本地由 cfdq daemon 在「搶到的」V100 節點上以此啟動,負責一「段」
# 計算 + 與 cfdq 溝通要不要續鏈。
#
# 與 cfdq --chain 的契約 (cfdq 於母機讀 $CFDQ_EXIT_FILE 判斷):
#   exit 0    收斂 / 使用者明確停 (a.out 收到 SIGUSR2/SIGTERM → exit 0)  → 停鏈
#   exit 124  SIGUSR1 優雅被搶 (checkpoint 已寫),或 a.out 崩潰          → 續鏈
#   exit 42   設定致命錯誤 (bad argv 等)                                 → 停鏈
#   exit 檔從未出現 + 程序消失  = 被 kill -9 / 節點掛掉                   → cfdq 視為續鏈
#
# 環境變數 (cfdq 注入;皆有預設,亦可手動執行):
#   CFDQ_EXIT_FILE  結束碼寫此檔 (NFS, 母機可讀);   預設 restart/.chain_exit
#   CFDQ_NP         MPI ranks = GPU 數;             預設 8
# 用法 (手動): bash hill_local_chain.sh
# ==============================================================================
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"

NP="${CFDQ_NP:-8}"
EXIT_FILE="${CFDQ_EXIT_FILE:-restart/.chain_exit}"
MPIRUN=/opt/openmpi-3.1.4/bin/mpirun
LOG="run_local_$(date +%Y%m%d_%H%M%S).log"

# a.out 執行期需要的動態庫 (NFS 全節點可見)
export LD_LIBRARY_PATH="/opt/openmpi-3.1.4/lib:/usr/local/cuda-12.4/lib64:${LD_LIBRARY_PATH:-}"
export PATH="/opt/openmpi-3.1.4/bin:$PATH"

mkdir -p restart
rm -f "$EXIT_FILE"                         # 清掉上段 exit 檔 → 代表「執行中」
finish() { echo "$1" > "$EXIT_FILE"; }     # 結束碼落 NFS 給 cfdq

# ---- GPU 排列: 降低 MPI halo 交換時間 ----
# 8×V100-SXM2 (DGX-1 拓樸) 是「兩個 4-GPU NVLink 島」(phys 0-3 / 4-7);順序排 0..7 時,
# 流向 rank3↔rank4 邊界落在 GPU3↔GPU4 — 那條 P2P 不通(nvidia-smi topo -p2p = TNS),halo
# 只能繞主機(PCIe+QPI)→ MPI 被拖到 11–22ms。改用全程 NVLink 的 Hamiltonian 鏈
#   rank: 0  1  2  3  4  5  6  7
#   GPU : 0  4  5  1  2  6  7  3   (每相鄰段在 P2P 矩陣皆 OK/NVLink)
# → 每個流向 neighbor 都走 NVLink、不經主機。物理零影響(只換實體GPU↔slab 對應)。
if [ "$NP" = "8" ]; then
    GPUS="0,4,5,1,2,6,7,3"                 # 全 NVLink 流向鏈 (DGX-1 8×V100)
else
    GPUS=$(seq -s, 0 $((NP-1)))            # 其他 NP: 順序排列 (fallback)
fi

# ---- 1. 冷啟 vs 續跑 (取最新、非 .WRITING 的 checkpoint) ----
latest=$(ls -1dv restart/checkpoint/step_* 2>/dev/null | grep -v '\.WRITING$' | tail -1)
if [ -n "$latest" ]; then FLAG="--restart=$latest"; MODE="續跑 from $latest"
else                      FLAG="--cold";            MODE="冷啟 (無 checkpoint)"; fi

{ echo "[chain] ===== $(date '+%F %T')  host=$(hostname -s)  $MODE ====="
  echo "[chain] CUDA_VISIBLE_DEVICES=$GPUS  mpirun -np $NP ./a.out $FLAG"; } | tee -a "$LOG"

# ---- 2. 啟動 (背景化以便轉發信號);-mca orte_forward_job_control 1 確保信號送達 a.out ----
export CUDA_VISIBLE_DEVICES="$GPUS"
"$MPIRUN" -np "$NP" --bind-to none -mca orte_forward_job_control 1 \
    ./a.out "$FLAG" >>"$LOG" 2>&1 &
MPI_PID=$!

# ---- 3. 信號轉發: 母機/使用者 → 本 wrapper → mpirun → a.out ----
#   USR1 = 優雅被搶 (a.out checkpoint+exit124);  USR2/TERM = 明確停 (a.out exit0)
trap 'echo "[chain] →SIGUSR1 (優雅被搶, 觸發 checkpoint)" | tee -a "$LOG"; kill -USR1 "$MPI_PID" 2>/dev/null' USR1
trap 'kill -USR2 "$MPI_PID" 2>/dev/null' USR2
trap 'kill -TERM "$MPI_PID" 2>/dev/null' TERM

# ---- 4. 等 a.out 真正結束 ----
#   保證至少 wait 一次(含瞬間失敗才不會誤判 rc=0);
#   wait 被 trap 打斷(rc>128)且子程序還活著時, 續等到它真的結束。
wait "$MPI_PID"; rc=$?
while [ "$rc" -gt 128 ] && kill -0 "$MPI_PID" 2>/dev/null; do wait "$MPI_PID"; rc=$?; done

echo "[chain] a.out exit rc=$rc" | tee -a "$LOG"
case "$rc" in
  0)   finish 0;   echo "[chain] DONE (收斂/停) → 停鏈" | tee -a "$LOG" ;;
  124) finish 124; echo "[chain] 優雅被搶 (checkpoint 已寫) → 續鏈" | tee -a "$LOG" ;;
  42)  finish 42;  echo "[chain] FATAL 設定錯誤 → 停鏈" | tee -a "$LOG" ;;
  *)   finish 124; echo "[chain] 崩潰 rc=$rc → 續鏈 (下台 V100 從上個 checkpoint 續)" | tee -a "$LOG" ;;
esac
exit "$rc"
