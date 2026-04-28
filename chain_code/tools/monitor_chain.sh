#!/bin/bash
# ==============================================================================
# monitor_chain.sh   —  持續監控 chain + dispatcher self-heal
# ==============================================================================
# 設計目標 (zero-touch monitoring):
#   1. 每 60 秒寫一筆 chain 狀態到 restart/monitor.log
#      含: chain_count / chain_jobid / squeue 狀態 / ETA / dispatcher 健康度 / latest checkpoint
#   2. 若 dispatcher daemon 死掉 → 自動 ./run dispatcher start 重啟
#      避免「sentinel 不一致 + daemon silent death」造成 chain 卡死
#   3. 雙保險檢查 (ps -ef + sentinel) 避免雙啟動
#
# 用法:
#   nohup bash chain_code/tools/monitor_chain.sh >> restart/monitor.log 2>&1 &
#   echo $! > restart/monitor.pid
#
# 停止:
#   kill $(cat restart/monitor.pid)
#
# 自動 self-heal 由本腳本主動觸發, 不依賴使用者介入.
# ==============================================================================
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/../.." || exit 1   # PROJECT_ROOT

INTERVAL="${MONITOR_INTERVAL:-60}"
HEAL_ENABLE="${MONITOR_HEAL:-1}"   # 0 = 只記錄不自動重啟

mkdir -p restart/

# ─────────────────────────────────────────────────────────────────────────
# Helper: 真實檢查 dispatcher 是否活著 (ps -ef + sentinel 雙保險)
# 回傳 0 = alive, 1 = dead
# ─────────────────────────────────────────────────────────────────────────
dispatcher_alive() {
    # (a) ps 直接找 submit_dispatcher.sh 的 process
    local real_pid
    real_pid=$(ps -ef 2>/dev/null \
        | grep -E 'bash[[:space:]].*submit_dispatcher\.sh' \
        | grep -v grep | grep -v monitor_chain \
        | awk '{print $2}' | head -1)
    if [ -n "$real_pid" ] && kill -0 "$real_pid" 2>/dev/null; then
        echo "$real_pid"
        return 0
    fi
    return 1
}

# Helper: ETA wait (用 squeue --start 拿 SLURM 預估開始時間)
job_eta_wait() {
    local jid="$1"
    [[ "$jid" =~ ^[0-9]+$ ]] || { echo "?"; return; }
    local start ts now wait_s
    start=$(squeue -h -j "$jid" --start -o '%S' 2>/dev/null | tr -d '[:space:]')
    if [ -z "$start" ] || [ "$start" = "N/A" ] || [ "$start" = "Unknown" ]; then
        echo "?"; return
    fi
    ts=$(date -d "$start" +%s 2>/dev/null) || { echo "?"; return; }
    now=$(date +%s)
    wait_s=$(( ts - now ))
    [ "$wait_s" -le 0 ] && { echo "now"; return; }
    if   [ "$wait_s" -lt 3600 ]; then echo "~$((wait_s/60))min"
    else                              echo "~$((wait_s/3600))h$((wait_s%3600/60))m"
    fi
}

while true; do
    ts=$(date '+%Y-%m-%d %H:%M:%S')

    # chain state
    cc=$(cat restart/chain_count 2>/dev/null | tr -d '[:space:]')
    cj=$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]')

    # squeue + ETA
    sq_state="?" sq_extra=""
    if [[ "$cj" =~ ^[0-9]+$ ]]; then
        sq=$(squeue -h -j "$cj" -o '%T|%M|%P' 2>/dev/null)
        if [ -z "$sq" ]; then
            ec=$(sacct -X -n -j "$cj" -o ExitCode 2>/dev/null | head -1 | tr -d '[:space:]')
            st=$(sacct -X -n -j "$cj" -o State 2>/dev/null | head -1 | awk '{print $1}')
            sq_state="$st"; sq_extra="ExitCode=$ec"
        else
            sq_state=$(echo "$sq" | cut -d'|' -f1)
            sq_elap=$(echo "$sq" | cut -d'|' -f2)
            sq_part=$(echo "$sq" | cut -d'|' -f3)
            if [ "$sq_state" = "PENDING" ]; then
                eta=$(job_eta_wait "$cj")
                sq_extra="part=$sq_part ETA=$eta"
            else
                sq_extra="part=$sq_part elapsed=$sq_elap"
            fi
        fi
    else
        sq_state="-"; sq_extra="(no chain_jobid)"
    fi

    # dispatcher 雙保險檢查 + self-heal
    real_pid=""
    if real_pid=$(dispatcher_alive); then
        # 修正 sentinel 不一致 (若需要)
        sent_pid=$(cat DISPATCHER_ACTIVE 2>/dev/null | tr -d '[:space:]')
        if [ "$sent_pid" != "$real_pid" ]; then
            echo "$real_pid" > DISPATCHER_ACTIVE 2>/dev/null
        fi
        dpc="alive(PID=$real_pid)"
    else
        dpc="DEAD"
        # self-heal: 嘗試重啟 dispatcher
        if [ "$HEAL_ENABLE" = "1" ] && [ ! -f restart/STOP_CHAIN ] && [ ! -f restart/STOP_NOCAPACITY ]; then
            # 清 stale sentinel
            rm -f DISPATCHER_ACTIVE STOP_DISPATCHER 2>/dev/null
            # 在背景重啟 dispatcher
            (cd "$(pwd)" && bash chain_code/dispatcher_start.sh >/dev/null 2>&1) &
            # 給 dispatcher_start 2 秒
            sleep 2
            if real_pid=$(dispatcher_alive); then
                dpc="HEALED→alive(PID=$real_pid)"
            else
                dpc="DEAD (heal failed)"
            fi
        fi
    fi

    # checkpoint
    latest_ck=$(ls -1d restart/checkpoint/step_*/ 2>/dev/null | sed 's|/$||' \
                | grep -v '\.WRITING$' \
                | awk -F_ '{print $0"\t"$NF}' | sort -k2 -n | tail -1 | cut -f1)
    [ -z "$latest_ck" ] && latest_ck="(none)" || latest_ck="${latest_ck##*/}"

    # 一筆寫出
    printf '[%s] cc=%s jid=%s | %s | %s | dispatcher=%s | ckpt=%s\n' \
           "$ts" "${cc:-?}" "${cj:-?}" "$sq_state" "$sq_extra" "$dpc" "$latest_ck"

    sleep "$INTERVAL"
done
