#!/usr/bin/env bash
# =============================================================================
# slot_handoff_sentinel.sh  —  dispatcher 首位士兵 (v1)
# -----------------------------------------------------------------------------
# 守住帳號 MST115169 在 64gpus 的「唯一 64-GPU slot」跨 chain 輪界的交接。
# 狀態機 WATCHING → IMMINENT → HANDOFF → VERDICT。
#
# 設計原則 (使用者拍板 2026-06-08):
#   * ALERT-ONLY：只偵測 + 印出告警行(由 Claude Monitor 捕捉 → 通知使用者)。
#     腳本本身「絕不」mutate 任何 job / 檔。所有補救都等使用者確認。
#   * 跨專案隔離：只讀本專案 restart/ + 唯讀 squeue;兄弟專案 job 只觀測不碰。
#   * a.out 死亡閘門：所有 binary 消失且無 .run.lock → 自我 exit 0。
#   * 只讀 / 唯一輸出 = stdout 告警行 + slot_handoff.log + slot_handoff.heartbeat。
#
# 用法:
#   bash chain_code/tools/slot_handoff_sentinel.sh        # 前景(Monitor 會跑這個)
#   DRYRUN=1 bash chain_code/tools/slot_handoff_sentinel.sh   # 跑一輪就退(自測)
# =============================================================================
set -uo pipefail

ROOT=/home/s8313697/5.Re10595/Edit8_NewInterpolation
cd "$ROOT" || { echo "FATAL: cannot cd $ROOT"; exit 1; }
LOG="$ROOT/slot_handoff.log"
HB="$ROOT/restart/slot_handoff.heartbeat"
ACCOUNT=MST115169
IMMINENT_SEC=${IMMINENT_SEC:-300}        # 剩餘 walltime < 此值 → 進警戒(SIGUSR1@120 留 3min lead)
HANDOFF_VERDICT_SEC=${HANDOFF_VERDICT_SEC:-120}   # HANDOFF 後最多等多久判 LOST
DRYRUN=${DRYRUN:-0}

log(){ echo "[slot-sentinel $(date '+%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

# ---- %L ([DD-]HH:MM:SS / MM:SS / SS) → 秒；空/None → 大數(視為非 imminent) ----
to_sec(){
  local t=${1:-}; [ -z "$t" ] || [ "$t" = "None" ] || [ "$t" = "N/A" ] && { echo 999999; return; }
  local d=0
  case "$t" in *-*) d=${t%%-*}; t=${t#*-};; esac
  local a=0 b=0 c=0; local IFS=:; set -- $t
  case $# in
    3) a=$1; b=$2; c=$3 ;;
    2) b=$1; c=$2 ;;
    1) c=$1 ;;
  esac
  a=${a//[!0-9]/}; b=${b//[!0-9]/}; c=${c//[!0-9]/}
  echo $(( d*86400 + 10#${a:-0}*3600 + 10#${b:-0}*60 + 10#${c:-0} ))
}

num(){ local v; v=$(printf '%s' "${1:-}" | grep -oE '^[0-9]+' | head -1); echo "${v:-0}"; }

# 任一 solver binary 存在即視為活;ls 多檔在缺一(如 H200-only 無 a.out.GB200)時會回非零,
# 配 set -o pipefail 會誤觸死亡閘門 → 改用 [ -e ] OR(任一存在為真),死亡閘門只在「全部消失」時觸發。
binary_present(){ [ -e "$ROOT"/a.out ] || [ -e "$ROOT"/a.out.H200 ] || [ -e "$ROOT"/a.out.GB200 ]; }

# 唯讀分類:回傳 "STATE|PART|REASON"(squeue 空 → "NOJOB||")
verdict_of(){
  local j=$1 st part reason
  read -r st part reason _ <<<"$(squeue -j "$j" -h -o '%T %P %r %b' 2>/dev/null)"
  [ -z "${st:-}" ] && { echo "NOJOB||"; return; }
  echo "${st}|${part}|${reason}"
}

# cap 競爭觀測(唯讀):誰在 64gpus 上 RUNNING(本帳號)
account_64_running(){ squeue -A "$ACCOUNT" -h -o '%i %P %T' 2>/dev/null | awk '$2=="64gpus"&&$3=="RUNNING"{print $1}' | tr '\n' ' '; }

STATE=WATCHING
OLD_JID=$(cat restart/chain_jobid 2>/dev/null)
OLD_CNT=$(num "$(cat restart/chain_count 2>/dev/null)")
HSTART=0
TICK=0
log "BORN state=WATCHING head=$OLD_JID count=$OLD_CNT IMMINENT_SEC=$IMMINENT_SEC ALERT-ONLY (v1)"

while true; do
  # ---- a.out 死亡閘門 ----
  if ! binary_present && [ ! -f .run.lock ]; then
    log "DEATH-GATE: no a.out + no .run.lock → exit 0"; exit 0
  fi
  touch "$HB" 2>/dev/null
  TICK=$((TICK+1))

  JID=$(cat restart/chain_jobid 2>/dev/null)
  read -r TL ST RS _ <<<"$(squeue -j "$JID" -h -o '%L %T %r' 2>/dev/null)"
  SLEEP=60

  case "$STATE" in
    WATCHING)
      LEFT=$(to_sec "${TL:-}")
      RC124=$(tail -n 30 restart/chain.log 2>/dev/null | grep -cE 'RC=124|Submitted next round')
      if [ -z "${ST:-}" ] || [ "$LEFT" -le "$IMMINENT_SEC" ] || [ "${RC124:-0}" -gt 0 ]; then
        STATE=IMMINENT; SLEEP=10
        log "IMMINENT head=$JID left=${TL:-gone} state=${ST:-gone} (進警戒接班)"
      else
        if [ "$LEFT" -gt 1800 ]; then SLEEP=300; else SLEEP=60; fi
        # 每 ~1h 心跳一次(WATCHING 平時靜默)
        if [ $((TICK % 60)) -eq 0 ]; then log "alive WATCHING head=$JID left=${TL} (slot 64gpus held)"; fi
      fi ;;

    IMMINENT)
      NEW_JID=$(cat restart/chain_jobid 2>/dev/null)
      NEW_CNT=$(num "$(cat restart/chain_count 2>/dev/null)")
      OLD_GONE=$(squeue -j "$OLD_JID" -h -o '%T' 2>/dev/null)
      if [ "$NEW_JID" != "$OLD_JID" ] || [ "$NEW_CNT" -gt "$OLD_CNT" ]; then
        STATE=HANDOFF; HSTART=$(date +%s); SLEEP=5
        log "HANDOFF old=$OLD_JID → new=$NEW_JID count=${OLD_CNT}→${NEW_CNT} (續投發生, 監看搶 slot)"
      else
        SLEEP=10
        [ -z "$OLD_GONE" ] && log "IMMINENT: old head $OLD_JID 已離隊, 等續投產生新 jobid…"
      fi ;;

    HANDOFF)
      NEW_JID=$(cat restart/chain_jobid 2>/dev/null)
      V=$(verdict_of "$NEW_JID")
      st=${V%%|*}; rest=${V#*|}; part=${rest%%|*}; reason=${rest#*|}
      ELAPSED=$(( $(date +%s) - HSTART ))
      if [ "$st" = "RUNNING" ] && [ "$part" = "64gpus" ]; then
        log "✅ VERDICT=GRABBED new=$NEW_JID RUNNING@64gpus — slot 守住, 輪界乾淨"
        OLD_JID=$NEW_JID; OLD_CNT=$(num "$(cat restart/chain_count 2>/dev/null)"); STATE=WATCHING; SLEEP=60
      elif [ "$st" = "PENDING" ] && [ "$part" = "64gpus" ]; then
        case "$reason" in
          MaxGRESPerAccount|QOSMaxGRESPerAccount|AssocGrpGRES|AssocGrpGPURunMinutes|QOSGrpGPU|QOSGrpGPULimit|QOSMax*)
            SIB=$(account_64_running)
            log "⚠️ SLOT-ALERT=AT-RISK new=$NEW_JID PENDING@64gpus reason=$reason — 兄弟專案 job[$SIB] 佔住帳號 cap;本專案不可碰, 只告警(等你決定)"
            SLEEP=15 ;;
          *)
            log "VERDICT=QUEUED-OK new=$NEW_JID PENDING@64gpus reason=${reason:-None} (等自己 GPU 釋出, 正常)"
            SLEEP=10 ;;
        esac
      elif [ "$st" = "RUNNING" ] || [ "$st" = "PENDING" ]; then
        log "⚠️ SLOT-ALERT=WRONG-PART new=$NEW_JID ${st}@${part} (非 64gpus) — dispatcher 降級, slot 縮水(只告警)"
        OLD_JID=$NEW_JID; STATE=WATCHING; SLEEP=60
      else
        if [ "$ELAPSED" -ge "$HANDOFF_VERDICT_SEC" ]; then
          ANY=$(squeue -A "$ACCOUNT" -h -o '%P' 2>/dev/null | grep -c '64gpus')
          log "❌ SLOT-ALERT=LOST new=$NEW_JID 無 64gpus 本帳號 job 達 ${HANDOFF_VERDICT_SEC}s;帳號 64gpus job 數=$ANY — slot 丟(只告警, 等你補救)"
          OLD_JID=$NEW_JID; STATE=WATCHING; SLEEP=60
        else
          SLEEP=5
        fi
      fi ;;
  esac

  [ "$DRYRUN" = "1" ] && { log "DRYRUN: one pass done, state=$STATE sleep_would_be=${SLEEP}s"; exit 0; }
  sleep "$SLEEP"
done
