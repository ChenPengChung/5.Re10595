#!/bin/bash
# ==============================================================================
# gen_interp.slurm — 計算節點上單純生成 interpolation_checkpoint (data point)
#   2026-06-18: regrid 冷啟資料點重生 (使用者指定 64gpus@4nodes)
#   執行: interp_checkpoint.py  origin(257x129,jp8,FTT148.7) -> new(641x321,jp32)
#   產出: restart/checkpoint/step_00000001/ + restart/grid_provenance
#   性質: 單核 numpy/scipy + Poisson 投影 (∇·u* < 1e-12 硬閘門); 不需 GPU/MPI。
#         本作業只生成 IC, 不跑 solver ("先單純生成資料點")。
# ==============================================================================
#SBATCH --job-name=Edit6_geninterp
#SBATCH --account=MST114348
#SBATCH --partition=64gpus
#SBATCH --nodes=4
#SBATCH --ntasks-per-node=8
#SBATCH --gres=gpu:8
#SBATCH --cpus-per-task=1
#SBATCH --time=1-00:00:00
#SBATCH --output=gen_interp_%j.log
#SBATCH --open-mode=truncate

cd "${SLURM_SUBMIT_DIR:-$(pwd)}" || { echo "[geninterp] FATAL: cannot cd"; exit 1; }

export OMP_NUM_THREADS=8
export OPENBLAS_NUM_THREADS=8
export MKL_NUM_THREADS=8

ORIGIN="phase2_generatecheckpoint/oldcheckpoint_Re5600_step_24115001"
OLD_GRID="phase1_generategrid/oldgrid_I257_J129_g2.0_a0.5.dat"
NEW_GRID="phase1_generategrid/newgrid_3.fine grid_I641_J321_s0.950000.dat"

echo "[geninterp] host    = $(hostname)"
echo "[geninterp] start   = $(date '+%F %T')"
echo "[geninterp] cwd     = $(pwd)"
echo "[geninterp] python3 = $(which python3)"
echo "[geninterp] numpy   = $(python3 -c 'import numpy;print(numpy.__version__)' 2>&1)"
echo "[geninterp] interp  : origin(257x129,jp8,FTT148.7) -> new(641x321,jp32)"
echo "================================================================"

stdbuf -oL -eL python3 -u phase2_generatecheckpoint/interp_checkpoint.py \
    --auto --step 1 \
    --old-dir "$ORIGIN" \
    --variables-h variables.h \
    --old-grid-dat "$OLD_GRID" \
    --new-grid-dat "$NEW_GRID" \
    --old-gamma 2.0 --old-alpha 0.5
rc=$?

echo "================================================================"
echo "[geninterp] interp rc=$rc  end=$(date '+%F %T')"

CK="restart/checkpoint/step_00000001"
if [ "$rc" -eq 0 ] && [ -s "$CK/metadata.dat" ] && [ -s "$CK/f00_0.bin" ] \
   && [ -s "$CK/rho_0.bin" ] && [ -s "restart/grid_provenance" ]; then
    echo "[geninterp] SUCCESS: checkpoint complete at $CK"
    ls -la "$CK" | head -5
    echo "--- metadata ---"; grep -aE 'step|FTT|grid_dims|mpi_rank_count|accu' "$CK/metadata.dat" 2>/dev/null | head
    echo "--- grid_provenance ---"; head "restart/grid_provenance" 2>/dev/null
    echo "[geninterp] DONE — IC ready. Next: rebuild + ./run submits solver (FTT_STATS_START=25)."
else
    echo "[geninterp] FATAL: interp failed or products incomplete (rc=$rc)"
    ls -la "$CK" 2>/dev/null
    exit 1
fi
