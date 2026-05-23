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
ACCOUNT="${ACCOUNT:-MST115169}"

# [P0 TRAP #2 FIX] 兩邊都忙時連續沒空位多少次後, 觸發明確停機 (避免隱形無限 sleep)
# 預設 60 次 × POLL_INTERVAL(30s) = 30 分鐘. 可用 env 覆寫 (e.g. NOCAPACITY_LIMIT=120 = 1hr)
NOCAPACITY_LIMIT="${NOCAPACITY_LIMIT:-60}"
NOCAPACITY_SENTINEL="restart/STOP_NOCAPACITY"

# Partition 候選清單: <ARCH>:<partition>
# - GB200 partitions 共用 a.out.GB200 / jobscript_chain.slurm.GB200
# - H200 dev 共用 a.out.H200 / jobscript_chain.slurm.H200
# - NCHC 目前 rack partition 名稱是 gb200-rack1 / gb200-rack2, 不是 gb200-rack
PARTITION_CANDIDATES_RAW="${PARTITION_CANDIDATES:-GB200:gb200 GB200:gb200-full GB200:gb200-rack1 GB200:gb200-rack2 GB200:gb200-dev H200:dev}"
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
    log "dispatcher 退出 (rc=$rc); 清理 sentinel"
    rm -f "$SENTINEL" 2>/dev/null
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
    local best_target="" best_epoch=0 best_set=0

    log "  pick_cluster: partition ETA-compare (tolerance=${TIE_TOLERANCE_SEC}s)" >&2
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
        local js; js="$(cluster_jobscript "$c")" || continue
        if [ ! -f "$js" ]; then
            log "    [$target] 略過: jobscript $js 不存在" >&2
            continue
        fi
        local eta; eta="$(_pick_cluster_eta_epoch "$js" "$part")"
        if [ "$eta" -lt 0 ]; then
            log "    [$target] ETA 查詢失敗 (sbatch --test-only 無解析結果)" >&2
            continue
        fi
        local now; now="$(date +%s)"
        local wait_s=$((eta - now))
        [ "$wait_s" -lt 0 ] && wait_s=0
        log "    [$target] ETA wait ~= $(_pick_cluster_fmt_wait "$wait_s")" >&2

        if [ "$best_set" -eq 0 ]; then
            best_target="$target"; best_epoch="$eta"; best_set=1
        else
            # 只有「比目前最佳早 TIE_TOLERANCE_SEC 以上」才覆蓋
            # → 平手範圍內維持 PARTITION_CANDIDATES 先到先選, 避免抖動
            local delta=$((best_epoch - eta))
            if [ "$delta" -gt "$TIE_TOLERANCE_SEC" ]; then
                best_target="$target"; best_epoch="$eta"
            fi
        fi
    done

    if [ "$best_set" -eq 1 ] && [ -n "$best_target" ]; then
        log "  pick_cluster: 選中 $best_target (ETA-compare 結果)" >&2
        echo "$best_target"
        return 0
    fi

    log "  pick_cluster: 全部候選都不可投 (binary 缺 / sbatch --test-only 全部失敗) -> no-capacity" >&2
    echo ""
    return 1
}

# 用 sbatch --test-only 查單一 jobscript 的預期開始 epoch.
# 輸出: >=0 epoch (成功), -1 (失敗/無解析)
_pick_cluster_eta_epoch() {
    local js="$1" part="${2:-}" out eta_str
    command -v sbatch >/dev/null 2>&1 || { echo -1; return; }
    # [WALLTIME-FIX] --test-only 也帶 --time= 讓 SLURM backfill 用正確 walltime 排程
    local wt="" time_arg=""
    if [ -n "$part" ] && type gb200_partition_walltime >/dev/null 2>&1; then
        wt="$(gb200_partition_walltime "$part")"
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
    if type gb200_partition_walltime >/dev/null 2>&1; then
        wt="$(gb200_partition_walltime "$part")"
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

while true; do
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
