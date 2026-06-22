#!/usr/bin/env bash
# hill_watcher.sh — Periodic Hill Re5600 watcher loop
set -u

_SELF="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
_SELF_ABS="$SCRIPT_DIR/$(basename "$_SELF")"   # 絕對自身路徑(供 RESTART_WATCHER 哨兵 re-exec 重讀腳本)
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
CONV_TIMEOUT=600   # 4.Ma_U_Time.py 單獨 ~7s; 拉高(180→600)防 login node 競爭/dat 大檔下逾時(完整解析優先)
BENCH_TIMEOUT=900  # float32 --lowmem benchmark 含 ~35GB VTK 完整性掃描+parse ~552s; 拉高(300→900)防 FTT≥G2 那次逾時出不了圖
CHECKLIST_TIMEOUT=60  # checklist.py 正常 ~0.16s; 包 timeout 防 NFS 卡住把 top-hb→conv-hb 空窗拉成無界(否則破壞 HB_STALE 不變量)
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
HB_STALE=1200                             # 心跳 > 此值視為擁有者已死、可奪鎖。★必須 > 單輪「相鄰兩次 _write_hb
                                          #   之間」最長空窗;否則該長 op 期間 heartbeat 假性過期 → 別節點錯誤
                                          #   奪鎖 → 真 watcher 被 self-evict 誤殺。實際 binding op = run_tauwall
                                          #   (≤BENCH_TIMEOUT=900s)到下一輪頂端,故迴圈在 conv/benchmark/tauwall
                                          #   前後都補 _write_hb,把單次 staleness 壓在「一個 op + sleep」內
                                          #   (≈900+POLL_SEC=930s),1200 對此有 ~270s 裕度。
# 不變量守門:相鄰心跳間隔必須 < HB_STALE。最長單 op = BENCH_TIMEOUT(benchmark/tauwall 共用)+ POLL_SEC sleep。
# 鎖死此關係, 防日後有人調高 timeout 卻忘了同步 HB_STALE → 靜默破壞「健康者不被誤判死」。
(( HB_STALE > BENCH_TIMEOUT + POLL_SEC )) || { log "FATAL: HB_STALE($HB_STALE) 必須 > BENCH_TIMEOUT($BENCH_TIMEOUT)+POLL_SEC($POLL_SEC)"; exit 1; }
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

    # --lowmem: watcher inline 在 login node 跑,用 float32 省記憶體(35GB VTK 撞 20GB cgroup)。
    # 監控圖捨入 ~6e-8 可忽略。手動 canonical(零誤差 float64)走 lbm-plot-benchmark → dev CPU job。
    capture=$(cd "$RESULT_DIR" && timeout "$BENCH_TIMEOUT" python3 "$BENCH_SCRIPT" \
        --Re "$RE" --no-ask-scales --no-ask-density --lowmem 2>&1)
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

    # --lowmem: 同 run_benchmark,float32 + 跳未用欄(保 P_mean 供 cp)防 35GB VTK 撞 20GB cgroup OOM(rc=137)。
    # cf/cp 捨入 ~1e-4(含 metric;<<5% 比對精度)。canonical 零誤差走 dev job(不帶 --lowmem,float64)。
    capture=$(cd "$RESULT_DIR" && timeout "$BENCH_TIMEOUT" python3 "$TAUWALL_SCRIPT" \
        --Re "$RE" --auto --lowmem 2>&1)
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
# ★去重:last_bench_step 改為跨 owner 共享檔 $LIVE_DIR/.last_bench_step(見下方 BENCH gate),
#   不再用 in-memory 變數 —— 否則重啟/換 owner 時歸零 → 同一 35GB VTK 重複跑 benchmark
#   + 重複 push 比對圖(實證:9 step 重複跑、step 50254100 重複 commit、rc=137 併跑)。
_displaced_strikes=0       # self-eviction debounce:連續幾輪偵測到 nodelock 已被別人接管

while :; do
    # ── 殭屍自我巡邏 self-eviction(跨節點, 免 SSH/2FA)─────────────────────────────────
    #    nodelock/owner 檔是「誰是合法持鎖者」的單一真相(只在 _claim_lock/_take 寫, 非每輪刷)。
    #    我若不再是 owner = 已被新 watcher 接管 → 我是被取代的殭屍 → 優雅自殺。任何被取代的 watcher
    #    都會在 ~2 個 poll 週期內自動消失, 無需跨節點 kill(共享 home FS 即可, 免 SSH/2FA)。
    #    · owner 為空(_take 的 rm→mkdir 瞬間)→ -n 判空跳過, 不誤判。
    #    · debounce 2 輪 → 防 NFS 半寫/暫態誤觸。
    #    · ★必須 `trap - EXIT`:預設 EXIT trap 會在 heartbeat host==MYHOST 時 rm NODELOCK + rm PID_FILE,
    #      但此刻那是「接管者的合法鎖 / 合法 pid 檔」, 殭屍絕不可刪 → 清掉 trap 只默默退出 + 清自己 marker。
    _lock_owner=$(cat "$NODELOCK/owner" 2>/dev/null || true)
    if [[ -n "${_lock_owner:-}" && "$_lock_owner" != "$MYHOST:$$" ]]; then
        _displaced_strikes=$(( _displaced_strikes + 1 ))
        if [[ "$_displaced_strikes" -ge 2 ]]; then
            log "SELF-EVICT: nodelock owner=$_lock_owner != me=$MYHOST:$$ (displaced 2 cycles) → zombie self-exit on $MYHOST"
            trap - EXIT
            find "$LIVE_DIR" -maxdepth 1 -name ".*.marker.$$" -delete 2>/dev/null || true
            exit 0
        fi
    else
        _displaced_strikes=0
    fi

    _write_hb                      # 刷新跨節點心跳(維持本節點對 watcher 鎖的擁有權)

    # ── 跨節點重啟/停止哨兵(任何登入節點 touch 即可,免互動 SSH)──────────────────
    #   RESTART_WATCHER → 原地 re-exec 重讀腳本(吃進程式碼變更, 如 --lowmem); 同 PID,
    #     startup 會在「同節點」自動重 claim 鎖(_take), 不製造跨節點重複實例。
    #   STOP_WATCHER    → graceful exit(trap 釋放鎖); 哨兵保留 → 防 keepalive 立即拉回,
    #     需手動 rm 才能 (重)啟。用法: `touch live/RESTART_WATCHER` 或 `touch live/STOP_WATCHER`。
    if [[ -f "$LIVE_DIR/RESTART_WATCHER" ]]; then
        rm -f "$LIVE_DIR/RESTART_WATCHER"          # 先消費哨兵, 避免 re-exec 後無限重啟
        log "RESTART_WATCHER → re-exec $_SELF_ABS on $MYHOST (reload script, same pid=$$, re-claim lock)"
        exec bash "$_SELF_ABS"
    fi
    if [[ -f "$LIVE_DIR/STOP_WATCHER" ]]; then
        log "STOP_WATCHER present → graceful exit on $MYHOST (rm live/STOP_WATCHER to allow (re)start)"
        exit 0
    fi
    # 每輪刷新 checklist.txt(daemon/chain 狀態檔即時清單); 唯讀掃描, 失敗不影響主循環。
    # 非零退出 = 非預期缺漏或產生器錯誤 → 只記一行警告供巡檢, 不中斷 watcher。
    timeout "$CHECKLIST_TIMEOUT" python3 "$PROJECT_DIR/checklist.py" >/dev/null 2>&1 \
        || log "checklist: 非零退出或逾時 >${CHECKLIST_TIMEOUT}s(非預期缺漏/錯誤/卡住, 詳見 checklist.txt)"
    RE=$(_read_re)

    if ! check_nan_divergence; then
        log "ALERT: simulation may be diverging — check slurm log immediately"
    fi

    # ── 收斂圖(monitor): 每輪(POLL_SEC=30s)從 time-series 渲染,不綁 VTK ──
    #    4.Ma_U_Time.py 讀 Ustar_Force_record/checkrho/timing_log(連續 time-series,與 VTK 頻率無關)。
    #    VTK 已降為每 1FTT(降 I/O),但收斂圖仍每 30s 更新 fresh。
    #    benchmark + tau_wall 才綁「新 VTK」(見下,需 VTK spatial fields)。
    ftt=$(get_latest_ftt)
    _write_hb                      # conv 前補心跳(run_convergence 可阻塞 ≤CONV_TIMEOUT=600s, 期間無法再刷)
    run_convergence "$ftt" || true

    vtk=$(pick_latest_vtk || true)
    if [[ -n "$vtk" && "$vtk" != "$last_processed" ]]; then
        if is_size_stable "$vtk"; then
            step=$(extract_step "$vtk")
            accu=$(get_accu_count)
            metrics=$(get_latest_metrics)

            log "──────────────────────────────────────"
            log "PROCESS step=$step  FTT=$ftt  accu=$accu"
            [[ -n "$metrics" ]] && log "  $metrics"

            # BENCH gate (G2): FTT >= FTT_STATS_START + CV_WINDOW_FTT
            # — only fire benchmark figures once CV window has filled,
            #   otherwise RS fields are too noisy (statistics not yet meaningful).
            bench_gate=$(get_bench_gate_ftt)
            if awk -v f="$ftt" -v g="$bench_gate" 'BEGIN{exit !(f>=g && g>0)}'; then
                # ★去重:用跨 owner 共享檔標記「此 step 已跑過 benchmark」。換 owner/重啟後
                #   新實例讀同一檔 → 已跑過就跳過,避免重複解析 35GB VTK + 重複 push。
                BENCH_MARK="$LIVE_DIR/.last_bench_step"
                done_bench_step=$(cat "$BENCH_MARK" 2>/dev/null || echo "")
                if [[ "$done_bench_step" != "$step" ]]; then
                    # ★跑之前先 atomic mv 搶占標記,讓併跑的他節點 owner 立刻看到 → 跳過(關併跑窗口)。
                    #   權衡:某 step benchmark 失敗(rc=137)不重試,由下一顆 VTK 補上(圖按 VTK 更新、不 stale)。
                    printf '%s\n' "$step" > "$BENCH_MARK.tmp.$$" 2>/dev/null && mv -f "$BENCH_MARK.tmp.$$" "$BENCH_MARK" 2>/dev/null || true
                    log "BENCH trigger: FTT=$ftt >= G2=$bench_gate (accu=$accu)"
                    _write_hb                  # benchmark 前補心跳(run_benchmark 阻塞 ≤BENCH_TIMEOUT=900s,
                                               #   期間無法再刷;HB_STALE=1200 > 900 故不會被誤判死)
                    run_benchmark "$step" || true
                    _write_hb                  # benchmark 後補心跳
                    run_tauwall "$step" || true
                    _write_hb                  # ★tauwall 後補心跳:tauwall→下一輪頂端才是真正最長空窗, 這裡是關鍵
                    # 自主推送 benchmark 圖到遠端(免 Claude/session;plumbing 不碰 dirty 工作樹)
                    bash "$PROJECT_DIR/chain_code/push_benchmark_figs.sh" "$step" || true
                    _write_hb                  # push 後補心跳(git fetch+push ~數秒)
                fi
            else
                log "BENCH skipped: FTT=$ftt < G2=$bench_gate (accu=$accu, CV window not full)"
            fi

            last_processed="$vtk"
        fi
    fi
    sleep "$POLL_SEC"
done
