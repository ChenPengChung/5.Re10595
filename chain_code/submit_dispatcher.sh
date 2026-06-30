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

# ── 跨節點單例鎖 (atomic mkdir on shared FS) ─────────────────────────────────
# ~/.config/systemd/user 在共享 home → systemd enable = 全 5 登入節點都起 dispatcher。
# HEAD.lockdir 已保證單一 job 投遞(重複 dispatcher 不會重複投), 此鎖再讓他節點 dispatcher
# 自動退讓做到徹底乾淨。fail-open: 不確定一律工作, 絕不變成零 dispatcher(Layer 2 jobscript
# 自我續投仍是最後保命)。心跳檔由主迴圈每輪刷新。
mkdir -p restart 2>/dev/null
_DHOST="$(hostname)"; _DLOCK="restart/dispatcher.nodelock"; _DHB="restart/dispatcher.heartbeat"
_dhb_age()  { local ts; ts=$(cut -d: -f3 "$_DHB" 2>/dev/null); [ -n "${ts:-}" ] && echo $(( $(date +%s) - ts )) || echo 999999; }
_dhb_host() { cut -d: -f1 "$_DHB" 2>/dev/null; }
_dtake()    { rm -rf "$_DLOCK" 2>/dev/null; mkdir "$_DLOCK" 2>/dev/null && echo "$_DHOST:$$" > "$_DLOCK/owner" 2>/dev/null; printf '%s:%s:%s\n' "$_DHOST" "$$" "$(date +%s)" > "$_DHB" 2>/dev/null; return 0; }
if mkdir "$_DLOCK" 2>/dev/null; then
    echo "$_DHOST:$$" > "$_DLOCK/owner" 2>/dev/null; printf '%s:%s:%s\n' "$_DHOST" "$$" "$(date +%s)" > "$_DHB" 2>/dev/null
else
    _doh=$(_dhb_host); _dage=$(_dhb_age)
    if [ "${_doh:-}" = "$_DHOST" ]; then _dtake          # 本節點殘留鎖 → 奪回
    elif [ "$_dage" -lt 180 ]; then
        echo "[dispatcher] another login node (${_doh:-?}) owns dispatcher lock (hb ${_dage}s); deferring on $_DHOST" >&2
        exit 0                                            # 他節點活躍擁有 → 退讓(systemd 不重啟 exit 0)
    else _dtake; fi                                       # 他節點心跳過期(已死)→ 奪鎖; fail-open
fi

# [systemd/standalone] 自寫 pid + DISPATCHER_ACTIVE sentinel, 讓 jobscript hand-off 檢查
# (kill -0 dispatcher.pid + [ -f DISPATCHER_ACTIVE ]) 認得本 daemon 活著。
# 無論由 systemd (edit11-dispatcher.service) 或 dispatcher_start.sh 啟動皆正確; trap 在退出時清除。
mkdir -p restart 2>/dev/null
echo $$ > restart/dispatcher.pid 2>/dev/null || true
echo $$ > DISPATCHER_ACTIVE 2>/dev/null || true
trap '[ "$BASHPID" = "$$" ] && { rm -f DISPATCHER_ACTIVE restart/DISPATCHER_ACTIVE 2>/dev/null; [ "$(cut -d: -f1 "$_DHB" 2>/dev/null)" = "$_DHOST" ] && rm -rf "$_DLOCK" 2>/dev/null; }' EXIT  # 只在主程序退出時清(防 subshell 誤觸)+ 釋放本節點持有的單例鎖

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

# [NET-THROUGHPUT SELECTOR + JP-SWITCH] (2026-06-02) — replaces the defunct cluster:partition
# ETA model with a jp×partition net-throughput selector (select_combo_lib) + bit-exact
# jp-switch primitive (jpswitch_lib). select_combo_lib re-sources partition_lib + jpswitch_lib.
for _lib in jpswitch_lib.sh select_combo_lib.sh; do
    if [ -f "$CHAIN_DIR/tools/$_lib" ]; then . "$CHAIN_DIR/tools/$_lib"
    else echo "[dispatcher] FATAL: $CHAIN_DIR/tools/$_lib 不存在" >&2; exit 1; fi
done

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
# - H200 h200 共用 a.out.H200 / jobscript_chain.slurm.H200
# - 2026-06-01: 改用 4 天 walltime 的正式 partition (h200 / gb200);
#   原 dev / gb200-dev / rack / full 短 walltime 候選移除 (使用者要 wall==4days).
#   注意: GB200:gb200 需 a.out.GB200 (aarch64/sm_100) 才能被選中, 否則 dispatcher 跳過.
PARTITION_CANDIDATES_RAW="${PARTITION_CANDIDATES:-H200:h200 GB200:gb200}"
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
# Helper: 單一 jobid 的權威存活判定 → 印出 ACTIVE / TERMINAL / UNKNOWN
#   [2026-07-01 根因修復] 舊版 chain_has_active_job 只靠單次 `squeue -j`; NCHC
#   控制器 failover / federation 抖動時 squeue 會「瞬間回空」→ 誤判 head 已死 →
#   對還在跑的 head 幻影重投(160542 / 160600 重複 job 事件)。改為:
#     squeue(快路徑) → 回空才用 sacct State(權威) 交叉確認, retry 數次騎過暫態;
#     兩者都查不到 → UNKNOWN(由 caller fail-safe, 寧可不投也不重複投)。
#   [2026-07-01 嚴謹強化, 對抗驗證所得 — 三 lens review]
#     (a) sacct `-o State` 預設欄寬僅 10 → OUT_OF_MEMORY/SPECIAL_EXIT 被截成
#         'OUT_OF_ME+'/'SPECIAL_E+' 漏判 terminal → UNKNOWN → fail-safe 卡住不重投
#         (OOM head 永遠不續投=chain stall)。用 `State%30` 防截斷 + 前綴比對雙保險。
#     (b) requeue 會讓同一 jobid 多列 → 掃所有列: 任一 active→ACTIVE, 否則任一
#         terminal→TERMINAL(prefer-active, 不被舊列誤判 terminal)。
#     (c) squeue/sacct 一律 `timeout` 包住 → 控制器硬中斷時不卡死主迴圈 / 不拖垮
#         跨節點 heartbeat 觸發 lock 搶奪 churn(逾時回空 → 走 UNKNOWN fail-safe)。
# ─────────────────────────────────────────────────────────────────────────
_job_liveness() {
    local id="$1" attempt sq sa
    [[ "$id" =~ ^[0-9]+$ ]] || { echo UNKNOWN; return; }
    for attempt in 1 2 3; do
        sq="$(timeout 10 squeue -h -j "$id" -o '%T' 2>/dev/null | head -1 | tr -d '[:space:]')"
        case "$sq" in
            RUNNING|PENDING|CONFIGURING|COMPLETING|RESIZING|SUSPENDED|STOPPED|REQUEUED)
                echo ACTIVE; return ;;
        esac
        # squeue 回空/非 active → sacct State(權威, 不受 federation 顯示 quirk);
        # State%30 防欄寬截斷; 掃所有列前綴比對(相容截斷 + 'CANCELLED by <uid>' + requeue 多列);
        # active 補齊 STOPPED/SIGNALING/STAGE_OUT/REQUEUE(否則 squeue 空時誤判 UNKNOWN 灌 streak);
        # REVOKED 不列 terminal(federation 上 sibling REVOKED 可能對應另一叢集仍 RUNNING)→ UNKNOWN fail-safe
        sa="$(timeout 20 sacct -X -n -j "$id" -o State%30 2>/dev/null | tr -d ' \t')"
        if grep -qE '^(RUNNING|PENDING|CONFIGURI|COMPLETING|RESIZING|SUSPENDED|STOPPED|SIGNALING|STAGE_OUT|REQUEUE)' <<<"$sa"; then
            echo ACTIVE; return
        fi
        if grep -qE '^(CANCELLED|COMPLETED|FAILED|TIMEOUT|NODE_FAIL|OUT_OF_ME|BOOT_FAIL|DEADLINE|PREEMPTED|SPECIAL_E)' <<<"$sa"; then
            echo TERMINAL; return
        fi
        [ "$attempt" -lt 3 ] && sleep 2   # squeue 與 sacct 皆空 = 暫態抖動 → 重試(末輪不睡)
    done
    echo UNKNOWN   # retry 後仍無法確認 → caller fail-safe(視為 active, 絕不重投)
}

# ─────────────────────────────────────────────────────────────────────────
# Helper: 本 chain 是否已有 active job 在 queue (running/pending)
#   - 同時檢查 chain_jobid 與 HEAD.lockdir owner(single-head 權威記錄); 任何路徑
#     續投(含 jobscript Layer 2 自投)都會更新 lock, chain_jobid 可能落後。dedup 同 id。
#   - 用 _job_liveness 權威判定: 任一 ACTIVE → 有 active(並把 chain_jobid 同步到該 job)
#   - 任一 UNKNOWN(SLURM 抖動查不到) → fail-safe 視為 active, 絕不幻影重投
#   - 只有「所有 id 都確認 TERMINAL」才回報「無 active job」(才會進到 pick_cluster 重投)
#   - [嚴謹強化] 連續 UNKNOWN 超門檻(疑孤兒/purged id 或 dbd 長中斷, 非暫態) → 升級
#     告警到 tracked chain_code/health_watchdog_alerts.log, 避免靜默永久 stall。
# ─────────────────────────────────────────────────────────────────────────
chain_has_active_job() {
    local cur_id lock_id id live saw_unknown=0 unknown_id= seen=" "
    cur_id="$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]')"
    lock_id="$(grep '^jobid=' restart/HEAD.lockdir/owner 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
    for id in "$cur_id" "$lock_id"; do
        [ -z "$id" ] && continue
        [[ "$id" =~ ^[0-9]+$ ]] || continue
        case "$seen" in *" $id "*) continue ;; esac   # dedup: 同一 id 不重複查(省 SLURM 壓力/延遲)
        seen="$seen$id "
        live="$(_job_liveness "$id")"
        case "$live" in
            ACTIVE)
                if [ "$id" != "$cur_id" ]; then
                    printf '%s\n' "$id" > restart/chain_jobid.tmp 2>/dev/null && mv -f restart/chain_jobid.tmp restart/chain_jobid 2>/dev/null
                fi
                _liveness_unknown_since=; _liveness_alert_slot=
                return 0 ;;
            UNKNOWN)
                saw_unknown=1; unknown_id="$id" ;;
        esac
    done
    if [ "$saw_unknown" -eq 1 ]; then
        local _now _elapsed _slot
        _now="$(date +%s)"
        [ -z "${_liveness_unknown_since:-}" ] && _liveness_unknown_since="$_now"
        _elapsed=$(( _now - _liveness_unknown_since ))
        log "[liveness] WARN: job $unknown_id squeue+sacct 皆無法確認狀態(SLURM 抖動?) 已 ${_elapsed}s → fail-safe 視為 active, 本輪不重投" 2>/dev/null || true
        # 持續 UNKNOWN 超門檻(預設 20 分; 用 wall-clock 而非輪數 — 中斷時每輪 block 較久, 輪數不可靠)
        # = 幾乎確定孤兒/purged id 或 dbd 長中斷(非暫態) → 升級告警(每 ~10 分一次, 避免洗版);
        # 不自動重投(避免在真中斷時誤投), 交人工/監控(/loop /edit11 + Route B watchdog)處理。
        if [ "$_elapsed" -ge "${LIVENESS_UNKNOWN_ESCALATE_SEC:-1200}" ]; then
            _slot=$(( _elapsed / 600 ))
            if [ "${_liveness_alert_slot:-x}" != "$_slot" ]; then
                _liveness_alert_slot="$_slot"
                printf '%s [edit11][liveness-STALL] chain id=%s 已連續 %ds squeue+sacct 皆查不到(疑孤兒/purged id 或 dbd 長中斷); dispatcher fail-safe 卡住不重投, 需人工確認 restart/chain_jobid 是否有效\n' \
                    "$(date '+%Y-%m-%d %H:%M:%S')" "$unknown_id" "$_elapsed" \
                    >> chain_code/health_watchdog_alerts.log 2>/dev/null || true
            fi
        fi
        return 0
    fi
    _liveness_unknown_since=; _liveness_alert_slot=
    return 1
}

# 取得本 chain 最後一輪 head 的 exit code (若已結束)
chain_last_exit_code() {
    local cur_id lock_id head_id state ec
    cur_id="$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]')"
    lock_id="$(grep '^jobid=' restart/HEAD.lockdir/owner 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
    # [2026-07-01 嚴謹強化] 權威「最後一輪 head」= HEAD.lockdir owner(每次投遞 / jobscript
    # Layer-2 自投都更新它); chain_jobid 可能落後 → 優先用 lock_id 讀 exit code, 否則才
    # chain_jobid。避免「chain_jobid 落後 + 新 head 在 poll 空檔內 RC=42 秒崩」時讀到舊 job
    # 的 exit 0 而漏掉新 head 的 RC=42 unavoidable-stop → 不該續投卻續投。
    if [[ "$lock_id" =~ ^[0-9]+$ ]]; then head_id="$lock_id"; else head_id="$cur_id"; fi
    [[ "$head_id" =~ ^[0-9]+$ ]] || { echo ""; return; }

    # [2026-07-01 防禦+retry] 先確認 head 已達終態才回報 exit code。RUNNING job 的 ExitCode
    # 是 "0:0" → 舊版會把還在跑的 head 誤判為「乾淨退出 exit 0」→ 幻影重投。State%30 防欄寬
    # 截斷; tail -1 取 requeue 最新一列。★numeric head 但 SLURM 抖動「查不到/仍 active」→ 回
    # "UNKNOWN"(非空字串) → caller 本輪不重投: 避免「單次 sacct 空讀」就漏掉 RC=42 unavoidable
    # -stop 而誤續投(舊版回 "" 會被 caller 當綠燈直接投)。空字串只保留給 cold-start(無 head)。
    local attempt
    for attempt in 1 2 3; do
        state="$(timeout 20 sacct -X -n -j "$head_id" -o State%30 2>/dev/null | tr -d ' \t' | tail -1)"
        case "$state" in
            RUNNING*|PENDING*|CONFIGURI*|COMPLETING*|RESIZING*|SUSPENDED*|STOPPED*|SIGNALING*|STAGE_OUT*|REQUEUE*)
                echo "UNKNOWN"; return ;;                 # 理論上不該到(已過 active gate)→ 保守不投
            ?*)                                            # 非空且非 active = 終態 → 讀 exit code
                # [BUGFIX] ExitCode 格式 "<exit_code>:<signal>"; "${ec%%:*}" 取前段才是真 exit code
                ec="$(timeout 20 sacct -X -n -j "$head_id" -o ExitCode 2>/dev/null | tail -1 | tr -d '[:space:]')"
                [ -z "$ec" ] && { echo "UNKNOWN"; return; } # ExitCode 也讀不到 → 保守不投
                echo "${ec%%:*}"; return ;;
        esac
        [ "$attempt" -lt 3 ] && sleep 2                    # state 空 = SLURM 抖動 → 重試(末輪不睡)
    done
    echo "UNKNOWN"                                         # 3 次仍空 → 不確定 → 本輪不重投
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
    local jp="$1" part="$2"
    local cur; cur="$(jpswitch_current_jp)"
    # [Codex P5 fix] never let cur be blank/0 → fall back to variables.h jp (else jp=0 -> bad sbatch)
    if ! { [ -n "$cur" ] && [ "$cur" -gt 0 ] 2>/dev/null; }; then
        cur="$(grep -E '^#define[[:space:]]+jp[[:space:]]+[0-9]+' variables.h | grep -oE '[0-9]+' | head -1)"
    fi
    cur="${cur:-32}"
    local jobscript="chain_code/jobscript_chain.slurm.H200"

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
    if ! acquire_head_lock "dispatcher-jp${jp}-${part}"; then
        log "[SINGLE-HEAD] acquire_head_lock 失敗 (HEAD.lockdir 已被佔), 放棄本次投遞"
        return 4
    fi
    log "[SINGLE-HEAD] ✓ 取得 HEAD.lockdir, state=SUBMITTING, 準備 sbatch"

    # ── jp-switch (bit-exact, stats-preserving) if selected jp ≠ current ──
    if [ "$jp" != "$cur" ]; then
        log "▷ jp-switch ${cur} -> ${jp}: mark REPARTITIONING (lock held ~min) + jpswitch_apply"
        mark_head_repartitioning "dispatcher-jp${jp}"
        if jpswitch_apply "$jp"; then
            log "▷ jp-switch OK: jp=$jp (a.out=a.out.jp${jp}, checkpoint repartitioned bit-exact)"
        else
            log "WARN jp-switch to $jp FAILED -> fall back to current jp=$cur (never idle)"
            jp="$cur"
            # [Codex P5 fix, hardened] verified restore: both binaries, or abort (don't submit a bad binary)
            if [ -s "a.out.jp${cur}" ] && cp -f "a.out.jp${cur}" a.out && cp -f "a.out.jp${cur}" a.out.H200; then
                log "▷ fallback binaries restored to jp=${cur}"
            else
                log "FATAL: fallback binary a.out.jp${cur} missing/uncopyable — cannot submit safely; release lock"
                release_head_lock; return 2
            fi
        fi
    else
        cp -f "a.out.jp${cur}" a.out 2>/dev/null && cp -f "a.out.jp${cur}" a.out.H200 2>/dev/null
        if [ ! -s a.out ]; then
            log "ERROR: a.out.jp${cur} 缺失, 釋放 HEAD.lockdir"; release_head_lock; return 2
        fi
    fi

    # [BLACKLIST-LIB] 黑名單統一走 bl_effective_exclude (TTL + NCHC sync + 50% cap)
    local ex_list exclude_arg=""
    ex_list="$(bl_effective_exclude "$part" 2>>"$LOG_FILE")"
    [ -n "$ex_list" ] && exclude_arg="--exclude=$ex_list"
    log "▷ effective exclude (partition=$part): ${ex_list:-(empty)}"

    # [WALLTIME] H200 partition → max walltime (partition_lib h200 map; jobscript fallback)
    local wt=""
    wt="$(h200_partition_walltime "$part")"
    if [ -z "$wt" ]; then
        wt="$(awk -F= '/^#SBATCH[[:space:]]+--time=/{gsub(/^[[:space:]]+|[[:space:]]+$/,"",$2); print $2; exit}' "$jobscript" 2>/dev/null)"
    fi

    # [SIZE] jp=N → N/8 H200 nodes × 8 GPU; size via sbatch CLI (jobscript mpirun reads $SLURM_NTASKS)
    local nodes=$((jp / 8))
    log "▷ sbatch --partition=$part --account=$ACCOUNT --nodes=$nodes --ntasks-per-node=8 --gres=gpu:8 --time=$wt $exclude_arg $jobscript"
    local next_id
    next_id="$(sbatch --parsable --partition="$part" --account="$ACCOUNT" \
        --nodes="$nodes" --ntasks-per-node=8 --gres=gpu:8 --time="$wt" \
        $exclude_arg "$jobscript" 2>&1)"
    local rc=$?

    if [ $rc -eq 0 ] && [[ "$next_id" =~ ^[0-9]+$ ]]; then
        log "SUBMIT-OK 已投 jp=$jp part=$part round: jobid=$next_id"
        echo "$next_id" > restart/chain_jobid
        # [COLD-START-INIT] 冷啟動情境下 chain_count 還不存在, 一併初始化以免 jobscript
        # 誤觸 "[REVIEW-FIX #7] chain state 半損毀" FATAL tripwire 形成無限迴圈.
        # 續跑情境 chain_count 已存在, 不覆寫.
        if [ ! -f restart/chain_count ]; then
            echo "1" > restart/chain_count
            log "[COLD-START-INIT] 初始化 restart/chain_count=1"
        fi
        # [SINGLE-HEAD] 把 jobid 寫進 HEAD.lockdir (state: SUBMITTING -> PENDING)
        if write_head_jobid "$next_id" "jp${jp}"; then
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
for _jp in $SC_VALID_JP; do
    if jpswitch_binary_ready "$_jp" >/dev/null 2>&1; then
        log "OK 偵測到 a.out.jp${_jp} (jp=$_jp ready)"; _init_ok=1
    fi
done
if [ "$_init_ok" -eq 0 ]; then
    log "FATAL: 沒有任何 a.out.jp<N> binary (需至少一個, 例如 a.out.jp32). 請先 pre-build."
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

# [PENDING RE-SELECT] (2026-06-02) 若 head job PENDING 太久, 用 job-guard 取消 (含 PENDING→RUNNING
# race-guard) 後 re-select 能更快開跑的組合. 滿足使用者切換時機 (b): pending 時重選.
# [TEMP-LOCK 2026-06-12] 暫時關閉 PENDING-churn: 原預設 10min 太短, GPU 滿載時每 10min
#   cancel+重投 → 永遠累積不到排隊年資、撐不到 backfill 窗。調高到 1440(=24h) 等同不 churn,
#   讓 head 撐住 16gpus 保留位開跑。解鎖還原: 改回 "${SC_PENDING_TIMEOUT_MIN:-10}"
SC_PENDING_TIMEOUT_MIN="${SC_PENDING_TIMEOUT_MIN:-1440}"
_pending_too_long() {
    local jid st pe now age
    jid="$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]')"
    [[ "$jid" =~ ^[0-9]+$ ]] || return 1
    st="$(squeue -h -j "$jid" -o '%T' 2>/dev/null | tr -d '[:space:]')"
    [ "$st" = "PENDING" ] || return 1
    pe="$(grep '^pending_at_epoch=' "$HEAD_LOCK_DIR/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
    [ -n "$pe" ] || return 1
    now=$(date +%s); age=$((now - pe))
    [ "$age" -ge "$((SC_PENDING_TIMEOUT_MIN * 60))" ]
}
_cancel_head_for_reselect() {
    local jid st
    jid="$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]')"
    st="$(squeue -h -j "$jid" -o '%T' 2>/dev/null | tr -d '[:space:]')"
    if [ "$st" != "PENDING" ]; then
        log "[PENDING] race-guard: $jid 已非 PENDING (now=$st) -> 放棄 re-select, 保留它"; return 1
    fi
    log "[PENDING] $jid pending > ${SC_PENDING_TIMEOUT_MIN}min -> job-guard scancel + re-select"
    ./run job-guard scancel "$jid" >>"$LOG_FILE" 2>&1
    sleep 3
    release_head_lock 2>/dev/null || rm -rf "$HEAD_LOCK_DIR" 2>/dev/null
    return 0
}

# [P0 TRAP #2 FIX] 連續找不到 capacity 的輪數
_nocapacity_count=0
_pending_reselect=   # [Codex A2 fix] set to 1 after a PENDING-timeout cancel → next select uses 1h horizon

while true; do
    printf '%s:%s:%s\n' "$(hostname)" "$$" "$(date +%s)" > restart/dispatcher.heartbeat 2>/dev/null || true   # 跨節點心跳(o source-3; 判哪個登入節點在跑)
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

    # 如果 chain 目前還有 active job, 等它結束 — 但 PENDING 太久則 cancel + re-select (never idle)
    if chain_has_active_job; then
        if _pending_too_long && _cancel_head_for_reselect; then
            _pending_reselect=1   # [Codex A2 fix] re-select using the 1h pending horizon
        else
            sleep "$POLL_INTERVAL"
            continue
        fi
    fi

    # ── 進到「該投下一輪」的分支 ──
    # 先查上一輪 exit code
    LAST_EC="$(chain_last_exit_code)"
    # [2026-07-01] numeric head 但 SLURM 抖動無法確認 exit code → chain_last_exit_code 回 "UNKNOWN"
    # → 本輪不重投(避免漏判 RC=42 unavoidable-stop 而誤續投), 下輪 SLURM 恢復再判。
    if [ "$LAST_EC" = "UNKNOWN" ]; then
        log "[liveness] 無法確認上一輪 head exit code(SLURM 抖動?) → 本輪不重投, ${POLL_INTERVAL}s 後再判"
        sleep "$POLL_INTERVAL"; continue
    fi
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

    # [Codex C2 fix] learn realized r_ftt for the just-finished jp from timing_log before selecting
    sc_update_r_ftt "$(jpswitch_current_jp)" 2>/dev/null || true
    # 選 (jp × partition) — net-throughput optimal, never-idle (select_combo_lib)
    # [Codex A2 fix] after a PENDING re-select, use the 1h pending horizon (favours start-now)
    log "----- 準備投下一輪: net-throughput 選 (jp × partition)${_pending_reselect:+ [pending re-select, 1h horizon]} -----"
    # [完全開啟 audit] 每一組候選 (SC_VALID_JP × SC_PARTITIONS) 都「實際評估後才跳過(帶理由)」, 寫入 log 供稽核
    sc_audit "$([ -n "$_pending_reselect" ] && echo "$SC_HORIZON_PEND_H" || echo "$SC_HORIZON_H")" 2>/dev/null \
        | while IFS= read -r _l; do log "    候選 $_l"; done
    NEXT_COMBO="$(sc_pick_combo ${_pending_reselect:+--pending})"
    _pending_reselect=
    NEXT_JP="${NEXT_COMBO%% *}"
    NEXT_PART="${NEXT_COMBO##* }"
    if [ -z "$NEXT_COMBO" ] || ! [[ "$NEXT_JP" =~ ^[0-9]+$ ]] || [ -z "$NEXT_PART" ]; then
        # 無可投組合 (極罕見: dev 不限額, 通常恆有 fallback). 計入 no-capacity.
        _nocapacity_count=$((_nocapacity_count + 1))
        log "sc_pick_combo 無可投組合 (binary 缺 / 全部 cap-blocked / sbatch --test-only 無解析), ${POLL_INTERVAL}s 後重試 (no-capacity ${_nocapacity_count}/${NOCAPACITY_LIMIT})"
        if [ "$_nocapacity_count" -ge "$NOCAPACITY_LIMIT" ]; then
            _total_wait_min=$(( _nocapacity_count * POLL_INTERVAL / 60 ))
            log "============================================================================="
            log "[P0 TRAP #2] 連續 ${_nocapacity_count} 輪 sc_pick_combo 無可投組合 (累積 ${_total_wait_min} 分鐘)"
            log "             可能原因: (a) 無 a.out.jp<N> binary (b) 帳號 cap 全滿且 dev 也排不到 (c) Slurm controller 暫停"
            log "             觸發明確停機: 寫入 $NOCAPACITY_SENTINEL 後退出."
            log "             -- 恢復: 確認有空後 rm $NOCAPACITY_SENTINEL && ./run dispatcher start"
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
        sleep "$POLL_INTERVAL"
        continue
    fi

    log "選中: jp=$NEXT_JP partition=$NEXT_PART (net-throughput)"
    submit_round "$NEXT_JP" "$NEXT_PART"
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
