#!/usr/bin/env bash
# ==============================================================================
# chain_code/tools/keepalive_cron_lib.sh
# ------------------------------------------------------------------------------
# Shared helpers for the a.out-GATED keepalive lifecycle. SOURCED (not executed)
# by:
#   - chain_code/dispatcher_start.sh   (BIRTH: install cron on dispatcher start)
#   - watcher/hill_watcher_start.sh    (BIRTH: install cron on watcher start)
#   - chain_code/tools/daemon_keepalive.sh  (DEATH: self-remove cron when binary gone)
#
# Cross-project safety: every crontab mutation touches ONLY THIS project's own
# keepalive line, identified by the absolute daemon_keepalive.sh path, and uses
# `grep -vF` so every other crontab line (other subprojects, other monitors) is
# preserved byte-for-byte. This lib NEVER does `crontab -u`, `-r`, or batch ops.
#
# This file defines functions + a few KEEPALIVE_* vars only; it has NO top-level
# side effects (no cd, no crontab write) so it is safe to source under `set -e`.
# ==============================================================================

# Resolve paths from THIS lib's own location (tools/ holds the engine too), so the
# helpers are correct regardless of the sourcing script's CWD or location.
_kpl_self="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
KEEPALIVE_TOOLS_DIR="$(cd "$(dirname "$_kpl_self")" 2>/dev/null && pwd)"
KEEPALIVE_ENGINE="$KEEPALIVE_TOOLS_DIR/daemon_keepalive.sh"
KEEPALIVE_PROJECT_ROOT="$(cd "$KEEPALIVE_TOOLS_DIR/../.." 2>/dev/null && pwd)"
# Canonical */5 line. PATH pinned for cron's minimal env; engine self-logs, so the
# cron output itself is discarded.
KEEPALIVE_CRON_LINE="*/5 * * * * PATH=/usr/bin:/bin /usr/bin/bash $KEEPALIVE_ENGINE >/dev/null 2>&1"

# ── ALIVE/DEATH gate: true(0) iff at least one solver binary exists & is non-empty.
#    Death (return 1) = none of a.out / a.out.H200 / a.out.GB200 present.
keepalive_solver_binary_present() {
    [ -s "$KEEPALIVE_PROJECT_ROOT/a.out" ] \
        || [ -s "$KEEPALIVE_PROJECT_ROOT/a.out.H200" ] \
        || [ -s "$KEEPALIVE_PROJECT_ROOT/a.out.GB200" ]
}

# ── FALSE-DEATH guard: true(0) iff a `./run` build is in progress. run.sh holds an
#    flock on .run.lock for the ENTIRE compile+deploy (exec 200>.run.lock; flock -n 200),
#    during which a fresh build can leave a.out / a.out.H200 / a.out.GB200 momentarily
#    all-absent. A death gate firing in that window would falsely tear the project down,
#    so every death gate must skip while this returns true. (flock test uses a separate
#    read fd in a subshell → acquires+releases without disturbing any caller-held lock.)
keepalive_build_in_progress() {
    local lk="$KEEPALIVE_PROJECT_ROOT/.run.lock"
    [ -e "$lk" ] || return 1                          # no lock file → no build active
    command -v flock >/dev/null 2>&1 || return 1      # cannot test → assume not building
    ( flock -n 9 ) 9< "$lk" 2>/dev/null && return 1   # lock acquired → nobody holds it → not building
    return 0                                           # could NOT acquire → run.sh holds it → building
}

# ── BIRTH: idempotently install THIS project's */5 keepalive cron.
keepalive_cron_install() {
    command -v crontab >/dev/null 2>&1 || { echo "[keepalive] crontab 不可用, 跳過 cron 安裝" >&2; return 0; }
    if crontab -l 2>/dev/null | grep -qF "$KEEPALIVE_ENGINE"; then
        return 0   # already present → no-op (idempotent)
    fi
    if { crontab -l 2>/dev/null; echo "$KEEPALIVE_CRON_LINE"; } | crontab - 2>/dev/null; then
        echo "[keepalive] ✓ 已安裝 */5 keepalive cron → $KEEPALIVE_ENGINE"
    else
        echo "[keepalive] ⚠ 無法安裝 keepalive cron (crontab 寫入失敗)" >&2
    fi
}

# ── DEATH: remove ONLY this project's keepalive cron line (self-destruct).
keepalive_cron_remove() {
    command -v crontab >/dev/null 2>&1 || return 0
    crontab -l 2>/dev/null | grep -qF "$KEEPALIVE_ENGINE" || return 0   # nothing to remove
    if crontab -l 2>/dev/null | grep -vF "$KEEPALIVE_ENGINE" | crontab - 2>/dev/null; then
        echo "[keepalive] ✓ 已移除本專案 keepalive cron (self-destruct)"
    else
        echo "[keepalive] ⚠ 無法移除 keepalive cron (crontab 寫入失敗)" >&2
    fi
}
