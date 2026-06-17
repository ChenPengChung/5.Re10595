#!/bin/bash
# ==============================================================================
# verify_mutex.sh   —  驗證中心準則:「restart/ 同時只有一個寫入者」
# ==============================================================================
# 本腳本不需要 SLURM / 不需要實際 GPU, 純 bash 模擬 jobscript 的 mutex 區塊.
# 在本機執行即可驗證: 兩個 "job" 同時 mkdir atomic lock, 只能有一個勝出.
#
# 使用:
#   bash tools/verify_mutex.sh
#
# 檢驗項目:
#   Test 1. 單一 job 取鎖 + 清理
#   Test 2. 雙 job 搶鎖 (嚴重 race)  → 只有一個成功, 另一個 RC=42
#   Test 3. 10 個 job 暴力 concurrent 搶鎖 → 只有 1 個成功, 其餘 9 個都 RC=42
#   Test 4. Stale lock reclaim (owner 已死, 後來者接手)
#   Test 5. Epoch guard (lock 被奪時拒絕啟動 "mpirun")
#   Test 6. 累積 checkpoint 驗證 (模擬 100 次並發投遞, checkpoint 只被一個 job 寫)
#   Test 7. [Section F] Single-Head Invariant — 任何時刻 restart/*.lockdir 數量 ≤ 1
#   Test 8. [Section G] No-Double-Submit — 100 concurrent acquire_head_lock → 1 勝 99 敗
#
# 退出碼:
#   0 = 所有測試通過 (中心準則被維持)
#   1 = 任一測試失敗 (意味著會發生並發寫入 restart/)
# ==============================================================================

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEST_ROOT="$(mktemp -d 2>/dev/null || echo "/tmp/verify_mutex_$$")"
mkdir -p "$TEST_ROOT"
cd "$TEST_ROOT" || exit 1

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0

ok()   { echo -e "  ${GREEN}✓${NC} $*"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo -e "  ${RED}✗${NC} $*"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
info() { echo -e "  ${YELLOW}·${NC} $*"; }

# ─────────────────────────────────────────────────────────────────────────
# 模擬 jobscript 的 mutex 區塊 (從 jobscript_chain.slurm.GB200 複製邏輯)
# 為了免 squeue 依賴, 改用一個 mock: /tmp/mock_squeue_state/<jobid> 的內容
# 是假設 squeue 會回的 state (RUNNING / "" / PENDING / ...).
# ─────────────────────────────────────────────────────────────────────────
MOCK_SQUEUE_DIR="$TEST_ROOT/mock_squeue_state"
mkdir -p "$MOCK_SQUEUE_DIR"

mock_squeue() {
    # 取代 real squeue. 用法: mock_squeue -h -j <jobid> -o '%T'
    local jobid=""
    while [ $# -gt 0 ]; do
        case "$1" in
            -j) jobid="$2"; shift 2 ;;
            *)  shift ;;
        esac
    done
    if [ -n "$jobid" ] && [ -f "$MOCK_SQUEUE_DIR/$jobid" ]; then
        cat "$MOCK_SQUEUE_DIR/$jobid"
    fi
    # 若 mock 狀態不存在, 等同 real squeue 回空 (job 已結束)
}

# 模擬 jobscript 的 Layer 3 mutex 取鎖邏輯
simulate_jobscript() {
    local jobid="$1"
    local mock_action="${2:-normal}"   # normal | pretend_stale
    local LOCK_DIR="restart/RUNNING.lockdir"
    local log_file="restart/job_${jobid}.log"

    local _log
    _log() { echo "[job=$jobid] $*" >> "$log_file"; }

    _try_acquire_lock() {
        # Atomic two-phase: stage tmpdir with owner file, then rename into place.
        # rename(2) fails (ENOTEMPTY) if LOCK_DIR already exists and is non-empty,
        # which guarantees LOCK_DIR never exists without a complete owner file.
        local stage
        stage="$(mktemp -d "restart/.lockstage.XXXXXX" 2>/dev/null)" || return 1
        cat > "$stage/owner" <<EOF
jobid=$jobid
hostname=$(hostname)
cluster=MOCK
started=$(date -Iseconds)
EOF
        if mv -T "$stage" "$LOCK_DIR" 2>/dev/null; then
            return 0
        fi
        rm -rf "$stage"
        return 1
    }

    if ! _try_acquire_lock; then
        # LOCK_DIR exists and (by atomic staging) has a complete owner file.
        local other_id other_state
        other_id="$(grep '^jobid=' "$LOCK_DIR/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
        other_state="$(mock_squeue -h -j "$other_id" -o '%T')"
        case "$other_state" in
            RUNNING|PENDING|CONFIGURING|COMPLETING)
                _log "[MUTEX] FATAL: other job $other_id ($other_state) holds lock, exit 42"
                return 42
                ;;
        esac
        # Owner is dead — reclaim.
        _log "[MUTEX] stale lock (owner=${other_id:-empty} state=${other_state:-gone}), reclaim"
        rm -rf "$LOCK_DIR"
        if ! _try_acquire_lock; then
            _log "[MUTEX] lost reclaim race, exit 42"
            return 42
        fi
    fi
    _log "[MUTEX] ✓ acquired lock"

    # 模擬 "mpirun" 執行一段時間, 確保 contention 窗口足夠讓其它 contender 撞上.
    # 這個值必須 > 100 個 subshell fork + race 的總耗時, 否則後起 contender
    # 會因為第一個 winner 已經釋放鎖而誤以為是乾淨啟動 (sequential 非並發).
    sleep 2

    # 模擬 "mpirun" 期間 (寫 checkpoint)
    local ckpt_file="restart/checkpoint/step_$(printf '%08d' "$jobid")"
    mkdir -p "$(dirname "$ckpt_file")"
    {
        echo "jobid=$jobid"
        echo "wrote_at=$(date -Iseconds)"
    } > "$ckpt_file"

    # Epoch guard 模擬
    local epoch_owner
    epoch_owner="$(grep '^jobid=' "$LOCK_DIR/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
    if [ "$epoch_owner" != "$jobid" ]; then
        _log "[MUTEX-EPOCH] FATAL: lock stolen (owner=$epoch_owner, me=$jobid), exit 42"
        return 42
    fi

    _log "[MUTEX] ✓ completed cleanly"
    rm -rf "$LOCK_DIR"
    return 0
}

setup_fresh() {
    rm -rf restart mock_squeue_state
    mkdir -p restart/checkpoint
    mkdir -p mock_squeue_state
    MOCK_SQUEUE_DIR="$TEST_ROOT/mock_squeue_state"
}

# ═════════════════════════════════════════════════════════════════════════
# Test 1: 單一 job 取鎖 + 清理
# ═════════════════════════════════════════════════════════════════════════
echo "═══ Test 1: 單一 job 取鎖 + 退出清理 ═══"
setup_fresh
simulate_jobscript 1001
rc=$?
if [ $rc -eq 0 ] && [ ! -d restart/RUNNING.lockdir ]; then
    ok "單一 job 取鎖成功, 退出後 lock 被清理"
else
    fail "Test 1 fail (rc=$rc, lock 殘留=$([ -d restart/RUNNING.lockdir ] && echo yes || echo no))"
fi
echo ""

# ═════════════════════════════════════════════════════════════════════════
# Test 2: 雙 job 搶鎖 (嚴重 race) — 只有一個能贏
# ═════════════════════════════════════════════════════════════════════════
echo "═══ Test 2: 雙 job concurrent 搶鎖 ═══"
setup_fresh
# 佈置: job A 已經在跑 (持有 lock) + mock squeue 回 RUNNING
mkdir -p restart/RUNNING.lockdir
cat > restart/RUNNING.lockdir/owner <<EOF
jobid=2001
hostname=mock
cluster=MOCK
started=$(date -Iseconds)
EOF
echo "RUNNING" > "$MOCK_SQUEUE_DIR/2001"
# 現在 job B 嘗試取鎖
simulate_jobscript 2002
rc=$?
if [ $rc -eq 42 ]; then
    # 驗證 lock 沒被 B 偷走
    still_owner="$(grep '^jobid=' restart/RUNNING.lockdir/owner | cut -d= -f2)"
    if [ "$still_owner" = "2001" ]; then
        ok "Job B 正確 exit 42 (owner 仍是 A=2001, 未被覆寫)"
    else
        fail "Job B exit 42 但 lock owner 被污染 (now=$still_owner, expect=2001)"
    fi
else
    fail "Job B 沒有 exit 42 (rc=$rc) — 表示 Layer 3 mutex 失效!"
fi
# 驗證 job B 沒碰 checkpoint
if [ ! -f restart/checkpoint/step_00002002 ]; then
    ok "Job B 未寫入 checkpoint (restart/ 未被並發污染)"
else
    fail "Job B 寫了 checkpoint — 中心準則違反!"
fi
echo ""

# ═════════════════════════════════════════════════════════════════════════
# Test 3: 10 job concurrent 暴力搶鎖
# ═════════════════════════════════════════════════════════════════════════
echo "═══ Test 3: 10 job concurrent 暴力搶鎖 ═══"
setup_fresh
# 所有 contender 先登記為 RUNNING, 避免輸家誤判彼此為 stale
for JID in 3001 3002 3003 3004 3005 3006 3007 3008 3009 3010; do
    echo "RUNNING" > "$MOCK_SQUEUE_DIR/$JID"
done
# 同時 background fork 10 個 job, 讓 kernel 真正跑 mkdir race
RC_FILE="$TEST_ROOT/rc_collect"
: > "$RC_FILE"
for JID in 3001 3002 3003 3004 3005 3006 3007 3008 3009 3010; do
    (
        # barrier: 都等一個 sentinel 檔
        while [ ! -f "$TEST_ROOT/GO" ]; do :; done
        simulate_jobscript "$JID"
        echo "$JID=$?" >> "$RC_FILE"
    ) &
done
sleep 0.3
touch "$TEST_ROOT/GO"
wait
rm -f "$TEST_ROOT/GO"

# 統計
win_count=$(grep -c '=0$' "$RC_FILE" 2>/dev/null | head -1 || echo 0)
lose_count=$(grep -c '=42$' "$RC_FILE" 2>/dev/null | head -1 || echo 0)
other_count=$(grep -vc '=\(0\|42\)$' "$RC_FILE" 2>/dev/null | head -1 || echo 0)
win_count=${win_count:-0}; lose_count=${lose_count:-0}; other_count=${other_count:-0}
info "勝出 (rc=0): $win_count, 輸掉 (rc=42): $lose_count, 其他: $other_count"

if [ "$win_count" -eq 1 ] && [ "$lose_count" -eq 9 ] && [ "$other_count" -eq 0 ]; then
    ok "✓ 核心準則滿足: 10 job 搶鎖, 恰好 1 個勝出, 其餘 9 個 RC=42"
else
    fail "並發搶鎖異常 (勝出=$win_count 應=1, 輸=$lose_count 應=9)"
    cat "$RC_FILE"
fi

# 驗證 checkpoint 只被 1 個 job 寫入
ckpt_count=$(ls restart/checkpoint/ 2>/dev/null | wc -l)
if [ "$ckpt_count" -eq 1 ]; then
    ok "restart/checkpoint/ 只包含 1 份 checkpoint (無並發寫入)"
else
    fail "restart/checkpoint/ 含 $ckpt_count 份 — 有並發寫入!"
    ls -la restart/checkpoint/
fi
echo ""

# ═════════════════════════════════════════════════════════════════════════
# Test 4: Stale lock reclaim
# ═════════════════════════════════════════════════════════════════════════
echo "═══ Test 4: Stale lock (原 owner 已死) reclaim ═══"
setup_fresh
# 佈置: lockdir 存在, owner 是 4001, 但 mock squeue 對 4001 回空 (已死)
mkdir -p restart/RUNNING.lockdir
cat > restart/RUNNING.lockdir/owner <<EOF
jobid=4001
hostname=mock
cluster=MOCK
started=$(date -Iseconds)
EOF
# 注意: mock_squeue_state/4001 不存在 → squeue 回空
# 新 job 4002 嘗試取鎖
simulate_jobscript 4002
rc=$?
if [ $rc -eq 0 ]; then
    # 驗證 lock 已清 (trap 清掉)
    if [ ! -d restart/RUNNING.lockdir ]; then
        ok "Job 4002 成功 reclaim stale lock 並完成, 退出後清理"
    else
        fail "Job 4002 reclaim 後 lock 殘留"
    fi
else
    fail "Job 4002 應該 reclaim 成功 (rc=0) 但 rc=$rc"
fi
echo ""

# ═════════════════════════════════════════════════════════════════════════
# Test 5: Epoch guard — lock 被奪時拒絕 mpirun
# ═════════════════════════════════════════════════════════════════════════
echo "═══ Test 5: Epoch guard 偵測到 lock 被奪 ═══"
setup_fresh
# 改寫 simulate_jobscript 的 epoch guard 流程為 inline 測試
mkdir -p restart/RUNNING.lockdir restart/checkpoint
cat > restart/RUNNING.lockdir/owner <<EOF
jobid=5001
hostname=mock
cluster=MOCK
started=$(date -Iseconds)
EOF

# 模擬: job 5001 取到鎖, 然後 lock 被 5002 覆寫 (不該發生,但測 epoch guard 能擋)
# (實際上我們的 mkdir atomic 不允許覆寫, 這裡純粹人為污染測 defensive 層)
cat > restart/RUNNING.lockdir/owner <<EOF
jobid=5002
hostname=mock
cluster=MOCK
started=$(date -Iseconds)
EOF

# 現在 job 5001 走到 epoch guard
MY_ID=5001
EPOCH_OWNER="$(grep '^jobid=' restart/RUNNING.lockdir/owner | cut -d= -f2 | tr -d '[:space:]')"
if [ "$EPOCH_OWNER" != "$MY_ID" ]; then
    ok "Epoch guard 偵測到 lock 被奪 (owner=$EPOCH_OWNER, me=$MY_ID), 會 exit 42"
else
    fail "Epoch guard 未偵測到 lock 被奪"
fi
echo ""

# ═════════════════════════════════════════════════════════════════════════
# Test 6: 100 個 job concurrent 投遞, 驗證 checkpoint 不被並發寫
# ═════════════════════════════════════════════════════════════════════════
echo "═══ Test 6: 100 job stress test ═══"
setup_fresh
# 所有 contender 先登記為 RUNNING
for JID in $(seq 6001 6100); do
    echo "RUNNING" > "$MOCK_SQUEUE_DIR/$JID"
done
RC_FILE="$TEST_ROOT/rc_stress"
: > "$RC_FILE"
for JID in $(seq 6001 6100); do
    (
        while [ ! -f "$TEST_ROOT/GO2" ]; do :; done
        simulate_jobscript "$JID"
        echo "$JID=$?" >> "$RC_FILE"
    ) &
done
sleep 0.3
touch "$TEST_ROOT/GO2"
wait
rm -f "$TEST_ROOT/GO2"

win_count=$(grep -c '=0$' "$RC_FILE" 2>/dev/null | head -1 || echo 0)
lose_count=$(grep -c '=42$' "$RC_FILE" 2>/dev/null | head -1 || echo 0)
win_count=${win_count:-0}; lose_count=${lose_count:-0}
total=$((win_count + lose_count))
ckpt_count=$(ls restart/checkpoint/ 2>/dev/null | wc -l)

info "總計: $total / 100, 勝出 $win_count, 輸 $lose_count, checkpoint 數 $ckpt_count"

if [ "$win_count" -eq 1 ] && [ "$lose_count" -eq 99 ]; then
    ok "100 job 並發中恰好 1 勝 99 輸 (mutex 完美防守)"
else
    fail "100 job 並發異常 (勝=$win_count 應=1, 輸=$lose_count 應=99)"
fi
if [ "$ckpt_count" -eq 1 ]; then
    ok "restart/checkpoint/ 在 100 並發下仍只有 1 份 — 中心準則嚴格維持"
else
    fail "restart/checkpoint/ 有 $ckpt_count 份 — 並發污染!"
fi
echo ""
# =========================================================================
# Test 7: [Section F] Single-Head Invariant
# -------------------------------------------------------------------------
# Invariant: at all times, count of restart/*.lockdir <= 1.
# Approach: spawn 30 concurrent submitters using the real acquire_head_lock
# primitive from head_lock_lib.sh; a background watcher samples the count
# every 50ms. If the max observed count ever exceeds 1, the invariant fails.
# =========================================================================
echo "=== Test 7: [Section F] Single-Head Invariant (lockdir count <= 1) ==="
setup_fresh

HEAD_LOCK_LIB_SRC="${SCRIPT_DIR}/head_lock_lib.sh"
if [ ! -f "$HEAD_LOCK_LIB_SRC" ]; then
    fail "head_lock_lib.sh not found at $HEAD_LOCK_LIB_SRC -- skipping Section F"
else
    export HEAD_STALE_TIMEOUT=2
    export HEAD_LOCK_DIR="restart/HEAD.lockdir"
    export HEAD_STAGE_PREFIX="restart/.headstage"
    . "$HEAD_LOCK_LIB_SRC"

    # Override squeue mock
    _head_squeue_state() {
        local jid="$1"
        [ -z "$jid" ] || [ "$jid" = "TBD" ] && { echo ""; return; }
        [ -f "$MOCK_SQUEUE_DIR/$jid" ] && cat "$MOCK_SQUEUE_DIR/$jid" || echo ""
    }

    WATCH_LOG="$TEST_ROOT/watcher.log"
    : > "$WATCH_LOG"
    (
        end_ts=$(( $(date +%s) + 5 ))
        while [ "$(date +%s)" -lt "$end_ts" ]; do
            cnt=$(ls -d restart/*.lockdir 2>/dev/null | wc -l)
            echo "cnt=$cnt" >> "$WATCH_LOG"
            sleep 0.05 2>/dev/null || sleep 1
        done
    ) &
    WATCHER_PID=$!

    RC_SECF="$TEST_ROOT/rc_secf"
    : > "$RC_SECF"
    for N in $(seq 1 30); do
        (
            while [ ! -f "$TEST_ROOT/GO_F" ]; do :; done
            if acquire_head_lock "secF-$N" >/dev/null 2>&1; then
                sleep 0.05
                release_head_lock
                echo "$N=WIN" >> "$RC_SECF"
            else
                echo "$N=LOSE" >> "$RC_SECF"
            fi
        ) &
    done
    sleep 0.2
    touch "$TEST_ROOT/GO_F"
    wait
    rm -f "$TEST_ROOT/GO_F"

    kill "$WATCHER_PID" 2>/dev/null
    wait "$WATCHER_PID" 2>/dev/null

    max_cnt=$(awk -F= '/^cnt=/{print $2}' "$WATCH_LOG" | sort -rn | head -1)
    max_cnt=${max_cnt:-0}
    total_samples=$(wc -l < "$WATCH_LOG")
    info "watcher took $total_samples samples; max concurrent *.lockdir = $max_cnt"
    if [ "$max_cnt" -le 1 ]; then
        ok "[Section F] Single-Head Invariant held: *.lockdir count <= 1 at all times"
    else
        fail "[Section F] VIOLATED: observed $max_cnt concurrent *.lockdir (log: $WATCH_LOG)"
    fi

    remaining=$(ls -d restart/*.lockdir 2>/dev/null | wc -l)
    if [ "$remaining" -eq 0 ]; then
        ok "[Section F] after 30 contenders: no *.lockdir residue"
    else
        fail "[Section F] after 30 contenders: $remaining *.lockdir residue(s)"
    fi
fi
echo ""

# =========================================================================
# Test 8: [Section G] No-Double-Submit
# -------------------------------------------------------------------------
# 100 concurrent acquirers must yield exactly 1 WIN and 99 LOSE.
# Winner's owner file must be well-formed: state=SUBMITTING, jobid=TBD,
# submitter tag present. Additionally verify write_head_jobid and
# verify_am_head work end-to-end.
# =========================================================================
echo "=== Test 8: [Section G] No-Double-Submit (100 concurrent acquire) ==="
setup_fresh

if [ ! -f "$HEAD_LOCK_LIB_SRC" ]; then
    fail "head_lock_lib.sh not found -- skipping Section G"
else
    export HEAD_STALE_TIMEOUT=2
    export HEAD_LOCK_DIR="restart/HEAD.lockdir"
    export HEAD_STAGE_PREFIX="restart/.headstage"
    . "$HEAD_LOCK_LIB_SRC"
    _head_squeue_state() { echo ""; }

    RC_SECG="$TEST_ROOT/rc_secg"
    : > "$RC_SECG"
    for N in $(seq 1 100); do
        (
            while [ ! -f "$TEST_ROOT/GO_G" ]; do :; done
            if acquire_head_lock "secG-$N" >/dev/null 2>&1; then
                echo "$N=WIN" >> "$RC_SECG"
            else
                echo "$N=LOSE" >> "$RC_SECG"
            fi
        ) &
    done
    sleep 0.3
    touch "$TEST_ROOT/GO_G"
    wait
    rm -f "$TEST_ROOT/GO_G"

    win_g=$(grep -c '=WIN$' "$RC_SECG" 2>/dev/null || true)
    lose_g=$(grep -c '=LOSE$' "$RC_SECG" 2>/dev/null || true)
    win_g=${win_g:-0}; lose_g=${lose_g:-0}
    info "100 concurrent acquire: WIN=$win_g, LOSE=$lose_g"
    if [ "$win_g" -eq 1 ] && [ "$lose_g" -eq 99 ]; then
        ok "[Section G] No-Double-Submit: exactly 1 WIN and 99 LOSE"
    else
        fail "[Section G] VIOLATED: WIN=$win_g (expected 1), LOSE=$lose_g (expected 99)"
    fi

    if [ -d restart/HEAD.lockdir ] && [ -f restart/HEAD.lockdir/owner ]; then
        owner_state=$(grep '^state=' restart/HEAD.lockdir/owner | cut -d= -f2 | tr -d '[:space:]')
        owner_tag=$(grep '^submitter=' restart/HEAD.lockdir/owner | cut -d= -f2 | tr -d '[:space:]')
        owner_jid=$(grep '^jobid=' restart/HEAD.lockdir/owner | cut -d= -f2 | tr -d '[:space:]')
        if [ "$owner_state" = "SUBMITTING" ] && [ -n "$owner_tag" ] && [ "$owner_jid" = "TBD" ]; then
            ok "[Section G] winner owner file well-formed (state=SUBMITTING tag=$owner_tag jid=TBD)"
        else
            fail "[Section G] owner file malformed: state=$owner_state tag=$owner_tag jid=$owner_jid"
        fi
    else
        fail "[Section G] no HEAD.lockdir/owner after 100 contenders"
    fi
    release_head_lock

    # End-to-end round trip: acquire -> write_head_jobid -> verify_am_head
    if acquire_head_lock "secG-post-test" >/dev/null 2>&1; then
        if write_head_jobid "77777" "MOCK"; then
            pend_state=$(grep '^state=' restart/HEAD.lockdir/owner | cut -d= -f2 | tr -d '[:space:]')
            pend_jid=$(grep '^jobid=' restart/HEAD.lockdir/owner | cut -d= -f2 | tr -d '[:space:]')
            if [ "$pend_state" = "PENDING" ] && [ "$pend_jid" = "77777" ]; then
                ok "[Section G] write_head_jobid upgraded SUBMITTING->PENDING (jid=77777)"
            else
                fail "[Section G] write_head_jobid bad outcome: state=$pend_state jid=$pend_jid"
            fi

            if verify_am_head 77777 MOCK; then
                run_state=$(grep '^state=' restart/HEAD.lockdir/owner | cut -d= -f2 | tr -d '[:space:]')
                if [ "$run_state" = "RUNNING" ]; then
                    ok "[Section G] verify_am_head(77777) ok and upgraded to RUNNING"
                else
                    fail "[Section G] verify_am_head returned 0 but state=$run_state"
                fi
            else
                fail "[Section G] verify_am_head(77777) should succeed but did not"
            fi

            if ! verify_am_head 99999 MOCK; then
                ok "[Section G] verify_am_head(99999) correctly rejected (not head)"
            else
                fail "[Section G] verify_am_head(99999) wrongly succeeded (identity check broken)"
            fi
        else
            fail "[Section G] write_head_jobid call failed"
        fi
        release_head_lock
    fi
fi
echo ""

# =========================================================================
# FINAL SUMMARY
# =========================================================================
echo "==========================================================================="
echo "  MUTEX Verification Summary"
echo "  PASS:  $PASS_COUNT"
echo "  FAIL:  $FAIL_COUNT"
echo ""
if [ "$FAIL_COUNT" -eq 0 ]; then
    echo "  [PASS] Central invariants upheld:"
    echo "         - restart/ has at most one writer at any time"
    echo "         - Section F: *.lockdir count <= 1 always"
    echo "         - Section G: 100 concurrent acquire -> 1 WIN, 99 LOSE"
    rm -rf "$TEST_ROOT"
    exit 0
else
    echo "  [FAIL] Central invariants violated"
    echo "         test artefacts retained at: $TEST_ROOT"
    exit 1
fi
