#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
14.phase4_compute_total_drag.py
================================

Compute the TOTAL streamwise viscous force from the two walls in the fixed
global e_y direction, using SIGNED tau_wall_local (no |tau| anywhere in the
force integration).

Procedure (matches the lattice formulation in step 5):

    tau_local(i,j)     = niu * du_t/dn      <- signed, lattice stress
                         (read from 7/8.*.dat tau_wall_signed column)

    tau_cell(j,i)      = 4-corner average of tau_local
    dA_cell(j,i)       = dx_i * sqrt(dy_j^2 + dz_j^2)
    t_y_cell(j)        = dy_j / sqrt(dy_j^2 + dz_j^2)        # streamwise
                                                              tangent y-component

    dTau_y(j,i)        = tau_cell(j,i) * t_y_cell(j) * dA_cell(j,i)

    The tau files store wall-local tangential stress.  Force balance needs a
    fixed Cartesian component, so the projection to e_y happens per cell before
    the sum.  The sign convention differs by wall because the bottom-wall
    normal used for tau is into the fluid, while the top-wall zeta direction is
    the fluid outward normal:

        F_vis_bottom_y_on_fluid = -sum(dTau_y)
        F_vis_top_y_on_fluid    = +sum(dTau_y)
        F_vis_y_on_fluid        = F_vis_bottom_y_on_fluid
                                + F_vis_top_y_on_fluid

    For positive streamwise body force, the wall force on the fluid is normally
    negative.  The positive drag magnitude is therefore

        SUM_F_vis = -F_vis_y_on_fluid

Assumption:
    Body force points in +e_y (channel-driving convention).

Output: 32.Re<X>_total_Fvics.dat
"""

from __future__ import annotations
import argparse
import glob
import os
import re
import sys

import numpy as np

# bootstrap: add Reference/ to sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "Reference"))

from phase1_common import (   # noqa: E402
    TAU_CONVENTION_LABEL,
    load_tauwall_dat,
    cell_average_2d,
    cell_areas_2d,
    verify_lattice_tau_dat,
)


def find_dat(folder: str, num: int, suffix: str) -> str:
    pattern = os.path.join(folder, f"{num}.Re*{suffix}")
    hits = sorted(glob.glob(pattern))
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise FileNotFoundError(f"no file matching {pattern}")
    raise FileNotFoundError(f"multiple matches for {pattern}: {hits}")


def streamwise_drag_per_wall(tau_path: str, wall: str) -> dict:
    """Compute streamwise viscous drag for one wall.

    Returns dict with:
        Nx, Ny           : grid shape
        A_wall           : total wall area
        tau_t_y_integral : signed integral of tau * t_y * dA over the wall
        F_vis_y          : global e_y force exerted by the wall on the fluid
        n_cells          : (Ny-1) * (Nx-1)
    """
    if wall not in ("bottom", "top"):
        raise ValueError(f"wall must be 'bottom' or 'top', got {wall!r}")

    verify_lattice_tau_dat(tau_path, f"{wall} tau input")
    d = load_tauwall_dat(tau_path)
    Nx, Ny = d["Nx"], d["Ny"]
    x, y, z = d["x"], d["y"], d["z"]
    tau_signed = d["tau_signed"]                          # (Ny, Nx)

    tau_cell = cell_average_2d(tau_signed)                # (Ny-1, Nx-1)
    dA_cell  = cell_areas_2d(x, y, z)                     # (Ny-1, Nx-1)

    dy = np.diff(y)                                       # (Ny-1,)
    dz = np.diff(z)                                       # (Ny-1,)
    ds = np.sqrt(dy * dy + dz * dz)                       # (Ny-1,)
    t_y_cell = dy / ds                                    # (Ny-1,)

    d_tau_y = tau_cell * t_y_cell[:, None] * dA_cell      # (Ny-1, Nx-1)
    tau_t_y_integral = float(d_tau_y.sum())

    # Bottom tau is differentiated along the inward wall normal from wall to
    # fluid, so wall-on-fluid force is the opposite of tau*t.  Top tau uses the
    # outward zeta direction, so its signed tau*t projection is already the
    # wall-on-fluid y component.
    if wall == "bottom":
        F_vis_y = -tau_t_y_integral
    else:
        F_vis_y = tau_t_y_integral
    D_vis = -F_vis_y

    return dict(
        Nx=Nx, Ny=Ny,
        A_wall=float(dA_cell.sum()),
        tau_t_y_integral=tau_t_y_integral,
        F_vis_y=F_vis_y,
        D_vis=D_vis,
        n_cells=dA_cell.size,
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Compute total streamwise viscous force in global e_y.")
    ap.add_argument("--folder", default="Output",
                    help="folder with 7/8.*.dat (default: Output)")
    ap.add_argument("--bot", default=None, help="7.*.dat path")
    ap.add_argument("--top", default=None, help="8.*.dat path")
    args = ap.parse_args(argv)

    folder = args.folder
    bot_path = args.bot or find_dat(folder, 7, "_i*j*_bottomtauwall.dat")
    top_path = args.top or find_dat(folder, 8, "_i*j*_toptauwall.dat")

    print(f"input bot : {bot_path}")
    print(f"input top : {top_path}")

    m = re.search(r"Re(\d+)", os.path.basename(bot_path))
    if not m:
        raise ValueError(f"could not parse Re token from filename: {bot_path}")
    Re = int(m.group(1))

    bot = streamwise_drag_per_wall(bot_path, "bottom")
    top = streamwise_drag_per_wall(top_path, "top")

    if (bot["Nx"], bot["Ny"]) != (top["Nx"], top["Ny"]):
        raise ValueError(
            f"shape mismatch: bot {bot['Nx']}x{bot['Ny']} "
            f"vs top {top['Nx']}x{top['Ny']}")

    tau_y_bot = bot["tau_t_y_integral"]
    tau_y_top = top["tau_t_y_integral"]

    F_vis_bottom_y = bot["F_vis_y"]
    F_vis_top_y = top["F_vis_y"]
    F_vis_y = F_vis_bottom_y + F_vis_top_y

    D_vis_bottom = bot["D_vis"]
    D_vis_top = top["D_vis"]
    F_total = D_vis_bottom + D_vis_top

    print()
    print(f"{'Re':<35}= {Re}")
    print(f"{'Nx, Ny (per wall)':<35}= {bot['Nx']}, {bot['Ny']}")
    print(f"{'cells per wall':<35}= {bot['n_cells']}")
    print()
    print(f"{'A_bottom':<35}= {bot['A_wall']:+.12e}")
    print(f"{'A_top':<35}= {top['A_wall']:+.12e}")
    print()
    print(f"{'tau_t_y_bottom_integral':<35}= {tau_y_bot:+.12e}")
    print(f"{'tau_t_y_top_integral':<35}= {tau_y_top:+.12e}")
    print()
    print(f"{'F_vis_bottom_y_on_fluid':<35}= {F_vis_bottom_y:+.12e}")
    print(f"{'F_vis_top_y_on_fluid':<35}= {F_vis_top_y:+.12e}")
    print(f"{'F_vis_y_on_fluid':<35}= {F_vis_y:+.12e}")
    print()
    print(f"{'D_vis_bottom (= -F_y)':<35}= {D_vis_bottom:+.12e}")
    print(f"{'D_vis_top    (= -F_y)':<35}= {D_vis_top:+.12e}")
    print(f"{'SUM_F_vis    (= -total F_y)':<35}= {F_total:+.12e}")

    stem = f"34.Re{Re}_total_Fvics"
    out_path = os.path.join(folder, f"{stem}.dat")
    with open(out_path, "w") as f:
        f.write("# Total streamwise viscous force in fixed global e_y.\n")
        f.write(f"# bottom source : {os.path.basename(bot_path)}\n")
        f.write(f"# top    source : {os.path.basename(top_path)}\n")
        f.write(f"# tau convention: {TAU_CONVENTION_LABEL}\n")
        f.write("#\n")
        f.write("# Per cell:  dTau_y = tau_signed * t_y * dA\n")
        f.write("# Project to fixed e_y before summing across cells.\n")
        f.write("# Wall-on-fluid signs:\n")
        f.write("#   bottom F_vis_y = -sum(dTau_y)\n")
        f.write("#   top    F_vis_y = +sum(dTau_y)\n")
        f.write("# Positive drag magnitude for +e_y body force: D_vis = -F_vis_y.\n")
        f.write("#\n")
        f.write(f"Re                       = {Re}\n")
        f.write(f"Nx                       = {bot['Nx']}\n")
        f.write(f"Ny                       = {bot['Ny']}\n")
        f.write(f"cells_per_wall           = {bot['n_cells']}\n")
        f.write("\n")
        f.write(f"A_bottom                 = {bot['A_wall']:+.12e}\n")
        f.write(f"A_top                    = {top['A_wall']:+.12e}\n")
        f.write("\n")
        f.write(f"tau_t_y_bottom_integral  = {tau_y_bot:+.12e}\n")
        f.write(f"tau_t_y_top_integral     = {tau_y_top:+.12e}\n")
        f.write("# Legacy aliases for the projected tau*t_y integrals above.\n")
        f.write(f"F_vis_bottom_signed      = {tau_y_bot:+.12e}\n")
        f.write(f"F_vis_top_signed         = {tau_y_top:+.12e}\n")
        f.write("\n")
        f.write(f"F_vis_bottom_y           = {F_vis_bottom_y:+.12e}\n")
        f.write(f"F_vis_top_y              = {F_vis_top_y:+.12e}\n")
        f.write(f"F_vis_y                  = {F_vis_y:+.12e}\n")
        f.write("\n")
        f.write(f"SUM_F_vis_bottom         = {D_vis_bottom:+.12e}\n")
        f.write(f"SUM_F_vis_top            = {D_vis_top:+.12e}\n")
        f.write(f"SUM_F_vis                = {F_total:+.12e}\n")
    print(f"\nsaved -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
