#!/bin/bash
# partition_lib.sh — GB200 partition 切換管理函式庫
# Source this from any script that needs partition override support.
# Depends on: cwd = PROJECT_ROOT (restart/ must be accessible)

GB200_PARTITION_FILE="${GB200_PARTITION_FILE:-restart/gb200_partition}"
H200_PARTITION_FILE="${H200_PARTITION_FILE:-restart/h200_partition}"

gb200_partition_walltime() {
    case "$1" in
        gb200)       echo "96:00:00" ;;
        gb200-dev)   echo "2:00:00" ;;
        gb200-rack1) echo "4:00:00" ;;
        gb200-rack2) echo "4:00:00" ;;
        gb200-full)  echo "12:00:00" ;;
        *)           echo "" ;;
    esac
}

gb200_known_partitions() {
    echo "gb200-dev gb200-rack1 gb200-rack2 gb200-full gb200"
}

# ── H200 single-cluster partitions (NCHC 2026-06-05 policy revision) ──
# 舊 federated normal/4nodes/large 已 State=INACTIVE 不可投。新 partition 的 per-account GPU cap
# 由 QOS p_<partition> MaxTRESPerAccount 決定 (verified 2026-06-05 sacctmgr):
#   dev=4, 8gpus=32, 16gpus=32, 32gpus=32, 64gpus=64。walltime = 各 partition MaxTime (live sinfo)。
h200_partition_walltime() {
    case "$1" in
        8gpus)   echo "2-00:00:00" ;;   # 2 day, QOS cap 32 GPU/acct
        16gpus)  echo "2-00:00:00" ;;   # 2 day, QOS cap 32 GPU/acct  ← 本專案暫時鎖定的 partition
        32gpus)  echo "1-00:00:00" ;;   # 1 day, QOS cap 32 GPU/acct
        64gpus)  echo "1-00:00:00" ;;   # 1 day, QOS cap 64 GPU/acct (需 jp64 + 更細網格)
        dev)     echo "4:00:00"    ;;   # 4 hour, QOS cap 4 GPU/acct (jp32 不適用)
        *)       echo "" ;;
    esac
}
# [EDIT11] jp 鎖定 64 → 候選集含 64gpus (cap=64, 唯一容得下 jp=64; 見 select_combo_lib.sh SC_PARTITIONS)。
h200_known_partitions() { echo "8gpus 16gpus 32gpus 64gpus"; }

h200_partition_cap() {   # static per-account GPU cap (QOS MaxTRESPerAccount, verified 2026-06-05)
    case "$1" in 8gpus|16gpus|32gpus) echo 32 ;; 64gpus) echo 64 ;; dev) echo 4 ;; *) echo 0 ;; esac
}

# H200 partition pin (暫時鎖定): 直接 ./run 投遞 + jobscript 自我續投 fallback 用;
# dispatcher 運行中仍以 select_combo_lib 在候選集自由切換 (pin 不約束 dispatcher)。
h200_active_partition() {
    if [ -f "$H200_PARTITION_FILE" ]; then
        local p
        p="$(cat "$H200_PARTITION_FILE" 2>/dev/null | tr -d '[:space:]')"
        if [ -n "$p" ]; then
            echo "$p"
            return 0
        fi
    fi
    return 0
}

h200_sbatch_partition_args() {
    local part wt
    part="$(h200_active_partition)"
    [ -z "$part" ] && return 0
    wt="$(h200_partition_walltime "$part")"
    if [ -z "$wt" ]; then
        echo "[partition_lib] WARN: unknown H200 partition '$part', ignoring override" >&2
        return 0
    fi
    echo "--partition=$part --time=$wt"
}

gb200_active_partition() {
    if [ -f "$GB200_PARTITION_FILE" ]; then
        local p
        p="$(cat "$GB200_PARTITION_FILE" 2>/dev/null | tr -d '[:space:]')"
        if [ -n "$p" ]; then
            echo "$p"
            return 0
        fi
    fi
    return 0
}

gb200_sbatch_partition_args() {
    local part wt
    part="$(gb200_active_partition)"
    [ -z "$part" ] && return 0
    wt="$(gb200_partition_walltime "$part")"
    if [ -z "$wt" ]; then
        echo "[partition_lib] WARN: unknown partition '$part', ignoring override" >&2
        return 0
    fi
    echo "--partition=$part --time=$wt"
}
