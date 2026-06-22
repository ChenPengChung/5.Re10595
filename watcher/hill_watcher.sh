#!/usr/bin/env bash
# hill_watcher.sh — Periodic Hill Re5600 watcher loop
set -u

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

_read_re() {
    local re
    re=$(awk '$1=="#define" && $2=="Re" {print $3; exit}' "$PROJECT_DIR/variables.h" 2>/dev/null | tr -d '[:space:]')
    printf '%s\n' "${re:-5600}"
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

# ── 跨節點單例鎖 (atomic mkdir on shared FS) + heartbeat ──────────────────────
# 因 ~/.config/systemd/user 在共享 home → systemd enable 等於全 5 個登入節點都啟用 →
# 每節點各起一隻 watcher(跨節點重複, 其中一節點曾 spin 到 ~140 隻)。enable/disable 是
# 共享符號連結, 無法只關某一節點; 故唯一正解 = 腳本內跨節點單例鎖: 全叢集同時只有一隻
# watcher 真正工作, 其餘節點 exit 0 退讓(systemd Restart=on-failure 不會重啟 exit 0)。
MYHOST="$(hostname)"
NODELOCK="$LIVE_DIR/watcher.nodelock"     # 原子 mkdir(NFS 上 mkdir 仍為原子)
HEARTBEAT="$LIVE_DIR/watcher.heartbeat"   # MYHOST:pid:epoch, 每輪刷新; o 的 source-3 跨節點權威
HB_STALE=180                              # 心跳 > 180s 視為擁有者已死, 可奪鎖
_hb_age()  { local ts; ts=$(cut -d: -f3 "$HEARTBEAT" 2>/dev/null); [ -n "${ts:-}" ] && echo $(( $(date +%s) - ts )) || echo 999999; }
_hb_host() { cut -d: -f1 "$HEARTBEAT" 2>/dev/null; }
_write_hb(){ printf '%s:%s:%s\n' "$MYHOST" "$$" "$(date +%s)" > "$HEARTBEAT" 2>/dev/null || true; }
_take()    { rm -rf "$NODELOCK" 2>/dev/null; mkdir "$NODELOCK" 2>/dev/null && { echo "$MYHOST:$$" > "$NODELOCK/owner" 2>/dev/null; _write_hb; }; return 0; }
_claim_lock() {   # 0=取得鎖(可工作); 1=他節點活躍擁有→退讓
    if mkdir "$NODELOCK" 2>/dev/null; then echo "$MYHOST:$$" > "$NODELOCK/owner" 2>/dev/null; _write_hb; return 0; fi
    local oh age; oh=$(_hb_host); age=$(_hb_age)
    [ "${oh:-}" = "$MYHOST" ] && { _take; return 0; }   # 本節點殘留鎖 → 奪回
    [ "$age" -lt "$HB_STALE" ] && return 1               # 他節點心跳新鮮 → 退讓
    _take; return 0                                       # 他節點心跳過期(已死)→ 奪鎖; 失敗也 fail-open
}

# 清本節點自己殘留的 stale marker(kill -9 時 run_convergence 的 rm 沒跑到 → 洩漏)
find "$LIVE_DIR" -maxdepth 1 \( -name '.conv.marker.*' -o -name '.bench.marker.*' -o -name '.tauwall.marker.*' \) ! -name "*.$$" -mmin +2 -delete 2>/dev/null || true

# node-local 防重(同節點)
if [[ -f "$PID_FILE" ]]; then
    old_pid=$(cat "$PID_FILE" 2>/dev/null || true)
    if [[ -n "${old_pid:-}" && "$old_pid" != "$$" ]] && kill -0 "$old_pid" 2>/dev/null; then
        log "same-node watcher already running (pid=$old_pid), refusing"
        exit 0
    fi
fi

# 跨節點單例: 他節點活躍擁有鎖 → 本節點退讓(exit 0)
if ! _claim_lock; then
    log "another login node ($(_hb_host)) owns watcher lock (hb age $(_hb_age)s); deferring on $MYHOST"
    exit 0
fi
echo "$$" > "$PID_FILE"
log "watcher started on $MYHOST (pid=$$), holds cross-node lock"
# EXIT: 清 pid + 釋放鎖(僅當擁有者仍是自己, 避免刪到他節點奪走的鎖)+ 清自己的 marker
trap '
  rm -f "$PID_FILE";
  [ "$(cut -d: -f1 "$HEARTBEAT" 2>/dev/null)" = "$MYHOST" ] && rm -rf "$NODELOCK" 2>/dev/null;
  find "$LIVE_DIR" -maxdepth 1 -name ".*.marker.$$" -delete 2>/dev/null;
  log "watcher exiting (pid=$$) on $MYHOST"
' EXIT

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

# [2026-06-18] 最新 step (給每輪收斂圖刷新用; 純 log 訊息標籤, 不依賴 VTK)
get_latest_step() {
    local slurm_log
    slurm_log=$(ls -t "$PROJECT_DIR"/slurm_*.log 2>/dev/null | head -1)
    [[ -n "$slurm_log" ]] || { echo "0"; return; }
    grep -oP 'Step=\K[0-9]+' "$slurm_log" | tail -1 || echo "0"
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
# 跨節點 benchmark 去重: 用共享檔案標記「哪個 step 已跑過 benchmark」, 取代舊的
# per-instance in-memory 變數(owner 換節點即歸零 → 同一 VTK 被重複解析+重複 commit)。
BENCH_MARK="$LIVE_DIR/.last_bench_step"

while :; do
    _write_hb                      # 刷新跨節點心跳(維持本節點對 watcher 鎖的擁有權)
    # 每輪刷新 checklist.txt(daemon/chain 狀態檔即時清單); 唯讀掃描, 失敗不影響主循環。
    # 非零退出 = 非預期缺漏或產生器錯誤 → 只記一行警告供巡檢, 不中斷 watcher。
    python3 "$PROJECT_DIR/checklist.py" >/dev/null 2>&1 \
        || log "checklist: 產生器非零退出(非預期缺漏或錯誤, 詳見 checklist.txt)"
    RE=$(_read_re)

    if ! check_nan_divergence; then
        log "ALERT: simulation may be diverging — check slurm log immediately"
    fi

    # [2026-06-18] 收斂圖每輪刷新(每 POLL_SEC, login-node, 與計算效率無關): 4.Ma_U_Time.py
    #   只讀 records(Ustar_Force_record/checkrho/timing_log), 不依賴 VTK → settle 期(VTK
    #   gated FTT>=FTT_STATS_START)也即時更新 monitor_latest.png。benchmark 仍綁 1FTT stats-VTK(下方)。
    run_convergence "$(get_latest_step)" || true

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

            # (收斂圖已移至每輪刷新, 見上方 run_convergence; 此處只跑 benchmark/tauwall — 綁 1FTT stats-VTK)
            # BENCH gate (G2): FTT >= FTT_STATS_START + CV_WINDOW_FTT
            # — only fire benchmark figures once CV window has filled,
            #   otherwise RS fields are too noisy (statistics not yet meaningful).
            bench_gate=$(get_bench_gate_ftt)
            if awk -v f="$ftt" -v g="$bench_gate" 'BEGIN{exit !(f>=g && g>0)}'; then
                # 跨節點去重: 讀共享標記; 此 step 已被任一 owner 跑過 → 跳過(不重複解析/commit)
                done_bench_step=$(cat "$BENCH_MARK" 2>/dev/null || echo "")
                if [[ "$done_bench_step" != "$step" ]]; then
                    # 跑之前先 atomic 搶占標記, 讓併跑的他節點 owner 立刻看到 → 跳過(關併跑窗口)。
                    # 搶占在跑之前: 某 step benchmark 失敗(rc=137)不會被重試, 由下一個 VTK 補上(圖不 stale)。
                    printf '%s\n' "$step" > "$BENCH_MARK.tmp.$$" && mv -f "$BENCH_MARK.tmp.$$" "$BENCH_MARK"
                    log "BENCH trigger: FTT=$ftt >= G2=$bench_gate (accu=$accu)"
                    run_benchmark "$step" || true
                    run_tauwall "$step" || true
                    # 比照 Edit11: 每次 benchmark 圖刷新後, 單獨 commit+push 8 張比對圖
                    # (session-independent — 不依賴 Claude /loop, 當機/限流都照推)
                    bash "$PROJECT_DIR/watcher/push_benchmark_figs.sh" "$PROJECT_DIR" "$RE" || true
                fi
            else
                log "BENCH skipped: FTT=$ftt < G2=$bench_gate (accu=$accu, CV window not full)"
            fi

            last_processed="$vtk"
        fi
    fi
    sleep "$POLL_SEC"
done
