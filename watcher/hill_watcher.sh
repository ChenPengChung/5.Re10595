#!/usr/bin/env bash
# hill_watcher.sh — Periodic Hill watcher loop (Re read live from variables.h)
set -u

_SELF="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR" || exit 1   # pin process cwd to PROJECT_ROOT so the keepalive death-kill's
                              # cwd guard matches this watcher, and relative restart/* checks work
RESULT_DIR="$PROJECT_DIR/result"
LIVE_DIR="$PROJECT_DIR/live"
LOG_FILE="$LIVE_DIR/watcher.log"
PID_FILE="$LIVE_DIR/watcher.pid"

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

# ── Cross-node-safe single-instance guard (atomic lockdir + per-loop heartbeat) ──
# Why not kill -0 on watcher.pid: PIDs are per-login-node, so a PID written by a
# watcher on ANOTHER login node always fails kill -0 here → false "dead" → a
# duplicate watcher is spawned. Why not log/image mtime: this loop only writes
# output when a NEW vtk appears, so mtime goes stale while the watcher is alive.
# The authoritative liveness signal is therefore $HB_FILE, refreshed every loop
# iteration below (shared FS, node-independent). The lockdir makes the
# single-instance decision atomic even under a simultaneous-start race.
HB_FILE="$LIVE_DIR/watcher.heartbeat"
LOCK_DIR="$LIVE_DIR/watcher.lock.d"
HB_FRESH=300   # must exceed the slowest single loop (a CONV render can take up to CONV_TIMEOUT=180s)
HOST="$(hostname 2>/dev/null || echo '?')"
_hb_age() { echo $(( $(date +%s) - $(stat -c %Y "$HB_FILE" 2>/dev/null || echo 0) )); }
_write_hb() { printf '%s %s %s\n' "$(date +%s)" "$HOST" "$$" > "$HB_FILE" 2>/dev/null || true; }
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    if [[ -f "$HB_FILE" ]] && (( $(_hb_age) < HB_FRESH )); then
        log "another watcher alive (heartbeat $(_hb_age)s ago: $(cat "$HB_FILE" 2>/dev/null)) — refusing to start (pid=$$ host=$HOST)"
        exit 0
    fi
    log "stale watcher lock (heartbeat $(_hb_age)s) — taking over (pid=$$ host=$HOST)"
    rmdir "$LOCK_DIR" 2>/dev/null || true
    mkdir "$LOCK_DIR" 2>/dev/null || { log "lock race lost — refusing (pid=$$ host=$HOST)"; exit 0; }
fi
echo "$$" > "$PID_FILE"
_write_hb
trap 'rmdir "$LOCK_DIR" 2>/dev/null; rm -f "$PID_FILE" "$HB_FILE"; log "watcher exiting (pid=$$ host=$HOST)"' EXIT

# [STOP guard 2026-06-04] 環境未就緒(chain 停)時不空轉: 對齊 daemon_keepalive 的 STOP_CHAIN 邏輯。
# STOP_CHAIN 在 → 不啟動 (被 keepalive/並行session 重生的新 watcher 載入此碼即立刻退出, 消除空轉噪音)。
if [ -f restart/STOP_CHAIN ]; then
    log "STOP_CHAIN present — watcher NOT starting (env not ready, pid=$$ host=$HOST)"
    exit 0
fi

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
    local before_marker="$LIVE_DIR/.conv.marker.$$"
    : > "$before_marker"

    capture=$(cd "$RESULT_DIR" && timeout "$CONV_TIMEOUT" python3 "$CONV_SCRIPT" --Re "$RE" 2>&1)
    rc=$?

    if (( rc == 124 )); then
        log "CONV step=$step  TIMEOUT after ${CONV_TIMEOUT}s"; rm -f "$before_marker"; return 1
    fi
    if (( rc != 0 )); then
        log "CONV step=$step  FAILED rc=$rc :: $(printf '%s' "$capture" | tail -c 300 | tr '\n' ' ')"
        rm -f "$before_marker"; return 1
    fi

    local src_png src_pdf copied_png="" copied_pdf=""
    src_png=$(ls -t "$RESULT_DIR"/monitor_convergence_*.png 2>/dev/null | head -1 || true)
    src_pdf=$(ls -t "$RESULT_DIR"/monitor_convergence_*.pdf 2>/dev/null | head -1 || true)

    if [[ -n "$src_png" ]] && [[ "$src_png" -nt "$before_marker" ]]; then
        cp -f "$src_png" "$LIVE_DIR/monitor_latest.png"; copied_png=$(basename "$src_png")
    fi
    if [[ -n "$src_pdf" ]] && [[ "$src_pdf" -nt "$before_marker" ]]; then
        cp -f "$src_pdf" "$LIVE_DIR/monitor_latest.pdf"; copied_pdf=$(basename "$src_pdf")
    fi
    rm -f "$before_marker"

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
log "  pid=$$  ppid=$PPID  host=$HOST  poll=${POLL_SEC}s"
log "  project  = $PROJECT_DIR"
log "  conv     = $CONV_SCRIPT"
log "  bench    = $BENCH_SCRIPT"
log "  tauwall  = $TAUWALL_SCRIPT"
log "=========================================="

last_processed=""
last_mtime=""
last_bench_step=""

while :; do
    # [STOP guard 2026-06-04] 每輪檢查: chain 停/環境未就緒 → 乾淨退出, 不再空轉。
    if [ -f restart/STOP_CHAIN ]; then
        log "STOP_CHAIN present — watcher exiting (pid=$$ host=$HOST)"
        exit 0
    fi
    # [a.out death gate] solver binary 全消失 → 專案已拆除 → watcher self-exit (跨節點安全:
    # 每個 watcher 各自檢查共享 FS 上的 a.out*; 補強 keepalive 同節點 kill 的盲點)。
    if [ ! -s "$PROJECT_DIR/a.out" ] && [ ! -s "$PROJECT_DIR/a.out.H200" ] && [ ! -s "$PROJECT_DIR/a.out.GB200" ]; then
        # FALSE-DEATH guard: a ./run build holds .run.lock flock while binaries may be
        # transiently absent — keep running through the build window instead of exiting.
        if [ -e "$PROJECT_DIR/.run.lock" ] && command -v flock >/dev/null 2>&1 \
                && ! ( flock -n 9 ) 9< "$PROJECT_DIR/.run.lock" 2>/dev/null; then
            log "DEATH GATE: binary 暫缺但 .run.lock 被佔用 (build 進行中) — watcher 續跑 (pid=$$ host=$HOST)"
        else
            log "DEATH GATE: no solver binary (a.out/.H200/.GB200) — watcher exiting (pid=$$ host=$HOST)"
            exit 0
        fi
    fi
    RE=$(_read_re)
    _write_hb            # cross-node liveness heartbeat — refresh every iteration

    if ! check_nan_divergence; then
        log "ALERT: simulation may be diverging — check slurm log immediately"
    fi

    # ── live convergence plot EVERY poll (decoupled from VTK arrival) ──
    # 4.Ma_U_Time.py reads ONLY the time-series .dat (Ustar_Force_record/
    # checkrho/timing_log, updated every ~50 steps ≈ 1s) — no VTK needed.
    # Refresh live/monitor_latest.png every poll (~3s/run) so the operator
    # sees Re/Ma/Ub/Force in near-real-time instead of every ~18min (VTK cadence).
    run_convergence "live" || true

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
            log "PROCESS step=$step  FTT=$ftt  accu=$accu"
            [[ -n "$metrics" ]] && log "  $metrics"

            # convergence plot already runs EVERY poll above (decoupled from
            # VTK arrival) — no need to re-run it here on new-VTK detection.

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
    # [checklist 即時刷新] 每輪重生 checklist.txt（純 stat 探測，唯讀於 job/daemon；
    # 失敗絕不中斷 watcher）。輸出抑制以免每 30s 灌爆 watcher.log。
    python3 "$PROJECT_DIR/checklist.py" >/dev/null 2>&1 || true
    _write_hb           # refresh heartbeat again before sleeping (bounds staleness)
    sleep "$POLL_SEC"
done
