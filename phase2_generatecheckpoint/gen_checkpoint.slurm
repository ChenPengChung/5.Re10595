#!/bin/bash
# ==============================================================================
# gen_checkpoint.slurm — Edit11_Krank5600 Phase-2 warm-start 種子場生成
# ------------------------------------------------------------------------------
# 在 H200 計算節點執行 interp_checkpoint.py (純 numpy/CPU, 無 MPI/GPU 計算):
#   舊場 641x321x321 (jp=32, Edit6 step_86300001) → 新細場 1025x512x512 (jp=64)
#   stitch 32 片 → 全場 → 內插 → div-exact 投影 → 重切 64 片 →
#   寫 restart/checkpoint/step_00000001/ + restart/grid_provenance。
#
# 一次性生成 job (非續鏈 chain); ./run job-guard 不認此 job → 需要時用 `! scancel <id>` 取消。
#
# Partition 選擇 (2026-06-14 實測 live capacity):
#   QOS p_8gpus: MinTRES=gres/gpu=1 (只需 1 token GPU), MaxTRESPerAccount=32,
#   且 MST115169 在 8gpus 用量=0 (Edit6 job 97266 把 16gpus 的 32 cap 佔滿)。
#   → 用 8gpus + gpu:1 立即開跑, 且不碰保留給 jp=64 chain 的 64gpus。2-day walltime。
#
# Poisson 投影: POISSON_SERIAL=1 — 平行 multiprocessing.Pool 會 fork() 繼承大 VSZ,
#   在嚴格 overcommit 下觸發 ENOMEM (Edit7 2026-06-04 實證, 即使 TB 級空閒記憶體);
#   序列路徑 bitwise-identical、~5-8 分鐘、峰值僅單一因式分解數 GB。
# div-gate: --div-gate-tol 2e-12 — 1025x512x512 細網格 float64 散度地板 ~1.2-1.5e-12
#   (≈ sqrt(N)*eps) 略高於原 1e-12 gate; 沿用 Edit7 已文件化的 2e-12。輸出交 Codex 複驗。
# ==============================================================================
#SBATCH --job-name=Edit11_GenerateCheckpoint
#SBATCH --account=MST115169
#SBATCH --partition=64gpus
#SBATCH --nodes=8
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=64
#SBATCH --gres=gpu:1
#SBATCH --mem=360G
#SBATCH --time=08:00:00
#SBATCH --output=phase2_generatecheckpoint/gen_checkpoint_%j.log
#SBATCH --error=phase2_generatecheckpoint/gen_checkpoint_%j.log
#SBATCH --open-mode=append

set -uo pipefail
cd "${SLURM_SUBMIT_DIR:-/home/s8313697/5.Re10595/Edit11_Krank5600}" || exit 1

# numpy/BLAS 多執行緒 (threaded interpolation); Poisson 走序列 (POISSON_SERIAL=1)
NTHREADS="${SLURM_CPUS_PER_TASK:-64}"
export OMP_NUM_THREADS="$NTHREADS"
export OPENBLAS_NUM_THREADS="$NTHREADS"
export MKL_NUM_THREADS="$NTHREADS"
export NUMEXPR_NUM_THREADS="$NTHREADS"
export POISSON_SERIAL=1

echo "================================================================"
echo "[genckpt] START $(date '+%F %T')  node=$(hostname)  jobid=${SLURM_JOB_ID:-?}"
echo "[genckpt] partition=${SLURM_JOB_PARTITION:-?}  account=${SLURM_JOB_ACCOUNT:-?}  threads=$NTHREADS"
echo "[genckpt] cwd=$(pwd)"
echo "[genckpt] RAM: $(free -g | awk '/^Mem:/{print $2" GB total, "$7" GB avail"}')"
echo "[genckpt] source: phase2_generatecheckpoint/oldcheckpoint_Re5600_step_86300001 (jp=32)"
echo "[genckpt] target: restart/checkpoint/step_00000001 (jp=64, 1025x512x512)"
echo "================================================================"

python3 -u phase2_generatecheckpoint/interp_checkpoint.py --auto --step 1 --div-gate-tol 2e-12
RC=$?

echo "================================================================"
echo "[genckpt] interp_checkpoint.py exit code = $RC"
echo "[genckpt] END $(date '+%F %T')"
if [ "$RC" -eq 0 ]; then
    echo "DONE_OK" > phase2_generatecheckpoint/.gen_status
    echo "[genckpt] 產物:"
    ls -la restart/checkpoint/step_00000001/ 2>/dev/null | head -5
    grep -E 'mpi_rank_count|grid_dims|step|FTT|accu_count|dt_global' restart/checkpoint/step_00000001/metadata.dat 2>/dev/null
else
    echo "FAILED" > phase2_generatecheckpoint/.gen_status
fi
echo "================================================================"
exit $RC
