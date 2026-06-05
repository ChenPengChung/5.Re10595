#!/bin/bash
# ==============================================================================
# verify_seed.sh — 驗證 s0.95 種子 checkpoint: (1)完整性 (2)U* 一致 (3)F* 一致
# ------------------------------------------------------------------------------
# 在 Phase B 切換「之前」對 $STG 的種子驗證 (不合格就不換, 保護主目錄)。
# 用法: bash verify_seed.sh
# ==============================================================================
set -uo pipefail
STG=/home/s8313697/5.Re10595/Edit8_stg_s095
SEED="$STG/restart/checkpoint/step_00000001"
OLDMETA="/home/s8313697/5.Re10595/Edit8_NewInterpolation/phase2_generatecheckpoint/oldcheckpoint_Re10595_step_12550001/metadata.dat"
LOG=$(ls -t "$STG"/genseed_s095_*.log 2>/dev/null | head -1)
EXPECT_BYTES=34780200    # 455*21*455*8 (NX6*NYD6*NZ6 double)

echo "================================================================"
echo "[verify] 種子: $SEED"
echo "[verify] genseed log: $LOG"
echo "================================================================"

# ─── (1) 完整性 ───────────────────────────────────────────────
echo "── (1) 完整性 ──"
[ -d "$SEED" ] || { echo "  ✗ 種子目錄不存在 (genseed 未成功)"; exit 1; }
NFB=$(find "$SEED" -maxdepth 1 -name 'f[0-9]*_*.bin' | wc -l)
NRH=$(find "$SEED" -maxdepth 1 -name 'rho_*.bin' | wc -l)
echo "  f-files = $NFB (期望 1216),  rho = $NRH (期望 64),  metadata = $([ -s "$SEED/metadata.dat" ] && echo 有 || echo 缺)"
BADSIZE=$(find "$SEED" -maxdepth 1 -name '*.bin' ! -size ${EXPECT_BYTES}c | wc -l)
echo "  非 ${EXPECT_BYTES}B 的 .bin 數 = $BADSIZE (期望 0)"
[ "$NFB" -eq 1216 ] && [ "$NRH" -eq 64 ] && [ "$BADSIZE" -eq 0 ] && [ -s "$SEED/metadata.dat" ] \
  && echo "  ✓ 完整性 PASS" || echo "  ✗ 完整性 FAIL"
# 抽樣 rank 有限性 (rank 0/31/63 的 f00 + rho)
python3 - "$SEED" "$EXPECT_BYTES" <<'PY'
import sys,glob,numpy as np
seed,nb=sys.argv[1],int(sys.argv[2]); n=nb//8
bad=0
for r in (0,31,63):
    for f in sorted(glob.glob(f"{seed}/f00_*{r:04d}*.bin"))[:1]+sorted(glob.glob(f"{seed}/rho_*{r:04d}*.bin"))[:1]:
        a=np.fromfile(f,dtype='<f8',count=n)
        fin=np.isfinite(a).all();
        tag="f" if "f00" in f else "rho"
        print(f"  rank{r:2d} {tag}: finite={fin} min={a.min():.4e} max={a.max():.4e}"+("" if fin else "  ✗NaN/Inf"))
        if not fin: bad+=1
print("  ✓ 抽樣有限性 PASS" if bad==0 else f"  ✗ {bad} 檔有 NaN/Inf")
PY

# ─── (2) F* 一致 (force 全域純量, interp 直接複製) ─────────────
echo "── (2) F* 一致 (force) ──"
FN=$(grep -iE '^Force=' "$SEED/metadata.dat" 2>/dev/null | head -1)
FO=$(grep -iE '^Force=' "$OLDMETA" 2>/dev/null | head -1)
echo "  新種子: $FN"
echo "  舊源場: $FO"
[ "$FN" = "$FO" ] && echo "  ✓ F* 完全相同 (逐字元一致)" || echo "  ✗ F* 不同! (interp 應複製, 異常)"

# ─── (3) U* 一致 (interp 強制 Ub(NEW)=Ub(OLD); 場為重採樣) ─────
echo "── (3) U* 一致 (velocity) ──"
echo "  [interp 自報] Ub 修正 (應 NEW Ub after ≈ OLD Ub=0.015, residual~機器零):"
grep -nE "Ub correction|OLD Ub|NEW Ub|residual" "$LOG" 2>/dev/null | sed 's/^/    /'
echo "  [interp 自報] 守恆 / 正性 / 場統計:"
grep -nE "max \|Sigma|Σf|max\|.*sum\(f\)|interior rho|interior max\|u|min\(f\)|f_neq / f_eq|Conservation|conservation" "$LOG" 2>/dev/null | tail -8 | sed 's/^/    /'
echo "  [interp 自報] div gate (u* + f_neq, 2e-12):"
grep -nE "divergence gate|dilatational gate|max\|div" "$LOG" 2>/dev/null | tail -6 | sed 's/^/    /'
echo ""
echo "  說明: U* 體速度 Ub 由 interp 明確 scale 強制 = 源場 (逐位元級); 但 257x129→897x449"
echo "        不同網格 → 速度「場」是重採樣, 點對點本就不同 (保留: Ub精確/場形/守恆)。"
echo "================================================================"
echo "[verify] 三項全 PASS 才進 Phase B。F* 必須完全相同; U* 看 Ub residual~機器零 + 守恆~1e-16; 完整性 1216/64/size。"
echo "================================================================"
