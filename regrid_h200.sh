#!/bin/bash
# ==============================================================================
# regrid_h200.sh — Checkpoint regrid interpolation on the hidden h200 partition
# ==============================================================================
# 全部參數從 variables.h 自動推導，不寫死任何數值。
# 網格已生成完畢 (J_Frohlich/adaptive_*.dat 已存在) — 本腳本「不」重新生成網格，
# 只做 checkpoint 插值 (interp_checkpoint.py)。
#
# h200 partition: 4 天上限, QoS=normal (無 64-GPU 限制), 最少 1 GPU。
# 記憶體: 4 GPU + --mem=0 (全節點 ~2TB) 確保不再 OOM。
# 插值: 預設用向量化快版 (Codex 已驗證正確, ~30-100x); INTERP_SCALAR=1 退回純量。
# ==============================================================================
#SBATCH --job-name=interp_10595
#SBATCH --account=MST114348
#SBATCH --partition=h200
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --gpus-per-node=4
#SBATCH --cpus-per-task=48
#SBATCH --mem=0
#SBATCH --time=06:00:00
#SBATCH --output=regrid_pipeline.log
#SBATCH --error=regrid_pipeline.log

set -eo pipefail
cd /home/s8313697/5.Re10595/Edit7_10595SNS

echo "=== Regrid interpolation started: $(date) ==="
echo "Node: $(hostname), Mem: $(free -h | awk '/Mem/{print $2}'), CPUs: ${SLURM_CPUS_PER_TASK:-?}"

# ── 從 variables.h 自動推導 NEW grid 參數 (不寫死) ───────────────────────────
VH=variables.h
SA=$(awk '/^#define[ \t]+STRETCH_A/{print $3}'   "$VH")
NX=$(awk '/^#define[ \t]+NX[ \t]/{print $3}'      "$VH")
NY=$(awk '/^#define[ \t]+NY[ \t]/{print $3}'      "$VH")
NZ=$(awk '/^#define[ \t]+NZ[ \t]/{print $3}'      "$VH")
JP=$(awk '/^#define[ \t]+jp[ \t]/{print $3}'      "$VH")
echo "[vars.h] STRETCH_A=$SA  NX=$NX NY=$NY NZ=$NZ jp=$JP"

# ── 自動探測 NEW / OLD grid 與 origin checkpoint ────────────────────────────
NEW_GRID=$(ls phase1_generategrid/newgrid_*_s${SA}.dat 2>/dev/null | head -1)
OLD_GRID=$(ls phase1_generategrid/oldgrid_*.dat        2>/dev/null | head -1)
OLD_DIR=$(ls -d phase2_generatecheckpoint/oldcheckpoint_*/ 2>/dev/null | head -1)
OLD_DIR=${OLD_DIR%/}

if [ -z "$NEW_GRID" ] || [ -z "$OLD_GRID" ] || [ -z "$OLD_DIR" ]; then
    echo "[FATAL] 缺少輸入: NEW='$NEW_GRID' OLD='$OLD_GRID' ORIGIN='$OLD_DIR'"
    exit 1
fi

# ── 從 OLD grid 檔名推導 old-gamma (不寫死 3.663562) ─────────────────────────
OLD_SA=$(echo "$OLD_GRID" | grep -oP '_s\K[0-9.]+(?=\.dat)')
OLD_GAMMA=$(awk -v a="$OLD_SA" 'BEGIN{printf "%.6f", log((1+a)/(1-a))}')

echo "[grid] NEW    = $NEW_GRID  (s=$SA)"
echo "[grid] OLD    = $OLD_GRID  (s=$OLD_SA -> gamma=$OLD_GAMMA)"
echo "[grid] ORIGIN = $OLD_DIR"

# ── solver grid 必須已存在 (網格已生成完畢, 本腳本不重新生成) ──────────────
SOLVER_GRID="J_Frohlich/adaptive_3.fine grid_I${NY}_J${NZ}_s${SA}.dat"
if [ ! -f "$SOLVER_GRID" ]; then
    echo "[FATAL] solver grid 不存在: $SOLVER_GRID"
    echo "        (網格應已生成; 本腳本不負責生成網格)"
    exit 1
fi
echo "[grid] solver = $SOLVER_GRID ✓ (不重新生成)"

# ── 清理任何殘留的 .WRITING 暫存 ───────────────────────────────────────────
rm -rf restart/checkpoint/step_00000001.WRITING restart/grid_provenance.WRITING 2>/dev/null || true

# ── 執行 checkpoint 插值 (NEW 側 NX/NY/NZ/jp/STRETCH_A 由 --variables-h 讀取) ─
python3 phase2_generatecheckpoint/interp_checkpoint.py --auto --step 1 \
    --old-dir       "$OLD_DIR" \
    --variables-h   "$VH" \
    --old-grid-dat  "$OLD_GRID" \
    --new-grid-dat  "$NEW_GRID" \
    --old-gamma     "$OLD_GAMMA" \
    --old-alpha     0.5

RC=$?
echo "=== Regrid interpolation finished: $(date), exit=$RC ==="

if [ $RC -eq 0 ]; then
    CK=restart/checkpoint/step_00000001
    echo "=== Checkpoint 檔案數: $(ls "$CK" 2>/dev/null | wc -l)  (期望 19*${JP}+${JP}+1 = $((19*JP+JP+1))) ==="
    echo "--- gate provenance (metadata.dat) ---"
    grep -E 'interp_u_star_div_gate_passed|interp_u_star_div_gate_tol|interp_final_div_max|interp_final_div_rms|interp_proj_div_max_before|interp_proj_div_max_after' "$CK/metadata.dat" 2>/dev/null
    echo "--- grid_provenance ---"
    cat restart/grid_provenance 2>/dev/null | head -20
fi
