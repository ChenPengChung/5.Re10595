#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
#  Algorithm2 factorial cell builder — WORKTREE ONLY.
#  絕不在 live job 目錄 (Edit6_5600DNS) 執行；本腳本只產生 a.out.<cell>，
#  不投 job、不碰 restart/。
#
#  用法:  bash build_cell.sh <cell>
#    algo1    → -DUSE_GILBM_ALGORITHM2=0  (Algorithm1 baseline)
#    gilbm_b  → -DUSE_GILBM_ALGORITHM2=1  (GEN=GILBM_RK2, STORE=COORDS)
#    (gilbm_a / itb_b / itb_a 於後續 Stage 加入)
#
#  開關注入靠 variables.h 的 #ifndef 包裹 — 不改動任何 source 檔。
# ════════════════════════════════════════════════════════════════════════════
set -eo pipefail   # 不用 -u: hpcx-init.sh 引用未設變數 (HPCX_ENABLE_NCCLNET_PLUGIN)
cd "$(dirname "$0")"

if [[ "$(pwd -P)" == */Edit6_5600DNS ]]; then
    echo "[FATAL] refusing to build in the live job directory" >&2
    exit 1
fi

CELL="${1:-algo1}"
case "$CELL" in
  algo1)   DEFS="-DUSE_GILBM_ALGORITHM2=0" ;;
  gilbm_b)       DEFS="-DUSE_GILBM_ALGORITHM2=1 -DGILBM_ALGO2_STORE=0" ;;  # COORDS (predict r,s)
  gilbm_a)       DEFS="-DUSE_GILBM_ALGORITHM2=1 -DGILBM_ALGO2_STORE=1" ;;  # WEIGHTS raw (legacy consumer)
  gilbm_a_fold)  DEFS="-DUSE_GILBM_ALGORITHM2=1 -DGILBM_ALGO2_STORE=2" ;;  # WEIGHTS_FOLDED (ITB-style pure MAC, 對標 ITBLBM)
  *) echo "unknown cell: $CELL (expected: algo1 | gilbm_b | gilbm_a | gilbm_a_fold)" >&2; exit 1 ;;
esac

# ── H200 toolchain env (鏡像 chain_code/build_and_submit.sh.H200) ──
export HPCX_HOME=/opt/nvidia/hpc_sdk/Linux_x86_64/25.9/comm_libs/13.0/hpcx/hpcx-2.24
source "$HPCX_HOME/hpcx-init.sh"
hpcx_load
export MATH_LIBS=/opt/nvidia/hpc_sdk/Linux_x86_64/25.9/math_libs/13.0/targets/x86_64-linux
: "${CUDA_HOME:=/opt/nvidia/hpc_sdk/Linux_x86_64/25.9/cuda/13.0}"

echo "[build_cell] cell=$CELL  defs=$DEFS"
echo "[build_cell] nvcc: $(which nvcc 2>/dev/null || echo /opt/nvidia/hpc_sdk/Linux_x86_64/25.9/compilers/bin/nvcc)"

NVCC="$(which nvcc 2>/dev/null || echo /opt/nvidia/hpc_sdk/Linux_x86_64/25.9/compilers/bin/nvcc)"
"$NVCC" -arch=sm_90 -O3 $DEFS main.cu \
    -I"${CUDA_HOME}/include" \
    -I"${MATH_LIBS}/include" \
    -I"${HPCX_MPI_DIR}/include" \
    -L"${CUDA_HOME}/lib64" \
    -L"${MATH_LIBS}/lib" \
    -L"${HPCX_MPI_DIR}/lib" -lmpi \
    -lcufft \
    -o "a.out.$CELL"

file "a.out.$CELL"
echo "[build_cell] OK → a.out.$CELL"
