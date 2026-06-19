#!/usr/bin/env python3
"""Direct WEIGHT-SPACE error (user's metric). Reuses the faithful ITB port.
Measures, on the true folded Lagrange weights W = wr(x)ws:
  (A) rank-1 separable  wr~wr(j), ws~ws(k)  -> L1 weight error / node (pure-lookup path)
  (B) low-rank of the WEIGHT tensor itself   -> 'precomputed factored weights' path
"""
import numpy as np
import importlib.util
spec = importlib.util.spec_from_file_location("m", "rs_weight_separability.py")
# we only need the helpers + Newton; re-exec the module up to run is heavy, so re-import pieces:
import rs_weight_separability as M  # runs full analysis again (provides dt, helpers)

dt = M.dt
shape7 = M.shape7
folded_ws = M.folded_ws
newton_class = M.newton_class
NY, NZ, NZ6, ORDER = M.NY, M.NZ, M.NZ6, M.ORDER
nw = np.zeros(NZ, bool); nw[:12] = True; nw[-12:] = True

print("\n" + "#"*70)
print("# DIRECT WEIGHT-SPACE ERROR  (true folded Lagrange weights)")
print("#"*70)

def weights_field(r, s, js, ks):
    """exact wr[NY,NZ,7] (from r) and ws_folded[NY,NZ,7] (from s,k)."""
    WR = np.zeros((NY, NZ, ORDER)); WS = np.zeros((NY, NZ, ORDER))
    Lr, _ = shape7(r)
    for t in range(js.size):
        WR[js[t], ks[t]-3] = Lr[t]
        _, fw = folded_ws(ks[t], s[t])
        WS[js[t], ks[t]-3] = fw
    return WR, WS

print("\n(A) RANK-1 SEPARABLE weight error  (collapse r->mean_k, s->mean_j); pure-lookup kernel")
print("    L1node = sum_a|dwr| + sum_b|dws|   (<=1 ~ total weight redistributed)")
print("class     L1_max     L1_NWmax    (NW=near-wall 12 layers)")
res_rank1 = {}
for (ey, ez) in M.CLASSES:
    js, ks, r, s, conv, iters = newton_class(ey, ez, dt)
    WR, WS = weights_field(r, s, js, ks)
    # rank-1 separable surrogates
    rbar = (np.zeros((NY,NZ))); sbar = np.zeros((NY,NZ))
    R2 = np.zeros((NY,NZ)); S2 = np.zeros((NY,NZ))
    for t in range(js.size): R2[js[t],ks[t]-3]=r[t]; S2[js[t],ks[t]-3]=s[t]
    rbar_j = R2.mean(axis=1, keepdims=True) * np.ones((1,NZ))   # per j
    sbar_k = S2.mean(axis=0, keepdims=True) * np.ones((NY,1))   # per k
    # surrogate weights
    WRb = shape7(rbar_j.reshape(-1))[0].reshape(NY,NZ,ORDER)
    WSb = np.zeros((NY,NZ,ORDER))
    for k in range(NZ):
        _, fw = folded_ws(k+3, sbar_k[0,k]); WSb[:,k]=fw
    L1 = np.abs(WR-WRb).sum(-1) + np.abs(WS-WSb).sum(-1)
    print(f"({ey:+d},{ez:+d})   {L1.max():9.3e}  {L1[:,nw].max():9.3e}")
    res_rank1[(ey,ez)] = (WR, WS)

print("\n(B) LOW-RANK of the WEIGHT TENSOR  (per-component SVD of W[:,:,a]); factored-weights kernel")
print("    err = max_a || W[:,:,a] - rankR ||_inf   (max abs weight error)")
print("class    comp   rank1      rank2      rank3      rank4")
for (ey, ez) in M.CLASSES:
    WR, WS = res_rank1[(ey,ez)]
    for name, W in (("wr", WR), ("ws", WS)):
        errs = {R: 0.0 for R in (1,2,3,4)}
        for a in range(ORDER):
            F = W[:,:,a]
            U, sv, Vt = np.linalg.svd(F, full_matrices=False)
            for R in (1,2,3,4):
                approx = (U[:,:R]*sv[:R]) @ Vt[:R]
                errs[R] = max(errs[R], np.abs(F-approx).max())
        print(f"({ey:+d},{ez:+d})  {name}   {errs[1]:9.3e} {errs[2]:9.3e} {errs[3]:9.3e} {errs[4]:9.3e}")

# memory for factored-weights path (rank R, both wr & ws, 7 comps)
print("\nMEMORY factored-weights (per class):  7*R*(NY+NZ)*8 bytes per factor table, x2 (wr,ws)")
NYNZ = NY*NZ
for R in (2,3,4):
    fac = 2 * ORDER * R * (NY+NZ) * 8
    print(f"  rank-{R}: {fac/1e6:6.3f} MB   vs expanded {NYNZ*152/1e6:.1f} MB  [{NYNZ*152/fac:.0f}x]")
print("DONE.")
