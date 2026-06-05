#!/bin/bash
# ==============================================================================
# phaseB_full_swap.sh  v3 (codex round-2+3 全部 FAIL 已修) — 原子切換 chain s0.8→s0.95
# ------------------------------------------------------------------------------
# v3 修正 (codex round-3):
#   FAIL① 跨節點 dispatcher: 除 --kill-now + 同節點 kill -0, 再用「dispatcher.heartbeat
#         變 stale >65s」證死 (daemon 每 30s touch, submit_dispatcher.sh:887) — 跨登入節點安全。
#   FAIL② set -e/pipefail: B0 驗證段用 set +e 包住 (純驗證無破壞), 不會被空 glob rc2 中止。
#   FAIL③ .run.lock 不盲刪: 先 flock -n 測; 仍被持有則 kill stale holder 再測。HEAD.lockdir
#         僅在 80734+dispatcher 都證死後 rm。備份失敗改 fatal。
#   + B3: mv 前再查 dest 不存在; 換後驗主 checkpoint f==1216/rho==64/metadata。
#   + B3.5: 先確認無 64gpus 佇列 job (否則 run.sh:762 early-exit 0 假過) 再 --preflight-only。
#   + B4: 用 sacct 抓「本次新投」jobid 並確認確有新 job; 顯示用 squeue 一律 || true。
#   (round-2 已修: 強種子驗證 f1216/rho64、awk 安全重寫 provenance、備份+trap、fail-closed squeue)
#   硬保護: restart/LOCK_COMBO == '64 H200@64gpus' 全程絕不碰。
# 用法: bash phaseB_full_swap.sh [--apply]
# ==============================================================================
set -euo pipefail
MAIN=/home/s8313697/5.Re10595/Edit8_NewInterpolation
STG=/home/s8313697/5.Re10595/Edit8_stg_s095
CKPT="$MAIN/restart/checkpoint"
SEED="$STG/restart/checkpoint/step_00000001"
SEED_BIN="$STG/a.out.H200"
STG_PROV="$STG/restart/grid_provenance"
GRID95_SRC="$MAIN/grid_experiment_s095/J_Frohlich/adaptive_3.fine grid_I897_J449_s0.950000.dat"
SOLVER_GRID="$MAIN/J_Frohlich/adaptive_3.fine grid_I897_J449_s0.950000.dat"
NEW_GRID="$MAIN/phase1_generategrid/newgrid_3.fine grid_I897_J449_s0.950000.dat"
OLD_GRID="$MAIN/phase1_generategrid/oldgrid_I257_J129_g2.0_a0.5.dat"
ORIGIN="$MAIN/phase2_generatecheckpoint/oldcheckpoint_Re10595_step_12550001"
LOCK_EXPECT="64 H200@64gpus"
GENSEED_JOB=81451       # interp(種子)dev job; 重投後更新
APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1
cd "$MAIN"
say(){ echo "[B] $*"; }
die(){ echo "[B] FATAL: $*" >&2; exit 1; }
lock_ok(){ [ "$(cat restart/LOCK_COMBO 2>/dev/null || echo x)" = "$LOCK_EXPECT" ]; }
cnt(){ find "$1" -maxdepth 1 -name "$2" 2>/dev/null | wc -l; }   # 不因空 glob abort

echo "================================================================"
say "$([ "$APPLY" -eq 1 ] && echo '*** APPLY ***' || echo 'DRY-RUN')"
echo "================================================================"

# ── B0. 前置驗證 (set +e 包住純驗證, 不被 pipefail 中止) ──
say "B0 前置驗證 ----"
set +e
OK=1; w(){ say "  ✗ $*"; OK=0; }
lock_ok && say "  ✓ 🔒 LOCK_COMBO = '$(cat restart/LOCK_COMBO)'" || w "🔒 LOCK_COMBO 異常"
# genseed 完成判定: squeue 顯示 RUNNING → 未完成擋下; squeue 空(已離隊)→ 用 sacct 確認 COMPLETED
GS="$(squeue -j "$GENSEED_JOB" -h -o '%T' 2>/dev/null)"
if [ -n "$GS" ]; then
  w "genseed($GENSEED_JOB) 仍 $GS — 種子未完成"
else
  GSST="$(sacct -j "$GENSEED_JOB" -n -o State 2>/dev/null | head -1 | tr -d ' ')"
  case "$GSST" in
    COMPLETED) say "  ✓ genseed($GENSEED_JOB) COMPLETED" ;;
    "")        say "  ✓ genseed($GENSEED_JOB) 已離隊 (sacct 無紀錄, 以種子完整性為準)" ;;
    *)         w "genseed($GENSEED_JOB) sacct 狀態=$GSST (非 COMPLETED)" ;;
  esac
fi
if [ -d "$SEED" ]; then
  NFB=$(cnt "$SEED" 'f[0-9]*_*.bin'); NRH=$(cnt "$SEED" 'rho_*.bin')
  { [ "$NFB" -eq 1216 ] && [ "$NRH" -eq 64 ] && [ -s "$SEED/metadata.dat" ]; } \
    && say "  ✓ 種子完整 f=$NFB rho=$NRH meta=$(stat -c %s "$SEED/metadata.dat")B" \
    || w "種子不完整 f=$NFB(需1216) rho=$NRH(需64)"
else w "種子 $SEED 不存在"; fi
[ "$(cnt "$STG/restart/checkpoint" '*.WRITING')" -ne 0 ] && w "種子側有 .WRITING"
[ -s "$STG_PROV" ] && say "  ✓ \$STG/grid_provenance 已寫" || w "\$STG/grid_provenance 未寫"
[ -f "$SEED_BIN" ] && say "  ✓ s0.95 binary $(md5sum "$SEED_BIN" | cut -d' ' -f1)" || w "binary 缺"
[ -f "$GRID95_SRC" ] && say "  ✓ s0.95 grid 源在" || w "grid 源缺"
{ [ -e "$CKPT/step_00000001" ] || [ -L "$CKPT/step_00000001" ]; } && w "主目錄已有 step_00000001(撞名)" || say "  ✓ 主目錄無 step_00000001"
CJ="$(cat restart/chain_jobid 2>/dev/null)"
{ [ -n "$CJ" ] && [ -n "$(squeue -j "$CJ" -h -o '%T' 2>/dev/null)" ]; } && say "  ✓ chain head=$CJ (運行中, 動態讀 chain_jobid)" || w "chain head '$CJ' 非運行中 (slot 可能已空?)"
say "  · head $CJ=$(squeue -j "$CJ" -h -o '%T %M' 2>/dev/null || echo GONE) · dispatcher.pid=$(cat restart/dispatcher.pid 2>/dev/null) · 根DISPATCHER_ACTIVE=$([ -f DISPATCHER_ACTIVE ] && echo 在 || echo 無)"
set -e

if [ "$APPLY" -eq 0 ]; then
  echo "----"; say "DRY-RUN: 前置 $([ "$OK" -eq 1 ] && echo '全綠 ✓' || echo '未全綠 ✗ (interp 跑完才行)')"
  say "  B1 停dispatcher(--kill-now + heartbeat-stale跨節點證死) → B2 STOP_CHAIN停80734等死透"
  say "  → B3 備份+原子換 → B3.5 --preflight-only驗 → B4 投+驗新jobid → B5 daemon"
  echo "================================================================"; exit 0
fi
[ "$OK" -eq 1 ] || die "B0 未全綠 — 拒絕 --apply。"

BK="$MAIN/restart/phaseB_backup_$(date +%Y%m%d_%H%M%S)"
trap 'rc=$?; [ $rc -ne 0 ] && { echo "[B] !! 失敗 rc=$rc — 回滾料在 $BK + waste; 手動還原 variables.h/a.out.H200/grid_provenance/chain狀態←$BK, checkpoint←waste, 或修後重跑 B3.5/B4。"; }' EXIT

# ── B1. 徹底停 dispatcher (PID 快路徑 + heartbeat-stale 跨節點證死) ──
say "B1 停 dispatcher ----"
HB0=$(stat -c %Y restart/dispatcher.heartbeat 2>/dev/null || echo 0)
DPID="$(cat restart/dispatcher.pid 2>/dev/null || echo '')"
bash chain_code/dispatcher_stop.sh --kill-now || true
for i in $(seq 1 20); do { [ -n "$DPID" ] && kill -0 "$DPID" 2>/dev/null; } || break; sleep 1; done
say "  等 heartbeat 變 stale (>65s 不動 = dispatcher 真死, 跨節點安全; daemon 每30s touch)..."
sleep 70
NOW=$(date +%s); HB1=$(stat -c %Y restart/dispatcher.heartbeat 2>/dev/null || echo 0)
if [ -f restart/dispatcher.heartbeat ] && [ $((NOW - HB1)) -lt 65 ]; then
  die "dispatcher.heartbeat 仍新鮮 (age=$((NOW-HB1))s) → dispatcher 還活著(可能別節點) → 拒動(防雙投/丟slot)"
fi
[ -f DISPATCHER_ACTIVE ] && rm -f DISPATCHER_ACTIVE
rm -f restart/DISPATCHER_INTENT restart/dispatcher.heartbeat STOP_DISPATCHER 2>/dev/null || true
[ -f DISPATCHER_ACTIVE ] && die "根 DISPATCHER_ACTIVE 仍在 → B4 會被拒"
say "  ✓ dispatcher 死透 (PID 死 + heartbeat stale), 根 DISPATCHER_ACTIVE 已清"

# ── B2. 優雅停 chain head (動態讀 chain_jobid; 此後 slot 才釋) ──
# stop-chain 建 STOP_CHAIN = 整條 chain 停 (含自我續投/dispatcher), 不只單一 job;
# B1 已停 dispatcher, 故 head 號此刻穩定。等 chain_jobid 指的 head 退出即可。
JOB="$(cat restart/chain_jobid 2>/dev/null)"
[ -n "$JOB" ] || die "chain_jobid 空 — 無 head 可停 (slot 狀態異常, 人工檢查)"
say "B2 停 chain head=$JOB ----"
if [ -n "$(squeue -j "$JOB" -h -o '%T' 2>/dev/null || echo busy)" ]; then
  ./run job-guard stop-chain || die "stop-chain 失敗"
  say "  STOP_CHAIN 已建, 等 head $JOB 寫 final(廢)+退出..."
  for i in $(seq 1 200); do
    st="$(squeue -j "$JOB" -h -o '%T' 2>/dev/null)"; rc=$?
    [ $rc -ne 0 ] && { sleep 5; continue; }   # squeue 失敗 → fail-closed (不當已死)
    [ -z "$st" ] && break
    sleep 10
  done
fi
[ -n "$(squeue -j "$JOB" -h -o '%T' 2>/dev/null || echo busy)" ] && die "head $JOB 未確認退出"
for i in $(seq 1 30); do [ "$(cnt "$CKPT" '*.WRITING')" -eq 0 ] && break; sleep 2; done
[ "$(cnt "$CKPT" '*.WRITING')" -ne 0 ] && die "仍有 .WRITING"
say "  ✓ head $JOB 死透, checkpoint 凍結"

# ── B2.5 清除舊 s0.8 模擬輸出 (head 已停; 新 s0.95 不被舊資料汙染) ──
# 使用者補充需求 + 選 A(三大紀錄檔重置, 備份後清空)。只清 s0.8 輸出,
# 保留 tracked result/*.py + DNS result/*.dat + LOCK_COMBO + 原始碼。
say "B2.5 清除舊 s0.8 模擬輸出 ----"
mkdir -p "$BK/records_s08" || die "建紀錄備份目錄失敗"
for f in checkrho.dat Ustar_Force_record.dat timing_log.dat; do
  [ -f "$f" ] && { mv "$f" "$BK/records_s08/" || die "備份 $f 失敗"; say "  reset(備份→BK) $f"; }
done
# 大檔硬刪 (s0.8 VTK/bin ~225G, 太大無法備份, 已作廢; *Final.vtk 已被 *.vtk 涵蓋)
find result -maxdepth 1 -type f \( -name '*.vtk' -o -name '*.bin' \) -delete 2>/dev/null || true
find result -maxdepth 1 -type f \( -name 'monitor_convergence_*.png' -o -name 'monitor_convergence_*.pdf' \
     -o -name 'benchmark_*.png' -o -name 'benchmark_*.pdf' \
     -o -name 'tau_wall_*.png' -o -name 'tau_wall_*.pdf' \) -delete 2>/dev/null || true
rm -rf statistics/ 2>/dev/null || true
rm -f gilbm_metrics_full.dat meshX.DAT meshYZ.DAT nan_monitor_log.txt 2>/dev/null || true
rm -f slurm_*.log slurm_*.err 2>/dev/null || true
rm -f live/*.png live/*.pdf 2>/dev/null || true
# 保護斷言: 不可誤刪 tracked / LOCK
PY_N=$(cnt result '*.py'); DNS_N=$(cnt result '*.dat'); RES_LEFT=$(( $(cnt result '*.vtk') + $(cnt result '*.bin') ))
{ [ "$PY_N" -ge 1 ] && lock_ok; } || die "B2.5 保護觸發 (result/*.py=$PY_N 或 LOCK_COMBO 異常)"
say "  ✓ 清除 s0.8: result VTK/bin 殘留=$RES_LEFT(應0) + statistics + metrics + mesh + slurm log + live圖"
say "  ✓ 保留 result/*.py=$PY_N, DNS=$DNS_N; 三大紀錄檔重置(備份 $BK/records_s08); 🔒LOCK_COMBO 完好"

# ── B3. 備份(fatal) + 原子換配置 ──
say "B3 換配置 ----"
mkdir -p "$BK" || die "建備份目錄失敗"
cp -p "$MAIN/variables.h" "$BK/variables.h" || die "備份 variables.h 失敗"
cp -p "$MAIN/a.out.H200" "$BK/a.out.H200" || die "備份 a.out.H200 失敗"
cp -p restart/grid_provenance "$BK/grid_provenance" || die "備份 grid_provenance 失敗"
cp -p restart/chain_jobid "$BK/" 2>/dev/null || true; cp -p restart/chain_count "$BK/" 2>/dev/null || true
say "  ✓ 備份 → $BK"
# (a) checkpoint: s0.8 廢移開 → 放 s0.95 (mv 前再查 dest, 換後驗 f/rho/metadata)
TS=$(date +%Y%m%d_%H%M%S); WASTE="$MAIN/restart/checkpoint_s08_waste_$TS"; mkdir -p "$WASTE" || die "建 waste 失敗"
shopt -s nullglob; n=0
for d in "$CKPT"/step_*; do [ "$(basename "$d")" = step_00000001 ] && continue; mv "$d" "$WASTE/" || die "移 $d 失敗"; n=$((n+1)); done
shopt -u nullglob
[ -L "$CKPT/latest" ] && rm -f "$CKPT/latest"
{ [ -e "$CKPT/step_00000001" ] || [ -L "$CKPT/step_00000001" ]; } && die "mv 前 dest 又出現 — 中止"
mv "$SEED" "$CKPT/step_00000001" || die "放 s0.95 種子失敗"
FNF=$(cnt "$CKPT/step_00000001" 'f[0-9]*_*.bin'); FRH=$(cnt "$CKPT/step_00000001" 'rho_*.bin')
{ [ "$FNF" -eq 1216 ] && [ "$FRH" -eq 64 ] && [ -s "$CKPT/step_00000001/metadata.dat" ]; } || die "換後種子不完整 f=$FNF rho=$FRH"
RN=$(ls -d "$CKPT"/step_* 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ')
[ "$(echo $RN)" = "step_00000001" ] || die "checkpoint 殘留非預期: '$RN'"
say "  ✓ checkpoint: 移開 $n 個 s0.8 → 只剩完整 step_00000001 (f=$FNF rho=$FRH)"
# (b) binary
cp -f "$SEED_BIN" "$MAIN/a.out.H200" || die "換 a.out.H200 失敗"; cp -f "$MAIN/a.out.H200" "$MAIN/a.out" || die
[ "$(md5sum "$MAIN/a.out.H200" | cut -d' ' -f1)" = "$(md5sum "$SEED_BIN" | cut -d' ' -f1)" ] || die "binary md5 不符"
say "  ✓ binary s0.95 ($(md5sum "$MAIN/a.out.H200" | cut -d' ' -f1))"
# (c) grid + 雜湊一致
cp -f "$GRID95_SRC" "$SOLVER_GRID" || die; cp -f "$GRID95_SRC" "$NEW_GRID" || die
[ "$(sha256sum "$NEW_GRID" | cut -d' ' -f1)" = "$(sha256sum "$SOLVER_GRID" | cut -d' ' -f1)" ] || die "NEW≠SOLVER grid 雜湊"
say "  ✓ s0.95 grid 就位 (NEW==SOLVER)"
# (d) variables.h
sed -i -E 's/(#define[[:space:]]+STRETCH_A[[:space:]]+)0\.80/\10.95/' "$MAIN/variables.h"
[ "$(grep -oE 'STRETCH_A[[:space:]]+[0-9.]+' "$MAIN/variables.h" | head -1 | grep -oE '[0-9.]+$')" = "0.95" ] || die "STRETCH_A 未變"
say "  ✓ variables.h STRETCH_A=0.95"
# (e) grid_provenance awk 安全重寫
TMP=restart/grid_provenance.new; cp "$STG_PROV" "$TMP" || die "讀 \$STG provenance 失敗"
setkv(){ awk -v k="$1" -v v="$2" '$0 ~ "^"k"=" {print k"="v; d=1; next} {print} END{if(!d)print k"="v}' "$TMP" > "$TMP.x" && mv "$TMP.x" "$TMP"; }
setkv new_grid "$NEW_GRID";       setkv new_grid_mtime "$(stat -c %Y "$NEW_GRID")"
setkv old_grid "$OLD_GRID";       setkv old_grid_mtime "$(stat -c %Y "$OLD_GRID")"
setkv solver_grid "$SOLVER_GRID"; setkv solver_grid_mtime "$(stat -c %Y "$SOLVER_GRID")"
setkv variables_h "$MAIN/variables.h"; setkv variables_h_mtime "$(stat -c %Y "$MAIN/variables.h")"
setkv origin "$ORIGIN";           setkv origin_metadata_mtime "$(stat -c %Y "$ORIGIN/metadata.dat")"
setkv solver_grid_match 1
mv "$TMP" restart/grid_provenance || die "寫 grid_provenance 失敗"
say "  ✓ grid_provenance 重寫 (全欄主目錄 + 重 stat)"
# (f) 重置 chain 狀態 (不碰 LOCK_COMBO); .run.lock 不盲刪 — flock 測 + 清 stale holder
lock_ok || die "LOCK_COMBO 重置前異常"
rm -f restart/chain_count restart/chain_jobid restart/STOP_CHAIN restart/STOP_DISPATCHER 2>/dev/null || true
rm -rf restart/HEAD.lockdir    # 安全: 80734+dispatcher 已證死
if [ -e .run.lock ]; then
  if ! flock -n .run.lock true 2>/dev/null; then
    H=$(lsof -t .run.lock 2>/dev/null | head -1)
    [ -n "$H" ] && ps -p "$H" -o comm= 2>/dev/null | grep -q nan_monitor && { kill "$H" 2>/dev/null || true; sleep 1; }
  fi
  flock -n .run.lock true 2>/dev/null && rm -f .run.lock || say "  ⚠ .run.lock 仍被持有 (留檔; ./run 會新建獨立鎖)"
fi
lock_ok && say "  ✓ chain 狀態重置; 🔒 LOCK_COMBO='$(cat restart/LOCK_COMBO)'" || die "LOCK_COMBO 重置後消失!"

# ── B3.5 preflight-only 驗 (先確認無 64gpus 佇列 job 防 run.sh:762 假過) ──
say "B3.5 preflight-only ----"
[ "$(squeue -u "$USER" -h -o '%P' 2>/dev/null | grep -c 64gpus)" -eq 0 ] || die "尚有 64gpus 佇列 job → --preflight-only 會 early-exit 假過; 中止"
RUNSH_DISPATCHER_BYPASS=0 ./run --preflight-only --no-queue-check || die "preflight-only 未過 (provenance/grid) — 修 provenance 後重跑 B3.5/B4"
say "  ✓ preflight-only 通過"

# ── B4. warm 投遞 + 確認真有新 job ──
say "B4 warm 投遞 ----"
lock_ok || die "LOCK_COMBO 投遞前異常"
[ -f DISPATCHER_ACTIVE ] && die "DISPATCHER_ACTIVE 又出現"
PRE_JOBS="$(squeue -u "$USER" -h -o '%i' 2>/dev/null | sort)"
./run --no-queue-check || die "投遞失敗 (看 Preflight C / lock)"
sleep 3
NEWJID="$(comm -13 <(echo "$PRE_JOBS") <(squeue -u "$USER" -h -o '%i' 2>/dev/null | sort) | head -1)"
[ -n "$NEWJID" ] || die "投遞後找不到新 job id — 可能未實際投出, 立即人工檢查 (slot 風險!)"
say "  ✓ 新 job=$NEWJID"

# ── B5. 重啟 daemon ──
say "B5 重啟 daemon ----"
./run dispatcher start || true
bash watcher/hill_watcher_start.sh || true

trap - EXIT
echo "================================================================"
say "完成。checkpoint=$(ls -d "$CKPT"/step_* 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ') | 🔒LOCK='$(cat restart/LOCK_COMBO)' | STRETCH_A=$(grep -oE 'STRETCH_A[[:space:]]+[0-9.]+' variables.h|head -1|grep -oE '[0-9.]+$') | 新job=$NEWJID"
say "  廢料 $WASTE + 備份 $BK — 新job warm三閘門過後再 rm 回收"
say "  → 看 slurm_${NEWJID}.log: Restart from step_00000001 / [G6] grid=match / [Phase5] dt consistent"
echo "================================================================"
