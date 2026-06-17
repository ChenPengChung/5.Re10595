#!/bin/bash
# =============================================================================
# jpswitch_lib.sh — core jp-switch primitive (NO submit / NO scancel / NO build).
# Sourced by submit_dispatcher.sh (daemon hot path) and usable by changejp.sh.
#
# Given a target jp N, make a jp=N round READY TO SUBMIT:
#   1) repartition the latest checkpoint (stats-preserving, bit-exact via
#      chain_code/repartition_jp.py — verified once by roundtrip_verify.sh),
#   2) swap in the pre-built a.out.jp<N> (cp only — NEVER nvcc in this path),
#   3) update variables.h jp + jobscript --nodes + grid_provenance mtime.
# The caller submits. On ANY failure the current checkpoint / binary / variables
# are left UNTOUCHED, so the caller can fall back to the current jp (never idle).
#
# Functions:
#   jpswitch_current_jp                 -> echo current jp (latest ckpt mpi_rank_count)
#   jpswitch_valid <N>                  -> rc0 if N a valid jp for this grid
#   jpswitch_binary_ready <N>           -> rc0 if a.out.jp<N> present (+manifest md5 if recorded)
#   jpswitch_record_manifest <N>        -> record a.out.jp<N> md5 into the manifest
#   jpswitch_apply <N>                  -> perform the switch; rc0 ok (2=binary,3=repart,4=verify,5=cp)
# Depends on: cwd=PROJECT_ROOT; chain_code/repartition_jp.py; restart/checkpoint/latest.
# =============================================================================

JPSWITCH_VH="${JPSWITCH_VH:-variables.h}"
JPSWITCH_REPART="${JPSWITCH_REPART:-chain_code/repartition_jp.py}"
JPSWITCH_PROV="${JPSWITCH_PROV:-restart/grid_provenance}"
JPSWITCH_MANIFEST="${JPSWITCH_MANIFEST:-restart/binary_manifest.dat}"
JPSWITCH_LATEST="${JPSWITCH_LATEST:-restart/checkpoint/latest}"
JPSWITCH_JS_H200="${JPSWITCH_JS_H200:-chain_code/jobscript_chain.slurm.H200}"
JPSWITCH_JS_GB200="${JPSWITCH_JS_GB200:-chain_code/jobscript_chain.slurm.GB200}"

_jps_log() { printf '[jpswitch] %s\n' "$*" >&2; }

_jps_NYm1() {  # echo (NY-1) from variables.h
    local ny; ny=$(grep -E '^#define[[:space:]]+NY[[:space:]]+[0-9]+' "$JPSWITCH_VH" | grep -oE '[0-9]+' | head -1)
    echo $((ny - 1))
}

jpswitch_current_jp() {
    local m="$JPSWITCH_LATEST/metadata.dat"
    [ -f "$m" ] && grep '^mpi_rank_count=' "$m" | head -1 | cut -d= -f2
}

jpswitch_valid() {  # $1=N : divisible, slab>=7, whole H200 node (8 GPU)
    local N="$1" NYm1; NYm1=$(_jps_NYm1)
    [[ "$N" =~ ^[0-9]+$ ]] && [ "$N" -ge 1 ] || return 1
    [ $((NYm1 % N)) -eq 0 ] || return 1
    [ $((NYm1 / N)) -ge 7 ] || return 1
    [ $((N % 8)) -eq 0 ] || return 1
    return 0
}

jpswitch_binary_ready() {  # $1=N : a.out.jp<N> exists AND manifest-matched (md5)
    local N="$1"; local bin="a.out.jp${N}"
    [ -s "$bin" ] || return 1
    # [Codex B1 fix] if a manifest exists, REQUIRE an entry for jp<N> and that md5 matches
    # (not just "matched if an entry happens to exist") — an unverified binary is rejected.
    if [ -f "$JPSWITCH_MANIFEST" ]; then
        local want; want=$(grep -E "^jp${N}=" "$JPSWITCH_MANIFEST" | cut -d= -f2 | tr -d '[:space:]')
        [ -n "$want" ] || { _jps_log "no manifest entry for jp${N} (binary unverified) -> reject"; return 2; }
        local got; got=$(md5sum "$bin" | cut -d' ' -f1)
        [ "$want" = "$got" ] || { _jps_log "manifest md5 mismatch for $bin ($got != $want)"; return 2; }
    fi
    return 0
}

jpswitch_record_manifest() {  # $1=N : record md5(a.out.jp<N>) -> manifest
    local N="$1"; local bin="a.out.jp${N}"
    [ -s "$bin" ] || return 1
    local md5; md5=$(md5sum "$bin" | cut -d' ' -f1)
    touch "$JPSWITCH_MANIFEST"
    { grep -v -E "^jp${N}=" "$JPSWITCH_MANIFEST" 2>/dev/null; echo "jp${N}=${md5}"; } \
        > "${JPSWITCH_MANIFEST}.tmp" && mv -f "${JPSWITCH_MANIFEST}.tmp" "$JPSWITCH_MANIFEST"
    _jps_log "manifest: jp${N}=${md5}"
}

jpswitch_apply() {  # $1=N
    local N="$1" cur; cur=$(jpswitch_current_jp)
    # [P3] if latest checkpoint metadata is missing/malformed, fall back to variables.h jp
    [ -n "$cur" ] || cur=$(grep -E '^#define[[:space:]]+jp[[:space:]]+[0-9]+' "$JPSWITCH_VH" | grep -oE '[0-9]+' | head -1)
    jpswitch_valid "$N"        || { _jps_log "jp=$N invalid for this grid"; return 1; }
    [ "$N" = "$cur" ]          && { _jps_log "jp already $N — no switch"; return 0; }
    # [Codex B3 fix] freeze sentinel: if jp-switching was frozen (e.g. byte-exact gate failed),
    # refuse so the caller falls back to the current jp (partition-only switching).
    [ -f restart/STOP_JPSWITCH ] && { _jps_log "restart/STOP_JPSWITCH present -> jp-switch FROZEN; falling back to current jp"; return 9; }
    jpswitch_binary_ready "$N" || { _jps_log "a.out.jp${N} missing/mismatch — abort (caller falls back)"; return 2; }

    local latest_target; latest_target="$(readlink -f "$JPSWITCH_LATEST")"
    [ -d "$latest_target" ] || { _jps_log "no latest checkpoint"; return 3; }

    # 1) repartition latest -> tmp (stats-preserving, bit-exact). Checkpoint untouched on fail.
    local tmp="restart/checkpoint/.jpswitch_tmp_jp${N}.$$"
    rm -rf "$tmp"
    if ! python3 "$JPSWITCH_REPART" --src "$latest_target" --dst "$tmp" --new-jp "$N" \
            >restart/.jpswitch_repart.log 2>&1; then
        _jps_log "repartition failed (restart/.jpswitch_repart.log); checkpoint untouched"
        rm -rf "$tmp"; return 3
    fi
    # 2) integrity check
    local got_jp; got_jp=$(grep '^mpi_rank_count=' "$tmp/metadata.dat" 2>/dev/null | cut -d= -f2)
    [ "$got_jp" = "$N" ] || { _jps_log "tmp metadata jp=$got_jp != $N"; rm -rf "$tmp"; return 4; }

    # ---- COMMIT ----
    # binary first (idempotent; nothing else changed yet → safe to bail with state untouched)
    # [Codex P5 fix] if either binary cp fails, restore BOTH a.out and a.out.H200 to the
    # current jp so a partial copy never leaves binary state inconsistent.
    if ! { cp -f "a.out.jp${N}" a.out && cp -f "a.out.jp${N}" a.out.H200; }; then
        _jps_log "cp a.out.jp${N} failed; restoring BOTH binaries to jp=${cur}"
        cp -f "a.out.jp${cur}" a.out 2>/dev/null; cp -f "a.out.jp${cur}" a.out.H200 2>/dev/null
        rm -rf "$tmp"; return 5
    fi
    # checkpoint swap — error-checked + rollback; the `mv $tmp -> latest_target` is the COMMIT POINT.
    local bak="${latest_target}_jp${cur}_bak.$$"
    if ! mv "$latest_target" "$bak"; then
        _jps_log "ERROR: backup move failed ($latest_target -> $bak); rolling back binary, state UNTOUCHED"
        rm -rf "$tmp"
        cp -f "a.out.jp${cur}" a.out 2>/dev/null; cp -f "a.out.jp${cur}" a.out.H200 2>/dev/null
        return 6
    fi
    if ! mv "$tmp" "$latest_target"; then
        _jps_log "ERROR: new-ckpt move failed; restoring backup + binary, state UNTOUCHED"
        mv "$bak" "$latest_target" 2>/dev/null
        rm -rf "$tmp"
        cp -f "a.out.jp${cur}" a.out 2>/dev/null; cp -f "a.out.jp${cur}" a.out.H200 2>/dev/null
        return 6
    fi
    ln -sfn "$(basename "$latest_target")" "$JPSWITCH_LATEST"
    # ===== past the commit point: jp=N is live (checkpoint + binary). The edits below are config
    # bookkeeping; the daemon sizes via sbatch CLI (not the jobscript), so a sed failure here is
    # non-fatal for a daemon run — but is logged loudly (no silent || true) + consistency-checked. =====
    sed -E -i "s/^(#define[[:space:]]+jp[[:space:]]+)[0-9]+/\1${N}/" "$JPSWITCH_VH" \
        || _jps_log "WARN: variables.h jp sed failed (stale vs binary jp=$N; only matters on a rebuild)"
    sed -E -i "s/^(#SBATCH --nodes=)[0-9]+/\1$((N / 8))/" "$JPSWITCH_JS_H200" 2>/dev/null \
        || _jps_log "WARN: jobscript H200 --nodes sed failed (size still set via sbatch CLI)"
    [ -f "$JPSWITCH_JS_GB200" ] && { sed -E -i "s/^(#SBATCH --nodes=)[0-9]+/\1$((N / 4))/" "$JPSWITCH_JS_GB200" 2>/dev/null \
        || _jps_log "WARN: jobscript GB200 --nodes sed failed"; }
    if [ -f "$JPSWITCH_PROV" ]; then
        local vh_mt chunk; vh_mt=$(stat -c %Y "$JPSWITCH_VH"); chunk=$(( $(_jps_NYm1) / N ))
        sed -E -i "s/^new_jp=.*/new_jp=${N}/; s/^new_chunk_j=.*/new_chunk_j=${chunk}/; s/^variables_h_mtime=.*/variables_h_mtime=${vh_mt}/" "$JPSWITCH_PROV" 2>/dev/null \
            || _jps_log "WARN: grid_provenance update failed (run.sh Preflight-C may FATAL on a manual ./run)"
    fi
    # [P2] post-commit consistency check: variables.h jp must now equal N
    local vh_jp; vh_jp=$(grep -E '^#define[[:space:]]+jp[[:space:]]+[0-9]+' "$JPSWITCH_VH" | grep -oE '[0-9]+' | head -1)
    [ "$vh_jp" = "$N" ] || _jps_log "WARN: variables.h jp=$vh_jp != $N after switch (inconsistent — manual check advised)"
    _jps_log "✓ jp ${cur} -> ${N}: checkpoint repartitioned bit-exact, binary a.out.jp${N}, old ckpt bak=$bak"
    return 0
}
