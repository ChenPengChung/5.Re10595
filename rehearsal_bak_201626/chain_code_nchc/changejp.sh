#!/bin/bash
# ==============================================================================
# changejp.sh — 通用 GPU 數 (jp) 切換 + 保流場續跑 + 重新投遞
# ------------------------------------------------------------------------------
# 把本 GILBM chain 從「當前 jp」切換到「目標 jp」, 完全保留現有流場 (checkpoint),
# 不冷啟動, 並維持既有 chain/dispatcher/watcher 流程, 只改變平行規模。
#
# 任意有效 jp 皆可 (16/32/64/128 … 只要通過下方驗證), 不限 4 的次方。
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
#   * accu_count>0 (已累積統計) 預設拒絕 (repartition 不搬統計), 需 --force-stats-loss
#   * 改 variables.h 後同步更新 grid_provenance mtime, 讓 run.sh Preflight C 放行
# ==============================================================================
set -euo pipefail

# ---- 0. 解析參數 --------------------------------------------------------------
NEW_JP="${1:-}"
APPLY=0; ALLOW_RUNNING=0; FORCE_STATS_LOSS=0
shift || true
for a in "$@"; do
  case "$a" in
    --apply)            APPLY=1 ;;
    --allow-running)    ALLOW_RUNNING=1 ;;
    --force-stats-loss) FORCE_STATS_LOSS=1 ;;
    *) echo "[changejp] 未知參數: $a" >&2; exit 2 ;;
  esac
done

if ! [[ "$NEW_JP" =~ ^[0-9]+$ ]] || [ "$NEW_JP" -lt 1 ]; then
  echo "用法: bash changejp.sh <NEW_JP> [--apply] [--allow-running] [--force-stats-loss]" >&2
  exit 2
fi
if [ "$NEW_JP" -ne 32 ]; then
  echo "[changejp][FATAL] 本專案 NCHC 起跑前已鎖定 jp=32；拒絕改成 jp=$NEW_JP" >&2
  echo "                 若未來要解除鎖定，需同步調整 selector/jobscript/selfcheck 後再開放。" >&2
  exit 2
fi

CHAIN_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$CHAIN_DIR/.." && pwd)"   # project root = parent of chain_code_nchc/
cd "$ROOT"
VH="variables.h"
JS_H200="$CHAIN_DIR/jobscript_chain.slurm.H200"
JS_GB200="$CHAIN_DIR/jobscript_chain.slurm.GB200"
PROV="restart/grid_provenance"
REPART="$CHAIN_DIR/repartition_jp.py"

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
  say "  [!] slab=7 → 內部 kernel 列數=0 (零裕度, 與 jp=128 同樣在邊緣); 可跑但無餘裕"; WARN=1
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
say "  將執行:"
say "   1) ./run job-guard stop-chain        (建立 STOP_CHAIN, 暫停自動續投)"
say "   2) ./run dispatcher stop             (停 daemon, 避免重投競態)"
say "   3) ./run job-guard scancel $HEAD_JID (驗 WorkDir 後取消舊 $CUR_JP-GPU job)"
say "   4) python3 $REPART --src $SRC_DIR --dst <tmp> --new-jp $NEW_JP"
say "      → 備份 $SRC_DIR → ${SRC_DIR}_jp${CUR_JP}_bak; 新檔換入同名; latest 重指"
say "   5) sed $VH : #define jp $CUR_JP → $NEW_JP"
say "   6) sed $JS_H200 : --nodes=$((CUR_JP/8))→$H200_NODES ; mpirun -np $CUR_JP→$NEW_JP ; NTASKS:-→$NEW_JP"
say "      sed $JS_GB200: --nodes=$((CUR_JP/4))→$GB200_NODES; mpirun -np $CUR_JP→$NEW_JP ; NTASKS:-→$NEW_JP"
say "   7) 更新 $PROV : new_jp=$NEW_JP, new_chunk_j=$CHUNK, variables_h_mtime=<新>"
say "   8) rm STOP_CHAIN; ./run --rebuild     (重編 a.out.H200 jp=$NEW_JP + 投遞新 head)"
say "   9) ./run dispatcher start; 重啟 watcher"
hr

# accu_count 守門 [更新 2026-06-02: repartition 已升級為「保統計」]
# repartition_jp.py 現會搬移 36 sum_* + 3 cv + accu_count (bit-exact) 並對 accu>0 做精確名字驗證,
# 故「統計階段也能完全自由切換」, 不再丟統計 → 預設允許, 僅資訊提示。--force-stats-loss 已過時 (no-op)。
if [ "${CK_ACCU:-0}" != "0" ]; then
  say "ℹ️ accu_count=$CK_ACCU > 0: repartition 會『保統計』搬移 (sum_*/cv/accu_count bit-exact), 統計連續不歸零。"
fi
# RUNNING 守門
if [ "$HEAD_STATE" = "RUNNING" ] && [ "$ALLOW_RUNNING" -ne 1 ]; then
  die "chain head $HEAD_JID 正在 RUNNING: scancel 會丟失「最後 checkpoint 之後」的計算。
       若確定要切 (用最後 checkpoint), 重跑加 --allow-running。"
fi

if [ "$APPLY" -ne 1 ]; then
  say "=== DRY-RUN: 未改任何東西。確認無誤後加 --apply 執行。 ==="
  exit 0
fi

# ---- 4. 執行 (--apply) -------------------------------------------------------
say "=== changejp 2. 執行 (--apply) ==="
say "[1/9] stop-chain"          ; ./run job-guard stop-chain || true
say "[2/9] dispatcher stop"     ; ./run dispatcher stop 2>/dev/null || true
case "${HEAD_STATE:-}" in
  ""|COMPLETED|CANCELLED|FAILED|NODE_FAIL|TIMEOUT|OUT_OF_MEMORY|BOOT_FAIL|DEADLINE)
    say "[3/9] 無 active head job 需取消 (state=${HEAD_STATE:-none})" ;;
  *)
    say "[3/9] scancel $HEAD_JID (經 job-guard 驗 WorkDir)"; ./run job-guard scancel "$HEAD_JID" ;;
esac

say "[4/9] repartition checkpoint $CUR_JP → $NEW_JP"
TMP="restart/checkpoint/.changejp_tmp_jp${NEW_JP}.$$"
rm -rf "$TMP"
python3 "$REPART" --src "$SRC_DIR" --dst "$TMP" --new-jp "$NEW_JP"
BAK="${SRC_DIR}_jp${CUR_JP}_bak"
[ -e "$BAK" ] && BAK="${BAK}.$$"
mv "$SRC_DIR" "$BAK"
mv "$TMP" "$SRC_DIR"
ln -sfn "$(basename "$SRC_DIR")" "$LATEST"
say "      原檔備份 → $BAK ; latest → $(readlink "$LATEST")"

say "[5/9] $VH : jp $CUR_JP → $NEW_JP"
sed -E -i "s/^(#define[[:space:]]+jp[[:space:]]+)[0-9]+/\1${NEW_JP}/" "$VH"

say "[6/9] jobscripts size + mpirun"
# H200 (8 GPU/node)
sed -E -i "s/^(#SBATCH --nodes=)[0-9]+/\1${H200_NODES}/"                "$JS_H200"
sed -E -i "/^[[:space:]]*#/!s/(mpirun -np )[0-9]+/\1${NEW_JP}/"          "$JS_H200"
sed -E -i "s/(SLURM_NTASKS:-)[0-9]+/\1${NEW_JP}/"                        "$JS_H200"
# GB200 (4 GPU/node)
sed -E -i "s/^(#SBATCH --nodes=)[0-9]+/\1${GB200_NODES}/"               "$JS_GB200"
sed -E -i "/^[[:space:]]*#/!s/(mpirun -np )[0-9]+/\1${NEW_JP}/"          "$JS_GB200"
sed -E -i "s/(SLURM_NTASKS:-)[0-9]+/\1${NEW_JP}/"                        "$JS_GB200"

say "[7/9] grid_provenance"
if [ -f "$PROV" ]; then
  VH_MT=$(stat -c %Y "$VH")
  sed -E -i "s/^new_jp=.*/new_jp=${NEW_JP}/"                  "$PROV" || true
  sed -E -i "s/^new_chunk_j=.*/new_chunk_j=${CHUNK}/"         "$PROV" || true
  sed -E -i "s/^variables_h_mtime=.*/variables_h_mtime=${VH_MT}/" "$PROV" || true
  say "      new_jp=$NEW_JP new_chunk_j=$CHUNK variables_h_mtime=$VH_MT"
else
  say "      (無 grid_provenance, 跳過)"
fi

say "[8/9] rm STOP_CHAIN + ./run --rebuild (重編 jp=$NEW_JP 並投遞)"
rm -f restart/STOP_CHAIN
./run --rebuild

say "[9/9] dispatcher start + watcher"
./run dispatcher start || true
if [ -f watcher_nchc/hill_watcher.sh ]; then
  pkill -F live/watcher.pid 2>/dev/null || true; rm -f live/watcher.pid 2>/dev/null || true
  nohup bash watcher_nchc/hill_watcher.sh > /dev/null 2>&1 &
  say "      watcher 重啟"
fi
hr
say "✓ 完成: jp $CUR_JP → $NEW_JP, 從 step=$CK_STEP (FTT=$CK_FTT) 保流場續跑。"
say "  舊 checkpoint 備份: $BAK (確認新規模穩定後可刪)。"
