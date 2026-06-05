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
# [2026-06-05 NCHC] 本帳號 (MST114348) 在 H200 上可投的主力 partition (同一池 H200) 為
# 8gpus(cap32,2d) / 16gpus(cap32,2d) / 32gpus(cap32,1d), 對映固定 jp=32 (自由切換集 {8gpus,16gpus,32gpus}@32jp);
# p_*gpus 每帳號 cap 實測皆=32 → 單帳號可填滿 32 GPU; normal/4nodes 目前 INACTIVE、dev cap=4 為 fallback
# (large/slinky/taide 限 gov* 帳號, 不可用); 64gpus(cap64) 已不在自由集。
# 自動選擇政策 (見 dispatcher pick_cluster):
#   規則1 有容量可即起 → 選最快 ETA (抓空閒; walltime 無關, 本 chain SIGUSR1 無縫續投)
#   規則2 全部得排隊   → 選 ETA 最短 (最快排到)
# ============================================================================
H200_PARTITION_FILE="${H200_PARTITION_FILE:-restart/h200_partition}"

h200_partition_walltime() {
    case "$1" in
        normal)  echo "2-00:00:00" ;;   # 2 天 (partition MaxTime)
        4nodes)  echo "1-00:00:00" ;;   # 1 天 (名稱暗示 <=4 節點)
        dev)     echo "04:00:00"   ;;   # [2026-06-04] 實測 MaxTime=4h (舊值 1h 為低估)
        # [2026-06-04] *gpus 系列 (同一池 H200, 依每帳號 GPU cap 命名), MaxTime 實測:
        8gpus)   echo "2-00:00:00" ;;   # cap 8,  2 天
        16gpus)  echo "2-00:00:00" ;;   # cap 16, 2 天
        32gpus)  echo "1-00:00:00" ;;   # cap 32, 1 天
        64gpus)  echo "1-00:00:00" ;;   # cap 64, 1 天
        *)       echo "" ;;
    esac
}

# 可用 H200 partition, walltime 長→短排序 (供 dispatcher 候選 + 手動切換清單)
# [2026-06-05] NCHC 政策: jp=32 自由切換集 {8gpus,16gpus,32gpus} (p_*gpus 每帳號 cap 實測皆=32,
#   故 jp=32 三者皆可投); normal/4nodes INACTIVE、dev cap=4、64gpus 已不在自由集。
h200_known_partitions() { echo "8gpus 16gpus 32gpus"; }

# 每帳號 GPU 上限 (MaxTRESPerAccount) — 來自 sacctmgr show qos:
#   p_normal=16 / p_4nodes=32 (2026-06 實測, 動態查 sacctmgr) → account 在該 partition 的 GPU 上限
#   p_dev               : 無上限
# 超過此上限的 jp(GPU 數)在該 partition 會永遠 PENDING (Reason=MaxGRESPerAccount),
# 故 pick 時必須先過濾掉「jp > 上限」的 partition。
partition_gpu_cap_per_account() {
    # [動態] 直接查 sacctmgr QOS MaxTRESPerAccount, 避免 hardcode 過期。
    #   2026-06 實測: NCHC 把 p_normal 從 gres/gpu=32 降成 16 → 舊 hardcode 會誤判 jp=32@normal 可行 → 永久 PENDING。
    #   partition X → QOS p_X; 查無 gres/gpu 上限 = 無上限。timeout 防 sacctmgr 卡住拖垮 daemon。
    local part="$1" cap
    cap="$(timeout 5 sacctmgr -nP show qos "p_${part}" format=MaxTRESPA 2>/dev/null | grep -oE 'gres/gpu=[0-9]+' | head -1 | cut -d= -f2)"
    if [ -n "$cap" ]; then echo "$cap"; return; fi
    # fallback(sacctmgr 不可用時; 已對齊 2026-06-04 實測值)
    case "$part" in
        normal) echo 16 ;;
        4nodes) echo 32 ;;
        dev)    echo 4 ;;       # [2026-06-04] NCHC 把 dev 從「無上限」砍到 4 GPU/帳號
        8gpus)  echo 32 ;;     # [2026-06-05 sacctmgr 實測] p_8gpus  MaxTRESPA gres/gpu=32
        16gpus) echo 32 ;;     # [2026-06-05 sacctmgr 實測] p_16gpus = 32 (單帳號可填滿 32 GPU)
        32gpus) echo 32 ;;     # [2026-06-05 sacctmgr 實測] p_32gpus = 32
        64gpus) echo 64 ;;
        *)      echo 100000 ;;
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

# 依 jp(GPU 數) 選一個「帳號 GPU 上限容得下」的 H200 partition (審計 PS-1/PS-4).
# 順序: pin(若已設且容得下) → header 預設(預設 normal, 呼叫端可傳第2參數覆寫) → dev(無上限保底).
# 避免 jp>cap(normal/4nodes=32) 落到該 partition 造成永久 PENDING (Reason=MaxGRESPerAccount).
h200_pick_partition_for_jp() {
    local jp="${1:-0}" hdr="${2:-normal}" p cap pin st
    pin="$(h200_active_partition)"
    # [2026-06-05] NCHC 政策 jp=32 自由切換集 {8gpus,16gpus,32gpus} (cap 皆=32, jp=32 皆可投);
    #   候選 pin → 8gpus → 16gpus → 32gpus; 跳過「超 cap」與「非 up」者。
    for p in "$pin" 8gpus 16gpus 32gpus; do
        [ -n "$p" ] || continue
        cap="$(partition_gpu_cap_per_account "$p")"
        [ "$jp" -le "$cap" ] || continue
        st="$(sinfo -h -p "$p" -o '%a' 2>/dev/null | head -1 | tr -d '[:space:]')"
        [ "$st" = "up" ] || continue
        echo "$p"; return 0
    done
    # 保底: 無 up+容得下者 → 回 hdr/pin 讓 SLURM 明確報錯 (勝過靜默回超 cap 的 dev → 永久 PENDING)
    echo "${pin:-$hdr}"
}

# 同 h200_sbatch_partition_args, 但「依 jp 做 GPU-cap 過濾」並「無條件」回傳可行 partition 的
# --partition/--time (即使無 pin 也保證避開超 cap 的 normal). 供 jobscript 自我續投 + 直投共用.
h200_sbatch_partition_args_for_jp() {
    local jp="${1:-0}" hdr="${2:-normal}" p wt
    p="$(h200_pick_partition_for_jp "$jp" "$hdr")"
    wt="$(h200_partition_walltime "$p")"
    [ -z "$wt" ] && wt="01:00:00"
    echo "--partition=$p --time=$wt"
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
