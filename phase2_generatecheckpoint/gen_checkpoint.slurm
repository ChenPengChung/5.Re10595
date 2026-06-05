#!/bin/bash
# =============================================================================
# gen_checkpoint.slurm — Phase 2 初始資料點生成 (在計算節點直接執行)
#
# [partition 例外] 本作業是「單一 CPU process」(numpy/scipy, 無 GPU/MPI), 只需 1 GPU 佔位。
#   16gpus 的 QOS p_16gpus 有 MinTRES gres/gpu=8 (每 job 最少 8 GPU) → 1-GPU job 會
#   永遠 PENDING(QOSMinGRES)。改投 dev(p_dev 無 MinTRES, cap 4 GPU, walltime 4h) 作為例外。
#
# 在 dev partition 的計算節點上跑 interp_checkpoint.py --auto:
#   輸入: phase1_generategrid/ 的 oldgrid/newgrid + phase2 origin checkpoint
#         (oldcheckpoint_Re5600_step_24115001, jp=8) + variables.h(jp=32)
#   流程: stitch OLD(jp8) → 全域插值到 NEW grid → 質量/體流修正
#         → 速度投影 div-exact(零化 max|div(u*)| < 1e-12) → 重建 f_q
#         → 切割成 jp=32 per-rank 檔 → 寫 restart/checkpoint/step_00000001
#   純 CPU/numpy+scipy (無 GPU/MPI); 進度以 [1/8]..[8/8] 即時寫入 log。
#
# 提交: sbatch phase2_generatecheckpoint/gen_checkpoint.slurm  (WorkDir=本專案)
# Log : phase2_generatecheckpoint/gen_checkpoint_<jobid>.log
# =============================================================================
#SBATCH --job-name=Edit6_gencheckpoint
#SBATCH --account=MST115169
#SBATCH --partition=dev
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --gres=gpu:1
#SBATCH --mem=192G
#SBATCH --time=4:00:00
#SBATCH --output=phase2_generatecheckpoint/gen_checkpoint_%j.log
#SBATCH --open-mode=truncate

set -o pipefail
cd "${SLURM_SUBMIT_DIR:-$(pwd)}" || { echo "[gen] FATAL: cannot cd to SLURM_SUBMIT_DIR"; exit 1; }

# numpy/scipy 來自 /usr/bin/python3 + $HOME/.local (shared home → compute node 一致);
# 不 module purge / 不載入會遮蔽 /usr/bin/python3 的 python module。
PY=/usr/bin/python3
export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK:-32}
export OPENBLAS_NUM_THREADS=${SLURM_CPUS_PER_TASK:-32}
export MKL_NUM_THREADS=${SLURM_CPUS_PER_TASK:-32}

echo "============================================================"
echo "[gen] host=$(hostname)  jobid=${SLURM_JOB_ID}  start=$(date '+%F %T')"
echo "[gen] cwd=$(pwd)"
echo "[gen] python=$($PY --version 2>&1)  $($PY -c 'import numpy,scipy;print("numpy",numpy.__version__,"scipy",scipy.__version__)' 2>&1)"
echo "[gen] partition=${SLURM_JOB_PARTITION} nodes=${SLURM_JOB_NUM_NODES} cpus=${SLURM_CPUS_PER_TASK} mem=192G"
echo "============================================================"

$PY -u phase2_generatecheckpoint/interp_checkpoint.py \
    --auto --step 1 \
    --old-dir phase2_generatecheckpoint/oldcheckpoint_Re5600_step_24115001 \
    --variables-h variables.h \
    --output-root restart/checkpoint \
    --project-velocity div-exact
RC=$?

echo "============================================================"
echo "[gen] python exit RC=${RC}  end=$(date '+%F %T')"
if [ "$RC" -eq 0 ]; then
    echo "[gen] OK — generated checkpoint:"
    ls -la restart/checkpoint/step_00000001/ 2>/dev/null | head -5
    echo "[gen]   (... $(ls restart/checkpoint/step_00000001/ 2>/dev/null | wc -l) files total)"
    grep -E 'mpi_rank_count|grid_dims|step=' restart/checkpoint/step_00000001/metadata.dat 2>/dev/null
fi
echo "============================================================"
exit $RC
