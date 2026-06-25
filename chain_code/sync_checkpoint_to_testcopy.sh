#!/usr/bin/env bash
# =============================================================================
# sync_checkpoint_to_testcopy.sh
#   把 Edit11(production)最新 checkpoint 同步到 Edit11x(隔離測試複製檔),
#   讓 Edit11x 成為 NCHC 停機(2026-06-27 09:00 ~ 06-28 14:00)期間的「第二道
#   防線」warm-restart 來源。jp 兩邊都是 64 → 純資料整目錄複製、無 repartition、
#   無插值、無 sed(.bin 場資料與專案名無關;grid_provenance/variables_h_mtime
#   已由 lbm_build_isolated_copy.sh 同步且 grid 相同)。
#
# 設計不變量(MUST):任何時刻 Edit11x 的 checkpoint/latest 都指向一個「完整」的
#   checkpoint。**絕不**在「已驗證的新 checkpoint 就位 + latest 切過去」之前刪掉
#   舊的那份(唯一好副本)。rsync 失敗 / 驗證失敗 / 中途中斷 → 舊副本與 latest
#   原封不動,vault 仍可用。
#
# 安全:來源/目標路徑硬編,只碰 Edit11(讀)與 Edit11x(寫);rm 僅限
#   $DST_ROOT/restart/checkpoint/ 之下且路徑含 "Edit11x" 才執行(防誤刪)。
#   絕不碰 Edit6 / Edit12 / Edit13 / 其他專案。
#
# 用法:
#   bash chain_code/sync_checkpoint_to_testcopy.sh            # 同步一次(dedup,已同步則秒退)
#   bash chain_code/sync_checkpoint_to_testcopy.sh --verify   # 只驗證同步狀態,不複製(exit 0=同步/2=不同步)
#   bash chain_code/sync_checkpoint_to_testcopy.sh --keep N   # 保留 vault 最近 N 份(預設 2)
# 退出碼:0=成功/已同步;2=--verify 下未同步;3=複製或驗證失敗(vault 仍安全);1=前置錯誤
# =============================================================================
set -u

SRC_ROOT="/home/s8313697/5.Re10595/Edit11_Krank5600"
DST_ROOT="/home/s8313697/5.Re10595/Edit11x_Krank5600"
KEEP=2
STABLE_SEC=90          # checkpoint 最新檔須 >= 此秒數未變動,確認非 mid-write
MODE="sync"
LOG="$SRC_ROOT/live/sync_checkpoint_to_testcopy.log"
LOCK="$SRC_ROOT/live/.sync_checkpoint.lock"

while [ $# -gt 0 ]; do
  case "$1" in
    --verify) MODE="verify";;
    --keep)   shift; KEEP="${1:-2}";;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac; shift
done

# --keep 驗證 + 下限:vault 是「第二道防線」→ 永遠保留 >= 2 份;非數字一律拒絕(不可靜默跳過 prune)
case "$KEEP" in ''|*[!0-9]*) echo "FATAL: --keep 必須是正整數(got: $KEEP)" >&2; exit 1;; esac
[ "$KEEP" -lt 2 ] && KEEP=2

mkdir -p "$SRC_ROOT/live"
ts(){ date '+%Y-%m-%d %H:%M:%S'; }
log(){ echo "[$(ts)] $*" | tee -a "$LOG" >&2; }

# ---- 硬安全閘:目標必須是 Edit11x ----
case "$DST_ROOT" in
  *Edit11x_Krank5600) : ;;
  *) echo "FATAL: DST_ROOT 非 Edit11x ($DST_ROOT) — 拒絕執行" >&2; exit 1;;
esac
[ -d "$SRC_ROOT/restart/checkpoint" ] || { echo "FATAL: 來源 checkpoint 目錄不存在" >&2; exit 1; }
[ -d "$DST_ROOT/restart/checkpoint" ] || { echo "FATAL: 目標 checkpoint 目錄不存在($DST_ROOT)" >&2; exit 1; }

SRC_CK="$SRC_ROOT/restart/checkpoint"
DST_CK="$DST_ROOT/restart/checkpoint"

PROD_STEP="$(readlink "$SRC_CK/latest" 2>/dev/null)"
[ -n "$PROD_STEP" ] && [ -d "$SRC_CK/$PROD_STEP" ] || { echo "FATAL: 來源 latest 解析失敗" >&2; exit 1; }
VAULT_STEP="$(readlink "$DST_CK/latest" 2>/dev/null || true)"

# 來源完整位元組數 + 檔數(整數比對,精確)
src_bytes(){ find "$SRC_CK/$PROD_STEP" -type f -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}'; }
src_files(){ find "$SRC_CK/$PROD_STEP" -type f 2>/dev/null | wc -l; }
dir_bytes(){ find "$1" -type f -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}'; }
dir_files(){ find "$1" -type f 2>/dev/null | wc -l; }

SB="$(src_bytes)"; SF="$(src_files)"

is_synced(){
  # vault 已含 PROD_STEP 且 latest 指向它 且 位元組/檔數相符
  [ "$VAULT_STEP" = "$PROD_STEP" ] || return 1
  [ -d "$DST_CK/$PROD_STEP" ] || return 1
  [ "$(dir_bytes "$DST_CK/$PROD_STEP")" = "$SB" ] || return 1
  [ "$(dir_files "$DST_CK/$PROD_STEP")" = "$SF" ] || return 1
  return 0
}

# ---- --verify 模式:只報狀態 ----
if [ "$MODE" = "verify" ]; then
  if is_synced; then
    log "VERIFY OK: Edit11x latest=$VAULT_STEP == prod latest=$PROD_STEP (bytes=$SB files=$SF)"
    exit 0
  else
    log "VERIFY MISMATCH: prod latest=$PROD_STEP (bytes=$SB files=$SF) ; vault latest=${VAULT_STEP:-<none>}"
    exit 2
  fi
fi

# ---- sync 模式 ----
# dedup:已同步直接退
if is_synced; then
  log "已同步(latest=$PROD_STEP) — 跳過"
  exit 0
fi

# 併發保護:non-blocking flock,前一份複製還在跑就退
exec 9>"$LOCK"
if ! flock -n 9; then
  log "另一個同步正在進行(flock held)— 跳過本輪"
  exit 0
fi

# mid-write 守門:來源最新檔須 >= STABLE_SEC 未變動
newest_mtime="$(find "$SRC_CK/$PROD_STEP" -type f -printf '%T@\n' 2>/dev/null | sort -nr | head -1 | cut -d. -f1)"
now="$(date +%s)"
if [ -n "$newest_mtime" ] && [ $((now - newest_mtime)) -lt "$STABLE_SEC" ]; then
  log "來源 $PROD_STEP 仍在寫入(最新檔 $((now-newest_mtime))s < ${STABLE_SEC}s)— 延後到下輪"
  exit 0
fi

log "START sync: prod $PROD_STEP ($SB bytes / $SF files) → Edit11x (vault 現為 ${VAULT_STEP:-<none>})"

PARTIAL="$DST_CK/${PROD_STEP}.partial"
FINAL="$DST_CK/$PROD_STEP"

# 若 vault 已有完整的 PROD_STEP(前次跑到一半 rename 完但 symlink 沒切)→ 跳過複製,直接切+prune
if [ -d "$FINAL" ] && [ "$(dir_bytes "$FINAL")" = "$SB" ] && [ "$(dir_files "$FINAL")" = "$SF" ]; then
  log "vault 已有完整 $PROD_STEP(前次中斷於 symlink 前)— 直接切 latest"
else
  rm -rf "$PARTIAL"
  log "rsync → ${PROD_STEP}.partial ..."
  if ! rsync -a --no-compress "$SRC_CK/$PROD_STEP/" "$PARTIAL/"; then
    log "ERROR: rsync 失敗 — 保留舊 vault,清掉 partial"
    rm -rf "$PARTIAL"
    exit 3
  fi
  # 驗證 partial 完整
  PB="$(dir_bytes "$PARTIAL")"; PF="$(dir_files "$PARTIAL")"
  if [ "$PB" != "$SB" ] || [ "$PF" != "$SF" ]; then
    log "ERROR: 複製不完整(bytes $PB/$SB files $PF/$SF)— 保留舊 vault,清掉 partial"
    rm -rf "$PARTIAL"
    exit 3
  fi
  # 原子 rename(同 FS)。先清掉任何外部殘留的「不完整 FINAL」(此分支僅在 FINAL 缺/不完整
  # 時到達 — 見上方 line-118 guard — 故 rm -rf "$FINAL" 絕不刪到完整 checkpoint),並檢查 rc。
  rm -rf "$FINAL"
  if ! mv -T "$PARTIAL" "$FINAL"; then
    log "ERROR: rename($PROD_STEP)失敗 — 保留舊 vault,清掉 partial"
    rm -rf "$PARTIAL"
    exit 3
  fi
  log "rename → $PROD_STEP 完成(bytes=$PB files=$PF)"
fi

# 原子切換 latest(temp symlink + mv -T;rc 檢查,失敗保留現狀不破壞既有 latest)
ln -sfn "$PROD_STEP" "$DST_CK/.latest.tmp.$$"
if ! mv -T "$DST_CK/.latest.tmp.$$" "$DST_CK/latest"; then
  log "ERROR: latest 切換失敗 — 保留現狀(舊 latest 未動)"
  rm -f "$DST_CK/.latest.tmp.$$"
  exit 3
fi
log "latest → $PROD_STEP 已切換"

# prune:保留最近 KEEP 份(依 step 數字大小),其餘刪除。只在 DST_CK 之下、路徑含 Edit11x。
case "$DST_CK" in *Edit11x_Krank5600*) : ;; *) log "FATAL: prune 路徑異常,跳過刪除"; exit 0;; esac
# ★先掃掉殘留 partial / 暫存 symlink,prune 才只看 verified-complete 的 step_<N>
#   (否則殘留 step_<N>.partial 會被 'step_*' 收進、依數字偷走一個 KEEP 名額 → 害刪到真備援)
find "$DST_CK" -maxdepth 1 -name '*.partial' -prune -exec rm -rf {} + 2>/dev/null
find "$DST_CK" -maxdepth 1 -name '.latest.tmp.*' -exec rm -f {} + 2>/dev/null
# ★排除 *.partial、只收純數字 step_<N>
mapfile -t ALL < <(find "$DST_CK" -maxdepth 1 -type d -name 'step_*' ! -name '*.partial' -printf '%f\n' 2>/dev/null \
                   | sed 's/^step_//' | grep -E '^[0-9]+$' | sort -n)
total=${#ALL[@]}
if [ "$total" -gt "$KEEP" ]; then
  drop=$((total - KEEP))
  for i in $(seq 0 $((drop-1))); do
    s="step_${ALL[$i]}"
    [ "$s" = "$PROD_STEP" ] && continue   # 永不刪當前 latest
    log "prune 舊 vault checkpoint: $s"
    rm -rf "${DST_CK:?}/$s"
  done
fi

log "DONE: Edit11x latest=$PROD_STEP(保留最近 $KEEP 份)"
exit 0
