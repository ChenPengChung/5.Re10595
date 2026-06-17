#!/bin/bash
# ==============================================================================
# build_local.sh — 本地 CFDLab V100 (sm_70) 編譯
# ------------------------------------------------------------------------------
# 取代 NCHC 的 chain_code_nchc/build_and_submit.sh.H200(那是 sm_90 + HPC-SDK CUDA13)。
# 本地改用:
#   CUDA : /usr/local/cuda-12.4   (含 nvcc + cuFFT)
#   MPI  : /opt/openmpi-3.1.4
#   arch : sm_70  ← 只編 V100;物理上跑不了 P100(sm_60),等於一道「絕不誤跑 P100」保險。
#
# 用法:  bash chain_code_local/build_local.sh   (在專案根目錄執行)
# 產物:  ./a.out  (本地 V100 binary, 落在專案根) + build_local.log
# ==============================================================================
set -eo pipefail
# 本腳本位於 <專案根>/chain_code_local/;cd 到上一層 = 專案根,a.out 才會落在根目錄
# (供 hill_local_chain.sh 的 ./a.out 取用),而非 chain_code_local/ 內。
cd "$(dirname "$(readlink -f "$0")")/.."

CUDA_HOME=/usr/local/cuda-12.4
MPI_HOME=/opt/openmpi-3.1.4
export PATH="$CUDA_HOME/bin:$MPI_HOME/bin:$PATH"

echo "=== 本地 V100 編譯 (sm_70) @ $(hostname) $(date '+%F %T') ==="
echo "nvcc  : $(command -v nvcc)"
nvcc --version | tail -1
echo "mpi   : $MPI_HOME  ($(command -v mpirun))"
echo

# -DANIM_ENABLE=0 : 關掉每次 VTK 後 system("python3 pipeline.py") 的動畫渲染(測試不需要)
nvcc -arch=sm_70 -O3 main.cu \
    -DANIM_ENABLE=0 \
    -I"$CUDA_HOME/include" \
    -I"$MPI_HOME/include" \
    -L"$CUDA_HOME/lib64" \
    -L"$MPI_HOME/lib" -lmpi \
    -lcufft \
    -o a.out

echo
echo "[OK] 編譯成功"
file a.out
