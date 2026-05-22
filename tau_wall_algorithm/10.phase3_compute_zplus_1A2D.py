










# -*- coding: utf-8 -*-
"""
10.phase3_compute_zplus_1A2D.py
===============================

Comparison pipeline: SPAN-AVERAGE u_tangent FIRST, then run the
wall-shear / z+ chain in 1D.

Why a separate pipeline?
------------------------
The default chain (steps 5/6/8) keeps the (i, j) span-stream grid alive
all the way to z+, which shows the local (per-(i,j)) viscous wall unit.
Span-averaging that 2D z+ field afterwards (as 9.plot_zplus.py does)
gives <z+>_x via Jensen's inequality:

     <(1/niu) sqrt(tau(i,j)) d_n(j)>_x  =  (d_n(j)/niu) <sqrt(tau)>_x

This script instead averages the SOURCE FIELD u_tangent over span first,
then derives a single 1D wall shear stress per j:

     tau_1d(j) = niu * [(h_xi/J) du_t_avg/d_zeta
                        - (e/(h_xi J)) du_t_avg/dxi]
     u_tau_1d  = sqrt(|tau_1d| / rho)
     z+_1d(j)  = u_tau_1d(j) * d_n(j) / niu

Comparison with the 2D-pipeline span-averaged z+
    z+_1D     = sqrt(|<tau_2D>_x|/rho) * d_n / niu  (this script)
    <z+_2D>_x = <sqrt(|tau_2D|/rho)>_x  * d_n / niu (9.plot_zplus.py)

Two inequalities interact:
    Jensen on concave sqrt:   <sqrt(|.|)>_x <= sqrt(<|.|>_x)
    abs of mean:              |<tau>_x|     <= <|tau|>_x   (with eq if
                                                            tau keeps sign
                                                            across span)

Result:
  - Where tau keeps a single sign across span (attached flow):
        z+_1D >= <z+_2D>_x      (Jensen-dominated)
  - Where tau changes sign across span (separation/reattachment lines):
        z+_1D <  <z+_2D>_x      (cancellation in |<tau>_x|
                                  beats Jensen suppression in <sqrt(.)>_x)

So the gap is direction-dependent and identifies which regions have
non-trivial span-wise tau structure.

Inputs (auto-detected, must be unique)
--------------------------------------
    2.<stem>.dat                    full 2D mesh (y, z)
    5.Re<X>_utan_*_k0-6.dat         bottom 7-layer utan (2D in (j, i))
    6.Re<X>_utan_*_k*-*.dat         top    7-layer utan
    Input/variables.h               niu, Uref, Re

Outputs (7 files in Output/)
----------------------------
    17.Re<X>_utan_spanavg_j257_k0-6.dat            (k_layer, j) bot
    18.Re<X>_utan_spanavg_j257_k122-128.dat        (k_layer, j) top
    19.Re<X>_j<Ny>_bottomtauwall_spanavg.dat       1D tau bot
    20.Re<X>_j<Ny>_toptauwall_spanavg.dat          1D tau top
    21.Re<X>_j<Ny>_zplus_bottom_spanavg.dat        1D z+ bot simple
    22.Re<X>_j<Ny>_zplus_top_spanavg.dat           1D z+ top simple
    23.Re<X>_j<Ny>_zplus_bottom_normal_spanavg.dat 1D z+ bot n-projection
"""

from __future__ import annotations
import argparse
import os
import re
import sys
import time
from typing import Dict, List, Tuple

import numpy as np

# ---- I/O directory bootstrap ---------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "Reference"))
INPUT_DIR     = os.path.join(_HERE, "Input")
OUTPUT_DIR    = os.path.join(_HERE, "Output")
REFERENCE_DIR = os.path.join(_HERE, "Reference")
os.makedirs(OUTPUT_DIR, exist_ok=True)
# --------------------------------------------------------------------------

from phase1_common import (
    FD6_FWD,
    FD6_BWD,
    TAU_CONVENTION_LABEL,
    WALL_DAT_COLUMNS,
    WALL_DAT_NCOLS,
    d_dj_periodic_row,
    d_dxi_periodic_2d_axis0,
    parse_tecplot_2d_mesh,
    find_unique_matching,
    parse_re_token,
    auto_detect_variables_h,
    parse_header_constants,
    find_const,
)


# ============================================================================
#  Auto-detection
# ============================================================================
_MESH_RE = re.compile(r"^2\.j.+\.dat$",                          re.IGNORECASE)
_BOT_RE  = re.compile(r"^5\..+_utan_.*_k0-6\.dat$",              re.IGNORECASE)
_TOP_RE  = re.compile(r"^6\..+_utan_.*_k\d+-\d+\.dat$",          re.IGNORECASE)


def auto_detect_mesh(folder=OUTPUT_DIR) -> str:
    return find_unique_matching(folder, "2.*.dat", _MESH_RE)


def auto_detect_bot_utan(folder=OUTPUT_DIR) -> str:
    return find_unique_matching(folder, "5.*.dat", _BOT_RE)


def auto_detect_top_utan(folder=OUTPUT_DIR) -> str:
    return find_unique_matching(folder, "6.*.dat", _TOP_RE)


# ============================================================================
#  Parsers — read 5.dat / 6.dat (15-col Tecplot POINT)
# ============================================================================
def load_utan_full(path: str
                   ) -> Tuple[np.ndarray, Dict[str, np.ndarray],
                              np.ndarray, np.ndarray, np.ndarray, List[int]]:
    """Read 5.*.dat or 6.*.dat (n_layers * Ny * Nx rows, 15 cols).

    Returns
    -------
    u_t      (n_layers, Ny, Nx)  u_tangent column
    wall_m   dict with 1D-in-j keys: h_xi, J, eXZ, y_kn, z_kn
    x_arr    (Nx,)
    y_2d_lyr (n_layers, Ny)   y at each k-layer (constant in i within layer)
    z_2d_lyr (n_layers, Ny)   z at each k-layer
    k_layers list of k-indices in the file
    """
    print(f"  reading {path} ...")
    t0 = time.time()
    data = np.loadtxt(path, skiprows=4)
    if data.shape[1] != WALL_DAT_NCOLS:
        raise ValueError(
            f"expected {WALL_DAT_NCOLS} columns, got {data.shape[1]}")
    print(f"    {data.shape[0]:,} rows  ({time.time() - t0:.1f}s)")

    k_col = data[:, WALL_DAT_COLUMNS["k"]].astype(int)
    j_col = data[:, WALL_DAT_COLUMNS["j"]].astype(int)
    i_col = data[:, WALL_DAT_COLUMNS["i"]].astype(int)
    k_layers = sorted(set(k_col.tolist()))
    n_layers = len(k_layers)
    Ny = int(j_col.max()) + 1
    Nx = int(i_col.max()) + 1
    if data.shape[0] != n_layers * Ny * Nx:
        raise ValueError(
            f"row count {data.shape[0]} != n_layers*Ny*Nx "
            f"({n_layers}*{Ny}*{Nx} = {n_layers*Ny*Nx})")

    u_t = data[:, WALL_DAT_COLUMNS["u_tangent"]].reshape(n_layers, Ny, Nx)
    # x varies only with i (first Nx rows: k=k_layers[0], j=0)
    x_arr = data[0:Nx, WALL_DAT_COLUMNS["x"]].copy()
    # y, z vary with (k_layer, j) but constant in i within each (k,j)
    y_2d_lyr = data[:, WALL_DAT_COLUMNS["y"]].reshape(n_layers, Ny, Nx)[:, :, 0]
    z_2d_lyr = data[:, WALL_DAT_COLUMNS["z"]].reshape(n_layers, Ny, Nx)[:, :, 0]

    # wall-row metric: constant in i and k -> read at i=0 of layer 0
    base = data[0:Ny * Nx:Nx]
    wall_m = {
        "h_xi": base[:, WALL_DAT_COLUMNS["h_xi"]].copy(),
        "J":    base[:, WALL_DAT_COLUMNS["J"]].copy(),
        "eXZ":  base[:, WALL_DAT_COLUMNS["e_xi.e_zeta"]].copy(),
        "y_kn": base[:, WALL_DAT_COLUMNS["y_kn"]].copy(),
        "z_kn": base[:, WALL_DAT_COLUMNS["z_kn"]].copy(),
    }
    return u_t, wall_m, x_arr, y_2d_lyr, z_2d_lyr, k_layers


# ============================================================================
#  variables.h parser
# ============================================================================
#  Single source of truth lives in phase1_common: auto_detect_variables_h,
#  parse_header_constants, find_const (all imported above).  No local copy.


# ============================================================================
#  Wall-normal direction (recomputed from 2D mesh, matches step 4)
# ============================================================================
def compute_wall_normal(y_2d, z_2d, k_wall, L_stream):
    y_xi = d_dj_periodic_row(y_2d[k_wall], period_offset=L_stream)
    z_xi = d_dj_periodic_row(z_2d[k_wall], period_offset=0.0)
    h_xi = np.sqrt(y_xi ** 2 + z_xi ** 2)
    n_y  = -z_xi / h_xi
    n_z  =  y_xi / h_xi
    return n_y, n_z, h_xi


# ============================================================================
#  Output writers
# ============================================================================
def write_utau_1d_txt(path: str, label: str, Re: int, niu: float,
                      Nx_orig: int, Ny: int,
                      y_wall: np.ndarray, z_wall: np.ndarray,
                      u_tau: np.ndarray,
                      generator: str, formula: str) -> None:
    """Write 1D u_tau as a function of y/h to TXT."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# 1D friction velocity on {label} wall (span-avg-first pipeline)\n")
        f.write(f"# generated by {generator}\n")
        f.write(f"# Re = {Re}, niu = {niu:.6e}\n")
        f.write(f"# Span points averaged before FD: Nx = {Nx_orig}\n")
        f.write("#\n")
        f.write("# Mathematical definition (lattice Boltzmann convention):\n")
        f.write("#   <u_t>_x(j) = (1/Nx) * sum_i u_t(i,j)   "
                "[span-average BEFORE FD]\n")
        f.write("#   tau_1d(j) = niu * d<u_t>_x/dn|_wall     "
                "[lattice shear stress]\n")
        f.write(f"#   {formula}\n")
        f.write("#\n")
        f.write(f"# Ny = {Ny} (streamwise points)\n")
        f.write(f"# {'j':>4s}  {'y/h':>18s}  {'z_wall':>18s}  "
                f"{'u_tau':>18s}\n")
        for j in range(Ny):
            f.write(f"  {j:4d}  {y_wall[j]:18.12e}  {z_wall[j]:18.12e}  "
                    f"{u_tau[j]:18.12e}\n")


def write_utan_spanavg_dat(path, label, u_t_1d, k_layers,
                           y_per_layer, z_per_layer, wall_m,
                           source_path, Nx_orig):
    """17.dat / 18.dat: span-averaged u_t per (k_layer, j).

    7-column Tecplot: k j y z u_t_avg h_xi e_xi.e_zeta
    (the wall-row metric h_xi, J, eXZ are constant in k-layer; we still
    write them on every row for self-containedness.)
    """
    n_layers, Ny = u_t_1d.shape
    chunks = []
    chunks.append(f"# Span-averaged u_tangent on {label} wall layer slab\n")
    chunks.append("# generated by 10.phase3_compute_zplus_1A2D.py\n")
    chunks.append(f"# source        : {os.path.basename(source_path)}\n")
    chunks.append(f"# averaging     : arithmetic mean over {Nx_orig} span "
                  "(i) points per (k, j)\n")
    chunks.append(f"# k_layers      : {k_layers}\n")
    chunks.append("# columns       : k  j  y(k,j)  z(k,j)  u_t_avg(k,j)  "
                  "h_xi(j)  J(j)  e_xi.e_zeta(j)\n")
    k_lo, k_hi = min(k_layers), max(k_layers)
    chunks.append(f'TITLE     = "Span-avg u_t on {label} wall '
                  f'(k={k_lo}..{k_hi})"\n')
    chunks.append('VARIABLES = "k" "j" "y" "z" "u_t_avg" "h_xi" "J" "e_xi.e_zeta"\n')
    chunks.append(f'ZONE T="{label}_utan_spanavg", J={Ny}, K={n_layers}, F=POINT\n')
    chunks.append('DT=(SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE)\n')
    for n, k_act in enumerate(k_layers):
        for j in range(Ny):
            chunks.append(
                f"{k_act:4d} {j:4d} "
                f"{y_per_layer[n, j]:.15e} {z_per_layer[n, j]:.15e} "
                f"{u_t_1d[n, j]:.15e} "
                f"{wall_m['h_xi'][j]:.15e} {wall_m['J'][j]:.15e} "
                f"{wall_m['eXZ'][j]:.15e}\n"
            )
    with open(path, "w") as f:
        f.writelines(chunks)


def write_tauwall_1d_dat(path, label, x_dummy, y_wall, z_wall,
                         du_t_dxi, du_t_dzeta, h_xi, J, eXZ,
                         du_t_dn, tau_signed, tau_abs,
                         convention_label, source_path, Nx_orig):
    """19.dat / 20.dat: 1D τ_wall (along j only)."""
    Ny = du_t_dn.shape[0]
    chunks = []
    chunks.append(f"# Span-averaged tau_wall on {label} wall, 1D in j\n")
    chunks.append("# generated by 10.phase3_compute_zplus_1A2D.py\n")
    chunks.append(f"# source utan  : {os.path.basename(source_path)}\n")
    chunks.append(f"# averaging    : u_t span-averaged over {Nx_orig} i-points "
                  "BEFORE FD\n")
    chunks.append(f"# convention   : {convention_label}\n")
    chunks.append(f'TITLE     = "tau_wall on {label} (span-avg pipeline)"\n')
    chunks.append('VARIABLES = "j" "y" "z" "du_t_dxi" "du_t_dzeta" "h_xi" "J" '
                  '"e_xi.e_zeta" "du_t_dn" "tau_signed" "tau_abs"\n')
    chunks.append(f'ZONE T="{label}_tau_spanavg", I={Ny}, F=POINT\n')
    chunks.append('DT=(SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE '
                  'SINGLE SINGLE SINGLE)\n')
    for j in range(Ny):
        chunks.append(
            f"{j:4d} {y_wall[j]:.15e} {z_wall[j]:.15e} "
            f"{du_t_dxi[j]:.15e} {du_t_dzeta[j]:.15e} "
            f"{h_xi[j]:.15e} {J[j]:.15e} {eXZ[j]:.15e} "
            f"{du_t_dn[j]:.15e} "
            f"{tau_signed[j]:.15e} {tau_abs[j]:.15e}\n"
        )
    with open(path, "w") as f:
        f.writelines(chunks)


def write_zplus_1d_dat(path, label, mode, y_wall, z_wall,
                       tau_local, u_tau_local, d_n, z_plus,
                       extra_cols=None, source_tau=None,
                       Re=0):
    """21/22/23.dat: 1D z+ (along j only).

    extra_cols (for 23.dat n-projection): dict with keys
       n_y, n_z, dy_step, dz_step
    """
    Ny = z_plus.shape[0]
    chunks = []
    chunks.append(f"# 1D z+ on {label} wall ({mode}), span-avg pipeline\n")
    chunks.append("# generated by 10.phase3_compute_zplus_1A2D.py\n")
    chunks.append(f"# source tau   : {os.path.basename(source_tau)}\n")
    chunks.append(f"# Re           = {Re}\n")
    chunks.append("# z_plus(j) = u_tau_local(j) * d_n(j) / niu  "
                  "(textbook y+, lattice convention)\n")
    if extra_cols is None:
        chunks.append(f'TITLE     = "1D z+ on {label} wall ({mode})"\n')
        chunks.append('VARIABLES = "j" "y" "z" "tau_local" "u_tau_local" '
                      '"d_n" "z_plus"\n')
        chunks.append(f'ZONE T="{label}_zplus_1d_{mode}", I={Ny}, F=POINT\n')
        chunks.append('DT=(SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE)\n')
        for j in range(Ny):
            chunks.append(
                f"{j:4d} {y_wall[j]:.15e} {z_wall[j]:.15e} "
                f"{tau_local[j]:.15e} {u_tau_local[j]:.15e} "
                f"{d_n[j]:.15e} {z_plus[j]:.15e}\n"
            )
    else:
        chunks.append(f'TITLE     = "1D z+ on {label} wall ({mode})"\n')
        chunks.append('VARIABLES = "j" "y" "z" "tau_local" "u_tau_local" '
                      '"dy_step" "dz_step" "n_y" "n_z" "d_n_proj" "z_plus_proj"\n')
        chunks.append(f'ZONE T="{label}_zplus_1d_{mode}", I={Ny}, F=POINT\n')
        chunks.append('DT=(SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE '
                      'SINGLE SINGLE SINGLE SINGLE)\n')
        for j in range(Ny):
            chunks.append(
                f"{j:4d} {y_wall[j]:.15e} {z_wall[j]:.15e} "
                f"{tau_local[j]:.15e} {u_tau_local[j]:.15e} "
                f"{extra_cols['dy_step'][j]:.15e} "
                f"{extra_cols['dz_step'][j]:.15e} "
                f"{extra_cols['n_y'][j]:.15e} {extra_cols['n_z'][j]:.15e} "
                f"{d_n[j]:.15e} {z_plus[j]:.15e}\n"
            )
    with open(path, "w") as f:
        f.writelines(chunks)


# ============================================================================
#  Main
# ============================================================================
def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Span-average u_tangent first, then derive 1D tau_wall "
                    "and z+. Outputs 17-23.")
    p.add_argument("--bot",     default=None,
                   help="5.*.dat (bottom utan slab; auto-detect)")
    p.add_argument("--top",     default=None,
                   help="6.*.dat (top utan slab; auto-detect)")
    p.add_argument("--mesh",    default=None,
                   help="2.*.dat (full 2D mesh; auto-detect)")
    p.add_argument("--variables-h", default=None,
                   help="Input/variables.h (auto-detect)")
    p.add_argument("--niu",     type=float, default=None,
                   help="kinematic viscosity (overrides variables.h)")
    p.add_argument("--Uref",    type=float, default=None,
                   help="reference velocity (auto-detect from variables.h)")
    args = p.parse_args(argv)

    bot_path  = args.bot         or auto_detect_bot_utan()
    top_path  = args.top         or auto_detect_top_utan()
    mesh_path = args.mesh        or auto_detect_mesh()
    var_h     = args.variables_h or auto_detect_variables_h(INPUT_DIR)

    print(f"input bot utan : {bot_path}")
    print(f"input top utan : {top_path}")
    print(f"input mesh     : {mesh_path}")
    print(f"input vars.h   : {var_h}")

    # ---- niu, Uref ----
    consts = parse_header_constants(var_h) if var_h else {}
    if args.niu is not None:
        niu = args.niu
    else:
        niu = find_const(consts, ["niu", "nu"], var_h or "")
    if args.Uref is not None:
        Uref = args.Uref
    else:
        Uref = find_const(consts, ["Uref", "U_ref"], var_h or "")
    Re = int(round(Uref / niu))   # niu = Uref/Re => Re = Uref/niu
    multiplier = niu               # step 4 already rescaled to lattice units
    convention_label = TAU_CONVENTION_LABEL
    print(f"\nniu (= mu) = {niu:.6e}, Uref = {Uref:.6e}, Re = {Re}")
    print(f"convention : {convention_label}")
    print(f"multiplier = niu = {multiplier:.6e}")

    # ---- [1] Load wall slabs ----
    print("\n[1] loading utan slabs ...")
    u_t_bot, m_bot, x_arr, y_lyr_bot, z_lyr_bot, k_layers_bot = load_utan_full(bot_path)
    u_t_top, m_top, _,     y_lyr_top, z_lyr_top, k_layers_top = load_utan_full(top_path)
    n_layers_bot, Ny, Nx = u_t_bot.shape
    print(f"  shape (n_layers, Ny, Nx) = ({n_layers_bot}, {Ny}, {Nx})")
    print(f"  bottom k_layers = {k_layers_bot}")
    print(f"  top    k_layers = {k_layers_top}")

    # ---- [2] Span-average u_t over i ----
    print("\n[2] span-averaging u_t over i ...")
    u_t_bot_avg = u_t_bot.mean(axis=2)        # (n_layers, Ny)
    u_t_top_avg = u_t_top.mean(axis=2)
    print(f"  bot u_t_avg range [{u_t_bot_avg.min():+.4e}, {u_t_bot_avg.max():+.4e}]")
    print(f"  top u_t_avg range [{u_t_top_avg.min():+.4e}, {u_t_top_avg.max():+.4e}]")
    # span-scatter info: coefficient of variation in i for u_t at wall row
    cv_bot = u_t_bot[0].std(axis=1) / (np.abs(u_t_bot[0]).mean(axis=1) + 1e-30)
    cv_top = u_t_top[-1].std(axis=1) / (np.abs(u_t_top[-1]).mean(axis=1) + 1e-30)
    print(f"  span CV of u_t at wall row:  bot max = {cv_bot.max():.3%}, "
          f"top max = {cv_top.max():.3%}")

    # ---- [3] Write 17/18.dat ----
    print("\n[3] writing 19/20.dat (span-avg u_t slabs) ...")
    out17 = os.path.join(OUTPUT_DIR,
                         f"19.Re{Re}_utan_spanavg_j{Ny}_k0-6.dat")
    out18 = os.path.join(OUTPUT_DIR,
                         f"20.Re{Re}_utan_spanavg_j{Ny}_k{k_layers_top[0]}-{k_layers_top[-1]}.dat")
    write_utan_spanavg_dat(out17, "bottom", u_t_bot_avg, k_layers_bot,
                           y_lyr_bot, z_lyr_bot, m_bot, bot_path, Nx)
    write_utan_spanavg_dat(out18, "top",    u_t_top_avg, k_layers_top,
                           y_lyr_top, z_lyr_top, m_top, top_path, Nx)
    print(f"  wrote {os.path.basename(out17)} ({os.path.getsize(out17):,} B)")
    print(f"  wrote {os.path.basename(out18)} ({os.path.getsize(out18):,} B)")

    # ---- [4] FD at wall: du_t/dzeta + du_t/dxi ----
    print("\n[4] FD at wall (1D pipeline) ...")
    # bottom: forward 6th-order Fornberg using all 7 layers
    dut_dzeta_bot = FD6_FWD @ u_t_bot_avg                 # (Ny,)
    # top: backward 6th-order Fornberg
    dut_dzeta_top = FD6_BWD @ u_t_top_avg                 # (Ny,)
    # du_t/dxi at wall row: 6th-order central with periodic wrap
    u_t_bot_wall = u_t_bot_avg[0]                          # (Ny,)
    u_t_top_wall = u_t_top_avg[-1]
    # use the 1D version of d_dxi (acts on shape (J,) row)
    dut_dxi_bot = d_dj_periodic_row(u_t_bot_wall, period_offset=0.0)
    dut_dxi_top = d_dj_periodic_row(u_t_top_wall, period_offset=0.0)
    print(f"  bot du_t/dzeta range [{dut_dzeta_bot.min():+.4e}, "
          f"{dut_dzeta_bot.max():+.4e}]")
    print(f"  top du_t/dzeta range [{dut_dzeta_top.min():+.4e}, "
          f"{dut_dzeta_top.max():+.4e}]")
    print(f"  bot du_t/dxi   range [{dut_dxi_bot.min():+.4e}, "
          f"{dut_dxi_bot.max():+.4e}]")

    # ---- [5] du_t/dn = (h_xi/J) du/dzeta - (e/(h_xi J)) du/dxi ----
    print("\n[5] chain rule du_t/dn ...")
    A_bot = m_bot["h_xi"] / m_bot["J"]
    B_bot = m_bot["eXZ"] / (m_bot["h_xi"] * m_bot["J"])
    A_top = m_top["h_xi"] / m_top["J"]
    B_top = m_top["eXZ"] / (m_top["h_xi"] * m_top["J"])
    dut_dn_bot = A_bot * dut_dzeta_bot - B_bot * dut_dxi_bot
    dut_dn_top = A_top * dut_dzeta_top - B_top * dut_dxi_top
    print(f"  bot du_t/dn range [{dut_dn_bot.min():+.4e}, {dut_dn_bot.max():+.4e}]")
    print(f"  top du_t/dn range [{dut_dn_top.min():+.4e}, {dut_dn_top.max():+.4e}]")

    # ---- [6] tau = niu * du_t/dn  (lattice stress, rho = 1) ----
    print(f"\n[6] tau_wall = niu * du_t/dn = {multiplier:.4e} * du_t/dn "
          f"({convention_label})")
    tau_bot_signed = multiplier * dut_dn_bot
    tau_top_signed = multiplier * dut_dn_top
    tau_bot_abs    = np.abs(tau_bot_signed)
    tau_top_abs    = np.abs(tau_top_signed)
    print(f"  bot tau_signed range [{tau_bot_signed.min():+.4e}, "
          f"{tau_bot_signed.max():+.4e}]")
    print(f"  top tau_signed range [{tau_top_signed.min():+.4e}, "
          f"{tau_top_signed.max():+.4e}]")

    # Write 19/20.dat
    out19 = os.path.join(OUTPUT_DIR,
                         f"21.Re{Re}_j{Ny}_bottomtauwall_spanavg.dat")
    out20 = os.path.join(OUTPUT_DIR,
                         f"22.Re{Re}_j{Ny}_toptauwall_spanavg.dat")
    write_tauwall_1d_dat(out19, "bottom", x_arr,
                         y_lyr_bot[0], z_lyr_bot[0],
                         dut_dxi_bot, dut_dzeta_bot,
                         m_bot["h_xi"], m_bot["J"], m_bot["eXZ"],
                         dut_dn_bot, tau_bot_signed, tau_bot_abs,
                         convention_label, bot_path, Nx)
    write_tauwall_1d_dat(out20, "top",    x_arr,
                         y_lyr_top[-1], z_lyr_top[-1],
                         dut_dxi_top, dut_dzeta_top,
                         m_top["h_xi"], m_top["J"], m_top["eXZ"],
                         dut_dn_top, tau_top_signed, tau_top_abs,
                         convention_label, top_path, Nx)
    print(f"  wrote {os.path.basename(out19)} ({os.path.getsize(out19):,} B)")
    print(f"  wrote {os.path.basename(out20)} ({os.path.getsize(out20):,} B)")

    # ---- [7] z+ via simple |delta_z| ----
    print("\n[7] z+ (1D) using simple |delta_z| ...")
    y_2d, z_2d, J_mesh, K_mesh = parse_tecplot_2d_mesh(mesh_path)
    L_stream = float(y_2d[0, -1] - y_2d[0, 0])
    Ny_mesh, Nz_mesh = J_mesh, K_mesh
    if Ny_mesh != Ny:
        print(f"[error] mesh Ny={Ny_mesh} != utan Ny={Ny}", file=sys.stderr)
        sys.exit(1)

    u_tau_bot_1d = np.sqrt(tau_bot_abs)        # rho = 1, lattice u_tau
    u_tau_top_1d = np.sqrt(tau_top_abs)

    dz_bot_step = z_2d[1, :]         - z_2d[0, :]
    dy_bot_step = y_2d[1, :]         - y_2d[0, :]
    dz_top_step = z_2d[Nz_mesh - 2,:] - z_2d[Nz_mesh - 1, :]
    delta_z_simple_bot = np.abs(dz_bot_step)
    delta_z_simple_top = np.abs(dz_top_step)

    z_plus_bot_simple = u_tau_bot_1d * delta_z_simple_bot / niu
    z_plus_top_simple = u_tau_top_1d * delta_z_simple_top / niu

    out21 = os.path.join(OUTPUT_DIR,
                         f"23.Re{Re}_j{Ny}_zplus_bottom_spanavg.dat")
    out22 = os.path.join(OUTPUT_DIR,
                         f"24.Re{Re}_j{Ny}_zplus_top_spanavg.dat")
    write_zplus_1d_dat(out21, "bottom", "simple",
                       y_2d[0, :], z_2d[0, :],
                       tau_bot_abs, u_tau_bot_1d,
                       delta_z_simple_bot, z_plus_bot_simple,
                       source_tau=out19, Re=Re)
    write_zplus_1d_dat(out22, "top", "simple",
                       y_2d[Nz_mesh - 1, :], z_2d[Nz_mesh - 1, :],
                       tau_top_abs, u_tau_top_1d,
                       delta_z_simple_top, z_plus_top_simple,
                       source_tau=out20, Re=Re)
    print(f"  bot z+_simple range [{z_plus_bot_simple.min():.4f}, "
          f"{z_plus_bot_simple.max():.4f}]")
    print(f"  top z+_simple range [{z_plus_top_simple.min():.4f}, "
          f"{z_plus_top_simple.max():.4f}]")

    # ---- [8] z+ via n̂ projection (bottom only) ----
    print("\n[8] z+ (1D) on bottom using n_hat projection ...")
    n_y, n_z, _ = compute_wall_normal(y_2d, z_2d, k_wall=0, L_stream=L_stream)
    d_n_proj_bot = np.abs(dy_bot_step * n_y + dz_bot_step * n_z)
    z_plus_bot_normal = u_tau_bot_1d * d_n_proj_bot / niu
    print(f"  d_n_proj range [{d_n_proj_bot.min():.4e}, {d_n_proj_bot.max():.4e}]")
    print(f"  z+_normal range [{z_plus_bot_normal.min():.4f}, "
          f"{z_plus_bot_normal.max():.4f}]")

    out23 = os.path.join(OUTPUT_DIR,
                         f"25.Re{Re}_j{Ny}_zplus_bottom_normal_spanavg.dat")
    write_zplus_1d_dat(out23, "bottom", "n-projection",
                       y_2d[0, :], z_2d[0, :],
                       tau_bot_abs, u_tau_bot_1d,
                       d_n_proj_bot, z_plus_bot_normal,
                       extra_cols={"n_y": n_y, "n_z": n_z,
                                   "dy_step": dy_bot_step,
                                   "dz_step": dz_bot_step},
                       source_tau=out19, Re=Re)

    # ---- [9] Comparison summary: 1D vs span-avg(2D) z+ ----
    print("\n[9] comparison vs 2D-pipeline span-averaged z+ ...")
    # Try to load existing 13.dat for comparison
    try:
        d13 = np.loadtxt(os.path.join(OUTPUT_DIR,
                                       f"15.Re{Re}_zplus_bottom.dat"),
                         skiprows=13)
        z_plus_2d_bot = d13[:, 8].reshape(Ny, Nx)
        zp_2d_avg_bot = z_plus_2d_bot.mean(axis=1)
        print(f"  bot 1D-pipeline z+ range  [{z_plus_bot_simple.min():.4f}, "
              f"{z_plus_bot_simple.max():.4f}]")
        print(f"  bot 2D-pipeline <z+>_x    [{zp_2d_avg_bot.min():.4f}, "
              f"{zp_2d_avg_bot.max():.4f}]")
        ratio = z_plus_bot_simple / (zp_2d_avg_bot + 1e-30)
        print(f"  ratio (1D / <2D>_x)  mean = {ratio.mean():.4f},  "
              f"min = {ratio.min():.4f},  max = {ratio.max():.4f}")
        print("  ratio > 1 in attached flow (Jensen-dominated)")
        print("  ratio < 1 where tau changes sign across span")
        print("    (sign-cancellation in |<tau>_x| beats sqrt-concavity)")
    except FileNotFoundError:
        print("  (skipped — 13.dat not present)")

    # ---- [10] u_tau 1D output ----
    print("\n[10] u_tau 1D output (span-avg pipeline) ...")
    gen = "10.phase3_compute_zplus_1A2D.py"
    formula = "u_tau_1d(j) = sqrt(|tau_1d(j)|)   [tau from span-avg u_t]"

    utau_bot_path = os.path.join(OUTPUT_DIR,
                                  f"39.Re{Re}_utau_1d_bottom_1A2D.txt")
    write_utau_1d_txt(utau_bot_path, "bottom", Re, niu, Nx, Ny,
                      y_lyr_bot[0], z_lyr_bot[0],
                      u_tau_bot_1d, gen, formula)
    print(f"  writing -> {utau_bot_path}")

    utau_top_path = os.path.join(OUTPUT_DIR,
                                  f"40.Re{Re}_utau_1d_top_1A2D.txt")
    write_utau_1d_txt(utau_top_path, "top", Re, niu, Nx, Ny,
                      y_lyr_top[-1], z_lyr_top[-1],
                      u_tau_top_1d, gen, formula)
    print(f"  writing -> {utau_top_path}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
