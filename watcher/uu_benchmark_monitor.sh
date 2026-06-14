#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# uu_benchmark_monitor.sh — READ-ONLY uu-convergence time-series logger
# ────────────────────────────────────────────────────────────────────────────
# Captures one snapshot of the uu statistical-convergence state and appends a
# row to live/uu_benchmark_timeseries.csv, then (if >=2 rows) regenerates
# live/uu_convergence_timeseries.png so uu's march toward convergence is visible
# over time. Pairs with the watcher, which refreshes the uu-vs-DNS fig_uu.png.
#
# STRICTLY READ-ONLY w.r.t. the simulation/job: no scancel/scontrol/sbatch, no
# touching restart/ or checkpoints. Only reads the slurm log + writes into live/.
# ────────────────────────────────────────────────────────────────────────────
set -u
cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"
LIVE="$ROOT/live"
CSV="$LIVE/uu_benchmark_timeseries.csv"
PNG="$LIVE/uu_convergence_timeseries.png"
mkdir -p "$LIVE"

# stats-start threshold (averaging window = FTT - FTT_STATS_START)
FSS=$(grep -aoE '#define[[:space:]]+FTT_STATS_START[[:space:]]+[0-9.]+' variables.h 2>/dev/null \
        | grep -oE '[0-9.]+$' | head -1); FSS=${FSS:-60.0}

LOG=$(ls -t slurm_*.log 2>/dev/null | head -1)
[ -z "${LOG:-}" ] && { echo "[uu-monitor] no slurm log found"; exit 0; }

# latest [Step .. | FTT=..] line
STEPLINE=$(grep -aE '\[Step .* FTT=' "$LOG" 2>/dev/null | tail -1)
STEP=$(echo "$STEPLINE" | grep -oE 'Step [0-9]+' | grep -oE '[0-9]+' | head -1)
FTT=$(echo "$STEPLINE"  | grep -oE 'FTT=[0-9.]+' | grep -oE '[0-9.]+' | head -1)

# latest [CONV .. CV(uu)=.. CV(k)=.. Status=XXX (n/m)] line
CONV=$(grep -aE '\[CONV\].*CV\(uu\)' "$LOG" 2>/dev/null | tail -1)
CVUU=$(echo "$CONV" | grep -oE 'CV\(uu\)=[0-9.]+%' | grep -oE '[0-9.]+' | head -1)
CVK=$(echo  "$CONV" | grep -oE 'CV\(k\)=[0-9.]+%'  | grep -oE '[0-9.]+' | head -1)
CVN=$(echo  "$CONV" | grep -oE '\([0-9]+/[0-9]+\)' | grep -oE '[0-9]+' | head -1)
CVM=$(echo  "$CONV" | grep -oE '\([0-9]+/[0-9]+\)' | grep -oE '[0-9]+' | tail -1)
CVSTAT=$(echo "$CONV" | grep -oE 'Status=[A-Z]+' | sed 's/Status=//' | head -1)

# latest accu_count
ACCU=$(grep -aoE 'accu_count=[0-9]+' "$LOG" 2>/dev/null | tail -1 | grep -oE '[0-9]+')

# fig_uu freshness (minutes since last DNS benchmark refresh by watcher)
if [ -f "$LIVE/fig_uu.png" ]; then
    FIGAGE=$(( ( $(date +%s) - $(stat -c %Y "$LIVE/fig_uu.png") ) / 60 ))
else
    FIGAGE=-1
fi

WIN=$(awk -v f="${FTT:-0}" -v s="$FSS" 'BEGIN{printf "%.3f", f-s}')
NOW=$(date '+%Y-%m-%dT%H:%M:%S')

[ -f "$CSV" ] || echo "time,step,ftt,accu_count,window_ftt,cv_uu_pct,cv_k_pct,cv_status,cv_n,cv_m,fig_uu_age_min" > "$CSV"
echo "$NOW,${STEP:-NA},${FTT:-NA},${ACCU:-NA},${WIN:-NA},${CVUU:-NA},${CVK:-NA},${CVSTAT:-NA},${CVN:-NA},${CVM:-NA},$FIGAGE" >> "$CSV"

# compact console status
echo "════════ uu benchmark time-monitor ════════"
echo "  time         : $NOW"
echo "  step / FTT   : ${STEP:-?} / ${FTT:-?}"
echo "  accu_count   : ${ACCU:-?}   (averaging window = ${WIN} FTT, start=$FSS)"
echo "  CV(uu)/CV(k) : ${CVUU:-?}% / ${CVK:-?}%   Status=${CVSTAT:-?} (${CVN:-?}/${CVM:-?})"
echo "  fig_uu age   : ${FIGAGE} min (watcher DNS-benchmark refresh)"
echo "  csv rows     : $(( $(wc -l < "$CSV") - 1 ))   -> $CSV"

# regenerate trend plot once we have >=2 rows
ROWS=$(( $(wc -l < "$CSV") - 1 ))
if [ "$ROWS" -ge 2 ]; then
python3 - "$CSV" "$PNG" <<'PY' 2>/dev/null && echo "  trend plot   : $PNG"
import sys, csv
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
csvf, png = sys.argv[1], sys.argv[2]
t=[];win=[];accu=[];cvuu=[];cvn=[]
with open(csvf) as f:
    for r in csv.DictReader(f):
        def g(k):
            v=r.get(k,"NA")
            try: return float(v)
            except: return None
        win.append(g("window_ftt")); accu.append(g("accu_count"))
        cvuu.append(g("cv_uu_pct")); cvn.append(g("cv_n"))
        t.append(r.get("time",""))
x=list(range(len(t)))
fig,ax=plt.subplots(2,1,figsize=(9,7),sharex=True)
ax[0].plot([w for w in win], [c for c in cvuu], 'o-', color='tab:red', label='CV(uu) %')
ax[0].set_ylabel('CV(uu) [%]'); ax[0].grid(True,alpha=.3); ax[0].set_title('uu statistical convergence vs averaging window')
ax2=ax[0].twinx()
ax2.plot([w for w in win], [n for n in cvn], 's--', color='tab:blue', alpha=.6, label='Status n/10')
ax2.set_ylabel('CV converged count (n/10)', color='tab:blue')
ax[1].plot([w for w in win], [a/1e6 if a else None for a in accu], 'o-', color='tab:green')
ax[1].set_xlabel('averaging window  (FTT - FTT_STATS_START)')
ax[1].set_ylabel('accu_count [×1e6]'); ax[1].grid(True,alpha=.3)
fig.tight_layout(); fig.savefig(png, dpi=110);
PY
fi
echo "════════════════════════════════════════════"
