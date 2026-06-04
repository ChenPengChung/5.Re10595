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
    # 2026-06 NCHC 改版: partition 改以 GPU 數命名 (QOS p_Xgpus, MaxTRESPerAccount=X);
    #   舊 normal/4nodes/large 已 inactive. dev 改 4h、cap 降為 4 (僅 jp<=4).
    case "$1" in
        8gpus)   echo "2-00:00:00" ;;   # 2 天, cap 8
        16gpus)  echo "2-00:00:00" ;;   # 2 天, cap 16
        32gpus)  echo "1-00:00:00" ;;   # 1 天, cap 32
        64gpus)  echo "1-00:00:00" ;;   # 1 天, cap 64
        dev)     echo "04:00:00"   ;;   # 4 小時, cap 4 (測試用)
        normal)  echo "2-00:00:00" ;;   # legacy (已 inactive, 保留以防殘留 pin)
        4nodes)  echo "1-00:00:00" ;;   # legacy (已 inactive)
        *)       echo "" ;;
    esac
}

# 可用 H200 partition, cap 高→低排序 (供 dispatcher 候選 + 手動切換清單)
# 2026-06 NCHC 改版後的 GPU-數命名 partition (cap=名稱數字); dev cap=4 保底.
h200_known_partitions() { echo "64gpus 32gpus 16gpus 8gpus dev"; }

# 註: arch-agnostic partition_walltime() 的權威定義在本檔下方(gb200-first fallthrough 版).
#   2026-06-03 一度誤判其未定義(當時測試 source 了錯誤路徑 chain_code/partition_lib.sh 而非
#   真正的 chain_code/tools/partition_lib.sh)而在此加了重複版, 已移除 — 兩版對所有輸入等價。
#   真正解鎖 dev 的是 partition_account_gpu_inuse 改 -t RUNNING + pick_cluster/pick_for_jp 加即時 headroom 過濾。

# 每帳號 GPU 上限 (MaxTRESPerAccount) — 來自 sacctmgr show qos:
#   p_normal=16 / p_4nodes=32 / p_dev=16 (2026-06 實測, 動態查 sacctmgr) → account 在該 partition 的 GPU 上限
# 超過此上限的 jp(GPU 數)在該 partition 會永遠 PENDING (Reason=MaxGRESPerAccount),
# 故 pick 時必須先過濾掉「jp > 上限」的 partition。
partition_gpu_cap_per_account() {
    # [動態] 直接查 sacctmgr QOS MaxTRESPerAccount, 避免 hardcode 過期。
    #   2026-06 實測: NCHC 把 p_normal 從 gres/gpu=32 降成 16 → 舊 hardcode 會誤判 jp=32@normal 可行 → 永久 PENDING。
    #   partition X → QOS p_X; 查無 gres/gpu 上限 = 無上限。timeout 防 sacctmgr 卡住拖垮 daemon。
    local part="$1" cap
    cap="$(timeout 5 sacctmgr -nP show qos "p_${part}" format=MaxTRESPA 2>/dev/null | grep -oE 'gres/gpu=[0-9]+' | head -1 | cut -d= -f2)"
    if [ -n "$cap" ]; then echo "$cap"; return; fi
    # fallback(sacctmgr 不可用時; 對齊 2026-06 NCHC 改版實測: p_Xgpus MaxTRESPA=gres/gpu=X, p_dev=4)
    case "$part" in
        8gpus)  echo 8  ;;
        16gpus) echo 16 ;;
        32gpus) echo 32 ;;
        64gpus) echo 64 ;;
        dev)    echo 4  ;;
        normal) echo 16 ;;   # legacy (已 inactive)
        4nodes) echo 32 ;;   # legacy (已 inactive)
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

# 帳號此刻在某 partition 已用的 GPU (只算 RUNNING; 排除本 chain head). 供 cap headroom 過濾。
#   與 submit_dispatcher.sh 內同名函式邏輯一致 (dispatcher 有自己一份; 此份供直投/自投/run.sh 共用)。
#   只算 RUNNING: MaxGRESPerAccount 只計已配置 GRES, PENDING 不占; 共用帳號別用戶 PENDING 不該擋本專案。
partition_account_gpu_inuse() {
    local part="$1" myhead; myhead="$(cat restart/chain_jobid 2>/dev/null | tr -dc 0-9)"
    squeue -A "${ACCOUNT:-MST115169}" -h -t RUNNING -o '%i|%P|%D|%b' 2>/dev/null | awk -F'|' -v p="$part" -v me="$myhead" '
        { jid=$1; pj=$2; n=$3; g=$4
          if (pj != p) next
          if (me != "" && jid == me) next
          sub(/.*gpu:/,"",g); sub(/[^0-9].*/,"",g); if (g=="") g=0
          tot += n*g }
        END { print tot+0 }'
}

# 依 jp(GPU 數) 選一個「帳號 GPU 上限容得下且此刻有空檔」的 H200 partition (審計 PS-1/PS-4).
# 順序: pin(若已設且容得下) → header 預設(預設 16gpus, 呼叫端可傳第2參數覆寫) → 64gpus(保底,cap最高).
# 雙重過濾: (1) 靜態 cap: jp>cap → 永久 PENDING(MaxGRESPerAccount); (2) 即時 inuse headroom:
#   jp>cap-inuse → 此刻別 job 占滿, 即投也 PENDING. sbatch 盲於 MaxTRESPerAccount 故須額外擋。
h200_pick_partition_for_jp() {
    local jp="${1:-0}" hdr="${2:-16gpus}" p cap pin inuse capfit=""
    pin="$(h200_active_partition)"
    # cap 升序涵蓋完整 GPU-數 partition 範圍 → jp 落在 exact-fit partition
    #   (NCHC 改版: jp=16→16gpus, jp=32→32gpus; 漏列會 fallback 到 dev 投不出).
    for p in "$pin" "$hdr" 8gpus 16gpus 32gpus 64gpus; do
        [ -n "$p" ] || continue
        cap="$(partition_gpu_cap_per_account "$p")"
        [ "$jp" -le "$cap" ] || continue
        [ -z "$capfit" ] && capfit="$p"   # 第一個 cap 容得下的(不論 inuse) = PENDING-safe fallback
        # [FIX 2026-06-03] 即時 inuse headroom (對齊 dispatcher pick_cluster): 修『直投/自投選中此刻
        #   滿的 4nodes → PENDING』(dispatcher fix #4 只修了 dispatcher 一條入口, 未涵蓋直投/自投路徑)。
        if type partition_account_gpu_inuse >/dev/null 2>&1; then
            inuse="$(partition_account_gpu_inuse "$p" 2>/dev/null || echo 0)"
            [ "$jp" -le $(( cap - inuse )) ] || continue
        fi
        echo "$p"; return 0
    done
    # 全部 cap-fit 的 partition 此刻都無 headroom → 回 cap-fit 的(PENDING 到有空檔再跑);
    #   無任何 cap-fit(jp 超過所有 partition) → 64gpus(cap 最高). 不再回 dev(cap4 對 jp>4 投不出).
    echo "${capfit:-64gpus}"
}

# 同 h200_sbatch_partition_args, 但「依 jp 做 GPU-cap 過濾」並「無條件」回傳可行 partition 的
# --partition/--time (即使無 pin 也保證避開超 cap 的 partition). 供 jobscript 自我續投 + 直投共用.
h200_sbatch_partition_args_for_jp() {
    local jp="${1:-0}" hdr="${2:-16gpus}" p wt
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
