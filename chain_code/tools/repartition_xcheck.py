#!/usr/bin/env python3
"""Cross-check repartition correctness WITHOUT trusting repartition_jp.py's own conventions.

Reconstruct the global PHYSICAL field from a checkpoint dir using the SOLVER's documented
linear index  idx = jl*NX6*NZ6 + k*NX6 + i  (fileIO.h:923,943) — i.e. reshape(NYD6,NZ6,NX6),
take interior rows jl in [BFR, BFR+CHUNK) and physical k/i in [BFR, BFR+NZ)/[BFR, BFR+NX).
If src (old jp) and dst (new jp) yield the SAME physical field bit-for-bit, the repartition
preserved the flow field across the jp change (a wrong axis order would FAIL this).

Usage: repartition_xcheck.py <src_ckpt_dir> <dst_ckpt_dir> [field ...]
"""
import sys, os
import numpy as np

BFR = 3

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
    src, dst = sys.argv[1], sys.argv[2]
    fields = sys.argv[3:] or ['f00', 'rho']
    ok = True
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
