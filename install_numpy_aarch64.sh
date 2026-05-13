#!/bin/bash
#SBATCH -p gb200-rack1
#SBATCH --account=MST114348
#SBATCH --gres=gpu:1
#SBATCH --time=00:10:00
#SBATCH --job-name=pip_aarch64
#SBATCH --output=/home/s8313697/pip_aarch64_%j.log

echo "Node: $(hostname), Arch: $(uname -m)"
target="$HOME/.local/lib/python3.9/site-packages-$(uname -m)"
mkdir -p "$target"
pip3 install --target="$target" numpy
echo "--- Verify ---"
PYTHONPATH="$target" python3 -c "import numpy; print('NumPy', numpy.__version__, numpy.__file__)"
echo "Done."
