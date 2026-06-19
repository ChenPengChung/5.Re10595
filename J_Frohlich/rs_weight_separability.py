#!/usr/bin/env python3
"""
rs_weight_separability.py  —  OFFLINE, READ-ONLY analysis.

Question (user direction): keep PRECOMPUTED weights; regularize the ITB (r,s)
field to cut memory while staying a pure table-lookup kernel. Measure the error
on the TRUE Lagrange weights (not on the (r,s) coordinates).

Faithful Python port of the shipping ITB precompute:
  ITB_GeomEffHost / ITB_YZShapeHost / ITB_EvaluateMapHost / ITB_NewtonSolveHost
(itblbm/isoparametric_precompute.h, GHOST_EXTRAP_ORDER=2, ITB_YZ_ORDER=7).

It does NOT touch the running job, restart/, or any solver file. Reads one .dat.
"""
import numpy as np, re, sys, os

# ---- params (from variables.h) ----
DAT   = "adaptive_3.fine grid_I641_J321_s0.950000.dat"
NY, NZ, NX = 641, 321, 321          # streamwise(j), wall-normal(k), spanwise(i)
LY, LZ, LX = 9.0, 3.036, 4.5
CFL        = 0.5
ORDER, HALF, GHOST = 7, 3, 2
NZ6 = NZ + 6                         # 327; interior C wall-normal index in [3, NZ6-4]=[3,323]
NX6 = NX + 6
NODES = np.arange(ORDER) - HALF      # [-3,-2,-1,0,1,2,3]
# 8 moving y-z classes (ey=streamwise, ez=wall-normal)
CLASSES = [(1,0),(-1,0),(0,1),(0,-1),(1,1),(-1,1),(1,-1),(-1,-1)]

# ---- parse tecplot .dat ----
def parse_dat(path):
    txt = open(path, encoding="latin-1").read().splitlines()
    ni = nj = None; start = 0
    for idx, l in enumerate(txt):
        u = l.upper()
        if "I=" in u and "J=" in u:
            ni = int(re.search(r"I=\s*(\d+)", u).group(1))
            nj = int(re.search(r"J=\s*(\d+)", u).group(1))
            start = idx + 1; break
    vals = []
    for l in txt[start:]:
        s = l.split()
        if len(s) >= 2:
            try: vals.append((float(s[0]), float(s[1])))
            except ValueError: pass
    v = np.array(vals)
    x = v[:, 0].reshape(nj, ni)   # [k_wallnormal, j_streamwise] streamwise phys
    y = v[:, 1].reshape(nj, ni)   # wall-normal phys
    return x, y, ni, nj

xdat, ydat, ni, nj = parse_dat(DAT)
assert ni == NY and nj == NZ, f"dat {ni}x{nj} != {NY}x{NZ}"
Xint = xdat.T.copy()   # [NY, NZ] streamwise phys, [j][k_interior]
Zint = ydat.T.copy()   # [NY, NZ] wall-normal phys

# ---- ghost-consistent geometry getter (ITB_GeomEffHost, order 2), per streamwise column ----
def geom_axis_col(arr_col, kc):
    # arr_col: interior values length NZ (k_interior 0..NZ-1); kc: C wall-normal index
    if kc < 3:
        d = 3.0 - kc
        f3, f4, f5 = arr_col[0], arr_col[1], arr_col[2]
        return (d+1)*(d+2)*0.5*f3 - d*(d+2)*f4 + d*(d+1)*0.5*f5
    if kc > NZ6 - 4:
        d = float(kc - (NZ6 - 4))
        f3, f4, f5 = arr_col[NZ-1], arr_col[NZ-2], arr_col[NZ-3]
        return (d+1)*(d+2)*0.5*f3 - d*(d+2)*f4 + d*(d+1)*0.5*f5
    return arr_col[kc - 3]

P = NY - 1   # streamwise period in nodes (640 cells)
def wrap_j(j):
    n = j // P
    return j - n * P, n * LY

# ---- build padded full grids Xfull/Zfull[jj=0..NY+5][kk=0..NZ6-1]; access via +3 row offset
Xfull = np.zeros((NY + 6, NZ6)); Zfull = np.zeros((NY + 6, NZ6))
for jj in range(NY + 6):
    jw, off = wrap_j(jj - 3)
    xc, zc = Xint[jw], Zint[jw]
    for kk in range(NZ6):
        Xfull[jj, kk] = geom_axis_col(xc, kk) + off
        Zfull[jj, kk] = geom_axis_col(zc, kk)
def Xg(j, kc): return Xfull[j + 3, kc]   # j any int in stored range, kc C-index
def Zg(j, kc): return Zfull[j + 3, kc]

# ---- dt from contravariant CFL (reproduce ComputeGlobalTimeStep) ----
def compute_dt():
    mx = 0.0
    for j in range(NY):
        for kc in range(3, NZ6 - 3):    # interior k
            Xj = 0.5 * (Xg(j+1, kc) - Xg(j-1, kc))
            Zj = 0.5 * (Zg(j+1, kc) - Zg(j-1, kc))
            Xk = 0.5 * (Xg(j, kc+1) - Xg(j, kc-1))
            Zk = 0.5 * (Zg(j, kc+1) - Zg(j, kc-1))
            J = Xj * Zk - Xk * Zj
            if abs(J) < 1e-30: continue
            xiX, xiZ = Zk / J, -Xk / J
            zeX, zeZ = -Zj / J, Xj / J
            for (ey, ez) in CLASSES:
                c1 = abs(ey * xiX + ez * xiZ)
                c2 = abs(ey * zeX + ez * zeZ)
                if c1 > mx: mx = c1
                if c2 > mx: mx = c2
    dx = LX / (NX6 - 7)
    mx = max(mx, 1.0 / dx)
    return CFL / mx

# ---- Lagrange shape + derivative (vectorized over node array) ----
def shape7(x):
    x = np.asarray(x, float); N = x.shape[0]
    L = np.ones((N, ORDER)); dL = np.zeros((N, ORDER))
    for a in range(ORDER):
        xa = NODES[a]
        for b in range(ORDER):
            if b == a: continue
            L[:, a] *= (x - NODES[b]) / (xa - NODES[b])
        acc = np.zeros(N)
        for m in range(ORDER):
            if m == a: continue
            term = np.full(N, 1.0 / (xa - NODES[m]))
            for b in range(ORDER):
                if b == a or b == m: continue
                term *= (x - NODES[b]) / (xa - NODES[b])
            acc += term
        dL[:, a] = acc
    return L, dL

# ---- vectorized Newton inverse over all interior nodes for one class ----
def newton_class(ey, ez, dt):
    js, ks = [], []
    for j in range(NY):
        for kc in range(3, NZ6 - 3):
            js.append(j); ks.append(kc)
    js = np.array(js); ks = np.array(ks); N = js.size
    # departure (physical straight line)
    yd = Xfull[js + 3, ks] - ey * dt
    zd = Zfull[js + 3, ks] - ez * dt
    # stencil coords Xst[N,7,7] = Xg(j-3+a, kc-3+b) = Xfull[j+a, kc-3+b]
    Xst = np.empty((N, ORDER, ORDER)); Zst = np.empty((N, ORDER, ORDER))
    for a in range(ORDER):
        rows = js + a                     # = (j-3+a)+3
        for b in range(ORDER):
            cols = ks - 3 + b
            Xst[:, a, b] = Xfull[rows, cols]
            Zst[:, a, b] = Zfull[rows, cols]
    r = np.zeros(N); s = np.zeros(N); conv = np.zeros(N, bool); iters = np.zeros(N, int)
    active = np.ones(N, bool)
    for it in range(1, 26):
        idx = np.where(active)[0]
        if idx.size == 0: break
        Lr, dLr = shape7(r[idx]); Ls, dLs = shape7(s[idx])
        Xs = Xst[idx]; Zs_ = Zst[idx]
        Y  = np.einsum('na,nb,nab->n', Lr,  Ls,  Xs)
        Z  = np.einsum('na,nb,nab->n', Lr,  Ls,  Zs_)
        Yr = np.einsum('na,nb,nab->n', dLr, Ls,  Xs)
        Ys = np.einsum('na,nb,nab->n', Lr,  dLs, Xs)
        Zr = np.einsum('na,nb,nab->n', dLr, Ls,  Zs_)
        Zsd= np.einsum('na,nb,nab->n', Lr,  dLs, Zs_)
        Ry = yd[idx] - Y; Rz = zd[idx] - Z
        det = Yr * Zsd - Ys * Zr
        ok_det = np.abs(det) > 1e-14
        dr = np.where(ok_det, ( Zsd * Ry - Ys * Rz) / np.where(ok_det, det, 1), 0.0)
        ds = np.where(ok_det, (-Zr  * Ry + Yr * Rz) / np.where(ok_det, det, 1), 0.0)
        scale = np.where((np.abs(dr) + np.abs(ds)) > 1.0, 0.5, 1.0)
        r[idx] += scale * dr; s[idx] += scale * ds
        iters[idx] = it
        upd = scale * (np.abs(dr) + np.abs(ds))
        res = np.abs(Ry) + np.abs(Rz)
        done = (upd < 1e-12) | (res < 1e-11) | (~ok_det)
        cd = idx[done]; conv[cd] = (res[done] < 1e-9) | (upd[done] < 1e-11)
        active[cd] = False
    return js, ks, r, s, conv, iters

def folded_ws(kc, s_val):
    """ITB_FoldKWeightsHost (order 2) -> (k_idx[7], folded_ws[7])."""
    Ls, _ = shape7(np.array([s_val])); raw = Ls[0]
    raw_k0 = kc - HALF
    phys_k0 = min(max(raw_k0, 3), NZ6 - 3 - ORDER)
    kidx = [phys_k0 + b for b in range(ORDER)]
    fw = np.zeros(ORDER)
    for b in range(ORDER):
        kg = raw_k0 + b; w = raw[b]
        if kg < 3:
            d = 3.0 - kg
            fw[3-phys_k0] += w*(d+1)*(d+2)*0.5; fw[4-phys_k0] += w*(-d*(d+2)); fw[5-phys_k0] += w*d*(d+1)*0.5
        elif kg > NZ6 - 4:
            d = float(kg - (NZ6-4))
            fw[(NZ6-4)-phys_k0]+=w*(d+1)*(d+2)*0.5; fw[(NZ6-5)-phys_k0]+=w*(-d*(d+2)); fw[(NZ6-6)-phys_k0]+=w*d*(d+1)*0.5
        else:
            fw[kg - phys_k0] += w
    return kidx, fw

# ============================ run ============================
print("="*70); print("ITB (r,s) -> Lagrange-weight separability analysis"); print("="*70)
print(f"grid {NY}x{NZ} (streamwise x wall-normal), s=0.95, GHOST={GHOST}")
print("computing dt (contravariant CFL)...", flush=True)
dt = compute_dt()
print(f"  dt_global (recomputed) = {dt:.6e}   [CFL={CFL}]")

# near-wall band (in interior k_int) for worst-case reporting
def is_nearwall(kc):  # within ~12 interior layers of either wall
    kint = kc - 3
    return (kint < 12) or (kint > NZ - 13)

rows = []
print("\nclass    avg_it max_it  conv%   |r|>1   |s|>1   min|detJ|note")
for (ey, ez) in CLASSES:
    js, ks, r, s, conv, iters = newton_class(ey, ez, dt)
    n = js.size
    agt = np.sum(np.abs(r) > 1.0); sgt = np.sum(np.abs(s) > 1.0)
    print(f"({ey:+d},{ez:+d})   {iters.mean():5.2f}  {iters.max():3d}   {100*conv.mean():5.1f}  "
          f"{agt:6d}  {sgt:6d}")
    rows.append((ey, ez, js, ks, r, s, conv))

print("\n--- self-validation (partition of unity on folded weights, sample) ---")
ey, ez, js, ks, r, s, conv = rows[2]   # class (0,1): pure wall-normal -> stresses s/fold
bad = 0
for t in range(0, js.size, 4001):
    Lr, _ = shape7(np.array([r[t]])); wr = Lr[0]
    kidx, fw = folded_ws(ks[t], s[t])
    if abs(wr.sum()-1) > 1e-12 or abs(fw.sum()-1) > 1e-12: bad += 1
print(f"  sampled {len(range(0,js.size,4001))} nodes; partition-of-unity violations(>1e-12) = {bad}")

# ---------------- separability of the (r,s) FIELDS ----------------
print("\n" + "="*70)
print("RANK-1 SEPARABLE test:  r ~ r_bar(j) only ,  s ~ s_bar(k) only")
print("  metric: foot deviation (lattice units) = 1st-moment weight error")
print("="*70)
print("class     max|dr_k|  NWmax|dr_k|   max|ds_j|  NWmax|ds_j|   (NW=near-wall)")
for (ey, ez, js, ks, r, s, conv) in rows:
    J2 = np.zeros((NY, NZ)); S2 = np.zeros((NY, NZ)); M = np.zeros((NY, NZ), bool)
    for t in range(js.size):
        J2[js[t], ks[t]-3] = r[t]; S2[js[t], ks[t]-3] = s[t]; M[js[t], ks[t]-3] = conv[t]
    rbar_j = J2.mean(axis=1, keepdims=True)        # r collapsed over k  -> per j
    sbar_k = S2.mean(axis=0, keepdims=True)         # s collapsed over j  -> per k
    dr = np.abs(J2 - rbar_j)                         # r's residual k-dependence
    ds = np.abs(S2 - sbar_k)                         # s's residual j-dependence (hill modulation)
    nw = np.zeros(NZ, bool); nw[:12] = True; nw[-12:] = True
    print(f"({ey:+d},{ez:+d})   {dr.max():9.3e}  {dr[:,nw].max():9.3e}   "
          f"{ds.max():9.3e}  {ds[:,nw].max():9.3e}")

# ---------------- SVD rank of (r,s) fields ----------------
print("\n" + "="*70)
print("LOW-RANK (SVD) of the (r,s) fields per class: singular-value energy")
print("  rankR_err = ||field - rankR||_inf  (lattice units)")
print("="*70)
print("class     field   rank1_err   rank2_err   rank3_err")
for (ey, ez, js, ks, r, s, conv) in rows:
    R2 = np.zeros((NY, NZ)); S2 = np.zeros((NY, NZ))
    for t in range(js.size):
        R2[js[t], ks[t]-3] = r[t]; S2[js[t], ks[t]-3] = s[t]
    for name, F in (("r", R2), ("s", S2)):
        U, sv, Vt = np.linalg.svd(F, full_matrices=False)
        def rkerr(R):
            approx = (U[:, :R] * sv[:R]) @ Vt[:R]
            return np.abs(F - approx).max()
        print(f"({ey:+d},{ez:+d})   {name}      {rkerr(1):9.3e}  {rkerr(2):9.3e}  {rkerr(3):9.3e}")

# ---------------- memory accounting ----------------
print("\n" + "="*70); print("MEMORY (per class, global table; bytes)"); print("="*70)
nodes = NY * NZ
expanded = nodes * (4 + 7*4 + 7*8 + 7*8 + 1)     # ITB_YZCoeff ~145->pad152
exp_pad  = nodes * 152
compact  = nodes * 16                              # (r,s) two doubles
sep_rank1 = NY*7*8 + NZ*(7*8 + 7*4)               # wr(j)[7]d + ws(k)[7]d + kidx(k)[7]i
print(f"  expanded ITB_YZCoeff   : {exp_pad/1e6:8.2f} MB   (152 B/node)")
print(f"  compact (r,s) only     : {compact/1e6:8.2f} MB   ({compact/nodes:.0f} B/node)  [{exp_pad/compact:.1f}x]")
print(f"  rank-1 separable tables: {sep_rank1/1e6:8.2f} MB                       [{exp_pad/sep_rank1:.0f}x]")
print("  (rank-1 separable keeps a PURE table-lookup kernel: wr[j] (x) ws[k])")
print("\nDONE.")
