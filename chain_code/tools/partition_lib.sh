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

# ============================================================================
# H200 partition 支援
# ----------------------------------------------------------------------------
# 叢集改版後舊的 `h200` partition 已移除。本帳號 (mst*) 在 H200 機器上有權的
# partition 為 dev / normal / 4nodes（large/slinky/taide 限 gov* 帳號, 不可用）。
# 自動選擇政策 (見 dispatcher pick_cluster):
#   規則1 有容量可即起 → 選 walltime 最長 (最少重投)  → normal 優先
#   規則2 全部得排隊   → 選 ETA 最短 (最快排到)
# ============================================================================
H200_PARTITION_FILE="${H200_PARTITION_FILE:-restart/h200_partition}"

h200_partition_walltime() {
    case "$1" in
        normal)  echo "2-00:00:00" ;;   # 2 天 (partition MaxTime)
        4nodes)  echo "1-00:00:00" ;;   # 1 天 (名稱暗示 <=4 節點)
        dev)     echo "01:00:00"   ;;   # 1 小時 (測試用)
        *)       echo "" ;;
    esac
}

# 可用 H200 partition, walltime 長→短排序 (供 dispatcher 候選 + 手動切換清單)
h200_known_partitions() { echo "normal 4nodes dev"; }

# 每帳號 GPU 上限 (MaxTRESPerAccount) — 來自 sacctmgr show qos:
#   p_normal / p_4nodes : gres/gpu=32  → 整個 account 在該 partition 最多 32 GPU
#   p_dev               : 無上限
# 超過此上限的 jp(GPU 數)在該 partition 會永遠 PENDING (Reason=MaxGRESPerAccount),
# 故 pick 時必須先過濾掉「jp > 上限」的 partition。
partition_gpu_cap_per_account() {
    case "$1" in
        normal|4nodes) echo 32 ;;
        dev)           echo 100000 ;;   # 無 per-account 上限
        *)             echo 100000 ;;    # GB200 等未知 → 視為無上限 (由 --test-only 決定)
    esac
}

h200_active_partition() {
    [ -f "$H200_PARTITION_FILE" ] || return 0
    local p; p="$(tr -d '[:space:]' < "$H200_PARTITION_FILE" 2>/dev/null)"
    [ -n "$p" ] && echo "$p"
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

# ---- 跨叢集統一查詢 (GB200 + H200), 供 dispatcher / build 共用 ----
partition_walltime() {
    local wt; wt="$(gb200_partition_walltime "$1")"
    [ -n "$wt" ] && { echo "$wt"; return; }
    h200_partition_walltime "$1"
}

# walltime 字串 → 秒 ("D-HH:MM:SS" / "HH:MM:SS" / "MM:SS" / infinite)
walltime_to_sec() {
    local w="$1" days=0 hms h=0 m=0 s=0
    case "$w" in infinite|UNLIMITED|"") echo 999999999; return ;; esac
    if [[ "$w" == *-* ]]; then days="${w%%-*}"; hms="${w#*-}"; else hms="$w"; fi
    local IFS=:; set -- $hms
    case $# in
        3) h=$1; m=$2; s=$3 ;;
        2) m=$1; s=$2 ;;
        1) s=$1 ;;
    esac
    echo $(( (10#${days:-0})*86400 + (10#${h:-0})*3600 + (10#${m:-0})*60 + 10#${s:-0} ))
}
