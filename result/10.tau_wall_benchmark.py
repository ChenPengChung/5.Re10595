#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
10.tau_wall_benchmark.py
========================
Compute SIGNED span-averaged tau_wall on bottom and top walls from a
time-averaged VTK and the 2D Periodic Hill mesh.

Output:
    tau_wall_signed_Re{N}.dat   — y/H, tau_bot, tau_top, cf_bot, cf_top

Usage:
    python3 10.tau_wall_benchmark.py                    # auto-detect latest VTK
    python3 10.tau_wall_benchmark.py --Re 5600
    python3 10.tau_wall_benchmark.py --vtk path/to.vtk

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
    """1D 6th-order central FD with periodic wrap. f shape (J,)."""
    J = f.shape[0]
    lo = f[J-4:J-1] - period_offset
    hi = f[1:4]     + period_offset
    e  = np.concatenate([lo, f, hi])
    return (-e[0:J] + 9*e[1:J+1] - 45*e[2:J+2]
            + 45*e[4:J+4] - 9*e[5:J+5] + e[6:J+6]) / 60.0


def fd6_periodic_2d_axis0(f2d):
    """2D 6th-order central FD along axis-0 (j), periodic, no offset."""
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
#  VTK BINARY/ASCII reader (self-contained, from 2.Benchmark.py)
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
    """
    u_t_7layers : (7, Ny, Nx) — wall-tangent velocity on 7 layers
    wall_metric : dict from metric_at_wall
    wall        : 'bottom' or 'top'

    Returns tau_wall(Ny, Nx) — SIGNED.
    """
    # [1] du_t/dzeta — 6th-order single-sided Fornberg
    if wall == "bottom":
        dut_dzeta = np.einsum('m,mji->ji', FD6_FWD, u_t_7layers)
    else:
        dut_dzeta = np.einsum('m,mji->ji', FD6_BWD, u_t_7layers)

    # [2] du_t/dxi — 6th-order central + periodic wrap (wall row only)
    if wall == "bottom":
        u_t_wall = u_t_7layers[0]    # k=0
    else:
        u_t_wall = u_t_7layers[-1]   # k=K-1
    dut_dxi = fd6_periodic_2d_axis0(u_t_wall)

    # [3] du_t/dn = (h_xi/J)*du_t/dzeta - (eXZ/(h_xi*J))*du_t/dxi
    m = wall_metric
    A = (m["h_xi"] / m["J"])[:, None]
    B = (m["eXZ"] / (m["h_xi"] * m["J"]))[:, None]
    dut_dn = A * dut_dzeta - B * dut_dxi

    # [4] tau_wall = niu * du_t/dn  (signed)
    return niu * dut_dn


# ====================================================================
#  Auto-detect helpers
# ====================================================================
def find_latest_vtk(folder, pattern="*velocity_merged_*.vtk"):
    hits = sorted(glob.glob(os.path.join(folder, pattern)))
    if not hits:
        return None
    return hits[-1]


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
    args = ap.parse_args(argv)

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
    Re    = args.Re or int(get_const(defs, ["Re"]))
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

    # ERCOFTAC → project: U_mean=stream→V_mean, V_mean=normal→W_mean
    V_stream = scalars["U_mean"].reshape(Nz, Ny, Nx) * Uref   # lattice units
    W_normal = scalars["V_mean"].reshape(Nz, Ny, Nx) * Uref

    print(f"    V_stream [{V_stream.min():.6e}, {V_stream.max():.6e}]")
    print(f"    W_normal [{W_normal.min():.6e}, {W_normal.max():.6e}]")

    # ── [2] extract 2D mesh from VTK points (already in h-units, H=1) ──
    print("[2] extracting 2D mesh from VTK POINTS (h-units) ...")
    pts3d = points.reshape(Nz, Ny, Nx, 3)
    y2d = pts3d[:, :, 0, 1]   # (Nz, Ny) — streamwise, h-units
    z2d = pts3d[:, :, 0, 2]   # (Nz, Ny) — wall-normal, h-units
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
    x_arr = pts3d[0, 0, :, 0].copy()
    # Bottom: strict-orthonormal projection
    V_bot = V_stream[0:7]
    W_bot = W_normal[0:7]
    yxi = bot_m["y_xi"][None, :, None]
    zxi = bot_m["z_xi"][None, :, None]
    hxi = bot_m["h_xi"][None, :, None]
    ut_bot = (V_bot * yxi + W_bot * zxi) / hxi    # (7, Ny, Nx)

    # Top: flat wall — u_tangent = V_stream (identity)
    ut_top = V_stream[Nz-7:Nz]                    # (7, Ny, Nx)

    print(f"    bottom u_t(k=0) |max| = {np.abs(ut_bot[0]).max():.3e}  (no-slip)")
    print(f"    top    u_t(k={Nz-1}) |max| = {np.abs(ut_top[-1]).max():.3e}  (no-slip)")

    # ── [5] compute signed tau_wall ──
    print("[5] tau_wall = niu * du_t/dn  (signed) ...")
    t0 = time.time()
    tau_bot = compute_tau_wall(ut_bot, bot_m, niu, "bottom")   # (Ny, Nx)
    tau_top = compute_tau_wall(ut_top, top_m, niu, "top")      # (Ny, Nx)
    print(f"    bottom tau [{tau_bot.min():+.4e}, {tau_bot.max():+.4e}]")
    print(f"    top    tau [{tau_top.min():+.4e}, {tau_top.max():+.4e}]")
    print(f"    ({time.time()-t0:.1f}s)")

    # ── [6] span-average ──
    print("[6] span-averaging over i (Nx={}) ...".format(Nx))
    tau_bot_avg = tau_bot.mean(axis=1)    # (Ny,)
    tau_top_avg = tau_top.mean(axis=1)    # (Ny,)

    # wall y-coordinate (streamwise) — from mesh k=0 row
    y_wall = y2d[0, :]                    # (Ny,) = J points, h-normalized
    cf_bot = tau_bot_avg / (0.5 * Uref**2)
    cf_top = tau_top_avg / (0.5 * Uref**2)

    utau_bot = np.sign(tau_bot_avg) * np.sqrt(np.abs(tau_bot_avg))
    utau_top = np.sign(tau_top_avg) * np.sqrt(np.abs(tau_top_avg))

    # ── separation / reattachment ──
    sign_changes = np.where(np.diff(np.sign(tau_bot_avg)))[0]
    print(f"\n    bottom wall sign changes (separation/reattachment):")
    for idx in sign_changes:
        y_cross = y_wall[idx] + (y_wall[idx+1]-y_wall[idx]) * (
            -tau_bot_avg[idx] / (tau_bot_avg[idx+1]-tau_bot_avg[idx]+1e-30))
        label = "separation" if tau_bot_avg[idx] > 0 else "reattachment"
        print(f"      {label:14s} at y/H ≈ {y_cross:.4f}")

    # global signed average
    tau_bot_global = tau_bot_avg.mean()
    tau_top_global = tau_top_avg.mean()
    print(f"\n    <tau_bot>_signed = {tau_bot_global:+.6e}")
    print(f"    <tau_top>_signed = {tau_top_global:+.6e}")
    print(f"    <cf_bot>_signed  = {tau_bot_global/(0.5*Uref**2):+.6e}")
    print(f"    <cf_top>_signed  = {tau_top_global/(0.5*Uref**2):+.6e}")

    # ── [7] write output ──
    out_path = args.output or os.path.join(
        SCRIPT_DIR, f"tau_wall_signed_Re{Re}.dat")
    print(f"\n[7] writing {out_path} ...")
    with open(out_path, "w") as f:
        f.write(f"# Signed span-averaged tau_wall — Periodic Hill Re={Re}\n")
        f.write(f"# VTK: {os.path.basename(vtk_path)}\n")
        f.write(f"# Mesh: from VTK POINTS (h-units, H={H_check:.4f})\n")
        f.write(f"# Re={Re}  Uref={Uref}  niu={niu:.6e}  Nx={Nx} Ny={Ny} Nz={Nz}\n")
        f.write(f"# tau = niu * du_t/dn (signed, lattice stress, rho=1)\n")
        f.write(f"# cf  = tau / (0.5 * Uref^2)\n")
        f.write(f"# u_tau = sign(tau) * sqrt(|tau|)\n")
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
                    f"{tau_top_avg[j]:+14.8e}  {cf_bot[j]:+14.8e}  "
                    f"{cf_top[j]:+14.8e}  {utau_bot[j]:+14.8e}  "
                    f"{utau_top[j]:+14.8e}\n")
    print(f"    wrote {os.path.getsize(out_path):,} bytes  ({Ny} points)")

    # ── [8] optional plot ──
    if not args.no_plot:
        try:
            import matplotlib
            if not os.environ.get("DISPLAY") and sys.platform != "win32":
                matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True)

            axes[0].plot(y_wall, cf_bot, "b-", lw=1.2, label="bottom (hill)")
            axes[0].plot(y_wall, cf_top, "r-", lw=1.2, label="top (flat)")
            axes[0].axhline(0, color="k", lw=0.5, ls="--")
            for idx in sign_changes:
                y_cross = y_wall[idx] + (y_wall[idx+1]-y_wall[idx]) * (
                    -tau_bot_avg[idx]/(tau_bot_avg[idx+1]-tau_bot_avg[idx]+1e-30))
                axes[0].axvline(y_cross, color="gray", lw=0.8, ls=":")
            axes[0].set_ylabel(r"$c_f = \tau_w / (0.5\,U_b^2)$")
            axes[0].set_title(f"Signed skin friction — Periodic Hill Re={Re}")
            axes[0].legend(fontsize=9)
            axes[0].grid(True, alpha=0.3)

            axes[1].plot(y_wall, tau_bot_avg, "b-", lw=1.2, label="bottom")
            axes[1].plot(y_wall, tau_top_avg, "r-", lw=1.2, label="top")
            axes[1].axhline(0, color="k", lw=0.5, ls="--")
            axes[1].set_xlabel("y / H  (streamwise)")
            axes[1].set_ylabel(r"$\tau_w$ (signed, lattice)")
            axes[1].legend(fontsize=9)
            axes[1].grid(True, alpha=0.3)

            plt.tight_layout()
            fig_path = out_path.replace(".dat", ".png")
            plt.savefig(fig_path, dpi=150)
            print(f"    plot: {fig_path}")
            pdf_path = out_path.replace(".dat", ".pdf")
            plt.savefig(pdf_path)
            print(f"    plot: {pdf_path}")
            plt.close()
        except ImportError:
            print("    (matplotlib not available — skipping plot)")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
