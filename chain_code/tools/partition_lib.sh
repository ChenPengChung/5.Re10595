#!/bin/bash
# partition_lib.sh — GB200 partition 切換管理函式庫
# Source this from any script that needs partition override support.
# Depends on: cwd = PROJECT_ROOT (restart/ must be accessible)

GB200_PARTITION_FILE="${GB200_PARTITION_FILE:-restart/gb200_partition}"

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

# ── H200 single-cluster partitions (NCHC 2026-06; replaces the defunct federated h200/gb200) ──
# Per-account GPU cap 32 on normal/4nodes (QOS MaxTRESPerAccount); dev uncapped.
h200_partition_walltime() {
    case "$1" in
        normal)  echo "2-00:00:00" ;;   # 2 day, cap 32 GPU/acct
        4nodes)  echo "1-00:00:00" ;;   # 1 day, cap 32 GPU/acct
        dev)     echo "1:00:00"    ;;   # 1 hour, UNCAPPED
        large)   echo "7-00:00:00" ;;   # gov-only (AllowAccounts); listed for completeness
        slinky)  echo "7-00:00:00" ;;   # gov-only
        *)       echo "" ;;
    esac
}
h200_known_partitions() { echo "normal 4nodes dev"; }   # usable (non-gov) partitions

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
