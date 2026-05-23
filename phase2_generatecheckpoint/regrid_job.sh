#!/bin/bash
#SBATCH --job-name=regrid_phase2
#SBATCH --account=MST115169
#SBATCH --partition=dev
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --gres=gpu:1
#SBATCH --mem=64G
#SBATCH --time=0:30:00
#SBATCH --output=slurm_regrid_%j.log
#SBATCH --error=slurm_regrid_%j.err

cd /home/s8313697/5.Re10595/Edit4_ChapmannForpart

echo "=== Phase 2: Checkpoint Interpolation ==="
echo "Host: $(hostname), Memory: $(free -h | awk '/Mem/{print $2}')"
date

python3 -u phase2_generatecheckpoint/interp_checkpoint.py --auto --step 1 \
    --old-dir phase2_generatecheckpoint/oldcheckpoint_Re10595_step_12550001 \
    --variables-h variables.h \
    --old-grid-dat phase1_generategrid/oldgrid_I257_J129_g2.0_a0.5.dat \
    --new-grid-dat phase1_generategrid/newgrid_I513_J257_g3.60_a0.5.dat
RC=$?

echo "=== Exit code: $RC ==="
date

if [ $RC -eq 0 ]; then
    echo "=== Checkpoint generated successfully ==="
    ls -lh restart/checkpoint/step_00000001/
else
    echo "=== FAILED ==="
fi
