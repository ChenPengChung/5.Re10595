#!/bin/bash
# ==============================================================================
# submit_dispatcher.sh   —  方案 B: 跨 partition 自動派工 daemon
# ==============================================================================
# 功能:
#   每次 chain round 結束後 (jobscript 因 DISPATCHER_ACTIVE 存在而不 self-resubmit),
#   由本 daemon 用 sbatch --test-only 比較可用 partition ETA, 投下一 round.
#
# 啟動方式 (不要直接呼叫本腳本, 請用 dispatcher_start.sh):
#   ./dispatcher_start.sh   # 背景啟動 + 寫 DISPATCHER_ACTIVE sentinel
#   ./dispatcher_stop.sh    # 停止 + 移除 sentinel
#   ./dispatcher_status.sh  # 看目前狀態
#
# 停止條件 (任一滿足就 clean-exit):
#   1. restart/STOP_CHAIN 存在       (自然收斂 / 使用者下停止訊號)
#   2. 最後一輪 jobscript exit 42     (unavoidable error, 永遠不重投)
#   3. STOP_DISPATCHER 存在           (dispatcher_stop.sh 觸發)
#   4. 無法查到有效 partition 且 chain 無 active job (fail-safe)
#
# 跨 partition 的 binary 管理:
#   GB200 (aarch64/sm_100) 與 H200 (x86_64/sm_90) 的 a.out 不能互換.
#   本 daemon 會在 submit 前做:  cp a.out.<CLUSTER>  a.out
#   所以使用者必須事先產生 a.out.GB200 + a.out.H200:
#     bash build_and_submit.sh.GB200 --build-only && cp a.out a.out.GB200
#     bash build_and_submit.sh.H200  --build-only && cp a.out a.out.H200
#   若只有一個 arch 的 binary 存在, dispatcher 只投對應 arch 的 partition.
# ==============================================================================

set -uo pipefail

# ── [方案 A path discipline] ──
# 本 daemon 與所有 chain_code 同伴 script 一樣, 必須以 PROJECT_ROOT 為 cwd
# (restart/, a.out, DISPATCHER_ACTIVE sentinel 等都在 PROJECT_ROOT).
_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || realpath "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
CHAIN_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
PROJECT_ROOT="$(cd "$CHAIN_DIR/.." && pwd)"
cd "$PROJECT_ROOT" || { echo "[dispatcher] FATAL: cannot cd to $PROJECT_ROOT" >&2; exit 1; }

# ─────────────────────────────────────────────────────────────────────────
# [SINGLE-HEAD] 載入 HEAD.lockdir 共用函式庫
# ─────────────────────────────────────────────────────────────────────────
if [ -f "$CHAIN_DIR/tools/head_lock_lib.sh" ]; then
    . "$CHAIN_DIR/tools/head_lock_lib.sh"
else
    echo "[dispatcher] FATAL: $CHAIN_DIR/tools/head_lock_lib.sh 不存在, 無法執行 Single-Head 機制" >&2
    exit 1
fi

# ─────────────────────────────────────────────────────────────────────────
# [BLACKLIST-LIB] 載入黑名單管理函式庫 (TTL + NCHC sync + cap)
# ─────────────────────────────────────────────────────────────────────────
if [ -f "$CHAIN_DIR/tools/blacklist_lib.sh" ]; then
    . "$CHAIN_DIR/tools/blacklist_lib.sh"
else
    echo "[dispatcher] FATAL: $CHAIN_DIR/tools/blacklist_lib.sh 不存在, 無法做 blacklist TTL/sync" >&2
    exit 1
fi

# [PARTITION-LIB] partition walltime 映射 (submit_round 需要 --time= 對應 partition max)
if [ -f "$CHAIN_DIR/tools/partition_lib.sh" ]; then
    . "$CHAIN_DIR/tools/partition_lib.sh"
fi

# ─────────────────────────────────────────────────────────────────────────
# 可調參數
# ─────────────────────────────────────────────────────────────────────────
POLL_INTERVAL="${POLL_INTERVAL:-30}"       # 秒; 查 squeue/sinfo 間隔
PROBE_RESUBMIT_DELAY="${PROBE_RESUBMIT_DELAY:-10}"  # 秒; sbatch 後等多久再回頭查
LOG_FILE="${LOG_FILE:-restart/dispatcher.log}"
SENTINEL="DISPATCHER_ACTIVE"
STOP_SENTINEL="STOP_DISPATCHER"
# [SELFTEST sentinel 隔離] DISPATCHER_SELFTEST 乾跑不可碰真正的 DISPATCHER_ACTIVE/STOP_DISPATCHER
# (否則 selftest 啟動寫 PID、退出又清掉 → 把真 daemon 的 sentinel 洗掉)。改用拋棄式檔。
if [ "${DISPATCHER_SELFTEST:-0}" = "1" ]; then
    SENTINEL="$(mktemp 2>/dev/null || echo /tmp/disp_selftest.$$.sentinel)"
    STOP_SENTINEL="$(mktemp 2>/dev/null || echo /tmp/disp_selftest_stop.$$)"
fi
ACCOUNT="${ACCOUNT:-MST114348}"   # [2026-06-10] 計畫改 MST114348 (與 Edit6/Edit8 的 MST115169 cap 脫鉤; fairshare 較佳)

# [P0 TRAP #2 FIX] 兩邊都忙時連續沒空位多少次後, 觸發明確停機 (避免隱形無限 sleep)
# [never-idle] 放寬: 預設 480 次 × POLL_INTERVAL(30s) = 4 小時才放棄 (原 60=30 分太短)。
# 只在「所有候選連 sbatch --test-only 都拿不到可解析 ETA」(controller down / QoS reject / binary 缺)
# 才會累加; 正常 PENDING 不會走到這裡。每輪都會 log 一行當 heartbeat, operator 不會失明。
NOCAPACITY_LIMIT="${NOCAPACITY_LIMIT:-480}"
NOCAPACITY_SENTINEL="restart/STOP_NOCAPACITY"

# Partition 候選清單: <ARCH>:<partition>
# [2026-06-05 NEW 政策 — 計畫 MST115169]
#   partition@jp 自由跳轉四組: 8gpus@32 / 16gpus@32 / 32gpus@32 / 64gpus@64。
#     jp 不再等於 partition 名數字: p_8gpus/p_16gpus/p_32gpus MaxTRESPA=gres/gpu=32 (皆容得下 jp=32),
#     p_64gpus=64 (容 jp=64)。故 jp∈{32,64}: jp=32→{8,16,32,64}gpus, jp=64→64gpus only。
#   暫時鎖定 64gpus@64 (見 restart/LOCK_COMBO, 缺檔則自由跳轉); 64gpus@64 最高、優先。
# - H200 partitions 共用 a.out.H200 / jobscript_chain.slurm.H200; pick_cluster 2-tier 政策
#   自動「有容量→最早 ETA / 全 pending→最短 ETA」, 各 partition 的 --time= 由 partition_walltime() 決定。
# - dev(cap4) 不列入 (jp 最小 32 已超其 cap); normal/4nodes 已 inactive。
# - GB200 (跨架構) 預設不在候選內 (本專案鎖 H200); 如需啟用以 env 覆寫 PARTITION_CANDIDATES
#   (jobscript_chain.slurm.GB200 已同步為 jp=64 / 16 nodes)。
PARTITION_CANDIDATES_RAW="${PARTITION_CANDIDATES:-H200:64gpus H200:32gpus H200:16gpus H200:8gpus}"
read -r -a PARTITION_CANDIDATES <<< "$PARTITION_CANDIDATES_RAW"

# Option C ETA-compare 的容忍區間 (秒). 兩邊 ETA 差距在此範圍內視為平手,
# 平手時依 PARTITION_CANDIDATES 先到先選, 避免多邊都 idle 時因秒級抖動反覆切換.
TIE_TOLERANCE_SEC="${TIE_TOLERANCE_SEC:-30}"

# ─────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────
mkdir -p restart/
# [BUGFIX 2026-04-22] 舊版 log() 寫 stdout, 導致 NEXT_CLUSTER="$(pick_cluster)"
#   把內部的 "略過: a.out..." log 一起吞進變數 → 邏輯毀壞
#   (畸形輸出範例: 選中: [timestamp] [dispatcher]   [GB200] 略過...).
# 新規則:
#   1) 一律直接 append 到 LOG_FILE (不經 stdout, 絕不污染 $(…) 捕獲).
#   2) 前景 (fd 2 是 tty) 才多印 stderr 給使用者即時看; 背景 nohup 2>&1 已合進
#      LOG_FILE, 跳過 stderr 避免雙寫.
#   3) 絕不寫 stdout → 所有用 $(…) 回傳的 helper (pick_cluster 等) 永遠安全.
log() {
    local ts msg
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    msg="[${ts}] [dispatcher] $*"
    printf '%s\n' "$msg" >> "$LOG_FILE" 2>/dev/null
    if [ -t 2 ]; then
        printf '%s\n' "$msg" >&2
    fi
}

# ─────────────────────────────────────────────────────────────────────────
# Sentinel / 狀態檔
# ─────────────────────────────────────────────────────────────────────────
on_exit() {
    local rc=$?
    # [ownership guard] 只有當 sentinel 仍記錄「我」的 PID 才清除 — 避免一個正在退出的舊 daemon
    # 洗掉後繼 daemon 剛寫入的 sentinel (重啟時的 race), 造成 DISPATCHER_ACTIVE 莫名變空。
    if [ "$(tr -dc 0-9 < "$SENTINEL" 2>/dev/null)" = "$$" ]; then
        rm -f "$SENTINEL" 2>/dev/null
        log "dispatcher 退出 (rc=$rc); 清理自己的 sentinel"
    else
        log "dispatcher 退出 (rc=$rc); sentinel 已屬他人, 不清"
    fi
    rm -f "$STOP_SENTINEL" 2>/dev/null
    exit $rc
}
trap on_exit EXIT INT TERM

# 寫 PID 到 sentinel (給 dispatcher_status.sh 讀)
echo "$$" > "$SENTINEL"
log "═════════════════════════════════════════════════════════════════════════"
log "dispatcher 啟動 (PID=$$)"
log "POLL_INTERVAL=$POLL_INTERVAL s, partition candidates: ${PARTITION_CANDIDATES[*]}"
log "case 目錄: $(pwd)"
log "═════════════════════════════════════════════════════════════════════════"

# ─────────────────────────────────────────────────────────────────────────
# Helper: partition → jobscript / build_script 映射
# ─────────────────────────────────────────────────────────────────────────
cluster_jobscript() {
    # 回傳 jobscript 絕對路徑 (以 CHAIN_DIR 為基準) — 不依賴 cwd 或 root-level symlink
    case "$1" in
        GB200) echo "$CHAIN_DIR/jobscript_chain.slurm.GB200" ;;
        H200)  echo "$CHAIN_DIR/jobscript_chain.slurm.H200" ;;
        *) return 1 ;;
    esac
}

cluster_partition() {
    local js
    js="$(cluster_jobscript "$1")" || return 1
    awk -F= '/^#SBATCH[[:space:]]+--partition=/{gsub(/^[[:space:]]+|[[:space:]]+$/,"",$2); print $2; exit}' "$js"
}

target_cluster() {
    case "$1" in
        *@*) printf '%s\n' "${1%%@*}" ;;
        *)   printf '%s\n' "$1" ;;
    esac
}

target_partition() {
    local target="$1" cluster part
    cluster="$(target_cluster "$target")"
    case "$target" in
        *@*) part="${target#*@}" ;;
        *)   part="$(cluster_partition "$cluster")" || return 1 ;;
    esac
    printf '%s\n' "$part"
}

# 檢查 arch-specific binary 是否存在
cluster_binary_ready() {
    [ -s "a.out.$1" ]
}

# ─────────────────────────────────────────────────────────────────────────
# Helper: 查 partition 是否有足夠 idle 資源可立即投
# 輸出: 0 = 有空閒 (可投), 1 = 忙碌 (所有 node allocated), 2 = down/drain
# ─────────────────────────────────────────────────────────────────────────
check_partition_idle() {
    local cluster="$1"
    local part
    part="$(cluster_partition "$cluster")" || return 2

    # sinfo %a=avail (up/down), %D=node count, %t=state, %T=state long
    # 我們要有 "idle" 或 "mix" 狀態的 node 才算可投 (mix=部分 free)
    local sinfo_out
    sinfo_out="$(sinfo -h -p "$part" -o '%a %t %D' 2>/dev/null)"
    if [ -z "$sinfo_out" ]; then
        log "  [$cluster] sinfo 查不到 $part partition"
        return 2
    fi

    # 檢查有沒有至少一個 up+idle 或 up+mix 的 node
    local has_idle=0
    while read -r avail state nodes; do
        [ -z "$avail" ] && continue
        if [ "$avail" = "up" ]; then
            case "$state" in
                idle*|mix*|IDLE*|MIX*) has_idle=1 ;;
            esac
        fi
    done <<< "$sinfo_out"

    if [ "$has_idle" -eq 1 ]; then
        return 0
    else
        return 1
    fi
}

# ─────────────────────────────────────────────────────────────────────────
# Helper: 本 chain 是否已有 active job 在 queue (running/pending)
#   - 讀 restart/chain_jobid 拿最近的 jobid
#   - squeue 確認該 jobid 狀態
# ─────────────────────────────────────────────────────────────────────────
chain_has_active_job() {
    local cur_id
    cur_id="$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]')"
    [ -z "$cur_id" ] && return 1

    local state
    state="$(squeue -h -j "$cur_id" -o '%T' 2>/dev/null | head -1)"
    case "$state" in
        RUNNING|PENDING|CONFIGURING|COMPLETING|RESIZING|SUSPENDED)
            return 0 ;;
        *)
            return 1 ;;
    esac
}

# 取得本 chain 最後一輪 job 的 exit code (若已結束)
chain_last_exit_code() {
    local cur_id
    cur_id="$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]')"
    [ -z "$cur_id" ] && { echo ""; return; }

    # [BUGFIX] sacct ExitCode 格式是 "<exit_code>:<signal>" (例如 "1:0" = exit 1, signal 0).
    # 舊版註解寫反了 ("<signal>:<exit_code>"), 所以 "${ec##*:}" 取冒號"後"段, 其實取到 signal.
    # 結果 jobscript 的 "exit 1" (FATAL) 被回報成 "exit 0" (自然停止) → dispatcher 不停鏈.
    # 正確: 用 "${ec%%:*}" 取冒號"前"段才是真實 exit code.
    local ec
    ec="$(sacct -X -n -j "$cur_id" -o ExitCode 2>/dev/null | head -1 | tr -d '[:space:]')"
    echo "${ec%%:*}"
}

# ─────────────────────────────────────────────────────────────────────────
# Helper: 選下一個要投的 target (Option D: partition ETA-compare)
#   設計原則:
#     - 永遠對「binary 存在」的候選 partition 呼叫 sbatch --test-only 取 ETA
#     - ETA 較早的 target 勝出 (idle ≈ now, 忙碌 ≈ 未來時間)
#     - 差距 ≤ TIE_TOLERANCE_SEC 視為平手 → 用 PARTITION_CANDIDATES 先到先選
#     - 全部查不到 ETA 或都缺 binary → 回 "" 走 no-capacity 路徑
#   [DEFENSIVE] log 全部走 stderr (>&2); 唯一 stdout 輸出 = ARCH@partition.
# ─────────────────────────────────────────────────────────────────────────
pick_cluster() {
    local entry c part target
    # [LOCK 臨時開關 2026-06-03] restart/LOCK_COMBO 存在 → 鎖定 partition(繞過矩陣評估).
    #   還原自由跳轉(partition&&jp): rm restart/LOCK_COMBO + ./run dispatcher 重啟 (見 CLAUDE.md「還原回自由跳轉」).
    if [ -f restart/LOCK_COMBO ]; then
        local _lc _ltgt; _lc="$(tr -d '\r\n' < restart/LOCK_COMBO 2>/dev/null)"; _ltgt="${_lc#* }"
        if [ -n "$_ltgt" ] && [ "$_ltgt" != "$_lc" ]; then
            log "  pick_cluster: [LOCK] LOCK_COMBO 臨時鎖定 partition → $_ltgt (還原: rm restart/LOCK_COMBO)" >&2
            echo "$_ltgt"; return 0
        fi
    fi
    # 收集所有「可投」候選: 平行陣列 (target / ETA epoch / walltime 秒)
    local -a _T=() _E=() _W=()
    # 目前 jp(= 要投的 GPU 數), 供 MaxTRESPerAccount 過濾
    local cur_jp; cur_jp="$(grep -E '^#define[[:space:]]+jp[[:space:]]+[0-9]+' variables.h 2>/dev/null | grep -oE '[0-9]+' | head -1)"
    cur_jp="${cur_jp:-0}"

    log "  pick_cluster: 2-tier 政策 (有容量→最長walltime; 全pending→最短ETA, tol=${TIE_TOLERANCE_SEC}s; jp=$cur_jp)" >&2
    for entry in "${PARTITION_CANDIDATES[@]}"; do
        c="${entry%%:*}"
        part="${entry#*:}"
        if [ -z "$c" ] || [ -z "$part" ] || [ "$c" = "$part" ]; then
            log "    [$entry] 略過: 候選格式需為 ARCH:partition" >&2
            continue
        fi
        target="${c}@${part}"
        if ! cluster_binary_ready "$c"; then
            log "    [$target] 略過: a.out.$c 不存在" >&2
            continue
        fi
        # MaxTRESPerAccount 過濾: 此 partition 每帳號 GPU 上限容不下目前 jp → 永遠 PENDING, 跳過
        local _gcap; _gcap="$(partition_gpu_cap_per_account "$part" 2>/dev/null || echo 100000)"
        if [ "$cur_jp" -gt "$_gcap" ]; then
            log "    [$target] 略過: jp=$cur_jp > 該 partition 每帳號 GPU 上限 $_gcap (MaxGRESPerAccount)" >&2
            continue
        fi
        # [FIX 2026-06-03] 即時 headroom: 靜態 cap 容得下, 但帳號此刻在此 partition 已被(同帳號其他
        #   user 的 RUNNING job)占用 → 即投仍會 PENDING. sbatch --test-only 盲於 MaxTRESPerAccount,
        #   故 pick_cluster 須在此額外擋掉(對齊 jp_partition_eta:730-734), 否則會選滿的 4nodes 而 PENDING,
        #   永遠到不了空閒的 dev. (這是 dev|16 跑不起來的第四個根因.)
        local _ginuse; _ginuse="$(partition_account_gpu_inuse "$part" 2>/dev/null || echo 0)"
        if [ "$cur_jp" -gt $(( _gcap - _ginuse )) ]; then
            log "    [$target] 略過: cap=$_gcap 容得下但帳號此刻已用 ${_ginuse}GPU, 剩 $(( _gcap - _ginuse ))<$cur_jp → 即投會 PENDING(即時占用非靜態cap)" >&2
            continue
        fi
        local js; js="$(cluster_jobscript "$c")" || continue
        if [ ! -f "$js" ]; then
            log "    [$target] 略過: jobscript $js 不存在" >&2
            continue
        fi
        local eta; eta="$(_pick_cluster_eta_epoch "$js" "$part")"
        if [ "$eta" -lt 0 ]; then
            log "    [$target] ETA 查詢失敗/不可投 (sbatch --test-only 無解析結果)" >&2
            continue
        fi
        local wt wsec; wt="$(partition_walltime "$part")"; wsec="$(walltime_to_sec "${wt:-}")"
        local now; now="$(date +%s)"
        local wait_s=$((eta - now)); [ "$wait_s" -lt 0 ] && wait_s=0
        log "    [$target] ETA wait ~= $(_pick_cluster_fmt_wait "$wait_s")  walltime=${wt:-?}(${wsec}s)" >&2
        _T+=("$target"); _E+=("$eta"); _W+=("$wsec")
    done

    local n=${#_T[@]}
    if [ "$n" -eq 0 ]; then
        log "  pick_cluster: 全部候選都不可投 (binary 缺 / sbatch --test-only 全部失敗) -> no-capacity" >&2
        echo ""
        return 1
    fi

    local now soon i bi
    now="$(date +%s)"
    soon=$(( now + ${PARTITION_START_SOON_SEC:-120} ))

    # ── 規則1: 在「可即起 (ETA<=now+SOON)」候選中, 選 ETA 最早(抓空閒) ──
    #   [PREF 2026-06-03] 不看 walltime: chain 自動續投機制不受 walltime 影響,
    #   故短 walltime(dev 1h) 與長 walltime(normal 2d) 同等對待, 純粹抓最早能起的(最空閒).
    bi=-1
    for (( i=0; i<n; i++ )); do
        [ "${_E[$i]}" -le "$soon" ] || continue
        if   [ "$bi" -lt 0 ]; then bi=$i
        elif [ "${_E[$i]}" -lt "${_E[$bi]}" ]; then bi=$i
        fi
    done
    if [ "$bi" -ge 0 ]; then
        log "  pick_cluster: [規則1] 有容量可即起 → 抓最早 ETA(不看 walltime): ${_T[$bi]}" >&2
        echo "${_T[$bi]}"
        return 0
    fi

    # ── 規則2: 全部都得排隊 → 選 ETA 最短 (早 TIE_TOLERANCE_SEC 以上才覆蓋) ──
    bi=0
    for (( i=1; i<n; i++ )); do
        if [ $(( ${_E[$bi]} - ${_E[$i]} )) -gt "$TIE_TOLERANCE_SEC" ]; then bi=$i; fi
    done
    log "  pick_cluster: [規則2] 全部 pending → 選最短 ETA: ${_T[$bi]}" >&2
    echo "${_T[$bi]}"
    return 0
}

# 用 sbatch --test-only 查單一 jobscript 的預期開始 epoch.
# 輸出: >=0 epoch (成功), -1 (失敗/無解析)
_pick_cluster_eta_epoch() {
    local js="$1" part="${2:-}" out eta_str
    command -v sbatch >/dev/null 2>&1 || { echo -1; return; }
    # [WALLTIME-FIX] --test-only 也帶 --time= 讓 SLURM backfill 用正確 walltime 排程
    local wt="" time_arg=""
    if [ -n "$part" ] && type partition_walltime >/dev/null 2>&1; then
        wt="$(partition_walltime "$part")"
    fi
    [ -n "$wt" ] && time_arg="--time=$wt"
    if [ -n "$part" ]; then
        out="$(sbatch --test-only --partition="$part" $time_arg "$js" 2>&1 || true)"
    else
        out="$(sbatch --test-only $time_arg "$js" 2>&1 || true)"
    fi
    if   echo "$out" | grep -qE "to start at[[:space:]]+[0-9]{4}-"; then
        eta_str="$(echo "$out" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+' | head -n1)"
        date -d "$eta_str" +%s 2>/dev/null || echo -1
    elif echo "$out" | grep -qE "allocation .*can be allocated|to start immediately|to start now"; then
        date +%s
    else
        echo -1
    fi
}

# 把等待秒數格式化成可讀字串
_pick_cluster_fmt_wait() {
    local w="$1"
    if   [ "$w" -le 60 ];   then echo "now"
    elif [ "$w" -lt 3600 ]; then echo "~$((w/60))min"
    else                         echo "~$((w/3600))h$((w%3600/60))m"
    fi
}

# ─────────────────────────────────────────────────────────────────────────
# Helper: 對選定的 cluster 做一次 sbatch
# ─────────────────────────────────────────────────────────────────────────
submit_round() {
    local target="$1"
    local cluster part
    cluster="$(target_cluster "$target")"
    part="$(target_partition "$target")" || return 1
    local jobscript
    jobscript="$(cluster_jobscript "$cluster")" || return 1

    if [ ! -f "$jobscript" ]; then
        log "ERROR: 找不到 $jobscript"
        return 2
    fi

    # ═════ [LAYER 2] pre-submit HEAD.lockdir fast-path ═════
    # 最快速的一道防線: 直接看 HEAD.lockdir 是否被人佔. owner 活著就放棄本輪.
    # 這條 single source of truth 讓 Single-Head invariant 成立 (一格資料夾一顆 head).
    if [ -d "$HEAD_LOCK_DIR" ]; then
        local h_state h_jid h_live
        h_state="$(grep '^state=' "$HEAD_LOCK_DIR/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
        h_jid="$(grep '^jobid='   "$HEAD_LOCK_DIR/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
        h_live="$(_head_squeue_state "$h_jid")"
        case "$h_state" in
            SUBMITTING)
                log "[LAYER 2] HEAD.lockdir 被 submitter 鎖住 (state=SUBMITTING), 放棄本次投遞"
                return 4
                ;;
            PENDING|RUNNING)
                case "$h_live" in
                    PENDING|RUNNING|CONFIGURING|COMPLETING|RESIZING|SUSPENDED)
                        log "[LAYER 2] HEAD.lockdir 被 jobid=$h_jid 持有 (state=$h_state squeue=$h_live), 放棄本次投遞"
                        return 4
                        ;;
                esac
                ;;
        esac
        # 進到這裡代表 state 不合法或 squeue 查不到 → 交由 acquire_head_lock 內的 stale 清理
    fi

    # ═════ [LAYER 2b] 向後相容: 舊 chain_jobid / RUNNING.lockdir ═════
    # 保留這兩道檢查以防 legacy 檔案殘留 (升級期間過渡用).
    local cur_id
    cur_id="$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]')"
    if [[ "$cur_id" =~ ^[0-9]+$ ]]; then
        local cur_state
        cur_state="$(squeue -h -j "$cur_id" -o '%T' 2>/dev/null | tr -d '[:space:]')"
        case "$cur_state" in
            PENDING|RUNNING|CONFIGURING|COMPLETING)
                log "[LAYER 2b] legacy chain_jobid=$cur_id 仍 active (state=$cur_state), 放棄本次投遞"
                return 4
                ;;
        esac
    fi
    if [ -d restart/RUNNING.lockdir ]; then
        local lk_id lk_state
        lk_id="$(grep '^jobid=' restart/RUNNING.lockdir/owner 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
        lk_state="$(squeue -h -j "$lk_id" -o '%T' 2>/dev/null | tr -d '[:space:]')"
        case "$lk_state" in
            PENDING|RUNNING|CONFIGURING|COMPLETING)
                log "[LAYER 2b] legacy RUNNING.lockdir 被 jobid=$lk_id 持有 (state=$lk_state), 放棄本次投遞"
                return 4
                ;;
        esac
    fi
    # ═════════════════════════════════════════════

    # ═════ [SINGLE-HEAD] 先取 HEAD.lockdir, 再呼叫 sbatch ═════
    # 若取不到代表 (a) 別的 submitter 正在搶 / (b) 有活 job 正在 queue.
    # 兩種情況都回 rc=4 讓 main loop 下輪再試 — 符合使用者決定 A+(a).
    if ! acquire_head_lock "dispatcher-$cluster"; then
        log "[SINGLE-HEAD] acquire_head_lock 失敗 (HEAD.lockdir 已被佔), 放棄本次投遞"
        return 4
    fi
    log "[SINGLE-HEAD] ✓ 取得 HEAD.lockdir, state=SUBMITTING, 準備 sbatch"

    log "▷ 切換到 $target: cp a.out.$cluster -> a.out"
    if ! cp -f "a.out.$cluster" "a.out"; then
        log "ERROR: cp a.out.$cluster 失敗, 釋放 HEAD.lockdir"
        release_head_lock
        return 2
    fi

    # [BLACKLIST-LIB] 黑名單統一走 bl_effective_exclude (TTL + NCHC sync + 50% cap)
    local ex_list exclude_arg=""
    ex_list="$(bl_effective_exclude "$part" 2>>"$LOG_FILE")"
    [ -n "$ex_list" ] && exclude_arg="--exclude=$ex_list"
    log "▷ effective exclude (partition=$part): ${ex_list:-(empty)}"

    # [WALLTIME-FIX] partition-specific walltime (partition_lib for GB200; jobscript fallback)
    local wt="" time_arg=""
    if type partition_walltime >/dev/null 2>&1; then
        wt="$(partition_walltime "$part")"
    fi
    if [ -z "$wt" ]; then
        wt="$(awk -F= '/^#SBATCH[[:space:]]+--time=/{gsub(/^[[:space:]]+|[[:space:]]+$/,"",$2); print $2; exit}' "$jobscript" 2>/dev/null)"
    fi
    [ -n "$wt" ] && time_arg="--time=$wt"

    log "▷ sbatch --parsable --partition=$part $time_arg $exclude_arg $jobscript"
    local next_id
    next_id="$(sbatch --parsable --partition="$part" $time_arg $exclude_arg "$jobscript" 2>&1)"
    local rc=$?

    if [ $rc -eq 0 ] && [[ "$next_id" =~ ^[0-9]+$ ]]; then
        log "SUBMIT-OK 已投 $target round: jobid=$next_id"
        echo "$next_id" > restart/chain_jobid
        # [COLD-START-INIT] 冷啟動情境下 chain_count 還不存在, 一併初始化以免 jobscript
        # 誤觸 "[REVIEW-FIX #7] chain state 半損毀" FATAL tripwire 形成無限迴圈.
        # 續跑情境 chain_count 已存在, 不覆寫.
        if [ ! -f restart/chain_count ]; then
            echo "1" > restart/chain_count
            log "[COLD-START-INIT] 初始化 restart/chain_count=1"
        fi
        # [SINGLE-HEAD] 把 jobid 寫進 HEAD.lockdir (state: SUBMITTING -> PENDING)
        if write_head_jobid "$next_id" "$cluster"; then
            log "[SINGLE-HEAD] HEAD.lockdir 升級 state=PENDING jobid=$next_id"
        else
            log "[SINGLE-HEAD] WARN write_head_jobid 失敗 (HEAD.lockdir 被外力移除?) -- jobscript verify_am_head 將以 RC=42 停鏈"
        fi
        return 0
    else
        log "SUBMIT-FAIL sbatch: $next_id -- 釋放 HEAD.lockdir"
        release_head_lock
        return 3
    fi
}

# ─────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────

# 初次啟動檢查: 至少一個 binary 必須存在
_init_ok=0
_seen_arch=""
for entry in "${PARTITION_CANDIDATES[@]}"; do
    c="${entry%%:*}"
    case " $_seen_arch " in *" $c "*) continue ;; esac
    _seen_arch="$_seen_arch $c"
    if cluster_binary_ready "$c"; then
        log "OK 偵測到 a.out.$c (${c} ready)"
        _init_ok=1
    else
        log "WARN a.out.$c 不存在, 將不投 $c 相關 partition"
    fi
done
if [ "$_init_ok" -eq 0 ]; then
    log "FATAL: a.out.GB200 和 a.out.H200 都不存在. 請先 ./run build <H200|GB200> --build-only"
    exit 10
fi

# 初次啟動: 檢查 chain 是否有 active job (可能是使用者 ./run.sh 後才啟動 dispatcher)
if chain_has_active_job; then
    CUR_ID="$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]')"
    log "偵測到已有 active job $CUR_ID, dispatcher 先監聽本輪結束再接手"
fi

# ═════════════════════════════════════════════════════════════════════════
# [LAYER 1] 啟動 grace period
# ─────────────────────────────────────────────────────────────────────────
# 使用情境: 使用者先跑 ./run.sh 投首輪,再立刻啟動 dispatcher.
# 問題:     run.sh 寫入 restart/chain_jobid 的值可能是 placeholder,
#           要等 jobscript 實際開跑才會被覆寫為真 SLURM jobid.
# 若 dispatcher 在此 window 內查 squeue -j <placeholder> 會得到「沒有 active job」,
# 誤判為「該投下一輪」, 造成與 run.sh 的 job 並發.
# 解法:     等 chain_jobid 變成 pure numeric 才開始正常輪詢,最多等 GRACE_MAX 秒.
# ═════════════════════════════════════════════════════════════════════════
GRACE_MAX="${GRACE_MAX:-180}"           # 秒; 最多等 3 分鐘 (SLURM controller 可能 lag)
GRACE_INTERVAL="${GRACE_INTERVAL:-5}"   # 秒; 輪詢間隔
_grace_elapsed=0
log "[LAYER 1] grace period: 等 restart/chain_jobid 變成 numeric jobid (max=${GRACE_MAX}s)"
while [ $_grace_elapsed -lt $GRACE_MAX ]; do
    _cur="$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]')"
    if [[ "$_cur" =~ ^[0-9]+$ ]]; then
        log "[LAYER 1] OK 偵測到 numeric jobid=$_cur, 進入正常輪詢"
        break
    fi
    log "[LAYER 1] chain_jobid='$_cur' 非 numeric, 等 ${GRACE_INTERVAL}s (elapsed=${_grace_elapsed}/${GRACE_MAX}s)"
    sleep "$GRACE_INTERVAL"
    _grace_elapsed=$((_grace_elapsed + GRACE_INTERVAL))
    # grace 期間也要尊重 STOP 訊號
    if [ -f "$STOP_SENTINEL" ] || [ -f restart/STOP_CHAIN ]; then
        log "[LAYER 1] grace 期間偵測到 STOP 訊號, dispatcher 收工"
        exit 0
    fi
done
if [ $_grace_elapsed -ge $GRACE_MAX ]; then
    log "[LAYER 1] WARN grace period 用盡 ($_grace_elapsed s), 強制進入正常輪詢"
    log "[LAYER 1]   (Layer 2/3 會繼續守護,但請人工確認 chain_jobid 狀態)"
fi

# [P0 TRAP #2 FIX] 連續找不到 capacity 的輪數
_nocapacity_count=0

# ──────────────────────────────────────────────────────────────────────────
# [PENDING-RESELECT] timing② : job 卡 PENDING 過久 → 比較其他 partition,
#   若有「明顯更快 (ETA 早 RESELECT_MARGIN_SEC 以上) 且不同 partition」者,
#   經 project_job_guard.sh (驗 WorkDir) 取消當前 job, 讓主迴圈以 2-tier 政策改投。
#   安全: 取消一律走 job-guard (只可能取消本專案 job, 杜絕跨專案誤殺); 保守門檻
#   (pending>=PENDING_RESELECT_SEC 且新者早 RESELECT_MARGIN_SEC 以上才動)。
#   PENDING_RESELECT=0 可完全停用此 watchdog。
# ──────────────────────────────────────────────────────────────────────────
# [6-a fix] scancel 前再驗一次是否「仍 PENDING」(squeue 即時): watchdog 入口讀 state 後, 還要經
# pick_cluster + 2 次 ETA 探測(數秒), 窗口內 backfill 可能把 job 轉 RUNNING → 取消前重驗,
# 避免誤殺自家剛起跑的 RUNNING job (丟失計算進度)。
_still_pending() { [ "$(squeue -h -j "$1" -o '%T' 2>/dev/null | tr -d ' ')" = "PENDING" ]; }

_pending_reselect_watchdog() {
    [ "${PENDING_RESELECT:-1}" = "1" ] || return 0
    local jid; jid="$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]')"
    [ -n "$jid" ] || return 0
    local st; st="$(sacct -n -X -j "$jid" -o State 2>/dev/null | head -1 | tr -d ' ')"
    [ "$st" = "PENDING" ] || return 0
    local subt nowt subepoch pend
    subt="$(sacct -n -X -j "$jid" -o Submit 2>/dev/null | head -1 | tr -d ' ')"
    nowt="$(date +%s)"
    subepoch="$(date -d "$subt" +%s 2>/dev/null || echo "$nowt")"
    pend=$(( nowt - subepoch )); [ "$pend" -lt 0 ] && pend=0
    [ "$pend" -ge "${PENDING_RESELECT_SEC:-1800}" ] || return 0

    local curpart; curpart="$(scontrol show job "$jid" 2>/dev/null | grep -oE 'Partition=[^ ]+' | head -1 | cut -d= -f2)"
    local pick; pick="$(pick_cluster)"; [ -n "$pick" ] || return 0
    local newpart; newpart="$(target_partition "$pick" 2>/dev/null)"
    if [ -z "$newpart" ] || [ "$newpart" = "$curpart" ]; then
        # partition 已最佳但仍卡 PENDING → 若開了 jp 控制器, 試 jp scale-down (小 footprint 較快排到)
        # [HIGH-2] 只在「真的縮小 jp」時才取消 PENDING job (放大不該由 watchdog 取消 RUNNING/排隊中的);
        # 且 job 至少已 PENDING T_DOWN 秒 (watchdog 進到這裡已 >= PENDING_RESELECT_SEC)。
        if [ "${JP_CONTROLLER:-0}" = "1" ] && [ "$pend" -ge "${T_DOWN:-1800}" ]; then
            local _jpd _newjp _curjp
            _jpd="$(pick_jp_and_partition 2>/dev/null)"
            _newjp="$(printf '%s\n' "$_jpd" | awk '/^CHANGE_JP/{print $2}')"
            _curjp="$(grep -E '^#define[[:space:]]+jp[[:space:]]+[0-9]+' variables.h 2>/dev/null | grep -oE '[0-9]+' | head -1)"
            if [ -n "$_newjp" ] && [ "${_curjp:-0}" -gt 0 ] && [ "$_newjp" -lt "$_curjp" ]; then
                if ! _still_pending "$jid"; then log "[PENDING-RESELECT][JP] job $jid 已非 PENDING (backfill 轉 RUNNING?) → 放棄取消, 不誤殺"; return 0; fi
                log "[PENDING-RESELECT][JP] job $jid pending ${pend}s, partition 已最佳; 縮小 jp $_curjp→$_newjp → 經 job-guard 取消, 主迴圈將切 jp 改投"
                bash "$CHAIN_DIR/tools/project_job_guard.sh" scancel "$jid" >/dev/null 2>&1 \
                  && log "[PENDING-RESELECT][JP] 已取消 $jid (主迴圈會 changejp + 重投)" \
                  || log "[PENDING-RESELECT][JP] WARN: 取消失敗, 維持排隊"
                return 0
            fi
        fi
        log "[PENDING-RESELECT] job $jid pending ${pend}s, 最佳仍是 ${curpart:-?} → 維持排隊, 不取消"
        return 0
    fi
    local c_js new_eta cur_eta
    c_js="$(cluster_jobscript "$(target_cluster "$pick")" 2>/dev/null)"
    new_eta="$(_pick_cluster_eta_epoch "$c_js" "$newpart")"
    cur_eta="$(_pick_cluster_eta_epoch "$c_js" "$curpart")"
    if [ "${new_eta:- -1}" -lt 0 ] || [ "${cur_eta:- -1}" -lt 0 ] || \
       [ $(( cur_eta - new_eta )) -le "${RESELECT_MARGIN_SEC:-900}" ]; then
        log "[PENDING-RESELECT] job $jid pending ${pend}s; $newpart 未明顯更快 → 維持排隊"
        return 0
    fi
    if ! _still_pending "$jid"; then log "[PENDING-RESELECT] job $jid 已非 PENDING (backfill 轉 RUNNING?) → 放棄取消改投, 不誤殺"; return 0; fi
    log "[PENDING-RESELECT] job $jid 在 ${curpart:-?} 已 pending ${pend}s; $newpart ETA 早 ~$(( cur_eta - new_eta ))s → 經 job-guard 取消改投"
    local _gout
    _gout="$(bash "$CHAIN_DIR/tools/project_job_guard.sh" scancel "$jid" 2>&1)"
    if [ $? -eq 0 ]; then
        log "[PENDING-RESELECT] 已取消 $jid, 下一圈主迴圈將以 2-tier 政策改投"
    else
        log "[PENDING-RESELECT] WARN: job-guard 取消 $jid 失敗 (WorkDir 不符/已結束?), 不強制: $_gout"
    fi
}

# ═════════════════════════════════════════════════════════════════════════
# [JP-CONTROLLER] 動態 jp 選擇 (Phase B). 預設 JP_CONTROLLER=1 → 完全啟用,
# 在輪界依「淨速度 + 絕不閒置」選 jp+partition (設 JP_CONTROLLER=0 可關回只切 partition)。
# 所有 log 走 >&2; 唯一 stdout = 決策字串 "KEEP|CHANGE_JP <jp> <ARCH@part>"。
# ═════════════════════════════════════════════════════════════════════════
JP_CONTROLLER="${JP_CONTROLLER:-1}"
JP_CANDIDATES_RAW="${JP_CANDIDATES:-64 32}"; read -r -a JP_CANDIDATES <<< "$JP_CANDIDATES_RAW"
# [2026-06-05 NEW 政策 — 計畫 MST115169] 候選 = {64,32} 對應 H200 {8,4} nodes (8 GPU/node):
#   使用者策略「partition@jp 自由跳轉: 8gpus@32 / 16gpus@32 / 32gpus@32 / 64gpus@64, 暫鎖 64gpus@64」.
#   jp 不再等於 partition 名數字 (MaxTRESPA): jp=64→64gpus only(cap64); jp=32→{8,16,32,64}gpus(皆 cap≥32);
#   dev(cap4) 不在候選內 (jp 最小 32 已超其 cap).
#   (舊 {64,32,16} 假設 cap=名稱數字, 已被實測 p_8/16/32gpus MaxTRESPA=32 推翻.)
#   64 GPU=8 nodes / 32=4 (896%{64,32}=0, slab={14,28}>=7 物理合法). 帳號 GPU cap(MaxTRESPA)=8/16/32gpus→32, 64gpus→64;
#   跨使用者共用動態占用; jp 大的全超標被「直接跳過警告(不先試 --test-only)」, 留最小可行 footprint 續跑.
#   切 jp 由 changejp.sh --prepare-only(repartition 純資料重排, 流場一位元不差)處理; accu=0 不丟統計。
JP_CHANGE_COOLDOWN="${JP_CHANGE_COOLDOWN:-1800}"
K_UP="${K_UP:-2}"                                 # scale-up 需連續確認次數
K_DOWN="${K_DOWN:-2}"                              # scale-down 也需連續確認 (對稱防抖, 修 HIGH-1)
T_DOWN="${T_DOWN:-1800}"                           # watchdog: jp 縮小前 job 至少已 PENDING 這麼久
JP_SWITCH_GAIN_PCT="${JP_SWITCH_GAIN_PCT:-15}"     # 新 jp 分數須勝現 jp 至少 +N% 才換 (防 thrash, 修 L2)
FTT_PRELOCK="${FTT_PRELOCK:-48}"
JP_FREEZE_ON_STATS="${JP_FREEZE_ON_STATS:-0}"     # 0=允許統計階段也自動切 jp(預設; repartition 已點對點搬移全部 36 累加器+accu_count+cv, 統計 bit 保留, Codex 驗證);
                                                  # 1=統計階段(accu>0/FTT>=prelock)凍結 jp(穩定優先, 舊版 repartition 會摧毀統計時的保護)
PARTITION_START_SOON_SEC="${PARTITION_START_SOON_SEC:-120}"
JP_STATE_FILE="restart/jp_controller.state"

_jp_read_current() { grep -E '^#define[[:space:]]+jp[[:space:]]+[0-9]+' variables.h 2>/dev/null | grep -oE '[0-9]+' | head -1; }
_jp_read_meta() {  # echo "accu ftt"
  local m a f; m="$(readlink -f restart/checkpoint/latest 2>/dev/null)/metadata.dat"
  a="$(grep -E '^accu_count=' "$m" 2>/dev/null | cut -d= -f2)"; f="$(grep -E '^FTT=' "$m" 2>/dev/null | cut -d= -f2)"
  echo "${a:-0} ${f:-0}"
}
_jp_state_get() { local k="$1" d="${2:-}" v; v="$(grep -E "^$k=" "$JP_STATE_FILE" 2>/dev/null | tail -1 | cut -d= -f2)"; echo "${v:-$d}"; }
_jp_state_set() { mkdir -p restart; while [ $# -ge 2 ]; do local k="$1" v="$2"; shift 2
    if [ -f "$JP_STATE_FILE" ] && grep -qE "^$k=" "$JP_STATE_FILE"; then sed -E -i "s|^$k=.*|$k=$v|" "$JP_STATE_FILE"; else echo "$k=$v" >> "$JP_STATE_FILE"; fi; done; }

# [4] 帳號此刻在某 partition 的 GPU 用量 (RUNNING+PENDING), 排除「本 chain 的 head」(它在下輪前會釋放)。
# 用於即時 headroom 過濾: 靜態 cap 容得下、但此刻已被同帳號其他 job 占滿的 partition, 即投仍會 PENDING。
partition_account_gpu_inuse() {
    local part="$1" myhead; myhead="$(cat restart/chain_jobid 2>/dev/null | tr -dc 0-9)"
    # [FIX 2026-06-03] 只算 RUNNING (不算 PENDING). MaxGRESPerAccount 只計入已配置(RUNNING)的
    #   GRES; PENDING job 尚未占用任何 GPU. 共用帳號(mst115169 跨 teddyji0315/u8035407/本專案)時,
    #   別用戶在某 partition 排隊(PENDING)的 job 不該被算成「占用」而擋掉本專案 → 否則 dev 上
    #   u8035407 的 PENDING 會讓本專案誤判 dev 爆滿(已用 256GPU)而永遠跳過 dev.
    squeue -A "${ACCOUNT:-MST114348}" -h -t RUNNING -o '%i|%P|%D|%b' 2>/dev/null | awk -F'|' -v p="$part" -v me="$myhead" '
        { jid=$1; pj=$2; n=$3; g=$4
          if (pj != p) next
          if (me != "" && jid == me) next
          sub(/.*gpu:/,"",g); sub(/[^0-9].*/,"",g); if (g=="") g=0
          tot += n*g }
        END { print tot+0 }'
}

# 假設 jp 在 (ARCH,part) 的開始 epoch; -1 = 不可行。先用 MaxTRESPerAccount 過濾, 再 --test-only(--nodes/--ntasks override 已實測有效)。
jp_partition_eta() {
  local jp="$1" c="$2" part="$3" cap nodes js wt ta out ts eta _ny _nm1
  # [JPC-1] 網格整除 + slab 下限前驗: 非法 jp 物理上不可跑(changejp 也會擋), 連 SLURM 都不必試。
  _ny="$(awk '/^#define[[:space:]]+NY[[:space:]]/{print $3; exit}' variables.h 2>/dev/null)"
  if [ -n "$_ny" ] && [ "$jp" -gt 0 ]; then
    _nm1=$((_ny - 1))
    if [ $((_nm1 % jp)) -ne 0 ] || [ $((_nm1 / jp)) -lt 7 ]; then
      log "[JP-CTL]   考慮 jp=$jp@$part → 網格不整除或 slab<7 (物理不可跑) → 跳過" >&2; echo -1; return
    fi
  fi
  case "$c" in H200) nodes=$((jp/8)) ;; GB200) nodes=$((jp/4)) ;; *) nodes=$((jp/8)) ;; esac
  [ "$nodes" -lt 1 ] && { echo -1; return; }
  js="$(cluster_jobscript "$c")" || { echo -1; return; }; [ -f "$js" ] || { echo -1; return; }
  # [2026-06-04 改 / 使用者要求「遇上限直接跳過給警告, 不要連試試看」]
  #   超 cap / 超即時 headroom 的組合: 直接跳過並警告, 不再先花一次 sbatch --test-only 探測。
  #   理由: --test-only 盲於 MaxTRESPerAccount, 對超 cap 組合會誤報「立即可起」, 其 ETA 結果反正
  #   會被丟棄(下方一律 echo -1) → 先試純粹浪費一次 sbatch 往返。故 cap/headroom 過不了就不探測。
  cap="$(partition_gpu_cap_per_account "$part" 2>/dev/null || echo 100000)"
  if [ "$jp" -gt "$cap" ]; then
    log "[JP-CTL]   考慮 jp=$jp@$part → 帳號GPU上限 $cap<$jp → 直接跳過(不試 --test-only; 投了永久 PENDING MaxGRESPerAccount)" >&2
    echo -1; return
  fi
  # [4] 即時 headroom: 靜態 cap 容得下, 但帳號此刻在此 partition 已被(同帳號其他 job)占用 → 仍會 PENDING。
  # 排除「本 chain 自己的 head」(下輪前會釋放); dev(cap 極大)實質不受影響。修「4nodes 此刻 32/32 滿
  # 卻仍被選中→PENDING、要等 watchdog 1800s 才補救」。
  local _inuse; _inuse="$(partition_account_gpu_inuse "$part")"
  if [ "$jp" -gt $(( cap - _inuse )) ]; then
    log "[JP-CTL]   考慮 jp=$jp@$part → cap=$cap 容得下但帳號此刻已用 ${_inuse}GPU, 剩 $(( cap - _inuse ))<$jp → 直接跳過(不試 --test-only; 即投會 PENDING)" >&2
    echo -1; return
  fi
  # cap + 即時 headroom 都通過, 才值得實際向 SLURM 問此組合 ETA(此時 --test-only 結果不會被丟棄)。
  wt="$(partition_walltime "$part")"; ta=""; [ -n "$wt" ] && ta="--time=$wt"
  out="$(sbatch --test-only --partition="$part" --nodes="$nodes" --ntasks="$jp" $ta "$js" 2>&1 || true)"
  if echo "$out" | grep -qE "to start at[[:space:]]+[0-9]{4}-"; then
    ts="$(echo "$out" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+' | head -1)"; eta="$(date -d "$ts" +%s 2>/dev/null || echo -1)"
  elif echo "$out" | grep -qE "allocation .*can be allocated|to start immediately|to start now"; then eta="$(date +%s)"
  else eta=-1; fi
  if [ -z "$eta" ] || [ "$eta" -lt 0 ]; then
    log "[JP-CTL]   考慮 jp=$jp@$part → cap 通過但 --test-only 無可解析 ETA → 跳過" >&2; echo -1; return
  fi
  echo "$eta"
}

# 對給定 jp 選最佳 partition (2-tier). echo "ARCH@part eta wsec startable" 或空。
_pick_partition_for_jp() {
  local jp="$1" entry c part eta w now soon i bi
  now="$(date +%s)"; soon=$((now + PARTITION_START_SOON_SEC))
  local -a sT=() sE=() sW=()
  for entry in "${PARTITION_CANDIDATES[@]}"; do
    c="${entry%%:*}"; part="${entry#*:}"
    { [ -z "$c" ] || [ -z "$part" ] || [ "$c" = "$part" ]; } && continue
    cluster_binary_ready "$c" || continue
    eta="$(jp_partition_eta "$jp" "$c" "$part")"; [ "$eta" -lt 0 ] && continue
    w="$(walltime_to_sec "$(partition_walltime "$part")")"
    sT+=("$c@$part"); sE+=("$eta"); sW+=("$w")
  done
  local n=${#sT[@]}; [ "$n" -eq 0 ] && { echo ""; return 1; }
  bi=-1
  for ((i=0;i<n;i++)); do [ "${sE[$i]}" -le "$soon" ] || continue
    # [PREF 2026-06-03] 不看 walltime, 抓最早 ETA(最空閒). chain 自動續投不受 walltime 影響,
    #   故短 walltime(dev 1h) 與長 walltime(normal 2d) 同等; 純粹抓能即起的空閒 partition.
    if [ "$bi" -lt 0 ] || [ "${sE[$i]}" -lt "${sE[$bi]}" ]; then bi=$i; fi
  done
  if [ "$bi" -ge 0 ]; then echo "${sT[$bi]} ${sE[$bi]} ${sW[$bi]} 1"; return 0; fi
  bi=0; for ((i=1;i<n;i++)); do [ "${sE[$i]}" -lt "${sE[$bi]}" ] && bi=$i; done
  echo "${sT[$bi]} ${sE[$bi]} ${sW[$bi]} 0"; return 0
}

# 主決策: echo "KEEP <jp> <ARCH@part>" 或 "CHANGE_JP <jp> <ARCH@part>"。
pick_jp_and_partition() {
  local cur acc ftt locked now n i; cur="$(_jp_read_current)"; cur="${cur:-0}"
  # [LOCK 臨時開關 2026-06-03] restart/LOCK_COMBO 存在 → 凍結 jp(維持當前) + 鎖 partition(繞過矩陣評估).
  #   還原自由跳轉(partition&&jp): rm restart/LOCK_COMBO + ./run dispatcher 重啟 (見 CLAUDE.md「還原回自由跳轉」).
  if [ -f restart/LOCK_COMBO ]; then
    local _lc _ltgt; _lc="$(tr -d '\r\n' < restart/LOCK_COMBO 2>/dev/null)"; _ltgt="${_lc#* }"
    if [ -n "$_ltgt" ] && [ "$_ltgt" != "$_lc" ]; then
      log "[JP-CTL] [LOCK] LOCK_COMBO → 凍結 jp=$cur + 鎖 partition=$_ltgt (還原自由跳轉: rm restart/LOCK_COMBO)" >&2
      echo "KEEP $cur $_ltgt"; return 0
    fi
  fi
  read -r acc ftt < <(_jp_read_meta)
  locked=0
  # [6-b] STOP_JPSWITCH 專屬開關: 凍結「自動 jp 切換」(jp 不變), 但 chain / dispatcher / partition 自動切換照常。
  # 與 STOP_CHAIN(停整條鏈) 與 JP_FREEZE_ON_STATS(統計階段才凍) 不同 → 提供「只凍 jp、不停鏈」的中間檔。
  [ -f restart/STOP_JPSWITCH ] && { locked=1; log "[JP-CTL] 偵測 restart/STOP_JPSWITCH → 凍結 jp 自動切換(仍自由切 partition)" >&2; }
  if [ "${JP_FREEZE_ON_STATS:-1}" = "1" ]; then
    { [ "${acc:-0}" != "0" ] || awk "BEGIN{exit !((${ftt:-0})>=(${FTT_PRELOCK}))}"; } && locked=1
  fi
  now="$(date +%s)"
  # [freeze-on-stats 短路, Codex-C #3] 統計鎖定時不探測其他 jp (省去整輪 sbatch --test-only),
  # 直接只選 current jp 的 partition 並 KEEP → 保護累積中的統計、永不改 jp。
  if [ "$locked" -eq 1 ]; then
    local ctgt; ctgt="$(_pick_partition_for_jp "$cur" | awk '{print $1}')"
    log "[JP-CTL] LOCK (accu=$acc ftt=$ftt, prelock=$FTT_PRELOCK) → KEEP jp=$cur part=${ctgt:-?} (不探測其他 jp)" >&2
    echo "KEEP $cur ${ctgt:-}"; return 0
  fi
  local -a J=() T=() E=() W=()
  local jp r
  for jp in "${JP_CANDIDATES[@]}"; do
    r="$(_pick_partition_for_jp "$jp")" || continue; [ -z "$r" ] && continue
    J+=("$jp"); T+=("$(echo "$r"|awk '{print $1}')"); E+=("$(echo "$r"|awk '{print $2}')"); W+=("$(echo "$r"|awk '{print $3}')")
  done
  n=${#J[@]}; [ "$n" -eq 0 ] && { log "[JP-CTL] 無可行組合" >&2; echo ""; return 1; }
  local best=-1 best_score=-1 cur_score=-1
  for ((i=0;i<n;i++)); do
    local wait=$(( ${E[$i]} - now )); [ "$wait" -lt 0 ] && wait=0
    local sw=0; [ "${J[$i]}" != "$cur" ] && sw=270
    local wsec=${W[$i]}; [ "$wsec" -le 0 ] && wsec=1
    # [PREF 2026-06-03] 高 jp 優先 + 抓空閒(低 wait); 移除 walltime(wsec) 權重.
    #   舊式 jp*eff*1000/wsec (eff=wsec-wait-sw) 會偏好長 walltime: 同樣 wait 在短 walltime(dev 1h)
    #   佔比大 → eff 低 → 分數低 → dev 被冷落. 但 chain 自動續投不受 walltime 影響, 短 walltime 無妨.
    #   新式: score = jp*1000 - wait - sw. jp 主導(16→64 = 16000→64000 級距), wait/switch 秒數為次要懲罰
    #   (偏好能即起的空閒 partition + 少切換), walltime 完全不計. 平手再由下方 tie-break 取較高 jp.
    local score=$(( ${J[$i]} * 1000 - wait - sw )); [ "$score" -lt 0 ] && score=0
    [ "${J[$i]}" = "$cur" ] && cur_score=$score
    log "[JP-CTL]   jp=${J[$i]} ${T[$i]} wait=${wait}s wt=${wsec}s sw=$sw score=$score" >&2
    if [ "$score" -gt "$best_score" ] || { [ "$score" -eq "$best_score" ] && [ "$best" -ge 0 ] && [ "${J[$i]}" -gt "${J[$best]}" ]; }; then best=$i; best_score=$score; fi
  done
  [ "$best" -lt 0 ] && { echo ""; return 1; }
  local want_jp="${J[$best]}" want_tgt="${T[$best]}"
  # (freeze-on-stats 已在迴圈前短路處理 — Codex-C #3)
  if [ "$want_jp" = "$cur" ]; then _jp_state_set jp_change_target 0 jp_change_count 0; echo "KEEP $cur $want_tgt"; return 0; fi
  # 防抖 1 [L2]: 新 jp 分數須勝現 jp 至少 +JP_SWITCH_GAIN_PCT% 才值得換
  #   (現 jp 不在可行清單時 cur_score=-1 → 現 jp 根本跑不動, 直接允許換)
  if [ "$cur_score" -ge 0 ] && [ $(( best_score * 100 )) -lt $(( cur_score * (100 + JP_SWITCH_GAIN_PCT) )) ]; then
    log "[JP-CTL] $want_jp 分數 $best_score 未勝現 jp $cur ($cur_score) 的 +${JP_SWITCH_GAIN_PCT}% → KEEP" >&2
    _jp_state_set jp_change_target 0 jp_change_count 0
    echo "KEEP $cur $(_pick_partition_for_jp "$cur" | awk '{print $1}')"; return 0
  fi
  # 防抖 2: cooldown (上次切 jp 後至少間隔 JP_CHANGE_COOLDOWN)
  local last_change; last_change="$(_jp_state_get last_jp_change_epoch 0)"
  if [ $(( now - last_change )) -lt "$JP_CHANGE_COOLDOWN" ]; then
    log "[JP-CTL] 想換 jp $cur→$want_jp 但 cooldown 未過 → KEEP" >&2
    echo "KEEP $cur $(_pick_partition_for_jp "$cur" | awk '{print $1}')"; return 0
  fi
  # 防抖 3 [HIGH-1]: 連續確認 — 放大需 K_UP 次, 縮小需 K_DOWN 次, 同一目標才累加 (對稱遲滯)
  local need; if [ "$want_jp" -gt "$cur" ]; then need="$K_UP"; else need="$K_DOWN"; fi
  local tgt cnt; tgt="$(_jp_state_get jp_change_target 0)"; cnt="$(_jp_state_get jp_change_count 0)"
  if [ "$tgt" = "$want_jp" ]; then cnt=$((cnt+1)); else tgt="$want_jp"; cnt=1; fi
  _jp_state_set jp_change_target "$tgt" jp_change_count "$cnt"
  if [ "$cnt" -ge "$need" ]; then echo "CHANGE_JP $want_jp $want_tgt"; return 0; fi
  log "[JP-CTL] 換 jp $cur→$want_jp 確認 $cnt/$need (gain+cooldown 已過) → 暫 KEEP" >&2
  echo "KEEP $cur $(_pick_partition_for_jp "$cur" | awk '{print $1}')"; return 0
}

# [SELFTEST] 乾跑一次 jp 決策後退出 (不啟 daemon、不投遞、不改檔; 只讀 variables.h + sbatch --test-only):
#   DISPATCHER_SELFTEST=1 bash chain_code/submit_dispatcher.sh
if [ "${DISPATCHER_SELFTEST:-0}" = "1" ]; then
    echo "=== [SELFTEST] pick_jp_and_partition 乾跑 (視 JP_CONTROLLER=1; 不投遞) ==="
    JP_CONTROLLER=1
    JP_STATE_FILE="$(mktemp 2>/dev/null || echo /tmp/jp_selftest_state.$$)"   # [MED-1] 用拋棄式 state, 不碰真正的 jp_controller.state
    DECISION="$(pick_jp_and_partition)"
    rm -f "$JP_STATE_FILE"
    echo ">>> 決策結果: ${DECISION:-<none>}"
    echo "=== [SELFTEST] 完成; 未投遞、未改任何檔 ==="
    exit 0
fi

while true; do
    # [restart-gap race fix] 每輪 touch heartbeat: jobscript(compute node)用此檔 mtime 判斷 daemon 是否存活
    # (跨節點無法 kill -0 login PID)。daemon 健康時每 ~POLL_INTERVAL touch 一次, jobscript 容忍 180s。
    touch restart/dispatcher.heartbeat 2>/dev/null || true
    # Stop 條件 0 [a.out death gate]: solver binary 全消失 (lbm-clean/reset/拆專案) → 專案已拆除,
    # dispatcher clean-exit。補強 keepalive 同節點 kill 的跨節點盲點: 即使 daemon 在別的 login node,
    # 也會在自己這輪自我退出。(注意: 啟動期檢查只在進迴圈前跑一次, 故迴圈內需此 per-loop 閘門。)
    if [ ! -s a.out ] && [ ! -s a.out.H200 ] && [ ! -s a.out.GB200 ]; then
        # FALSE-DEATH guard: ./run build 持有 .run.lock flock 時 binary 可能暫缺 → 不退出, 續跑等 build 完成。
        if [ -e .run.lock ] && command -v flock >/dev/null 2>&1 && ! ( flock -n 9 ) 9< .run.lock 2>/dev/null; then
            log "DEATH GATE: binary 暫缺但 .run.lock 被佔用 (build 進行中) -> dispatcher 續跑"
        else
            log "DEATH GATE: 無 solver binary (a.out/.H200/.GB200) -> dispatcher clean-exit (專案已拆除)"
            rm -f restart/DISPATCHER_INTENT restart/dispatcher.heartbeat 2>/dev/null || true
            break
        fi
    fi
    # Stop 條件 1: STOP_DISPATCHER
    if [ -f "$STOP_SENTINEL" ]; then
        log "偵測到 $STOP_SENTINEL -> dispatcher 停止"
        break
    fi
    # Stop 條件 2: restart/STOP_CHAIN
    if [ -f restart/STOP_CHAIN ]; then
        log "偵測到 restart/STOP_CHAIN -> chain 自然停止, dispatcher 收工"
        break
    fi
    # Stop 條件 3 [P0 TRAP #2 FIX]: no-capacity 封頂 (兩邊都滿超過 LIMIT 輪)
    if [ -f "$NOCAPACITY_SENTINEL" ]; then
        log "偵測到 $NOCAPACITY_SENTINEL -> 叢集長時間無空位, dispatcher 收工"
        break
    fi

    # 如果 chain 目前還有 active job, 等它結束
    if chain_has_active_job; then
        # [PENDING-RESELECT] timing② : job 卡 PENDING 過久且有更快 partition → 改投
        # (預設啟用; PENDING_RESELECT=0 可停用。只取消 state=PENDING, 經 job-guard)
        _pending_reselect_watchdog || true
        sleep "$POLL_INTERVAL"
        continue
    fi

    # ── 進到「該投下一輪」的分支 ──
    # 先查上一輪 exit code
    LAST_EC="$(chain_last_exit_code)"
    if [ -n "$LAST_EC" ]; then
        log "上一輪 job exit code = $LAST_EC"
        # RC=42: unavoidable, 不重投
        if [ "$LAST_EC" = "42" ]; then
            log "RC=42 [POLICY-C1] unavoidable stop. dispatcher 收工."
            break
        fi
        # ─────────────────────────────────────────────────────────────────
        # [POLICY-C1 FIX 2026-04-25] RC=0 不再單獨判定為「自然停止」.
        #   原因: jobscript 在 DISPATCHER 模式下, 即便 mpirun RC≠0 (例如 walltime
        #   timeout RC=205 / MPI 通訊失敗 / fast-fail), 在 chain_count++ 後仍會
        #   exit 0 把續投權交給 dispatcher. sacct 只看到 ExitCode=0:0, 無從區分.
        #   真實「自然停止」訊號是 restart/STOP_CHAIN sentinel
        #   (jobscript 在 RC=0 自然停 / FAST-FAIL guard 觸發時 touch).
        # ─────────────────────────────────────────────────────────────────
        if [ "$LAST_EC" = "0" ]; then
            if [ -f restart/STOP_CHAIN ]; then
                log "RC=0 + restart/STOP_CHAIN [POLICY-C1] 確認自然停止. dispatcher 收工."
                break
            else
                _cc_now="$(cat restart/chain_count 2>/dev/null | tr -d '[:space:]')"
                log "RC=0 但無 restart/STOP_CHAIN sentinel -> 視為「jobscript 把續投權交給 dispatcher」(chain_count=${_cc_now:-?}, 上一輪 jobid=$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]'))"
                log "     原因可能是: walltime/MPI 失聯/fast-fail 而 jobscript 已 chain_count++. 進入 pick_cluster 投下一輪."
                # 不 break, 繼續往下走 pick_cluster + submit_round
            fi
        fi
    fi

    # 再查一次 STOP (可能 jobscript 剛寫入)
    if [ -f restart/STOP_CHAIN ]; then
        log "發現 restart/STOP_CHAIN -> chain 自然停止, dispatcher 收工"
        break
    fi

    # 選 cluster
    log "----- 準備投下一輪, 查詢 partition 狀態 -----"
    # [JP-CONTROLLER] 投 partition 前先決定 jp (僅 JP_CONTROLLER=1; =0 完全跳過, 行為同今日)
    if [ "$JP_CONTROLLER" = "1" ]; then
        JP_DECISION="$(pick_jp_and_partition)"
        log "[JP-CTL] 決策: ${JP_DECISION:-<none>}"
        case "${JP_DECISION:-}" in
            "CHANGE_JP "*)
                _NEW_JP="$(printf '%s\n' "$JP_DECISION" | awk '{print $2}')"
                if [ -n "$_NEW_JP" ]; then
                    log "[JP-CTL] 切換 jp -> $_NEW_JP : timeout 600 changejp.sh --prepare-only (重編+repartition, 不投遞)"
                    if timeout 600 bash "$CHAIN_DIR/changejp.sh" "$_NEW_JP" --prepare-only >> "$LOG_FILE" 2>&1; then
                        _jp_state_set last_jp_change_epoch "$(date +%s)"
                        log "[JP-CTL] jp 已切到 $_NEW_JP; 下一圈 pick_cluster 用新 jp 投遞"
                    else
                        log "[JP-CTL] changejp --prepare-only 失敗/逾時, 維持現 jp 繼續"
                    fi
                    sleep "$PROBE_RESUBMIT_DELAY"
                    continue
                fi
                ;;
        esac
    fi
    NEXT_CLUSTER="$(pick_cluster)"
    # [DEFENSIVE 2026-04-22] 除了 empty, 還驗證回傳值只能是安全的 target 名.
    # 用 case 避開 [[ =~ regex ]] 避免某些 linter 在 regex 的 $ 錨點觸發截斷.
    # 即使 pick_cluster 未來因 log() 漏出而被污染, 也不會把 log 文字誤當 cluster
    # 進到 submit_round, 避免 "選中: [timestamp] ..." 這類畸形輸出 + 無效 sbatch.
    _CLUSTER_BAD=0
    if [ -z "$NEXT_CLUSTER" ]; then
        _CLUSTER_BAD=1
    else
        case "$NEXT_CLUSTER" in
            *[!A-Za-z0-9@._-]*) _CLUSTER_BAD=1 ;;
            [A-Z]*) : ;;                      # 首字必為大寫
            *) _CLUSTER_BAD=1 ;;
        esac
    fi
    if [ "$_CLUSTER_BAD" -eq 1 ]; then
        # [P0 TRAP #2 FIX] 只有「兩邊都忙 (NEXT_CLUSTER 空)」才計入 no-capacity.
        # pick_cluster 回傳畸形值 (BUG-GUARD 情境) 不算, 避免雜訊把我們推向誤停.
        if [ -z "$NEXT_CLUSTER" ]; then
            _nocapacity_count=$((_nocapacity_count + 1))
            log "pick_cluster ETA-compare 失敗 (全部候選 binary 缺 或 sbatch --test-only 都無解析), ${POLL_INTERVAL}s 後重試 (no-capacity ${_nocapacity_count}/${NOCAPACITY_LIMIT})"
            if [ "$_nocapacity_count" -ge "$NOCAPACITY_LIMIT" ]; then
                _total_wait_min=$(( _nocapacity_count * POLL_INTERVAL / 60 ))
                log "============================================================================="
                log "[P0 TRAP #2] 連續 ${_nocapacity_count} 輪全部候選都拿不到 ETA (累積 ${_total_wait_min} 分鐘)"
                log "             可能原因: (a) Slurm controller 暫停 (b) QoS 超額被 reject (c) a.out.<ARCH> binary 不存在 (d) partition 名稱無效"
                log "             注意: 正常情況下 Stage 2 ETA 挑選會讓 chain 即使兩邊忙也能進 PENDING, 不會走到這裡."
                log "             觸發明確停機: 寫入 $NOCAPACITY_SENTINEL 後退出."
                log "             -- 恢復方法 (擇一) --"
                log "             1. 確認叢集有空後: rm $NOCAPACITY_SENTINEL && ./run dispatcher start"
                log "             2. 調整容忍度:     NOCAPACITY_LIMIT=120 ./run dispatcher start (1hr)"
                log "             3. 只用單 cluster: ./run --h200 或 ./run --gb200 (手動投, 不啟 dispatcher)"
                log "============================================================================="
                {
                    printf 'reason=no_capacity\n'
                    printf 'consecutive_rounds=%d\n' "$_nocapacity_count"
                    printf 'poll_interval_sec=%d\n' "$POLL_INTERVAL"
                    printf 'total_wait_minutes=%d\n' "$_total_wait_min"
                    printf 'limit=%d\n' "$NOCAPACITY_LIMIT"
                    printf 'triggered_at=%s\n' "$(date -Iseconds 2>/dev/null || date)"
                    printf 'triggered_at_epoch=%s\n' "$(date +%s)"
                    printf 'hostname=%s\n' "$(hostname 2>/dev/null || echo unknown)"
                } > "$NOCAPACITY_SENTINEL"
                break
            fi
        else
            log "[BUG-GUARD] pick_cluster 回傳非合法 cluster 名 (值=$(printf %q "$NEXT_CLUSTER")), ${POLL_INTERVAL}s 後重試"
        fi
        sleep "$POLL_INTERVAL"
        continue
    fi

    log "選中: $NEXT_CLUSTER (partition=$(target_partition "$NEXT_CLUSTER"))"
    submit_round "$NEXT_CLUSTER"
    SUBMIT_RC=$?
    case $SUBMIT_RC in
        0)  # [P0 TRAP #2 FIX] 投遞成功 → 重置 no-capacity 計數
            _nocapacity_count=0
            sleep "$PROBE_RESUBMIT_DELAY" ;;
        4)  # Layer 2 擋下 (已有 active job 或 lock 被持有) -- 非 error, 下輪再試
            log "[LAYER 2] 投遞被 pre-submit scan 擋下, ${POLL_INTERVAL}s 後重試"
            sleep "$POLL_INTERVAL" ;;
        *)  log "submit 失敗 (rc=$SUBMIT_RC), ${POLL_INTERVAL}s 後重試"
            sleep "$POLL_INTERVAL" ;;
    esac
done

log "============================================================================="
log "dispatcher 正常結束."
log "============================================================================="
exit 0
