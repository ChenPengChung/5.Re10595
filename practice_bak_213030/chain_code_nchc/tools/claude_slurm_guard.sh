#!/bin/bash
# ==============================================================================
# claude_slurm_guard.sh — Claude Code PreToolUse hook for Bash tool
# ==============================================================================
# Blocks dangerous SLURM commands that could affect other projects' jobs.
#
# BLOCKED:
#   - bare `scancel` (must go through ./run job-guard scancel)
#   - `scontrol update/hold/release/requeue/suspend/resume` on arbitrary jobs
#
# ALLOWED:
#   - `scancel` via job-guard path (project_job_guard.sh / ./run job-guard)
#   - read-only `scontrol show`, `scontrol listpids`
#   - `squeue`, `sinfo`, `sacct` (read-only queries)
#   - all non-SLURM commands
#
# Hook input: JSON on stdin with tool_input.command
# Exit 0 = allow, non-zero = block (stderr shown to Claude)
# ==============================================================================

set -uo pipefail

INPUT="$(cat 2>/dev/null || true)"
[ -z "$INPUT" ] && exit 0

CMD="$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    inp = d.get('tool_input', d)
    print(inp.get('command', ''))
except:
    print('')
" 2>/dev/null || true)"

[ -z "$CMD" ] && exit 0

# ── scancel guard ──
if echo "$CMD" | grep -qwi 'scancel'; then
    if echo "$CMD" | grep -qE '(job.guard|project_job_guard).*scancel'; then
        exit 0
    fi
    if echo "$CMD" | grep -qE '\./run\s+job-guard\s+scancel'; then
        exit 0
    fi
    cat >&2 <<'EOF'
[SLURM GUARD] BLOCKED: bare 'scancel' is forbidden.
To cancel a job that belongs to THIS project only:
  ./run job-guard scancel <jobid>
This ensures the jobid is verified against this project's restart state
before any cancellation occurs.
EOF
    exit 1
fi

# ── scontrol modification guard ──
# Check modifying subcommands FIRST — a compound command like
# "scontrol show job 123; scontrol hold 456" must be caught.
if echo "$CMD" | grep -qwi 'scontrol'; then
    if echo "$CMD" | grep -qiE 'scontrol\s+(update|hold|release|requeue|cancel|suspend|resume)'; then
        cat >&2 <<'EOF'
[SLURM GUARD] BLOCKED: modifying 'scontrol' command is forbidden.
Only read-only scontrol subcommands (show, listpids) are allowed.
To stop this project's chain: ./run job-guard stop-chain
To cancel this project's job: ./run job-guard scancel <jobid>
EOF
        exit 1
    fi
fi

exit 0
