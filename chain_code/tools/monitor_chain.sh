#!/bin/bash
# ==============================================================================
# monitor_chain.sh   —  持續監控 chain 狀態 (每 N 秒寫一筆 monitor.log)
# ==============================================================================
# 用法:
#   nohup bash chain_code/tools/monitor_chain.sh >> restart/monitor.log 2>&1 &
#   echo $! > restart/monitor.pid
#
# 停止:
#   kill $(cat restart/monitor.pid)
#
# 內容: 每筆一行, 包含
#   - 時間戳
#   - chain_count / chain_jobid
#   - squeue 狀態 (PENDING/RUNNING/elapsed/reason)
#   - dispatcher daemon 是否活著
#   - 最新 checkpoint step
#   - chain.log 最後一行 / dispatcher.log 最後一行 (簡短摘要)
# ==============================================================================
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/../.." || exit 1   # PROJECT_ROOT

INTERVAL="${MONITOR_INTERVAL:-60}"
TARGET_LOG="${MONITOR_LOG:-restart/monitor.log}"

mkdir -p restart/

while true; do
    ts=$(date '+%Y-%m-%d %H:%M:%S')

    # chain state
    cc=$(cat restart/chain_count 2>/dev/null | tr -d '[:space:]')
    cj=$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]')

    # squeue
    if [[ "$cj" =~ ^[0-9]+$ ]]; then
        sq=$(squeue -h -j "$cj" -o '%T|%M|%P|%R' 2>/dev/null)
        if [ -z "$sq" ]; then
            ec=$(sacct -X -n -j "$cj" -o ExitCode 2>/dev/null | head -1 | tr -d '[:space:]')
            st=$(sacct -X -n -j "$cj" -o State 2>/dev/null | head -1 | awk '{print $1}')
            sq_state="$st"
            sq_extra="ExitCode=$ec"
        else
            sq_state=$(echo "$sq" | cut -d'|' -f1)
            sq_elap=$(echo "$sq" | cut -d'|' -f2)
            sq_part=$(echo "$sq" | cut -d'|' -f3)
            sq_reason=$(echo "$sq" | cut -d'|' -f4)
            sq_extra="elapsed=$sq_elap part=$sq_part reason=$sq_reason"
        fi
    else
        sq_state="?"
        sq_extra="(no chain_jobid)"
    fi

    # dispatcher
    dpid=$(cat DISPATCHER_ACTIVE 2>/dev/null | tr -d '[:space:]')
    if [ -n "$dpid" ] && kill -0 "$dpid" 2>/dev/null; then
        dpc="alive(PID=$dpid)"
    elif [ -n "$dpid" ]; then
        dpc="DEAD(PID=$dpid)"
    else
        dpc="not-started"
    fi

    # checkpoint
    latest_ck=$(ls -1d restart/checkpoint/step_*/ 2>/dev/null | sed 's|/$||' \
                | grep -v '\.WRITING$' \
                | awk -F_ '{print $0"\t"$NF}' | sort -k2 -n | tail -1 | cut -f1)
    [ -z "$latest_ck" ] && latest_ck="(none)"
    latest_ck_short="${latest_ck##*/}"

    # one-liner output
    printf '[%s] cc=%s jid=%s | %s | %s | dispatcher=%s | ckpt=%s\n' \
           "$ts" "${cc:-?}" "${cj:-?}" "$sq_state" "$sq_extra" "$dpc" "$latest_ck_short"

    sleep "$INTERVAL"
done
