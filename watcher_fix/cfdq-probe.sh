#!/usr/bin/env bash
# cfdq-probe.sh — runs ON a single CFDLab node (via ssh, file on shared NFS).
# Prints pipe-delimited lines + a terminal END sentinel. The daemon DISCARDS all
# output from a node unless the LAST line is a well-formed END| line (guards against
# truncated/partial probes on SICK / timed-out nodes).
#
# Positional args ($@):
#   $1 = comma list of REMOTE PIDs of our own running jobs to liveness-check (or "-")
#   $2 = comma list of /proc/PID/stat field-22 START-TIMEs matching $1 positionally (or "-")
#
# Output lines:
#   H|<host>|<nproc>|<load1>|<boot_id>      node health (empty nproc => SICK)
#   M|<model>|<ngpu>                        dominant GPU model (V100/P100/...) + GPU count
#   G|<index>|<uuid>|<mem_used_mb|BAD>      one per GPU (BAD/non-numeric => treat as busy)
#   A|<pid>|<gpu_uuid>|<user|__UNKNOWN__>   one per compute-app (foreign occupancy)
#   L|<pid>|alive|dead                      liveness + PID-reuse identity of each of our PIDs
#   END|<host>|ok                           MUST be last; absence => DOWN/partial
set -u
OUT=""
emit(){ OUT+="$1"$'\n'; }

h=$(hostname -s 2>/dev/null); h=${h:-?}
ncpu=$(nproc 2>/dev/null)
load=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null); load=${load:--}
boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null); boot=${boot:--}
emit "H|${h}|${ncpu}|${load}|${boot}"

if command -v nvidia-smi >/dev/null 2>&1; then
    # Dominant GPU model + count (used by daemon for V100-only filtering).
    model=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 \
            | grep -oiE 'V100|P100|A100|H100|H200|GH200|GB200|RTX ?[0-9]+' | head -1)
    ngpu=$(nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null | grep -c .)
    emit "M|${model:--}|${ngpu:-0}"

    # Per-GPU memory + uuid. Daemon maps uuid->index.
    while IFS=',' read -r idx uuid used; do
        idx=$(printf '%s' "$idx" | tr -d ' ')
        uuid=$(printf '%s' "$uuid" | tr -d ' ')
        used=$(printf '%s' "$used" | tr -d ' ')
        [ -z "$idx" ] && continue
        if [[ "$used" =~ ^[0-9]+$ ]]; then
            emit "G|${idx}|${uuid}|${used}"
        else
            emit "G|${idx}|${uuid}|BAD"   # [N/A] / [Insufficient Permissions] => busy
        fi
    done < <(nvidia-smi --query-gpu=index,gpu_uuid,memory.used \
                 --format=csv,noheader,nounits 2>/dev/null)

    # Compute-apps -> owners. PID may vanish mid-call => __UNKNOWN__ (still a foreign owner).
    while IFS=',' read -r pid guuid; do
        pid=$(printf '%s' "$pid" | tr -d ' ')
        guuid=$(printf '%s' "$guuid" | tr -d ' ')
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        u=$(ps -o user= -p "$pid" 2>/dev/null | tr -d ' ')
        [ -z "$u" ] && u="__UNKNOWN__"
        emit "A|${pid}|${guuid}|${u}"
    done < <(nvidia-smi --query-compute-apps=pid,gpu_uuid \
                 --format=csv,noheader 2>/dev/null)
else
    emit "M|-|0"
fi

# Liveness + PID-reuse identity check for our own jobs on this node.
PIDS="${1:--}"; STARTS="${2:--}"
if [ "$PIDS" != "-" ] && [ -n "$PIDS" ]; then
    IFS=',' read -r -a _pa <<< "$PIDS"
    IFS=',' read -r -a _sa <<< "$STARTS"
    for i in "${!_pa[@]}"; do
        p="${_pa[$i]}"; want="${_sa[$i]:-}"
        [[ "$p" =~ ^[0-9]+$ ]] || { emit "L|${p}|dead"; continue; }
        if kill -0 "$p" 2>/dev/null; then
            cur=$(awk '{print $22}' "/proc/$p/stat" 2>/dev/null)
            if [ -n "$want" ] && [ "$want" != "-" ] && [ -n "$cur" ] && [ "$cur" != "$want" ]; then
                emit "L|${p}|dead"     # PID reused (different start-time) => our job gone
            else
                emit "L|${p}|alive"
            fi
        else
            emit "L|${p}|dead"
        fi
    done
fi

# 每使用者 GPU job 明細 (J 行; `cfdq nodes` 的 job 區用; daemon 解析器會忽略)
#   J|host|user|ngpu|start_epoch|elapsed_sec|memMB|comm|cwd_basename
if command -v nvidia-smi >/dev/null 2>&1; then
    _apps=$(nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory \
                --format=csv,noheader,nounits 2>/dev/null)
    if [ -n "$_apps" ]; then
        declare -A _ug _um _ue _up
        while IFS=',' read -r pid uuid mem; do
            pid=$(printf '%s' "$pid" | tr -d ' ')
            uuid=$(printf '%s' "$uuid" | tr -d ' ')
            mem=$(printf '%s' "$mem" | tr -d ' ')
            [[ "$pid" =~ ^[0-9]+$ ]] || continue
            u=$(ps -o user= -p "$pid" 2>/dev/null | tr -d ' '); [ -z "$u" ] && u='?'
            et=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' '); [[ "$et" =~ ^[0-9]+$ ]] || et=0
            case ",${_ug[$u]:-}," in *",$uuid,"*) ;; *) _ug[$u]="${_ug[$u]:+${_ug[$u]},}$uuid" ;; esac
            _um[$u]=$(( ${_um[$u]:-0} + ${mem:-0} ))
            if [ "$et" -gt "${_ue[$u]:-0}" ]; then _ue[$u]=$et; _up[$u]=$pid; fi
        done <<< "$_apps"
        _nowe=$(date +%s)
        for u in "${!_ug[@]}"; do
            ng=$(printf '%s' "${_ug[$u]}" | tr ',' '\n' | grep -c .)
            et=${_ue[$u]:-0}; rp=${_up[$u]:-0}
            comm=$(ps -o comm= -p "$rp" 2>/dev/null | tr -d ' '); [ -z "$comm" ] && comm='-'
            cwd=$(readlink "/proc/$rp/cwd" 2>/dev/null); cwd="${cwd##*/}"; [ -z "$cwd" ] && cwd='-'
            emit "J|${h}|${u}|${ng}|$(( _nowe - et ))|${et}|${_um[$u]:-0}|${comm}|${cwd}"
        done
    fi
fi

emit "END|${h}|ok"
printf '%s' "$OUT"
