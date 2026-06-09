#!/bin/bash
# ==============================================================================
# genseed.slurm — Phase 2 種子場生成 (regrid checkpoint interpolation)
# ------------------------------------------------------------------------------
# 在 H200 16gpus 計算節點直接執行 interp_checkpoint.py:
#   舊粗場 129x257x129 (jp=8) → 新細場 449x897x449 (jp=16)
#   div-exact 投影使 u* 散度 < 1e-10 (--div-gate-tol), 不過閘不寫出。
# 單一 process numpy 工作 (無 MPI/GPU), 取整節點是為了 1.9TB RAM + 112 CPU。
# log: phase2_generatecheckpoint/genseed_<jobid>.log (python -u 即時輸出)。
# 計畫編號 MST114348 (NCHC 政策), partition 16gpus@16。
# ==============================================================================
#SBATCH --job-name=genseed_Re10595
#SBATCH --account=MST114348
#SBATCH --partition=16gpus
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --exclusive
#SBATCH --gres=gpu:8
#SBATCH --time=08:00:00
#SBATCH --output=phase2_generatecheckpoint/genseed_%j.log
#SBATCH --error=phase2_generatecheckpoint/genseed_%j.log
#SBATCH --open-mode=append

set -uo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}" || exit 1

# numpy/BLAS 多執行緒 (整節點 112 核)
NTHREADS="${SLURM_CPUS_ON_NODE:-104}"
export OMP_NUM_THREADS="$NTHREADS"
export OPENBLAS_NUM_THREADS="$NTHREADS"
export MKL_NUM_THREADS="$NTHREADS"
export NUMEXPR_NUM_THREADS="$NTHREADS"

# [FIX 2026-06-04] div-exact 投影預設用 multiprocessing.Pool(min(ncpu,225)) 平行 225 個 FFT
# mode 的 splu 因式分解; fork() 104 個 worker (繼承大 VSZ 父程序) 在嚴格 overcommit 下觸發
# ENOMEM (即使 1.9TB 空閒)。POISSON_SERIAL=1 → 單 worker 序列路徑 (bitwise-identical, 無 fork,
# 峰值僅 1 個因式分解 ~數 GB, ~5-8 分鐘), 仍達機器零散度 (滿足 div-gate 1e-10)。
export POISSON_SERIAL=1

echo "================================================================"
echo "[genseed] START $(date '+%F %T')  node=$(hostname)  jobid=${SLURM_JOB_ID:-?}"
echo "[genseed] partition=${SLURM_JOB_PARTITION:-?}  account=${SLURM_JOB_ACCOUNT:-?}"
echo "[genseed] threads=$NTHREADS  cwd=$(pwd)"
echo "[genseed] RAM: $(free -g | awk '/^Mem:/{print $2" GB total, "$7" GB avail"}')"
echo "================================================================"

# [s0.95] --div-gate-tol 2e-12: 細網格 div-exact 浮點地板 ~1.2e-12 > 原 1e-12 gate (sqrt(N)*eps);
#         放寬到 2e-12 (與 Layer-2 fneq_div_gate_tol 一致), 文件化理由見 interp_checkpoint.py:3957
python3 -u phase2_generatecheckpoint/interp_checkpoint.py --auto --step 1 --div-gate-tol 2e-12
RC=$?

echo "================================================================"
echo "[genseed] interp_checkpoint.py exit code = $RC"
echo "[genseed] END $(date '+%F %T')"
if [ "$RC" -eq 0 ]; then
    echo "[genseed] 產物: restart/checkpoint/step_00000001/  +  restart/grid_provenance"
    ls -la restart/checkpoint/step_00000001/ 2>/dev/null | head -5
fi
echo "================================================================"
exit $RC
