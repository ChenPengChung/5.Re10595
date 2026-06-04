#!/usr/bin/env bash
# ==============================================================================
# chain_code/tools/daemon_keepalive.sh — a.out-GATED keepalive (dispatcher + watcher)
# ------------------------------------------------------------------------------
# LIFECYCLE (the gate is the solver binary's existence):
#
#   BIRTH : dispatcher_start.sh / hill_watcher_start.sh install a */5 cron → this
#           script. (i.e. the keepalive begins the first time the user starts the
#           dispatcher OR the watcher.)
#
#   ALIVE : while a solver binary exists (a.out | a.out.H200 | a.out.GB200) →
#           auto-heal: revive the watcher (→ live/) and, in dispatcher mode
#           (DISPATCHER_INTENT present), the dispatcher (→ restart/) whenever their
#           shared-FS heartbeat goes stale.
#
#   DEATH : when NO solver binary exists (user ran lbm-clean / reset / torn down) →
#           HARD DEATH + SELF-DESTRUCT: actively SIGTERM→SIGKILL THIS project's
#           dispatcher + watcher (same-node, cwd + argv verified), clean sentinels
#           (without resurrecting restart/ or live/), and self-remove this cron.
#           The dispatcher and watcher ALSO carry a per-loop binary self-exit gate,
#           so a daemon on a different login node (which this same-node kill cannot
#           signal) exits on its own next loop.
#
# FALSE-DEATH guard: a `./run` build can leave all binaries momentarily absent. Every
#   death decision is skipped while run.sh holds the .run.lock flock
#   (keepalive_build_in_progress), and re-confirmed after the SIGTERM grace window.
#
# Safety:
#   - single-instance: a non-blocking flock (fd 8) means overlapping */5 ticks never
#     double-kill or double-mutate the crontab;
#   - liveness via shared-FS heartbeat mtime, NOT kill -0 (cron may run on a different
#     login node than the daemon);
#   - a kill targets a PID only if cwd == PROJECT_ROOT AND it is actually RUNNING the
#     daemon script (argv[0] a shell, argv[1] basename == the script) — never a mere
#     editor/tail/grep that happens to mention the path;
#   - cron ops touch ONLY this project's own line (grep -vF on the engine path);
#   - logs to a flat keepalive.log at PROJECT_ROOT — never under restart/ or live/, so
#     a DEATH pass never resurrects those dirs.
# ==============================================================================
set -u

_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
TOOLS_DIR="$(cd "$(dirname "$_SELF")" && pwd)" || exit 1
PROJECT_ROOT="$(cd "$TOOLS_DIR/../.." && pwd)" || exit 1   # tools/ → chain_code/ → ROOT
cd "$PROJECT_ROOT" || exit 1

# shellcheck source=keepalive_cron_lib.sh
. "$TOOLS_DIR/keepalive_cron_lib.sh"

KLOG="keepalive.log"   # flat root log; decoupled from live//restart/ so DEATH stays clean
_log() { echo "[$(date '+%F %T')] [keepalive] $*" >> "$KLOG" 2>/dev/null; }

# ── single-instance: skip this tick if a previous one is still running (no overlap) ──
exec 8>".keepalive.lock"
if command -v flock >/dev/null 2>&1 && ! flock -n 8; then
    exit 0
fi

# Is PID an actual run of daemon script $2 (NOT a process whose argv merely mentions it,
# e.g. `vim submit_dispatcher.sh` / `tail -f hill_watcher.sh`)? Requires cwd==PROJECT_ROOT
# (cross-project guard) AND argv[0] a shell AND argv[1] basename == the script.
_is_owned_daemon() {
    local pid="$1" script="$2" cwd a0 a1
    local -a _argv
    cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null)" || return 1
    [ "$cwd" = "$PROJECT_ROOT" ] || return 1
    mapfile -d '' -t _argv < "/proc/$pid/cmdline" 2>/dev/null || return 1
    a0="${_argv[0]:-}"; a1="${_argv[1]:-}"
    case "${a0##*/}" in bash|sh|dash) ;; *) return 1 ;; esac
    [ "${a1##*/}" = "$script" ]
}
_signal_owned() {   # $1=script  $2=signal
    local script="$1" sig="$2" pid
    command -v pgrep >/dev/null 2>&1 || return 0
    for pid in $(pgrep -u "$(id -u)" -f "$script" 2>/dev/null); do
        _is_owned_daemon "$pid" "$script" || continue
        _log "DEATH: ${sig} $script PID=$pid"
        kill "-$sig" "$pid" 2>/dev/null || true
    done
}

# ════════════════════════════════════════════════════════════════════════════
# DEATH GATE: no solver binary → hard death + self-destruct
# ════════════════════════════════════════════════════════════════════════════
if ! keepalive_solver_binary_present; then
    # FALSE-DEATH guard: a ./run build (holds .run.lock flock) can leave binaries
    # transiently all-absent. Do NOT tear down mid-build.
    if keepalive_build_in_progress; then
        _log "DEATH GATE: binary 暫缺但 .run.lock 被佔用 (./run build 進行中) → 跳過, 待下輪"
        exit 0
    fi
    _log "DEATH GATE: 無 solver binary (a.out/.H200/.GB200) → 終止 dispatcher+watcher, 移除 cron"
    # 1) SIGTERM this project's dispatcher + watcher (same-node, cwd + argv verified)
    _signal_owned "submit_dispatcher.sh" TERM
    _signal_owned "hill_watcher.sh"      TERM
    sleep 2
    # TOCTOU re-check: if a build started and a binary reappeared during the grace
    # window, ABORT the irreversible teardown — leave the cron so a later tick revives.
    if keepalive_solver_binary_present || keepalive_build_in_progress; then
        _log "DEATH GATE: solver binary 在終止過程中重新出現/build 開始 → 中止 teardown (保留 cron)"
        exit 0
    fi
    # 2) SIGKILL stragglers
    _signal_owned "submit_dispatcher.sh" KILL
    _signal_owned "hill_watcher.sh"      KILL
    # 3) clean sentinels WITHOUT recreating restart/ or live/
    rm -f DISPATCHER_ACTIVE STOP_DISPATCHER 2>/dev/null || true
    [ -d restart ] && rm -f restart/DISPATCHER_INTENT restart/dispatcher.heartbeat 2>/dev/null || true
    [ -d live ]    && rm -f live/watcher.pid live/watcher.heartbeat 2>/dev/null || true
    [ -d live/watcher.lock.d ] && rmdir live/watcher.lock.d 2>/dev/null || true
    # 4) self-destruct: remove THIS project's keepalive cron
    keepalive_cron_remove
    _log "DEATH: 完成 — daemons 已死, cron 已移除 (需手動 rebuild + ./run dispatcher start 才復活)"
    exit 0
fi

# ════════════════════════════════════════════════════════════════════════════
# ALIVE: solver binary exists → revive on demand
# ════════════════════════════════════════════════════════════════════════════
# Soft pause: user explicitly stopped the whole chain → do NOT revive (cron stays, so
# revival auto-resumes once STOP_CHAIN is cleared). Distinct from DEATH.
[ -f restart/STOP_CHAIN ] && exit 0

# ── watcher auto-heal (independent of dispatcher mode; chain alive ⇒ keep watcher) ──
if [ -x watcher/hill_watcher_start.sh ]; then
    _hbf="live/watcher.heartbeat"; _hbage=999999
    [ -f "$_hbf" ] && _hbage=$(( $(date +%s) - $(stat -c %Y "$_hbf" 2>/dev/null || echo 0) ))
    if [ "$_hbage" -ge "${WATCHER_HB_STALE:-300}" ]; then
        _log "WATCHDOG: watcher heartbeat stale ${_hbage}s → 經 hill_watcher_start.sh 重啟 (auto-heal)"
        # 8>&- : close the single-instance lock fd in the child, else the spawned watcher
        # inherits fd 8 and holds .keepalive.lock for its whole life → blocks every future
        # keepalive tick (incl. the death tick). MUST close it on every daemon spawn.
        bash watcher/hill_watcher_start.sh >> "$KLOG" 2>&1 8>&- || true
    fi
fi

# ── dispatcher auto-heal (ONLY in dispatcher mode = DISPATCHER_INTENT present) ──
[ -f STOP_DISPATCHER ]           && exit 0   # dispatcher being gracefully stopped
[ -f restart/DISPATCHER_INTENT ] || exit 0   # not in dispatcher mode → done (watcher handled)
[ -f restart/STOP_NOCAPACITY ]   && exit 0   # launcher refuses to restart while this is set → don't spin
# dispatcher_start.sh requires an ARCH binary (a.out.H200/.GB200), not bare a.out — match
# its precondition, else an only-bare-a.out state would retry an unsatisfiable revive forever.
[ -s a.out.H200 ] || [ -s a.out.GB200 ] || { _log "WATCHDOG: 只有裸 a.out, 無 arch binary → 不試 dispatcher revive (launcher 會拒)"; exit 0; }

HB_STALE="${KEEPALIVE_HB_STALE:-300}"        # >this ⇒ daemon dead/hung (300s ≫ ~35s restart gap)
age=999999
[ -f restart/dispatcher.heartbeat ] && age=$(( $(date +%s) - $(stat -c %Y restart/dispatcher.heartbeat 2>/dev/null || echo 0) ))
[ "$age" -le "$HB_STALE" ] && exit 0          # dispatcher healthy → nothing to do

_log "WATCHDOG: DISPATCHER_INTENT 在但 heartbeat stale ${age}s (>$HB_STALE) → dispatcher 死/hung, 救回中"
# Kill a same-node hung dispatcher first (else dispatcher_start dup-guard blocks restart).
_pid=""; [ -f DISPATCHER_ACTIVE ] && _pid="$(tr -dc 0-9 < DISPATCHER_ACTIVE 2>/dev/null)"
if [ -n "$_pid" ] && kill -0 "$_pid" 2>/dev/null && _is_owned_daemon "$_pid" "submit_dispatcher.sh"; then
    _log "  hung dispatcher PID=$_pid (本專案) → kill"
    kill "$_pid" 2>/dev/null || true; sleep 2; kill -9 "$_pid" 2>/dev/null || true
fi
rm -f DISPATCHER_ACTIVE 2>/dev/null || true
# 8>&- : close the single-instance lock fd in the child (see watcher spawn above) so the
# spawned dispatcher daemon does not inherit fd 8 and hold .keepalive.lock for its lifetime.
bash chain_code/dispatcher_start.sh >> "$KLOG" 2>&1 8>&-
_log "WATCHDOG: dispatcher_start 已呼叫 → DISPATCHER_ACTIVE=$(cat DISPATCHER_ACTIVE 2>/dev/null || echo '?')"
