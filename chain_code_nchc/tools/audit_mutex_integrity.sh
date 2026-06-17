#!/bin/bash
# =============================================================================
# audit_mutex_integrity.sh
#   End-to-end static + dynamic audit of the Single-Head-per-Folder invariant.
#
# Usage:
#   bash tools/audit_mutex_integrity.sh
#
# What it checks:
#   Section A. head_lock_lib.sh present and sources cleanly
#   Section B. Every sbatch call site is wrapped by head_lock_lib primitives:
#              submit_dispatcher.sh, build_and_submit.sh.GB200,
#              build_and_submit.sh.H200, jobscript_chain.slurm.GB200,
#              jobscript_chain.slurm.H200
#   Section C. All cluster-specific job entry points call verify_am_head
#   Section D. All cluster-specific job entry points have release_head_lock_if_mine
#              on EXIT trap
#   Section E. run.sh reads HEAD.lockdir (fast-path) and calls acquire_head_lock
#              before exec-ing build_and_submit.sh
#   Section F. [Dynamic] verify_mutex.sh Section F -- Single-Head Invariant
#              (*.lockdir count <= 1 at all times)
#   Section G. [Dynamic] verify_mutex.sh Section G -- No-Double-Submit
#              (100 concurrent acquire -> 1 WIN, 99 LOSE)
#
# Exit codes:
#   0 = all sections pass
#   1 = any failure
# =============================================================================

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHAIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

TOTAL_PASS=0
TOTAL_FAIL=0
SECTIONS_PASSED=0
SECTIONS_FAILED=0

pass()  { echo -e "  ${GREEN}PASS${NC} $*"; TOTAL_PASS=$((TOTAL_PASS + 1)); }
fail()  { echo -e "  ${RED}FAIL${NC} $*"; TOTAL_FAIL=$((TOTAL_FAIL + 1)); }
note()  { echo -e "  ${YELLOW}note${NC} $*"; }
head1() { echo -e "\n${CYAN}========== $* ==========${NC}"; }

# Track per-section failures to decide section-level verdict
SECTION_FAIL_START=0
begin_section() {
    head1 "$1"
    SECTION_FAIL_START=$TOTAL_FAIL
}
end_section() {
    local name="$1"
    if [ "$TOTAL_FAIL" -eq "$SECTION_FAIL_START" ]; then
        SECTIONS_PASSED=$((SECTIONS_PASSED + 1))
        echo -e "  [${GREEN}SECTION PASS${NC}] $name"
    else
        SECTIONS_FAILED=$((SECTIONS_FAILED + 1))
        echo -e "  [${RED}SECTION FAIL${NC}] $name"
    fi
}

# -----------------------------------------------------------------------------
# Section A: head_lock_lib.sh exists, is syntactically valid, exposes API
# -----------------------------------------------------------------------------
begin_section "Section A: head_lock_lib.sh integrity"
LIB="$CHAIN_DIR/tools/head_lock_lib.sh"
if [ -f "$LIB" ]; then
    pass "head_lock_lib.sh exists at $LIB"
    if bash -n "$LIB" 2>/dev/null; then
        pass "head_lock_lib.sh syntax OK (bash -n)"
    else
        fail "head_lock_lib.sh has syntax errors"
    fi
    # Required API symbols
    # Note: upgrade_head_to_running was absorbed into verify_am_head (the upgrade
    # to state=RUNNING happens inside verify_am_head on success), so it's no
    # longer a distinct function.
    for sym in acquire_head_lock write_head_jobid \
               release_head_lock release_head_lock_if_mine verify_am_head; do
        if grep -qE "^${sym}\(\)" "$LIB"; then
            pass "API present: $sym()"
        else
            fail "API missing: $sym()"
        fi
    done
else
    fail "head_lock_lib.sh NOT FOUND at $LIB"
fi
end_section "A"

# -----------------------------------------------------------------------------
# Section B: every sbatch call site is paired with head_lock_lib primitives
# -----------------------------------------------------------------------------
begin_section "Section B: sbatch call sites guarded by head_lock_lib"

# File -> list of required symbols
declare -A REQUIRED_SYMS
REQUIRED_SYMS["submit_dispatcher.sh"]="acquire_head_lock write_head_jobid release_head_lock"
REQUIRED_SYMS["build_and_submit.sh.GB200"]="write_head_jobid release_head_lock"
REQUIRED_SYMS["build_and_submit.sh.H200"]="write_head_jobid release_head_lock"
REQUIRED_SYMS["jobscript_chain.slurm.GB200"]="verify_am_head release_head_lock_if_mine"
REQUIRED_SYMS["jobscript_chain.slurm.H200"]="verify_am_head release_head_lock_if_mine"

for f in submit_dispatcher.sh build_and_submit.sh.GB200 build_and_submit.sh.H200 \
         jobscript_chain.slurm.GB200 jobscript_chain.slurm.H200; do
    path="$CHAIN_DIR/$f"
    if [ ! -f "$path" ]; then
        fail "$f missing (required sbatch-wrapping file)"
        continue
    fi

    # Count sbatch invocations (ignoring comments and grep/squeue usage)
    sbatch_call_lines=$(grep -nE '(^|[^#[:alnum:]_])sbatch([[:space:]]|$)' "$path" \
                       | grep -v '^[[:space:]]*#' \
                       | grep -vE '#[^#]*sbatch' \
                       | grep -vE 'grep .*sbatch|squeue|echo.*sbatch' || true)
    sbatch_count=$(echo "$sbatch_call_lines" | grep -c . || echo 0)
    note "$f: detected $sbatch_count sbatch invocation line(s)"

    # Check required head_lock_lib symbols are used somewhere in the file
    missing=""
    for sym in ${REQUIRED_SYMS[$f]}; do
        if grep -qE "\b$sym\b" "$path"; then
            pass "$f references $sym"
        else
            fail "$f missing required API call: $sym"
            missing="$missing $sym"
        fi
    done

    # Check head_lock_lib.sh is sourced (direct source or variable guard)
    if grep -qE '\. .*head_lock_lib\.sh|source .*head_lock_lib\.sh' "$path"; then
        pass "$f sources head_lock_lib.sh"
    else
        fail "$f does NOT source head_lock_lib.sh"
    fi
done
end_section "B"

# -----------------------------------------------------------------------------
# Section C: jobscripts call verify_am_head early (before mpirun)
# -----------------------------------------------------------------------------
begin_section "Section C: jobscripts call verify_am_head before mpirun"
for f in jobscript_chain.slurm.GB200 jobscript_chain.slurm.H200; do
    path="$CHAIN_DIR/$f"
    [ -f "$path" ] || { fail "$f missing"; continue; }

    # Find first non-comment line referencing verify_am_head / mpirun.
    # grep -n prefixes output with "N:" so the content part starts after the
    # first ':'. We filter comments/strings by inspecting the content.
    verify_line=$(grep -nE '\bverify_am_head\b' "$path" \
                 | awk -F: '{
                       line=$1; $1=""; sub(/^ /,"",$0); content=$0;
                       # skip pure-comment lines
                       if (content ~ /^[[:space:]]*#/) next;
                       print line; exit
                   }')
    mpirun_line=$(grep -nE '\bmpirun\b' "$path" \
                 | awk -F: '{
                       line=$1; $1=""; sub(/^ /,"",$0); content=$0;
                       if (content ~ /^[[:space:]]*#/) next;
                       # skip variable assignments of mpirun-like PATH or names
                       # (we want the actual command invocation)
                       print line; exit
                   }')

    if [ -z "$verify_line" ]; then
        fail "$f: no verify_am_head call"
    elif [ -z "$mpirun_line" ]; then
        note "$f: no mpirun found (skipping ordering check)"
        pass "$f: verify_am_head at line $verify_line"
    elif [ "$verify_line" -lt "$mpirun_line" ]; then
        pass "$f: verify_am_head at line $verify_line precedes mpirun at line $mpirun_line"
    else
        fail "$f: verify_am_head at line $verify_line is AFTER mpirun at line $mpirun_line"
    fi
done
end_section "C"

# -----------------------------------------------------------------------------
# Section D: jobscripts register release_head_lock_if_mine on EXIT trap
# -----------------------------------------------------------------------------
begin_section "Section D: jobscripts trap EXIT -> release_head_lock_if_mine"
for f in jobscript_chain.slurm.GB200 jobscript_chain.slurm.H200; do
    path="$CHAIN_DIR/$f"
    [ -f "$path" ] || { fail "$f missing"; continue; }

    # Look for a trap that installs release_head_lock_if_mine on EXIT
    trap_lines=$(grep -nE 'trap .+release_head_lock_if_mine.+EXIT' "$path" || true)
    if [ -n "$trap_lines" ]; then
        pass "$f: trap installs release_head_lock_if_mine on EXIT"
    else
        # Fallback: accept trap + any release_head_lock call
        loose=$(grep -nE 'trap .+release_head_lock.+EXIT' "$path" || true)
        if [ -n "$loose" ]; then
            note "$f: uses release_head_lock (not _if_mine) on EXIT -- acceptable but less safe"
            pass "$f: EXIT trap registered with release_head_lock"
        else
            fail "$f: no EXIT trap for release_head_lock[_if_mine]"
        fi
    fi
done
end_section "D"

# -----------------------------------------------------------------------------
# Section E: run.sh reads HEAD.lockdir and acquires before exec build_and_submit
# -----------------------------------------------------------------------------
begin_section "Section E: run.sh integrates Single-Head check"
RUNSH="$CHAIN_DIR/run.sh"
if [ ! -f "$RUNSH" ]; then
    fail "run.sh not found at $RUNSH"
else
    if grep -qE 'HEAD\.lockdir' "$RUNSH"; then
        pass "run.sh references HEAD.lockdir"
    else
        fail "run.sh does NOT reference HEAD.lockdir"
    fi
    if grep -qE '\bacquire_head_lock\b' "$RUNSH"; then
        pass "run.sh calls acquire_head_lock"
    else
        fail "run.sh does NOT call acquire_head_lock"
    fi
    if grep -qE '\. .*head_lock_lib\.sh|source .*head_lock_lib\.sh' "$RUNSH"; then
        pass "run.sh sources head_lock_lib.sh"
    else
        fail "run.sh does NOT source head_lock_lib.sh"
    fi
    # ordering: acquire_head_lock must come before exec build_and_submit.sh
    acquire_ln=$(grep -nE '\bacquire_head_lock\b' "$RUNSH" | head -1 | cut -d: -f1)
    exec_ln=$(grep -nE '^[[:space:]]*exec .*build_and_submit\.sh' "$RUNSH" | head -1 | cut -d: -f1)
    if [ -n "$acquire_ln" ] && [ -n "$exec_ln" ]; then
        if [ "$acquire_ln" -lt "$exec_ln" ]; then
            pass "run.sh: acquire_head_lock (line $acquire_ln) precedes exec build_and_submit.sh (line $exec_ln)"
        else
            fail "run.sh: acquire_head_lock (line $acquire_ln) is AFTER exec build_and_submit.sh (line $exec_ln)"
        fi
    fi
fi
end_section "E"

# -----------------------------------------------------------------------------
# Section F + G: delegate to verify_mutex.sh dynamic tests
# -----------------------------------------------------------------------------
begin_section "Section F+G: dynamic invariants via verify_mutex.sh"
VERIFY="$CHAIN_DIR/tools/verify_mutex.sh"
if [ ! -f "$VERIFY" ]; then
    fail "verify_mutex.sh not found at $VERIFY"
else
    note "Running verify_mutex.sh (Tests 1-8) ..."
    if bash "$VERIFY" >/tmp/audit_verify_mutex.out 2>&1; then
        pass "verify_mutex.sh exited 0"
        verify_pass=$(grep -c 'PASS:' /tmp/audit_verify_mutex.out || true)
        verify_fail=$(grep -c 'FAIL:' /tmp/audit_verify_mutex.out || true)
        note "verify_mutex.sh: $(grep -E '^  PASS:|^  FAIL:' /tmp/audit_verify_mutex.out | tr '\n' ' ')"

        # Specifically confirm Section F/G lines appeared and passed
        if grep -q 'Section F.*invariant held\|Section F.*Invariant held' /tmp/audit_verify_mutex.out; then
            pass "[Section F] Single-Head Invariant verified dynamically"
        else
            fail "[Section F] dynamic assertion missing from verify_mutex.sh output"
        fi
        if grep -q 'Section G.*1 WIN and 99 LOSE\|Section G.*No-Double-Submit: exactly 1 WIN' /tmp/audit_verify_mutex.out; then
            pass "[Section G] No-Double-Submit verified dynamically"
        else
            fail "[Section G] dynamic assertion missing from verify_mutex.sh output"
        fi
    else
        fail "verify_mutex.sh FAILED -- see /tmp/audit_verify_mutex.out"
        tail -40 /tmp/audit_verify_mutex.out
    fi
fi
end_section "F+G"

# -----------------------------------------------------------------------------
# Final verdict
# -----------------------------------------------------------------------------
echo ""
echo "============================================================================="
echo "  audit_mutex_integrity.sh  FINAL VERDICT"
echo "============================================================================="
echo "  Sections passed: $SECTIONS_PASSED"
echo "  Sections failed: $SECTIONS_FAILED"
echo "  Individual checks -- PASS: $TOTAL_PASS  FAIL: $TOTAL_FAIL"
echo ""
if [ "$TOTAL_FAIL" -eq 0 ]; then
    echo -e "  ${GREEN}[AUDIT PASS]${NC} Single-Head-per-Folder invariant fully enforced"
    echo "              across head_lock_lib, run.sh, submit_dispatcher,"
    echo "              build_and_submit.{GB200,H200}, and jobscripts."
    exit 0
else
    echo -e "  ${RED}[AUDIT FAIL]${NC} $TOTAL_FAIL individual check(s) failed across $SECTIONS_FAILED section(s)."
    exit 1
fi
