#!/bin/bash
# ==============================================================================
# project_job_guard.sh
# ------------------------------------------------------------------------------
# Project-local job safety guard.
#
# This tool refuses to operate on any SLURM job unless the job id is recorded in
# this project folder's restart state.  It is intended to prevent accidental
# scancel / stop actions from touching jobs that belong to other projects.
#
# Allowed project-owned ids:
#   - restart/chain_jobid
#   - restart/HEAD.lockdir/owner: jobid=<id>
#   - restart/RUNNING.lockdir/owner: jobid=<id>   (legacy)
#
# Usage:
#   chain_code/tools/project_job_guard.sh list
#   chain_code/tools/project_job_guard.sh check <jobid>
#   chain_code/tools/project_job_guard.sh scancel <jobid>
#   chain_code/tools/project_job_guard.sh stop-chain
# ==============================================================================

set -euo pipefail

_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || realpath "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
TOOL_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
PROJECT_ROOT="$(cd "$TOOL_DIR/../.." && pwd)"
cd "$PROJECT_ROOT" || { echo "[job-guard] FATAL: cannot cd to $PROJECT_ROOT" >&2; exit 1; }

usage() {
    sed -n '2,21p' "$_SELF" | sed 's/^# //;s/^#$//'
}

_is_numeric_id() {
    [[ "${1:-}" =~ ^[0-9]+$ ]]
}

_read_owner_jobid() {
    local owner_file="$1"
    [ -f "$owner_file" ] || return 0
    grep '^jobid=' "$owner_file" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]' || true
}

project_job_ids() {
    local id

    if [ -f restart/chain_jobid ]; then
        id="$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]' || true)"
        _is_numeric_id "$id" && echo "$id"
    fi

    id="$(_read_owner_jobid restart/HEAD.lockdir/owner)"
    _is_numeric_id "$id" && echo "$id"

    id="$(_read_owner_jobid restart/RUNNING.lockdir/owner)"
    _is_numeric_id "$id" && echo "$id"
}

is_project_job() {
    local target="$1"
    local id
    while IFS= read -r id; do
        [ "$id" = "$target" ] && return 0
    done < <(project_job_ids | sort -u)
    return 1
}

show_project_jobs() {
    echo "[job-guard] project root: $PROJECT_ROOT"
    echo "[job-guard] project-owned job ids:"
    local any=0 id
    while IFS= read -r id; do
        any=1
        if command -v squeue >/dev/null 2>&1; then
            local line
            line="$(squeue -h -j "$id" -o '%i %j %T %M %R' 2>/dev/null | head -1 || true)"
            if [ -n "$line" ]; then
                echo "  $line"
            else
                echo "  $id (not in squeue)"
            fi
        else
            echo "  $id"
        fi
    done < <(project_job_ids | sort -u)
    [ "$any" -eq 1 ] || echo "  <none>"
}

cmd="${1:-help}"
shift || true

case "$cmd" in
    list)
        show_project_jobs
        ;;

    check)
        jobid="${1:-}"
        if ! _is_numeric_id "$jobid"; then
            echo "[job-guard] FATAL: check requires numeric jobid" >&2
            exit 2
        fi
        if is_project_job "$jobid"; then
            echo "[job-guard] OK: jobid=$jobid belongs to this project"
            exit 0
        fi
        echo "[job-guard] REFUSE: jobid=$jobid is not recorded in this project state" >&2
        show_project_jobs >&2
        exit 10
        ;;

    scancel)
        jobid="${1:-}"
        if ! _is_numeric_id "$jobid"; then
            echo "[job-guard] FATAL: scancel requires numeric jobid" >&2
            exit 2
        fi
        if ! is_project_job "$jobid"; then
            echo "[job-guard] REFUSE: not cancelling non-project jobid=$jobid" >&2
            show_project_jobs >&2
            exit 10
        fi
        echo "[job-guard] scancel allowed for project jobid=$jobid"
        scancel "$jobid"
        ;;

    stop-chain)
        mkdir -p restart
        touch restart/STOP_CHAIN
        echo "[job-guard] Created restart/STOP_CHAIN for this project only."
        echo "[job-guard] This does not cancel or modify other projects' jobs."
        ;;

    help|-h|--help|"")
        usage
        ;;

    *)
        echo "[job-guard] unknown command: $cmd" >&2
        usage >&2
        exit 2
        ;;
esac
