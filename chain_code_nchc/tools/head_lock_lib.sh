#!/usr/bin/env bash
# ==============================================================================
# tools/head_lock_lib.sh  —  Single-Head per Folder 共用函式庫
# ==============================================================================
# 中心準則 (Single-Head Invariant):
#   每一格資料夾在任何時刻, queue 內最多只能有 1 個屬於本 chain 的 job.
#   不論 PENDING 還是 RUNNING, 都由 restart/HEAD.lockdir 這個 single sentinel 把關.
#
# Lifecycle:
#   EMPTY
#     │  (submitter atomic mv -T stage)  state=SUBMITTING, jobid=TBD
#     ▼
#   HEAD.lockdir [state=SUBMITTING]
#     │  (submitter 呼叫 sbatch)
#     │─ 成功 → write_head_jobid → HEAD.lockdir [state=PENDING, jobid=X]
#     │         │  (jobscript 進 RUNNING 時)
#     │         │  verify_am_head + upgrade state → [state=RUNNING, jobid=X]
#     │         │  trap EXIT 清除
#     │         ▼ EMPTY
#     │─ 失敗 → release_head_lock → EMPTY
#
# 公用函式 API:
#   acquire_head_lock <submitter_tag>              -> rc 0=取得, 1=忙, 2=stale 清理後仍取不到
#   write_head_jobid  <jobid> <cluster>            -> 覆寫 owner, state 改 PENDING
#   verify_am_head    <jobid> <cluster>            -> rc 0=我是 head (並升級 state=RUNNING), 非 0=否
#   release_head_lock                              -> rm -rf HEAD.lockdir (submit 失敗用)
#   release_head_lock_if_mine <jobid>              -> 僅在 owner.jobid 仍是自己時清 (EXIT trap)
# ==============================================================================

HEAD_LOCK_DIR="${HEAD_LOCK_DIR:-restart/HEAD.lockdir}"
HEAD_STAGE_PREFIX="${HEAD_STAGE_PREFIX:-restart/.headstage}"
HEAD_STALE_TIMEOUT="${HEAD_STALE_TIMEOUT:-30}"   # 秒; SUBMITTING 超過此值視為 stale
# [P1] 一個 jp-switch 會持鎖做 repartition (~數分鐘). 用 REPARTITIONING state, 容忍較長 age
# (預設 15 分) 才視為 stale, 避免被其他 submitter 在 30s SUBMITTING timeout 後誤搶 → 雙投.
HEAD_REPART_TIMEOUT="${HEAD_REPART_TIMEOUT:-900}"  # 秒; REPARTITIONING 超過此值才視為 stale

# 內部: 查 squeue 是否有某 jobid 活著 (echo state / 空字串)
_head_squeue_state() {
    local jid="$1"
    [ -z "$jid" ] || [ "$jid" = "TBD" ] && { echo ""; return; }
    if ! command -v squeue >/dev/null 2>&1; then
        # 測試環境無 squeue, fallback: 呼叫 mock 函式 (若已定義)
        if command -v mock_squeue >/dev/null 2>&1; then
            mock_squeue -h -j "$jid" -o '%T' 2>/dev/null | tr -d '[:space:]'
            return
        fi
        echo ""
        return
    fi
    local _st
    _st="$(squeue -h -j "$jid" -o '%T' 2>/dev/null | head -1 | tr -d '[:space:]')"
    case "$_st" in
        RUNNING|PENDING|CONFIGURING|COMPLETING|RESIZING|SUSPENDED)
            printf '%s' "$_st"; return ;;
    esac
    # [SQUEUE-BLIND FALLBACK] NCHC h200 在 slurmctld failover / cross-cluster federation
    # 盲窗時, squeue -h -j 可能查不到「仍在跑」的 job (回空字串) → 誤判 job 已結束.
    # 三道 single-head guard (chain_has_active_job / LAYER 2 / verify_am_head Case-c) 都靠
    # 本函式; 盲窗一到就同時 fail-open → 重複投遞, 或後到的 job 搶 head 造成雙跑撞 checkpoint.
    # 修法: squeue 查無 active 時改用 sacct (authoritative on NCHC h200) 覆核; 只有 squeue
    # 與 sacct 皆非 active 才回該狀態. 比照同 codebase 既有範本 presubdep_successor_alive().
    if command -v sacct >/dev/null 2>&1; then
        local _sst
        _sst="$(sacct -X -n -j "$jid" -o State 2>/dev/null | head -1 | awk '{print $1}' | tr -d '[:space:]')"
        case "$_sst" in
            RUNNING|PENDING|CONFIGURING|COMPLETING|RESIZING|SUSPENDED)
                printf '%s' "$_sst"; return ;;
        esac
        # squeue 回空但 sacct 有明確終態 → 回 sacct 終態, 讓 caller 照常判定已結束
        [ -n "$_sst" ] && { printf '%s' "$_sst"; return; }
    fi
    printf '%s' "$_st"
}

# 內部: 寫 staging owner
_head_write_stage_owner() {
    local stage="$1" tag="$2"
    cat > "$stage/owner" <<EOF_STAGE
state=SUBMITTING
jobid=TBD
submitter=$tag
submitter_pid=$$
submitted_at=$(date -Iseconds 2>/dev/null || date)
submitted_at_epoch=$(date +%s)
hostname=$(hostname 2>/dev/null || echo unknown)
EOF_STAGE
}

# 取得 HEAD lock. 成功 rc=0, 失敗 rc=1 (有別人在用)
acquire_head_lock() {
    local tag="${1:-unknown}"
    local _stage
    _stage="$(mktemp -d "${HEAD_STAGE_PREFIX}.XXXXXX" 2>/dev/null)" || return 1
    _head_write_stage_owner "$_stage" "$tag"
    if mv -T "$_stage" "$HEAD_LOCK_DIR" 2>/dev/null; then
        return 0
    fi
    rm -rf "$_stage" 2>/dev/null

    # 有別人持, 檢查是否 stale
    local cur_state cur_jid cur_epoch now age
    cur_state="$(grep '^state=' "$HEAD_LOCK_DIR/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
    cur_jid="$(grep '^jobid=' "$HEAD_LOCK_DIR/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
    cur_epoch="$(grep '^submitted_at_epoch=' "$HEAD_LOCK_DIR/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
    now="$(date +%s)"
    age=$((now - ${cur_epoch:-0}))

    local stale=0
    case "$cur_state" in
        SUBMITTING)
            # 若 submitter 超時沒進入 PENDING → 視為 submitter 崩了
            [ "$age" -gt "$HEAD_STALE_TIMEOUT" ] && stale=1
            ;;
        REPARTITIONING)
            # [P1] dispatcher 正在持鎖做 jp-switch repartition; 容忍較長 age 才視為 stale
            [ "$age" -gt "$HEAD_REPART_TIMEOUT" ] && stale=1
            ;;
        PENDING|RUNNING)
            local live; live="$(_head_squeue_state "$cur_jid")"
            case "$live" in
                RUNNING|PENDING|CONFIGURING|COMPLETING|RESIZING|SUSPENDED) stale=0 ;;
                *) stale=1 ;;
            esac
            ;;
        *)
            # unknown / empty → 若老化夠久就清
            [ "$age" -gt "$HEAD_STALE_TIMEOUT" ] && stale=1
            ;;
    esac

    if [ "$stale" -eq 1 ]; then
        rm -rf "$HEAD_LOCK_DIR" 2>/dev/null
        _stage="$(mktemp -d "${HEAD_STAGE_PREFIX}.XXXXXX" 2>/dev/null)" || return 1
        _head_write_stage_owner "$_stage" "$tag"
        if mv -T "$_stage" "$HEAD_LOCK_DIR" 2>/dev/null; then
            return 0
        fi
        rm -rf "$_stage" 2>/dev/null
    fi
    return 1
}

# 寫 jobid 進 HEAD.lockdir (state → PENDING). 僅在 acquire_head 成功後呼叫.
write_head_jobid() {
    local jid="$1" cluster="$2"
    [ -d "$HEAD_LOCK_DIR" ] || return 1
    local tmp; tmp="$(mktemp "$HEAD_LOCK_DIR/.owner.XXXXXX")" || return 1
    cat > "$tmp" <<EOF_PEND
state=PENDING
jobid=$jid
cluster=$cluster
hostname=$(hostname 2>/dev/null || echo unknown)
pending_at=$(date -Iseconds 2>/dev/null || date)
pending_at_epoch=$(date +%s)
EOF_PEND
    mv -f "$tmp" "$HEAD_LOCK_DIR/owner"
}

# [P1] 標記 HEAD.lockdir 進入 REPARTITIONING (jp-switch 期間). 在 acquire_head_lock 成功
# (state=SUBMITTING) 之後、開始 repartition 之前呼叫. 重置 submitted_at_epoch, 讓
# HEAD_REPART_TIMEOUT (預設 15 分) 從 repartition 起算; 完成後再 write_head_jobid 轉 PENDING.
mark_head_repartitioning() {
    local tag="${1:-jp-switch}"
    [ -d "$HEAD_LOCK_DIR" ] || return 1
    local tmp; tmp="$(mktemp "$HEAD_LOCK_DIR/.owner.XXXXXX")" || return 1
    cat > "$tmp" <<EOF_REPART
state=REPARTITIONING
jobid=TBD
submitter=$tag
submitter_pid=$$
submitted_at=$(date -Iseconds 2>/dev/null || date)
submitted_at_epoch=$(date +%s)
hostname=$(hostname 2>/dev/null || echo unknown)
EOF_REPART
    mv -f "$tmp" "$HEAD_LOCK_DIR/owner"
}

# 內部: 升級 state 為 RUNNING 並寫回 owner (寫入成功 rc=0)
_head_upgrade_to_running() {
    local my_jid="$1" my_cluster="$2"
    local tmp; tmp="$(mktemp "$HEAD_LOCK_DIR/.owner.XXXXXX")" || return 12
    cat > "$tmp" <<EOF_RUN
state=RUNNING
jobid=$my_jid
cluster=$my_cluster
hostname=$(hostname 2>/dev/null || echo unknown)
running_at=$(date -Iseconds 2>/dev/null || date)
running_at_epoch=$(date +%s)
EOF_RUN
    mv -f "$tmp" "$HEAD_LOCK_DIR/owner"
    return 0
}

# 內部: self-heal — 清掉損壞/陳舊的 HEAD.lockdir, 用我的 jobid 重建後升級為 RUNNING.
# 僅在下列情境呼叫 (由 verify_am_head 判斷後):
#   (a) HEAD.lockdir 不存在
#   (b) owner 檔損毀 (無法解析 jobid)
#   (c) owner 持有者是別的 jobid, 但該 jobid 在 squeue 中已不存在 (crash / 前一輪未清)
_head_self_heal_and_take() {
    local my_jid="$1" my_cluster="$2" reason="$3"
    printf '[head_lock] SELF-HEAL 觸發 (reason=%s, my_jid=%s, cluster=%s)\n' \
           "$reason" "$my_jid" "$my_cluster" >&2
    # 保留一份損毀的 owner 作為事後分析 (若有)
    if [ -d "$HEAD_LOCK_DIR" ] && [ -f "$HEAD_LOCK_DIR/owner" ]; then
        local salvage="restart/HEAD.lockdir.healed.$(date +%s).${my_jid}"
        cp -f "$HEAD_LOCK_DIR/owner" "$salvage" 2>/dev/null && \
            printf '[head_lock]   舊 owner 備份至 %s\n' "$salvage" >&2
    fi
    rm -rf "$HEAD_LOCK_DIR" 2>/dev/null
    local _stage
    _stage="$(mktemp -d "${HEAD_STAGE_PREFIX}.XXXXXX" 2>/dev/null)" || return 13
    cat > "$_stage/owner" <<EOF_HEAL
state=PENDING
jobid=$my_jid
cluster=$my_cluster
hostname=$(hostname 2>/dev/null || echo unknown)
pending_at=$(date -Iseconds 2>/dev/null || date)
pending_at_epoch=$(date +%s)
healed_from=$reason
healed_at_epoch=$(date +%s)
EOF_HEAL
    if ! mv -T "$_stage" "$HEAD_LOCK_DIR" 2>/dev/null; then
        rm -rf "$_stage" 2>/dev/null
        # 極罕見: 剛好另一個 submitter 在此瞬間也 acquire 成功 → 放棄 heal
        printf '[head_lock]   heal 寫入競爭敗北, 放棄 self-heal\n' >&2
        return 14
    fi
    _head_upgrade_to_running "$my_jid" "$my_cluster" || return $?
    printf '[head_lock]   heal 完成, HEAD.lockdir 重建為 jobid=%s state=RUNNING\n' "$my_jid" >&2
    return 0
}

# jobscript 開場: 驗證自己是 HEAD 並把 state 升級成 RUNNING.
# rc 0=驗證通過 (或 self-heal 成功), 非 0=真的有別人活著持鎖 → caller 應 exit 42.
#
# [P1 TRAP #3 FIX] 加 self-heal: 下列情境不再直接回錯, 而是嘗試自救 —
#   (a) HEAD.lockdir 不見 (NFS 抖動 / 被誤刪)
#   (b) owner 檔空白 / 格式損毀 / 無 jobid
#   (c) owner jobid 非我, 但該 jobid 在 squeue 中已不存在 (上輪 crash 殘留)
# 只有「owner jobid 非我且該 jobid 仍在 squeue 跑」時, 才返回 11 讓 caller exit 42.
verify_am_head() {
    local my_jid="$1" my_cluster="$2"

    # 先基本 sanity: 沒有合法 my_jid 就沒得驗
    if [ -z "$my_jid" ] || ! printf '%s' "$my_jid" | grep -qE '^[0-9]+$'; then
        printf '[head_lock] verify_am_head: 非法 my_jid=%q, 拒絕 heal\n' "$my_jid" >&2
        return 20
    fi

    # Case (a): HEAD.lockdir 不存在 → self-heal (submit 路徑漏了, 但我是合法 Slurm job)
    if [ ! -d "$HEAD_LOCK_DIR" ]; then
        _head_self_heal_and_take "$my_jid" "$my_cluster" "missing-lockdir"
        return $?
    fi

    # 讀 owner. 若 owner 檔不存在或讀取 timeout → 視為損毀
    local head_jid head_state
    if [ ! -f "$HEAD_LOCK_DIR/owner" ]; then
        _head_self_heal_and_take "$my_jid" "$my_cluster" "missing-owner-file"
        return $?
    fi
    head_jid="$(grep '^jobid=' "$HEAD_LOCK_DIR/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
    head_state="$(grep '^state=' "$HEAD_LOCK_DIR/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"

    # Case (b): owner 損毀 — 無法解析 jobid 或為空 (排除合法 "TBD" 但 state=SUBMITTING)
    if [ -z "$head_jid" ]; then
        _head_self_heal_and_take "$my_jid" "$my_cluster" "corrupt-owner-empty-jobid"
        return $?
    fi
    if [ "$head_jid" = "TBD" ]; then
        # [RACE-WINDOW FIX] owner=TBD state=SUBMITTING 表示 dispatcher 剛 stage lock
        # 但還沒 write_head_jobid. 若 sbatch 回 jobid 比 write_head_jobid 快, jobscript
        # 可能 1-3s 內就在 compute node 起跑, 搶在 dispatcher 更新 owner 之前.
        # 正確做法: 等 dispatcher 把 jobid 寫進來 (最多 HEAD_TBD_RETRY_LIMIT 秒), 再判斷.
        local retry_limit="${HEAD_TBD_RETRY_LIMIT:-20}"
        local retries=0
        while [ "$head_jid" = "TBD" ] && [ "$retries" -lt "$retry_limit" ]; do
            sleep 1
            retries=$((retries + 1))
            head_jid="$(grep '^jobid=' "$HEAD_LOCK_DIR/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
            head_state="$(grep '^state=' "$HEAD_LOCK_DIR/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
        done
        if [ "$head_jid" = "TBD" ]; then
            # 等完 retry 仍 TBD → submitter 真的掛了
            local head_epoch
            head_epoch="$(grep '^submitted_at_epoch=' "$HEAD_LOCK_DIR/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
            local age=$(( $(date +%s) - ${head_epoch:-0} ))
            if [ "$age" -gt "$HEAD_STALE_TIMEOUT" ]; then
                _head_self_heal_and_take "$my_jid" "$my_cluster" "stale-TBD-after-retry-age=${age}s"
                return $?
            else
                printf '[head_lock] verify: 等 %ds 後 owner 仍 TBD state=%s age=%ss, return 11\n' \
                       "$retries" "$head_state" "$age" >&2
                return 11
            fi
        fi
        printf '[head_lock] verify: race window 結束 (等 %ds), owner jobid=%s state=%s, 我 jobid=%s → 繼續後續驗證\n' \
               "$retries" "$head_jid" "$head_state" "$my_jid" >&2
        # 這裡 head_jid 已是 numeric, 不 return, 讓下面的「head_jid==my_jid」或「查 squeue」邏輯接手
    fi
    if ! printf '%s' "$head_jid" | grep -qE '^[0-9]+$'; then
        _head_self_heal_and_take "$my_jid" "$my_cluster" "corrupt-owner-nonnumeric-jobid"
        return $?
    fi

    # Case: 我就是 head (正常路徑) → 升級 state 為 RUNNING
    if [ "$head_jid" = "$my_jid" ]; then
        _head_upgrade_to_running "$my_jid" "$my_cluster"
        return $?
    fi

    # Case (c): head_jid != my_jid → 查 squeue. 若對方已死就 heal, 還活就退出.
    local other_state; other_state="$(_head_squeue_state "$head_jid")"
    case "$other_state" in
        RUNNING|PENDING|CONFIGURING|COMPLETING|RESIZING|SUSPENDED)
            # 對方還活著 → 真的衝突, 我不是合法 head
            printf '[head_lock] verify: owner jobid=%s 仍在 squeue (state=%s), 我 jobid=%s 非 head\n' \
                   "$head_jid" "$other_state" "$my_jid" >&2
            return 11
            ;;
        *)
            # 對方在 squeue 查不到 → 上輪 jobscript crash 前沒清 lock, heal it
            _head_self_heal_and_take "$my_jid" "$my_cluster" \
                "stale-owner-jobid=${head_jid}-squeue-state=${other_state:-unknown}"
            return $?
            ;;
    esac
}

# 釋放 HEAD.lockdir (submit 失敗或 jobscript EXIT 且仍是自己).
release_head_lock() {
    rm -rf "$HEAD_LOCK_DIR" 2>/dev/null
}

# 專門供 jobscript trap EXIT 用: 若 owner.jobid 已換成別人 (續投成功), 不清.
release_head_lock_if_mine() {
    local my_jid="$1"
    [ -d "$HEAD_LOCK_DIR" ] || return 0
    local cur; cur="$(grep '^jobid=' "$HEAD_LOCK_DIR/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
    if [ "$cur" = "$my_jid" ]; then
        rm -rf "$HEAD_LOCK_DIR" 2>/dev/null
    fi
}
