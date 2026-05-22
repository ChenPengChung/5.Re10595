# -*- coding: utf-8 -*-
"""
12.phase3_compute_zplus_2nd1A2D.py
==================================

2nd-order finite-difference comparison pipeline.  Identical FRAMEWORK to
10.phase3_compute_zplus_1A2D.py (full chain-rule with Jacobian and
cross-coupling), but the velocity-derivative finite-difference orders
are dropped from 6 to 2:

    du_t/d_zeta  :  2nd-order one-sided FD  (forward at k=0, backward at k=NZ-1)
                    -- 3-point stencil (only k=0,1,2 / k=NZ-3..NZ-1 used)
    du_t/d_xi    :  2nd-order central FD with periodic wrap (3-point stencil)

The wall-row metric (h_xi, J, e_xi.e_zeta) is UNCHANGED -- still the
6th-order Fornberg result computed by step 4 and saved in 5/6.dat.
Only the velocity-derivative FD orders are degraded.

Why?
----
Step 10's 6th-order Fornberg FD on velocity has FD-coefficient L1 norm
~27.7, which amplifies high-frequency DNS statistical noise in V_mean
~14x more than 1st-order FD.  This script tests the middle ground:
2nd-order FD with L1 norm ~4 (about 7x less noise amplification than
6th-order, but 2x more than 1st-order, while keeping O(h^2) truncation
accuracy and the FULL chain-rule framework -- including the
non-orthogonal cross-coupling term that the 1st-order pipeline skipped).

Pipeline (per wall, identical structure to step 10)
---------------------------------------------------
    5/6.dat (3D u_t)  ──span-avg over i──>  17/18.dat (k_layer × j)
                                               │
                                               ▼  FD2 fwd/bwd (zeta) + FD2 central (xi)
                                       du_t/dn = (h_xi/J) du_t/dzeta
                                              -  (e_xi.e_zeta/(h_xi*J)) du_t/dxi
                                               │
                                               ▼  tau = niu * du_t/dn  (lattice stress)
                                       19/20.dat (1D tau_wall(j))
                                               │
                                               ▼  sqrt(|tau|/rho) * d_n / niu
                                       21/22.dat (1D z+ simple)
                                       23.dat   (1D z+ n-projection, bottom only)

(Output numbering is 25..29, parallel to 10.py's 19..23, so both
 pipelines coexist.)

Output filenames
----------------
    25.Re<X>_j<Ny>_bottomtauwall_spanavg_2nd.dat       1D tau bot
    26.Re<X>_j<Ny>_toptauwall_spanavg_2nd.dat          1D tau top
    27.Re<X>_j<Ny>_zplus_bottom_spanavg_2nd.dat        1D z+ bot, |dz|
    28.Re<X>_j<Ny>_zplus_top_spanavg_2nd.dat           1D z+ top, |dz|
    29.Re<X>_j<Ny>_zplus_bottom_normal_spanavg_2nd.dat 1D z+ bot, n-proj
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
    TAU_CONVENTION_LABEL,
    WALL_DAT_COLUMNS,
    WALL_DAT_NCOLS,
    d_dj_periodic_row,        # 6th-order, used for wall-normal n_hat only
    parse_tecplot_2d_mesh,
    find_unique_matching,
    auto_detect_variables_h,
    parse_header_constants,
    find_const,
)


# ============================================================================
#  2nd-order FD coefficients (the key change from step 10)
# ============================================================================
#  Forward 2nd-order at k=0 reads u(k=0), u(k=1), u(k=2):
#       f'(0) ≈ [ -3 f(0) + 4 f(1) - f(2) ] / (2 h)
FD2_FWD = np.array([-3.0, 4.0, -1.0]) / 2.0     # = [-1.5, 2.0, -0.5]

#  Backward 2nd-order at k=K-1 reads u(K-3), u(K-2), u(K-1):
#       f'(K-1) ≈ [ f(K-3) - 4 f(K-2) + 3 f(K-1) ] / (2 h)
FD2_BWD = np.array([1.0, -4.0, 3.0]) / 2.0     # = [0.5, -2.0, 1.5]

#  L1 norms (sum of |coefficients|): forward = 4, backward = 4
#  -> noise amplification factor 4 / h
#  Compare to 6th-order Fornberg: 27.7 / h (about 7x more noise sensitive)
#  Compare to 1st-order forward:  2.0 / h


def d_dj_periodic_row_2nd(f_row: np.ndarray,
                          period_offset: float = 0.0) -> np.ndarray:
    """2nd-order central FD on a 1D row, periodic wrap with optional offset.

    Convention (matches phase1_common.d_dj_periodic_row but 2nd-order
    instead of 6th-order):
        j = -1   reads f[J-2] = f[N-1]   minus period_offset
        j = N+1  reads f[1]              plus  period_offset
    """
    J = f_row.shape[0]
    pad_lo = f_row[J - 2:J - 1] - period_offset      # j = -1, length 1
    pad_hi = f_row[1:2]         + period_offset      # j = N+1, length 1
    f_ext = np.concatenate([pad_lo, f_row, pad_hi])  # length J + 2
    return (f_ext[2:J + 2] - f_ext[0:J]) / 2.0


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
#  Reader for 5/6.dat (identical to step 10)
# ============================================================================
def load_utan_full(path: str
                   ) -> Tuple[np.ndarray, Dict[str, np.ndarray],
                              np.ndarray, np.ndarray, np.ndarray, List[int]]:
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
    x_arr = data[0:Nx, WALL_DAT_COLUMNS["x"]].copy()
    y_2d_lyr = data[:, WALL_DAT_COLUMNS["y"]].reshape(n_layers, Ny, Nx)[:, :, 0]
    z_2d_lyr = data[:, WALL_DAT_COLUMNS["z"]].reshape(n_layers, Ny, Nx)[:, :, 0]

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
#  Wall-normal direction (same as step 10; 6th-order from mesh)
# ============================================================================
def compute_wall_normal(y_2d, z_2d, k_wall, L_stream):
    """Outward unit wall normal at the wall row k_wall.
    Uses the existing 6th-order d_dj_periodic_row from phase1_common
    (mesh quantities are smooth, no benefit to lowering FD order here).
    """
    y_xi = d_dj_periodic_row(y_2d[k_wall], period_offset=L_stream)
    z_xi = d_dj_periodic_row(z_2d[k_wall], period_offset=0.0)
    h_xi = np.sqrt(y_xi ** 2 + z_xi ** 2)
    n_y  = -z_xi / h_xi
    n_z  =  y_xi / h_xi
    return n_y, n_z, h_xi


# ============================================================================
#  Output writers (same Tecplot format as step 10)
# ============================================================================
def write_utau_1d_txt(path: str, label: str, Re: int, niu: float,
                      Nx_orig: int, Ny: int,
                      y_wall: np.ndarray, z_wall: np.ndarray,
                      u_tau: np.ndarray,
                      generator: str, formula: str) -> None:
    """Write 1D u_tau as a function of y/h to TXT (2nd-order pipeline)."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# 1D friction velocity on {label} wall (2nd-order FD pipeline)\n")
        f.write(f"# generated by {generator}\n")
        f.write(f"# Re = {Re}, niu = {niu:.6e}\n")
        f.write(f"# Span points averaged before FD: Nx = {Nx_orig}\n")
        f.write("#\n")
        f.write("# Mathematical definition (lattice Boltzmann convention):\n")
        f.write("#   <u_t>_x(j) = (1/Nx) * sum_i u_t(i,j)   "
                "[span-average BEFORE FD]\n")
        f.write("#   tau_1d(j) = niu * d<u_t>_x/dn|_wall     "
                "[2nd-order FD, lattice shear stress]\n")
        f.write(f"#   {formula}\n")
        f.write("#\n")
        f.write(f"# Ny = {Ny} (streamwise points)\n")
        f.write(f"# {'j':>4s}  {'y/h':>18s}  {'z_wall':>18s}  "
                f"{'u_tau':>18s}\n")
        for j in range(Ny):
            f.write(f"  {j:4d}  {y_wall[j]:18.12e}  {z_wall[j]:18.12e}  "
                    f"{u_tau[j]:18.12e}\n")


def write_tauwall_1d_dat(path, label, x_dummy, y_wall, z_wall,
                         du_t_dxi, du_t_dzeta, h_xi, J, eXZ,
                         du_t_dn, tau_signed, tau_abs,
                         convention_label, source_path, Nx_orig):
    Ny = du_t_dn.shape[0]
    chunks = []
    chunks.append(f"# 2nd-order FD span-avg tau_wall on {label} wall, 1D in j\n")
    chunks.append("# generated by 12.phase3_compute_zplus_2nd1A2D.py\n")
    chunks.append(f"# source utan  : {os.path.basename(source_path)}\n")
    chunks.append(f"# averaging    : u_t span-averaged over {Nx_orig} i-points "
                  "BEFORE FD\n")
    chunks.append(f"# FD orders    : du_t/dzeta = 2nd-order one-sided "
                  "(3-point stencil)\n")
    chunks.append(f"#               du_t/dxi   = 2nd-order central + periodic\n")
    chunks.append(f"#               wall metric (h_xi, J, e_xi.e_zeta) = "
                  "6th-order from step 4\n")
    chunks.append(f"# convention   : {convention_label}\n")
    chunks.append(f'TITLE     = "tau_wall on {label} (2nd-order FD pipeline)"\n')
    chunks.append('VARIABLES = "j" "y" "z" "du_t_dxi" "du_t_dzeta" "h_xi" "J" '
                  '"e_xi.e_zeta" "du_t_dn" "tau_signed" "tau_abs"\n')
    chunks.append(f'ZONE T="{label}_tau_2nd", I={Ny}, F=POINT\n')
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
    Ny = z_plus.shape[0]
    chunks = []
    chunks.append(f"# 1D z+ on {label} wall ({mode}), 2nd-order FD pipeline\n")
    chunks.append("# generated by 12.phase3_compute_zplus_2nd1A2D.py\n")
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
                    "and z+ via 2nd-order FD chain rule (with 6th-order "
                    "wall metric reused from step 4).")
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
    Re = int(round(Uref / niu))
    multiplier = niu              # step 4 already rescaled to lattice units
    convention_label = TAU_CONVENTION_LABEL
    print(f"\nniu (= mu) = {niu:.6e}, Uref = {Uref:.6e}, Re = {Re}")
    print(f"convention : {convention_label}")
    print(f"multiplier = niu = {multiplier:.6e}")
    print(f"FD orders: du_t/dzeta = 2nd-order one-sided")
    print(f"           du_t/dxi   = 2nd-order central + periodic")
    print(f"           wall metric (h_xi, J, e_xi.e_zeta) = 6th-order from step 4")

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

    # ---- [3] FD2 at wall: du_t/dzeta + du_t/dxi ----
    print("\n[3] FD at wall (2nd-order, 1D pipeline) ...")
    # bottom: forward 2nd-order using k=0,1,2 (only first 3 layers!)
    dut_dzeta_bot = FD2_FWD @ u_t_bot_avg[0:3]            # (Ny,)
    # top: backward 2nd-order using k=K-3,K-2,K-1 (last 3 layers)
    dut_dzeta_top = FD2_BWD @ u_t_top_avg[-3:]            # (Ny,)
    # du_t/dxi at wall row: 2nd-order central with periodic wrap
    u_t_bot_wall = u_t_bot_avg[0]                          # (Ny,)
    u_t_top_wall = u_t_top_avg[-1]
    dut_dxi_bot = d_dj_periodic_row_2nd(u_t_bot_wall, period_offset=0.0)
    dut_dxi_top = d_dj_periodic_row_2nd(u_t_top_wall, period_offset=0.0)
    print(f"  bot du_t/dzeta range [{dut_dzeta_bot.min():+.4e}, "
          f"{dut_dzeta_bot.max():+.4e}]")
    print(f"  top du_t/dzeta range [{dut_dzeta_top.min():+.4e}, "
          f"{dut_dzeta_top.max():+.4e}]")
    print(f"  bot du_t/dxi   range [{dut_dxi_bot.min():+.4e}, "
          f"{dut_dxi_bot.max():+.4e}]")

    # ---- [4] du_t/dn = (h_xi/J) du/dzeta - (e/(h_xi J)) du/dxi (chain rule) ----
    print("\n[4] chain rule du_t/dn (6th-order metric) ...")
    A_bot = m_bot["h_xi"] / m_bot["J"]
    B_bot = m_bot["eXZ"] / (m_bot["h_xi"] * m_bot["J"])
    A_top = m_top["h_xi"] / m_top["J"]
    B_top = m_top["eXZ"] / (m_top["h_xi"] * m_top["J"])
    dut_dn_bot = A_bot * dut_dzeta_bot - B_bot * dut_dxi_bot
    dut_dn_top = A_top * dut_dzeta_top - B_top * dut_dxi_top
    print(f"  bot du_t/dn range [{dut_dn_bot.min():+.4e}, {dut_dn_bot.max():+.4e}]")
    print(f"  top du_t/dn range [{dut_dn_top.min():+.4e}, {dut_dn_top.max():+.4e}]")
    # cross-coupling magnitude (should be small for nearly orthogonal mesh)
    main_bot  = np.max(np.abs(A_bot * dut_dzeta_bot))
    cross_bot = np.max(np.abs(B_bot * dut_dxi_bot))
    print(f"  bot cross-coupling / main = {cross_bot/main_bot*100:.4f}%   "
          "(small if mesh near orthogonal)")

    # ---- [5] tau = niu * du_t/dn  (lattice stress, rho = 1) ----
    print(f"\n[5] tau_wall = niu * du_t/dn = {multiplier:.4e} * du_t/dn "
          f"({convention_label})")
    tau_bot_signed = multiplier * dut_dn_bot
    tau_top_signed = multiplier * dut_dn_top
    tau_bot_abs    = np.abs(tau_bot_signed)
    tau_top_abs    = np.abs(tau_top_signed)
    print(f"  bot tau_signed range [{tau_bot_signed.min():+.4e}, "
          f"{tau_bot_signed.max():+.4e}]")
    print(f"  top tau_signed range [{tau_top_signed.min():+.4e}, "
          f"{tau_top_signed.max():+.4e}]")

    out25 = os.path.join(OUTPUT_DIR,
                         f"27.Re{Re}_j{Ny}_bottomtauwall_spanavg_2nd.dat")
    out26 = os.path.join(OUTPUT_DIR,
                         f"28.Re{Re}_j{Ny}_toptauwall_spanavg_2nd.dat")
    write_tauwall_1d_dat(out25, "bottom", x_arr,
                         y_lyr_bot[0], z_lyr_bot[0],
                         dut_dxi_bot, dut_dzeta_bot,
                         m_bot["h_xi"], m_bot["J"], m_bot["eXZ"],
                         dut_dn_bot, tau_bot_signed, tau_bot_abs,
                         convention_label, bot_path, Nx)
    write_tauwall_1d_dat(out26, "top",    x_arr,
                         y_lyr_top[-1], z_lyr_top[-1],
                         dut_dxi_top, dut_dzeta_top,
                         m_top["h_xi"], m_top["J"], m_top["eXZ"],
                         dut_dn_top, tau_top_signed, tau_top_abs,
                         convention_label, top_path, Nx)
    print(f"  wrote {os.path.basename(out25)} ({os.path.getsize(out25):,} B)")
    print(f"  wrote {os.path.basename(out26)} ({os.path.getsize(out26):,} B)")

    # ---- [6] z+ via simple |delta_z| ----
    print("\n[6] z+ (1D) using simple |delta_z| ...")
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

    out27 = os.path.join(OUTPUT_DIR,
                         f"29.Re{Re}_j{Ny}_zplus_bottom_spanavg_2nd.dat")
    out28 = os.path.join(OUTPUT_DIR,
                         f"30.Re{Re}_j{Ny}_zplus_top_spanavg_2nd.dat")
    write_zplus_1d_dat(out27, "bottom", "simple",
                       y_2d[0, :], z_2d[0, :],
                       tau_bot_abs, u_tau_bot_1d,
                       delta_z_simple_bot, z_plus_bot_simple,
                       source_tau=out25, Re=Re)
    write_zplus_1d_dat(out28, "top", "simple",
                       y_2d[Nz_mesh - 1, :], z_2d[Nz_mesh - 1, :],
                       tau_top_abs, u_tau_top_1d,
                       delta_z_simple_top, z_plus_top_simple,
                       source_tau=out26, Re=Re)
    print(f"  bot z+_simple range [{z_plus_bot_simple.min():.4f}, "
          f"{z_plus_bot_simple.max():.4f}]")
    print(f"  top z+_simple range [{z_plus_top_simple.min():.4f}, "
          f"{z_plus_top_simple.max():.4f}]")

    # ---- [7] z+ via n̂ projection (bottom only) ----
    print("\n[7] z+ (1D) on bottom using n_hat projection ...")
    n_y, n_z, _ = compute_wall_normal(y_2d, z_2d, k_wall=0,
                                       L_stream=L_stream)
    d_n_proj_bot = np.abs(dy_bot_step * n_y + dz_bot_step * n_z)
    z_plus_bot_normal = u_tau_bot_1d * d_n_proj_bot / niu
    print(f"  d_n_proj range [{d_n_proj_bot.min():.4e}, {d_n_proj_bot.max():.4e}]")
    print(f"  z+_normal range [{z_plus_bot_normal.min():.4f}, "
          f"{z_plus_bot_normal.max():.4f}]")

    out29 = os.path.join(OUTPUT_DIR,
                         f"31.Re{Re}_j{Ny}_zplus_bottom_normal_spanavg_2nd.dat")
    write_zplus_1d_dat(out29, "bottom", "n-projection",
                       y_2d[0, :], z_2d[0, :],
                       tau_bot_abs, u_tau_bot_1d,
                       d_n_proj_bot, z_plus_bot_normal,
                       extra_cols={"n_y": n_y, "n_z": n_z,
                                   "dy_step": dy_bot_step,
                                   "dz_step": dz_bot_step},
                       source_tau=out25, Re=Re)

    # ---- [8] Comparison vs 6th-order pipeline (step 10) ----
    print("\n[8] comparison vs step 10 (6th-order) and span-avg of step 5 (2D)")
    try:
        d19 = np.loadtxt(os.path.join(OUTPUT_DIR,
                                       f"21.Re{Re}_j{Ny}_bottomtauwall_spanavg.dat"),
                         skiprows=9)
        tau_6th = d19[:, 9]
        diff_2nd_vs_6th = np.abs(tau_bot_signed - tau_6th).max()
        rel = diff_2nd_vs_6th / np.abs(tau_6th).max()
        print(f"  bot tau_2nd vs tau_6th: max abs diff = {diff_2nd_vs_6th:.4e}, "
              f"rel = {rel:.3%}")
        print(f"  (expected: small since both pipelines apply chain rule;\n"
              f"   only difference is FD order on smooth-ish u_t_avg)")
    except FileNotFoundError:
        print("  (skipped — 19.dat not present)")

    # ---- [9] u_tau 1D output ----
    print("\n[9] u_tau 1D output (2nd-order pipeline) ...")
    gen = "12.phase3_compute_zplus_2nd1A2D.py"
    formula = "u_tau_1d(j) = sqrt(|tau_1d(j)|)   [tau from 2nd-order FD]"

    utau_bot_path = os.path.join(OUTPUT_DIR,
                                  f"41.Re{Re}_utau_1d_bottom_2nd1A2D.txt")
    write_utau_1d_txt(utau_bot_path, "bottom", Re, niu, Nx, Ny,
                      y_lyr_bot[0], z_lyr_bot[0],
                      u_tau_bot_1d, gen, formula)
    print(f"  writing -> {utau_bot_path}")

    utau_top_path = os.path.join(OUTPUT_DIR,
                                  f"42.Re{Re}_utau_1d_top_2nd1A2D.txt")
    write_utau_1d_txt(utau_top_path, "top", Re, niu, Nx, Ny,
                      y_lyr_top[-1], z_lyr_top[-1],
                      u_tau_top_1d, gen, formula)
    print(f"  writing -> {utau_top_path}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
