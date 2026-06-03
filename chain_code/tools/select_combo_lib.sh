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
# [自由切換候選 — 完全開啟] jp{128,64,32} × partition{normal,dev,4nodes}。
#   完全開啟原則: 每一組都「實際評估後才跳過(帶理由)」, 不盲目預排除 (見 sc_audit / sc_enumerate)。
#   - 128: 會被 jpswitch_valid 評估後判無效(640/128=5<7 slab, kernel 內部列=-2)→ 帶理由跳過。
#   - 64 : 只能 dev(>normal cap16 / >4nodes cap32)。  32: dev/4nodes(>normal cap16)。
#   - 16 : 三者皆可但未預編 a.out.jp16 → 評估 binary 後帶理由跳過。
#   要真正用 >64 GPU 須換更細網格(NY-1≥896 才能讓 128 的 slab≥7), 屬不同解析度的 DNS。
SC_VALID_JP="${SC_VALID_JP:-8}"
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

sc_cap() {  # $1=partition -> per-account GPU cap (QOS MaxTRESPerAccount; live read, hardcoded fallback)
    # [Codex A3 fix] caps are PER-QOS and NOT uniform: verified p_normal=16, p_4nodes=32, dev=uncapped.
    # The old hardcoded "normal|4nodes)=32" was wrong for normal (16) → jp=32 was wrongly deemed
    # admissible on normal and every submit there PENDs (MaxGRESPerAccount). Read live from sacctmgr
    # (NCHC QOS = p_<partition>); fall back to the verified values if sacctmgr is unavailable.
    local part="$1" cap
    cap=$(sacctmgr -n -P show qos "p_${part}" format=MaxTRESPerAccount 2>/dev/null \
            | grep -oE 'gres/gpu=[0-9]+' | cut -d= -f2 | head -1)
    [ -n "$cap" ] && { echo "$cap"; return; }
    case "$part" in normal) echo 16 ;; 4nodes) echo 32 ;; *) echo 100000 ;; esac
}

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

sc_record_r_ftt() {  # $1=jp $2=measured(FTT/hr) -> EWMA 平滑寫入 (防單樣本雜訊翻面 → thrash)
    # blended = α·measured + (1-α)·old  (α=SC_EWMA_ALPHA, 預設 0.4: 偏重歷史, 抗單輪 init/contention 雜訊)
    local jp="$1" v="$2" a="${SC_EWMA_ALPHA:-0.4}" old blended; touch "$SC_TPDB"
    old=$(grep -E "^jp${jp}=" "$SC_TPDB" | cut -d= -f2 | tr -d '[:space:]')
    if [ -n "$old" ] && awk -v o="$old" 'BEGIN{exit !(o>0)}'; then
        blended=$(awk -v m="$v" -v o="$old" -v a="$a" 'BEGIN{printf "%.5f", a*m+(1-a)*o}')
    else
        blended="$v"
    fi
    { grep -v -E "^jp${jp}=" "$SC_TPDB" 2>/dev/null; echo "jp${jp}=${blended}"; } \
        > "${SC_TPDB}.tmp" && mv -f "${SC_TPDB}.tmp" "$SC_TPDB"
}

# [Codex C2 fix] learn the realized FTT/hr for jp from the last round in timing_log.dat and persist
# it (so net-scoring uses measured rates, not just the bootstrap). Columns: Step FTT GPU_min Wall_min ...
# Wall_min resets each round → use the trailing monotonic-Wall block (the last round):
#   r_ftt = (FTT_last - FTT_first) / (Wall_min_last - Wall_min_first) * 60.
sc_update_r_ftt() {  # $1=jp
    local jp="$1" tl="${SC_TIMING_LOG:-timing_log.dat}" r
    [[ "$jp" =~ ^[0-9]+$ ]] && [ "$jp" -gt 0 ] || return 1
    [ -f "$tl" ] || return 1
    r=$(awk '!/^#/ && NF>=4 {
            ftt=$2+0; wall=$4+0
            if (pw!="" && wall < pw) f0=""        # Wall reset => new round => restart window
            if (f0=="") { f0=ftt; w0=wall }
            f1=ftt; w1=wall; pw=wall
        } END { dw=w1-w0; if (dw>0.001) printf "%.5f", (f1-f0)/dw*60 }' "$tl")
    if [ -n "$r" ] && awk -v x="$r" 'BEGIN{exit !(x>0)}'; then
        sc_record_r_ftt "$jp" "$r"
        echo "[select] measured r_ftt(jp=$jp)=$r FTT/hr (from $tl) -> persisted" >&2
    fi
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

sc_pick_combo() {  # [--pending] -> "jp part"  (含 anti-thrash 遲滯)
    local H="$SC_HORIZON_H"; [ "${1:-}" = "--pending" ] && H="$SC_HORIZON_PEND_H"
    local enum best best_jp cur best_cur nb nc
    enum=$(sc_enumerate "$H" | sort -k4 -g -r)
    best=$(printf '%s\n' "$enum" | head -1); [ -z "$best" ] && return
    best_jp=$(printf '%s\n' "$best" | awk '{print $1}')
    cur=$(jpswitch_current_jp); cur="${cur:-0}"
    # 最佳就是當前 jp → 直接用 (partition 內切換免遲滯)
    if [ "$best_jp" = "$cur" ]; then printf '%s\n' "$best" | awk '{print $1, $2}'; return; fi
    # [弱點#1 修補] 最佳是「不同 jp」→ 須淨贏過「當前 jp 最佳組合」× margin(預設1.15)才切, 否則留在當前 jp。
    # 防 r_ftt 雜訊造成 jp32↔jp64 來回 thrash(每切一次=一個 repartition+restart)。
    best_cur=$(printf '%s\n' "$enum" | awk -v c="$cur" '$1==c' | head -1)
    [ -z "$best_cur" ] && { printf '%s\n' "$best" | awk '{print $1, $2}'; return; }  # 當前 jp 不可投→必須切
    nb=$(printf '%s\n' "$best" | awk '{print $4}'); nc=$(printf '%s\n' "$best_cur" | awk '{print $4}')
    if awk -v nb="$nb" -v nc="$nc" -v m="${SC_THRASH_MARGIN:-1.15}" 'BEGIN{exit !(nb > nc*m)}'; then
        printf '%s\n' "$best"     | awk '{print $1, $2}'   # 切 jp 划算 (淨贏 > margin)
    else
        printf '%s\n' "$best_cur" | awk '{print $1, $2}'   # anti-thrash: 留在當前 jp
    fi
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

# [完全開啟 audit] 對 SC_VALID_JP × SC_PARTITIONS 的「每一組」都實際評估, 各印一行 verdict
# (SKIP:理由 / EVAL:net) — 證明「做過選擇(評估)後才跳過」, 非盲目預排除。daemon 每次決策前 log 它。
sc_audit() {  # [H]
    local H="${1:-$SC_HORIZON_H}" cur jp part cap run hr sd net slab
    cur=$(jpswitch_current_jp); cur="${cur:-0}"
    for jp in $SC_VALID_JP; do
        for part in $SC_PARTITIONS; do
            if ! jpswitch_valid "$jp"; then
                slab=$(( $(_jps_NYm1) / jp )); echo "jp${jp} ${part}  SKIP invalid-grid (slab=${slab}<7)"; continue
            fi
            if ! jpswitch_binary_ready "$jp" >/dev/null 2>&1; then
                echo "jp${jp} ${part}  SKIP no-binary/manifest (a.out.jp${jp})"; continue
            fi
            if ! sc_acct_allowed "$part"; then
                echo "jp${jp} ${part}  SKIP partition-not-authorized"; continue
            fi
            cap=$(sc_cap "$part")
            if [ "$jp" -gt "$cap" ]; then
                # QOS 硬上限: jp 超過該 partition 的 per-account GPU cap → 警告 + 跳過不投
                echo "jp${jp} ${part}  ⚠ QOS-SKIP over-cap (jp>${cap}, QOS p_${part} MaxTRESPerAccount gres/gpu=${cap}) — 跳過不處理"; continue
            fi
            run=$(sc_acct_running_gpu "$part"); hr=$((cap - run))
            if [ "$jp" -le "$hr" ]; then
                sd=$(sc_eta_hours "$jp" "$part"); net=$(sc_net "$jp" "$part" "$sd" "$cur" "$H")
                echo "jp${jp} ${part}  EVAL startΔ=${sd}h net=${net} (sbatch --test-only probed)"
            else
                # QOS headroom 用罄(帳號其他 job 已佔滿該 QOS cap)→ 警告 + 給大罰分(實質不投)
                sd="$SC_CAPBLOCK_SD_H"; net=$(sc_net "$jp" "$part" "$sd" "$cur" "$H")
                echo "jp${jp} ${part}  ⚠ QOS-BLOCK headroom=${hr} (帳號已佔滿 QOS p_${part} cap=${cap}) → 罰分 startΔ=${sd}h net=${net}, 不投"
            fi
        done
    done
}
