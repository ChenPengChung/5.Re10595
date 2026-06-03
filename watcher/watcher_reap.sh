#!/usr/bin/env bash
# ==============================================================================
# watcher/watcher_reap.sh — safely reap THIS project's stray watcher processes
#                            on the CURRENT login node only.
# ------------------------------------------------------------------------------
# CROSS-PROJECT KILL BOUNDARY (the whole point of this script):
#   A process is killed ONLY if BOTH conditions hold:
#     (a) /proc/<pid>/cmdline contains THIS project's watcher/hill_watcher.sh path
#     (b) /proc/<pid>/cwd == this PROJECT_DIR
#   It NEVER kills by a bare PID taken from live/watcher.pid — that file can hold
#   a PID belonging to a watcher on ANOTHER login node, which on this node may be
#   an unrelated process (a different project, or a system process). Killing such
#   a PID blindly is exactly the cross-project accident this guard prevents.
#   Other projects' watchers (different path / cwd) are always skipped.
#
# Cannot see other login nodes: this only reaps LOCAL duplicates. A watcher on a
# different login node is left alone (it is harmless and self-clears via the
# heartbeat single-instance guard when it next restarts).
#
# Usage:
#   watcher/watcher_reap.sh           # de-dup: keep the single newest, reap the rest
#   watcher/watcher_reap.sh --all     # reap every matching watcher (full stop)
# ==============================================================================
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHER_PATH="$PROJECT_DIR/watcher/hill_watcher.sh"
HOST="$(hostname 2>/dev/null || echo '?')"
KEEP_ONE=1; [ "${1:-}" = "--all" ] && KEEP_ONE=0

is_ours() {  # $1=pid → 0 iff a verified THIS-project watcher
    local pid="$1" cmd cwd
    [ -r "/proc/$pid/cmdline" ] || return 1
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"
    cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null)"
    case "$cmd" in *"$WATCHER_PATH"*) : ;; *) return 1 ;; esac   # (a) cmdline path
    [ "$cwd" = "$PROJECT_DIR" ] || return 1                       # (b) cwd
    return 0
}

# Verified-ours PIDs on THIS node, ordered oldest → newest by /proc start mtime.
mapfile -t cands < <(
    for pid in $(pgrep -u "$(id -u)" -f "hill_watcher.sh" 2>/dev/null); do
        is_ours "$pid" && printf '%s %s\n' "$(stat -c %Y "/proc/$pid" 2>/dev/null || echo 0)" "$pid"
    done | sort -n | awk '{print $2}'
)
n=${#cands[@]}
if [ "$n" -eq 0 ]; then echo "[reap] $HOST: no this-project watcher running — nothing to do"; exit 0; fi

keep=""
[ "$KEEP_ONE" -eq 1 ] && keep="${cands[$((n-1))]}"   # keep newest
for p in "${cands[@]}"; do
    if [ "$p" = "$keep" ]; then echo "[reap] keep   pid=$p (verified, newest)"; continue; fi
    if kill "$p" 2>/dev/null; then echo "[reap] killed pid=$p (verified THIS-project watcher)"
    else echo "[reap] could not kill pid=$p (gone?)"; fi
done
if [ -n "$keep" ]; then echo "$keep" > "$PROJECT_DIR/live/watcher.pid"; echo "[reap] watcher.pid -> $keep"; fi
exit 0
