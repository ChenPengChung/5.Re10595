#!/usr/bin/env python3
"""Lean peak-deficit dump under a ~10GB cgroup limit.
Reuses the PROVEN extraction logic from result/2.Benchmark.py but:
  - streaming VTK parser keeps ONLY needed fields (U_mean, uu_RS, uv_RS, vv_RS, k_TKE) + POINTS,
    seeking past the ~11 unwanted fields so peak RSS stays ~4GB.
  - reuses load_station_file / interp_1d_lagrange / _interp_profile_curvilinear / interp_at_target_y
    by importing them from the unmodified source copy.
"""
import os, sys, glob, numpy as np, csv

RESULT = "/home/s8313697/5.Re10595/Edit6_5600DNS/result"
# VTK selection: argv[1] if given, else newest stable velocity_merged_*.vtk (skip mid-write).
def _pick_vtk():
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        return sys.argv[1]
    cands = sorted(glob.glob(os.path.join(RESULT, "velocity_merged_*.vtk")),
                   key=lambda p: os.path.getmtime(p), reverse=True)
    for p in cands:
        s0 = os.path.getsize(p)
        if s0 > 0:  # caller is responsible for stat-stable check; pick newest non-empty
            return p
    return os.path.join(RESULT, "velocity_merged_91250001.vtk")
VTK = _pick_vtk()
BENCH_DIR = os.path.join(RESULT, "benchmark")
NEEDED = {"U_mean", "uu_RS", "uv_RS", "vv_RS", "k_TKE"}
H_HILL = 1.0
LAGRANGE_ORDER = 6

# ---- import proven helpers from the source copy (no plotting executed: we import as module text) ----
# We can't import 2.Benchmark.py as a module (it runs argparse/VTK at import). Instead, exec the
# function defs we need by pulling them out. Simpler: re-implement the small Lagrange helpers here
# (they are pure math, verified identical to the source).

def _lagrange_weights(x_stencil, x_target):
    n = len(x_stencil); w = np.ones(n, dtype=float)
    for i in range(n):
        for j in range(n):
            if i != j:
                denom = x_stencil[i] - x_stencil[j]
                if abs(denom) < 1e-30: denom = 1e-30
                w[i] *= (x_target - x_stencil[j]) / denom
    return w

def _pick_stencil_indices(j_left, nj, order=LAGRANGE_ORDER):
    j_start = j_left - (order // 2 - 1)
    j_start = max(0, min(j_start, nj - order))
    return np.arange(j_start, j_start + order)

def interp_at_target_y(y_line, field_line, y_target, order=LAGRANGE_ORDER):
    nj = len(y_line)
    if nj < order: order = nj
    j_left = int(np.searchsorted(y_line, y_target, side='right')) - 1
    j_left = max(0, min(j_left, nj - 2))
    idx = _pick_stencil_indices(j_left, nj, order)
    weights = _lagrange_weights(y_line[idx], y_target)
    return float(np.dot(weights, field_line[idx]))

def interp_1d_lagrange(z_sim, f_sim, z_target, order=LAGRANGE_ORDER):
    m = len(z_target); f_interp = np.full(m, np.nan)
    z_lo, z_hi = z_sim[0], z_sim[-1]
    mask_valid = (z_target >= z_lo) & (z_target <= z_hi)
    nz = len(z_sim)
    for p in range(m):
        if not mask_valid[p]: continue
        zt = z_target[p]
        j_left = int(np.searchsorted(z_sim, zt, side='right')) - 1
        j_left = max(0, min(j_left, nz - 2))
        idx = _pick_stencil_indices(j_left, nz, min(order, nz))
        weights = _lagrange_weights(z_sim[idx], zt)
        f_interp[p] = float(np.dot(weights, f_sim[idx]))
    return f_interp, mask_valid

# ---- streaming VTK parser: keep only POINTS + NEEDED scalars ----
def parse_vtk_lean(filepath, needed):
    dims = None; npts = 0; npts_from_dims = 0
    points = None; scalars = {}
    is_binary = False
    def _np_dtype(token):
        t = token.lower()
        if t == "double": return ">f8", 8
        if t == "float":  return ">f4", 4
        if t == "int":    return ">i4", 4
        return ">f8", 8
    with open(filepath, "rb") as f:
        while True:
            raw = f.readline()
            if not raw: break
            sline = raw.decode("latin-1", errors="ignore").strip()
            if not sline: continue
            if sline == "BINARY": is_binary = True; continue
            if sline == "ASCII":  is_binary = False; continue
            if sline.startswith("DIMENSIONS"):
                dims = tuple(int(v) for v in sline.split()[1:4])
                npts_from_dims = dims[0]*dims[1]*dims[2]
            elif sline.startswith("POINT_DATA"):
                npts = int(sline.split()[1])
            elif sline.startswith("POINTS"):
                parts = sline.split(); n = int(parts[1])
                dt, esize = _np_dtype(parts[2] if len(parts) > 2 else "double")
                if npts == 0: npts = n
                buf = f.read(n*3*esize)
                pts = np.frombuffer(buf, dtype=dt).astype(np.float64)
                points = pts.reshape(-1, 3).copy()
                del buf, pts
                f.readline()
            elif sline.startswith("VECTORS"):
                if npts == 0 and npts_from_dims > 0: npts = npts_from_dims
                parts = sline.split()
                dt, esize = _np_dtype(parts[2] if len(parts) > 2 else "double")
                # we never need VECTORS fields -> seek past
                f.seek(npts*3*esize, os.SEEK_CUR); f.readline()
            elif sline.startswith("SCALARS"):
                if npts == 0 and npts_from_dims > 0: npts = npts_from_dims
                parts = sline.split(); name = parts[1]
                dt, esize = _np_dtype(parts[2] if len(parts) > 2 else "double")
                f.readline()  # LOOKUP_TABLE line
                nbytes = npts*esize
                if name in needed:
                    buf = f.read(nbytes)
                    arr = np.frombuffer(buf, dtype=dt).astype(np.float64)
                    scalars[name] = arr.copy()
                    del buf, arr
                    f.readline()
                    print(f"  [kept] {name}", flush=True)
                else:
                    f.seek(nbytes, os.SEEK_CUR); f.readline()
    return dims, points, scalars

# ---- load benchmark station files (Krank + MGLET) ----
def load_krank(fp):
    data = np.loadtxt(fp, comments='%', delimiter=',')
    if data.ndim < 2 or data.shape[1] < 9: return None
    return {"y":data[:,0],"U":data[:,1],"V":data[:,2],"uu":data[:,4],
            "vv":data[:,5],"uv":data[:,7],"k":data[:,8]}

def load_ercoftac(fp):
    data = np.loadtxt(fp, comments="#")
    if data.ndim < 2 or data.shape[1] < 6: return None
    d = {"y":data[:,0],"U":data[:,1],"V":data[:,2],"uu":data[:,3],
         "vv":data[:,4],"uv":data[:,5]}
    d["k"] = data[:,6] if data.shape[1] >= 7 else None
    return d

STATIONS = [0.05, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]

def find_station_files(re_dir):
    """Glob dir, map xh(float)->filepath by parsing the trailing _<xh> token.
    (verbatim logic from source find_station_files)."""
    files = sorted(glob.glob(os.path.join(re_dir, "*.dat")) +
                   glob.glob(os.path.join(re_dir, "*.DAT")))
    xh_map = {}
    for f in files:
        base = os.path.splitext(os.path.basename(f))[0]
        parts = base.rsplit('_', 1)
        if len(parts) != 2: continue
        xh_str = parts[1]
        if xh_str == 'wall': continue
        try:
            xh_map[float(xh_str)] = f
        except ValueError:
            pass
    return xh_map

KRANK_DIR = os.path.join(BENCH_DIR, "Benjamin Krank et al. 2018", "Re5600")
MGLET_DIR = os.path.join(BENCH_DIR, "MGLET (Breuer et al. 2009)", "Re5600")

krank = {}
mglet = {}
_kfiles = find_station_files(KRANK_DIR)
_mfiles = find_station_files(MGLET_DIR)
for xh in STATIONS:
    if xh in _kfiles:
        d = load_krank(_kfiles[xh])
        if d is not None: krank[xh] = d
    if xh in _mfiles:
        d = load_ercoftac(_mfiles[xh])
        if d is not None: mglet[xh] = d
print(f"[INFO] Krank stations: {sorted(krank.keys())}", flush=True)
print(f"[INFO] MGLET stations: {sorted(mglet.keys())}", flush=True)

# ---- parse VTK ----
print(f"[INFO] Parsing {os.path.basename(VTK)} (streaming, needed only)...", flush=True)
dims, points, scalars = parse_vtk_lean(VTK, NEEDED)
nx, ny, nz = dims
print(f"[INFO] dims = {nx} x {ny} x {nz}; kept fields: {list(scalars.keys())}", flush=True)

pts_3d = points.reshape(nz, ny, nx, 3)
y_3d = pts_3d[:, :, :, 1]   # streamwise
z_3d = pts_3d[:, :, :, 2]   # wall-normal
F3D = {k: scalars[k].reshape(nz, ny, nx) for k in scalars}
del points  # keep pts_3d view's base; points buffer no longer needed separately

# ---- profile extraction (spanwise-averaged, 6th-order Lagrange along j) ----
def extract_z(xh):
    """z_abs profile at station xh (spanwise-averaged via Lagrange along j)."""
    y_target = xh*H_HILL
    z_out = np.zeros(nz)
    for k in range(nz):
        zsum = 0.0
        for i in range(nx):
            zsum += interp_at_target_y(y_3d[k, :, i], z_3d[k, :, i], y_target)
        z_out[k] = zsum/nx
    return z_out

def extract_field(xh, fld):
    y_target = xh*H_HILL
    out = np.zeros(nz)
    for k in range(nz):
        s = 0.0
        for i in range(nx):
            s += interp_at_target_y(y_3d[k, :, i], fld[k, :, i], y_target)
        out[k] = s/nx
    return out

FIELDS = ['uu', 'vv', 'k', 'uv']
FKEY = {'uu':'uu_RS','vv':'vv_RS','k':'k_TKE','uv':'uv_RS'}

profiles = {}
print("\n[INFO] Extracting GILBM profiles per station...", flush=True)
for xh in STATIONS:
    if xh not in krank: continue
    zp = extract_z(xh)
    z_abs = zp  # already absolute wall-normal z; Krank y/H also absolute
    prof = {'z_abs': z_abs}
    for f in FIELDS:
        prof[f] = extract_field(xh, F3D[FKEY[f]])
    profiles[xh] = prof
    print(f"  xh={xh}: z_abs[0]={z_abs[0]:.4f} (wall), z_abs[-1]={z_abs[-1]:.4f}", flush=True)

def _ext_idx(a, field):
    a = np.asarray(a, float)
    return int(np.nanargmin(a)) if field == 'uv' else int(np.nanargmax(a))

rows = []
print("\n" + "="*120)
print("PER-STATION PEAK DEFICIT (GILBM vs Krank DNS primary; MGLET noted)")
print("DNS peak located on Krank profile; GILBM interp (6th Lagrange) onto Krank z-grid, compared AT DNS-peak y/H.")
print("pct_deficit = 100*(dns-gilbm)/dns at the DNS peak.")
print("="*120)
for field in FIELDS:
    print(f"\n--- {field} ---")
    print(f"{'x/h':>6s} {'y_peak':>8s} {'dns_pk_Krank':>14s} {'gilbm@ypk':>12s} {'gilbm_ownpk':>12s} {'pct_def':>9s} {'MGLET_pk':>11s} {'MGLET_pct':>10s}")
    for xh in STATIONS:
        if xh not in krank or xh not in profiles: continue
        bd = krank[xh]; zb = np.asarray(bd['y'], float); fb = bd.get(field)
        if fb is None: continue
        fb = np.asarray(fb, float)
        ipk = _ext_idx(fb, field); y_peak = float(zb[ipk]); dns_pk = float(fb[ipk])
        p = profiles[xh]; zs = np.asarray(p['z_abs'], float); fs = np.asarray(p[field], float)
        fsab, mask = interp_1d_lagrange(zs, fs, zb)
        g_at = float(fsab[ipk]) if mask[ipk] else float('nan')
        g_own = float(fs[_ext_idx(fs, field)])
        pct = 100.0*(dns_pk - g_at)/dns_pk if abs(dns_pk) > 1e-30 else float('nan')
        mpk = float('nan'); mpct = float('nan')
        if xh in mglet and mglet[xh].get(field) is not None:
            zm = np.asarray(mglet[xh]['y'], float); fm = np.asarray(mglet[xh][field], float)
            im = _ext_idx(fm, field); mpk = float(fm[im])
            fsm, mm = interp_1d_lagrange(zs, fs, zm)
            if mm[im] and abs(mpk) > 1e-30:
                mpct = 100.0*(mpk - float(fsm[im]))/mpk
        print(f"{xh:6.2f} {y_peak:8.4f} {dns_pk:14.6e} {g_at:12.4e} {g_own:12.4e} {pct:8.2f}% {mpk:11.4e} {mpct:9.2f}%", flush=True)
        rows.append({'field':field,'xh':xh,'y_peak':y_peak,'dns_peak_krank':dns_pk,
                     'gilbm_at_ypk':g_at,'gilbm_ownpk':g_own,'pct_deficit':pct,
                     'mglet_peak':mpk,'mglet_pct':mpct})

with open('/tmp/peak_deficit.csv','w',newline='') as fh:
    w = csv.DictWriter(fh, fieldnames=['field','xh','y_peak','dns_peak_krank',
        'gilbm_at_ypk','gilbm_ownpk','pct_deficit','mglet_peak','mglet_pct'])
    w.writeheader()
    for r in rows: w.writerow(r)
print("\n[CSV] /tmp/peak_deficit.csv", flush=True)

print("\n" + "="*70)
print("SUMMARY")
print("="*70)
for field in FIELDS:
    fr = [r for r in rows if r['field']==field and np.isfinite(r['pct_deficit'])]
    if not fr: continue
    defs = [r['pct_deficit'] for r in fr]
    worst = max(fr, key=lambda r: r['pct_deficit'])
    print(f"{field}: mean={np.mean(defs):6.2f}%  range=[{min(defs):6.2f}%,{max(defs):6.2f}%]  worst@xh={worst['xh']} ({worst['pct_deficit']:.2f}%)")
