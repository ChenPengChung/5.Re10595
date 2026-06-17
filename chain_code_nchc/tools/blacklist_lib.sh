#!/usr/bin/env bash
# ==============================================================================
# chain_code/tools/blacklist_lib.sh
# ==============================================================================
# 節點黑名單管理函式庫 (shared across jobscripts + dispatcher + build_and_submit)
#
# 設計初衷:
#   1. 本地黑名單 restart/bad_nodes 有 TTL (預設 24h), 過期自動釋放.
#   2. 加入黑名單前先查 NCHC sinfo 確認節點真的壞 (NCHC 說健康就拒絕加入),
#      避免本地程式 bug (如 --forward-signals) 污染黑名單.
#   3. 讀取 exclude 列表時實時和 NCHC 狀態合併:
#         - NCHC 現在 idle/mix/alloc 的節點 → 從本地黑名單釋放
#         - NCHC 現在 down/drain/fail/maint → 加入 effective exclude
#   4. --exclude 列表不得超過 partition 50% (避免無條件可滿足的排除).
#
# 檔案格式 (restart/bad_nodes):
#   新格式: <node>\t<epoch_ts>\t<reason>\t<src>
#   舊格式: <node>                          (純 hostname, legacy, 首次 GC 自動升級)
#
# 可調環境變數:
#   BLACKLIST_TTL_SEC   TTL 秒數            (預設 86400 = 24h)
#   BLACKLIST_MAX_PCT   exclude 上限百分比  (預設 50)
#   BAD_NODES_FILE      本地黑名單路徑      (預設 restart/bad_nodes)
#   GLOBAL_BAD_FILE     跨專案黑名單        (預設 ~/.bad_nodes_global)
#
# 用法:
#   . tools/blacklist_lib.sh
#   bl_add <node> <reason> <src>                 # 寫入 (先驗 NCHC)
#   bl_effective_exclude <partition>             # 產生完整 --exclude= 值 (合併 3 來源 + cap)
#   bl_local_list | bl_live_list | bl_global_list # 單獨取各來源
# ==============================================================================

: "${BLACKLIST_TTL_SEC:=86400}"
: "${BLACKLIST_MAX_PCT:=50}"
: "${BAD_NODES_FILE:=restart/bad_nodes}"
: "${GLOBAL_BAD_FILE:=$HOME/.bad_nodes_global}"

# ─────────────────────────────────────────────────────────────────────────
# 內部 helper: 把 Slurm state 字串分類
# 健康狀態 (節點可用): IDLE / MIXED / ALLOCATED / COMPLETING
# 其餘都視為不健康 (含 UNKNOWN, RESERVED 等邊界狀態 — 保守)
# ─────────────────────────────────────────────────────────────────────────
_bl_state_is_healthy() {
    local s; s=$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')
    case "$s" in
        IDLE*|ALLOC*|MIX*|COMP*) return 0 ;;
        *)                       return 1 ;;
    esac
}

# ─────────────────────────────────────────────────────────────────────────
# NCHC 即時查詢
# ─────────────────────────────────────────────────────────────────────────
# NCHC 認為目前不健康的節點 (sinfo -R: 有 REASON 的節點 = drain/down/fail/maint)
bl_nchc_unhealthy() {
    sinfo -h -R -o "%n" 2>/dev/null | grep -v '^[[:space:]]*$' | sort -u
}

# 查單一節點的 NCHC 即時 state; 輸出 state 字串 (空=查不到)
bl_nchc_node_state() {
    local node="$1"
    [ -z "$node" ] && return 1
    sinfo -h -N -n "$node" -o "%T" 2>/dev/null | sort -u | head -n1
}

# ─────────────────────────────────────────────────────────────────────────
# GC: 刪除超過 TTL 的 entry, 同時把舊格式 (純 hostname) 升級為新格式
# ─────────────────────────────────────────────────────────────────────────
bl_gc() {
    local file="${1:-$BAD_NODES_FILE}"
    [ -f "$file" ] || return 0
    local now cutoff tmp kept=0 expired=0 upgraded=0
    now=$(date +%s)
    cutoff=$(( now - BLACKLIST_TTL_SEC ))
    tmp="${file}.gc.$$"
    : > "$tmp"
    while IFS= read -r line; do
        case "$line" in ''|'#'*) continue ;; esac
        if printf '%s' "$line" | grep -q $'\t'; then
            # 新格式
            local node ts
            node=$(printf '%s' "$line" | cut -f1)
            ts=$(printf '%s' "$line" | cut -f2)
            if [ -z "$node" ]; then
                continue
            fi
            if ! printf '%s' "$ts" | grep -qE '^[0-9]+$'; then
                ts=0  # 損壞時間戳 → 視為過期
            fi
            if [ "$ts" -lt "$cutoff" ]; then
                expired=$((expired+1))
                continue
            fi
            printf '%s\n' "$line" >> "$tmp"
            kept=$((kept+1))
        else
            # 舊格式: <node>  → 升級為 <node>\t<now>\tlegacy\tmigration
            local node; node=$(printf '%s' "$line" | tr -d '[:space:]')
            [ -z "$node" ] && continue
            printf '%s\t%d\tlegacy\tmigration\n' "$node" "$now" >> "$tmp"
            kept=$((kept+1))
            upgraded=$((upgraded+1))
        fi
    done < "$file"
    mv -f "$tmp" "$file"
    if [ "$expired" -gt 0 ] || [ "$upgraded" -gt 0 ]; then
        printf '[blacklist] GC: kept=%d expired=%d upgraded=%d (TTL=%ss)\n' \
               "$kept" "$expired" "$upgraded" "$BLACKLIST_TTL_SEC" >&2
    fi
}

# ─────────────────────────────────────────────────────────────────────────
# 實時同步: 查 NCHC 現在是否把本地黑名單節點恢復為健康
#   - NCHC state = idle/mix/alloc/completing  → 從本地黑名單釋放
#   - NCHC state = drain/down/fail/maint/... → 保留
#   - NCHC 查不到 (臨時故障) → 保留 (conservative)
# ─────────────────────────────────────────────────────────────────────────
bl_sync_with_nchc() {
    local file="${1:-$BAD_NODES_FILE}"
    [ -f "$file" ] || return 0
    bl_gc "$file"
    local tmp kept=0 released=0
    tmp="${file}.sync.$$"
    : > "$tmp"
    # 先一次撈所有節點狀態到 cache, 避免對 N 個節點 call N 次 sinfo
    local cache="${file}.nchc_cache.$$"
    sinfo -h -N -o "%n %T" 2>/dev/null | sort -u > "$cache"
    while IFS= read -r line; do
        case "$line" in ''|'#'*) continue ;; esac
        local node; node=$(printf '%s' "$line" | cut -f1)
        [ -z "$node" ] && continue
        # 從 cache 查 state
        local state; state=$(awk -v n="$node" '$1==n {print $2; exit}' "$cache")
        if [ -z "$state" ]; then
            printf '%s\n' "$line" >> "$tmp"
            kept=$((kept+1))
        elif _bl_state_is_healthy "$state"; then
            local src; src=$(printf '%s' "$line" | cut -f4)
            if [ "$src" = "manual-blacklist" ]; then
                printf '%s\n' "$line" >> "$tmp"
                kept=$((kept+1))
                printf '[blacklist] KEEP %s (NCHC state=%s, manual-blacklist 不自動釋放)\n' "$node" "$state" >&2
            else
                released=$((released+1))
                printf '[blacklist] RELEASE %s (NCHC state=%s, 本地黑名單釋放)\n' "$node" "$state" >&2
            fi
        else
            printf '%s\n' "$line" >> "$tmp"
            kept=$((kept+1))
        fi
    done < "$file"
    rm -f "$cache"
    mv -f "$tmp" "$file"
    if [ "$released" -gt 0 ]; then
        printf '[blacklist] SYNC: kept=%d released=%d\n' "$kept" "$released" >&2
    fi
}

# ─────────────────────────────────────────────────────────────────────────
# 加入黑名單 — 先驗 NCHC: NCHC 說健康就拒絕加入 (避免本地 bug 污染黑名單)
# 回傳: 0=加入成功, 1=參數錯, 2=NCHC 說節點健康所以拒絕加入
# ─────────────────────────────────────────────────────────────────────────
bl_add() {
    local node="$1" reason="${2:-unspecified}" src="${3:-unknown}"
    [ -z "$node" ] && return 1
    local ts; ts=$(date +%s)
    local state; state=$(bl_nchc_node_state "$node")

    if [ -z "$state" ]; then
        printf '[blacklist] WARN %s sinfo 查不到狀態, 保守起見加入 (reason=%s src=%s)\n' \
               "$node" "$reason" "$src" >&2
    elif _bl_state_is_healthy "$state"; then
        printf '[blacklist] SKIP %s NCHC state=%s (reason=%s) -- NCHC 認為節點健康, 不加入 (疑似本地程式問題)\n' \
               "$node" "$state" "$reason" >&2
        return 2
    else
        printf '[blacklist] CONFIRM %s NCHC state=%s (reason=%s src=%s) -> 加入黑名單\n' \
               "$node" "$state" "$reason" "$src" >&2
    fi

    mkdir -p "$(dirname "$BAD_NODES_FILE")"
    touch "$BAD_NODES_FILE"
    # 若 node 已存在 (任一格式), 先刪舊條目再 append (更新時間戳 + reason)
    local tmp="${BAD_NODES_FILE}.add.$$"
    awk -F'\t' -v n="$node" '
        $1 == n { next }                              # 新格式: 比 cut -f1
        NF == 1 && $0 == n { next }                   # 舊格式: 整行就是 hostname
        { print }
    ' "$BAD_NODES_FILE" > "$tmp" 2>/dev/null || true
    printf '%s\t%d\t%s\t%s\n' "$node" "$ts" "$reason" "$src" >> "$tmp"
    mv -f "$tmp" "$BAD_NODES_FILE"
    return 0
}

# ─────────────────────────────────────────────────────────────────────────
# 三個來源的列表 (comma-separated, 空字串=該來源無節點)
# ─────────────────────────────────────────────────────────────────────────
bl_local_list() {
    local file="${1:-$BAD_NODES_FILE}"
    bl_gc "$file" 2>/dev/null
    [ -f "$file" ] || return 0
    awk -F'\t' 'NF>=1 && $1!="" && $1!~/^#/ {print $1}' "$file" | sort -u | paste -sd,
}

bl_global_list() {
    [ -f "$GLOBAL_BAD_FILE" ] || return 0
    grep -v '^[[:space:]]*$' "$GLOBAL_BAD_FILE" 2>/dev/null | sort -u | paste -sd,
}

bl_live_list() {
    bl_nchc_unhealthy | paste -sd,
}

# ─────────────────────────────────────────────────────────────────────────
# 取 partition 總節點數 (用於 cap)
# ─────────────────────────────────────────────────────────────────────────
bl_partition_size() {
    local partition="$1"
    [ -z "$partition" ] && { echo 0; return; }
    sinfo -h -p "$partition" -o "%D" 2>/dev/null | awk '{s+=$1} END {print s+0}'
}

# ─────────────────────────────────────────────────────────────────────────
# 產生最終 --exclude 的值 (不含 "--exclude=" 前綴)
# 合併: local (TTL-valid + NCHC-synced) + global + NCHC live-unhealthy
# 超過 partition MAX_PCT 時截斷, 優先保留 NCHC live (真實壞) + 最新 local 條目
# 用法: EX_LIST=$(bl_effective_exclude <partition>)
# ─────────────────────────────────────────────────────────────────────────
bl_effective_exclude() {
    local partition="$1"
    # bl_sync_with_nchc 只寫 stderr (diagnostic), 不寫 stdout,
    # 所以不能用 2>&1 壓掉 — 否則會把 [blacklist] RELEASE/WARN 全部吞掉.
    bl_sync_with_nchc "$BAD_NODES_FILE" || true

    local local_bad global_bad live_bad merged n_merged
    local_bad=$(bl_local_list)
    global_bad=$(bl_global_list)
    live_bad=$(bl_live_list)
    merged=$( { printf '%s\n' "$local_bad"; printf '%s\n' "$global_bad"; printf '%s\n' "$live_bad"; } \
              | tr ',' '\n' | grep -v '^[[:space:]]*$' | sort -u )
    n_merged=$(printf '%s\n' "$merged" | grep -cv '^[[:space:]]*$')
    [ "$n_merged" -eq 0 ] && { printf ''; return; }

    # Cap 檢查
    if [ -n "$partition" ]; then
        local total cap
        total=$(bl_partition_size "$partition")
        if [ "$total" -gt 0 ]; then
            cap=$(( total * BLACKLIST_MAX_PCT / 100 ))
            [ "$cap" -lt 1 ] && cap=1
            if [ "$n_merged" -gt "$cap" ]; then
                printf '[blacklist] WARN: 合併後 %d 節點待排除, 超過 partition %s 的 %d%% (total=%d cap=%d)\n' \
                       "$n_merged" "$partition" "$BLACKLIST_MAX_PCT" "$total" "$cap" >&2
                printf '[blacklist]       截斷至 %d 個 (優先保留 NCHC live, 再按時間戳取最新 local)\n' "$cap" >&2
                # Step 1: 保留 NCHC live (這些是 NCHC 當下真的壞)
                local keep_live; keep_live=$(printf '%s\n' "$live_bad" | tr ',' '\n' | grep -v '^[[:space:]]*$' | sort -u)
                local n_live; n_live=$(printf '%s\n' "$keep_live" | grep -cv '^[[:space:]]*$')
                local picked
                if [ "$n_live" -ge "$cap" ]; then
                    picked=$(printf '%s\n' "$keep_live" | head -n "$cap")
                else
                    # Step 2: local file 按時間戳降序, 取前 (cap - n_live) 個
                    local remain=$(( cap - n_live ))
                    local extra=""
                    if [ -f "$BAD_NODES_FILE" ]; then
                        extra=$(awk -F'\t' 'NF>=2 && $1!="" {print $2"\t"$1}' "$BAD_NODES_FILE" \
                                | sort -k1,1 -n -r \
                                | awk -F'\t' '{print $2}')
                    fi
                    picked=$( { printf '%s\n' "$keep_live"; printf '%s\n' "$extra"; } \
                              | grep -v '^[[:space:]]*$' | awk '!seen[$0]++' | head -n "$cap")
                fi
                printf '%s\n' "$picked" | paste -sd,
                return
            fi
        fi
    fi

    printf '%s\n' "$merged" | paste -sd,
}
