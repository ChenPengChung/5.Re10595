#!/bin/bash
# ============================================================================
# grab_poller.sh — Edit13 復機「自動偵測→搶投→武裝→自動解除」(由 edit13-grab.timer ~30s 觸發)
# ----------------------------------------------------------------------------
# 三選擇(2026-06-26 使用者定): 只 Edit13 / 積極 ~30s / 成功後自動解除 timer。
# 流程:
#   ① 視窗閘: 只在 WINDOW_START→WINDOW_END 內動作; 窗外便宜 exit, 絕不干擾。
#   ② 存活檢查: Edit13 head 已活(squeue PENDING/RUNNING ∪ dispatcher.heartbeat<300s)
#      → 確保 daemon 武裝後 **自動解除 grab timer** 並 exit (冪等)。
#   ③ Pass-0 fail-closed 偵測復機: scontrol ping + 無覆蓋本帳號 maint reservation +
#      `sbatch --test-only`(讀 jobscript 真值) 回「近期 start」。任一不過 → 唯讀 exit(不投)。
#   ④ Pass-1a warm 搶投: 清 stop 哨兵(不清 HEAD.lockdir) → `./run --h200 --no-queue-check`
#      (絕不 --force-cold/--rebuild; ./run 自驗 checkpoint+grid, 無 ckpt 會自己擋, 不冷啟)。
#   ⑤ Pass-1b 武裝 daemon: enable --now edit13-{dispatcher,watcher}.service + watchdog.timer。
#   ⑥ 確認 head 進佇列 + 自動解除 grab timer。
# 安全: partition/account/walltime 一律讀 jobscript(不硬編、跟著鎖走); fail-closed; 冪等;
#       只操作本專案; warm-only; 單頭交 HEAD.lockdir/run.sh 自保。
# 測試: LBM_GRAB_DRY=1 → 只印「would-」不真投/不武裝/不解除; LBM_GRAB_FORCE_WINDOW=1 → 略過視窗閘。
# 純副作用僅在偵測到復機且 head 不活時才發生。
# ============================================================================
set -uo pipefail
_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
CHAIN_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
ROOT="$(cd "$CHAIN_DIR/.." && pwd)"
cd "$ROOT" 2>/dev/null || exit 3
JS="$CHAIN_DIR/jobscript_chain.slurm.H200"
LOGDIR="$ROOT/live"; mkdir -p "$LOGDIR" 2>/dev/null
LOG="$LOGDIR/grab_poller.log"
DRY="${LBM_GRAB_DRY:-0}"
TIMER_UNIT="edit13-grab.timer"

log(){ printf '[%s] %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }
say(){ log "$*"; }   # 別名

# ── 復機搶投視窗 (使用者定: 06/28 12:00 → 06/29 12:00; 提前 2h arm, 過 14:00 仍續輪詢容忍 delay) ──
WINDOW_START="2026-06-28 12:00:00"
WINDOW_END="2026-06-29 12:00:00"
NOW=$(date +%s)
WS=$(date -d "$WINDOW_START" +%s 2>/dev/null || echo 0)
WE=$(date -d "$WINDOW_END" +%s 2>/dev/null || echo 0)

# ① 視窗閘 (窗外便宜 exit)
if [ "${LBM_GRAB_FORCE_WINDOW:-0}" != "1" ]; then
    if [ "$NOW" -lt "$WS" ] || [ "$NOW" -gt "$WE" ]; then exit 0; fi
fi

# ── 讀 jobscript 真值 (不硬編 partition/account/walltime; 跟著鎖走) ──
PART=$(awk -F= '/^#SBATCH[[:space:]]+--partition=/{gsub(/^[ \t]+|[ \t]+$/,"",$2);print $2;exit}' "$JS")
ACCT=$(awk -F= '/^#SBATCH[[:space:]]+--account=/{gsub(/^[ \t]+|[ \t]+$/,"",$2);print $2;exit}' "$JS")
WT=$(awk -F= '/^#SBATCH[[:space:]]+--time=/{gsub(/^[ \t]+|[ \t]+$/,"",$2);print $2;exit}' "$JS")
JP=$(grep -oE '#define[[:space:]]+jp[[:space:]]+[0-9]+' "$ROOT/variables.h" 2>/dev/null | grep -oE '[0-9]+$' | head -1)
NODES=$(( ${JP:-32} / 8 ))   # H200 8 GPU/node

# 自動解除 grab timer (成功或 head 已活時呼叫)
disarm(){
    if [ "$DRY" = 1 ]; then say "would-disarm: systemctl --user disable --now $TIMER_UNIT"; return 0; fi
    systemctl --user disable --now "$TIMER_UNIT" 2>/dev/null && say "✓ 已自動解除 $TIMER_UNIT (不再輪詢)"
}

# 武裝 daemon (冪等)
arm_daemons(){
    if [ "$DRY" = 1 ]; then say "would-arm: enable --now edit13-{dispatcher,watcher}.service + watchdog.timer"; return 0; fi
    systemctl --user enable --now edit13-dispatcher.service edit13-watcher.service edit13-watchdog.timer 2>/dev/null
    say "PASS-1b 武裝 daemon (enable --now dispatcher/watcher/watchdog)"
}

# ② 存活檢查: head job 在佇列, 或 dispatcher 已武裝(heartbeat 新鮮 且 無 STOP_CHAIN) → 確保武裝 + 自動解除
#    (dispatcher 心跳新鮮但 STOP_CHAIN 在 = 被擋住、未武裝、非真活 → 不可誤判, 須續搶投)
JID=$(cat "$ROOT/restart/chain_jobid" 2>/dev/null | tr -d '[:space:]')
HBF="$ROOT/restart/dispatcher.heartbeat"; HB=$(stat -c %Y "$HBF" 2>/dev/null || echo 0)
JST=""; [[ "$JID" =~ ^[0-9]+$ ]] && JST=$(squeue -h -j "$JID" -o '%T' 2>/dev/null | head -1)
JOB_ALIVE=0; { [ "$JST" = RUNNING ] || [ "$JST" = PENDING ]; } && JOB_ALIVE=1
DISP_ARMED=0; { [ "$HB" -gt 0 ] && [ $((NOW-HB)) -lt 300 ] && [ ! -f "$ROOT/restart/STOP_CHAIN" ]; } && DISP_ARMED=1
if [ "$JOB_ALIVE" = 1 ] || [ "$DISP_ARMED" = 1 ]; then
    say "Edit13 已活/已武裝 (job=${JID:-?} state=${JST:-none} hb_age=$((NOW-HB))s STOP_CHAIN=$(test -f "$ROOT/restart/STOP_CHAIN" && echo 在 || echo 無)) → 武裝確認 + 自動解除"
    arm_daemons
    disarm
    exit 0
fi

# ③ Pass-0 fail-closed 偵測復機 (任一不過 → 唯讀 exit, 絕不投)
scontrol ping >/dev/null 2>&1 || { say "Pass-0 等待: slurmctld 不可達 (維護中?)"; exit 0; }
RESV=$(scontrol show reservation 2>/dev/null | grep -iE 'ReservationName=.*maint|Flags=.*MAINT' || true)
if [ -n "$RESV" ]; then say "Pass-0 等待: 仍有 maint reservation"; exit 0; fi
PUP=$(sinfo -h -p "$PART" -o '%a' 2>/dev/null | grep -c '^up')
[ "${PUP:-0}" -ge 1 ] || { say "Pass-0 等待: partition $PART 非 up"; exit 0; }
# ★ 決定性: test-only 讀 jobscript 真值, 須回「to start at」(且近期; 容忍排隊但須能被排)
TO=$(sbatch --test-only --partition="$PART" --account="$ACCT" --nodes="$NODES" \
        --ntasks-per-node=8 --gres=gpu:8 --time="$WT" "$JS" 2>&1)
echo "$TO" | grep -q 'to start at' || { say "Pass-0 等待: test-only 未過 ($(echo "$TO"|head -c120))"; exit 0; }
say "Pass-0 PASS 復機偵測: part=$PART acct=$ACCT nodes=$NODES test-only=[$(echo "$TO"|grep -oE 'to start at [^ ]+')]"

# ④ Pass-1a warm 搶投
# Gate-A 輕量 (唯讀): 須有 latest checkpoint metadata (./run 不帶 --force-cold → 無 ckpt 會自己 abort, 不冷啟)
CK=$(readlink "$ROOT/restart/checkpoint/latest" 2>/dev/null)
if [ -z "$CK" ] || [ ! -s "$ROOT/restart/checkpoint/latest/metadata.dat" ]; then
    say "ABORT-NO-CKPT: 無有效 latest checkpoint → 不投 (避免冷啟風險)"; exit 0
fi
say "PASS-1a warm 搶投: ./run --h200 --no-queue-check (ckpt=$CK)"
if [ "$DRY" = 1 ]; then
    say "would-clean stop-sentinels(STOP_CHAIN/STOP_DISPATCHER/STOP_NOCAPACITY)+stale DISPATCHER_ACTIVE; would-submit: RUNSH_DISPATCHER_BYPASS=1 ./run --h200 --no-queue-check"
else
    # 清 stop 哨兵 (不清 HEAD.lockdir → 交 run.sh:liveness-checked self-heal)
    rm -f "$ROOT/restart/STOP_CHAIN" "$ROOT/restart/STOP_DISPATCHER" "$ROOT/restart/STOP_NOCAPACITY" 2>/dev/null
    # stale DISPATCHER_ACTIVE: 整串數字 PID + kill -0 死才刪
    for d in "$ROOT/DISPATCHER_ACTIVE" "$ROOT/restart/DISPATCHER_ACTIVE"; do
        [ -f "$d" ] || continue; pid=$(tr -dc '0-9' < "$d"); { [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; } && rm -f "$d"
    done
    rm -f "$ROOT/.run.lock" 2>/dev/null
    OUT=$(cd "$ROOT" && RUNSH_DISPATCHER_BYPASS=1 ./run --h200 --no-queue-check 2>&1)
    echo "$OUT" | grep -iE 'sbatch jobid|submit.*OK|case-1|FATAL|cold' | while read -r l; do say "  run: $l"; done
fi

# ⑤ 武裝 daemon
arm_daemons

# ⑥ 確認 head 進佇列 + 自動解除
NJID=$(cat "$ROOT/restart/chain_jobid" 2>/dev/null | tr -d '[:space:]')
NST=""; [[ "$NJID" =~ ^[0-9]+$ ]] && NST=$(squeue -h -j "$NJID" -o '%T' 2>/dev/null | head -1)
if [ "$DRY" = 1 ]; then
    say "would-confirm + would-disarm (DRY)"
elif [ "$NST" = PENDING ] || [ "$NST" = RUNNING ]; then
    say "✓ 搶投成功: job=$NJID state=$NST + daemon 武裝 → 自動解除 grab timer"
    disarm
else
    say "WARN: 投遞後 head 未進佇列 (job=${NJID:-?} state=${NST:-none}) → 保留 timer 下輪重試"
fi
exit 0
