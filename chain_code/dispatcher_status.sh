#!/bin/bash
# ==============================================================================
# dispatcher_status.sh   —  dispatcher status (Plan B)
# ==============================================================================
# Shows:
#   1. Whether dispatcher is running (PID + liveness)
#   2. Chain status (chain_count + latest jobid in queue)
#   3. sinfo for dispatcher candidate partitions
#   4. Arch binary availability
#   5. restart/RUNNING.lockdir mutex state (Layer 3)
#   6. dispatcher.log tail
# ==============================================================================

set -uo pipefail

# ── [方案 A path discipline] ──
_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || realpath "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
CHAIN_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
PROJECT_ROOT="$(cd "$CHAIN_DIR/.." && pwd)"
cd "$PROJECT_ROOT" || { echo "[dispatcher_status] FATAL: cannot cd to $PROJECT_ROOT" >&2; exit 1; }

SENTINEL="DISPATCHER_ACTIVE"
STOP_SENTINEL="STOP_DISPATCHER"
LOG_FILE="restart/dispatcher.log"

echo "==========================================================================="
echo "  DISPATCHER STATUS  -  $(date '+%Y-%m-%d %H:%M:%S')"
echo "==========================================================================="
echo ""

# --- (1) dispatcher daemon ---
echo "> Dispatcher daemon:"
if [ -f "$SENTINEL" ]; then
    PID="$(cat "$SENTINEL" 2>/dev/null | tr -d '[:space:]')"
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        echo "  [OK] running (PID=$PID)"
        if [ -f "$STOP_SENTINEL" ]; then
            echo "  [WARN] STOP_DISPATCHER exists, daemon will stop on next poll"
        fi
    else
        echo "  [DEAD] sentinel exists but PID ($PID) is dead - manual cleanup: rm $SENTINEL"
    fi
else
    echo "  (not running)"
fi
echo ""

# --- (2) Chain status ---
echo "> Chain status:"
if [ -f restart/chain_count ]; then
    echo "  chain_count = $(cat restart/chain_count 2>/dev/null)"
fi
if [ -f restart/chain_jobid ]; then
    CUR_ID="$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]')"
    echo "  chain_jobid = $CUR_ID"
    if [ -n "$CUR_ID" ]; then
        SQOUT="$(squeue -j "$CUR_ID" -o '%T %R %l %M' 2>/dev/null | tail -n +2)"
        if [ -n "$SQOUT" ]; then
            echo "  squeue:       $SQOUT"
        else
            LAST_EC="$(sacct -X -n -j "$CUR_ID" -o ExitCode 2>/dev/null | head -1 | tr -d '[:space:]')"
            echo "  squeue:       (finished, exit=${LAST_EC:-?})"
        fi
    fi
else
    echo "  (no chain_jobid yet)"
fi
if [ -f restart/STOP_CHAIN ]; then
    echo "  [WARN] restart/STOP_CHAIN exists -> chain will stop naturally"
fi
if [ -f restart/STOP_NOCAPACITY ]; then
    echo "  [WARN] restart/STOP_NOCAPACITY exists -> dispatcher 因長時間無空位而停機:"
    sed 's/^/             /' restart/STOP_NOCAPACITY
    echo "             恢復: rm restart/STOP_NOCAPACITY && ./run dispatcher start"
fi
echo ""

# --- (3) Partition status ---
# [BUGFIX 2026-04-22] 舊版用 '%-12P' 左對齊寬度語法, 但 Slurm sinfo 的 -o 格式
#   只吃 %<num><letter> (固定/右對齊寬度), 不認 '-<num>' → 於是 %-12P 被原字印出
#   變成 '%-12P  avail=%-4a  nodes=%-4D  state=%-12t' 這種畸形輸出. 只有 %E
#   (無寬度) 有正確 substitute.
#   修法: 去掉 '-' dash, 改用 Slurm 官方認的 %<num><letter>, 在所有版本都相容.
echo "> Partition status:"
PARTITION_CANDIDATES_RAW="${PARTITION_CANDIDATES:-GB200:gb200 GB200:gb200-full GB200:gb200-rack1 GB200:gb200-rack2 GB200:gb200-dev H200:16gpus H200:32gpus H200:64gpus H200:dev}"
_seen_parts=""
for entry in $PARTITION_CANDIDATES_RAW; do
    p="${entry#*:}"
    [ -z "$p" ] && continue
    case " $_seen_parts " in *" $p "*) continue ;; esac
    _seen_parts="$_seen_parts $p"
    LINE="$(sinfo -h -p "$p" -o '  %12P  avail=%4a  nodes=%4D  state=%12t  reason=%E' 2>/dev/null)"
    if [ -n "$LINE" ]; then
        echo "$LINE"
    else
        echo "  $p: (not found)"
    fi
done
echo ""

# --- (3.5) Mutex Lock (Layer 3) ---
echo "> Mutex lock (restart/RUNNING.lockdir):"
if [ -d restart/RUNNING.lockdir ]; then
    LK_OWNER="$(grep '^jobid=' restart/RUNNING.lockdir/owner 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
    LK_CLUSTER="$(grep '^cluster=' restart/RUNNING.lockdir/owner 2>/dev/null | cut -d= -f2)"
    LK_HOST="$(grep '^hostname=' restart/RUNNING.lockdir/owner 2>/dev/null | cut -d= -f2)"
    LK_START="$(grep '^started=' restart/RUNNING.lockdir/owner 2>/dev/null | cut -d= -f2-)"
    LK_STATE="$(squeue -h -j "$LK_OWNER" -o '%T' 2>/dev/null | tr -d '[:space:]')"
    echo "  owner   : jobid=$LK_OWNER cluster=$LK_CLUSTER host=$LK_HOST"
    echo "  started : $LK_START"
    case "$LK_STATE" in
        PENDING|RUNNING|CONFIGURING|COMPLETING)
            echo "  state   : [OK] $LK_STATE (owner alive, lock valid)" ;;
        "")
            echo "  state   : [STALE] owner is dead - next job will reclaim" ;;
        *)
            echo "  state   : [?] $LK_STATE" ;;
    esac
else
    echo "  (no active lock - no job currently writing to restart/)"
fi
echo ""

# --- (4) Arch binary ---
echo "> Arch-specific binary:"
for c in GB200 H200; do
    if [ -s "a.out.$c" ]; then
        SZ="$(stat -c%s "a.out.$c" 2>/dev/null)"
        [ -z "$SZ" ] && SZ="$(stat -f%z "a.out.$c" 2>/dev/null)"
        MT="$(stat -c%y "a.out.$c" 2>/dev/null)"
        [ -z "$MT" ] && MT="$(stat -f%Sm "a.out.$c" 2>/dev/null)"
        MT="${MT%%.*}"
        echo "  [OK] a.out.$c  ($SZ bytes, $MT)"
    else
        echo "  [MISSING] a.out.$c  ($c partition cannot be used)"
    fi
done
echo ""

# --- (5) Log tail ---
echo "> Log last 20 lines ($LOG_FILE):"
if [ -f "$LOG_FILE" ]; then
    tail -n 20 "$LOG_FILE" | sed 's/^/  /'
else
    echo "  (no log file)"
fi
echo ""
echo "==========================================================================="
