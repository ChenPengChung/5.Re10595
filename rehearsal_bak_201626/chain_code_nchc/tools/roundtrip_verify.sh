#!/bin/bash
# =============================================================================
# roundtrip_verify.sh — OWNED-field byte-exact round-trip gate for the
# checkpoint repartitioner (chain_code/repartition_jp.py).
#
# Runs  src(jp0) -> intermediate_jp -> jp0  and asserts that every f*/rho/sum_*
# OWNED region (j_local 3..3+N-1, the physical field) is byte-identical to the
# source. Ghost/overlap rows are intentionally EXCLUDED — the repartitioner
# refills them from periodic neighbours and the solver re-exchanges halos on
# load, so only the owned interior must be exact.
#
# This is the MANDATORY gate before any automated jp-switch. Exit 0 = exact,
# 1 = mismatch (caller must freeze jp / not deploy).
#
# Usage:  bash roundtrip_verify.sh <src_checkpoint_dir> <intermediate_jp> [workdir]
# Example: bash roundtrip_verify.sh restart/_changejp_bak/step_9500001_jp64 32
#
# Read-only w.r.t. the source; writes only under <workdir> (auto-cleaned).
# =============================================================================
set -euo pipefail
SRC="${1:?usage: roundtrip_verify.sh <src_ckpt_dir> <intermediate_jp> [workdir]}"
MID="${2:?intermediate jp}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPART="$(cd "$HERE/.." && pwd)/repartition_jp.py"
WORK="${3:-/work/$USER/rtv_$$}"

[ -f "$SRC/metadata.dat" ] || { echo "[rtv] FATAL: no $SRC/metadata.dat"; exit 2; }
[ -f "$REPART" ]          || { echo "[rtv] FATAL: repartition tool not found: $REPART"; exit 2; }
ORIG_JP=$(grep '^mpi_rank_count=' "$SRC/metadata.dat" | head -1 | cut -d= -f2)
[ -n "$ORIG_JP" ] || { echo "[rtv] FATAL: cannot read mpi_rank_count"; exit 2; }

mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT
echo "[rtv] $SRC : jp $ORIG_JP -> $MID -> $ORIG_JP   (work=$WORK)"

python3 "$REPART" --src "$SRC"    --dst "$WORK/A" --new-jp "$MID"     >"$WORK/a.log" 2>&1 \
    || { echo "[rtv] FATAL: repartition A (jp$ORIG_JP->$MID) failed:"; tail -5 "$WORK/a.log"; exit 3; }
python3 "$REPART" --src "$WORK/A" --dst "$WORK/B" --new-jp "$ORIG_JP" >"$WORK/b.log" 2>&1 \
    || { echo "[rtv] FATAL: repartition B ($MID->jp$ORIG_JP) failed:"; tail -5 "$WORK/b.log"; exit 3; }

set +e
python3 - "$SRC" "$WORK/B" <<'PY'
import sys, os, numpy as np
src, rt = sys.argv[1], sys.argv[2]
meta = dict(l.strip().split('=',1) for l in open(f"{src}/metadata.dat") if '=' in l)
jp = int(meta['mpi_rank_count']); NX6, NYD6, NZ6 = map(int, meta['grid_dims'].split(','))
N = NYD6 - 7
fields = [f"f{q:02d}" for q in range(19)] + ["rho"]
fields += sorted({f[:-6] for f in os.listdir(src)
                  if f.startswith('sum_') and f.endswith('_0.bin')})
worst = 0.0; wf = None; nchk = 0
for fld in fields:
    for r in range(jp):
        a = np.fromfile(f"{src}/{fld}_{r}.bin").reshape((NYD6, NZ6, NX6))[3:3+N]
        b = np.fromfile(f"{rt}/{fld}_{r}.bin").reshape((NYD6, NZ6, NX6))[3:3+N]
        d = float(np.max(np.abs(a - b))); nchk += 1
        if d > worst: worst = d; wf = f"{fld}_{r}"
print(f"[rtv] owned-field round-trip: {nchk} slabs, {len(fields)} fields, worst |Δ|={worst:.3e} ({wf})")
sys.exit(0 if worst == 0.0 else 1)
PY
rc=$?
set -e

if [ $rc -eq 0 ]; then
    echo "[rtv] PASS — owned field byte-exact (Δ=0); repartition is safe to deploy."
else
    echo "[rtv] FAIL — repartition is NOT byte-exact in the owned field; jp-switch must stay frozen."
fi
exit $rc
