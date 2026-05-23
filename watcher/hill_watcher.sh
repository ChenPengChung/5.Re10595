#!/usr/bin/env bash
# hill_watcher.sh — Periodic Hill Re10595 watcher loop
set -u

# If launched from run.sh/build_and_submit.sh, do not hold run.sh's flock fd.
{ exec 200>&-; } 2>/dev/null || true

_SELF="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RESULT_DIR="$PROJECT_DIR/result"
LIVE_DIR="$PROJECT_DIR/live"
LOG_FILE="$LIVE_DIR/watcher.log"
PID_FILE="$LIVE_DIR/watcher.pid"

CONV_SCRIPT="$RESULT_DIR/4.Ma_U_Time.py"
BENCH_SCRIPT="$RESULT_DIR/2.Benchmark.py"
TAUWALL_SCRIPT="$RESULT_DIR/10.tau_wall_benchmark.py"
DENSITY_AUDIT_SCRIPT="$RESULT_DIR/10.restart_density_audit.py"

RE=10595
POLL_SEC=30
SIZE_STABLE_WAIT=3
CONV_TIMEOUT=180
BENCH_TIMEOUT=300
DENSITY_AUDIT_TIMEOUT=300
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
trap 'rm -f "$PID_FILE"; log "watcher exiting (pid=$$)"' EXIT

pick_latest_vtk() {
    local f best_step=-1 best_path="" step
    for f in "$RESULT_DIR"/velocity_merged_*.vtk; do
        [[ -f "$f" ]] || continue
        step=$(basename "$f" | sed -nE 's/^velocity_merged_0*([0-9]+)\.vtk$/\1/p')
        [[ -n "$step" ]] || continue
        if (( step > best_step )); then
            best_step=$step
            best_path=$f
        fi
    done
    [[ -n "$best_path" ]] && printf '%s\n' "$best_path"
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
    if tail -200 "$slurm_log" | grep -qiE 'nan|inf|diverge|ABORT|FATAL'; then
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

run_density_audit() {
    local step="$1" capture rc
    local before_marker="$LIVE_DIR/.density.marker.$$"
    : > "$before_marker"

    if [[ ! -f "$DENSITY_AUDIT_SCRIPT" ]]; then
        log "DENSITY step=$step  skipped: script not found"
        rm -f "$before_marker"
        return 0
    fi

    capture=$(cd "$RESULT_DIR" && timeout "$DENSITY_AUDIT_TIMEOUT" python3 "$DENSITY_AUDIT_SCRIPT" 2>&1)
    rc=$?

    if (( rc == 124 )); then
        log "DENSITY step=$step  TIMEOUT after ${DENSITY_AUDIT_TIMEOUT}s"
        rm -f "$before_marker"
        return 1
    fi
    if (( rc != 0 )); then
        log "DENSITY step=$step  FAILED rc=$rc :: $(printf '%s' "$capture" | tail -c 300 | tr '\n' ' ')"
        rm -f "$before_marker"
        return 1
    fi

    local src_png src_pdf src_latex_pdf src_tex src_csv copied=""
    src_png="$RESULT_DIR/restart_density_audit.png"
    src_pdf="$RESULT_DIR/restart_density_audit.pdf"
    src_latex_pdf="$RESULT_DIR/restart_density_audit_latex.pdf"
    src_tex="$RESULT_DIR/restart_density_audit.tex"
    src_csv="$RESULT_DIR/restart_density_audit.csv"

    if [[ -f "$src_png" ]] && [[ "$src_png" -nt "$before_marker" ]]; then
        cp -f "$src_png" "$LIVE_DIR/restart_density_latest.png"
        copied="$copied restart_density_latest.png"
    fi
    if [[ -f "$src_pdf" ]] && [[ "$src_pdf" -nt "$before_marker" ]]; then
        cp -f "$src_pdf" "$LIVE_DIR/restart_density_latest.pdf"
        copied="$copied restart_density_latest.pdf"
    fi
    if [[ -f "$src_latex_pdf" ]] && [[ "$src_latex_pdf" -nt "$before_marker" ]]; then
        cp -f "$src_latex_pdf" "$LIVE_DIR/restart_density_latest_latex.pdf"
        copied="$copied restart_density_latest_latex.pdf"
    fi
    if [[ -f "$src_tex" ]] && [[ "$src_tex" -nt "$before_marker" ]]; then
        cp -f "$src_tex" "$LIVE_DIR/restart_density_latest.tex"
        copied="$copied restart_density_latest.tex"
    fi
    if [[ -f "$src_csv" ]] && [[ "$src_csv" -nt "$before_marker" ]]; then
        cp -f "$src_csv" "$LIVE_DIR/restart_density_latest.csv"
        copied="$copied restart_density_latest.csv"
    fi
    rm -f "$before_marker"

    local density_line
    density_line=$(printf '%s\n' "$capture" | grep -E '^\[LATEST\]' | tail -1 | sed -E 's/^[[:space:]]+//' || true)
    log "DENSITY step=$step outputs:${copied:- (none)} ${density_line:+:: }$density_line"
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
last_bench_step=""

while :; do
    if ! check_nan_divergence; then
        log "ALERT: simulation may be diverging — check slurm log immediately"
    fi

    vtk=$(pick_latest_vtk || true)
    if [[ -n "$vtk" && "$vtk" != "$last_processed" ]]; then
        if is_size_stable "$vtk"; then
            step=$(extract_step "$vtk")
            ftt=$(get_latest_ftt)
            accu=$(get_accu_count)
            metrics=$(get_latest_metrics)

            log "──────────────────────────────────────"
            log "PROCESS step=$step  FTT=$ftt  accu=$accu"
            [[ -n "$metrics" ]] && log "  $metrics"

            run_convergence "$step" || true
            run_density_audit "$step" || true

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
        fi
    fi
    sleep "$POLL_SEC"
done
