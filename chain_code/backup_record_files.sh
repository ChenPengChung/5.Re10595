#!/usr/bin/env bash
# =============================================================================
# backup_record_files.sh — 三大紀錄檔分層備份(merged design + 對抗審查強化, 2026-06-26)
#   Ustar_Force_record.dat / checkrho.dat / timing_log.dat
#   (gitignored、過大不進 git;append-only 成長,唯 cold-start/clean/reset 才會清)
#
# 分層(兩份;⚠ /home 與 /work 同一 weka 後端 → 抗 rm/clean/reset/誤刪/quota,但**非真異地**,
#       不抗整叢集硬體故障。要真異地需另給遠端走 rclone/scp):
#   主 PRIMARY  = ~/log_backups/Edit11_Krank5600/      (/home 持久、專案樹外 → 耐久主份)
#   次 SECONDARY= /work/s8313697/edit11_log_backups/   (不同 volume;scratch 可能被清,便利份)
#
# 快照式:檔名 <base>_<YYYYMMDD_HHMMSS>_step<零填12位N>.dat.gz,各 dest/各檔輪替留最近 KEEP=10。
# 驗證:gzip 直寫主份→gzip -t→md5;cp 到次份→md5 與主份雙向核對 + gzip -t(兩份 byte-identical)。
# append-only 守門(fail-CLOSED):下限 = max(manifest 記錄, 現存最新快照 gzip -l 解壓大小);來源比下限
#   小(疑 truncate/cold reset)→ **跳過本次快照**(保住既有好快照不被輪替沖出)+ ⚠SHRINK。確定是正常
#   reset 才用 --accept-reset 重新 baseline。manifest 同時複製到次份 → 任一存活 dest 都能自證下限。
# 併發:flock -n 序列化(watchdog 高頻呼叫遇鎖即 no-op)。
#
# 安全:只讀 production 三檔;只寫 PRIMARY/SECONDARY(路徑硬閘);rm 僅限這兩 dest 下、前綴吻合。
#   不碰流場/checkpoint/job/別專案。
#
# 用法:
#   bash chain_code/backup_record_files.sh                # 快照一次(含輪替)
#   bash chain_code/backup_record_files.sh --throttle N   # N 分鐘內已快照過則跳過(watchdog 用)
#   bash chain_code/backup_record_files.sh --force        # 無視 throttle 強制(停機前硬閘用)
#   bash chain_code/backup_record_files.sh --accept-reset # 接受 source 變小(正常 cold-reset 後重 baseline)
#   bash chain_code/backup_record_files.sh --status       # 只看狀態
# 退出碼:0=OK/throttle 跳過;2=有檔 SHRINK 被擋(保留既有好快照、需人工確認);1=前置錯誤
# =============================================================================
set -u

SRC_ROOT="/home/s8313697/5.Re10595/Edit11_Krank5600"
PRIMARY="$HOME/log_backups/Edit11_Krank5600"
SECONDARY="/work/s8313697/edit11_log_backups"
FILES=(Ustar_Force_record.dat checkrho.dat timing_log.dat)
KEEP=10
LOG="$SRC_ROOT/live/backup_record_files.log"
MANIFEST="$PRIMARY/backup_manifest.txt"

case "$PRIMARY"   in */log_backups/Edit11_Krank5600) : ;; *) echo "FATAL: PRIMARY 路徑異常"   >&2; exit 1;; esac
case "$SECONDARY" in */edit11_log_backups)           : ;; *) echo "FATAL: SECONDARY 路徑異常" >&2; exit 1;; esac

MODE="backup"; THROTTLE_MIN=0; FORCE=0; ACCEPT_RESET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --status)       MODE="status";;
    --throttle)     shift; THROTTLE_MIN="${1:-0}";;
    --force)        FORCE=1;;
    --accept-reset) ACCEPT_RESET=1;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac; shift
done
case "$THROTTLE_MIN" in ''|*[!0-9]*) THROTTLE_MIN=0;; esac

mkdir -p "$SRC_ROOT/live" "$PRIMARY" "$SECONDARY" 2>/dev/null
[ -d "$PRIMARY" ] || { echo "FATAL: 無法建 PRIMARY $PRIMARY"; exit 1; }
ts(){ date '+%Y-%m-%d %H:%M:%S'; }
mts(){ date '+%Y-%m-%dT%H:%M:%S'; }
log(){ echo "[$(ts)] $*" | tee -a "$LOG"; }

if [ "$MODE" = "status" ]; then
  echo "=== record 備份狀態 ==="
  for f in "${FILES[@]}"; do
    base="${f%.dat}"
    np=$(find "$PRIMARY"   -maxdepth 1 -name "${base}_*.dat.gz" 2>/dev/null | wc -l)
    ns=$(find "$SECONDARY" -maxdepth 1 -name "${base}_*.dat.gz" 2>/dev/null | wc -l)
    newest=$(find "$PRIMARY" -maxdepth 1 -name "${base}_*.dat.gz" -printf '%f\n' 2>/dev/null | sort | tail -1)
    echo "  $f: 主 ${np} 份 / 次 ${ns} 份 ; 最新=${newest:-<none>}"
  done
  echo "  主 $PRIMARY"; echo "  次 $SECONDARY"
  exit 0
fi

# 併發序列化:flock -n(鎖檔在主份;高頻 watchdog 遇鎖即靜默退出)
exec 9>"$PRIMARY/.backup.lock" 2>/dev/null || true
if ! flock -n 9 2>/dev/null; then log "另一個備份進行中(flock)— 跳過本輪"; exit 0; fi

# 清理崩潰殘留的 .tmp(>60 分;路徑硬閘;不碰當前 run 剛建的)
for d in "$PRIMARY" "$SECONDARY"; do
  case "$d" in */log_backups/Edit11_Krank5600|*/edit11_log_backups) find "$d" -maxdepth 1 -name '.*.tmp.*' -mmin +60 -delete 2>/dev/null;; esac
done

# throttle:主份最近一份 .gz 在 THROTTLE_MIN 分鐘內 → 跳過(--force 無視)
if [ "$FORCE" = "0" ] && [ "$THROTTLE_MIN" -gt 0 ]; then
  newest_t=$(find "$PRIMARY" -maxdepth 1 -name '*.dat.gz' -printf '%T@\n' 2>/dev/null | sort -nr | head -1 | cut -d. -f1)
  if [ -n "$newest_t" ] && [ $(( $(date +%s) - newest_t )) -lt $(( THROTTLE_MIN * 60 )) ]; then exit 0; fi
fi

STEP=$(tail -1 "$SRC_ROOT/checkrho.dat" 2>/dev/null | awk '{print $1}')
case "$STEP" in ''|*[!0-9]*) STEP=0;; esac
STEP_PAD=$(printf '%012d' "$STEP" 2>/dev/null || echo "$STEP")
STAMP=$(date '+%Y%m%d_%H%M%S')

# 防退化下限:max(manifest 記錄, 兩 dest 現存最新快照的 gzip -l 解壓大小)→ fail-closed(manifest 遺失仍自證)
prev_src_bytes(){
  local f="$1" base="${1%.dat}" m=0 g=0 newest d gg
  [ -f "$MANIFEST" ] && m=$(awk -v f="$f" '$2==f{v=$3} END{print v+0}' "$MANIFEST" 2>/dev/null)
  for d in "$PRIMARY" "$SECONDARY"; do
    newest=$(find "$d" -maxdepth 1 -name "${base}_*.dat.gz" -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -1 | cut -d' ' -f2-)
    [ -n "$newest" ] || continue
    gg=$(gzip -l "$newest" 2>/dev/null | awk 'NR==2{print $2+0}'); gg=${gg:-0}
    [ "$gg" -gt "$g" ] && g="$gg"
  done
  m=${m:-0}; g=${g:-0}
  echo $(( g > m ? g : m ))
}

rotate(){  # $1=dest, $2=base ; 各檔保留最近 KEEP 份(檔名已零填 step → 字典序=時間序)
  local dest="$1" base="$2" n drop i
  case "$dest" in */log_backups/Edit11_Krank5600|*/edit11_log_backups) : ;; *) return 0;; esac
  mapfile -t snaps < <(find "$dest" -maxdepth 1 -name "${base}_*.dat.gz" -printf '%f\n' 2>/dev/null | sort)
  n=${#snaps[@]}
  if [ "$n" -gt "$KEEP" ]; then
    drop=$((n - KEEP))
    for i in $(seq 0 $((drop-1))); do rm -f "${dest:?}/${snaps[$i]}"; done
  fi
}

updated=0; shrank=0; skipped=0
for f in "${FILES[@]}"; do
  src="$SRC_ROOT/$f"; base="${f%.dat}"
  [ -f "$src" ] || { log "SKIP $f(來源不存在)"; skipped=$((skipped+1)); continue; }
  nb=$(stat -c %s "$src"); ob=$(prev_src_bytes "$f"); ob=${ob:-0}
  if [ "$nb" -lt "$ob" ]; then
    if [ "$ACCEPT_RESET" = "1" ]; then
      log "⚠ ACCEPTED RESET $f:re-baseline ${ob}B→${nb}B,照常快照(--accept-reset)"
    else
      log "⚠ SHRINK $f(now ${nb}B < 下限 ${ob}B)— 跳過本次快照(保住既有好快照),疑 cold-reset/truncate;確認正常 reset 才用 --accept-reset"
      shrank=$((shrank+1)); continue
    fi
  fi
  snap="${base}_${STAMP}_step${STEP_PAD}.dat.gz"
  ptmp="$PRIMARY/.${snap}.tmp.$$"
  if ! gzip -nc "$src" > "$ptmp"; then log "ERROR: gzip $f 失敗"; rm -f "$ptmp"; skipped=$((skipped+1)); continue; fi
  if ! gzip -t "$ptmp" 2>/dev/null; then log "ERROR: $f gzip -t 不過"; rm -f "$ptmp"; skipped=$((skipped+1)); continue; fi
  m=$(md5sum "$ptmp" | awk '{print $1}')
  mv -f "$ptmp" "$PRIMARY/$snap"
  gz=$(stat -c %s "$PRIMARY/$snap" 2>/dev/null || echo 0)

  # 次份:cp 主份 → 暫存 → 原子 mv → md5 雙向核對 + gzip -t;失敗則移除未驗證的次份檔(僅主份算數)
  ok_s=0; stmp="$SECONDARY/.${snap}.tmp.$$"
  if cp -f "$PRIMARY/$snap" "$stmp" 2>/dev/null && mv -f "$stmp" "$SECONDARY/$snap" 2>/dev/null; then
    if [ "$(md5sum "$SECONDARY/$snap" | awk '{print $1}')" = "$m" ] && gzip -t "$SECONDARY/$snap" 2>/dev/null; then ok_s=1; fi
  fi
  rm -f "$stmp" 2>/dev/null
  if [ "$ok_s" = "0" ]; then case "$SECONDARY" in */edit11_log_backups) rm -f "$SECONDARY/$snap" 2>/dev/null;; esac; fi

  printf '%s %s %s %s %s %s %s\n' "$(mts)" "$f" "$nb" "$gz" "$m" "$snap" "primary$([ "$ok_s" = 1 ] && echo +secondary)" >> "$MANIFEST"
  log "OK $f → $snap (src ${nb}B→gz ${gz}B; md5 ${m:0:12}; 主✓ 次$([ "$ok_s" = 1 ] && echo ✓ || echo ✗))"
  [ "$ok_s" = "0" ] && log "⚠ 次份(/work)寫入或核對失敗 — 主份(/home)仍 good,下輪重試"
  updated=$((updated+1))

  rotate "$PRIMARY"   "$base"
  rotate "$SECONDARY" "$base"
done

# manifest 複製到次份(任一存活 dest 都能自證 shrink 下限)
cp -f "$MANIFEST" "$SECONDARY/backup_manifest.txt" 2>/dev/null || true

log "DONE: updated=$updated skipped=$skipped shrank=$shrank | 主=$PRIMARY 次=$SECONDARY (各檔留最近 $KEEP)"
[ "$shrank" -gt 0 ] && exit 2 || exit 0
