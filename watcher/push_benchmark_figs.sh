#!/usr/bin/env bash
# push_benchmark_figs.sh — commit+push the 8 benchmark comparison figures as a
# standalone commit, Edit11-style: "更新 benchmark 比對圖 FTT-NN(step XXXXX)".
#
# Called by watcher/hill_watcher.sh after each successful benchmark+tauwall
# refresh, so the live benchmark figures are pushed to the remote on every
# update WITHOUT depending on a Claude /loop (survives Claude crash / API
# rate-limit). Session-independent: runs on the login node inside the watcher.
#
# Safety: only touches the 8 benchmark figs (never `git add -A`), orphan-lock
# guard (rm index.lock only if stale >120s AND no real git process), refuses to
# commit if a record file somehow staged, push is fast-forward only (never
# --force). ALWAYS exits 0 so it can never break the watcher loop.
#
# Args: $1=PROJECT_DIR (default: parent of this script's dir)
#       $2=Re          (default: 5600)
#       $3=SPF steps/FTT (default: 1179000)
set -u

PROJ="${1:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"
RE="${2:-5600}"
SPF="${3:-1179000}"

cd "$PROJ" 2>/dev/null || exit 0
command -v git >/dev/null 2>&1 || exit 0

log() { printf '[%s] [push_bench] %s\n' "$(date '+%F %T')" "$*"; }

FIGS="result/fig_mean_u.png result/fig_mean_v.png result/fig_uu.png result/fig_vv.png result/fig_uv.png result/fig_k.png result/tau_wall_signed_Re${RE}_cf.png result/tau_wall_signed_Re${RE}_cp.png"

# 1) only act when one of the 8 benchmark figs actually changed
CHG=$(git status --short -- $FIGS 2>/dev/null | grep -c .)
[ "${CHG:-0}" -gt 0 ] || { log "no benchmark fig change — skip"; exit 0; }

# 2) orphan-lock guard: remove .git/index.lock ONLY if it is stale (>120s, far
#    longer than committing 8 small PNGs takes) AND no real git process is alive.
#    Use `pgrep -x git` (matches process NAME, not cmdline) so this script's own
#    command string can't self-match.
if [ -f .git/index.lock ]; then
    LA=$(( $(date +%s) - $(stat -c %Y .git/index.lock 2>/dev/null || echo 0) ))
    if [ "$LA" -gt 120 ] && ! pgrep -u "$USER" -x git >/dev/null 2>&1; then
        rm -f .git/index.lock && log "removed orphan index.lock (age ${LA}s)"
    else
        log "git busy / lock fresh (age ${LA}s) — skip this round, retry next"
        exit 0
    fi
fi

# 3) stage exactly the 8 figs (NEVER -A)
git add $FIGS 2>/dev/null

# 4) belt-and-suspenders: refuse if any of the 3 record files got staged
if git diff --cached --name-only 2>/dev/null | grep -qE 'Ustar_Force_record|timing_log|checkrho'; then
    log "ABORT: a record file is staged — unstaging the 8 figs, no commit"
    git reset -q HEAD -- $FIGS 2>/dev/null
    exit 0
fi

# 5) nothing staged (e.g. auto-committer raced us) → fine, skip
[ -n "$(git diff --cached --name-only 2>/dev/null)" ] || { log "nothing staged (raced) — skip"; exit 0; }

# 6) message: FTT-NN(step <latest VTK step>) — the VTK the figs were computed on
VTK=$(ls -t result/velocity_merged_*.vtk 2>/dev/null | head -1 | grep -oE '[0-9]+' | head -1)
FTT=$(awk -v s="${VTK:-0}" -v p="$SPF" 'BEGIN{ printf "%d", (p>0)? s/p : 0 }')

if git commit -q -m "更新 benchmark 比對圖 FTT-${FTT}(step ${VTK:-NA})" 2>/dev/null; then
    log "committed FTT-${FTT} step ${VTK:-NA}"
else
    log "commit produced nothing — skip push"
    exit 0
fi

# 7) push fast-forward only; on non-ff / transient failure just report (never --force)
git fetch -q origin 2>/dev/null || true
if git push -q 2>/dev/null; then
    log "pushed OK → $(git rev-parse --short HEAD 2>/dev/null)"
else
    log "push failed (non-ff or transient) — committed locally, will sync next round"
fi

exit 0
