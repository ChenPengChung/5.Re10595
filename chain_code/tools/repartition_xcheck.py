#!/usr/bin/env python3
"""Cross-check repartition correctness WITHOUT trusting repartition_jp.py's own conventions.

Reconstruct the global PHYSICAL field from a checkpoint dir using the SOLVER's documented
linear index  idx = jl*NX6*NZ6 + k*NX6 + i  (fileIO.h:923,943) — i.e. reshape(NYD6,NZ6,NX6),
take interior rows jl in [BFR, BFR+CHUNK) and physical k/i in [BFR, BFR+NZ)/[BFR, BFR+NX).
If src (old jp) and dst (new jp) yield the SAME physical field bit-for-bit, the repartition
preserved the flow field across the jp change (a wrong axis order would FAIL this).

Usage: repartition_xcheck.py [--stats] <src_ckpt_dir> <dst_ckpt_dir> [field ...]
  --stats : additionally cross-check ALL 36 turbulence accumulators (sum_*); FAIL if any of
            the 36 is absent in either checkpoint (catches a partial/incomplete stats migration).
"""
import sys, os
import numpy as np

BFR = 3

# Mirror of repartition_jp.py / fileIO.h:299-317 — the 36 accumulators the solver writes.
EXPECTED_STAT_BASES = [
    "sum_u", "sum_v", "sum_w",
    "sum_uu", "sum_uv", "sum_uw", "sum_vv", "sum_vw", "sum_ww",
    "sum_uuu", "sum_uuv", "sum_uuw", "sum_uvv", "sum_uvw", "sum_uww",
    "sum_vvv", "sum_vvw", "sum_vww", "sum_www",
    "sum_P", "sum_PP", "sum_Pu", "sum_Pv", "sum_Pw",
    "sum_dudx2", "sum_dudy2", "sum_dudz2",
    "sum_dvdx2", "sum_dvdy2", "sum_dvdz2",
    "sum_dwdx2", "sum_dwdy2", "sum_dwdz2",
    "sum_ox", "sum_oy", "sum_oz",
]

def meta(d):
    m = {}
    for l in open(os.path.join(d, 'metadata.dat')):
        if '=' in l:
            k, v = l.strip().split('=', 1); m[k] = v
    return m

def gphys(d, fname):
    m = meta(d)
    jp = int(m['mpi_rank_count'])
    NX6, NYD6, NZ6 = (int(x) for x in m['grid_dims'].split(','))
    NX, NZ = NX6 - 6, NZ6 - 6
    chunk = NYD6 - 7
    NY = chunk * jp + 1
    G = np.empty((NY - 1, NZ, NX), dtype=np.float64)   # unique physical rows (jp*chunk = NY-1)
    for r in range(jp):
        slab = np.fromfile(os.path.join(d, '%s_%d.bin' % (fname, r)),
                           dtype=np.float64).reshape(NYD6, NZ6, NX6)   # SOLVER layout
        G[r * chunk:(r + 1) * chunk, :, :] = slab[BFR:BFR + chunk, BFR:BFR + NZ, BFR:BFR + NX]
    return G

def main():
    argv = sys.argv[1:]
    stats = '--stats' in argv
    argv = [a for a in argv if a != '--stats']
    src, dst = argv[0], argv[1]
    fields = argv[2:] or ['f00', 'rho']
    ok = True
    if stats:
        miss = [b for b in EXPECTED_STAT_BASES
                if not (os.path.exists(os.path.join(src, b + '_0.bin'))
                        and os.path.exists(os.path.join(dst, b + '_0.bin')))]
        if miss:
            print("  STATS INCOMPLETE: %d/36 accumulator(s) absent in src and/or dst: %s"
                  % (len(miss), ', '.join(miss)))
            ok = False
        fields += [b for b in EXPECTED_STAT_BASES if b not in miss and b not in fields]
        print("  --stats: cross-checking %d/36 accumulators present in both" % (36 - len(miss)))
    print("src=%s (jp=%s)  dst=%s (jp=%s)" % (src, meta(src)['mpi_rank_count'], dst, meta(dst)['mpi_rank_count']))
    for f in fields:
        a = gphys(src, f); b = gphys(dst, f)
        # strictly-unique physical nodes exclude the spanwise periodic-duplicate column
        # (i=NX-1 ≡ i=0). repartition re-syncs that duplicate bit-identically (correct, per
        # interp_checkpoint.py); the original checkpoint may have it ~1e-9 off, which is benign
        # (re-synced by the solver on load). So the PASS criterion is the strictly-unique nodes.
        au, bu = a[:, :, :-1], b[:, :, :-1]
        same_u = (au.shape == bu.shape) and np.array_equal(au, bu)
        full_diff = float(np.max(np.abs(a - b))) if a.shape == b.shape else -1.0
        dupcol_diff = float(np.max(np.abs(a[:, :, -1] - b[:, :, -1]))) if a.shape == b.shape else -1.0
        print("  %-5s unique-nodes bit-identical=%s | full maxdiff=%.3e (all on periodic-dup col=%.3e)"
              % (f, same_u, full_diff, dupcol_diff))
        ok = ok and same_u
    print("RESULT:", "PASS — physical field preserved bit-for-bit across jp change" if ok
          else "FAIL — field altered by repartition!")
    sys.exit(0 if ok else 1)

if __name__ == '__main__':
    main()
