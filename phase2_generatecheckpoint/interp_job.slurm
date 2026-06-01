#!/bin/bash
#SBATCH --job-name=Edit7_interp_phase2
#SBATCH --account=MST115169
#SBATCH --partition=h200
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=24
#SBATCH --gpus-per-node=2
#SBATCH --mem=400G
#SBATCH --time=2-00:00:00
#SBATCH --output=phase2_generatecheckpoint/interp_slurm_%j.log
#SBATCH --error=phase2_generatecheckpoint/interp_slurm_%j.log
#SBATCH --open-mode=append

# Phase-2 checkpoint interpolation (CPU-only). Re5600 257x513x257 (jp=16)
# -> Re10595 449x897x449 (jp=128). Writes restart/checkpoint/step_00000001.
set -euo pipefail
cd "$SLURM_SUBMIT_DIR"

export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK}
export OPENBLAS_NUM_THREADS=${SLURM_CPUS_PER_TASK}
export MKL_NUM_THREADS=${SLURM_CPUS_PER_TASK}
export NUMEXPR_NUM_THREADS=${SLURM_CPUS_PER_TASK}

echo "=== interp job start: $(date) ==="
echo "node=$(hostname)  WorkDir=$PWD  cpus=$SLURM_CPUS_PER_TASK"
python3 --version
python3 -c "import numpy,scipy;print('numpy',numpy.__version__,'scipy',scipy.__version__)"

python3 phase2_generatecheckpoint/interp_checkpoint.py --auto --step 1
RC=$?
echo "=== interp_checkpoint.py exit code: $RC ==="
echo "=== interp job end: $(date) ==="
exit $RC
