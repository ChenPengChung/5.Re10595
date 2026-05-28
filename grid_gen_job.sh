#!/bin/bash
#SBATCH --job-name=gridgen_10595
#SBATCH --account=MST114348
#SBATCH --partition=dev
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --gres=gpu:1
#SBATCH --time=01:00:00
#SBATCH --output=grid_gen_%j.log
#SBATCH --error=grid_gen_%j.err

cd /home/s8313697/5.Re10595/Edit7_10595SNS

echo "=== Grid Generation Start ==="
echo "Date: $(date)"
echo "Node: $(hostname)"
echo "Grid: NY=897 x NZ=449 (Re=10595)"
echo "Poisson tol: 1e-12, max_iter: 50000"
echo ""

time python3 -u J_Frohlich/grid_zeta_tool.py --auto

EXIT_CODE=$?
echo ""
echo "=== Grid Generation Done ==="
echo "Exit code: $EXIT_CODE"
echo "Date: $(date)"

if [ $EXIT_CODE -eq 0 ]; then
    echo "Grid files generated:"
    ls -lh J_Frohlich/adaptive_*.dat 2>/dev/null
    ls -lh J_Frohlich/grid_data_*.txt 2>/dev/null
else
    echo "!! GRID GENERATION FAILED !!"
fi
