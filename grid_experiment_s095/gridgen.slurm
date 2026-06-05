#!/bin/bash
# ==============================================================================
# gridgen.slurm — 獨立實驗性網格生成 (s=0.95) on dev compute node
# ------------------------------------------------------------------------------
# 在隔離沙盒 grid_experiment_s095/ 內跑 grid_zeta_tool.py --auto:
#   讀沙盒 variables.h (STRETCH_A=0.95, GAMMA=3.6636) + reference 3.fine grid.dat
#   → Steger-Sorenson Poisson 基底 + Vinokur 拉伸 → adaptive_3.fine grid_I897_J449_s0.950000.dat
#   目標: dz_min < 8.87e-4 (預估 8.61e-4)。完全不碰正在跑的 chain。
# 單一 process numpy (Poisson solve)。計畫 MST115169, partition dev。
# log: grid_experiment_s095/gridgen_<jobid>.log (python -u 即時)。
# ==============================================================================
#SBATCH --job-name=Edit8_gridgen_s095
#SBATCH --account=MST115169
#SBATCH --partition=dev
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --gres=gpu:1
#SBATCH --time=02:00:00
#SBATCH --output=gridgen_%j.log
#SBATCH --error=gridgen_%j.log
#SBATCH --open-mode=append

set -uo pipefail
# 沙盒絕對路徑 (不用 SLURM_SUBMIT_DIR — 那是 sbatch 發起目錄, 可能是專案根; 用它會誤讀專案 variables.h)
cd /home/s8313697/5.Re10595/Edit8_NewInterpolation/grid_experiment_s095 || exit 1

NTHREADS="${SLURM_CPUS_PER_TASK:-8}"
export OMP_NUM_THREADS="$NTHREADS"
export OPENBLAS_NUM_THREADS="$NTHREADS"
export MKL_NUM_THREADS="$NTHREADS"

echo "================================================================"
echo "[gridgen] START $(date '+%F %T')  node=$(hostname)  jobid=${SLURM_JOB_ID:-?}"
echo "[gridgen] cwd=$(pwd)  STRETCH_A=$(grep -oE '#define[[:space:]]+STRETCH_A[[:space:]]+[0-9.]+' variables.h)"
echo "================================================================"

python3 -u J_Frohlich/grid_zeta_tool.py --auto
RC=$?

echo "================================================================"
echo "[gridgen] grid_zeta_tool.py exit = $RC"
echo "[gridgen] END $(date '+%F %T')"
if [ "$RC" -eq 0 ]; then
    echo "[gridgen] 產出網格:"
    ls -la J_Frohlich/adaptive_*s0.95*.dat 2>/dev/null
fi
echo "================================================================"
exit $RC
