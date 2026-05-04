#!/bin/bash
#SBATCH -p gb200-dev
#SBATCH --account=MST114348
#SBATCH --gres=gpu:1
#SBATCH --time=00:10:00
#SBATCH --job-name=pip_aarch64
#SBATCH --output=/home/s8313697/pip_aarch64_%j.log

set -euo pipefail

ARCH="$(uname -m)"
echo "Node: $(hostname), Arch: $ARCH, Python: $(python3 --version)"

if [ "$ARCH" != "aarch64" ]; then
    echo "ERROR: This script must run on aarch64 (GB200) node, got $ARCH"
    echo "Submit via: sbatch install_numpy_aarch64.sh"
    exit 1
fi

TARGET="$HOME/.local/lib/python3.9/site-packages-aarch64"
mkdir -p "$TARGET"

pip3 install --target="$TARGET" --upgrade numpy
pip3 install --target="$TARGET" --upgrade matplotlib

echo "--- Verify ---"
PYTHONPATH="$TARGET" python3 -c "
import numpy as np
print(f'NumPy  {np.__version__}  {np.__file__}')
a = np.array([1,2,3])
print(f'Test:  {a.sum()} == 6 -> {\"OK\" if a.sum()==6 else \"FAIL\"} ')
"

ARCH_CHECK=$(file "$TARGET"/numpy/_core/*.so 2>/dev/null | head -1 || true)
if echo "$ARCH_CHECK" | grep -q "aarch64"; then
    echo "Architecture check: PASS (aarch64 binary confirmed)"
else
    echo "WARNING: .so files may not be aarch64!"
    echo "$ARCH_CHECK"
fi

echo "Done. Target: $TARGET"
