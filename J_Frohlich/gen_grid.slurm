#!/bin/bash
# ==============================================================================
# gen_grid.slurm — 在計算節點上產生 Edit6_5600DNS 的 adaptive 網格
#   執行: python3 J_Frohlich/grid_zeta_tool.py --auto
#   讀取: variables.h (NY=641, NZ=321, STRETCH_A=0.80, RATIO_LO=0.0, RATIO_HI=20.0)
#   產出: J_Frohlich/adaptive_3.fine grid_I641_J321_s0.800000.dat
#   log : J_Frohlich/gridgen_<jobid>.log  (python -u + stdbuf 即時更新, 可 tail 追蹤)
# 純 CPU 單核 numpy 工作 (Steger-Sorenson Poisson SOR, 上限 15000 iter,
#   每 2000 iter 印 max_corr) — 不需 GPU。
# ==============================================================================
#SBATCH --job-name=Edit6_gridgen
#SBATCH --account=MST114348
#SBATCH --partition=dev
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --gpus-per-node=1
#SBATCH --time=00:50:00
#SBATCH --output=J_Frohlich/gridgen_%j.log
#SBATCH --open-mode=truncate

cd "${SLURM_SUBMIT_DIR:-$(pwd)}" || { echo "[gridgen] FATAL: cannot cd"; exit 1; }

export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK:-4}
export OPENBLAS_NUM_THREADS=${SLURM_CPUS_PER_TASK:-4}

echo "[gridgen] host       : $(hostname)"
echo "[gridgen] start      : $(date '+%F %T')"
echo "[gridgen] cwd        : $(pwd)"
echo "[gridgen] python3    : $(which python3)"
echo "[gridgen] numpy      : $(python3 -c 'import numpy; print(numpy.__version__)' 2>&1)"
echo "[gridgen] cmd        : python3 -u J_Frohlich/grid_zeta_tool.py --auto"
echo "================================================================"

stdbuf -oL -eL python3 -u J_Frohlich/grid_zeta_tool.py --auto
rc=$?

echo "================================================================"
echo "[gridgen] exit rc=$rc  end=$(date '+%F %T')"
if [ "$rc" -eq 0 ]; then
    echo "[gridgen] produced:"
    ls -la J_Frohlich/adaptive_*.dat 2>/dev/null
fi
exit $rc
