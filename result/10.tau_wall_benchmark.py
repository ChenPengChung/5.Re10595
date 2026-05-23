#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
10.tau_wall_benchmark.py
========================
Compute SIGNED span-averaged tau_wall on bottom and top walls from a
time-averaged VTK and overlay benchmark DNS data (Krank, MGLET).

Output:
    tau_wall_signed_Re{N}.dat   — y/H, tau_bot, tau_top, cf_bot, cf_top
    tau_wall_signed_Re{N}.png   — cf plot with benchmark scatter

Usage:
    python3 10.tau_wall_benchmark.py                    # interactive
    python3 10.tau_wall_benchmark.py --Re 5600
    python3 10.tau_wall_benchmark.py --Re 5600 --no-ask-density

Convention:
    tau_wall = niu * du_t/dn        (signed, lattice stress, rho=1)
    cf       = tau_wall / (0.5 * Ub^2)   where Ub = Uref (bulk velocity)
    y = streamwise, z = wall-normal, x = spanwise
"""
from __future__ import annotations
import argparse, glob, os, re, sys, time
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR   = os.path.dirname(SCRIPT_DIR)
BENCH_DIR  = os.path.join(SCRIPT_DIR, "benchmark")

# ====================================================================
#  6th-order Fornberg FD coefficients (7-point stencil, unit spacing)
# ====================================================================
FD6_COEFF = np.array([
    [-147,  360, -450,  400, -225,   72,  -10],   # p=0 forward
    [ -10,  -77,  150, -100,   50,  -15,    2],   # p=1
    [   2,  -24,  -35,   80,  -30,    8,   -1],   # p=2
    [  -1,    9,  -45,    0,   45,   -9,    1],   # p=3 central
    [   1,   -8,   30,  -80,   35,   24,   -2],   # p=4
    [  -2,   15,  -50,  100, -150,   77,   10],   # p=5
    [  10,  -72,  225, -400,  450, -360,  147],   # p=6 backward
], dtype=np.float64) / 60.0

FD6_FWD = FD6_COEFF[0]
FD6_BWD = FD6_COEFF[6]


# ====================================================================
#  6th-order periodic central FD (1D row and 2D axis-0)
# ====================================================================
def fd6_periodic_row(f, period_offset=0.0):
    J = f.shape[0]
    lo = f[J-4:J-1] - period_offset
    hi = f[1:4]     + period_offset
    e  = np.concatenate([lo, f, hi])
    return (-e[0:J] + 9*e[1:J+1] - 45*e[2:J+2]
            + 45*e[4:J+4] - 9*e[5:J+5] + e[6:J+6]) / 60.0


def fd6_periodic_2d_axis0(f2d):
    J = f2d.shape[0]
    lo = f2d[J-4:J-1]
    hi = f2d[1:4]
    e  = np.concatenate([lo, f2d, hi], axis=0)
    return (-e[0:J] + 9*e[1:J+1] - 45*e[2:J+2]
            + 45*e[4:J+4] - 9*e[5:J+5] + e[6:J+6]) / 60.0


# ====================================================================
#  variables.h parser
# ====================================================================
def find_variables_h(start_dir, max_up=5):
    cur = os.path.abspath(start_dir)
    for _ in range(max_up + 1):
        for name in ("variables.h", "variable.h"):
            p = os.path.join(cur, name)
            if os.path.isfile(p):
                return p
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


_DEFINE_RE = re.compile(
    r"^\s*#define\s+(\w+)\s+(.+?)\s*(?://|/\*|$)", re.MULTILINE)


def parse_defines(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    raw = {m.group(1): m.group(2).strip() for m in _DEFINE_RE.finditer(text)}
    out = {}
    for name, expr in raw.items():
        sub = expr
        for k in sorted(raw, key=len, reverse=True):
            sub = re.sub(r"\b" + re.escape(k) + r"\b", f"({raw[k]})", sub)
        if re.fullmatch(r"[\d.+\-*/()eE\s]+", sub):
            try:
                out[name] = float(eval(sub, {"__builtins__": {}}, {}))
            except Exception:
                pass
    return out


def get_const(defs, names):
    for n in names:
        for k, v in defs.items():
            if k.lower() == n.lower():
                return v
    raise KeyError(f"missing {names} in variables.h")


# ====================================================================
#  VTK BINARY/ASCII reader
# ====================================================================
def parse_vtk(filepath):
    dims = None; npts = 0; npts_from_dims = 0
    points = np.empty((0, 3)); scalars = {}; is_binary = False

    def _dt(tok):
        t = tok.lower()
        if t == "double": return ">f8", 8
        if t == "float":  return ">f4", 4
        return ">f8", 8

    def _read_ascii(f, n, stop):
        arr = np.empty(n, dtype=np.float64); idx = 0
        while idx < n:
            dl = f.readline()
            if not dl: break
            s = dl.decode("latin-1", errors="ignore").strip()
            if not s: continue
            if s.split()[0].startswith(stop): return arr[:idx], dl
            for v in s.split():
                if idx < n: arr[idx] = float(v); idx += 1
        return arr[:idx], None

    with open(filepath, "rb") as f:
        pb = None
        while True:
            raw = pb if pb is not None else f.readline()
            pb = None
            if not raw: break
            s = raw.decode("latin-1", errors="ignore").strip()
            if not s: continue
            if s == "BINARY":    is_binary = True;  continue
            if s == "ASCII":     is_binary = False; continue
            if s.startswith("DIMENSIONS"):
                dims = tuple(int(v) for v in s.split()[1:4])
                npts_from_dims = dims[0]*dims[1]*dims[2]
            elif s.startswith("POINT_DATA"):
                npts = int(s.split()[1])
            elif s.startswith("POINTS"):
                p = s.split(); n = int(p[1]); dt, es = _dt(p[2] if len(p)>2 else "double")
                if npts == 0: npts = n
                if is_binary:
                    buf = f.read(n*3*es)
                    points = np.frombuffer(buf, dtype=dt).astype(np.float64).copy().reshape(-1,3)
                    f.readline()
                else:
                    pts, pb = _read_ascii(f, n*3, ("SCALARS","VECTORS","POINT_DATA"))
                    points = pts.reshape(-1,3)
            elif s.startswith("VECTORS"):
                if npts==0 and npts_from_dims>0: npts = npts_from_dims
                p = s.split(); nm = p[1]; dt, es = _dt(p[2] if len(p)>2 else "double")
                if is_binary:
                    buf = f.read(npts*3*es)
                    vec = np.frombuffer(buf, dtype=dt).astype(np.float64).copy()
                    f.readline()
                else:
                    vec, pb = _read_ascii(f, npts*3, ("SCALARS","VECTORS","POINT_DATA"))
                if vec.size == npts*3:
                    scalars[f"{nm}_x"] = vec[0::3].copy()
                    scalars[f"{nm}_y"] = vec[1::3].copy()
                    scalars[f"{nm}_z"] = vec[2::3].copy()
            elif s.startswith("SCALARS"):
                if npts==0 and npts_from_dims>0: npts = npts_from_dims
                p = s.split(); nm = p[1]; dt, es = _dt(p[2] if len(p)>2 else "double")
                f.readline()
                if is_binary:
                    buf = f.read(npts*es)
                    arr = np.frombuffer(buf, dtype=dt).astype(np.float64).copy()
                    f.readline()
                else:
                    arr, pb = _read_ascii(f, npts, ("SCALARS","VECTORS"))
                scalars[nm] = arr
    return dims, points, scalars


# ====================================================================
#  Wall metric at k=0 or k=K-1
# ====================================================================
def metric_at_wall(y2d, z2d, wall):
    K, J = y2d.shape
    if wall == "bottom":
        coef = FD6_COEFF[0]
        y_zeta = coef @ y2d[0:7, :]
        z_zeta = coef @ z2d[0:7, :]
        kw = 0
    else:
        coef = FD6_COEFF[6]
        y_zeta = coef @ y2d[K-7:K, :]
        z_zeta = coef @ z2d[K-7:K, :]
        kw = K - 1
    L = float(y2d[kw, -1] - y2d[kw, 0])
    y_xi = fd6_periodic_row(y2d[kw], period_offset=L)
    z_xi = fd6_periodic_row(z2d[kw], period_offset=0.0)
    J_det = y_xi * z_zeta - y_zeta * z_xi
    h_xi  = np.sqrt(y_xi**2 + z_xi**2)
    eXZ   = y_xi * y_zeta + z_xi * z_zeta
    return dict(y_xi=y_xi, z_xi=z_xi, y_zeta=y_zeta, z_zeta=z_zeta,
                h_xi=h_xi, J=J_det, eXZ=eXZ, kw=kw)


# ====================================================================
#  Core: compute signed tau_wall on one wall
# ====================================================================
def compute_tau_wall(u_t_7layers, wall_metric, niu, wall):
    if wall == "bottom":
        dut_dzeta = np.einsum('m,mji->ji', FD6_FWD, u_t_7layers)
    else:
        dut_dzeta = np.einsum('m,mji->ji', FD6_BWD, u_t_7layers)

    if wall == "bottom":
        u_t_wall = u_t_7layers[0]
    else:
        u_t_wall = u_t_7layers[-1]
    dut_dxi = fd6_periodic_2d_axis0(u_t_wall)

    m = wall_metric
    A = (m["h_xi"] / m["J"])[:, None]
    B = (m["eXZ"] / (m["h_xi"] * m["J"]))[:, None]
    dut_dn = A * dut_dzeta - B * dut_dxi

    return niu * dut_dn


# ====================================================================
#  Auto-detect helpers
# ====================================================================
def find_latest_vtk(folder, pattern="*velocity_merged_*.vtk"):
    hits = sorted(glob.glob(os.path.join(folder, pattern)))
    return hits[-1] if hits else None


# ====================================================================
#  Benchmark data loaders
# ====================================================================
BENCH_SOURCES = {
    'Krank': {
        'dir_name':  'Benjamin Krank et al. 2018',
        'label':     r'Krank $\mathit{et\;al.}$ (2018) DNS',
        'color':     '#7B2D8E',
        'marker':    'o',
        'markersize': 3.5,
        'default_density': 20,
    },
    'MGLET': {
        'dir_name':  'MGLET (Breuer et al. 2009)',
        'label':     r'Breuer $\mathit{et\;al.}$ (2009) DNS',
        'color':     '#228B22',
        'marker':    'D',
        'markersize': 3.5,
        'default_density': 6,
    },
}


def load_krank_cf(Re):
    d = os.path.join(BENCH_DIR, 'Benjamin Krank et al. 2018', f'Re{Re}')
    out = {}
    for wall in ('bottom', 'top'):
        pat = os.path.join(d, f'*Re{Re}_cf_cp_{wall}.dat')
        hits = glob.glob(pat)
        if not hits:
            continue
        raw = np.loadtxt(hits[0], comments='%', delimiter=',')
        out[wall] = {'xH': raw[:, 0], 'cf': raw[:, 1], 'cp': raw[:, 2]}
    return out


def load_mglet_cf(Re):
    re_map = {2800: 'Re2800', 5600: 'Re5600', 1400: 'Re1400',
              10595: 'Re10595'}
    redir = re_map.get(Re)
    if redir is None:
        return {}
    d = os.path.join(BENCH_DIR, 'MGLET (Breuer et al. 2009)', redir)
    pat = os.path.join(d, f'*_wall.dat')
    hits = glob.glob(pat)
    if not hits:
        return {}
    raw = np.loadtxt(hits[0])
    # MGLET data uses 1/(ρu_b²) normalization; standard uses 1/(½ρu_b²) → multiply by 2
    return {'bottom': {'xH': raw[:, 0], 'cf': raw[:, 1] * 2.0, 'cp': raw[:, 2] * 2.0}}


def subsample_uniform(arr_x, arr_y, density_pct):
    if density_pct >= 100 or len(arr_x) <= 2:
        return arr_x, arr_y
    n = len(arr_x)
    n_keep = max(2, int(round(n * density_pct / 100.0)))
    idx = np.round(np.linspace(0, n - 1, n_keep)).astype(int)
    return arr_x[idx], arr_y[idx]


def compute_l2_error(y_sim, cf_sim, y_ref, cf_ref):
    cf_interp = np.interp(y_ref, y_sim, cf_sim)
    mask = ~(np.isnan(cf_interp) | np.isnan(cf_ref))
    if mask.sum() < 2:
        return np.nan
    diff = cf_interp[mask] - cf_ref[mask]
    ref_rms = np.sqrt(np.mean(cf_ref[mask]**2))
    if ref_rms < 1e-30:
        return np.nan
    return np.sqrt(np.mean(diff**2)) / ref_rms * 100.0


# ====================================================================
#  Academic plot style (from plot_lodic.py)
# ====================================================================
def setup_academic_style():
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "font.family":          "serif",
        "font.serif":           ["Times New Roman", "Computer Modern Roman",
                                 "DejaVu Serif"],
        "mathtext.fontset":     "cm",
        "axes.labelsize":       14,
        "font.size":            12,
        "legend.fontsize":      9.5,
        "xtick.labelsize":      12,
        "ytick.labelsize":      12,
        "axes.linewidth":       0.8,
        "lines.linewidth":      1.5,
        "xtick.direction":      "in",
        "ytick.direction":      "in",
        "xtick.top":            True,
        "ytick.right":          True,
        "xtick.major.size":     5,
        "ytick.major.size":     5,
        "xtick.minor.size":     3,
        "ytick.minor.size":     3,
        "xtick.minor.visible":  True,
        "ytick.minor.visible":  True,
        "figure.dpi":           150,
        "savefig.dpi":          300,
        "savefig.bbox":         "tight",
        "savefig.pad_inches":   0.05,
    })


# ====================================================================
#  Main
# ====================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Compute signed span-averaged tau_wall for benchmark.")
    ap.add_argument("--Re", type=int, default=None)
    ap.add_argument("--vtk", default=None)
    ap.add_argument("--output", default=None)
    ap.add_argument("--no-plot", action="store_true")
    ap.add_argument("--no-ask-density", action="store_true",
                    help="Use default benchmark density, skip interactive")
    ap.add_argument("--auto", action="store_true",
                    help="Non-interactive: default density, png only")
    args = ap.parse_args(argv)
    if args.auto:
        args.no_ask_density = True

    # ── locate inputs ──
    vtk_path = args.vtk or find_latest_vtk(SCRIPT_DIR)
    if vtk_path is None:
        vtk_path = find_latest_vtk(ROOT_DIR)
    if vtk_path is None:
        print("[error] no velocity_merged VTK found", file=sys.stderr)
        return 1
    var_h = find_variables_h(ROOT_DIR)
    if var_h is None:
        print("[error] variables.h not found", file=sys.stderr)
        return 1

    defs  = parse_defines(var_h)
    if args.Re is not None:
        Re = args.Re
    else:
        try:
            Re = int(input(f"Reynolds number (default {int(get_const(defs, ['Re']))}): ")
                     or str(int(get_const(defs, ["Re"]))))
        except (ValueError, EOFError):
            Re = int(get_const(defs, ["Re"]))
    Uref  = get_const(defs, ["Uref", "U_ref"])
    niu   = get_const(defs, ["niu", "nu"])

    print(f"VTK  : {vtk_path}")
    print(f"Re={Re}  Uref={Uref}  niu={niu:.6e}")

    # ── [1] read VTK ──
    print("\n[1] reading VTK (ERCOFTAC convention: U_mean=stream, V_mean=normal) ...")
    t0 = time.time()
    dims, points, scalars = parse_vtk(vtk_path)
    Nx, Ny, Nz = dims
    print(f"    dims ({Nx}, {Ny}, {Nz})  ({time.time()-t0:.1f}s)")

    V_stream = scalars["U_mean"].reshape(Nz, Ny, Nx) * Uref
    W_normal = scalars["V_mean"].reshape(Nz, Ny, Nx) * Uref

    print(f"    V_stream [{V_stream.min():.6e}, {V_stream.max():.6e}]")
    print(f"    W_normal [{W_normal.min():.6e}, {W_normal.max():.6e}]")

    # ── [2] extract 2D mesh from VTK points (already in h-units, H=1) ──
    print("[2] extracting 2D mesh from VTK POINTS (h-units) ...")
    pts3d = points.reshape(Nz, Ny, Nx, 3)
    y2d = pts3d[:, :, 0, 1]
    z2d = pts3d[:, :, 0, 2]
    H_check = z2d[0, :].max() - z2d[0, :].min()
    print(f"    mesh (Nz={Nz}, Ny={Ny})  y/H=[{y2d.min():.4f}, {y2d.max():.4f}]  "
          f"z/H=[{z2d.min():.4f}, {z2d.max():.4f}]  H={H_check:.4f}")

    # ── [3] wall metric ──
    print("[3] wall metric ...")
    bot_m = metric_at_wall(y2d, z2d, "bottom")
    top_m = metric_at_wall(y2d, z2d, "top")
    print(f"    bottom J range [{bot_m['J'].min():.4e}, {bot_m['J'].max():.4e}]")
    print(f"    top    J range [{top_m['J'].min():.4e}, {top_m['J'].max():.4e}]")

    # ── [4] slice 7 layers + project onto wall tangent ──
    print("[4] wall-tangent velocity (strict-orthonormal at bottom, identity at top) ...")
    V_bot = V_stream[0:7]
    W_bot = W_normal[0:7]
    yxi = bot_m["y_xi"][None, :, None]
    zxi = bot_m["z_xi"][None, :, None]
    hxi = bot_m["h_xi"][None, :, None]
    ut_bot = (V_bot * yxi + W_bot * zxi) / hxi

    ut_top = V_stream[Nz-7:Nz]

    print(f"    bottom u_t(k=0) |max| = {np.abs(ut_bot[0]).max():.3e}  (no-slip)")
    print(f"    top    u_t(k={Nz-1}) |max| = {np.abs(ut_top[-1]).max():.3e}  (no-slip)")

    # ── [5] compute signed tau_wall ──
    print("[5] tau_wall = niu * du_t/dn  (signed) ...")
    t0 = time.time()
    tau_bot = compute_tau_wall(ut_bot, bot_m, niu, "bottom")
    tau_top = compute_tau_wall(ut_top, top_m, niu, "top")
    print(f"    bottom tau [{tau_bot.min():+.4e}, {tau_bot.max():+.4e}]")
    print(f"    top    tau [{tau_top.min():+.4e}, {tau_top.max():+.4e}]")
    print(f"    ({time.time()-t0:.1f}s)")

    # ── [6] span-average ──
    print("[6] span-averaging over i (Nx={}) ...".format(Nx))
    tau_bot_avg = tau_bot.mean(axis=1)
    tau_top_avg = tau_top.mean(axis=1)

    y_wall = y2d[0, :]
    cf_bot = tau_bot_avg / (0.5 * Uref**2)
    # Negate top wall: wall-normal n points outward (upward) → τ < 0 for attached flow;
    # Krank convention: cf > 0 = flow in streamwise direction → negate
    cf_top = -tau_top_avg / (0.5 * Uref**2)

    tau_top_conv = -tau_top_avg  # sign-flipped for output consistency
    utau_bot = np.sign(tau_bot_avg) * np.sqrt(np.abs(tau_bot_avg))
    utau_top = np.sign(tau_top_conv) * np.sqrt(np.abs(tau_top_conv))

    # ── separation / reattachment ──
    sign_changes = np.where(np.diff(np.sign(tau_bot_avg)))[0]
    print(f"\n    bottom wall sign changes (separation/reattachment):")
    for idx in sign_changes:
        y_cross = y_wall[idx] + (y_wall[idx+1]-y_wall[idx]) * (
            -tau_bot_avg[idx] / (tau_bot_avg[idx+1]-tau_bot_avg[idx]+1e-30))
        label = "separation" if tau_bot_avg[idx] > 0 else "reattachment"
        print(f"      {label:14s} at y/H = {y_cross:.4f}")

    print(f"\n    <cf_bot> = {cf_bot.mean():+.6e}")
    print(f"    <cf_top> = {cf_top.mean():+.6e}  (negated: positive = streamwise flow)")

    # ── [6b] wall pressure → cp ──
    has_pressure = "P_mean" in scalars
    if has_pressure:
        print("\n[6b] computing cp from P_mean ...")
        P3d = scalars["P_mean"].reshape(Nz, Ny, Nx)
        # Wall nodes (k=0, k=Nz-1) are bounce-back → P=0; use first fluid node
        p_bot_avg = P3d[1, :, :].mean(axis=1)
        p_top_avg = P3d[Nz-2, :, :].mean(axis=1)
        # p_ref at top wall, y/H ≈ 0 (Krank convention)
        j_ref = np.argmin(np.abs(y_wall - 0.0))
        p_ref = p_top_avg[j_ref]
        q_dyn = 0.5 * Uref**2
        cp_bot = (p_bot_avg - p_ref) / q_dyn
        cp_top = (p_top_avg - p_ref) / q_dyn
        print(f"    p_ref = P_mean(top k=1, y/H={y_wall[j_ref]:.4f}) = {p_ref:.6e}")
        print(f"    cp_bot [{cp_bot.min():+.4f}, {cp_bot.max():+.4f}]")
        print(f"    cp_top [{cp_top.min():+.4f}, {cp_top.max():+.4f}]")
    else:
        print("\n    (P_mean not in VTK — skipping cp)")
        cp_bot = cp_top = None

    # ── [7] load benchmark data ──
    print("\n[7] loading benchmark data ...")
    bench_data = {}
    krank = load_krank_cf(Re)
    if krank:
        bench_data['Krank'] = krank
        for wall, d in krank.items():
            print(f"    Krank {wall}: {len(d['xH'])} pts, "
                  f"cf=[{d['cf'].min():.4e}, {d['cf'].max():.4e}]")
    mglet = load_mglet_cf(Re)
    if mglet:
        bench_data['MGLET'] = mglet
        for wall, d in mglet.items():
            print(f"    MGLET {wall}: {len(d['xH'])} pts, "
                  f"cf=[{d['cf'].min():.4e}, {d['cf'].max():.4e}]")
    if not bench_data:
        print("    (no benchmark cf data found for this Re)")

    # ── benchmark density (interactive or default) ──
    bench_density = {}
    if bench_data:
        if args.no_ask_density:
            for src_id in bench_data:
                bench_density[src_id] = BENCH_SOURCES[src_id]['default_density']
            print(f"\n    benchmark density (--no-ask-density): {bench_density}")
        else:
            print(f"\n{'='*60}")
            print(f"  Benchmark scatter density")
            print(f"  100% = all points, 20% = every 5th, 0% = hide")
            print(f"{'='*60}")
            for src_id in bench_data:
                info = BENCH_SOURCES[src_id]
                default = info['default_density']
                n_total = sum(len(d['xH']) for d in bench_data[src_id].values())
                try:
                    raw = input(f"  {info['label']:40s} "
                                f"({n_total} pts, default {default}%): ").strip()
                    d = max(0, min(100, int(raw))) if raw else default
                except (ValueError, EOFError):
                    d = default
                bench_density[src_id] = d
                print(f"    -> {d}%")
            print(f"{'='*60}")

    # ── L2 error ──
    if bench_data:
        print("\n    L2 error (cf, bottom wall):")
        for src_id, walls in bench_data.items():
            if 'bottom' in walls:
                ref = walls['bottom']
                err = compute_l2_error(y_wall, cf_bot, ref['xH'], ref['cf'])
                print(f"      vs {src_id:>8s}: {err:.2f}%")
        if cp_bot is not None:
            print("    L2 error (cp, bottom wall):")
            for src_id, walls in bench_data.items():
                if 'bottom' in walls and 'cp' in walls['bottom']:
                    ref = walls['bottom']
                    err = compute_l2_error(y_wall, cp_bot, ref['xH'], ref['cp'])
                    print(f"      vs {src_id:>8s}: {err:.2f}%")

    # ── [8] write output ──
    out_path = args.output or os.path.join(
        SCRIPT_DIR, f"tau_wall_signed_Re{Re}.dat")
    print(f"\n[8] writing {out_path} ...")
    with open(out_path, "w") as f:
        f.write(f"# Signed span-averaged tau_wall — Periodic Hill Re={Re}\n")
        f.write(f"# VTK: {os.path.basename(vtk_path)}\n")
        f.write(f"# Mesh: from VTK POINTS (h-units, H={H_check:.4f})\n")
        f.write(f"# Re={Re}  Uref={Uref}  niu={niu:.6e}  Nx={Nx} Ny={Ny} Nz={Nz}\n")
        f.write(f"# tau = niu * du_t/dn (signed, lattice stress, rho=1)\n")
        f.write(f"# cf  = tau / (0.5 * Uref^2)  (top wall negated: positive = streamwise)\n")
        f.write(f"# u_tau = sign(cf) * sqrt(|tau|)\n")
        f.write(f"#\n")
        sep_pts = []
        for idx in sign_changes:
            y_cross = y_wall[idx] + (y_wall[idx+1]-y_wall[idx]) * (
                -tau_bot_avg[idx]/(tau_bot_avg[idx+1]-tau_bot_avg[idx]+1e-30))
            label = "sep" if tau_bot_avg[idx] > 0 else "reat"
            sep_pts.append(f"{label}@{y_cross:.4f}")
        f.write(f"# bottom wall: {', '.join(sep_pts) if sep_pts else 'no sign change'}\n")
        f.write(f"#\n")
        f.write(f"# {'y/H':>10s}  {'tau_bot':>14s}  {'tau_top':>14s}  "
                f"{'cf_bot':>14s}  {'cf_top':>14s}  "
                f"{'utau_bot':>14s}  {'utau_top':>14s}\n")
        for j in range(Ny):
            f.write(f"  {y_wall[j]:10.6f}  {tau_bot_avg[j]:+14.8e}  "
                    f"{tau_top_conv[j]:+14.8e}  {cf_bot[j]:+14.8e}  "
                    f"{cf_top[j]:+14.8e}  {utau_bot[j]:+14.8e}  "
                    f"{utau_top[j]:+14.8e}\n")
    print(f"    wrote {os.path.getsize(out_path):,} bytes  ({Ny} points)")

    # ── [9] plot ──
    if not args.no_plot:
        try:
            import matplotlib
            if not os.environ.get("DISPLAY") and sys.platform != "win32":
                matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            # ── output format selection ──
            if args.auto or args.no_ask_density:
                save_fmts = ["png"]
            else:
                print(f"\n{'='*60}")
                print(f"  Plot output format")
                print(f"  1 = png only (default)")
                print(f"  2 = pdf only")
                print(f"  3 = png + pdf")
                print(f"{'='*60}")
                try:
                    choice = input("  選擇 [1]: ").strip() or "1"
                except EOFError:
                    choice = "1"
                if choice == "2":
                    save_fmts = ["pdf"]
                elif choice == "3":
                    save_fmts = ["png", "pdf"]
                else:
                    save_fmts = ["png"]

            setup_academic_style()

            def _bench_scatter(ax_target, field):
                for src_id, walls in bench_data.items():
                    info = BENCH_SOURCES[src_id]
                    density = bench_density.get(src_id, 100)
                    if density <= 0:
                        continue
                    for wall, d in walls.items():
                        if field not in d:
                            continue
                        xh_sub, val_sub = subsample_uniform(
                            d['xH'], d[field], density)
                        suffix = " (bot)" if wall == "bottom" else " (top)"
                        ax_target.scatter(
                            xh_sub, val_sub,
                            marker=info['marker'],
                            s=info['markersize']**2,
                            facecolors='none',
                            edgecolors=info['color'],
                            linewidths=0.6,
                            label=info['label'] + suffix,
                            zorder=3)

            def _save_fig(fig_obj, base_path, tag=""):
                stem = base_path.replace(".dat", tag)
                for fmt in save_fmts:
                    p = f"{stem}.{fmt}"
                    fig_obj.savefig(p)
                    print(f"    plot: {p}")

            # ── (a) cf ──
            fig_cf, ax_cf = plt.subplots(figsize=(9, 4.8))
            ax_cf.plot(y_wall, cf_bot, "-", color="#D62728", lw=1.6,
                       label=r"GILBM (bot)")
            ax_cf.plot(y_wall, cf_top, "-", color="#1F77B4", lw=1.6,
                       label=r"GILBM (top)")
            ax_cf.axhline(0, color="k", lw=0.5, ls="--")
            for idx in sign_changes:
                y_cross = y_wall[idx] + (y_wall[idx+1]-y_wall[idx]) * (
                    -tau_bot_avg[idx]/(tau_bot_avg[idx+1]-tau_bot_avg[idx]+1e-30))
                ax_cf.axvline(y_cross, color="0.6", lw=0.7, ls=":")
            _bench_scatter(ax_cf, 'cf')
            ax_cf.set_xlabel(r"$y \,/\, h$")
            ax_cf.set_ylabel(r"$c_f = \dfrac{\tau_w}{\frac{1}{2}\,\rho\,U_b^2}$")
            ax_cf.set_xlim(y_wall.min(), y_wall.max())
            ax_cf.legend(frameon=True, fancybox=False, edgecolor="0.4",
                         framealpha=1.0, loc="upper left", fontsize=9)
            ax_cf.text(0.02, 0.02, r"$\mathrm{(a)}$", transform=ax_cf.transAxes,
                       fontsize=14, va="bottom", ha="left")
            fig_cf.tight_layout()
            _save_fig(fig_cf, out_path)
            plt.close(fig_cf)

            # ── (b) cp ──
            if cp_bot is not None:
                fig_cp, ax_cp = plt.subplots(figsize=(9, 4.8))
                ax_cp.plot(y_wall, cp_bot, "-", color="#D62728", lw=1.6,
                           label=r"GILBM (bot)")
                ax_cp.plot(y_wall, cp_top, "-", color="#1F77B4", lw=1.6,
                           label=r"GILBM (top)")
                ax_cp.axhline(0, color="k", lw=0.5, ls="--")
                _bench_scatter(ax_cp, 'cp')
                ax_cp.set_xlabel(r"$y \,/\, h$")
                ax_cp.set_ylabel(r"$c_p = \dfrac{p - p_\mathrm{ref}}{\frac{1}{2}\,\rho\,U_b^2}$")
                ax_cp.set_xlim(y_wall.min(), y_wall.max())
                ax_cp.legend(frameon=True, fancybox=False, edgecolor="0.4",
                             framealpha=1.0, loc="best", fontsize=9)
                ax_cp.text(0.02, 0.02, r"$\mathrm{(b)}$", transform=ax_cp.transAxes,
                           fontsize=14, va="bottom", ha="left")
                fig_cp.tight_layout()
                _save_fig(fig_cp, out_path, "_cp")
                plt.close(fig_cp)

        except ImportError:
            print("    (matplotlib not available — skipping plot)")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
