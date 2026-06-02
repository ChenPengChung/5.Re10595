#!/bin/bash
# =============================================================================
# select_combo_lib.sh — net-throughput (jp × partition) selector for the chain daemon.
#
# Picks the (jp, partition) combo that advances the most FTT over a decision
# horizon, and NEVER idles (dev is uncapped → always a placeable fallback).
#
#   net(c) = r_ftt(jp) · max(0, H − startdelay − overhead)
#     overhead = nrounds·restart_ovh + (jp≠current ? switch_ovh : 0),  nrounds = max(1,(H−sd)/walltime)
#   → penalises long start delay, short walltime (dev's 1h ⇒ many restarts), and jp changes.
#
# Admissibility (hard filters):
#   * jp ∈ {16,32,64} valid for the grid (jpswitch_valid)
#   * a.out.jp<N> pre-built + manifest-matched (jpswitch_binary_ready)
#   * partition AllowAccounts permits our account (sc_acct_allowed)
#   * jp ≤ per-account GPU cap of the partition (normal/4nodes=32, dev=∞)
#   * cap-headroom: if jp > (cap − account-GPUs-running-in-partition) the combo can't
#     start now → large startdelay (sbatch --test-only does NOT model this — Codex M2).
#
# Functions:
#   sc_enumerate <H>           -> lines "jp part startdelay_h net"
#   sc_pick_combo [--pending]  -> echo "jp part" of max net  (empty if none)
#   sc_simulate  [--pending]   -> human-readable table (no action) + the pick
#   sc_record_r_ftt <jp> <v>   -> persist measured FTT/hr for jp
# Depends on: cwd=PROJECT_ROOT; partition_lib.sh + jpswitch_lib.sh (sourced below).
# =============================================================================

SC_ACCT="${SC_ACCT:-MST115169}"
SC_VALID_JP="${SC_VALID_JP:-16 32 64}"
SC_PARTITIONS="${SC_PARTITIONS:-normal 4nodes dev}"
SC_GPN="${SC_GPN:-8}"                          # GPU per H200 node
SC_BADNODE="${SC_BADNODE:-25a-hgpn207}"
SC_JS="${SC_JS:-chain_code/jobscript_chain.slurm.H200}"
SC_TPDB="${SC_TPDB:-restart/throughput_by_jp.dat}"
SC_R32_DEFAULT="${SC_R32_DEFAULT:-0.93}"       # bootstrap FTT/hr @ jp=32 (measured 2026-06-02)
SC_SCALE_EXP="${SC_SCALE_EXP:-0.85}"           # sub-linear weak-scaling exponent
SC_HORIZON_H="${SC_HORIZON_H:-48}"             # round-end decision horizon (h)
SC_HORIZON_PEND_H="${SC_HORIZON_PEND_H:-1}"    # pending re-select horizon (h) → favours start-now
SC_RESTART_OVH_H="${SC_RESTART_OVH_H:-0.05}"   # ~3 min per-round restart
SC_SWITCH_OVH_H="${SC_SWITCH_OVH_H:-0.1}"      # ~6 min repartition when jp changes
SC_CAPBLOCK_SD_H="${SC_CAPBLOCK_SD_H:-24}"     # startdelay assigned to a cap-blocked combo

_sc_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$_sc_here/partition_lib.sh"
. "$_sc_here/jpswitch_lib.sh"

_sc_to_hours() {  # D-HH:MM:SS | HH:MM:SS -> hours
    awk -v t="$1" 'BEGIN{
        d=0; rest=t; if(split(t,a,"-")==2){d=a[1]; rest=a[2]}
        m=split(rest,b,":"); h=b[1]+0; mi=(m>=2?b[2]:0)+0; s=(m>=3?b[3]:0)+0
        printf "%.4f", d*24 + h + mi/60 + s/3600 }'
}

sc_cap() { case "$1" in normal|4nodes) echo 32 ;; *) echo 100000 ;; esac; }

sc_acct_allowed() {  # $1=partition -> 0 allowed
    local aa; aa=$(scontrol show partition "$1" 2>/dev/null | tr ' ' '\n' | grep '^AllowAccounts=' | cut -d= -f2)
    { [ -z "$aa" ] || [ "$aa" = "ALL" ]; } && return 0
    printf '%s' "$aa" | tr ',' '\n' | grep -qiE "^${SC_ACCT}$"
}

sc_acct_running_gpu() {  # $1=partition -> account GPUs RUNNING in that partition (cap is per-account, all users)
    squeue -A "$SC_ACCT" -p "$1" -h -t R -o "%D %b" 2>/dev/null | awk '
        { n=$1+0; g=0; if (match($2,/gpu:[0-9]+/)) g=substr($2,RSTART+4,RLENGTH-4)+0; tot+=n*g } END{print tot+0}'
}

sc_eta_hours() {  # $1=jp $2=partition -> hours to start (0=now, -1=cannot)
    local jp="$1" part="$2" nodes wt out
    nodes=$((jp / SC_GPN)); wt="$(h200_partition_walltime "$part")"
    [ -n "$wt" ] || { echo "-1"; return; }
    out=$(sbatch --test-only --partition="$part" --account="$SC_ACCT" \
            --nodes="$nodes" --ntasks-per-node="$SC_GPN" --gres=gpu:"$SC_GPN" \
            --time="$wt" --exclude="$SC_BADNODE" "$SC_JS" 2>&1 | head -1)
    case "$out" in
        *"to start at"*)
            local ts e_now e_st
            ts=$(echo "$out" | grep -oE 'to start at [0-9T:-]+' | awk '{print $4}')
            e_now=$(date +%s); e_st=$(date -d "$ts" +%s 2>/dev/null || echo "$e_now")
            awk -v a="$e_st" -v b="$e_now" 'BEGIN{d=(a-b)/3600; if(d<0)d=0; printf "%.4f", d}' ;;
        *allocat*|*immediately*) echo "0" ;;
        *) echo "-1" ;;
    esac
}

sc_record_r_ftt() {  # $1=jp $2=value(FTT/hr)
    local jp="$1" v="$2"; touch "$SC_TPDB"
    { grep -v -E "^jp${jp}=" "$SC_TPDB" 2>/dev/null; echo "jp${jp}=${v}"; } \
        > "${SC_TPDB}.tmp" && mv -f "${SC_TPDB}.tmp" "$SC_TPDB"
}

sc_r_ftt() {  # $1=jp -> FTT/hr (measured if in db, else bootstrap r32·(jp/32)^exp)
    local jp="$1" r32="" v
    if [ -f "$SC_TPDB" ]; then
        v=$(grep -E "^jp${jp}=" "$SC_TPDB" | cut -d= -f2 | tr -d '[:space:]')
        [ -n "$v" ] && { echo "$v"; return; }
        r32=$(grep -E "^jp32=" "$SC_TPDB" | cut -d= -f2 | tr -d '[:space:]')
    fi
    [ -n "$r32" ] || r32="$SC_R32_DEFAULT"
    awk -v r="$r32" -v jp="$jp" -v e="$SC_SCALE_EXP" 'BEGIN{ printf "%.4f", r*(jp/32.0)^e }'
}

sc_net() {  # $1=jp $2=part $3=startdelay_h $4=cur_jp $5=H -> net FTT in horizon
    local jp="$1" part="$2" sd="$3" cur="$4" H="$5" wt_h rftt
    wt_h=$(_sc_to_hours "$(h200_partition_walltime "$part")"); rftt=$(sc_r_ftt "$jp")
    awk -v jp="$jp" -v cur="$cur" -v sd="$sd" -v H="$H" -v wt="$wt_h" -v rftt="$rftt" \
        -v ro="$SC_RESTART_OVH_H" -v so="$SC_SWITCH_OVH_H" 'BEGIN{
        if (sd < 0) { print "0.000"; exit }
        avail = H - sd; if (avail < 0) avail = 0
        nr = (wt>0 ? avail/wt : 1); if (nr < 1) nr = 1
        ovh = nr*ro + (jp!=cur ? so : 0)
        usable = avail - ovh; if (usable < 0) usable = 0
        printf "%.3f", rftt*usable }'
}

sc_enumerate() {  # $1=H -> lines "jp part startdelay_h net"
    local H="$1" cur jp part cap run headroom sd net
    cur=$(jpswitch_current_jp); cur="${cur:-0}"
    for jp in $SC_VALID_JP; do
        jpswitch_valid "$jp" || continue
        jpswitch_binary_ready "$jp" >/dev/null 2>&1 || continue
        for part in $SC_PARTITIONS; do
            sc_acct_allowed "$part" || continue
            cap=$(sc_cap "$part"); [ "$jp" -le "$cap" ] || continue
            run=$(sc_acct_running_gpu "$part"); headroom=$((cap - run))
            if [ "$jp" -le "$headroom" ]; then sd=$(sc_eta_hours "$jp" "$part")
            else sd="$SC_CAPBLOCK_SD_H"; fi
            net=$(sc_net "$jp" "$part" "$sd" "$cur" "$H")
            echo "$jp $part $sd $net"
        done
    done
}

sc_pick_combo() {  # [--pending] -> "jp part"
    local H="$SC_HORIZON_H"; [ "${1:-}" = "--pending" ] && H="$SC_HORIZON_PEND_H"
    sc_enumerate "$H" | sort -k4 -g -r | head -1 | awk '{print $1, $2}'
}

sc_simulate() {  # [--pending] -> table + pick (no action)
    local H="$SC_HORIZON_H"; [ "${1:-}" = "--pending" ] && H="$SC_HORIZON_PEND_H"
    local cur; cur=$(jpswitch_current_jp)
    echo "[select] account=$SC_ACCT  current jp=${cur:-?}  horizon=${H}h"
    printf "  %-4s %-7s %-9s %-8s %-9s\n" jp part startΔh r_ftt net
    sc_enumerate "$H" | sort -k4 -g -r | while read -r jp part sd net; do
        printf "  %-4s %-7s %-9s %-8s %-9s\n" "$jp" "$part" "$sd" "$(sc_r_ftt "$jp")" "$net"
    done
    echo "  -> PICK: $(sc_pick_combo "${1:-}" | sed 's/^$/<none — all idle, will retry>/')"
}
