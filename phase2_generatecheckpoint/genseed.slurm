#!/bin/bash
# ==============================================================================
# genseed.slurm — Phase 2 種子場生成 (regrid checkpoint interpolation) [Edit8]
# ------------------------------------------------------------------------------
# 在 H200 'dev' 計算節點直接執行 interp_checkpoint.py:
#   舊粗場 129x257x129 (jp=8) → 新細場 449x897x449 (jp=64)
#   還原版 (== Edit7 低誤差版): CE f_neq 用 2 階 operator-consistent CD2,
#   div-exact 投影使 u* 散度 < 1e-12 (--div-gate-tol),
#   第二層 dilatational gate 同樣 < 1e-12, 不過閘則 sys.exit 不寫出。
# 單一 process numpy 工作 (無 MPI, 不用 GPU; gpu:1 只為符合 dev 節點配置)。
# 資源沿用已驗證可跑的 job 78457: dev / mst115169 / 28cpu / 480G / gpu:1。
# log: phase2_generatecheckpoint/genseed_<jobid>.log (python -u 即時輸出)。
# 計畫編號 MST115169 (NCHC 政策), partition dev (4h 上限, 4 GPU/account cap)。
# ==============================================================================
#SBATCH --job-name=Edit8_genseed
#SBATCH --account=MST115169
#SBATCH --partition=dev
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=28
#SBATCH --mem=480G
#SBATCH --gres=gpu:1
#SBATCH --time=04:00:00
#SBATCH --output=phase2_generatecheckpoint/genseed_%j.log
#SBATCH --error=phase2_generatecheckpoint/genseed_%j.log
#SBATCH --open-mode=append

set -uo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}" || exit 1

# numpy/BLAS 多執行緒
NTHREADS="${SLURM_CPUS_PER_TASK:-28}"
export OMP_NUM_THREADS="$NTHREADS"
export OPENBLAS_NUM_THREADS="$NTHREADS"
export MKL_NUM_THREADS="$NTHREADS"
export NUMEXPR_NUM_THREADS="$NTHREADS"

# [FIX 2026-06-04, 沿用 Edit7] div-exact 投影預設用 multiprocessing.Pool 平行多個 splu
# 因式分解; fork() worker 繼承大 VSZ 父程序, 嚴格 overcommit 下觸發 ENOMEM。
# POISSON_SERIAL=1 → 單 worker 序列路徑 (bitwise-identical, 無 fork, 峰值僅數 GB,
# ~5-8 分鐘), 仍達機器零散度 (滿足 div-gate 1e-12)。
export POISSON_SERIAL=1

echo "================================================================"
echo "[genseed] START $(date '+%F %T')  node=$(hostname)  jobid=${SLURM_JOB_ID:-?}"
echo "[genseed] partition=${SLURM_JOB_PARTITION:-?}  account=${SLURM_JOB_ACCOUNT:-?}"
echo "[genseed] threads=$NTHREADS  cwd=$(pwd)"
echo "[genseed] RAM: $(free -g | awk '/^Mem:/{print $2" GB total, "$7" GB avail"}')"
echo "[genseed] interp_checkpoint.py sha256: $(sha256sum phase2_generatecheckpoint/interp_checkpoint.py | cut -d' ' -f1)"
echo "================================================================"

python3 -u phase2_generatecheckpoint/interp_checkpoint.py --auto --step 1
RC=$?

echo "================================================================"
echo "[genseed] interp_checkpoint.py exit code = $RC"
echo "[genseed] END $(date '+%F %T')"
if [ "$RC" -eq 0 ]; then
    echo "[genseed] 產物: restart/checkpoint/step_00000001/  +  restart/grid_provenance"
    ls -la restart/checkpoint/step_00000001/ 2>/dev/null | head -8
    echo "[genseed] grid_provenance:"
    cat restart/grid_provenance 2>/dev/null | head -20
fi
echo "================================================================"
exit $RC
