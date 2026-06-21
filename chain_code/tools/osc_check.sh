#!/bin/bash
# =============================================================================
# osc_check.sh — oscillation monitor for the GILBM chain (jp-switch / resume safety).
#
# Reads Ustar_Force_record.dat col3 = Ub/Uref (per-50-step). The repartition-BUG
# signature is HIGH-FREQUENCY erratic Ub jumps (>3% between consecutive rows);
# a healthy controller transient is a SMOOTH slow drift (<1% per row). It isolates
# the current run (rows after the most recent FTT-rewind = restart) so a clean
# restart's tiny re-convergence isn't confused with sustained oscillation.
# Read-only. Verdict: ✓ smooth / ⚠ OSCILLATION / ⚠ Ma / hard-fail.
# =============================================================================
cd /home/s8313697/5.Re10595/Edit11_Krank5600 || exit 1
JID=$(cat restart/chain_jobid 2>/dev/null | tr -d ' ')
ST=$(sacct -j "$JID" -n -o State 2>/dev/null | head -1 | tr -d ' ')
echo "job=$JID state=${ST:-?}  $(date '+%H:%M:%S')"
REC=Ustar_Force_record.dat
[ -f "$REC" ] || { echo "  (no Ustar_Force_record.dat yet)"; exit 0; }
awk 'NR>1 && $1!~/#/ && NF>=12 {
        ftt=$1+0; ub=$2+0; ma=$4+0; err=$9+0   # cols: 1=FTT 2=Ub/Uref 4=Ma_max 9=Error
        if (pf!="" && ftt < pf-1e-9) { n=0 }     # FTT rewind => restart => reset window to this run
        pf=ftt; n++; F[n]=ftt; U[n]=ub; M[n]=ma; E[n]=err
    }
    END{
        if (n==0){ print "  (no data rows yet)"; exit }
        lo=(n>15?n-14:1); maxd=0
        printf "  Ub/Uref (last %d of current run): ", n-lo+1
        for(i=lo;i<=n;i++){ printf "%.4f ", U[i];
            if(i>lo){ d=U[i]-U[i-1]; ad=(d<0?-d:d)/(U[i-1]>0?U[i-1]:1); if(ad>maxd)maxd=ad } }
        print ""
        printf "  FTT %.4f -> %.4f   Ma_max=%.4f   Error=%.3e   (rows this run=%d)\n", F[lo], F[n], M[n], E[n], n
        printf "  max consecutive |dUb/Ub| = %.2f%%  -> %s\n", maxd*100, (maxd>0.03?"*** OSCILLATION ***":"smooth OK")
        if (M[n]>0.3) printf "  *** Ma_max=%.3f > 0.3 (compressibility risk) ***\n", M[n]
    }' "$REC"
LOG="slurm_${JID}.log"
if [ -f "$LOG" ]; then
    h=$(grep -ciE 'nan|MPI_Abort|FATAL|DIVERG|\[G6\].*mismatch' "$LOG" 2>/dev/null)
    echo "  slurm NaN/FATAL/G6 hits: ${h:-0} $([ "${h:-0}" -gt 0 ] && echo '*** ALERT ***' || echo OK)"
fi
echo "  latest ckpt: $(readlink restart/checkpoint/latest 2>/dev/null)"
