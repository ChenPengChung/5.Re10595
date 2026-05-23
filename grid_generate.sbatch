#!/bin/bash
#SBATCH --job-name=grid_gen_5600
#SBATCH --partition=dev
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --gres=gpu:1
#SBATCH --time=01:00:00
#SBATCH --account=MST114348
#SBATCH --output=slurm_grid_%j.log
#SBATCH --error=slurm_grid_%j.err

echo "=== Grid Generation on Compute Node ==="
echo "  Node: $(hostname)"
echo "  Date: $(date)"
echo "  PWD:  $(pwd)"
echo ""

cd /home/s8313697/5.Re10595/Edit6_5600DNS

python3 J_Frohlich/grid_zeta_tool.py --auto

echo ""
echo "=== Grid Generation Complete ==="
echo "  Exit code: $?"
echo "  Date: $(date)"
ls -lh J_Frohlich/adaptive_*.dat 2>/dev/null
