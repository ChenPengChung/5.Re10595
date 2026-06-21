#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# handoff_scout.sh — Dispatcher "first soldier": READ-ONLY chain-handoff scout.
#
# WHAT IT DOES (observe + alert only):
#   1. Auto-detects the APPROACHING walltime handoff (LEFT = EndTime - now).
#   2. Watches whether the NEXT round GRABS a slot (submit->start latency,
#      PENDING reason classification), WorkDir-verified to THIS project.
#   3. Detects the genuinely costly failure: a SILENT DEAD CHAIN (current head
#      terminal but no successor written after a grace window).
#
# WHAT IT NEVER DOES (hard rules — do not weaken):
#   * NO sbatch / scancel / scontrol-mutate / srun  (never a 3rd submitter)
#   * NO writes to restart/ control state (chain_jobid, chain_count,
#     HEAD.lockdir/*, DISPATCHER_ACTIVE, STOP_*)
#   * NO git commit/add
#   * NO action on sibling projects' jobs (verify WorkDir, then ignore)
#   Only side effect: append a status line to live/handoff_scout.log (untracked).
#
# Submission resilience is ALREADY covered by two mutually-exclusive submitters
# (compute-node jobscript self-submit  +  login-node dispatcher daemon), de-duped
# by restart/HEAD.lockdir + verify_am_head. This scout adds VISIBILITY + a loud
# tripwire, nothing more.
#
# EXIT CODES (severity, for a Monitor to key on):
#   0  OK        head RUNNING, not near walltime (or chain intentionally STOPPED)
#   10 ARMED     head RUNNING, LEFT <= ARM_SEC (handoff approaching)
#   20 HANDOFF   round changing over (old gone <grace, or successor PENDING<warn,
#                or chain_jobid placeholder) — transient, expected
#   30 WARN      successor PENDING longer than WARN_SEC (classify reason)
#   40 CRITICAL  dead chain (head terminal, no successor after grace) /
#                STOP_NOCAPACITY / >1 live head for this project (lock failure)
#   2  USAGE/ENV error
#
# Read-only: uses only cat / squeue / sacct / scontrol show.
# ─────────────────────────────────────────────────────────────────────────────
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
R="$PROJECT_DIR/restart"
LOG="$PROJECT_DIR/live/handoff_scout.log"
ME="$(whoami)"

# Tunables (env-overridable; pure thresholds, no behaviour beyond alerting)
ARM_SEC="${SCOUT_ARM_SEC:-900}"        # <=15min to EndTime -> ARMED
DEAD_GRACE_SEC="${SCOUT_DEAD_GRACE:-180}"   # head terminal & no successor this long -> CRITICAL
WARN_SEC="${SCOUT_WARN_SEC:-600}"      # successor PENDING this long -> WARN/ALERT
QUIET="${SCOUT_QUIET:-0}"              # 1 = stdout only, no logfile append

now="$(date +%s)"
ts="$(date '+%Y-%m-%d %H:%M:%S')"

emit() {  # $1=severity tag  $2=message  ; prints + appends (unless QUIET)
  local line="[$ts] [$1] $2"
  printf '%s\n' "$line"
  if [ "$QUIET" != "1" ]; then
    mkdir -p "$PROJECT_DIR/live" 2>/dev/null
    printf '%s\n' "$line" >> "$LOG" 2>/dev/null || true
  fi
}

is_numeric() { case "$1" in (''|*[!0-9]*) return 1;; (*) return 0;; esac; }

# WorkDir ownership guard — never trust truncated job names across projects.
job_is_mine() {  # $1=jobid -> 0 if WorkDir == PROJECT_DIR
  local wd
  wd="$(scontrol show job "$1" 2>/dev/null | grep -oE 'WorkDir=[^ ]+' | head -1 | cut -d= -f2-)"
  [ -n "$wd" ] && [ "$wd" = "$PROJECT_DIR" ]
}

sacct_state() {  # $1=jobid -> first token of State, or empty
  sacct -X -n -j "$1" -o State 2>/dev/null | head -1 | awk '{print $1}'
}
sq_state()  { squeue -h -j "$1" -o '%T' 2>/dev/null | head -1; }
sq_reason() { squeue -h -j "$1" -o '%r' 2>/dev/null | head -1; }
epoch_of()  { [ -n "$1" ] && [ "$1" != "Unknown" ] && date -d "$1" +%s 2>/dev/null || echo ""; }

case "${1:-}" in
  -h|--help) sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0;;
esac

[ -d "$R" ] || { emit ENV "restart/ not found at $R"; exit 2; }

# Intentional stop?
if [ -f "$R/STOP_CHAIN" ]; then
  emit OK "STOP_CHAIN present -> chain intentionally stopped; not a fault."
  exit 0
fi
if [ -f "$R/STOP_NOCAPACITY" ]; then
  emit CRITICAL "restart/STOP_NOCAPACITY present -> dispatcher gave up on capacity. Human action needed."
  exit 40
fi

CUR="$(cat "$R/chain_jobid" 2>/dev/null | tr -d '[:space:]')"
CNT="$(cat "$R/chain_count" 2>/dev/null | tr -d '[:space:]')"

if [ -z "$CUR" ]; then
  emit HANDOFF "chain_jobid empty (submit in progress?)."
  exit 20
fi
if ! is_numeric "$CUR"; then
  emit HANDOFF "chain_jobid='$CUR' not numeric (placeholder; submit in progress)."
  exit 20
fi

# Ownership sanity (chain_jobid should always be ours)
if ! job_is_mine "$CUR"; then
  # scontrol may not know a freshly-terminated job; only flag if sacct also unknown
  st_chk="$(sacct_state "$CUR")"
  emit WARN "chain_jobid=$CUR WorkDir!=this project (or unknown). sacct=$st_chk. Verify manually."
fi

# Determine head state: prefer sacct (authoritative on NCHC federation), fall back to squeue
ST="$(sacct_state "$CUR")"
[ -z "$ST" ] && ST="$(sq_state "$CUR")"

# Double-submit tripwire: count THIS project's live heads
mine=0
for j in $(squeue -h -u "$ME" -t RUNNING,PENDING,CONFIGURING,COMPLETING -o '%i' 2>/dev/null); do
  job_is_mine "$j" && mine=$((mine+1))
done
DUP_NOTE=""
[ "$mine" -gt 1 ] && DUP_NOTE=" | DUP-WARN: $mine live heads for this project (HEAD.lockdir should forbid >1)"

case "$ST" in
  RUNNING|CONFIGURING|RESIZING|SUSPENDED)
    end="$(scontrol show job "$CUR" 2>/dev/null | grep -oE 'EndTime=[^ ]+' | head -1 | cut -d= -f2-)"
    ee="$(epoch_of "$end")"
    if [ -n "$ee" ]; then
      left=$(( ee - now )); lh=$(( left/3600 )); lm=$(( (left%3600)/60 ))
      if [ "$left" -le "$ARM_SEC" ]; then
        emit ARMED "round=$CNT head=$CUR RUNNING; ${lh}h${lm}m to EndTime ($end) -> handoff imminent.$DUP_NOTE"
        [ -n "$DUP_NOTE" ] && exit 40 || exit 10
      fi
      emit OK "round=$CNT head=$CUR RUNNING; ${lh}h${lm}m left (EndTime $end).$DUP_NOTE"
      [ -n "$DUP_NOTE" ] && exit 40 || exit 0
    fi
    emit OK "round=$CNT head=$CUR RUNNING (EndTime unknown).$DUP_NOTE"
    [ -n "$DUP_NOTE" ] && exit 40 || exit 0
    ;;

  PENDING)
    rsn="$(sq_reason "$CUR")"; [ -z "$rsn" ] && rsn="$(sacct -X -n -j "$CUR" -o Reason 2>/dev/null | head -1 | awk '{print $1}')"
    sub="$(sacct -X -n -j "$CUR" -o Submit 2>/dev/null | head -1 | awk '{print $1}')"
    se="$(epoch_of "$sub")"; page=0; [ -n "$se" ] && page=$(( now - se ))
    # classify reason
    cls="benign"
    case "$rsn" in
      QOS*|Assoc*Grp*GRES*|*GrpGRES*) cls="ACCOUNT-CAP";;
      ReqNodeNotAvail*|*DOWN*|Partition*|BeginTime) cls="NODE/MAINT";;
      None|Priority|Resources|"") cls="benign";;
      *) cls="other:$rsn";;
    esac
    if [ "$page" -ge "$WARN_SEC" ]; then
      emit WARN "round=$CNT successor=$CUR PENDING ${page}s (>=${WARN_SEC}s) reason=$rsn [$cls]. If self-submitted on 16gpus it can't reselect; dispatcher reselects after 10min if alive.$DUP_NOTE"
      exit 30
    fi
    emit HANDOFF "round=$CNT successor=$CUR PENDING ${page}s reason=$rsn [$cls] (grabbing).$DUP_NOTE"
    [ -n "$DUP_NOTE" ] && exit 40 || exit 20
    ;;

  COMPLETED|TIMEOUT|FAILED|CANCELLED*|NODE_FAIL|OUT_OF_MEMORY|PREEMPTED|DEADLINE|BOOT_FAIL)
    # Head terminal. A successor should appear (chain_jobid flips to a new id).
    # Since CUR still == chain_jobid, no successor has been written yet.
    end="$(sacct -X -n -j "$CUR" -o End 2>/dev/null | head -1 | awk '{print $1}')"
    ee="$(epoch_of "$end")"; age=0; [ -n "$ee" ] && age=$(( now - ee ))
    if [ -f "$R/STOP_CHAIN" ]; then
      emit OK "head=$CUR $ST and STOP_CHAIN present -> intentional stop."; exit 0
    fi
    if [ "$age" -le "$DEAD_GRACE_SEC" ]; then
      emit HANDOFF "head=$CUR just ended ($ST, ${age}s ago); awaiting successor (grace ${DEAD_GRACE_SEC}s).$DUP_NOTE"
      exit 20
    fi
    emit CRITICAL "DEAD CHAIN? head=$CUR $ST ${age}s ago, chain_jobid still=$CUR (no successor) & no STOP_CHAIN. Both submitters may have failed. Check: jobscript Section-7 / systemd edit11-dispatcher / sacct lineage.$DUP_NOTE"
    exit 40
    ;;

  "")
    emit HANDOFF "head=$CUR not in sacct/squeue yet (reporting lag); re-check shortly.$DUP_NOTE"
    exit 20
    ;;
  *)
    emit WARN "head=$CUR unexpected state='$ST'.$DUP_NOTE"
    exit 30
    ;;
esac
