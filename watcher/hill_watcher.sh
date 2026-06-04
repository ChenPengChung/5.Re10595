#!/usr/bin/env bash
# hill_watcher.sh — Periodic Hill watcher loop (Re read live from variables.h)
set -u

_SELF="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RESULT_DIR="$PROJECT_DIR/result"
LIVE_DIR="$PROJECT_DIR/live"
LOG_FILE="$LIVE_DIR/watcher.log"
PID_FILE="$LIVE_DIR/watcher.pid"
HEARTBEAT="$LIVE_DIR/watcher.heartbeat"

CONV_SCRIPT="$RESULT_DIR/4.Ma_U_Time.py"
BENCH_SCRIPT="$RESULT_DIR/2.Benchmark.py"
TAUWALL_SCRIPT="$RESULT_DIR/10.tau_wall_benchmark.py"

_read_re() {
    local re
    re=$(awk '$1=="#define" && $2=="Re" {print $3; exit}' "$PROJECT_DIR/variables.h" 2>/dev/null | tr -d '[:space:]')
    printf '%s\n' "${re:-10595}"
}
RE=$(_read_re)
POLL_SEC=30
SIZE_STABLE_WAIT=3
CONV_TIMEOUT=180
BENCH_TIMEOUT=300
MIN_VTK_BYTES=1048576

mkdir -p "$LIVE_DIR"

# Redirect all stdout/stderr to LOG_FILE so callers can use `> /dev/null` safely.
exec >>"$LOG_FILE" 2>&1

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }

if [[ -f "$PID_FILE" ]]; then
    old_pid=$(cat "$PID_FILE" 2>/dev/null || true)
    if [[ -n "${old_pid:-}" && "$old_pid" != "$$" ]] && kill -0 "$old_pid" 2>/dev/null; then
        log "another watcher already running (pid=$old_pid), refusing to start"
        exit 1
    fi
fi
echo "$$" > "$PID_FILE"
trap 'rm -f "$PID_FILE" "$HEARTBEAT" "$LIVE_DIR"/.conv.marker.$$ "$LIVE_DIR"/.bench.marker.$$ "$LIVE_DIR"/.tauwall.marker.$$ 2>/dev/null; log "watcher exiting (pid=$$)"' EXIT

# [清殘留 marker] 啟動時掃除前代被殺(尤其 SIGKILL, trap 無效)殘留的 .*.marker.* temp 檔。
# 只清本專案 live/ (跨專案安全)。conv 已改無-marker, 此為清歷史殘留 + 防 bench/tauwall 殘留。
rm -f "$LIVE_DIR"/.conv.marker.* "$LIVE_DIR"/.bench.marker.* "$LIVE_DIR"/.tauwall.marker.* 2>/dev/null || true

pick_latest_vtk() {
    # Pick by modification time, NOT by the step number in the filename.
    # After a chain restart from an earlier checkpoint the global step counter
    # regresses, leaving a stale higher-step VTK in rolling retention. Selecting
    # by step number would freeze on that stale file and never process the
    # currently-written (lower-step) frames; mtime always tracks the live sim.
    local f
    f=$(ls -t "$RESULT_DIR"/velocity_merged_*.vtk 2>/dev/null | head -1 || true)
    [[ -n "$f" && -f "$f" ]] && printf '%s\n' "$f"
}

extract_step() {
    basename "$1" | sed -nE 's/^velocity_merged_0*([0-9]+)\.vtk$/\1/p'
}

is_size_stable() {
    local f="$1" s1 s2
    [[ -f "$f" ]] || return 1
    s1=$(stat -c %s "$f" 2>/dev/null) || return 1
    (( s1 >= MIN_VTK_BYTES )) || return 1
    sleep "$SIZE_STABLE_WAIT"
    s2=$(stat -c %s "$f" 2>/dev/null) || return 1
    [[ "$s1" == "$s2" ]]
}

get_accu_count() {
    local slurm_log
    slurm_log=$(ls -t "$PROJECT_DIR"/slurm_*.log 2>/dev/null | head -1)
    [[ -n "$slurm_log" ]] || { echo 0; return; }
    grep -oP 'accu=\K[0-9]+' "$slurm_log" | tail -1 || echo 0
}

# Parse FTT_STATS_START + CV_WINDOW_FTT from variables.h once.
# BENCH gate (G2): FTT >= G1 + CV window full → CV/RS statistics valid.
get_bench_gate_ftt() {
    local vh="$PROJECT_DIR/variables.h"
    [[ -f "$vh" ]] || { echo "0.0"; return; }
    awk '
        /^[[:space:]]*#define[[:space:]]+FTT_STATS_START[[:space:]]/ { gsub(/\/\/.*/,""); a=$3 }
        /^[[:space:]]*#define[[:space:]]+CV_WINDOW_FTT[[:space:]]/   { gsub(/\/\/.*/,""); b=$3 }
        END {
            if (a == "" || b == "") print "0.0";
            else printf "%.3f\n", a + b;
        }' "$vh"
}

get_latest_ftt() {
    local slurm_log
    slurm_log=$(ls -t "$PROJECT_DIR"/slurm_*.log 2>/dev/null | head -1)
    [[ -n "$slurm_log" ]] || { echo "0.00"; return; }
    grep -oP 'FTT=\K[0-9.]+' "$slurm_log" | tail -1 || echo "0.00"
}

get_latest_metrics() {
    local slurm_log
    slurm_log=$(ls -t "$PROJECT_DIR"/slurm_*.log 2>/dev/null | head -1)
    [[ -n "$slurm_log" ]] || return
    grep '^\[Step' "$slurm_log" | tail -1
}

check_nan_divergence() {
    local slurm_log
    slurm_log=$(ls -t "$PROJECT_DIR"/slurm_*.log 2>/dev/null | head -1)
    [[ -n "$slurm_log" ]] || return 0
    # [fix] whole-word 比對, 不要 substring: 原 'nan|inf' 會誤中 "Info"(solver 的 [ Info ] 行 128×)/"sinfo"
    # → 假發散 → 每輪 skip CONV → live/monitor_latest.png 不再更新。\binf\b 不會中 "Info"(f 後接 o 無邊界)。
    if tail -200 "$slurm_log" | grep -qiE '\b(nan|inf|infinity)\b|diverg|mpi_abort|fatal'; then
        log "WARNING: NaN/divergence detected in $slurm_log"
        return 1
    fi
    return 0
}

run_convergence() {
    local step="$1" capture rc
    # [無 marker 檔] 記執行前 conv 圖 mtime 當基準, 取代原 .conv.marker.$$ temp 檔。
    # 原因: conv 現在每輪都跑(~常駐), watcher 若被殺正落在 conv 中, 原本的 rm 不會執行 →
    # 殘留一堆 .conv.marker.<死PID>。改用 mtime 比對(無檔)→ 被殺也不留垃圾。
    local pre_png_mt=0 pre_pdf_mt=0 _f _m
    _f=$(ls -t "$RESULT_DIR"/monitor_convergence_*.png 2>/dev/null | head -1 || true); [[ -n "$_f" ]] && pre_png_mt=$(stat -c %Y "$_f" 2>/dev/null || echo 0)
    _f=$(ls -t "$RESULT_DIR"/monitor_convergence_*.pdf 2>/dev/null | head -1 || true); [[ -n "$_f" ]] && pre_pdf_mt=$(stat -c %Y "$_f" 2>/dev/null || echo 0)

    capture=$(cd "$RESULT_DIR" && timeout "$CONV_TIMEOUT" python3 "$CONV_SCRIPT" --Re "$RE" 2>&1)
    rc=$?

    if (( rc == 124 )); then
        log "CONV step=$step  TIMEOUT after ${CONV_TIMEOUT}s"; return 1
    fi
    if (( rc != 0 )); then
        log "CONV step=$step  FAILED rc=$rc :: $(printf '%s' "$capture" | tail -c 300 | tr '\n' ' ')"
        return 1
    fi

    local src_png src_pdf copied_png="" copied_pdf=""
    src_png=$(ls -t "$RESULT_DIR"/monitor_convergence_*.png 2>/dev/null | head -1 || true)
    src_pdf=$(ls -t "$RESULT_DIR"/monitor_convergence_*.pdf 2>/dev/null | head -1 || true)

    if [[ -n "$src_png" ]]; then
        _m=$(stat -c %Y "$src_png" 2>/dev/null || echo 0)
        (( _m > pre_png_mt )) && { cp -f "$src_png" "$LIVE_DIR/monitor_latest.png"; copied_png=$(basename "$src_png"); }
    fi
    if [[ -n "$src_pdf" ]]; then
        _m=$(stat -c %Y "$src_pdf" 2>/dev/null || echo 0)
        (( _m > pre_pdf_mt )) && { cp -f "$src_pdf" "$LIVE_DIR/monitor_latest.pdf"; copied_pdf=$(basename "$src_pdf"); }
    fi

    local conv_line
    conv_line=$(printf '%s\n' "$capture" | grep -E '\[OK\]|CONVERGED|NEAR|NOT_CONVERGED|CV' | tail -1 | sed -E 's/^[[:space:]]+//' || true)

    log "CONV step=$step  Re=$RE  png=$copied_png  pdf=$copied_pdf  ${conv_line:+:: }$conv_line"
    return 0
}

run_benchmark() {
    local step="$1" capture rc
    local before_marker="$LIVE_DIR/.bench.marker.$$"
    : > "$before_marker"

    capture=$(cd "$RESULT_DIR" && timeout "$BENCH_TIMEOUT" python3 "$BENCH_SCRIPT" \
        --Re "$RE" --no-ask-scales --no-ask-density 2>&1)
    rc=$?

    if (( rc == 124 )); then
        log "BENCH step=$step  TIMEOUT after ${BENCH_TIMEOUT}s"; rm -f "$before_marker"; return 1
    fi
    if (( rc != 0 )); then
        log "BENCH step=$step  FAILED rc=$rc :: $(printf '%s' "$capture" | tail -c 300 | tr '\n' ' ')"
        rm -f "$before_marker"; return 1
    fi

    local src copied=""
    for pat in fig_mean_u.png fig_mean_v.png fig_uu.png fig_vv.png fig_uv.png fig_k.png; do
        src=$(ls -t "$RESULT_DIR"/$pat 2>/dev/null | head -1 || true)
        if [[ -n "$src" ]] && [[ "$src" -nt "$before_marker" ]]; then
            cp -f "$src" "$LIVE_DIR/$(basename "$src")"; copied="$copied $(basename "$src")"
        fi
    done
    rm -f "$before_marker"

    log "BENCH step=$step  Re=$RE  outputs:${copied:- (none)}"
    return 0
}

run_tauwall() {
    local step="$1" capture rc
    local before_marker="$LIVE_DIR/.tauwall.marker.$$"
    : > "$before_marker"

    capture=$(cd "$RESULT_DIR" && timeout "$BENCH_TIMEOUT" python3 "$TAUWALL_SCRIPT" \
        --Re "$RE" --auto 2>&1)
    rc=$?

    if (( rc == 124 )); then
        log "TAUWALL step=$step  TIMEOUT after ${BENCH_TIMEOUT}s"; rm -f "$before_marker"; return 1
    fi
    if (( rc != 0 )); then
        log "TAUWALL step=$step  FAILED rc=$rc :: $(printf '%s' "$capture" | tail -c 300 | tr '\n' ' ')"
        rm -f "$before_marker"; return 1
    fi

    local src copied=""
    for pat in "tau_wall_signed_Re${RE}_cf.png" "tau_wall_signed_Re${RE}_cp.png"; do
        src="$RESULT_DIR/$pat"
        if [[ -f "$src" ]] && [[ "$src" -nt "$before_marker" ]]; then
            cp -f "$src" "$LIVE_DIR/$pat"; copied="$copied $pat"
        fi
    done
    rm -f "$before_marker"

    log "TAUWALL step=$step  Re=$RE  outputs:${copied:- (none)}"
    return 0
}

log "=========================================="
log "Periodic Hill Re$RE watcher started"
log "  pid=$$  ppid=$PPID  poll=${POLL_SEC}s"
log "  project  = $PROJECT_DIR"
log "  conv     = $CONV_SCRIPT"
log "  bench    = $BENCH_SCRIPT"
log "  tauwall  = $TAUWALL_SCRIPT"
log "=========================================="

last_processed=""
last_mtime=""
last_bench_step=""

while :; do
    # [a.out 生命週期自死, 2026-06-04] a.out 不存在 = 專案已拆除 → watcher 自死。
    # 跨節點 watcher 也能「自己停」, 不需 ssh 去殺 (解決 watcher-ignores-stopchain 的跨節點難題)。
    # 同時尊重 STOP_CHAIN (使用者刻意停)。死前清自己的 heartbeat/pid, 免 keepalive/啟動器誤判仍活。
    if [ ! -e "$PROJECT_DIR/a.out" ] || [ -f "$PROJECT_DIR/restart/STOP_CHAIN" ]; then
        if [ -e "$PROJECT_DIR/a.out" ]; then log "STOP_CHAIN 偵測到 → watcher 自死 (使用者已停 chain)"
        else log "a.out 不存在 → watcher 自死 (專案已拆除)"; fi
        rm -f "$HEARTBEAT" "$PID_FILE" 2>/dev/null || true
        exit 0
    fi
    # [跨節點判活] 每輪 touch heartbeat; keepalive/啟動器以此檔 mtime 新鮮度判活,
    # 不靠 kill -0 (cron 可能在別 login node, kill -0 會誤判 watcher 死 → 反覆誤殺重啟 churn)。
    touch "$HEARTBEAT" 2>/dev/null || true

    RE=$(_read_re)

    if ! check_nan_divergence; then
        log "ALERT: simulation may be diverging — check slurm log immediately"
    fi

    # [改] 收斂圖每輪都重產: 4.Ma_U_Time.py 讀 .dat 時間序列(非 VTK), 每輪都有新資料點
    # → live/monitor_latest.png 持續更新, 不再被「等下一顆 VTK(~NDTVTK 步)」卡住變舊圖。
    cstep=$(get_latest_metrics | grep -oP '\[Step \K[0-9]+' | head -1)
    run_convergence "${cstep:-live}" || true

    vtk=$(pick_latest_vtk || true)
    if [[ -n "$vtk" ]]; then
        cur_mtime=$(stat -c %Y "$vtk" 2>/dev/null || echo 0)
        # Reprocess when a different file is newest OR the same path was
        # rewritten (mtime changed). Covers chain-restart step regression and
        # same-step VTK overwrites that path string-equality alone would miss.
        if [[ "$vtk" != "$last_processed" || "$cur_mtime" != "$last_mtime" ]] && is_size_stable "$vtk"; then
            step=$(extract_step "$vtk")
            ftt=$(get_latest_ftt)
            accu=$(get_accu_count)
            metrics=$(get_latest_metrics)

            log "──────────────────────────────────────"
            log "PROCESS step=$step  FTT=$ftt  accu=$accu (new VTK)"
            [[ -n "$metrics" ]] && log "  $metrics"

            # 收斂圖已在每輪迴圈頂端重產 (見上方 run_convergence), 此處不重複呼叫。

            # BENCH gate (G2): FTT >= FTT_STATS_START + CV_WINDOW_FTT
            # — only fire benchmark figures once CV window has filled,
            #   otherwise RS fields are too noisy (statistics not yet meaningful).
            bench_gate=$(get_bench_gate_ftt)
            if awk -v f="$ftt" -v g="$bench_gate" 'BEGIN{exit !(f>=g && g>0)}'; then
                if [[ "$last_bench_step" != "$step" ]]; then
                    log "BENCH trigger: FTT=$ftt >= G2=$bench_gate (accu=$accu)"
                    run_benchmark "$step" || true
                    run_tauwall "$step" || true
                    last_bench_step="$step"
                fi
            else
                log "BENCH skipped: FTT=$ftt < G2=$bench_gate (accu=$accu, CV window not full)"
            fi

            last_processed="$vtk"
            last_mtime="$cur_mtime"
        fi
    fi
    sleep "$POLL_SEC"
done
