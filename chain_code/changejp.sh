#!/bin/bash
# ==============================================================================
# changejp.sh — 通用 GPU 數 (jp) 切換 + 保流場續跑 + 重新投遞
# ------------------------------------------------------------------------------
# 把本 GILBM chain 從「當前 jp」切換到「目標 jp」, 完全保留現有流場 (checkpoint),
# 不冷啟動, 並維持既有 chain/dispatcher/watcher 流程, 只改變平行規模。
#
# 任意有效 jp 皆可 (16/32/64 … 只要通過下方驗證), 不限 4 的次方。
# (本專案 NCHC 政策自由切換集 = {16,32,64}; 暫時鎖定 jp=16。)
#
# 用法:
#   bash changejp.sh <NEW_JP>            # DRY-RUN: 只驗證 + 印出完整計畫, 不改任何東西
#   bash changejp.sh <NEW_JP> --apply    # 真的執行 (含 job-guard scancel + 重編重投)
#   bash changejp.sh <NEW_JP> --apply --allow-running   # 允許在 job RUNNING 時切 (會丟最後 checkpoint 後的進度)
#
# 安全保證:
#   * 取消 job 一律走 ./run job-guard scancel (驗 WorkDir, 受 hook 保護, 絕不誤殺別專案)
#   * checkpoint 用 repartition_jp.py 純資料重排 (無插值) → 流場一位元不差
#   * 原 checkpoint 會備份為 *_jp<OLD>_bak, 可回退
#   * accu_count>0 (已累積統計) 預設「點對點搬移全部 36 個累加器 + accu_count + cv」, 統計一位元不差;
#     repartition 會硬性要求 36 個累加器全齊且每個 rank 檔齊全, 否則 abort (絕不靜默漏搬);
#     僅 --force-stats-loss 才會刻意丟棄統計並 reset accu_count=0
#   * 改 variables.h 後同步更新 grid_provenance mtime, 讓 run.sh Preflight C 放行
# ==============================================================================
set -euo pipefail

# ---- 0. 解析參數 --------------------------------------------------------------
NEW_JP="${1:-}"
APPLY=0; ALLOW_RUNNING=0; FORCE_STATS_LOSS=0; PREPARE_ONLY=0
shift || true
for a in "$@"; do
  case "$a" in
    --apply)            APPLY=1 ;;
    --prepare-only)     PREPARE_ONLY=1; APPLY=1 ;;   # build+repartition, 不投遞 (供 dispatcher 呼叫)
    --allow-running)    ALLOW_RUNNING=1 ;;
    --force-stats-loss) FORCE_STATS_LOSS=1 ;;
    *) echo "[changejp] 未知參數: $a" >&2; exit 2 ;;
  esac
done

if ! [[ "$NEW_JP" =~ ^[0-9]+$ ]] || [ "$NEW_JP" -lt 1 ]; then
  echo "用法: bash changejp.sh <NEW_JP> [--apply] [--allow-running] [--force-stats-loss]" >&2
  exit 2
fi

# 本腳本位於 chain_code/; PROJECT_ROOT 是其上層, 所有相對路徑以 PROJECT_ROOT 為基準。
CHAIN_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$CHAIN_DIR/.." && pwd)"
cd "$PROJECT_ROOT"
VH="variables.h"
JS_H200="chain_code/jobscript_chain.slurm.H200"
JS_GB200="chain_code/jobscript_chain.slurm.GB200"
PROV="restart/grid_provenance"
REPART="chain_code/repartition_jp.py"

say()  { printf '%s\n' "$*"; }
hr()   { printf -- '----------------------------------------------------------------------\n'; }
die()  { printf '[changejp][FATAL] %s\n' "$*" >&2; exit 1; }

[ -f "$VH" ]      || die "找不到 $VH (請在專案根目錄執行)"
[ -f "$REPART" ]  || die "找不到 $REPART (本流程需要 same-grid repartition 工具)"
[ -f "$JS_H200" ] || die "找不到 $JS_H200"

# ---- 1. 讀當前狀態 ------------------------------------------------------------
CUR_JP=$(grep -E '^#define[[:space:]]+jp[[:space:]]+[0-9]+' "$VH" | head -1 | grep -oE '[0-9]+' | head -1)
NY=$(grep -E '^#define[[:space:]]+NY[[:space:]]+[0-9]+'  "$VH" | head -1 | grep -oE '[0-9]+' | head -1)
NX=$(grep -E '^#define[[:space:]]+NX[[:space:]]+[0-9]+'  "$VH" | head -1 | grep -oE '[0-9]+' | head -1)
NZ=$(grep -E '^#define[[:space:]]+NZ[[:space:]]+[0-9]+'  "$VH" | head -1 | grep -oE '[0-9]+' | head -1)
[ -n "${CUR_JP:-}" ] && [ -n "${NY:-}" ] || die "無法從 $VH 解析 jp / NY"
NYm1=$((NY-1))

# ---- 2. 驗證目標 jp (硬性 + 資訊) --------------------------------------------
say "=== changejp 0. 驗證 ==="
say "  目前: jp=$CUR_JP   網格 NX=$NX NY=$NY NZ=$NZ   (被分割軸 NY-1=$NYm1)"
say "  目標: jp=$NEW_JP"
hr

ERRS=0; WARN=0
# (a) 整除 (variables.h:104 編譯期 #error)
if [ $(( NYm1 % NEW_JP )) -ne 0 ]; then
  say "  [✗] (NY-1)=$NYm1 不能被 jp=$NEW_JP 整除 → 會觸發 variables.h 編譯期 #error"; ERRS=1
else
  CHUNK=$(( NYm1 / NEW_JP ))
  say "  [✓] 整除: (NY-1)/jp = $NYm1/$NEW_JP = $CHUNK cells/slab"
fi
# (b) kernel slab 下限 (evolution.h:762 隱含 (NY-1)/jp>=7)
if [ "${CHUNK:-0}" -lt 7 ]; then
  say "  [✗] slab=${CHUNK:-?} < 7 → 內部 kernel 列數 = slab-7 < 0, 不可用"; ERRS=1
elif [ "${CHUNK:-0}" -eq 7 ]; then
  say "  [!] slab=7 → 內部 kernel 列數=0 (零裕度, 已達 slab 下限邊緣); 可跑但無餘裕"; WARN=1
else
  say "  [✓] slab 下限: $CHUNK >= 7 (內部 kernel 列數 = $((CHUNK-7)), 有裕度)"
fi
# (c) 整節點對映 (H200=8GPU/node, GB200=4GPU/node)
if [ $(( NEW_JP % 8 )) -ne 0 ]; then
  if [ $(( NEW_JP % 4 )) -eq 0 ]; then
    say "  [!] jp=$NEW_JP 不是 8 的倍數 → H200 會有半節點; 僅 GB200(4/node) 可整除。建議改用 8 的倍數, 或只跑 GB200。"; WARN=1
  else
    say "  [✗] jp=$NEW_JP 不是 4 的倍數 → 無法對映 GB200(4) 或 H200(8) 整節點"; ERRS=1
  fi
fi
H200_NODES=$(( NEW_JP / 8 )); H200_REM=$(( NEW_JP % 8 ))
GB200_NODES=$(( NEW_JP / 4 )); GB200_REM=$(( NEW_JP % 4 ))
# (info) 2 的冪 / 4 的冪
ISPOW2=$([ $(( NEW_JP & (NEW_JP-1) )) -eq 0 ] && echo yes || echo no)
ISPOW4=no; if [ "$ISPOW2" = yes ]; then n=$NEW_JP; z=0; while [ $((n%2)) -eq 0 ] 2>/dev/null && [ $n -gt 1 ]; do n=$((n/2)); z=$((z+1)); done; [ $((z%2)) -eq 0 ] && ISPOW4=yes; fi
say "  [i] jp=$NEW_JP : 2 的冪=$ISPOW2, 4 的冪=$ISPOW4"
[ "$H200_REM" -eq 0 ]  && say "  [i] H200 : $H200_NODES 節點 × 8 GPU = $NEW_JP"
[ "$GB200_REM" -eq 0 ] && say "  [i] GB200: $GB200_NODES 節點 × 4 GPU = $NEW_JP"
hr

if [ "$NEW_JP" -eq "$CUR_JP" ]; then say "  目標與目前相同 (jp=$CUR_JP), 無事可做。"; exit 0; fi
if [ "$ERRS" -ne 0 ]; then
  say "  本網格 (NY=$NY) 的「合法 jp 候選」(8 的倍數 + 整除 + slab>=7):"
  for j in $(seq 8 8 "$NYm1"); do [ $((NYm1%j)) -eq 0 ] && [ $((NYm1/j)) -ge 7 ] && printf '      jp=%-4d slab=%-3d nodes(H200=%d GB200=%d)\n' "$j" "$((NYm1/j))" "$((j/8))" "$((j/4))"; done
  die "目標 jp=$NEW_JP 不合法, 已中止 (未改任何東西)。"
fi
[ "$H200_REM" -ne 0 ] && die "目標 jp=$NEW_JP 不是 8 的倍數, 與 H200 不相容 (此腳本兩個 partition 都改)。"

# ---- 3. checkpoint / 統計 / job 狀態 偵測 ------------------------------------
LATEST="restart/checkpoint/latest"
[ -e "$LATEST" ] || die "找不到 $LATEST (沒有可續跑的 checkpoint)"
SRC_DIR="$(readlink -f "$LATEST")"
META="$SRC_DIR/metadata.dat"
[ -f "$META" ] || die "找不到 $META"
CK_RANK=$(grep -E '^mpi_rank_count=' "$META" | cut -d= -f2)
CK_STEP=$(grep -E '^step='           "$META" | cut -d= -f2)
CK_FTT=$( grep -E '^FTT='            "$META" | cut -d= -f2)
CK_ACCU=$(grep -E '^accu_count='     "$META" | cut -d= -f2 || echo 0); CK_ACCU=${CK_ACCU:-0}
HEAD_JID=$(cat restart/chain_jobid 2>/dev/null || echo "")
HEAD_STATE=""; [ -n "$HEAD_JID" ] && HEAD_STATE=$(sacct -j "$HEAD_JID" -n -o State 2>/dev/null | head -1 | tr -d ' ' || echo "")

say "=== changejp 1. 計畫 (CUR jp=$CUR_JP → NEW jp=$NEW_JP) ==="
say "  checkpoint : $SRC_DIR"
say "               rank_count=$CK_RANK  step=$CK_STEP  FTT=$CK_FTT  accu_count=$CK_ACCU"
say "  chain head : jobid=${HEAD_JID:-<none>}  state=${HEAD_STATE:-<none>}"
say "  將執行 (build-before-commit; --prepare-only 跳過 a/最後投遞):"
say "   a) (僅 --apply) stop-chain + dispatcher stop + scancel 舊 $CUR_JP-GPU job (job-guard)"
say "   1) sed $VH : #define jp $CUR_JP → $NEW_JP (供編譯)"
say "   2) 直呼 build_and_submit.sh.H200 --build-only 重編 → 驗證 → cp a.out→a.out.H200 (build 失敗則回滾 $VH)"
say "   3) sed $JS_H200/$JS_GB200 : --nodes→$H200_NODES/$GB200_NODES ; mpirun -np→$NEW_JP ; NTASKS:-→$NEW_JP"
say "   4) 更新 $PROV : new_jp=$NEW_JP, new_chunk_j=$CHUNK, variables_h_mtime=<新>"
say "   5) python3 $REPART → 原子換入; 備份移至 restart/ckpt_bak/; 殘留舊 jp step_* 一併移走"
say "   6) (僅 --apply) rm STOP_CHAIN + ./run 投遞 + dispatcher/watcher; (--prepare-only 到 5 為止, 交 dispatcher 投)"
hr

if [ "$APPLY" -ne 1 ]; then
  say "=== DRY-RUN: 未改任何東西。--apply 執行(含投遞);--prepare-only 重編+repartition 但不投遞。 ==="
  exit 0
fi

# ---- 守門 (任一不過即中止; 僅在 --apply/--prepare-only 強制) ----
# (a) 統計累積 (accu_count>0): repartition 會「點對點搬移」36 個 sum_* + 複製全域 cv_*,
#     accu_count 原值保留 → 每個物理點的時間平均 sum/accu_count bit 不差地保住 (整體性保證, 無內插)。
#     --force-stats-loss 才會改成「丟棄統計」(repartition --drop-stats, accu_count 歸 0)。
if [ "${CK_ACCU:-0}" != "0" ]; then
  if [ "$FORCE_STATS_LOSS" -eq 1 ]; then
    say "[守門] accu_count=$CK_ACCU > 0 且指定 --force-stats-loss → 將『丟棄』統計 (accu_count 歸 0)"
  else
    say "[守門] accu_count=$CK_ACCU > 0 → repartition 將『點對點搬移』36 個 sum_* + cv_*, 統計量完整保留 (無內插)"
  fi
fi
# (c) RUNNING job
if [ "$HEAD_STATE" = "RUNNING" ] && [ "$ALLOW_RUNNING" -ne 1 ]; then
  die "chain head $HEAD_JID RUNNING: scancel 會丟失最後 checkpoint 後的計算。需 --allow-running。"
fi
# (d) checkpoint 正在寫 (.WRITING) → 拒切, 避免 torn read
if ls -d restart/checkpoint/step_*.WRITING 2>/dev/null | grep -q .; then
  die "偵測到 restart/checkpoint/step_*.WRITING (checkpoint 寫入中): 拒絕切換, 請稍後再試。"
fi

# ---- 4. 執行 (build-before-commit) ------------------------------------------
MODE_NAME="$([ "$PREPARE_ONLY" -eq 1 ] && echo prepare-only || echo apply)"
say "=== changejp 2. 執行 ($MODE_NAME) ==="
JOURNAL="restart/jp_switch.inprogress"

# [CKPT-ATOM-1/3] 開始新切換前, 先處理上一次中斷殘留: 清不完整暫存 + 依交易日誌做確定性復原,
# 避免在不一致狀態上疊加新切換 (原本 journal 只寫不讀 → crash 後無人復原)。
_recover_stale_switch() {
  rm -rf restart/checkpoint/.changejp_tmp_jp*.* 2>/dev/null || true   # 不完整 repartition 暫存(永不被 latest 引用) → 安全清除 [CKPT-ATOM-3]
  [ -f "$JOURNAL" ] || return 0
  local jfrom jto jphase jsrc vh_jp now_rc bak bak_rc
  jfrom="$(grep -E '^from_jp=' "$JOURNAL" 2>/dev/null | cut -d= -f2)"
  jto="$(  grep -E '^to_jp='   "$JOURNAL" 2>/dev/null | cut -d= -f2)"
  jphase="$(grep -E '^phase='  "$JOURNAL" 2>/dev/null | tail -1 | cut -d= -f2)"
  jsrc="$( grep -E '^src_dir=' "$JOURNAL" 2>/dev/null | cut -d= -f2)"
  vh_jp="$(grep -E '^#define[[:space:]]+jp[[:space:]]' "$VH" | awk '{print $3; exit}')"
  now_rc="$(grep -E '^mpi_rank_count=' "$(readlink -f "$LATEST" 2>/dev/null)/metadata.dat" 2>/dev/null | cut -d= -f2 || true)"
  say "[recover] 偵測殘留切換日誌 (from=$jfrom to=$jto phase=$jphase); latest rank_count=${now_rc:-none}, variables.h jp=$vh_jp"
  if [ -n "$now_rc" ] && [ "$now_rc" = "$vh_jp" ]; then
    say "[recover] checkpoint 與 variables.h jp 一致 → 狀態完好, 清除殘留日誌後繼續 (roll-forward)"
    rm -f "$JOURNAL"; return 0
  fi
  bak="$(ls -1dt restart/ckpt_bak/"$(basename "${jsrc:-X}")"_jp*.* 2>/dev/null | head -1 || true)"
  if [ -n "$bak" ] && [ -d "$bak" ] && [ -n "$jsrc" ]; then
    bak_rc="$(grep -E '^mpi_rank_count=' "$bak/metadata.dat" 2>/dev/null | cut -d= -f2 || echo "${jfrom:-0}")"
    say "[recover] 不一致 → roll-back: 還原 $bak (rank_count=$bak_rc) → $jsrc, 並對齊 variables.h jp=$bak_rc"
    rm -rf "$jsrc" 2>/dev/null || true
    mv "$bak" "$jsrc"
    ln -sfn "$(basename "$jsrc")" "$LATEST"
    sed -E -i "s/^(#define[[:space:]]+jp[[:space:]]+)[0-9]+/\1${bak_rc}/" "$VH"
    # [CKPT-ATOM-1 fix] 同步 provenance: sed 改 variables.h → mtime 變; 不更新 variables_h_mtime
    # 會讓 Preflight C stale FATAL。並把 new_jp/new_chunk_j 對齊回滾後的 jp。
    if [ -f "$PROV" ]; then
      _RVHMT="$(stat -c %Y "$VH" 2>/dev/null)"
      [ -n "$_RVHMT" ] && sed -E -i "s/^variables_h_mtime=.*/variables_h_mtime=$_RVHMT/" "$PROV" 2>/dev/null || true
      sed -E -i "s/^new_jp=.*/new_jp=${bak_rc}/" "$PROV" 2>/dev/null || true
      _RNY="$(awk '/^#define[[:space:]]+NY[[:space:]]/{print $3; exit}' "$VH" 2>/dev/null)"
      [ -n "$_RNY" ] && [ "${bak_rc:-0}" -gt 0 ] && sed -E -i "s/^new_chunk_j=.*/new_chunk_j=$(( (_RNY-1)/bak_rc ))/" "$PROV" 2>/dev/null || true
    fi
    rm -f "$JOURNAL"
    say "[recover] 已還原到一致狀態 (jp=$bak_rc)。請先 ./run --rebuild 使 a.out 對齊 jp, 再重新執行 changejp。"
    exit 0
  fi
  die "[recover] 殘留切換日誌但無法自動還原 (latest rank_count=${now_rc:-none} != jp=$vh_jp, 無 ${jsrc:-?} 的 ckpt_bak 備份)。請人工檢查 restart/checkpoint 與 restart/ckpt_bak 後手動刪除 $JOURNAL。"
}
_recover_stale_switch

TMP=""           # repartition 暫存 (供 _rollback 清理; 先佔位)
BAK=""           # 舊 checkpoint 備份路徑 (供 _rollback 還原; 先佔位)
COMMITTED=0      # 過了 checkpoint 原子提交點才 =1
# 交易快照: 原子提交前的所有可變產物, 供 build 失敗 / crash / kill (含 dispatcher 的 timeout 600
# SIGTERM) 時完整回滾 → 舊 jp 一定完整可跑 (修 HIGH-4 / MED-6)。
SNAP="$(mktemp -d)"
cp "$VH" "$SNAP/variables.h"
[ -f "$JS_H200" ]  && cp "$JS_H200"  "$SNAP/js_h200"   || true
[ -f "$JS_GB200" ] && cp "$JS_GB200" "$SNAP/js_gb200"  || true
[ -f "$PROV" ]     && cp "$PROV"     "$SNAP/prov"      || true
[ -f a.out.H200 ]  && cp a.out.H200  "$SNAP/a.out.H200"  || true
[ -f a.out.GB200 ] && cp a.out.GB200 "$SNAP/a.out.GB200" || true   # [CJP-2] 供 rollback 還原舊 jp GB200 binary
_rollback() {
  [ "${COMMITTED:-0}" = "1" ] && return 0
  # [CKPT-ATOM-2 roll-forward] 若 checkpoint 已原子換成新 jp (mv -T 完成但 COMMITTED 旗標
  # 尚未及設定: 261↔263 窗口被 SIGTERM/SIGKILL 命中), 此時 config 也已是新 jp → 一致,
  # 回滾反而造成 config(舊)/checkpoint(新) 不一致。偵測到即「保留新 jp 狀態」不回滾。
  local _rc=""
  [ -d "$SRC_DIR" ] && _rc="$(grep -E '^mpi_rank_count=' "$SRC_DIR/metadata.dat" 2>/dev/null | cut -d= -f2 || true)"
  if [ "${_rc:-}" = "$NEW_JP" ]; then
    say "[rollback] checkpoint 已是新 jp=$NEW_JP (原子提交實際已完成) → roll-forward, 不回滾"
    rm -f "$JOURNAL" 2>/dev/null || true
    return 0
  fi
  say "[rollback] 切換未提交 → 還原 variables.h / jobscripts / grid_provenance / a.out.H200 (舊 jp 完整可跑)"
  cp "$SNAP/variables.h" "$VH" 2>/dev/null || true
  [ -f "$SNAP/js_h200" ]    && cp "$SNAP/js_h200"    "$JS_H200"  2>/dev/null || true
  [ -f "$SNAP/js_gb200" ]   && cp "$SNAP/js_gb200"   "$JS_GB200" 2>/dev/null || true
  [ -f "$SNAP/prov" ]       && cp "$SNAP/prov"       "$PROV"     2>/dev/null || true
  # [CKPT-ATOM-2 fix] cp 還原 variables.h 會把其 mtime 設成現在 → 必須同步 provenance 的
  # variables_h_mtime, 否則回滾後 run.sh Preflight C 會因 mtime stale 而 FATAL (擋住舊 jp 續跑)。
  if [ -f "$PROV" ] && [ -f "$VH" ]; then
    _RBMT="$(stat -c %Y "$VH" 2>/dev/null)"
    [ -n "$_RBMT" ] && sed -E -i "s/^variables_h_mtime=.*/variables_h_mtime=$_RBMT/" "$PROV" 2>/dev/null || true
  fi
  [ -f "$SNAP/a.out.H200" ]  && cp "$SNAP/a.out.H200"  a.out.H200  2>/dev/null || true
  [ -f "$SNAP/a.out.GB200" ] && cp "$SNAP/a.out.GB200" a.out.GB200 2>/dev/null || true   # [CJP-2] 還原舊 jp GB200 binary
  # [CKPT-ATOM-2 roll-back] checkpoint 提交窗口中斷 (舊 SRC_DIR 已 mv 到 BAK, 新的尚未換入)
  # → 從備份移回 + 修 latest symlink, 確保舊 jp checkpoint 完整可續跑。
  if [ -n "${BAK:-}" ] && [ ! -d "$SRC_DIR" ] && [ -d "$BAK" ]; then
    say "[rollback] checkpoint 提交中斷 → 從備份還原 $SRC_DIR (latest 重指)"
    mv "$BAK" "$SRC_DIR" 2>/dev/null || true
    ln -sfn "$(basename "$SRC_DIR")" "$LATEST" 2>/dev/null || true
  fi
  [ -n "${TMP:-}" ] && rm -rf "$TMP" 2>/dev/null || true
  rm -f "$JOURNAL" 2>/dev/null || true
}
_cleanup() { _rollback; rm -rf "$SNAP" 2>/dev/null || true; }
trap _cleanup EXIT
trap 'exit 130' INT TERM
{ echo "from_jp=$CUR_JP"; echo "to_jp=$NEW_JP"; echo "prepare_only=$PREPARE_ONLY";
  echo "src_dir=$SRC_DIR"; echo "snapshot_dir=$SNAP"; echo "phase=START"; } > "$JOURNAL"

# --prepare-only 由 dispatcher 在「無 active job 的輪界」呼叫 → 不停鏈/不停 daemon/不取消。
# 一般 --apply (手動) 才停鏈 + 取消舊 job, 並於取消後 re-stat accu_count 閉 race。
if [ "$PREPARE_ONLY" -ne 1 ]; then
  say "[a] stop-chain + dispatcher stop"; ./run job-guard stop-chain || true; ./run dispatcher stop 2>/dev/null || true
  case "${HEAD_STATE:-}" in
    ""|COMPLETED|CANCELLED|FAILED|NODE_FAIL|TIMEOUT|OUT_OF_MEMORY|BOOT_FAIL|DEADLINE)
      say "[b] 無 active job 需取消 (state=${HEAD_STATE:-none})" ;;
    *) say "[b] scancel $HEAD_JID (job-guard$([ "$ALLOW_RUNNING" -eq 1 ] && echo ' --allow-running'))"
       ./run job-guard scancel "$HEAD_JID" $([ "$ALLOW_RUNNING" -eq 1 ] && echo --allow-running) ;;
  esac
  _RS="$(readlink -f "$LATEST" 2>/dev/null)"
  _ACC2="$(grep -E '^accu_count=' "$_RS/metadata.dat" 2>/dev/null | cut -d= -f2 || echo 0)"
  [ "${_ACC2:-0}" != "0" ] && say "[b] scancel 後 re-stat accu_count=$_ACC2 (>0) → repartition 會點對點搬移統計, 完整保留"
fi

# (1) 先改 variables.h jp (編譯所需); build 失敗會回滾
say "[1] $VH : jp $CUR_JP → $NEW_JP (供編譯)"
sed -E -i "s/^(#define[[:space:]]+jp[[:space:]]+)[0-9]+/\1${NEW_JP}/" "$VH"
echo "phase=VH_EDITED" >> "$JOURNAL"

# (2) build-only: 直呼 build_and_submit (不走 ./run → 不碰 HEAD.lockdir/不投遞)
say "[2] 重編 a.out (jp=$NEW_JP, build-only, 直呼 build_and_submit.sh.H200)"
if ! bash "$CHAIN_DIR/build_and_submit.sh.H200" --build-only; then
  die "build 失敗 (EXIT trap 會回滾 variables.h; checkpoint/jobscript 未動, 舊 jp 完整可跑)。"
fi
if [ ! -s a.out ] || { command -v file >/dev/null 2>&1 && ! file a.out 2>/dev/null | grep -qi "ELF"; }; then
  die "build 後 a.out 無效 (空/非 ELF; EXIT trap 會回滾)。"
fi
# (3) cp 成 arch binary (dispatcher 從 a.out.H200 投; build-only 只產 a.out)
cp -f a.out a.out.H200
# [CJP-1] 移除舊 jp 的 aarch64 binary: 留著會讓 dispatcher 用「錯 jp」的 GB200 binary 投遞 (比缺檔更危險)。
# 但 x86 login node 無法 cross-build aarch64 → 不會自動重編 (原註解「下次自會重編」不實, 已更正)。
# 後果: GB200 候選暫時消失, 直到於 GB200 節點 ./run build 重編。rollback 時 _rollback 會從 SNAP 還原舊 jp 的 GB200 binary。
if [ -f a.out.GB200 ]; then
  rm -f a.out.GB200
  say "    ✓ a.out.H200 已更新 (jp=$NEW_JP); 移除舊 jp 的 a.out.GB200 → ⚠ GB200 候選暫停用, 需於 GB200 節點重編才恢復"
else
  say "    ✓ a.out.H200 已更新 (jp=$NEW_JP); 無 a.out.GB200 (本專案目前只有 H200 binary)"
fi
echo "phase=BUILT" >> "$JOURNAL"

# (4) build 成功 → 提交 jobscript / provenance
say "[3] jobscripts size + mpirun (H200=$H200_NODES 節點 / GB200=$GB200_NODES 節點)"
sed -E -i "s/^(#SBATCH --nodes=)[0-9]+/\1${H200_NODES}/"                "$JS_H200"
sed -E -i "/^[[:space:]]*#/!s/(mpirun -np )[0-9]+/\1${NEW_JP}/"          "$JS_H200"
sed -E -i "s/(SLURM_NTASKS:-)[0-9]+/\1${NEW_JP}/"                        "$JS_H200"
sed -E -i "s/^(#SBATCH --nodes=)[0-9]+/\1${GB200_NODES}/"               "$JS_GB200"
sed -E -i "/^[[:space:]]*#/!s/(mpirun -np )[0-9]+/\1${NEW_JP}/"          "$JS_GB200"
sed -E -i "s/(SLURM_NTASKS:-)[0-9]+/\1${NEW_JP}/"                        "$JS_GB200"

say "[4] grid_provenance"
if [ -f "$PROV" ]; then
  VH_MT=$(stat -c %Y "$VH")
  sed -E -i "s/^new_jp=.*/new_jp=${NEW_JP}/"                      "$PROV" || true
  sed -E -i "s/^new_chunk_j=.*/new_chunk_j=${CHUNK}/"             "$PROV" || true
  sed -E -i "s/^variables_h_mtime=.*/variables_h_mtime=${VH_MT}/" "$PROV" || true
fi

# (5) 原子提交 checkpoint repartition (dst 為非 step_* 的全新目錄; 備份移出 step_* glob)
say "[5] repartition checkpoint $CUR_JP → $NEW_JP (atomic)"
echo "phase=REPARTITION" >> "$JOURNAL"
TMP="restart/checkpoint/.changejp_tmp_jp${NEW_JP}.$$"
rm -rf "$TMP"
python3 "$REPART" --src "$SRC_DIR" --dst "$TMP" --new-jp "$NEW_JP" $([ "$FORCE_STATS_LOSS" -eq 1 ] && echo --drop-stats)
mkdir -p restart/ckpt_bak
BAK="restart/ckpt_bak/$(basename "$SRC_DIR")_jp${CUR_JP}.$$"
mv "$SRC_DIR" "$BAK"
mv -T "$TMP" "$SRC_DIR"
COMMITTED=1   # 不可逆點: 新 jp checkpoint 已原子換入 SRC_DIR (rename, 同 fs 原子); 此後 trap 不回滾。latest 緊接重指。
ln -sfn "$(basename "$SRC_DIR")" "$LATEST"
say "    原檔備份 → $BAK ; latest → $(readlink "$LATEST")"
# 把殘留「舊 jp」的其他 step_* 移出 (避免 jobscript fallback resume 撈到錯 rank_count)
for d in restart/checkpoint/step_*/; do
  [ -d "$d" ] || continue
  _rc="$(grep -E '^mpi_rank_count=' "$d/metadata.dat" 2>/dev/null | cut -d= -f2)"
  if [ -n "$_rc" ] && [ "$_rc" != "$NEW_JP" ]; then
    mv "$d" "restart/ckpt_bak/$(basename "${d%/}")_jp${_rc}.$$" 2>/dev/null || true
    say "    移走殘留舊 jp checkpoint: $(basename "${d%/}") (rank_count=$_rc)"
  fi
done
# [REPART-STATS-1] 提交後做一次獨立軸序交叉驗證 (BAK=舊 jp vs SRC_DIR=新 jp, f00+rho unique-node bit 比對).
# 已過不可逆點 → 只能 fail-loud 告警(不回滾); f00+rho 即足以抓出軸序退化(Edit6 震盪真因), 統計走同一路徑.
if [ -f "$CHAIN_DIR/tools/repartition_xcheck.py" ] && [ -d "$BAK" ]; then
  if python3 "$CHAIN_DIR/tools/repartition_xcheck.py" "$BAK" "$SRC_DIR" f00 rho >/dev/null 2>&1; then
    say "    ✓ xcheck: 舊↔新 jp 流場 unique-node bit 一致 (軸序正確)"
  else
    say "    ⚠⚠ xcheck 失敗: repartition 後流場與舊 jp 不一致 (已提交不可回滾) — 立即人工檢查, 切勿續跑!"
  fi
fi
echo "phase=COMMITTED" >> "$JOURNAL"
rm -f "$JOURNAL"   # SNAP 由 EXIT trap (_cleanup) 清理

# (6) prepare-only: 到此為止, 不投遞、不碰 daemon/STOP_CHAIN
if [ "$PREPARE_ONLY" -eq 1 ]; then
  hr
  say "✓ prepare-only 完成: jp $CUR_JP → $NEW_JP。a.out.H200 已重編、checkpoint 已 repartition、"
  say "  variables.h/jobscript/provenance 已更新。**未投遞** — 交由 dispatcher 下輪選 partition 投遞。"
  say "  舊 checkpoint 備份: restart/ckpt_bak/"
  exit 0
fi

# (7) 一般 --apply: 投遞既有 a.out(不重編) + 重啟 daemon
say "[6] rm STOP_CHAIN + 投遞 (用剛編好的 a.out, 不重編)"
rm -f restart/STOP_CHAIN
./run
say "[7] dispatcher start + watcher"
./run dispatcher start || true
if [ -f watcher/hill_watcher.sh ]; then
  # [pkill-safety] 只殺「確認是本專案 watcher」的 PID, 避免 stale/回收 PID 誤殺同帳號別程序 (原 pkill -F 無此防護).
  if [ -f live/watcher.pid ]; then
    _wpid="$(tr -dc 0-9 < live/watcher.pid 2>/dev/null)"
    if [ -n "$_wpid" ] && kill -0 "$_wpid" 2>/dev/null \
       && tr '\0' ' ' < "/proc/$_wpid/cmdline" 2>/dev/null | grep -q 'hill_watcher'; then
      kill "$_wpid" 2>/dev/null || true
    fi
    rm -f live/watcher.pid 2>/dev/null || true
  fi
  nohup bash watcher/hill_watcher.sh > /dev/null 2>&1 &
  say "      watcher 重啟"
fi
hr
say "✓ 完成: jp $CUR_JP → $NEW_JP, 從 step=$CK_STEP (FTT=$CK_FTT) 保流場續跑。備份: restart/ckpt_bak/"
