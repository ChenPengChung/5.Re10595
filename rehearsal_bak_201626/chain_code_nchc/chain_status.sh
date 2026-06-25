#!/bin/bash
# ==============================================================================
# chain_status.sh — GILBM Periodic Hill 自動續鏈狀態彙整工具
#
# 功能:
#   於每次 run.sh 投遞前 / jobscript 開始執行時 / 結束時自動呼叫,
#   在 restart/summary/ 生成一份「乾淨整齊」的續跑檔案快照,
#   並在 restart/SUMMARY.md 追加一筆人類可讀的摘要。
#
# 使用者也可手動呼叫:
#   bash chain_status.sh                  # 當下整理並列印摘要
#   bash chain_status.sh --pre-submit     # run.sh 於 sbatch 前呼叫
#   bash chain_status.sh --job-start      # jobscript 開工時呼叫
#   bash chain_status.sh --job-end RC     # jobscript 結束時呼叫 (帶 exit code)
#   bash chain_status.sh --cluster=GB200  # 明示叢集 (被 run.sh/jobscript 傳入)
#   bash chain_status.sh --quiet          # 不列印到 stdout,只寫檔
#
# 產生物 (全部都在 restart/ 下,不污染專案根目錄):
#   restart/SUMMARY.md                     主摘要 (append-only, 人類可讀)
#   restart/summary/latest.txt             最新一次快照 (覆寫)
#   restart/summary/snapshots/<TS>.txt     歷次快照 (append)
#   restart/summary/checkpoint_index.txt   已找到的 checkpoint 列表
#
# 設計原則:
#   - 永遠不動到 solver 的 state 檔 (chain_jobid / chain_count / checkpoint/)
#   - 任何 I/O 失敗都以 warn 輸出後繼續 (不讓 jobscript 因摘要而失敗)
#   - 全程 read-only 對 chain state,只寫自己的 summary/ 資料夾
#   - 快照大小控制:只記錄 metadata,不複製大型 checkpoint 二進位檔
# ==============================================================================

set +e   # 摘要工具本身絕對不該讓呼叫端失敗

# ── [方案 A path discipline] ──
# 以 PROJECT_ROOT 為 cwd (restart/, result/, a.out 都在 root). CHAIN_DIR 為
# 本 script 所在, 用於未來需要引用同伴 script 的絕對路徑.
_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || realpath "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
CHAIN_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
PROJECT_ROOT="$(cd "$CHAIN_DIR/.." && pwd)"
cd "$PROJECT_ROOT" 2>/dev/null || true

MODE="ondemand"
RC_FROM_CALLER=""
CLUSTER="auto"
QUIET=0

for arg in "$@"; do
    case "$arg" in
        --pre-submit)   MODE="pre-submit" ;;
        --job-start)    MODE="job-start" ;;
        --job-end)      MODE="job-end" ;;
        --job-end=*)    MODE="job-end"; RC_FROM_CALLER="${arg#--job-end=}" ;;
        --cluster=*)    CLUSTER="${arg#--cluster=}" ;;
        --quiet|-q)     QUIET=1 ;;
        --rc=*)         RC_FROM_CALLER="${arg#--rc=}" ;;
        [0-9]*)
            # Allow `--job-end 42` shell-friendly form
            if [ "$MODE" = "job-end" ] && [ -z "$RC_FROM_CALLER" ]; then
                RC_FROM_CALLER="$arg"
            fi
            ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0 ;;
    esac
done

say() { [ "$QUIET" -eq 0 ] && echo "$@"; }
warn() { echo "[chain_status.sh] WARN: $*" >&2; }

# ── Resolve cluster if not provided ───────────────────────────────────────
if [ "$CLUSTER" = "auto" ] || [ -z "$CLUSTER" ]; then
    case "$(uname -m 2>/dev/null)" in
        aarch64|arm64) CLUSTER="GB200" ;;
        x86_64|amd64)  CLUSTER="H200" ;;
        *)             CLUSTER="unknown" ;;
    esac
fi

# ── Ensure output dirs ────────────────────────────────────────────────────
mkdir -p restart/summary/snapshots 2>/dev/null

TS=$(date '+%Y%m%d_%H%M%S')
NOW=$(date '+%F %T %Z')

SNAPSHOT_FILE="restart/summary/snapshots/${TS}_${MODE}.txt"
LATEST_FILE="restart/summary/latest.txt"
CKPT_INDEX="restart/summary/checkpoint_index.txt"
SUMMARY_MD="restart/SUMMARY.md"

# ── Read chain state (read-only) ──────────────────────────────────────────
CHAIN_JOBID=$(cat restart/chain_jobid 2>/dev/null)
CHAIN_COUNT=$(cat restart/chain_count 2>/dev/null)
STOP_CHAIN_SENTINEL="no"
[ -f restart/STOP_CHAIN ] && STOP_CHAIN_SENTINEL="yes"
FAST_FAIL=$(cat restart/fast_fail_count 2>/dev/null || echo 0)
# [BLACKLIST-LIB] bad_nodes 可能是舊格式 (純 hostname) 或新格式 (<node>\t<ts>\t<reason>\t<src>)
# 顯示時只取第一欄 (hostname), 空行與註解略過
BAD_NODES=$(awk -F'\t' 'NF>=1 && $1!="" && $1!~/^#/ {print $1}' restart/bad_nodes 2>/dev/null \
            | sort -u | paste -sd,)
HAS_STOP_CHAIN="$STOP_CHAIN_SENTINEL"

# ── Enumerate checkpoints (valid = has non-empty metadata.dat) ────────────
LATEST_STEP=""
LATEST_DIR=""
VALID_CKPTS=()
INVALID_CKPTS=()
WRITING_CKPTS=()
if [ -d restart/checkpoint ]; then
    for d in $(ls -1d restart/checkpoint/step_*/ 2>/dev/null | sort -V); do
        d="${d%/}"
        case "$d" in
            *.WRITING) WRITING_CKPTS+=("$d"); continue ;;
        esac
        if [ -s "$d/metadata.dat" ]; then
            VALID_CKPTS+=("$d")
        else
            INVALID_CKPTS+=("$d")
        fi
    done
fi
if [ ${#VALID_CKPTS[@]} -gt 0 ]; then
    LATEST_DIR="${VALID_CKPTS[-1]}"
    LATEST_STEP="${LATEST_DIR##*step_}"
fi

# ── Binary info ───────────────────────────────────────────────────────────
BIN_STATUS="absent"
BIN_SIZE=""
BIN_MD5=""
if [ -x ./a.out ]; then
    BIN_STATUS="present"
    BIN_SIZE=$(stat -c %s ./a.out 2>/dev/null || stat -f %z ./a.out 2>/dev/null)
    BIN_MD5=$(md5sum ./a.out 2>/dev/null | awk '{print $1}')
    [ -z "$BIN_MD5" ] && BIN_MD5=$(md5 -q ./a.out 2>/dev/null)
fi

# ── Queue status (best-effort) ────────────────────────────────────────────
QUEUE_LINE=""
if command -v squeue >/dev/null 2>&1 && [ -n "$CHAIN_JOBID" ]; then
    QUEUE_LINE=$(squeue -h -j "$CHAIN_JOBID" -o '%i %T %M %R' 2>/dev/null | head -1)
fi

# ── SLURM job context (if running inside jobscript) ──────────────────────
SLURM_CTX=""
if [ -n "$SLURM_JOB_ID" ]; then
    SLURM_CTX="SLURM_JOB_ID=$SLURM_JOB_ID SLURM_NODELIST=${SLURM_NODELIST:-?} SLURM_NTASKS=${SLURM_NTASKS:-?}"
fi

# ── Compose snapshot ──────────────────────────────────────────────────────
{
    echo "# GILBM Periodic Hill — chain snapshot"
    echo "#"
    echo "# mode            : $MODE"
    echo "# timestamp       : $NOW"
    echo "# cluster         : $CLUSTER"
    echo "# pwd             : $(pwd)"
    [ -n "$SLURM_CTX" ] && echo "# slurm           : $SLURM_CTX"
    echo ""
    echo "## Chain state"
    echo "chain_jobid       : ${CHAIN_JOBID:-<none>}"
    echo "chain_count       : ${CHAIN_COUNT:-<none>}"
    echo "STOP_CHAIN        : $HAS_STOP_CHAIN"
    echo "fast_fail_count   : $FAST_FAIL"
    echo "bad_nodes         : ${BAD_NODES:-<none>}"
    echo ""
    echo "## Binary"
    echo "a.out             : $BIN_STATUS"
    [ -n "$BIN_SIZE" ] && echo "a.out size        : $BIN_SIZE bytes"
    [ -n "$BIN_MD5" ]  && echo "a.out md5         : $BIN_MD5"
    echo ""
    echo "## Checkpoints"
    echo "valid count       : ${#VALID_CKPTS[@]}"
    echo "invalid count     : ${#INVALID_CKPTS[@]}"
    echo "writing count     : ${#WRITING_CKPTS[@]}"
    if [ -n "$LATEST_DIR" ]; then
        echo "latest valid dir  : $LATEST_DIR"
        echo "latest valid step : $LATEST_STEP"
        META_SIZE=$(stat -c %s "$LATEST_DIR/metadata.dat" 2>/dev/null)
        [ -n "$META_SIZE" ] && echo "latest meta bytes : $META_SIZE"
    else
        echo "latest valid dir  : <none>"
    fi
    if [ ${#INVALID_CKPTS[@]} -gt 0 ]; then
        echo "invalid samples   :"
        for d in "${INVALID_CKPTS[@]:0:3}"; do echo "  - $d"; done
    fi
    if [ ${#WRITING_CKPTS[@]} -gt 0 ]; then
        echo "writing samples   :"
        for d in "${WRITING_CKPTS[@]:0:3}"; do echo "  - $d"; done
    fi
    echo ""
    echo "## Queue"
    if [ -n "$QUEUE_LINE" ]; then
        echo "squeue line       : $QUEUE_LINE"
    else
        echo "squeue line       : <no queued job or squeue unavailable>"
    fi
    echo ""
    echo "## Files of interest (read-only references)"
    echo "  restart/chain_jobid"
    echo "  restart/chain_count"
    echo "  restart/chain.log           <-- full tee log"
    echo "  restart/MANIFEST.txt        <-- build + resume journal"
    echo "  restart/STOP_CHAIN          <-- sentinel to halt chain (only if present)"
    echo "  restart/checkpoint/step_*/  <-- solver state (binary, do not move)"
    echo "  restart/fast_fail_count     <-- consecutive fast-fail counter"
    echo "  restart/bad_nodes           <-- project-local node blacklist"
    if [ -n "$RC_FROM_CALLER" ]; then
        echo ""
        echo "## Exit status (from caller)"
        echo "RC                : $RC_FROM_CALLER"
        case "$RC_FROM_CALLER" in
            0)   echo "semantics         : 自然停止 (converged/diverged/FTT/STOP_CHAIN) — 鏈停" ;;
            42)  echo "semantics         : POLICY-C1 不可避免錯誤 — 鏈停 (不續投)" ;;
            124) echo "semantics         : walltime 救援 / SIGUSR1 — 續鏈 (resumable)" ;;
            *)   echo "semantics         : crash / node failure — 續鏈" ;;
        esac
    fi
} > "$SNAPSHOT_FILE"

# latest.txt mirrors the most recent snapshot
cp -f "$SNAPSHOT_FILE" "$LATEST_FILE" 2>/dev/null

# Append-only checkpoint index (for quick grep/tail review)
if [ -n "$LATEST_DIR" ]; then
    printf '%s  valid=%d  latest=%s  mode=%s\n' \
        "$NOW" "${#VALID_CKPTS[@]}" "$LATEST_STEP" "$MODE" >> "$CKPT_INDEX"
else
    printf '%s  valid=%d  latest=<none>  mode=%s\n' \
        "$NOW" "${#VALID_CKPTS[@]}" "$MODE" >> "$CKPT_INDEX"
fi

# ── Maintain SUMMARY.md (human-facing) ────────────────────────────────────
if [ ! -f "$SUMMARY_MD" ]; then
    cat > "$SUMMARY_MD" <<EOF_HDR
# GILBM Periodic Hill — 續鏈彙整 (SUMMARY.md)

此檔由 \`chain_status.sh\` 自動維護。每次 run.sh 投遞前 / jobscript 開工 / 結束時
會追加一行事件紀錄。完整快照存於 \`restart/summary/snapshots/\`,最近一次快照
存於 \`restart/summary/latest.txt\`。

## 事件時間軸

| 時刻 | 模式 | cluster | round | latest_step | 有效 ckpt | fast_fail | STOP | RC | 備註 |
|------|------|---------|-------|-------------|-----------|-----------|------|----|------|
EOF_HDR
fi

# Append a row. Use defensive defaults so empty cells render as "-".
RC_DISPLAY="${RC_FROM_CALLER:--}"
CC_DISPLAY="${CHAIN_COUNT:--}"
LATEST_DISPLAY="${LATEST_STEP:--}"
NOTE=""
if [ "$HAS_STOP_CHAIN" = "yes" ]; then NOTE="${NOTE} STOP_CHAIN sentinel;"; fi
if [ -n "${INVALID_CKPTS[*]}" ]; then NOTE="${NOTE} ${#INVALID_CKPTS[@]} invalid ckpt(s);"; fi
if [ -n "${WRITING_CKPTS[*]}" ]; then NOTE="${NOTE} ${#WRITING_CKPTS[@]} .WRITING;"; fi
[ -z "$NOTE" ] && NOTE="-"

printf '| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |\n' \
    "$NOW" "$MODE" "$CLUSTER" "$CC_DISPLAY" "$LATEST_DISPLAY" \
    "${#VALID_CKPTS[@]}" "$FAST_FAIL" "$HAS_STOP_CHAIN" "$RC_DISPLAY" "$NOTE" \
    >> "$SUMMARY_MD"

# ── Human feedback (unless --quiet) ──────────────────────────────────────
say "════════════════════════════════════════════════════════════════"
say " chain_status.sh [$MODE] @ $NOW  ($CLUSTER)"
say "   chain_count     : ${CHAIN_COUNT:-<none>}"
say "   chain_jobid     : ${CHAIN_JOBID:-<none>}"
say "   a.out           : $BIN_STATUS${BIN_MD5:+ (md5=${BIN_MD5:0:8}…)}"
say "   latest ckpt     : ${LATEST_DIR:-<none>}"
say "   valid ckpts     : ${#VALID_CKPTS[@]}  (invalid=${#INVALID_CKPTS[@]}, .WRITING=${#WRITING_CKPTS[@]})"
say "   fast_fail_count : $FAST_FAIL"
say "   STOP_CHAIN      : $HAS_STOP_CHAIN"
[ -n "$RC_FROM_CALLER" ] && say "   RC (caller)     : $RC_FROM_CALLER"
say "   snapshot        : $SNAPSHOT_FILE"
say "   summary (md)    : $SUMMARY_MD"
say "════════════════════════════════════════════════════════════════"

exit 0
