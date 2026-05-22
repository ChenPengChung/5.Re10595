# -*- coding: utf-8 -*-
"""
8.phase2_compute_zplus_1D2A.py
==============================

Compute viscous wall-unit grid spacings (z+) globally and pointwise on
the bottom and top walls.

Project convention: z is the WALL-NORMAL direction (y is stream, x is
span), so the wall-distance viscous unit is naturally called z+.

Outputs (4 files)
-----------------
    14.Re<X>_zplus_summary.txt        global summary
    15.Re<X>_zplus_bottom.dat         bottom z+ using simple |delta_z|
    16.Re<X>_zplus_top.dat            top    z+ using simple |delta_z|
    17.Re<X>_zplus_bottom_normal.dat  bottom z+ using n_hat projection
                                      (additional output for comparison)

Why 13 vs 15 (two bottom-wall outputs)?
---------------------------------------
The bottom wall is a CURVED hill, so the first-cell vector
    (delta_y, delta_z) = (y(j,1) - y(j,0), z(j,1) - z(j,0))
is NOT exactly normal to the wall surface.  Two reasonable distances:

    15.dat:  delta_z_simple(j) = |z(j,1) - z(j,0)|     (z component only)
    17.dat:  d_n_proj(j)       = delta_y * n_y + delta_z * n_z
                                 (projection onto outward unit wall normal)

For the Froehlich grid (cos(theta_xi_zeta) < 0.06 at bottom wall) the
two differ by at most a few % at the steepest hill sections.  17.dat is
the geometrically rigorous wall-normal distance for a non-orthogonal
mesh; 15.dat is the simple z-component.

The TOP wall is FLAT (z = const), so n_hat ~= +z_hat and the simple
|delta_z| IS the wall-normal distance -- only one output (16.dat).

Definitions (textbook lattice form, matches step 5/6)
-----------------------------------------------------
    niu = Uref / Re            kinematic viscosity (lattice)
    tau_local(i, j)   = |tau_wall_abs(i, j)|       lattice stress
    u_tau_local(i, j) = sqrt(tau_local / rho)      lattice friction velocity
    z+ formula        : delta_*_plus = u_tau * delta_* / niu
                       (= textbook y+ = u_tau * y / nu)

Wall-distance choices:
    Bottom 15:  delta_z_simple(j) = |z(j, 1)   - z(j, 0)|
    Top    16:  delta_z_simple(j) = |z(j, Nz-2) - z(j, Nz-1)|
    Bottom 17:  d_n_proj(j)       = (y(j,1) - y(j,0)) * n_y(j)
                                   + (z(j,1) - z(j,0)) * n_z(j)
                where n_hat(j) = (-z_xi(j,0), y_xi(j,0)) / h_xi(j,0)
                      computed via 6th-order central FD with periodic
                      wrap on j (matches step 4's metric_at_wall).

Final z+:
    z_plus_local(i, j) = u_tau_local(i, j) * d_n(j) / niu

Global (14.txt, using u_tau_global and 11.txt extrema):
    delta_x+         = u_tau_global * delta_x_avg / niu
    delta_y+_max/min = u_tau_global * delta_y_max/min / niu
    delta_z+_max/min = u_tau_global * delta_z_max/min / niu

Inputs (auto-detected, must be unique)
--------------------------------------
    2.<stem>.dat                         full 2D mesh (y, z)
    7.Re<X>_*_bottomtauwall.dat          bottom wall tau
    8.Re<X>_*_toptauwall.dat             top    wall tau
    9.Re<X>_tauwall_global.dat           u_tau_global
    11.<stem>_delta_extrema.txt          mesh extrema (delta_y/z min/max)
    Input/variables.h                    niu, Uref
"""

from __future__ import annotations
import argparse
import os
import re
import sys
import time
from typing import Dict, Tuple

import numpy as np

# ---- I/O directory bootstrap (matches the Input/Output/Reference layout) -
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "Reference"))
OUTPUT_DIR = os.path.join(_HERE, "Output")
os.makedirs(OUTPUT_DIR, exist_ok=True)
# --------------------------------------------------------------------------

from phase1_common import (
    parse_tecplot_2d_mesh,
    load_tauwall_dat,
    find_unique_matching,
    d_dj_periodic_row,
    auto_detect_variables_h,
    parse_header_constants,
    find_const,
    verify_lattice_tau_dat,
)

INPUT_DIR = os.path.join(_HERE, "Input")


# ============================================================================
#  Auto-detection of all 5 inputs
# ============================================================================
_MESH_RE    = re.compile(r"^2\.j.+\.dat$",                  re.IGNORECASE)
_BOT_RE     = re.compile(r"^7\..+_bottomtauwall\.dat$",     re.IGNORECASE)
_TOP_RE     = re.compile(r"^8\..+_toptauwall\.dat$",        re.IGNORECASE)
_GLOBAL_RE  = re.compile(r"^9\..+_tauwall_global\.dat$",    re.IGNORECASE)
_EXTREMA_RE = re.compile(r"^11\..+_delta_extrema\.txt$",    re.IGNORECASE)


def auto_detect_mesh(folder: str = ".") -> str:
    return find_unique_matching(folder, "2.*.dat",  _MESH_RE)


def auto_detect_bot(folder: str = ".") -> str:
    return find_unique_matching(folder, "7.*.dat",  _BOT_RE)


def auto_detect_top(folder: str = ".") -> str:
    return find_unique_matching(folder, "8.*.dat",  _TOP_RE)


def auto_detect_global(folder: str = ".") -> str:
    return find_unique_matching(folder, "9.*.dat",  _GLOBAL_RE)


def auto_detect_extrema(folder: str = ".") -> str:
    return find_unique_matching(folder, "11.*.txt", _EXTREMA_RE)


# ============================================================================
#  Side-channel parsers (11.txt and 9.dat)
# ============================================================================
def read_extrema_txt(path: str) -> Dict[str, dict]:
    """Parse 11.*.txt 4-row table -> dict with extrema values and (y,z) locations.

    Each entry: {'value': float, 'j': int, 'k': int, 'y': float, 'z': float}
    """
    with open(path) as f:
        text = f.read()
    out: Dict[str, dict] = {}
    for m in re.finditer(
            r"^(delta_[yz]_(?:min|max))\s+([\d.eE+-]+)\s+(\d+)\s+(\d+)"
            r"\s+([+-]?[\d.]+)\s+([+-]?[\d.]+)",
            text, re.MULTILINE):
        out[m.group(1)] = {
            'value': float(m.group(2)),
            'j': int(m.group(3)),
            'k': int(m.group(4)),
            'y': float(m.group(5)),
            'z': float(m.group(6)),
        }
    expected = {"delta_y_min", "delta_y_max", "delta_z_min", "delta_z_max"}
    if set(out.keys()) != expected:
        raise ValueError(
            f"expected extrema {expected} in {path}, got {set(out.keys())}")
    return out


def read_utau_global(path: str) -> float:
    """Parse u_tau_global from 9.*.dat key=value summary."""
    with open(path) as f:
        s = f.read()
    m = re.search(r"^u_tau_global\s*=\s*([\d.eE+-]+)", s, re.MULTILINE)
    if not m:
        raise ValueError(f"u_tau_global not found in {path}")
    return float(m.group(1))


def parse_re_int(name: str) -> int:
    """Extract integer Reynolds number from a filename token like Re<num>."""
    m = re.search(r"Re(\d+)", name)
    if not m:
        raise ValueError(f"cannot find Re<num> in: {name}")
    return int(m.group(1))


# ============================================================================
#  Wall-normal direction recomputed from 2D mesh
# ============================================================================
def compute_wall_normal(y_2d: np.ndarray, z_2d: np.ndarray,
                        k_wall: int, L_stream: float
                        ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Outward unit wall normal at the wall row k_wall.

        n_hat = ( -z_xi(j, k_wall),  y_xi(j, k_wall) ) / h_xi(j, k_wall)

    Uses 6th-order central FD with periodic wrap on j (offset L_stream
    on y, none on z).  Matches step 4's metric_at_wall.

    Returns (n_y, n_z, h_xi)  each shape (Ny,).
    """
    y_row = y_2d[k_wall]
    z_row = z_2d[k_wall]
    y_xi  = d_dj_periodic_row(y_row, period_offset=L_stream)
    z_xi  = d_dj_periodic_row(z_row, period_offset=0.0)
    h_xi  = np.sqrt(y_xi ** 2 + z_xi ** 2)
    n_y   = -z_xi / h_xi
    n_z   =  y_xi / h_xi
    return n_y, n_z, h_xi


# ============================================================================
#  Per-row delta computation (for center/bottom/top sections)
# ============================================================================
def compute_delta_at_k(y_2d: np.ndarray, z_2d: np.ndarray,
                       k_row: int, L_stream: float
                       ) -> Tuple[np.ndarray, np.ndarray]:
    """Compute delta_y(j) and delta_z(j) at a specific k-row.

    Same formulas as 7.phase2_grid_delta.py:
      delta_y(j) = ( |y(j+1,k)-y(j,k)| + |y(j,k)-y(j-1,k)| ) / 2
      delta_z(j) = ( |z(j,k+1)-z(j,k)| + |z(j,k)-z(j,k-1)| ) / 2

    Boundary: j-periodic with L_stream offset; k linear extrapolation.
    Returns (delta_y, delta_z) each shape (Ny,).
    """
    Nz, Ny = y_2d.shape

    # delta_y: periodic wrap in j
    y_row = y_2d[k_row, :]
    y_pad = np.empty(Ny + 2)
    y_pad[1:Ny + 1] = y_row
    y_pad[0]         = y_row[Ny - 2] - L_stream
    y_pad[Ny + 1]    = y_row[1]      + L_stream
    delta_y = (np.abs(y_pad[2:] - y_pad[1:-1])
               + np.abs(y_pad[1:-1] - y_pad[:-2])) / 2.0

    # delta_z: boundary handling in k
    z_row = z_2d[k_row, :]
    if k_row == 0:
        z_above = z_2d[1, :]
        z_below = 2.0 * z_row - z_above
    elif k_row == Nz - 1:
        z_below = z_2d[Nz - 2, :]
        z_above = 2.0 * z_row - z_below
    else:
        z_above = z_2d[k_row + 1, :]
        z_below = z_2d[k_row - 1, :]
    delta_z = (np.abs(z_above - z_row) + np.abs(z_row - z_below)) / 2.0

    return delta_y, delta_z


def write_utau_spanavg_txt(path: str, label: str, Re: int, niu: float,
                           Nx: int, Ny: int,
                           y_wall: np.ndarray, z_wall: np.ndarray,
                           u_tau_avg: np.ndarray,
                           u_tau_std: np.ndarray,
                           u_tau_min: np.ndarray,
                           u_tau_max: np.ndarray,
                           generator: str, formula: str) -> None:
    """Write u_tau span-averaged as a function of y/h to TXT."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Span-averaged local friction velocity on {label} wall\n")
        f.write(f"# generated by {generator}\n")
        f.write(f"# Re = {Re}, niu = {niu:.6e}, Nx = {Nx} (span points averaged)\n")
        f.write("#\n")
        f.write("# Mathematical definition (lattice Boltzmann convention):\n")
        f.write("#   tau_wall(i,j) = niu * du_t/dn|_wall        "
                "[lattice wall shear stress]\n")
        f.write("#   u_tau_local(i,j) = sqrt(|tau_wall(i,j)|)   "
                "[rho = 1 in lattice units]\n")
        f.write(f"#   {formula}\n")
        f.write("#\n")
        f.write(f"# Ny = {Ny} (streamwise points)\n")
        f.write(f"# {'j':>4s}  {'y/h':>18s}  {'z_wall':>18s}  "
                f"{'u_tau_avg':>18s}  {'u_tau_std':>18s}  "
                f"{'u_tau_min':>18s}  {'u_tau_max':>18s}\n")
        for j in range(Ny):
            f.write(f"  {j:4d}  {y_wall[j]:18.12e}  {z_wall[j]:18.12e}  "
                    f"{u_tau_avg[j]:18.12e}  {u_tau_std[j]:18.12e}  "
                    f"{u_tau_min[j]:18.12e}  {u_tau_max[j]:18.12e}\n")


# ============================================================================
#  Output writers
# ============================================================================
def write_global_summary(path: str,
                         Re: int, u_tau_global: float, niu: float,
                         delta_x: float, dx_plus: float,
                         Nx: int, Ny: int, Nz: int,
                         full_grid_extrema: Dict[str, dict],
                         center: dict,
                         bottom: dict,
                         top_wall: dict) -> None:
    """14.txt -- comprehensive wall-unit summary with three k-row sections."""
    k_center = center['k']

    def _loc_yz(e):
        return (f"    at (j={e['j']}, k={e['k']})"
                f"  y={e['y']:+.7f}  z={e['z']:+.7f}")

    def _loc_xyz(e):
        return (f"    at (i={e['i']}, j={e['j']}, k={e['k']})"
                f"  x={e['x']:+.7f}  y={e['y']:+.7f}  z={e['z']:+.7f}")

    _D = ["delta_y_max", "delta_y_min", "delta_z_max", "delta_z_min"]
    _DP = ["delta_y_plus_max", "delta_y_plus_min",
           "delta_z_plus_max", "delta_z_plus_min"]

    with open(path, "w") as f:
        f.write("# Global wall-unit summary\n")
        f.write("# generated by 8.phase2_compute_zplus_1D2A.py\n")
        f.write("# Lattice convention:  niu = Uref/Re"
                " (kinematic viscosity)\n")
        f.write("# tau_wall = niu * du_t/dn,"
                "  u_tau = sqrt(tau/rho)\n")
        f.write("# Formula:  delta_*_plus = u_tau * delta_* / niu\n")
        f.write("\n")
        f.write(f"Re                  = {Re}\n")
        f.write(f"u_tau_global        = {u_tau_global:.12e}"
                f"    # lattice friction velocity\n")
        f.write(f"niu                 = {niu:.12e}"
                f"    # = Uref/Re, lattice nu\n")
        f.write(f"Nx                  = {Nx}"
                f"      # span (uniform)\n")
        f.write(f"Ny                  = {Ny}"
                f"      # stream\n")
        f.write(f"Nz                  = {Nz}"
                f"      # wall-normal\n")
        f.write(f"k_center            = {k_center}"
                f"       # = (Nz-1)/2\n")
        f.write("\n")
        f.write("# Uniform span spacing\n")
        f.write(f"delta_x_avg         = {delta_x:.12e}"
                f"    # uniform span = LX/(Nx-1)\n")
        f.write(f"delta_x_plus        = {dx_plus:.6f}"
                f"              # u_tau_global * delta_x / niu\n")

        # ---- Full-grid extrema (reference) ----
        f.write("\n")
        f.write("# " + "=" * 64 + "\n")
        f.write("#  Full-grid extrema (over all j, k) -- reference\n")
        f.write("# " + "=" * 64 + "\n")
        for key in _D:
            e = full_grid_extrema[key]
            f.write(f"{key:20s}= {e['value']:.12e}{_loc_yz(e)}\n")

        # ---- Center Node ----
        f.write("\n")
        f.write("# " + "=" * 64 + "\n")
        f.write(f"#  Center Node (k = {k_center} = (NZ-1)/2)\n")
        f.write("# " + "=" * 64 + "\n")
        f.write("# u_tau = u_tau_global (constant,"
                " center not on wall surface)\n")
        f.write(f"# Mesh spacings at k={k_center}\n")
        for key in _D:
            e = center[key]
            f.write(f"{key:20s}= {e['value']:.12e}{_loc_yz(e)}\n")
        f.write("# Wall-unit spacings (u_tau_global constant"
                " -> same location as mesh spacing)\n")
        for key in _DP:
            e = center[key]
            f.write(f"{key:20s}= {e['value']:.6f}{_loc_yz(e)}\n")

        # ---- Wall section writer ----
        def _write_wall_section(label, k_label, sec):
            f.write("\n")
            f.write("# " + "=" * 64 + "\n")
            f.write(f"#  {label} ({k_label})\n")
            f.write("# " + "=" * 64 + "\n")
            f.write("# u_tau = u_tau_local(i,j)"
                    " (varies along wall surface)\n")
            e = sec['u_tau_max']
            f.write(f"{'u_tau_local_max':20s}"
                    f"= {e['value']:.12e}{_loc_xyz(e)}\n")
            e = sec['u_tau_min']
            f.write(f"{'u_tau_local_min':20s}"
                    f"= {e['value']:.12e}{_loc_xyz(e)}\n")
            f.write(f"# Mesh spacings at {k_label}\n")
            for key in _D:
                e = sec[key]
                f.write(f"{key:20s}= {e['value']:.12e}"
                        f"{_loc_yz(e)}\n")
            f.write("# Wall-unit spacings"
                    " (u_tau_local -> 3D extremum search)\n")
            for key in _DP:
                e = sec[key]
                f.write(f"{key:20s}= {e['value']:.6f}"
                        f"{_loc_xyz(e)}\n")

        _write_wall_section("Bottom Wall",
                            "k=0, zeta=0_bottom", bottom)
        _write_wall_section("Top Wall",
                            f"k={Nz - 1}, zeta=0_top", top_wall)


def _broadcast(values_1d: np.ndarray, Ny: int, Nx: int,
               axis: str = "j") -> np.ndarray:
    """Broadcast 1D array of length Ny (axis='j') or Nx (axis='i') to (Ny,Nx)."""
    if axis == "j":
        return np.broadcast_to(values_1d[:, None], (Ny, Nx))
    elif axis == "i":
        return np.broadcast_to(values_1d[None, :], (Ny, Nx))
    raise ValueError(f"axis must be 'i' or 'j', got {axis!r}")


def write_zplus_simple_dat(path: str, label: str, source: str,
                           Re: int,
                           x_arr: np.ndarray,
                           y_wall: np.ndarray, z_wall: np.ndarray,
                           tau_local: np.ndarray,
                           u_tau_local: np.ndarray,
                           delta_z_simple: np.ndarray,
                           z_plus: np.ndarray) -> None:
    """15.dat / 16.dat: local z+ using simple |delta_z|."""
    Ny, Nx = z_plus.shape
    js, is_ = np.indices((Ny, Nx))
    x_g  = _broadcast(x_arr,         Ny, Nx, axis="i")
    y_g  = _broadcast(y_wall,        Ny, Nx, axis="j")
    z_g  = _broadcast(z_wall,        Ny, Nx, axis="j")
    dz_g = _broadcast(delta_z_simple, Ny, Nx, axis="j")
    data = np.column_stack([
        is_.ravel(), js.ravel(),
        x_g.ravel(), y_g.ravel(), z_g.ravel(),
        tau_local.ravel(), u_tau_local.ravel(),
        dz_g.ravel(), z_plus.ravel(),
    ])
    with open(path, "w") as f:
        f.write(f"# Local z+ on {label} wall using simple |delta_z|\n")
        f.write("# generated by 8.phase2_compute_zplus_1D2A.py\n")
        f.write(f"# source tau : {os.path.basename(source)}\n")
        f.write(f"# Re         = {Re}\n")
        f.write("# Procedure (textbook lattice form, niu = Uref/Re):\n")
        f.write("#   tau_local(i,j)        = |tau_wall_abs(i,j)|  (lattice stress)\n")
        f.write("#   u_tau_local(i,j)      = sqrt(tau_local / rho)\n")
        if label == "bottom":
            f.write("#   delta_z_simple(j)     = |z(j,1) - z(j,0)|\n")
        else:
            f.write("#   delta_z_simple(j)     = |z(j,Nz-2) - z(j,Nz-1)|\n")
        f.write("#   z_plus(i,j)           = u_tau_local * delta_z_simple / niu\n")
        f.write(f'TITLE     = "Local z+ on {label} wall (simple delta_z)"\n')
        f.write('VARIABLES = "i" "j" "x" "y_wall" "z_wall" "tau_local" '
                '"u_tau_local" "delta_z_simple" "z_plus"\n')
        f.write(f'ZONE T="{label}_zplus_simple", I={Nx}, J={Ny}, F=POINT\n')
        f.write('DT=(SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE)\n')
        np.savetxt(f, data, fmt="%4d %4d %.15e %.15e %.15e %.15e %.15e %.15e %.15e")


def write_zplus_normal_dat(path: str, source: str, Re: int,
                           x_arr: np.ndarray,
                           y_wall: np.ndarray, z_wall: np.ndarray,
                           tau_local: np.ndarray,
                           u_tau_local: np.ndarray,
                           dy_step: np.ndarray, dz_step: np.ndarray,
                           n_y: np.ndarray, n_z: np.ndarray,
                           d_n_proj: np.ndarray,
                           z_plus_proj: np.ndarray) -> None:
    """17.dat: bottom wall z+ using n_hat projection."""
    Ny, Nx = z_plus_proj.shape
    js, is_ = np.indices((Ny, Nx))
    x_g  = _broadcast(x_arr,    Ny, Nx, axis="i")
    y_g  = _broadcast(y_wall,   Ny, Nx, axis="j")
    z_g  = _broadcast(z_wall,   Ny, Nx, axis="j")
    dy_g = _broadcast(dy_step,  Ny, Nx, axis="j")
    dz_g = _broadcast(dz_step,  Ny, Nx, axis="j")
    ny_g = _broadcast(n_y,      Ny, Nx, axis="j")
    nz_g = _broadcast(n_z,      Ny, Nx, axis="j")
    dn_g = _broadcast(d_n_proj, Ny, Nx, axis="j")
    data = np.column_stack([
        is_.ravel(), js.ravel(),
        x_g.ravel(), y_g.ravel(), z_g.ravel(),
        tau_local.ravel(), u_tau_local.ravel(),
        dy_g.ravel(), dz_g.ravel(),
        ny_g.ravel(), nz_g.ravel(),
        dn_g.ravel(), z_plus_proj.ravel(),
    ])
    with open(path, "w") as f:
        f.write("# Local z+ on bottom wall using n_hat projection\n")
        f.write("# generated by 8.phase2_compute_zplus_1D2A.py\n")
        f.write(f"# source tau : {os.path.basename(source)}\n")
        f.write(f"# Re         = {Re}\n")
        f.write("# Procedure (rigorous wall-normal distance for "
                "non-orthogonal mesh, lattice convention):\n")
        f.write("#   tau_local(i,j)   = |tau_wall_abs(i,j)|       (lattice stress)\n")
        f.write("#   u_tau_local(i,j) = sqrt(tau_local / rho)     (lattice u_tau)\n")
        f.write("#   n_hat(j) = (n_y, n_z) = (-z_xi(j,0), y_xi(j,0)) / h_xi(j,0)\n")
        f.write("#                          (outward unit wall normal at k=0)\n")
        f.write("#   delta_y(j) = y(j,1) - y(j,0);   delta_z(j) = z(j,1) - z(j,0)\n")
        f.write("#   d_n_proj(j) = delta_y * n_y + delta_z * n_z\n")
        f.write("#                  (signed projection onto outward wall normal;\n")
        f.write("#                   |.| applied for safety)\n")
        f.write("#   z_plus_proj(i,j) = u_tau_local * d_n_proj / niu\n")
        f.write('TITLE     = "Local z+ on bottom wall (n_hat projection)"\n')
        f.write('VARIABLES = "i" "j" "x" "y_wall" "z_wall" "tau_local" '
                '"u_tau_local" "delta_y_step" "delta_z_step" '
                '"n_y" "n_z" "d_n_proj" "z_plus_proj"\n')
        f.write(f'ZONE T="bottom_zplus_normal", I={Nx}, J={Ny}, F=POINT\n')
        f.write('DT=(SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE '
                'SINGLE SINGLE SINGLE SINGLE SINGLE)\n')
        np.savetxt(f, data,
                   fmt="%4d %4d %.15e %.15e %.15e %.15e %.15e %.15e %.15e "
                       "%.15e %.15e %.15e %.15e")


# ============================================================================
#  Main
# ============================================================================
def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Compute viscous wall-unit grid spacings z+ "
                    "(global + pointwise on both walls) with both simple "
                    "delta_z and n_hat-projected distance for the curved "
                    "bottom wall.")
    p.add_argument("--mesh",    default=None, help="2.*.dat (auto-detect)")
    p.add_argument("--bot",     default=None, help="7.*.dat (auto-detect)")
    p.add_argument("--top",     default=None, help="8.*.dat (auto-detect)")
    p.add_argument("--global",  dest="global_path", default=None,
                   help="9.*.dat (auto-detect)")
    p.add_argument("--extrema", default=None, help="11.*.txt (auto-detect)")
    args = p.parse_args(argv)

    folder = OUTPUT_DIR
    mesh_path    = args.mesh        or auto_detect_mesh(folder)
    bot_path     = args.bot         or auto_detect_bot(folder)
    top_path     = args.top         or auto_detect_top(folder)
    global_path  = args.global_path or auto_detect_global(folder)
    extrema_path = args.extrema     or auto_detect_extrema(folder)

    print(f"input mesh    : {mesh_path}")
    print(f"input bot tau : {bot_path}")
    print(f"input top tau : {top_path}")
    print(f"input global  : {global_path}")
    print(f"input extrema : {extrema_path}")
    verify_lattice_tau_dat(bot_path, "bottom tau input")
    verify_lattice_tau_dat(top_path, "top tau input")
    verify_lattice_tau_dat(global_path, "global tau input")

    # ---- niu from variables.h (no fallback to 1/Re; we want the
    #      same number the solver used) ----
    var_h = auto_detect_variables_h(INPUT_DIR)
    if var_h is None:
        raise FileNotFoundError(
            f"Input/variables.h not found in {INPUT_DIR}; needed for niu")
    consts = parse_header_constants(var_h)
    niu = float(find_const(consts, ["niu", "nu"], var_h))

    # ---- [1] read all inputs ----
    print("\n[1] reading inputs ...")
    t0 = time.time()
    y_2d, z_2d, J, K = parse_tecplot_2d_mesh(mesh_path)
    Ny, Nz = J, K
    L_stream = float(y_2d[0, -1] - y_2d[0, 0])
    bot = load_tauwall_dat(bot_path)
    top = load_tauwall_dat(top_path)
    Nx = bot["Nx"]
    if (bot["Nx"], bot["Ny"]) != (top["Nx"], top["Ny"]):
        print("[error] bot/top grid shape mismatch", file=sys.stderr)
        sys.exit(1)
    extrema      = read_extrema_txt(extrema_path)
    u_tau_global = read_utau_global(global_path)
    Re           = parse_re_int(os.path.basename(global_path))
    print(f"  Re = {Re}, u_tau_global = {u_tau_global:.6e} (lattice), "
          f"niu = {niu:.6e}")
    print(f"  grid (Nx, Ny, Nz) = ({Nx}, {Ny}, {Nz})")
    print(f"  L_stream = {L_stream:.6f}")
    print(f"  ({time.time() - t0:.2f}s)")

    # ---- pre-compute derived quantities for all sections ----
    k_center = (Nz - 1) // 2

    # Bottom wall
    tau_local_bot   = bot["tau_abs"]                      # (Ny, Nx)
    u_tau_local_bot = np.sqrt(tau_local_bot)              # rho = 1
    dy_bot_step = y_2d[1, :] - y_2d[0, :]                # needed by step [5]
    dz_bot_step = z_2d[1, :] - z_2d[0, :]

    # Top wall
    tau_local_top   = top["tau_abs"]
    u_tau_local_top = np.sqrt(tau_local_top)

    # Delta at each k-row
    delta_y_bot, delta_z_bot = compute_delta_at_k(
        y_2d, z_2d, 0, L_stream)
    delta_y_cen, delta_z_cen = compute_delta_at_k(
        y_2d, z_2d, k_center, L_stream)
    delta_y_top, delta_z_top = compute_delta_at_k(
        y_2d, z_2d, Nz - 1, L_stream)

    # Aliases for steps [3]/[4] dat file writers
    delta_z_simple_bot = delta_z_bot
    delta_z_simple_top = delta_z_top

    # ---- [2] compute extrema for center / bottom / top ----
    print("\n[2] computing extrema for center / bottom / top ...")
    factor   = u_tau_global / niu
    dx_array = np.diff(bot["x"])
    delta_x  = float(dx_array.mean())
    dx_plus  = factor * delta_x
    print(f"  delta_x_avg  = {delta_x:.6e}   "
          f"(range [{dx_array.min():.6e}, {dx_array.max():.6e}])")
    print(f"  delta_x_plus = {dx_plus:.4f}")

    # -- helpers --
    def _ext_1d(arr, func, k_row):
        j = int(func(arr))
        return {'value': float(arr[j]), 'j': j, 'k': k_row,
                'y': float(y_2d[k_row, j]),
                'z': float(z_2d[k_row, j])}

    def _ext_2d(field, func, k_row, xa, ya, za):
        idx = func(field)
        j, i = np.unravel_index(idx, field.shape)
        return {'value': float(field[j, i]),
                'i': int(i), 'j': int(j), 'k': k_row,
                'x': float(xa[i]),
                'y': float(ya[j]), 'z': float(za[j])}

    # ---- Center (k = k_center, u_tau_global constant) ----
    center = {'k': k_center}
    for tag, arr in [("delta_y", delta_y_cen),
                     ("delta_z", delta_z_cen)]:
        center[f"{tag}_max"] = _ext_1d(arr, np.argmax, k_center)
        center[f"{tag}_min"] = _ext_1d(arr, np.argmin, k_center)
        p_max = dict(center[f"{tag}_max"])
        p_max['value'] = factor * p_max['value']
        center[f"{tag}_plus_max"] = p_max
        p_min = dict(center[f"{tag}_min"])
        p_min['value'] = factor * p_min['value']
        center[f"{tag}_plus_min"] = p_min

    print(f"\n  [center k={k_center}]")
    for tag in ("delta_y", "delta_z"):
        e = center[f"{tag}_max"]
        print(f"    {tag}_max      = {e['value']:.6e}   "
              f"at (j={e['j']}, k={e['k']})")
        e = center[f"{tag}_plus_max"]
        print(f"    {tag}_plus_max = {e['value']:.6f}    "
              f"at (j={e['j']}, k={e['k']})")

    # ---- Bottom wall (k=0, u_tau_local) ----
    bottom = {}
    xa_b, ya_b, za_b = bot["x"], bot["y"], bot["z"]
    bottom['u_tau_max'] = _ext_2d(u_tau_local_bot, np.argmax,
                                  0, xa_b, ya_b, za_b)
    bottom['u_tau_min'] = _ext_2d(u_tau_local_bot, np.argmin,
                                  0, xa_b, ya_b, za_b)
    for tag, arr in [("delta_y", delta_y_bot),
                     ("delta_z", delta_z_bot)]:
        bottom[f"{tag}_max"] = _ext_1d(arr, np.argmax, 0)
        bottom[f"{tag}_min"] = _ext_1d(arr, np.argmin, 0)
        pf = u_tau_local_bot * arr[:, None] / niu
        bottom[f"{tag}_plus_max"] = _ext_2d(
            pf, np.argmax, 0, xa_b, ya_b, za_b)
        bottom[f"{tag}_plus_min"] = _ext_2d(
            pf, np.argmin, 0, xa_b, ya_b, za_b)

    print(f"\n  [bottom k=0]")
    e = bottom['u_tau_max']
    print(f"    u_tau_local_max = {e['value']:.6e}   "
          f"at (i={e['i']}, j={e['j']})")
    e = bottom['u_tau_min']
    print(f"    u_tau_local_min = {e['value']:.6e}   "
          f"at (i={e['i']}, j={e['j']})")
    for tag in ("delta_y", "delta_z"):
        e = bottom[f"{tag}_max"]
        print(f"    {tag}_max      = {e['value']:.6e}   "
              f"at (j={e['j']}, k=0)")
        e = bottom[f"{tag}_plus_max"]
        print(f"    {tag}_plus_max = {e['value']:.6f}    "
              f"at (i={e['i']}, j={e['j']}, k=0)")

    # ---- Top wall (k=Nz-1, u_tau_local) ----
    top_sec = {}
    xa_t, ya_t, za_t = top["x"], top["y"], top["z"]
    top_sec['u_tau_max'] = _ext_2d(u_tau_local_top, np.argmax,
                                   Nz - 1, xa_t, ya_t, za_t)
    top_sec['u_tau_min'] = _ext_2d(u_tau_local_top, np.argmin,
                                   Nz - 1, xa_t, ya_t, za_t)
    for tag, arr in [("delta_y", delta_y_top),
                     ("delta_z", delta_z_top)]:
        top_sec[f"{tag}_max"] = _ext_1d(arr, np.argmax, Nz - 1)
        top_sec[f"{tag}_min"] = _ext_1d(arr, np.argmin, Nz - 1)
        pf = u_tau_local_top * arr[:, None] / niu
        top_sec[f"{tag}_plus_max"] = _ext_2d(
            pf, np.argmax, Nz - 1, xa_t, ya_t, za_t)
        top_sec[f"{tag}_plus_min"] = _ext_2d(
            pf, np.argmin, Nz - 1, xa_t, ya_t, za_t)

    print(f"\n  [top k={Nz - 1}]")
    e = top_sec['u_tau_max']
    print(f"    u_tau_local_max = {e['value']:.6e}   "
          f"at (i={e['i']}, j={e['j']})")
    e = top_sec['u_tau_min']
    print(f"    u_tau_local_min = {e['value']:.6e}   "
          f"at (i={e['i']}, j={e['j']})")
    for tag in ("delta_y", "delta_z"):
        e = top_sec[f"{tag}_max"]
        print(f"    {tag}_max      = {e['value']:.6e}   "
              f"at (j={e['j']}, k={Nz - 1})")
        e = top_sec[f"{tag}_plus_max"]
        print(f"    {tag}_plus_max = {e['value']:.6f}    "
              f"at (i={e['i']}, j={e['j']}, k={Nz - 1})")

    # ---- write summary ----
    summary_path = os.path.join(folder, f"14.Re{Re}_zplus_summary.txt")
    print(f"\n  writing -> {summary_path}")
    write_global_summary(summary_path, Re, u_tau_global, niu,
                         delta_x, dx_plus, Nx, Ny, Nz,
                         extrema, center, bottom, top_sec)

    # ---- [3] bottom local z+ (simple |delta_z|) ----
    # tau_local_bot, u_tau_local_bot, dy/dz_bot_step, delta_z_simple_bot
    # already computed above (pre-compute block before step 2)
    print("\n[3] bottom wall local z+ (simple |delta_z|) ...")
    z_plus_bot_simple  = u_tau_local_bot * delta_z_simple_bot[:, None] / niu
    print(f"  delta_z_simple_bot range "
          f"[{delta_z_simple_bot.min():.4e}, {delta_z_simple_bot.max():.4e}]")
    print(f"  z_plus_bot_simple range  "
          f"[{z_plus_bot_simple.min():.4f}, {z_plus_bot_simple.max():.4f}]")
    bot_simple_path = os.path.join(folder, f"15.Re{Re}_zplus_bottom.dat")
    print(f"  writing -> {bot_simple_path}")
    write_zplus_simple_dat(bot_simple_path, "bottom", bot_path, Re,
                           bot["x"], bot["y"], bot["z"],
                           tau_local_bot, u_tau_local_bot,
                           delta_z_simple_bot, z_plus_bot_simple)

    # ---- [4] top local z+ (simple |delta_z|) ----
    # tau_local_top, u_tau_local_top, delta_z_simple_top
    # already computed above (pre-compute block before step 2)
    print("\n[4] top wall local z+ (simple |delta_z|) ...")
    z_plus_top_simple  = u_tau_local_top * delta_z_simple_top[:, None] / niu
    print(f"  delta_z_simple_top range "
          f"[{delta_z_simple_top.min():.4e}, {delta_z_simple_top.max():.4e}]")
    print(f"  z_plus_top_simple range  "
          f"[{z_plus_top_simple.min():.4f}, {z_plus_top_simple.max():.4f}]")
    top_simple_path = os.path.join(folder, f"16.Re{Re}_zplus_top.dat")
    print(f"  writing -> {top_simple_path}")
    write_zplus_simple_dat(top_simple_path, "top", top_path, Re,
                           top["x"], top["y"], top["z"],
                           tau_local_top, u_tau_local_top,
                           delta_z_simple_top, z_plus_top_simple)

    # ---- [5] bottom local z+ (n_hat projection) ----
    print("\n[5] bottom wall local z+ (n_hat projection) ...")
    n_y, n_z, h_xi_w = compute_wall_normal(y_2d, z_2d, k_wall=0,
                                            L_stream=L_stream)
    d_n_proj_bot = np.abs(dy_bot_step * n_y + dz_bot_step * n_z)
    z_plus_bot_normal = u_tau_local_bot * d_n_proj_bot[:, None] / niu
    print(f"  n_y range          [{n_y.min():+.4e}, {n_y.max():+.4e}]")
    print(f"  n_z range          [{n_z.min():+.4e}, {n_z.max():+.4e}]")
    print(f"  d_n_proj_bot range "
          f"[{d_n_proj_bot.min():.4e}, {d_n_proj_bot.max():.4e}]")
    print(f"  z_plus_bot_normal range "
          f"[{z_plus_bot_normal.min():.4f}, {z_plus_bot_normal.max():.4f}]")

    # comparison metric: how much does d_n_proj differ from delta_z_simple?
    # np.divide(..., where=...) avoids the inf/NaN that would arise if any
    # cell had a degenerate d_n_proj == 0 (cannot happen on a valid mesh,
    # but the guard keeps this diagnostic safe under future mesh changes).
    rel_diff = np.divide(np.abs(d_n_proj_bot - delta_z_simple_bot),
                         d_n_proj_bot,
                         out=np.zeros_like(d_n_proj_bot),
                         where=d_n_proj_bot > 0)
    print(f"  rel diff |d_n_proj - delta_z_simple| / d_n_proj:")
    print(f"     max = {rel_diff.max():.4e}, mean = {rel_diff.mean():.4e}")
    print(f"     -> larger near steep hill sections where |delta_y_step| > 0")

    bot_normal_path = os.path.join(folder,
                                    f"17.Re{Re}_zplus_bottom_normal.dat")
    print(f"  writing -> {bot_normal_path}")
    # Pass SIGNED dy/dz steps so the file matches its own header documentation:
    #   "delta_y(j) = y(j,1) - y(j,0);  delta_z(j) = z(j,1) - z(j,0)"
    # and a reader can verify d_n_proj = delta_y * n_y + delta_z * n_z
    # directly from the columns.
    write_zplus_normal_dat(bot_normal_path, bot_path, Re,
                           bot["x"], bot["y"], bot["z"],
                           tau_local_bot, u_tau_local_bot,
                           dy_bot_step, dz_bot_step,
                           n_y, n_z, d_n_proj_bot, z_plus_bot_normal)

    # ---- [6] u_tau span-averaged in x-direction ----
    print("\n[6] u_tau span-averaged in x-direction ...")
    u_tau_avg_bot = u_tau_local_bot.mean(axis=1)
    u_tau_avg_top = u_tau_local_top.mean(axis=1)
    print(f"  bot <u_tau>_x range [{u_tau_avg_bot.min():.6e}, "
          f"{u_tau_avg_bot.max():.6e}]")
    print(f"  top <u_tau>_x range [{u_tau_avg_top.min():.6e}, "
          f"{u_tau_avg_top.max():.6e}]")

    gen = "8.phase2_compute_zplus_1D2A.py"
    formula = "<u_tau>_x(j) = (1/Nx) * sum_{i=0}^{Nx-1} u_tau_local(i,j)"

    utau_bot_path = os.path.join(folder,
                                  f"37.Re{Re}_utau_spanavg_bottom_1D2A.txt")
    write_utau_spanavg_txt(utau_bot_path, "bottom", Re, niu, Nx, Ny,
                           bot["y"], bot["z"],
                           u_tau_avg_bot,
                           u_tau_local_bot.std(axis=1),
                           u_tau_local_bot.min(axis=1),
                           u_tau_local_bot.max(axis=1),
                           gen, formula)
    print(f"  writing -> {utau_bot_path}")

    utau_top_path = os.path.join(folder,
                                  f"38.Re{Re}_utau_spanavg_top_1D2A.txt")
    write_utau_spanavg_txt(utau_top_path, "top", Re, niu, Nx, Ny,
                           top["y"], top["z"],
                           u_tau_avg_top,
                           u_tau_local_top.std(axis=1),
                           u_tau_local_top.min(axis=1),
                           u_tau_local_top.max(axis=1),
                           gen, formula)
    print(f"  writing -> {utau_top_path}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
