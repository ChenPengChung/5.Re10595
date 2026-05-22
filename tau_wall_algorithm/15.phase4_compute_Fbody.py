#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
15.phase4_compute_Fbody.py
==========================

Compute the LBM body-force volume integral and 3D fluid volume.

    F_body_y = F_metadata * V_fluid     (constant F per unit volume)

The full force balance (F_body + F_vis + F_pressure = 0) is verified in
step 16 (16.verify_force_balance.py).  This script only computes the
driving-force side.

V_fluid (3D) computation -- careful curvature handling
------------------------------------------------------
The hill bottom is curved, so the cross-section in (y, z) is NOT a
rectangle.  We compute V_fluid three independent ways and require them
to agree:

    Method A  (trapezoidal in y, assumes y depends only on j):
        h(j)  = z_top(j) - z_bot(j)
        A_2D  = sum_j  0.5 * (h[j] + h[j+1]) * (y[j+1] - y[j])

    Method B  (quadrilateral shoelace, fully general 2D curvilinear):
        For each (j, k) cell with corners P0..P3 (CCW):
        cell_area = 0.5 * |((P2-P0) x (P3-P1))|_z
        A_2D = sum over all (j, k) cells

    Method C  (Jacobian Riemann sum, dxi = dk = 1):
        J(j,k) = | (dy/dxi)(dz/deta) - (dy/deta)(dz/dxi) |
                 evaluated at cell center via 4-corner average
        A_2D = sum_{j,k} J(j,k)

    V_3D = LX * A_2D   (LX = uniform spanwise period)

All three should agree to ~1e-12 relative.

Output: 33.Re<X>_Fbody_volume.dat
"""

from __future__ import annotations
import argparse
import glob
import os
import re
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "Reference"))
INPUT_DIR  = os.path.join(_HERE, "Input")
OUTPUT_DIR = os.path.join(_HERE, "Output")

from phase1_common import (   # noqa: E402
    parse_tecplot_2d_mesh,
    auto_detect_variables_h,
    find_const,
    parse_header_constants,
    detect_ftt_start_from_monitor,
    parse_monitor_force_avg,
)


def find_metadata(folder: str) -> str:
    pattern = os.path.join(folder, "Re*_metadata.dat")
    hits = sorted(glob.glob(pattern))
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise FileNotFoundError(f"no file matching {pattern}")
    raise FileNotFoundError(f"multiple matches for {pattern}: {hits}")


def parse_metadata(path: str) -> dict:
    """Parse simple key=value metadata file, ignoring blank/comment lines."""
    out = {}
    with open(path) as f:
        for ln in f:
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            if "=" not in s:
                continue
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip()
    return out




def vol_method_A_trapezoidal(y2d: np.ndarray, z2d: np.ndarray) -> tuple:
    """Trapezoidal method.  Assumes y[k, j] depends only on j (span-uniform
    streamwise lines).  Returns (A_2D, max_y_dependence_on_k).
    """
    K, J = y2d.shape
    y_kavg = y2d.mean(axis=0)                       # (J,)
    y_max_dev = float(np.abs(y2d - y_kavg).max())   # how much y varies with k

    z_bot = z2d[0,    :]                            # (J,)
    z_top = z2d[-1,   :]                            # (J,)
    h = z_top - z_bot                               # (J,)
    dy = np.diff(y_kavg)                            # (J-1,)
    A_2D = 0.5 * (h[:-1] + h[1:]) * dy
    return float(A_2D.sum()), y_max_dev


def vol_method_B_shoelace(y2d: np.ndarray, z2d: np.ndarray) -> float:
    """Quadrilateral shoelace.  Fully general 2D curvilinear mesh.

    For each cell with corners P0=(j,k), P1=(j+1,k), P2=(j+1,k+1), P3=(j,k+1):
        area = 0.5 * |(P2-P0) x (P3-P1)|_z
             = 0.5 * |((y2-y0)*(z3-z1) - (y3-y1)*(z2-z0))|
    """
    P0_y, P0_z = y2d[:-1, :-1], z2d[:-1, :-1]
    P1_y, P1_z = y2d[:-1, 1: ], z2d[:-1, 1: ]
    P2_y, P2_z = y2d[1: , 1: ], z2d[1: , 1: ]
    P3_y, P3_z = y2d[1: , :-1], z2d[1: , :-1]
    cross = (P2_y - P0_y) * (P3_z - P1_z) \
          - (P3_y - P1_y) * (P2_z - P0_z)
    cell_area = 0.5 * np.abs(cross)
    return float(cell_area.sum())


def vol_method_C_jacobian(y2d: np.ndarray, z2d: np.ndarray) -> float:
    """Jacobian Riemann sum.

    Treat (j, k) as computational coordinates with d_xi = d_eta = 1.
    Jacobian at each cell center via the 4-corner edges:
        dy_dj = 0.5*((y[k,j+1]-y[k,j]) + (y[k+1,j+1]-y[k+1,j]))
        dz_dj = 0.5*((z[k,j+1]-z[k,j]) + (z[k+1,j+1]-z[k+1,j]))
        dy_dk = 0.5*((y[k+1,j]-y[k,j]) + (y[k+1,j+1]-y[k,j+1]))
        dz_dk = 0.5*((z[k+1,j]-z[k,j]) + (z[k+1,j+1]-z[k,j+1]))
    Cell volume in computational space = 1*1 = 1, physical area = |J|.
    """
    dy_dj = 0.5 * ((y2d[:-1, 1:] - y2d[:-1, :-1])
                 + (y2d[1: , 1:] - y2d[1: , :-1]))
    dz_dj = 0.5 * ((z2d[:-1, 1:] - z2d[:-1, :-1])
                 + (z2d[1: , 1:] - z2d[1: , :-1]))
    dy_dk = 0.5 * ((y2d[1:, :-1] - y2d[:-1, :-1])
                 + (y2d[1:, 1: ] - y2d[:-1, 1: ]))
    dz_dk = 0.5 * ((z2d[1:, :-1] - z2d[:-1, :-1])
                 + (z2d[1:, 1: ] - z2d[:-1, 1: ]))
    J_det = np.abs(dy_dj * dz_dk - dy_dk * dz_dj)
    return float(J_det.sum())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Compute F_body = F * V_fluid (body-force volume integral).")
    ap.add_argument("--input-folder",  default=INPUT_DIR,
                    help="folder containing Re*_metadata.dat (default: Input)")
    ap.add_argument("--output-folder", default=OUTPUT_DIR,
                    help="folder containing 2.* mesh (default: Output)")
    ap.add_argument("--metadata", default=None, help="explicit metadata path")
    ap.add_argument("--mesh",     default=None, help="explicit 2.*.dat path")
    ap.add_argument("--variables-h", default=None,
                    help="Input/variables.h (default: auto-detect)")
    ap.add_argument("--lx", dest="LX", type=float, default=None,
                    help="spanwise length override (default: LX from variables.h)")
    ap.add_argument("--monitor", default=None,
                    help="Ustar_Force_record.dat for time-averaged Force")
    ap.add_argument("--ftt-stats-start", type=float, default=None,
                    help=("FTT when statistics started; default auto-detects "
                          "the first monitor row with accu_cnt > 0"))
    args = ap.parse_args(argv)

    in_folder  = args.input_folder
    out_folder = args.output_folder

    # ---------------- locate inputs ----------------
    meta_path = args.metadata or find_metadata(in_folder)

    mesh_path = args.mesh
    if mesh_path is None:
        hits = sorted(glob.glob(os.path.join(out_folder, "2.j*_k*_g*_a*.dat")))
        if len(hits) != 1:
            raise FileNotFoundError(
                f"need exactly one 2.*.dat in {out_folder}, found {hits}")
        mesh_path = hits[0]

    var_h = args.variables_h or auto_detect_variables_h(
        os.path.join(_HERE, "Input"))
    consts = parse_header_constants(var_h) if var_h else {}

    if args.LX is not None:
        LX = args.LX
    else:
        LX = find_const(consts, ["LX"], var_h or "variables.h")

    LY = find_const(consts, ["LY"], var_h or "variables.h")
    Uref = find_const(consts, ["Uref", "U_ref"], var_h or "variables.h")

    ftt_stats = args.ftt_stats_start

    print(f"metadata     : {meta_path}")
    print(f"mesh (2.dat) : {mesh_path}")
    print(f"variables.h  : {var_h}")

    # ---------------- parse F ----------------
    meta = parse_metadata(meta_path)
    F_meta = float(meta["Force"])
    print(f"\nF_metadata (final)   = {F_meta:+.12e}")
    print(f"LX (spanwise)        = {LX:+.12e}")

    # ---------------- time-averaged F from monitor ----------------
    monitor_path = args.monitor
    if monitor_path is None:
        hits = sorted(glob.glob(os.path.join(in_folder,
                                             "*_Ustar_Force_record.dat")))
        if not hits:
            parent = os.path.dirname(os.path.dirname(
                os.path.abspath(out_folder)))
            hits = sorted(glob.glob(os.path.join(parent,
                                                 "*_Ustar_Force_record.dat")))
        if hits:
            monitor_path = hits[-1]

    F_avg = None
    if monitor_path and os.path.isfile(monitor_path):
        mon = parse_monitor_force_avg(monitor_path, ftt_stats, Uref, LY)
        F_avg = mon["Force_avg"]
        ftt_start_used = mon["ftt_start_used"]
        print(f"monitor file         : {os.path.basename(monitor_path)}")
        print(f"FTT stats start      : {ftt_start_used:.6f}  "
              f"(auto-detected from accu_cnt)"
              if ftt_stats is None else
              f"FTT stats start      : {ftt_start_used:.6f}  (CLI override)")
        print(f"<Force> (time-avg)   = {F_avg:+.12e}  "
              f"(ratio avg/final = {F_avg/F_meta:.4f})")

    # ---------------- 2D mesh in (y, z) ----------------
    y2d, z2d, J, K = parse_tecplot_2d_mesh(mesh_path)
    print(f"mesh shape (K, J)    = ({K}, {J})  "
          f"= ({z2d.shape[0]}, {z2d.shape[1]})")
    print(f"y range              = [{y2d.min():.6f}, {y2d.max():.6f}]   "
          f"=> LY = {y2d.max() - y2d.min():.6f}")
    print(f"z_bot range (k=0)    = [{z2d[0, :].min():.6f}, "
          f"{z2d[0, :].max():.6f}]")
    print(f"z_top range (k=K-1)  = [{z2d[-1, :].min():.6f}, "
          f"{z2d[-1, :].max():.6f}]")

    # ---------------- 2D area, three ways ----------------
    A_trap, y_dev_k = vol_method_A_trapezoidal(y2d, z2d)
    A_shoe          = vol_method_B_shoelace(y2d, z2d)
    A_jac           = vol_method_C_jacobian(y2d, z2d)

    V_trap = LX * A_trap
    V_shoe = LX * A_shoe
    V_jac  = LX * A_jac

    print(f"\n--- 2D cross-section area ---")
    print(f"A_2D (trapezoidal)   = {A_trap:+.12e}")
    print(f"A_2D (shoelace)      = {A_shoe:+.12e}")
    print(f"A_2D (Jacobian sum)  = {A_jac:+.12e}")
    print(f"y(k) max deviation   = {y_dev_k:.3e}   "
          "(0 means y depends only on j)")

    print(f"\n--- 3D fluid volume = LX * A_2D ---")
    print(f"V_3D (trapezoidal)   = {V_trap:+.12e}")
    print(f"V_3D (shoelace)      = {V_shoe:+.12e}")
    print(f"V_3D (Jacobian sum)  = {V_jac:+.12e}")

    rel_dev = max(abs(V_trap - V_shoe),
                  abs(V_shoe - V_jac),
                  abs(V_trap - V_jac)) / V_shoe
    print(f"max relative spread  = {rel_dev:.3e}")
    if rel_dev > 1e-10:
        print(f"[warn] methods disagree by > 1e-10 relative")

    V_fluid = V_shoe

    # ---------------- F_body = F * V_fluid ----------------
    F_body_final = F_meta * V_fluid
    F_body_avg = F_avg * V_fluid if F_avg is not None else None
    F_body = F_body_avg if F_body_avg is not None else F_body_final

    print(f"\n--- body-force volume integral ---")
    print(f"F_body (F_final * V)       = {F_body_final:+.12e}")
    if F_body_avg is not None:
        print(f"F_body (<F_avg> * V)       = {F_body_avg:+.12e}  << used for balance")

    # ---------------- Re token + write output ----------------
    m = re.search(r"Re(\d+)", os.path.basename(meta_path))
    if not m:
        raise ValueError("could not parse Re token from metadata filename")
    Re = int(m.group(1))

    stem = f"35.Re{Re}_Fbody_volume"
    out_path = os.path.join(out_folder, f"{stem}.dat")
    with open(out_path, "w") as f:
        f.write("# Body-force volume integral over 3D fluid domain.\n")
        f.write("# F_body_y = Force * V_fluid_3D   (constant F per unit volume)\n")
        f.write("# For force balance, use <Force> (time-averaged) to match\n")
        f.write("# time-averaged P_mean in VTK.  See 34.dat for verification.\n")
        f.write(f"# metadata source : {os.path.basename(meta_path)}\n")
        f.write(f"# mesh source     : {os.path.basename(mesh_path)}\n")
        if monitor_path:
            f.write(f"# monitor source  : {os.path.basename(monitor_path)}\n")
        f.write("#\n")
        f.write("# V_3D computed three ways for cross-validation:\n")
        f.write("#   A trapezoidal in y     (assumes y depends only on j)\n")
        f.write("#   B quadrilateral shoelace (general curvilinear)\n")
        f.write("#   C Jacobian Riemann sum   (computational cell volumes)\n")
        f.write("#\n")
        f.write(f"Re                       = {Re}\n")
        f.write(f"F_metadata               = {F_meta:+.12e}\n")
        if F_avg is not None:
            f.write(f"F_time_avg               = {F_avg:+.12e}\n")
        f.write("\n")
        f.write(f"LX (spanwise)            = {LX:+.12e}\n")
        f.write(f"y(k) max deviation       = {y_dev_k:+.3e}\n")
        f.write("\n")
        f.write(f"A_2D_trapezoidal         = {A_trap:+.12e}\n")
        f.write(f"A_2D_shoelace            = {A_shoe:+.12e}\n")
        f.write(f"A_2D_jacobian            = {A_jac:+.12e}\n")
        f.write("\n")
        f.write(f"V_3D_trapezoidal         = {V_trap:+.12e}\n")
        f.write(f"V_3D_shoelace            = {V_shoe:+.12e}\n")
        f.write(f"V_3D_jacobian            = {V_jac:+.12e}\n")
        f.write(f"V_3D_max_relative_spread = {rel_dev:+.3e}\n")
        f.write("\n")
        f.write(f"F_body_y                 = {F_body:+.12e}\n")
        if F_body_avg is not None:
            f.write(f"F_body_y_final           = {F_body_final:+.12e}\n")

    print(f"\nsaved -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
