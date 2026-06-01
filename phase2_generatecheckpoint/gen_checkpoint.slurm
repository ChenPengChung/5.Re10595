#!/bin/bash
# ==============================================================================
# gen_checkpoint.slurm — phase2 資料點生成 (在 h200 計算節點上做計算)
#   執行: python3 phase2_generatecheckpoint/interp_checkpoint.py --auto --step 1
#   auto 自動解析:
#     OLD ckpt : phase2_generatecheckpoint/oldcheckpoint_Re5600_step_16242001 (257x513x257, jp16)
#     OLD grid : phase1_generategrid/oldgrid_3.fine grid_I513_J257_s0.950000.dat
#     NEW grid : phase1_generategrid/newgrid_3.fine grid_I641_J321_s0.800000.dat
#     NEW 參數 : variables.h -> NX321 NY641 NZ321 jp64
#   產出: restart/checkpoint/step_00000001  (FTT/accu_count 歸零, regrid 設計如此)
#   預設: CE f_neq + jacobian-gl + div-exact 投影 (= 上一輪驗證過設定)
#   log : phase2_generatecheckpoint/phase2_interp_<jobid>.log (python -u + stdbuf 即時)
#   純 CPU + scipy 序列工作 (非 MPI); div-exact 稀疏分解最吃資源 -> 4 天 walltime 餘裕
# ==============================================================================
#SBATCH --job-name=Edit6_phase2interp
#SBATCH --account=MST115169
#SBATCH --partition=h200
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --gres=gpu:1
#SBATCH --time=4-00:00:00
#SBATCH --output=phase2_generatecheckpoint/phase2_interp_%j.log
#SBATCH --open-mode=truncate

cd "${SLURM_SUBMIT_DIR:-$(pwd)}" || { echo "[phase2] FATAL: cannot cd"; exit 1; }

export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK:-8}
export OPENBLAS_NUM_THREADS=${SLURM_CPUS_PER_TASK:-8}
export MKL_NUM_THREADS=${SLURM_CPUS_PER_TASK:-8}

echo "[phase2] job_id=${SLURM_JOB_ID}"
echo "[phase2] host=$(hostname)"
echo "[phase2] start=$(date '+%F %T')"
echo "[phase2] cwd=$(pwd)"
echo "[phase2] python=$(which python3)"

# Pre-flight: numpy + scipy must import (div-exact projection needs scipy.sparse)
if ! python3 -c "import numpy, scipy" 2>/tmp/phase2_dep_err.$$; then
    echo "[phase2] FATAL: numpy/scipy not importable on this node:"
    cat /tmp/phase2_dep_err.$$ 2>/dev/null
    rm -f /tmp/phase2_dep_err.$$
    exit 1
fi
rm -f /tmp/phase2_dep_err.$$
echo "[phase2] numpy=$(python3 -c 'import numpy;print(numpy.__version__)') scipy=$(python3 -c 'import scipy;print(scipy.__version__)')"
echo "================================================================"

# --auto resolves NEW grid (s-naming OK), old checkpoint, and variables.h params.
# OLD grid is supplied explicitly because its s{STRETCH_A} filename is not matched by
# the --auto OLD-grid scanner (it only parses legacy _g{GAMMA}_a{ALPHA}). s0.95 -> GAMMA=ln(39).
stdbuf -oL -eL python3 -u phase2_generatecheckpoint/interp_checkpoint.py --auto --step 1 \
    --old-grid-dat "phase1_generategrid/oldgrid_3.fine grid_I513_J257_s0.950000.dat" \
    --old-gamma 3.6635616461296454 --old-alpha 0.5
rc=$?

echo "================================================================"
echo "[phase2] exit rc=${rc}  end=$(date '+%F %T')"
if [ "$rc" -eq 0 ]; then
    echo "[phase2] output checkpoint:"
    ls -la restart/checkpoint/step_00000001/ 2>/dev/null | head -8
    echo "[phase2] metadata:"
    grep -E "mpi_rank_count|grid_dims|step|FTT|accu_count|dt_global" restart/checkpoint/step_00000001/metadata.dat 2>/dev/null
fi
exit $rc
