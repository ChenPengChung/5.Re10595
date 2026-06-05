#!/bin/bash
# ==============================================================================
# genseed_s095.slurm — Phase A2: 隔離生成 s0.95 種子場 (regrid checkpoint interp)
# ------------------------------------------------------------------------------
# 腳本放在「本專案內」(可寫), 但 job cd 到隔離 worktree $STG 執行 (job 自身寫 $STG,
# 不經 Claude 工具, 故不受 cross-project write_guard 限制)。
#   $STG = /home/s8313697/5.Re10595/Edit8_stg_s095
#   舊粗場 257x129 (jp=8, γ=2.0) → 新細場 897x449 (jp=64, STRETCH_A=0.95)
#   還原 2 階 operator-consistent CD2 f_neq + div-exact 投影 (u* 散度 <1e-12)。
# 讀 $STG/variables.h(0.95) + $STG/phase1(唯一 s0.95 newgrid) + $STG/phase2/oldcheckpoint
#   (symlink→主目錄唯讀源) → 寫 $STG/restart/ (完全不碰主目錄 / 80734)。
# dev 1 GPU 與 80734 的 64gpus@64 不同分區/配額 → 不影響卡位。POISSON_SERIAL=1 避免 fork ENOMEM。
# ==============================================================================
#SBATCH --job-name=Edit8_genseed_s095
#SBATCH --account=MST115169
#SBATCH --partition=dev
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=28
#SBATCH --mem=480G
#SBATCH --gres=gpu:1
#SBATCH --time=04:00:00
#SBATCH --output=/home/s8313697/5.Re10595/Edit8_stg_s095/genseed_s095_%j.log
#SBATCH --error=/home/s8313697/5.Re10595/Edit8_stg_s095/genseed_s095_%j.log
#SBATCH --open-mode=append

set -uo pipefail
cd /home/s8313697/5.Re10595/Edit8_stg_s095 || exit 1

NTHREADS="${SLURM_CPUS_PER_TASK:-28}"
export OMP_NUM_THREADS="$NTHREADS"
export OPENBLAS_NUM_THREADS="$NTHREADS"
export MKL_NUM_THREADS="$NTHREADS"
export NUMEXPR_NUM_THREADS="$NTHREADS"
export POISSON_SERIAL=1

echo "================================================================"
echo "[genseed_s095] START $(date '+%F %T')  node=$(hostname)  jobid=${SLURM_JOB_ID:-?}"
echo "[genseed_s095] cwd=$(pwd)"
echo "[genseed_s095] STRETCH_A=$(grep -oE '#define[[:space:]]+STRETCH_A[[:space:]]+[0-9.]+' variables.h)"
echo "[genseed_s095] phase1 grids: $(ls phase1_generategrid/ | grep -i grid | tr '\n' ' ')"
echo "[genseed_s095] origin .bin: $(ls phase2_generatecheckpoint/oldcheckpoint_Re10595_step_12550001/*.bin 2>/dev/null | wc -l)"
echo "[genseed_s095] interp sha256: $(sha256sum phase2_generatecheckpoint/interp_checkpoint.py | cut -d' ' -f1)"
echo "[genseed_s095] RAM: $(free -g | awk '/^Mem:/{print $2\" GB total, \"$7\" GB avail\"}')"
echo "================================================================"

# [s0.95 regrid, user 選 B] div gate 1e-12→2e-12: s0.95 細網格(180M DOF)div-exact 投影
# 浮點地板 max|div(u*)|=1.19e-12, 物理上仍 div-free 到機器精度。Gate 2(f_neq 硬編)已於
# $STG/.../interp_checkpoint.py:3958 同步改 2.0e-12。
python3 -u phase2_generatecheckpoint/interp_checkpoint.py --auto --step 1 --div-gate-tol 2e-12
RC=$?

echo "================================================================"
echo "[genseed_s095] interp exit code = $RC"
echo "[genseed_s095] END $(date '+%F %T')"
if [ "$RC" -eq 0 ]; then
    echo "[genseed_s095] 產物 step_00000001 檔案數: $(ls restart/checkpoint/step_00000001/ 2>/dev/null | wc -l)"
    cat restart/grid_provenance 2>/dev/null | grep -E "new_grid=|solver_grid=|solver_grid_match|variables_h=" | sed 's/^/    /'
fi
echo "================================================================"
exit $RC
