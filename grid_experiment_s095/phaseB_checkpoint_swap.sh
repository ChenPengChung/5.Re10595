#!/bin/bash
# ==============================================================================
# phaseB_checkpoint_swap.sh — 安全刪除 80734 廢 s0.8 checkpoint + 放入 s0.95 step_00000001
# ------------------------------------------------------------------------------
# 這是 Phase B「換 checkpoint」核心步驟。設計目標 (使用者要求):
#   (1) 枚舉並分類 restart/checkpoint/ 內每個 step_*, 只刪 80734 廢 s0.8、保留/放入 s0.95。
#   (2) partition@jp 鎖 restart/LOCK_COMBO 與其他狀態檔「絕不刪」(白名單保護 + 硬性斷言)。
#   (3) 可逆: 廢資料「移開」(rename) 而非直接 rm, s0.95 確認跑起來後才回收。
#   (4) 預設 DRY-RUN, 只印分類與計畫; 加 --apply 才真的動。
# 用法: bash phaseB_checkpoint_swap.sh            # DRY-RUN (只看分類)
#       bash phaseB_checkpoint_swap.sh --apply    # 真的執行 (需 80734 已死透)
# ==============================================================================
set -uo pipefail
MAIN=/home/s8313697/5.Re10595/Edit8_NewInterpolation
STG=/home/s8313697/5.Re10595/Edit8_stg_s095
CKPT="$MAIN/restart/checkpoint"
SRC="$STG/restart/checkpoint/step_00000001"     # interp 產出的 s0.95 種子
LOCK_EXPECT="64 H200@64gpus"
APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1
cd "$MAIN" || exit 1

echo "================================================================"
echo "[swap] $([ "$APPLY" -eq 1 ] && echo '*** APPLY 模式 (會真的動) ***' || echo 'DRY-RUN (不動任何東西)')"
echo "================================================================"

# ── 0. 受保護檔案 (白名單, 絕不刪/不動) — 先印出來確認都在 ──
echo "--- [PROTECT] 受保護, 腳本絕不碰 ---"
for p in LOCK_COMBO grid_provenance h200_partition gb200_partition \
         dispatcher.log chain.log blacklist.log MANIFEST.txt SUMMARY.md; do
  [ -e "restart/$p" ] && echo "    🔒 restart/$p"
done
for d in summary history_backup_*; do [ -d "restart/$d" ] && echo "    🔒 restart/$d/"; done

# ── 0b. 硬性保護: LOCK_COMBO 必須存在且 == 鎖定值, 否則中止 (防任何誤刪/漂移) ──
LOCK_NOW="$(cat restart/LOCK_COMBO 2>/dev/null || echo '<缺>')"
if [ "$LOCK_NOW" != "$LOCK_EXPECT" ]; then
  echo "[swap] FATAL: restart/LOCK_COMBO = '$LOCK_NOW' ≠ 預期 '$LOCK_EXPECT'"
  echo "       → 中止, 絕不在 partition@jp 鎖異常時動 checkpoint。"
  exit 1
fi
echo "    ✓ LOCK_COMBO 校驗通過 = '$LOCK_NOW' (partition@jp 鎖完好)"

# ── 1. 前置安全閘 (僅 --apply 硬擋; DRY-RUN 只警告) ──
GATE_FAIL=0
J80="$(squeue -j 80734 -h -o '%T' 2>/dev/null)"
if [ -n "$J80" ]; then
  echo "[swap] ⚠ 80734 仍在佇列 (state=$J80) → 換 checkpoint 前必須先 stop-chain 等它死透 (避免邊刪邊被寫)"
  GATE_FAIL=1
fi
if ls "$CKPT"/*.WRITING 2>/dev/null | grep -q .; then
  echo "[swap] ⚠ 偵測到 .WRITING (寫入中) → 等寫完"
  GATE_FAIL=1
fi

# ── 2. 枚舉 + 分類 restart/checkpoint/ ──
echo "--- checkpoint 分類 ---"
DEL=()
shopt -s nullglob
for d in "$CKPT"/step_*; do
  name=$(basename "$d")
  if [ "$name" = "step_00000001" ]; then
    echo "    [KEEP]   $name  ← s0.95 種子 (絕不刪)"
  else
    echo "    [DELETE] $name  ← 80734 廢 s0.8"
    DEL+=( "$d" )
  fi
done
shopt -u nullglob
[ -L "$CKPT/latest" ] && echo "    [RESET]  latest → $(readlink "$CKPT/latest")  (s0.8 指標, 將移除)"
echo "    小計: 待刪 ${#DEL[@]} 個 s0.8 廢 checkpoint"

# ── 3. s0.95 種子就緒檢查 ──
echo "--- s0.95 種子 (\$STG) 就緒檢查 ---"
if [ -d "$SRC" ]; then
  NF=$(ls "$SRC"/*.bin 2>/dev/null | wc -l)
  HASMETA=$([ -f "$SRC/metadata.dat" ] && echo yes || echo NO)
  echo "    $SRC : $NF .bin, metadata=$HASMETA"
  if [ "$NF" -lt 1216 ] || [ "$HASMETA" != yes ]; then
    echo "    ⚠ 種子未就緒 (應 ≥1216 .bin + metadata) → interp 可能未完成"; GATE_FAIL=1
  fi
else
  echo "    ⚠ $SRC 不存在 → interp (genseed) 尚未產出, 換 checkpoint 還不能做"; GATE_FAIL=1
fi

# ── DRY-RUN 或閘失敗 → 止步 ──
if [ "$APPLY" -eq 0 ]; then
  echo "================================================================"
  echo "[swap] DRY-RUN 結束。確認分類無誤 + 種子就緒 + 80734 已死後, 再 --apply。"
  echo "       --apply 將: 移開 ${#DEL[@]} 個 s0.8(可逆) → 原子 mv 放入 s0.95 step_00000001"
  echo "       LOCK_COMBO / grid_provenance / 日誌 一律不動。"
  echo "================================================================"
  exit 0
fi
if [ "$GATE_FAIL" -ne 0 ]; then
  echo "[swap] FATAL: 安全閘未過 (80734 未死 / .WRITING / 種子未就緒) → 拒絕 --apply。"
  exit 1
fi

# ── 4. 執行 (--apply): 廢資料「移開」(可逆) → 放入 s0.95 ──
TS=$(date +%Y%m%d_%H%M%S)
WASTE="$MAIN/restart/checkpoint_s08_waste_$TS"
echo "--- 移開 80734 廢 s0.8 (整批 rename, 可逆) → $WASTE ---"
mkdir -p "$WASTE"
for d in "${DEL[@]}"; do mv "$d" "$WASTE/" && echo "    moved $(basename "$d")"; done
[ -L "$CKPT/latest" ] && rm -f "$CKPT/latest" && echo "    removed latest 指標"
echo "--- 原子放入 s0.95 step_00000001 (同 /home, rename 瞬間) ---"
if [ -e "$CKPT/step_00000001" ]; then echo "    FATAL: 目標 step_00000001 已存在!"; exit 1; fi
mv "$SRC" "$CKPT/step_00000001" && echo "    placed step_00000001"

# ── 5. 驗證 ──
echo "--- 驗證 ---"
echo "    checkpoint/ 現有 step_*: $(ls -d "$CKPT"/step_* 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ')  (應只有 step_00000001)"
echo "    step_00000001 .bin 數: $(ls "$CKPT"/step_00000001/*.bin 2>/dev/null | wc -l)"
echo "    🔒 LOCK_COMBO 仍 = '$(cat restart/LOCK_COMBO 2>/dev/null)' (應完好未動)"
echo "    🔒 grid_provenance 仍在: $([ -f restart/grid_provenance ] && echo yes)"
echo "    廢資料保存於 $WASTE — s0.95 確認 warm-start 成功後, 手動 rm -rf 回收 (~200GB)"
echo "================================================================"
echo "[swap] 完成。注意: 本腳本只換 checkpoint; Phase B 還需 (binary/grid/variables.h/grid_provenance 改寫/投遞) — 由完整 B 腳本接續。"
