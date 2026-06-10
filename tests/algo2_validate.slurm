#!/usr/bin/env bash
#SBATCH --job-name=A2val_wt
#SBATCH --account=MST115169
#SBATCH --partition=16gpus
#SBATCH --nodes=4
#SBATCH --ntasks-per-node=8
#SBATCH --gres=gpu:8
#SBATCH --cpus-per-task=1
#SBATCH --time=00:30:00
#SBATCH --output=slurm_a2val_%j.log
#SBATCH --error=slurm_a2val_%j.err
# ════════════════════════════════════════════════════════════════════════════
#  Algorithm2 一次性驗證 run（worktree 專用 — 與 live job 完全隔離）
#  - 無 chain 自投、無 dispatcher、不碰 Edit6_5600DNS live 目錄
#  - 預先 touch restart/STOP_CHAIN → solver ~100 步即優雅停機 (init 驗證閘 + 短跑)
#  - 驗收三點 (log):
#      1. "GILBM Algorithm2: coords table built on DEVICE + validated"
#      2. "[ALGO2][rank N] coords validation: ... bitwise mismatch = 0 ... [BIT-EXACT OK] ... [MAP OK]"  ×32 ranks
#      3. warm-load 三閘門: Restart from step_58700001 / [G6] Schema OK / dt_global consistent
#         + ~100 步無 NaN、Re/Ub 接續
#  用法 (由使用者讓出卡位後):  cd <worktree> && sbatch tests/algo2_validate.slurm [cell]
#    cell 預設 gilbm_b; 可帶 algo1 跑 baseline 對照
# ════════════════════════════════════════════════════════════════════════════
set -eo pipefail
cd "$SLURM_SUBMIT_DIR"

if [[ "$(pwd -P)" == */Edit6_5600DNS ]]; then
    echo "[FATAL] refusing to run in the live job directory" >&2
    exit 1
fi

CELL="${1:-gilbm_b}"
BIN="./a.out.$CELL"
[ -x "$BIN" ] || { echo "[FATAL] $BIN not found — run build_cell.sh $CELL first" >&2; exit 1; }

# bounded run: 哨兵先放好 → solver 第一個 ~100 步檢查點就優雅停機 + 寫 final checkpoint
mkdir -p restart
touch restart/STOP_CHAIN

# env (鏡像 chain jobscript)
export HPCX_HOME=/opt/nvidia/hpc_sdk/Linux_x86_64/25.9/comm_libs/13.0/hpcx/hpcx-2.24
set +u; source "$HPCX_HOME/hpcx-init.sh"; hpcx_load; set -u 2>/dev/null || true

# ★ 相對路徑 (solver main.cu:309 對非絕對路徑加 ./ 前綴; 絕對路徑會變 .//home/... 開不了)
#   對照 live chain: ARGS="--restart=restart/checkpoint/step_<latest>" (jobscript_chain.slurm:393)
LATEST_DIR="restart/checkpoint/$(readlink restart/checkpoint/latest)"
[ -d "$LATEST_DIR" ] || { echo "[FATAL] no checkpoint at $LATEST_DIR" >&2; exit 1; }
echo "[A2val] cell=$CELL  restart=$LATEST_DIR  np=$SLURM_NTASKS"

# mpirun 不受 set -e 影響: 失敗(coords gate MPI_Abort(92)/NaN/FATAL — 正是本 job 要抓的)
# 時仍要跑到下方 summary grep + exit $RC
set +e
mpirun -np "$SLURM_NTASKS" --map-by ppr:8:node --bind-to none \
       stdbuf -oL "$BIN" --restart="$LATEST_DIR"
RC=$?
set -e
echo "[A2val] mpirun RC=$RC"

echo "[A2val] ===== validation summary ====="
grep -E 'Algorithm2: coords table|coords validation|BIT-EXACT|MAP OK|Restart from|Schema OK|dt_global|NaN|FATAL' \
    "slurm_a2val_${SLURM_JOB_ID}.log" 2>/dev/null | head -45 || true
exit $RC
