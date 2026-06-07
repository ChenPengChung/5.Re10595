#!/bin/bash
# resubmit_guard.sh — Edit7_10595SNS 「dispatcher 續跑守衛士兵」
# ─────────────────────────────────────────────────────────────────────────────
# 目的: 每個 /loop tick 跑一次, 自動偵測「接近續跑」, 並在自動續投發生時盯住
#       「下一 round 是否真的搶到鎖定的 16gpus @ jp=32 / 4 節點 / 32 task」,
#       無整輪重算、無靜默 PENDING 卡死。
#
# 安全姿態 (預設純唯讀):
#   - 預設只「偵測 + 回報」, 不改任何檔、不 sbatch/scancel、不碰別專案。
#   - 唯一可選的寫入動作 = 重啟「本專案自己」死掉的 dispatcher, 需 --act 才啟用,
#     且通過多重守門 (heartbeat 真死 / 無 STOP / a.out 在 / 無 live head / WorkDir 驗證)。
#   - 判 job 歸屬一律用 WorkDir; 判 job 狀態用 sacct -X (非 squeue -u, federated 會漏);
#     判 daemon 死活用 heartbeat mtime (非 kill -0, 跨 login node 假死)。
#
# 用法:  bash chain_code/tools/resubmit_guard.sh [--act] [--quiet]
# 退出碼: 0=NORMAL/SECURED 無告警 ; 1=有 GUARD-ALERT ; 2=APPROACHING/HANDOFF(資訊)
#
# 常數來源 (2026-06-07 workflow w3220xode + 獨立複驗, file:line):
#   POLL_INTERVAL=30           submit_dispatcher.sh:67
#   PENDING_RESELECT_SEC=1800  submit_dispatcher.sh:654   (嚴格鎖下無法落回別分區)
#   dispatcher 寫 chain_jobid   submit_dispatcher.sh:542
#   STOP_CHAIN 守門             submit_dispatcher.sh:907
#   SIGUSR1@120 (W-120s)        jobscript_chain.slurm.H200:19-21
#   KILL_AFTER=60 / TIMEOUT     jobscript_chain.slurm.H200:505-506
#   RC=124 = walltime 續鏈      jobscript_chain.slurm.H200:46
#   NDTBIN=NDTVTK=50000         variables.h:198-199  (重算窗 <=50k)
# ─────────────────────────────────────────────────────────────────────────────
set -u
PROJ="/home/s8313697/5.Re10595/Edit7_10595SNS"
cd "$PROJ" 2>/dev/null || { echo "GUARD-FATAL: cannot cd $PROJ"; exit 1; }

ACT=0; QUIET=0
for a in "$@"; do
  case "$a" in --act) ACT=1;; --quiet) QUIET=1;; esac
done

# ── thresholds (verified) ──
T_APPROACH=180          # 秒到 walltime 的告警門檻 (SIGUSR1@W-120, 留 60s 緩衝)
HB_STALE=180            # dispatcher heartbeat 視為死的 age
PEND_STALL=1800         # PENDING 卡死門檻 = PENDING_RESELECT_SEC
RECOMP_LOUD=50000       # 重算 gap > 一個 ckpt 窗 = 大聲告警
STATE_FILE="live/resubmit_guard.state"
mkdir -p live

now=$(date +%s)
ALERTS=0
RC_INFO=0
say(){ [ "$QUIET" = 1 ] || echo "$1"; }
info(){ say "  GUARD: $1"; }
alert(){ echo "  GUARD-ALERT($1): $2"; ALERTS=$((ALERTS+1)); }
note(){ say "  GUARD-NOTE($1): $2"; }

# "D-HH:MM:SS"/"HH:MM:SS"/"MM:SS"/"SS" → 秒
to_s(){ awk -F'[-:]' '{d=0;h=0;m=0;s=0;
  if(NF==4){d=$1;h=$2;m=$3;s=$4} else if(NF==3){h=$1;m=$2;s=$3}
  else if(NF==2){m=$1;s=$2} else {s=$1} print d*86400+h*3600+m*60+s}'; }
hb_age(){ [ -f restart/dispatcher.heartbeat ] && echo $(( now - $(stat -c %Y restart/dispatcher.heartbeat) )) || echo 999999; }
jf(){ scontrol show job "$1" 2>/dev/null | tr ' ' '\n' | grep "^$2=" | head -1 | cut -d= -f2; }
sstate(){ sacct -X -n -j "$1" -o State 2>/dev/null | head -1 | tr -d ' '; }
laststep(){ grep -oE 'Step[ =]+[0-9]+' "slurm_$1.log" 2>/dev/null | grep -oE '[0-9]+' | sort -n | tail -1; }

# 驗單一 job 是否落在鎖定組合 (回 0=OK on lock, 1=偏離但本專案, 2=非本專案)
grab_verify_scale(){
  local jid="$1" sj wd part nn nt
  sj=$(scontrol show job "$jid" 2>/dev/null)
  [ -n "$sj" ] || { info "$jid 不在 scontrol(可能剛終態)"; return 3; }
  wd=$(echo "$sj" | tr ' ' '\n' | grep '^WorkDir=' | cut -d= -f2)
  if [ "$wd" != "$PROJ" ]; then info "$jid WorkDir=$wd ≠ 本專案 → 外專案 job, 略過"; return 2; fi
  part=$(echo "$sj" | tr ' ' '\n' | grep '^Partition=' | cut -d= -f2)
  nn=$(echo "$sj" | tr ' ' '\n' | grep '^NumNodes=' | cut -d= -f2)
  nt=$(echo "$sj" | tr ' ' '\n' | grep '^NumTasks=' | cut -d= -f2)
  local bad=0
  [ "$part" = "16gpus" ] || { alert 5f "$jid Partition=$part ≠ 16gpus (LOCK bypass!)"; bad=1; }
  [ "$nn" = "4" ]        || { alert 5f "$jid NumNodes=$nn ≠ 4"; bad=1; }
  [ "$nt" = "32" ]       || { alert 5f "$jid NumTasks=$nt ≠ 32 (jp 漂移!)"; bad=1; }
  [ "$bad" = 0 ] && return 0 || return 1
}

# 重算偵測: 前輪最後 step vs 新輪 --restart= step
recompute_detect(){
  local oid="$1" nid="$2" old_last new_log restart_step gap
  old_last=$(laststep "$oid")
  new_log="slurm_$nid.log"
  if grep -q -- '--cold' "$new_log" 2>/dev/null; then alert RECOMP "新輪被 COLD 啟動(--cold) → 鏈血統斷裂!"; return; fi
  restart_step=$(grep -oE 'restart=step_[0-9]+|Restart from:[^0-9]*step_[0-9]+' "$new_log" 2>/dev/null | grep -oE '[0-9]+' | head -1)
  if [ -n "$old_last" ] && [ -n "$restart_step" ]; then
    gap=$(( old_last - restart_step ))
    if   [ "$gap" -le 0 ]; then info "無重算 (前輪last=$old_last, 新restart=$restart_step)"
    elif [ "$gap" -le "$RECOMP_LOUD" ]; then note 5e "重算 gap=${gap} 步 (在一個 50k ckpt 窗內, 可接受)"
    else alert 5e "重算 gap=${gap} > ${RECOMP_LOUD} → 最終+邊界 ckpt 皆漏寫 (RC=205/NODE_FAIL?)"; fi
  else info "step 驗證待命 (新 log 尚未印 Step/--restart)"
  fi
}

# §6 唯一寫入動作: 重啟本專案自己死掉的 dispatcher (多重守門)
dispatcher_dead_path(){
  local HB="$1" headstate="$2"
  if [ "$HB" -le "$HB_STALE" ]; then return; fi
  [ -e restart/STOP_CHAIN ]      && { info "STOP_CHAIN 在 → 尊重使用者停機, 不重啟 dispatcher"; return; }
  [ -e restart/STOP_DISPATCHER ] && { info "STOP_DISPATCHER 在 → 不重啟"; return; }
  [ -e a.out ] || { info "a.out 不在 → 專案拆除(死亡閘預期), 不重啟"; return; }
  case "$headstate" in RUNNING|PENDING|CONFIGURING) info "head job 仍 $headstate → dispatcher 落後而非死, 暫不重啟"; return;; esac
  if [ "$ACT" = 1 ]; then
    alert 5c "dispatcher heartbeat 死 ${HB}s + 無 STOP + a.out 在 + 無 live head → 重啟本專案 dispatcher (--act)"
    ( cd "$PROJ" && ./run dispatcher stop >/dev/null 2>&1; ./run dispatcher start >/dev/null 2>&1 )
    sleep 5; local nha; nha=$(hb_age)
    [ "$nha" -lt 60 ] && info "dispatcher 已復活 (hb_age=${nha}s)" || alert 5c "重啟後 hb 仍 ${nha}s → keepalive cron 為後盾, 升級給使用者"
  else
    alert 5c "dispatcher heartbeat 死 ${HB}s + 無 STOP + a.out 在 + 無 live head → 應重啟 dispatcher (加 --act 才自動執行)"
  fi
}

# ── 載入前一 tick 的守衛狀態 ──
PHASE=NORMAL; OLD_ID=""; OLD_LAST=""; PEND_SINCE=0
[ -f "$STATE_FILE" ] && . "$STATE_FILE" 2>/dev/null

id=$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]')
HB=$(hb_age)

# ── LOCK 漂移 (§5d) ──
[ -e restart/LOCK_JP_PARTITION ] || alert 5d "restart/LOCK_JP_PARTITION 不見 → 政策鎖被清!"
[ "$(tr -d '[:space:]' <restart/h200_partition 2>/dev/null)" = "16gpus" ] || alert 5d "h200_partition pin ≠ 16gpus"
[ -f live/jp_lock_status ] && { s=$(cat live/jp_lock_status); [ "$s" = "DRIFT" ] && alert 5d "live/jp_lock_status=DRIFT"; }

case "$id" in
  ''|*[!0-9]*)   # chain_jobid 空/非數字 → 交接 gap 或 auto_restore
    PHASE=HANDOFF
    if [ "$HB" -le "$HB_STALE" ]; then
      info "chain_jobid='$id' (gap/auto_restore), dispatcher 活(hb=${HB}s) → 等它投下一輪, defer"; RC_INFO=1
    else
      dispatcher_dead_path "$HB" "NONE"
    fi ;;
  *)             # chain_jobid 是數字
    st=$(sstate "$id")
    [ -z "$st" ] && st="UNKNOWN"
    # (A) 新輪 id 出現 (交接完成)
    if [ -n "$OLD_ID" ] && [ "$id" != "$OLD_ID" ]; then
      info "偵測到新 head: chain_jobid $OLD_ID → $id (state=$st) → 跑 GRAB-VERIFY"
      grab_verify_scale "$id"; gv=$?
      recompute_detect "$OLD_ID" "$id"
      if [ "$st" = "RUNNING" ] && [ "$gv" = 0 ]; then
        info "✅ SECURED: 下一輪搶到 16gpus@jp=32 並 RUNNING (head=$id)"
        PHASE=NORMAL; OLD_ID=""; OLD_LAST=""; PEND_SINCE=0
      elif [ "$st" = "PENDING" ]; then
        PHASE=VERIFY
      fi
    fi
    case "$st" in
      RUNNING)
        TL=$(jf "$id" TimeLimit); RT=$(jf "$id" RunTime)
        if [ -n "$TL" ] && [ -n "$RT" ]; then
          LEFT=$(( $(to_s <<<"$TL") - $(to_s <<<"$RT") ))
          if [ "$LEFT" -le "$T_APPROACH" ]; then
            info "APPROACHING: head=$id LEFT=${LEFT}s ≤ ${T_APPROACH} (SIGUSR1@W-120 將觸發續投)"
            PHASE=APPROACHING; [ -z "$OLD_ID" ] && { OLD_ID="$id"; OLD_LAST=$(laststep "$id"); }
            RC_INFO=1
          else
            [ "$PHASE" = APPROACHING ] || PHASE=NORMAL
            info "NORMAL: head=$id RUNNING, LEFT=$(( LEFT/3600 ))h$(( (LEFT%3600)/60 ))m 到 walltime"
          fi
        fi
        grab_verify_scale "$id" >/dev/null; [ $? = 0 ] && info "head=$id 仍黏 16gpus/4節點/32task ✓"
        ;;
      PENDING|CONFIGURING)
        info "head=$id $st (新輪排隊中)"
        grab_verify_scale "$id"
        [ "$PEND_SINCE" = 0 ] && PEND_SINCE=$now
        pend=$(( now - PEND_SINCE ))
        rsn=$(jf "$id" Reason)
        if [ "$pend" -ge "$PEND_STALL" ]; then
          alert 5a "head=$id PENDING ${pend}s ≥ ${PEND_STALL} (Reason=$rsn); 嚴格鎖無法落回別分區"
          case "$rsn" in
            MaxGRESPerAccount|QOSGrpGRES)
              alert 5a "cap 被同帳號 MST114348 其他 job 佔住 → squeue -A MST114348 查佔用者:";
              squeue -A MST114348 -h -t RUNNING,PENDING -o '    %.10i %.8u %.10P %.6D %.8T %R' 2>/dev/null | head -8 ;;
          esac
        else
          info "PENDING ${pend}s (Reason=$rsn, 門檻 ${PEND_STALL}s)"
        fi
        PHASE=VERIFY; RC_INFO=1
        ;;
      COMPLETED|FAILED|NODE_FAIL|CANCELLED*|TIMEOUT|OUT_OF_MEMORY|BOOT_FAIL|DEADLINE)
        info "head=$id 已終態=$st → HANDOFF (等 dispatcher 投下一輪)"
        PHASE=HANDOFF; [ -z "$OLD_ID" ] && { OLD_ID="$id"; OLD_LAST=$(laststep "$id"); }
        case "$st" in NODE_FAIL|FAILED) note 5b "$id=$st → jobscript 應自動續投+黑名單; 守衛盯下一輪是否出現";; esac
        dispatcher_dead_path "$HB" "$st"
        RC_INFO=1
        ;;
      UNKNOWN)
        info "head=$id 查無 sacct 狀態 (可能剛提交/剛終態) → defer"; RC_INFO=1 ;;
    esac ;;
esac

# heartbeat 健康 (非交接期也報)
if [ "$HB" -le "$HB_STALE" ]; then info "dispatcher heartbeat age=${HB}s (活)"
else alert 5c "dispatcher heartbeat 死 ${HB}s"; fi

# ── 寫回守衛狀態 ──
{ echo "PHASE=$PHASE"; echo "OLD_ID=$OLD_ID"; echo "OLD_LAST=$OLD_LAST"; echo "PEND_SINCE=$PEND_SINCE"; } > "$STATE_FILE"

[ "$ALERTS" -gt 0 ] && exit 1
[ "$RC_INFO" = 1 ] && exit 2
exit 0
