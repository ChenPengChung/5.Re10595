#!/bin/bash
# ==============================================================================
# run.sh — GILBM Periodic Hill 統一啟動入口 (cluster-aware dispatcher)
#
# 一個指令涵蓋所有續跑情境。自動偵測環境並作正確處置。
#
# 用法:
#   ./run.sh                  自動偵測情境 + 叢集,並投遞 (最常用)
#   ./run.sh --status         只看狀態,不投遞
#   ./run.sh --rebuild        強制重編 a.out 再投
#   ./run.sh --force-cold     清空所有 state / history 後從頭跑 (需確認)
#   ./run.sh --h200           強制使用 H200 變體 (x86_64, sm_90, dev partition)
#   ./run.sh --gb200          強制使用 GB200 變體 (aarch64, sm_100, gb200-dev)
#   ./run.sh --no-queue-check 關閉 partition 擁塞檢查 (CI/自動化用)
#   ./run.sh -h | --help      顯示此使用說明
#
# Partition 智能查詢 (方案一, 2026-04-21):
#   每次投遞前會用 sinfo/squeue 查詢當前叢集 partition 的:
#     - idle / alloc / down 節點數
#     - 使用者自己的 pending / running jobs
#     - 全 partition 的 pending queue 深度
#   若 partition 擁塞 (idle=0 且 pending>=5), 會印 advisory 提示可否切另一邊,
#   但不阻擋投遞 (純資訊). 用 --no-queue-check 可跳過此查詢.
#
# Cluster 偵測:
#   - 預設: uname -m → aarch64 ⇒ GB200 / x86_64 ⇒ H200
#   - 兩種變體的檔案嚴格分離 (「區分功能正確性」),不互相干擾:
#       GB200: build_and_submit.sh.GB200 + jobscript_chain.slurm.GB200
#       H200 : build_and_submit.sh.H200  + jobscript_chain.slurm.H200
#   - 可用 --h200 / --gb200 override (例如在 login-node 預編譯另一叢集的 a.out)
#
# 情境對照 (皆用 ./run.sh):
#   情境 1 冷啟動 (全新)       → 自動編譯 + 空 state + sbatch
#   情境 2 只有 checkpoint     → 自動偵測 + 備份 history + 建 Round 2 + sbatch
#   情境 3A 鏈正常在跑         → 偵測 queue 有 job,報告後退出 (不重投)
#   情境 3B 鏈斷了             → 保留 state,直接 sbatch 接續
#
# 安全措施:
#   - flock restart/.lock 防止兩個 run.sh 同時執行雙投
#   - 情境 2 前自動備份 checkrho.dat / Ustar_Force_record.dat
#   - --force-cold 必須人工確認
#   - 若 checkpoint 全數無效,FATAL,引導使用者用 --force-cold
# ==============================================================================

set -eo pipefail   # 不使用 -u: hpcx-init.sh 會踩 unbound variable

# ── [方案 A path discipline] 自我定位 + 鎖 cwd 到 PROJECT_ROOT ────────────────
# 本 script 位於 chain_code/, 但許多相對路徑 (restart/, a.out, result/, .run.lock)
# 都在 PROJECT_ROOT. 其他 chain_code/ 內的同伴 script 則透過 $CHAIN_DIR 絕對路徑呼叫.
_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || realpath "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
CHAIN_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
PROJECT_ROOT="$(cd "$CHAIN_DIR/.." && pwd)"
cd "$PROJECT_ROOT" || { echo "[run.sh] FATAL: cannot cd to PROJECT_ROOT=$PROJECT_ROOT" >&2; exit 1; }

MODE_COLD=0
MODE_REBUILD=0
MODE_STATUS=0
MODE_NO_QCHECK=0   # 1 = 跳過 partition 擁塞查詢 (CI/自動化)
MODE_CLUSTER=""    # "" = auto-detect; "H200" or "GB200" = user override

for arg in "$@"; do
    case "$arg" in
        --force-cold)      MODE_COLD=1 ;;
        --rebuild)         MODE_REBUILD=1 ;;
        --status)          MODE_STATUS=1 ;;
        --no-queue-check)  MODE_NO_QCHECK=1 ;;
        --h200|--H200)     MODE_CLUSTER="H200" ;;
        --gb200|--GB200)   MODE_CLUSTER="GB200" ;;
        -h|--help)
            sed -n '2,38p' "$0"
            exit 0 ;;
        *)
            echo "[run.sh] Unknown arg: $arg"
            echo "         請用 -h / --help 查看合法參數"
            exit 2 ;;
    esac
done

# ═════════════════════════════════════════════════════════════════════════
# Cluster 自動偵測 (partition-smart-ETA → idle-count → uname -m fallback) + override
# 優先順序:
#   [1] --h200 / --gb200 override (使用者明示)
#   [2] partition-smart-ETA: 雙 arch binary 都已預建 + sbatch 可用
#       → 用 `sbatch --test-only` 問 SLURM 兩邊 partition 真正的預期開始時間 (ETA),
#         選較早能跑的那個 (60s 內視為平手, 比 idle 節點數多的贏)
#   [3] partition-smart-IDLE (fallback): --test-only 失敗時改比 sinfo idle 節點數
#   [4] uname -m fallback: 單 arch 或 sinfo 不可用時
# 同時驗證對應檔案存在,不存在就明確報錯,不靜默退回他變體。
# ═════════════════════════════════════════════════════════════════════════
CLUSTER=""
CLUSTER_SRC=""

# [1] 使用者明示 override (最高優先)
if [ -n "$MODE_CLUSTER" ]; then
    CLUSTER="$MODE_CLUSTER"
    CLUSTER_SRC="override(--${MODE_CLUSTER,,})"
fi

# [2] Partition-smart-ETA: 未 override、雙 binary 都存在、sbatch 可用時才啟用
# 用 `sbatch --test-only` 問 SLURM: "如果現在投這個 jobscript, 幾點會開始?"
# 輸出格式: "sbatch: Job 12345 to start at 2026-04-22T06:30:43 using ..."
# 或立即可跑: "sbatch: Job allocation 12345 can be allocated now"
if [ -z "$CLUSTER" ] \
   && [ -s a.out.GB200 ] && [ -s a.out.H200 ] \
   && [ -f "$CHAIN_DIR/jobscript_chain.slurm.GB200" ] && [ -f "$CHAIN_DIR/jobscript_chain.slurm.H200" ] \
   && command -v sbatch >/dev/null 2>&1 \
   && command -v sinfo  >/dev/null 2>&1; then

    # 先拿 idle 節點數 (用於顯示 + fallback)
    # 注意: 尾端 "|| true" 是必要的 — set -eo pipefail 下, sinfo 若回 1
    # 會讓 X="$(pipeline)" 觸發 set -e 靜默退出 rc=1
    GB_IDLE="$(sinfo -h -p gb200-dev -t idle -o '%D' 2>/dev/null | awk '{s+=$1} END{print s+0}' || true)"
    H_IDLE="$( sinfo -h -p dev       -t idle -o '%D' 2>/dev/null | awk '{s+=$1} END{print s+0}' || true)"

    # 內部函數: 用 --test-only 查單一 jobscript 的 ETA epoch, 失敗回 -1
    _eta_epoch() {
        local js="$1" out eta_str
        # --test-only 不會真的投遞, 只詢問 scheduler 預期
        out=$(sbatch --test-only "$js" 2>&1 || true)
        if   echo "$out" | grep -qE "to start at[[:space:]]+[0-9]{4}-"; then
            eta_str=$(echo "$out" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+' | head -1)
            date -d "$eta_str" +%s 2>/dev/null || echo -1
        elif echo "$out" | grep -qE "allocation .*can be allocated|to start immediately|to start now"; then
            date +%s   # 立即可跑
        else
            echo -1    # --test-only 失敗 / 無法解析
        fi
    }

    GB_EPOCH=$(_eta_epoch "$CHAIN_DIR/jobscript_chain.slurm.GB200")
    H_EPOCH=$( _eta_epoch "$CHAIN_DIR/jobscript_chain.slurm.H200" )
    NOW_EPOCH=$(date +%s)

    # 格式化 wait 時間為可讀字串 (e.g. "now", "~12min", "~3h15m")
    _fmt_wait() {
        local w=$1
        if   [ $w -lt 0 ];     then echo "unknown"
        elif [ $w -le 60 ];    then echo "now"
        elif [ $w -lt 3600 ];  then echo "~$((w/60))min"
        else                        echo "~$((w/3600))h$((w%3600/60))m"
        fi
    }

    # 兩邊 ETA 都成功 → 比 ETA
    if [ "$GB_EPOCH" -ge 0 ] && [ "$H_EPOCH" -ge 0 ]; then
        GB_WAIT=$((GB_EPOCH - NOW_EPOCH))
        H_WAIT=$(( H_EPOCH - NOW_EPOCH))
        [ $GB_WAIT -lt 0 ] && GB_WAIT=0
        [ $H_WAIT  -lt 0 ] && H_WAIT=0
        GB_WS=$(_fmt_wait $GB_WAIT)
        H_WS=$( _fmt_wait $H_WAIT)
        DELTA=$((GB_WAIT - H_WAIT))
        # 差距 >60s 才算分勝負, 否則看 idle 節點數
        if   [ $DELTA -lt -60 ]; then
            CLUSTER="GB200"
            CLUSTER_SRC="partition-smart-ETA(GB=$GB_WS < H=$H_WS; idle gb=$GB_IDLE/h=$H_IDLE)"
        elif [ $DELTA -gt  60 ]; then
            CLUSTER="H200"
            CLUSTER_SRC="partition-smart-ETA(H=$H_WS < GB=$GB_WS; idle gb=$GB_IDLE/h=$H_IDLE)"
        else
            # ETA 平手 (60s 內) → idle 節點多的贏
            if   [ "${GB_IDLE:-0}" -gt "${H_IDLE:-0}" ]; then
                CLUSTER="GB200"; CLUSTER_SRC="partition-smart-ETA(tie $GB_WS~$H_WS; gb_idle=$GB_IDLE > h_idle=$H_IDLE)"
            elif [ "${H_IDLE:-0}" -gt "${GB_IDLE:-0}" ]; then
                CLUSTER="H200";  CLUSTER_SRC="partition-smart-ETA(tie $GB_WS~$H_WS; h_idle=$H_IDLE > gb_idle=$GB_IDLE)"
            fi
        fi
    else
        # --test-only 失敗 → fallback 回 idle-count
        if   [ "${GB_IDLE:-0}" -gt "${H_IDLE:-0}" ]; then
            CLUSTER="GB200"; CLUSTER_SRC="partition-smart-IDLE(ETA-fail; gb_idle=$GB_IDLE > h_idle=$H_IDLE)"
        elif [ "${H_IDLE:-0}" -gt "${GB_IDLE:-0}" ]; then
            CLUSTER="H200";  CLUSTER_SRC="partition-smart-IDLE(ETA-fail; h_idle=$H_IDLE > gb_idle=$GB_IDLE)"
        fi
    fi
    # 全部平手或兩邊都 0 → CLUSTER 保持空,fall-through 到 [4]
fi

# [3] Fallback: uname -m
if [ -z "$CLUSTER" ]; then
    case "$(uname -m)" in
        aarch64|arm64) CLUSTER="GB200"; CLUSTER_SRC="uname=aarch64" ;;
        x86_64|amd64)  CLUSTER="H200";  CLUSTER_SRC="uname=x86_64"  ;;
        *)
            echo "[run.sh] FATAL: 未知的 uname -m = $(uname -m)"
            echo "         請明確指定 --h200 或 --gb200"
            exit 3 ;;
    esac
fi

JOBSCRIPT="$CHAIN_DIR/jobscript_chain.slurm.${CLUSTER}"
BUILD_SCRIPT="$CHAIN_DIR/build_and_submit.sh.${CLUSTER}"

if [ ! -f "$JOBSCRIPT" ]; then
    echo "[run.sh] FATAL: 偵測到 $CLUSTER ($CLUSTER_SRC),但缺少 $JOBSCRIPT"
    echo "         可用變體: $(ls "$CHAIN_DIR"/jobscript_chain.slurm.* 2>/dev/null | tr '\n' ' ')"
    exit 3
fi
if [ ! -f "$BUILD_SCRIPT" ]; then
    echo "[run.sh] FATAL: 偵測到 $CLUSTER ($CLUSTER_SRC),但缺少 $BUILD_SCRIPT"
    exit 3
fi

mkdir -p restart/

# ═════════════════════════════════════════════════════════════════════════
# flock: 防止並發 run.sh 同時執行 (兩個 terminal 同時 ./run.sh → 雙投 bug)
# ═════════════════════════════════════════════════════════════════════════
exec 200>.run.lock
if ! flock -n 200; then
    echo "[run.sh] 另一個 run.sh 正在執行 (.run.lock 被佔用)"
    echo "         若確定沒有其他 run.sh,可移除 lock: rm .run.lock"
    exit 4
fi

# ═════════════════════════════════════════════════════════════════════════
# DISPATCHER 模式 sentinel check (防呆)
# ═════════════════════════════════════════════════════════════════════════
# 若 DISPATCHER_ACTIVE 存在，代表 dispatcher 正在接管續投。
# 使用者若直接 ./run.sh 會造成雙投 (dispatcher + 使用者手動同時投)。
# dispatcher 自己呼叫 run.sh 時會設 RUNSH_DISPATCHER_BYPASS=1 繞過此檢查。
# ─────────────────────────────────────────────────────────────────────────
if [ -f DISPATCHER_ACTIVE ] && [ "${RUNSH_DISPATCHER_BYPASS:-0}" != "1" ]; then
    echo "[run.sh] ⚠ 偵測到 DISPATCHER_ACTIVE — dispatcher 正在接管續投."
    echo "         若要手動投一輪,請先 ./dispatcher_stop.sh"
    echo "         若要看 dispatcher 狀態,請執行 ./dispatcher_status.sh"
    exit 5
fi

# ═════════════════════════════════════════════════════════════════════════
# [SINGLE-HEAD / MUTEX LAYER 3 — run.sh pre-flight]
# ─────────────────────────────────────────────────────────────────────────
# 中心準則 (Single-Head per Folder):
#   每格資料夾在 queue 內最多只能有 1 個 job. HEAD.lockdir 是 single source of truth.
#   若 HEAD.lockdir 被活 owner 佔住, 拒絕投遞; 若 owner 已死, 自動清理 stale entry.
#   (向後相容: 同時檢查 legacy RUNNING.lockdir / chain_jobid.)
# ═════════════════════════════════════════════════════════════════════════

# 先載入 head_lock_lib (提供 _head_squeue_state 等函式)
if [ -f "$CHAIN_DIR/tools/head_lock_lib.sh" ]; then
    # shellcheck disable=SC1091
    . "$CHAIN_DIR/tools/head_lock_lib.sh"
fi

# ── Primary 檢查: restart/HEAD.lockdir ──
if [ -d "${HEAD_LOCK_DIR:-restart/HEAD.lockdir}" ]; then
    HD="${HEAD_LOCK_DIR:-restart/HEAD.lockdir}"
    # 尾端 "|| true" 必要 — set -eo pipefail 下 grep/squeue 若無 match 回 1,
    # X="$(pipeline)" 會觸發 set -e 靜默退出 rc=1 (無 banner, 最難 debug)
    HEAD_STATE="$(grep '^state=' "$HD/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]' || true)"
    HEAD_JID="$(grep   '^jobid=' "$HD/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]' || true)"
    HEAD_EPOCH="$(grep '^submitted_at_epoch=' "$HD/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]' || true)"
    HEAD_LIVE=""
    if [[ "$HEAD_JID" =~ ^[0-9]+$ ]]; then
        HEAD_LIVE="$(squeue -h -j "$HEAD_JID" -o '%T' 2>/dev/null | tr -d '[:space:]' || true)"
    fi
    NOW_EPOCH="$(date +%s)"
    AGE_S=$(( NOW_EPOCH - ${HEAD_EPOCH:-0} ))

    case "$HEAD_STATE" in
        SUBMITTING)
            if [ "$AGE_S" -gt "${HEAD_STALE_TIMEOUT:-30}" ]; then
                echo "[run.sh] 偵測到 stale HEAD.lockdir (state=SUBMITTING age=${AGE_S}s > ${HEAD_STALE_TIMEOUT:-30}s), 自動清理"
                rm -rf "$HD"
            else
                echo "[run.sh] ⚠ HEAD.lockdir 正被 submitter 鎖住 (state=SUBMITTING age=${AGE_S}s)"
                echo "         有人正在投遞,拒絕再投. 請稍後再試."
                exit 6
            fi
            ;;
        PENDING|RUNNING)
            case "$HEAD_LIVE" in
                PENDING|RUNNING|CONFIGURING|COMPLETING|RESIZING|SUSPENDED)
                    echo "[run.sh] ⚠ HEAD.lockdir 被 jobid=$HEAD_JID ($HEAD_STATE, squeue=$HEAD_LIVE) 持有"
                    echo "         拒絕投遞以維持「每格資料夾單一 head」準則"
                    echo "         若 $HEAD_JID 是 orphan: scancel $HEAD_JID && rm -rf $HD"
                    exit 6
                    ;;
                "")
                    echo "[run.sh] 偵測到 stale HEAD.lockdir (jobid=$HEAD_JID 已不在 squeue), 自動清理"
                    rm -rf "$HD"
                    ;;
            esac
            ;;
        *)
            if [ "$AGE_S" -gt "${HEAD_STALE_TIMEOUT:-30}" ]; then
                echo "[run.sh] 偵測到 unknown-state HEAD.lockdir (state=$HEAD_STATE age=${AGE_S}s), 自動清理"
                rm -rf "$HD"
            fi
            ;;
    esac
fi

# ── Legacy 相容檢查: 舊 RUNNING.lockdir ──
if [ -d restart/RUNNING.lockdir ]; then
    # 尾端 "|| true" 必要 (同 HEAD.lockdir 區段解釋)
    LOCK_OWNER="$(grep '^jobid=' restart/RUNNING.lockdir/owner 2>/dev/null | cut -d= -f2 | tr -d '[:space:]' || true)"
    LOCK_STATE="$(squeue -h -j "$LOCK_OWNER" -o '%T' 2>/dev/null | tr -d '[:space:]' || true)"
    case "$LOCK_STATE" in
        PENDING|RUNNING|CONFIGURING|COMPLETING)
            echo "[run.sh] ⚠ legacy restart/RUNNING.lockdir 被 jobid=$LOCK_OWNER ($LOCK_STATE) 持有"
            echo "         拒絕投遞以維持單一寫入者準則"
            echo "         若 $LOCK_OWNER 是 orphan: scancel $LOCK_OWNER && rm -rf restart/RUNNING.lockdir"
            exit 6
            ;;
        "")
            echo "[run.sh] 偵測到 stale legacy RUNNING.lockdir (owner=$LOCK_OWNER 已死), 自動清理"
            rm -rf restart/RUNNING.lockdir
            ;;
    esac
fi

# ── Legacy 相容檢查: 舊 chain_jobid ──
if [ -f restart/chain_jobid ]; then
    # 尾端 "|| true" 必要 — chain_jobid 若是 stale ID, squeue 回 1,
    # set -eo pipefail 下 X="$(pipeline)" 會觸發靜默退出 rc=1 (實戰症狀根因)
    CUR_CHAIN_ID="$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ "$CUR_CHAIN_ID" =~ ^[0-9]+$ ]]; then
        CUR_CHAIN_STATE="$(squeue -h -j "$CUR_CHAIN_ID" -o '%T' 2>/dev/null | tr -d '[:space:]' || true)"
        case "$CUR_CHAIN_STATE" in
            PENDING|RUNNING|CONFIGURING|COMPLETING)
                echo "[run.sh] ⚠ chain_jobid=$CUR_CHAIN_ID 仍 active ($CUR_CHAIN_STATE)"
                echo "         拒絕再投以免並發寫入 restart/"
                echo "         若要接手: 等本輪結束,或 scancel $CUR_CHAIN_ID"
                exit 7
                ;;
        esac
    fi
fi

# ═════════════════════════════════════════════════════════════════════════
# 狀態偵測
# ═════════════════════════════════════════════════════════════════════════
HAS_BIN=0
[ -x ./a.out ] && HAS_BIN=1

HAS_CKPT=0
while IFS= read -r _d; do
    _d=${_d%/}
    case "$_d" in *.WRITING) continue ;; esac
    if [ -s "$_d/metadata.dat" ]; then
        HAS_CKPT=1
        break
    fi
done < <(ls -1d restart/checkpoint/step_*/ 2>/dev/null)

HAS_STATE=0
if [ -f restart/chain_count ] && [ -f restart/chain_jobid ]; then
    HAS_STATE=1
fi

JOB_NAME=$(awk -F= '/^#SBATCH[[:space:]]+--job-name=/{gsub(/^[[:space:]]+|[[:space:]]+$/,"",$2); print $2; exit}' "$JOBSCRIPT" 2>/dev/null)
JOB_NAME="${JOB_NAME:-GILBM_PH}"

# ═════════════════════════════════════════════════════════════════════════
# [方案一 2026-04-21] Partition 智能查詢
# ═════════════════════════════════════════════════════════════════════════
PARTITION=$(awk -F= '/^#SBATCH[[:space:]]+--partition=/{gsub(/^[[:space:]]+|[[:space:]]+$/,"",$2); print $2; exit}' "$JOBSCRIPT" 2>/dev/null)
PARTITION="${PARTITION:-unknown}"

PART_IDLE="?"
PART_ALLOC="?"
PART_DOWN="?"
PART_PEND_MINE="?"
PART_RUN_MINE="?"
PART_PEND_ALL="?"
PART_CONGESTED=0

query_partition_status() {
    [ "$MODE_NO_QCHECK" -eq 1 ] && return 0
    [ "$PARTITION" = "unknown" ] && return 0
    if ! command -v sinfo >/dev/null 2>&1; then
        return 0
    fi
    local _sum_state
    _sum_state() {
        sinfo -h -p "$PARTITION" -t "$1" -o "%D" 2>/dev/null \
            | awk '{s+=$1} END{print s+0}'
    }
    PART_IDLE=$(_sum_state idle)
    PART_ALLOC=$(_sum_state alloc)
    PART_DOWN=$(_sum_state 'down,drain,fail,drng' 2>/dev/null)
    if [ -z "$PART_DOWN" ] || [ "$PART_DOWN" = "0" ]; then
        local _d1 _d2 _d3 _d4
        _d1=$(_sum_state down)
        _d2=$(_sum_state drain)
        _d3=$(_sum_state fail 2>/dev/null)
        _d4=$(_sum_state drng 2>/dev/null)
        PART_DOWN=$((_d1 + _d2 + ${_d3:-0} + ${_d4:-0}))
    fi
    PART_PEND_MINE=$(squeue -h -u "$USER" -p "$PARTITION" -t PD -o "%i" 2>/dev/null | grep -c . || true)
    PART_RUN_MINE=$(squeue -h -u "$USER" -p "$PARTITION" -t R  -o "%i" 2>/dev/null | grep -c . || true)
    PART_PEND_ALL=$(squeue -h -p "$PARTITION" -t PD -o "%i" 2>/dev/null | grep -c . || true)
    if [ "${PART_IDLE:-0}" -eq 0 ] 2>/dev/null && [ "${PART_PEND_ALL:-0}" -ge 5 ] 2>/dev/null; then
        PART_CONGESTED=1
    fi
}

query_partition_status

QUEUE_JOBS=$(squeue -u "$USER" -h -o '%i %j %T %M %R' 2>/dev/null \
             | grep -E "[[:space:]]${JOB_NAME}([[:space:]]|$)" || true)

# ═════════════════════════════════════════════════════════════════════════
# 列印狀態 banner
# ═════════════════════════════════════════════════════════════════════════
CC_DISPLAY="(none)"
[ "$HAS_STATE" -eq 1 ] && CC_DISPLAY="count=$(cat restart/chain_count) jobid=$(cat restart/chain_jobid)"

echo "════════════════════════════════════════════════════════════════"
echo " run.sh 狀態偵測 @ $(date '+%F %T')"
echo "   pwd          : $(pwd)"
echo "   cluster      : $CLUSTER   ($CLUSTER_SRC)"
echo "   partition    : $PARTITION"
echo "   jobscript    : $JOBSCRIPT"
echo "   build script : $BUILD_SCRIPT"
echo "   a.out        : $([ "$HAS_BIN"   -eq 1 ] && echo 'YES' || echo 'NO')"
echo "   checkpoint   : $([ "$HAS_CKPT"  -eq 1 ] && echo 'YES (restart/checkpoint/)' || echo 'NO')"
echo "   chain state  : $CC_DISPLAY"
echo "   queue jobs   :"
if [ -n "$QUEUE_JOBS" ]; then
    echo "$QUEUE_JOBS" | sed 's/^/      /'
else
    echo "      (無)"
fi
if [ "$MODE_NO_QCHECK" -eq 1 ]; then
    echo "   partition    : (--no-queue-check, 已跳過 sinfo/squeue 查詢)"
elif [ "$PART_IDLE" = "?" ]; then
    echo "   partition    : sinfo 不可用 (本機開發環境?)"
else
    echo "   partition $PARTITION 狀態:"
    echo "      idle nodes   : $PART_IDLE"
    echo "      alloc nodes  : $PART_ALLOC"
    echo "      down/drain   : $PART_DOWN"
    echo "      我的 pending : $PART_PEND_MINE    running: $PART_RUN_MINE"
    echo "      全 pending   : $PART_PEND_ALL"
    if [ "$PART_CONGESTED" -eq 1 ]; then
        OTHER="H200"; OTHER_ARCH="x86_64"
        [ "$CLUSTER" = "H200" ] && OTHER="GB200" && OTHER_ARCH="aarch64"
        echo ""
        echo "   ⚠  [advisory] $PARTITION 擁塞中 (idle=0, pending=$PART_PEND_ALL)"
        echo "      若有 $OTHER login ($OTHER_ARCH) 可用, 考慮切過去:"
        echo "         ssh <$OTHER-login>  &&  cd $(pwd | sed "s|$HOME|~|")  &&  ./run.sh --rebuild"
        echo "      (checkpoint 可攜, 需在該 login 重編 a.out)"
    fi
fi
echo "════════════════════════════════════════════════════════════════"

if [ "$MODE_STATUS" -eq 1 ]; then
    echo "[--status] 只顯示狀態,退出."
    exit 0
fi

# ═════════════════════════════════════════════════════════════════════════
# 情境 3A: 鏈已在跑 → 不做事
# ═════════════════════════════════════════════════════════════════════════
if [ -n "$QUEUE_JOBS" ]; then
    echo ""
    echo "[3A] Chain 正常運行中 -- run.sh 不會重投."
    echo "     停鏈: touch restart/STOP_CHAIN   (solver 100 步內感應)"
    echo "     強停: scancel <jobid>"
    exit 0
fi

# ═════════════════════════════════════════════════════════════════════════
# --force-cold: 清空所有 state / history / checkpoint, 從頭跑
# ═════════════════════════════════════════════════════════════════════════
if [ "$MODE_COLD" -eq 1 ]; then
    echo ""
    echo "WARN --force-cold 會刪除:"
    echo "   restart/ (含 checkpoint)"
    echo "   checkrho.dat / Ustar_Force_record.dat / timing_log.dat"
    echo "   statistics/ / checkpoint/ (legacy root) "
    read -r -p "   確認從頭跑? 舊 chain 資料將永久消失 [y/N]: " ok
    if [ "$ok" != "y" ] && [ "$ok" != "Y" ]; then
        echo "已取消."
        exit 0
    fi
    rm -rf restart/ checkpoint/
    rm -f checkrho.dat Ustar_Force_record.dat timing_log.dat
    rm -rf statistics/
    mkdir -p restart/
    HAS_CKPT=0
    HAS_STATE=0
    echo "[--force-cold] 已清理完畢, 進入 Scenario 1 冷啟動流程"
fi

# ═════════════════════════════════════════════════════════════════════════
# 編譯 a.out (Scenario 1 / Scenario 2 缺 binary / --rebuild)
# ═════════════════════════════════════════════════════════════════════════
if [ "$HAS_BIN" -eq 0 ] || [ "$MODE_REBUILD" -eq 1 ]; then
    if [ "$MODE_REBUILD" -eq 1 ]; then
        echo "[build] --rebuild 指定, 強制重編 a.out ($CLUSTER)..."
    else
        echo "[build] a.out 缺失, 呼叫 $BUILD_SCRIPT --build-only 編譯..."
    fi
    bash "$BUILD_SCRIPT" --build-only
    if [ ! -x ./a.out ]; then
        echo "[FATAL] 編譯失敗, a.out 未產出."
        exit 1
    fi
    HAS_BIN=1
fi

# ═════════════════════════════════════════════════════════════════════════
# 佈置 chain state (依三情境分派)
# ═════════════════════════════════════════════════════════════════════════
if   [ "$HAS_CKPT" -eq 0 ] && [ "$HAS_STATE" -eq 0 ]; then
    echo ""
    echo "[1] 冷啟動 (全新)"
    echo "    chain state 將由 $JOBSCRIPT 自動建立 (round=1)"

elif [ "$HAS_CKPT" -eq 1 ] && [ "$HAS_STATE" -eq 0 ]; then
    echo ""
    echo "[2] checkpoint 存在但 chain state 遺失 -> 自動佈置為 Round 2"

    TS=$(date +%Y%m%d_%H%M%S)
    BACKUP_DIR="restart/history_backup_${TS}"
    NEED_BACKUP=0
    for f in checkrho.dat Ustar_Force_record.dat timing_log.dat; do
        [ -f "$f" ] && NEED_BACKUP=1
    done
    if [ "$NEED_BACKUP" -eq 1 ]; then
        mkdir -p "$BACKUP_DIR"
        for f in checkrho.dat Ustar_Force_record.dat timing_log.dat; do
            [ -f "$f" ] && cp -p "$f" "$BACKUP_DIR/" 2>/dev/null
        done
        echo "    [safeguard] 備份 history 檔 -> $BACKUP_DIR/"
    fi
    if [ -f restart/MANIFEST.txt ]; then
        echo "    [info] MANIFEST.txt 存在, 請人工核對本次 a.out 是否與 checkpoint 相容"
        echo "           (NX/NY/NZ/jp 不符可能造成續跑後 checkrho.dat 欄位錯亂)"
    fi

    echo "auto_restore_${TS}" > restart/chain_jobid
    echo "2" > restart/chain_count
    echo "    -> restart/chain_count=2, restart/chain_jobid=auto_restore_${TS}"

elif [ "$HAS_CKPT" -eq 1 ] && [ "$HAS_STATE" -eq 1 ]; then
    CC=$(cat restart/chain_count)
    echo ""
    echo "[3B] 鏈斷了 -> 接續 chain_count=$CC"
    echo "     不動 chain state, 由 jobscript 讀取續跑"

elif [ "$HAS_CKPT" -eq 0 ] && [ "$HAS_STATE" -eq 1 ]; then
    CC=$(cat restart/chain_count)
    if [ "$CC" = "1" ]; then
        echo ""
        echo "[1-edge] chain_count=1 且無 checkpoint -> Round 1 冷啟動"
    else
        echo ""
        echo "[FATAL] 異常狀態: chain_count=$CC (>=2) 但 restart/checkpoint/ 不存在"
        echo "        可能誤刪 checkpoint 或 checkpoint 目錄損毀."
        echo "        若確定要從頭跑: ./run.sh --force-cold"
        exit 1
    fi
fi

if [ -f restart/STOP_CHAIN ]; then
    echo "[cleanup] 移除舊 restart/STOP_CHAIN sentinel"
    rm -f restart/STOP_CHAIN
fi

# ═════════════════════════════════════════════════════════════════════════
# 投遞
# ═════════════════════════════════════════════════════════════════════════
echo ""
echo "[submit] 投遞: bash $BUILD_SCRIPT --no-clean --no-build  ($CLUSTER)"

# ── Auto-summary hook ──────
# [方案一 2026-04-21] 透過 env var 把 partition 狀態傳給 chain_status.sh
export RUNSH_PARTITION="$PARTITION"
export RUNSH_PART_IDLE="$PART_IDLE"
export RUNSH_PART_ALLOC="$PART_ALLOC"
export RUNSH_PART_PEND_ALL="$PART_PEND_ALL"
export RUNSH_PART_CONGESTED="$PART_CONGESTED"
if [ -f "$CHAIN_DIR/chain_status.sh" ]; then
    bash "$CHAIN_DIR/chain_status.sh" --pre-submit --cluster="$CLUSTER" 2>/dev/null || true
fi

# ═════════════════════════════════════════════════════════════════════════
# [SINGLE-HEAD] 取 HEAD.lockdir 後再呼叫 build_and_submit.sh
# ─────────────────────────────────────────────────────────────────────────
# 中心準則: 每格資料夾在 queue 最多 1 個 job.
# run.sh 在 exec 進 build_and_submit.sh 前先鎖 HEAD.lockdir (state=SUBMITTING),
# build_and_submit.sh 成功 sbatch 後負責 write_head_jobid 升級 state=PENDING.
# 若 lock 取不到 -> 已經有人在投 (A+(a) 決策: 直接讓步).
# ═════════════════════════════════════════════════════════════════════════
if type acquire_head_lock >/dev/null 2>&1; then
    if ! acquire_head_lock "run.sh-$CLUSTER"; then
        echo "[run.sh] ⚠ [SINGLE-HEAD] acquire_head_lock 失敗 — 已有 submitter 或活 job 持有 HEAD.lockdir"
        echo "         依 Single-Head 準則放棄本次投遞 (A+(a)). 請稍後再試或檢查 restart/HEAD.lockdir/owner."
        exit 6
    fi
    echo "[run.sh] [SINGLE-HEAD] ✓ 取得 HEAD.lockdir (state=SUBMITTING), 進入 build_and_submit.sh"
    export HEAD_LOCK_ACQUIRED=1
    export RUNSH_CLUSTER="$CLUSTER"
else
    echo "[run.sh] WARN: head_lock_lib.sh 未載入, 以舊邏輯 (legacy chain_jobid only) 投遞"
fi

exec bash "$BUILD_SCRIPT" --no-clean --no-build
