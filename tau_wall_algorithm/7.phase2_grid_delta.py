# -*- coding: utf-8 -*-
"""
7.phase2_grid_delta.py
======================

Compute mesh-spacing measures delta_y(j,k) and delta_z(j,k) at every
(j, k) grid point of the 2D Periodic-Hill mesh, then report the four
extrema delta_y_max, delta_y_min, delta_z_max, delta_z_min.

Pointwise definitions (forward-step + backward-step average; |.| keeps
the value strictly non-negative even on a non-monotonic segment):

    delta_y(j, k) = ( |y(j+1, k) - y(j  , k)|
                    + |y(j  , k) - y(j-1, k)| ) / 2

    delta_z(j, k) = ( |z(j, k+1) - z(j, k  )|
                    + |z(j, k  ) - z(j, k-1)| ) / 2

Boundary handling
-----------------

j-direction (stream, axis 1)  -- PERIODIC with y-coordinate offset
    Convention (matches step 3's d_dj_periodic):
        j = 0 and j = Ny-1 represent the SAME physical point;
        period in j-index = Ny - 1.
        y(j = -1)  = y(j = Ny-2) - L_stream
        y(j = Ny)  = y(j = 1)    + L_stream
    Result: delta_y(0, k) == delta_y(Ny-1, k)  exactly  (verified below).

k-direction (wall-normal, axis 0)  -- LINEAR EXTRAPOLATION of z
    z(j, k = -1)   = 2*z(j, 0)    - z(j, 1)
    z(j, k = Nz)   = 2*z(j, Nz-1) - z(j, Nz-2)
    Substituting into the |.| formula collapses the boundary delta_z to
    a one-sided difference:
        delta_z(j, 0)    = |z(j, 1)    - z(j, 0)|
        delta_z(j, Nz-1) = |z(j, Nz-1) - z(j, Nz-2)|
    i.e. the actual first / last wall-cell size (physically meaningful).

Procedure (5 numbered steps)
----------------------------

    Step 1 -- Read mesh (y, z) at every grid corner (Ny*Nz pts).
    Step 2 -- Pad axis-1 (j) with periodic wrap; pad axis-0 (k) with
              linear extrapolation.
    Step 3 -- Forward + backward step magnitudes via np.abs of the
              padded array differences (vectorised, no Python loops).
    Step 4 -- delta_y, delta_z = (|fwd| + |bwd|) / 2 at every (j, k).
    Step 5 -- Locate the four extrema and write the dat output.

Inputs (auto-detected, must be unique):
    2.j*_k*_g*_a*.dat        h-normalised 2D mesh from phase1_transdat
    9.Re<X>_tauwall_global.dat   u_tau_global (lattice friction velocity,
                                  produced by 6.py with the lattice convention
                                  tau = niu * du_t/dn)
    Input/variables.h            niu = Uref/Re (read at runtime)

Output:
    10.<stem>_delta.dat      Tecplot POINT, I=Ny, J=Nz, 4 cols (j, k, dy, dz)
    11.<stem>_delta_extrema.txt   4 extrema summary
    12.<stem>_grid_info.txt       grid parameter information
    13.Re<X>_Deltay_Deltaz.vtk   3D VTK STRUCTURED_GRID with
                                  delta_y, delta_z, delta_y_plus, delta_z_plus

Wall-unit formula (textbook lattice form, matches steps 5/6/8)
--------------------------------------------------------------
    Δy+ = Δy · u_τ / ν,    Δz+ = Δz · u_τ / ν

Both u_τ and ν are in matching lattice units:
    Δy, Δz       : h-normalised mesh (length unit = H_HILL = 1)
    ν            : niu from variables.h = Uref / Re
    u_τ          : sqrt(tau_wall / rho) with tau_wall = niu * du_t/dn
                   (read straight from 9.*.dat; no extra Uref factor)
"""

from __future__ import annotations
import argparse
import os
import re
import sys
import math
import time
from typing import Tuple

import numpy as np

# ---- I/O directory bootstrap (matches the Input/Output/Reference layout) -
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "Reference"))
INPUT_DIR  = os.path.join(_HERE, "Input")
OUTPUT_DIR = os.path.join(_HERE, "Output")
os.makedirs(OUTPUT_DIR, exist_ok=True)
# --------------------------------------------------------------------------

from phase1_common import (
    parse_tecplot_2d_mesh,
    find_unique_matching,
    parse_header_constants,
    find_const,
    auto_detect_variables_h,
    verify_lattice_tau_dat,
)


# ============================================================================
#  Auto-detection of 2.j*_k*_g*_a*.dat
# ============================================================================
_INPUT_RE = re.compile(r"^2\.j.+\.dat$", re.IGNORECASE)


def auto_detect_mesh(folder: str = ".") -> str:
    return find_unique_matching(folder, "2.*.dat", _INPUT_RE)


def _stem_from_mesh(mesh_path: str) -> str:
    base = os.path.basename(mesh_path)
    m = re.match(r"^2\.(.+)\.dat$", base, re.IGNORECASE)
    if not m:
        raise ValueError(f"input filename must match '2.<stem>.dat': {base}")
    return m.group(1)


def build_output_path(folder: str, mesh_path: str) -> str:
    """`<folder>/2.j257_k129_g2.0_a0.5.dat` ->
       `<folder>/10.j257_k129_g2.0_a0.5_delta.dat`"""
    return os.path.join(folder, f"10.{_stem_from_mesh(mesh_path)}_delta.dat")


def build_extrema_txt_path(folder: str, mesh_path: str) -> str:
    """`<folder>/2.j257_k129_g2.0_a0.5.dat` ->
       `<folder>/11.j257_k129_g2.0_a0.5_delta_extrema.txt`"""
    return os.path.join(folder,
                        f"11.{_stem_from_mesh(mesh_path)}_delta_extrema.txt")


def build_grid_info_path(folder: str, mesh_path: str) -> str:
    return os.path.join(folder,
                        f"12.{_stem_from_mesh(mesh_path)}_grid_info.txt")


# ============================================================================
#  Auto-detection of 9.*_tauwall_global.dat (for u_tau_global)
# ============================================================================
_GLOBAL_RE = re.compile(r"^9\..+_tauwall_global\.dat$", re.IGNORECASE)


def auto_detect_global(folder: str = ".") -> str:
    return find_unique_matching(folder, "9.*.dat", _GLOBAL_RE)


def read_utau_global(path: str) -> float:
    """Parse u_tau_global from 9.*.dat key=value summary."""
    with open(path) as f:
        s = f.read()
    m = re.search(r"^u_tau_global\s*=\s*([\d.eE+-]+)", s, re.MULTILINE)
    if not m:
        raise ValueError(f"u_tau_global not found in {path}")
    return float(m.group(1))


def parse_re_from_global(path: str) -> str:
    """Extract Re token (e.g. 'Re5600') from 9.Re<X>_tauwall_global.dat."""
    base = os.path.basename(path)
    m = re.search(r"(Re\d+)", base)
    if not m:
        raise ValueError(f"cannot find Re<num> in: {base}")
    return m.group(1)


def build_vtk_output_path(folder: str, re_tok: str) -> str:
    return os.path.join(folder, f"13.{re_tok}_Deltay_Deltaz.vtk")


# ============================================================================
#  Grid parameter analysis -- tanh_wall back-calculation from Gamma
# ============================================================================
def parse_grid_params_from_stem(stem: str) -> dict:
    """Parse Ny, Nz, gamma, alpha from stem like 'j257_k129_g2.0_a0.5'."""
    params: dict = {}
    for pat, key, conv in [
        (r'j(\d+)', 'Ny', int),
        (r'k(\d+)', 'Nz', int),
        (r'g([0-9.]+)', 'gamma', float),
        (r'a([0-9.]+)', 'alpha', float),
    ]:
        m = re.search(pat, stem)
        if m:
            params[key] = conv(m.group(1))
    return params


def tanh_wall(L: float, a: float, j: int, N: int) -> float:
    """Hyperbolic-tangent wall stretching (from initializationTool.h).

    z(j) = L/2 + (L/2/a)*tanh((-1+2*j/N)/2 * ln((1+a)/(1-a)))

    Maps j in [0, N] to z in [0, L].  Symmetric about L/2.
    a -> 0: uniform spacing (dz = L/N).
    a -> 1: extreme wall clustering (dz_wall -> 0).
    """
    return (L / 2.0
            + (L / 2.0 / a)
            * math.tanh((-1.0 + 2.0 * j / N) / 2.0
                        * math.log((1.0 + a) / (1.0 - a))))


def vinokur_tanh_distribution(N_nodes: int, gamma: float,
                              alpha: float = 0.5) -> np.ndarray:
    """Vinokur two-sided tanh clustering.  Returns zeta in [0, 1]."""
    eta = np.linspace(0, 1, N_nodes)
    if gamma < 1e-14:
        return eta.copy()
    denom = np.tanh(gamma * alpha)
    if abs(denom) < 1e-30:
        return eta.copy()
    zeta = 0.5 * (1.0 + np.tanh(gamma * (eta - alpha)) / denom)
    zeta[0] = 0.0
    zeta[-1] = 1.0
    return zeta


def gamma_to_tanh_a(gamma: float, alpha: float = 0.5) -> float:
    """Convert Vinokur gamma to equivalent tanh_wall 'a' parameter.

    For alpha=0.5 (symmetric stretching) there is an exact closed-form:
        a = tanh(gamma / 2)
    because the two formulas are mathematically identical:
        Vinokur:   z = 1/2 + 1/(2*tanh(g/2)) * tanh(g*(eta - 1/2))
        tanh_wall: z = 1/2 + 1/(2a) * tanh((2*eta - 1) * atanh(a))
    Setting g = 2*atanh(a)  =>  tanh(g/2) = a, and the two coincide.

    For alpha != 0.5 the Vinokur distribution is asymmetric and cannot
    be reproduced by tanh_wall (which is always symmetric).  In that
    case we fall back to bisection, matching the minimum wall spacing.
    """
    if abs(alpha - 0.5) < 1e-12:
        return math.tanh(gamma / 2.0)
    # Asymmetric: match minimum positive wall spacing via bisection
    N_probe = 256
    zeta = vinokur_tanh_distribution(N_probe + 1, gamma, alpha)
    dzeta = np.diff(zeta)
    positive = dzeta[dzeta > 0]
    if len(positive) == 0:
        return 0.0
    dz_norm = float(positive.min())
    return back_calculate_tanh_a(N_probe, dz_norm)


def back_calculate_tanh_a(N_cells: int, dz_norm: float) -> float:
    """Find tanh_wall stretching parameter 'a' in (0,1) via bisection.

    Given normalised wall spacing dz_norm = dz_wall / L,
    solve  tanh_wall(1, a, 1, N_cells) == dz_norm  for a.
    """
    a_lo, a_hi = 1e-10, 1.0 - 1e-15
    for _ in range(200):
        a_mid = (a_lo + a_hi) / 2.0
        dz = tanh_wall(1.0, a_mid, 1, N_cells)
        if dz > dz_norm:
            a_lo = a_mid
        else:
            a_hi = a_mid
        if abs(dz - dz_norm) / max(dz_norm, 1e-30) < 1e-12:
            break
    return a_mid


def compute_grid_info(z_2d: np.ndarray, Ny: int, Nz: int,
                      stem: str) -> dict:
    """Compute comprehensive grid parameter information.

    - Parses Gamma/Alpha from the filename stem
    - Measures actual wall spacing from the grid at j=0
    - Back-calculates the equivalent tanh_wall 'a' parameter
    - For Vinokur regime (Gamma >= 1): computes analytical spacing too
    """
    N_cells = Nz - 1
    params = parse_grid_params_from_stem(stem)
    info = dict(params)
    info['N_cells_z'] = N_cells
    info['N_cells_y'] = Ny - 1

    # ---- Actual grid measurements at j=0 (reference column) ----
    z_col0 = z_2d[:, 0]
    L_col0 = float(z_col0[-1] - z_col0[0])
    dz_col0 = np.diff(z_col0)
    dz_wall_bot = float(dz_col0[0])
    dz_wall_top = float(dz_col0[-1])
    dz_center   = float(dz_col0[N_cells // 2])
    ratio_actual = float(dz_col0.max() / dz_col0.min())

    info['L_col0']        = L_col0
    info['dz_wall_bot']   = dz_wall_bot
    info['dz_wall_top']   = dz_wall_top
    info['dz_center']     = dz_center
    info['ratio_actual']  = ratio_actual

    dz_norm_actual = dz_wall_bot / L_col0
    info['dz_norm_actual'] = dz_norm_actual
    info['a_from_grid']    = back_calculate_tanh_a(N_cells, dz_norm_actual)

    # ---- Analytical values from Gamma/Alpha ----
    if 'gamma' in params and 'alpha' in params:
        gamma = params['gamma']
        alpha = params['alpha']
        if gamma < 1.0:
            info['a_from_gamma']    = gamma
            info['stretching_mode'] = 'tanh_wall (gamma < 1)'
            dz_norm_analytical      = tanh_wall(1.0, gamma, 1, N_cells)
        else:
            info['stretching_mode'] = 'Vinokur tanh (gamma >= 1)'
            zeta  = vinokur_tanh_distribution(Nz, gamma, alpha)
            dzeta = np.diff(zeta)
            dz_pos = dzeta[dzeta > 0]
            dz_norm_analytical       = float(dz_pos.min())
            info['dz_norm_vinokur']  = dz_norm_analytical
            info['ratio_vinokur']    = float(dz_pos.max() / dz_pos.min())
            # a = tanh(gamma/2) exact for alpha=0.5; bisection fallback
            info['a_from_gamma']     = gamma_to_tanh_a(gamma, alpha)
        info['dz_norm_analytical'] = dz_norm_analytical

    return info


# ============================================================================
#  VTK writer — 3D STRUCTURED_GRID (constant in x-direction)
# ============================================================================
def write_delta_plus_vtk(path: str,
                         y_2d: np.ndarray, z_2d: np.ndarray,
                         LX: float, Nx_vtk: int,
                         delta_y: np.ndarray, delta_z: np.ndarray,
                         delta_y_plus: np.ndarray,
                         delta_z_plus: np.ndarray) -> None:
    """Write a 3D STRUCTURED_GRID VTK (ASCII) with 4 scalar fields.

    The grid is extruded uniformly in x (span) with Nx_vtk points.
    Data is constant across the x-direction (each (j,k) value is replicated
    Nx_vtk times along x).  Point ordering follows the VTK convention for
    DIMENSIONS Nx Ny Nz: x is the fastest index, z the slowest.
    """
    Nz, Ny = delta_y.shape
    n_points = Nx_vtk * Ny * Nz
    x_coords = np.linspace(0.0, LX, Nx_vtk)

    # ---- Build coordinate array via numpy broadcast (Nz, Ny, Nx_vtk, 3) ----
    # Final flat order: k-slow, j-mid, ix-fast (matches DIMENSIONS Nx Ny Nz).
    X = np.broadcast_to(x_coords[None, None, :], (Nz, Ny, Nx_vtk))
    Y = np.broadcast_to(y_2d[:, :, None],        (Nz, Ny, Nx_vtk))
    Z = np.broadcast_to(z_2d[:, :, None],        (Nz, Ny, Nx_vtk))
    coords = np.stack([X, Y, Z], axis=-1).reshape(-1, 3)

    with open(path, "w") as f:
        f.write("# vtk DataFile Version 3.0\n")
        f.write("Grid spacing delta_y, delta_z, delta_y_plus, delta_z_plus\n")
        f.write("ASCII\n")
        f.write("DATASET STRUCTURED_GRID\n")
        f.write(f"DIMENSIONS {Nx_vtk} {Ny} {Nz}\n")
        f.write(f"POINTS {n_points} double\n")
        np.savetxt(f, coords, fmt="%.10e %.10e %.10e")

        f.write(f"\nPOINT_DATA {n_points}\n")
        for name, field in [("delta_y",      delta_y),
                            ("delta_z",      delta_z),
                            ("delta_y_plus", delta_y_plus),
                            ("delta_z_plus", delta_z_plus)]:
            f.write(f"SCALARS {name} double 1\n")
            f.write("LOOKUP_TABLE default\n")
            flat = np.broadcast_to(field[:, :, None],
                                   (Nz, Ny, Nx_vtk)).reshape(-1)
            np.savetxt(f, flat, fmt="%.10e")


def write_grid_info_txt(path: str, mesh_path: str, info: dict) -> None:
    """Write grid parameter information to a plain-text file."""
    with open(path, "w") as f:
        f.write("# Grid Parameter Information\n")
        f.write(f"# source mesh : {os.path.basename(mesh_path)}\n")
        f.write(f"# generated by: 7.phase2_grid_delta.py\n")
        f.write("#\n")
        f.write("# tanh_wall formula (initializationTool.h):\n")
        f.write("#   z(j) = L/2 + (L/2/a)*tanh((-1+2*j/N)/2"
                " * ln((1+a)/(1-a)))\n")
        f.write("#   L = column height, N = cell count, a in (0,1)\n")
        f.write("#   a->0 : uniform   a->1 : extreme wall clustering\n")
        f.write("#\n")
        f.write("# Closed-form (alpha=0.5):\n")
        f.write("#   a = tanh(gamma/2)\n")
        f.write("#   gamma = 2*atanh(a) = log((1+a)/(1-a))\n")
        f.write("#\n")

        f.write(f"Ny (stream nodes)          = {info.get('Ny', '?')}\n")
        f.write(f"Nz (wall-normal nodes)     = {info.get('Nz', '?')}\n")
        f.write(f"N_cells_y (stream)         = {info.get('N_cells_y', '?')}\n")
        f.write(f"N_cells_z (wall-normal)    = {info.get('N_cells_z', '?')}\n")
        f.write("\n")

        if 'gamma' in info:
            f.write(f"Gamma (from filename)      = {info['gamma']}\n")
        if 'alpha' in info:
            f.write(f"Alpha (from filename)      = {info['alpha']}\n")
        if 'stretching_mode' in info:
            f.write(f"Stretching mode            = {info['stretching_mode']}\n")
        f.write("\n")

        f.write("# --- Analytical (from Gamma/Alpha) ---\n")
        if 'dz_norm_analytical' in info:
            f.write(f"dz_norm (analytical)       = "
                    f"{info['dz_norm_analytical']:.15e}\n")
        if 'ratio_vinokur' in info:
            f.write(f"Stretching ratio (Vinokur) = "
                    f"{info['ratio_vinokur']:.6f}\n")
        if 'a_from_gamma' in info:
            f.write(f"Equivalent tanh_wall 'a'   = "
                    f"{info['a_from_gamma']:.15f}\n")
        f.write("\n")

        f.write("# --- Measured from grid (j=0 reference column) ---\n")
        f.write(f"L_column (z_top - z_bot)   = {info['L_col0']:.15e}\n")
        f.write(f"dz_wall_bottom             = {info['dz_wall_bot']:.15e}\n")
        f.write(f"dz_wall_top                = {info['dz_wall_top']:.15e}\n")
        f.write(f"dz_center                  = {info['dz_center']:.15e}\n")
        f.write(f"dz_norm (measured)         = {info['dz_norm_actual']:.15e}\n")
        f.write(f"Stretching ratio (actual)  = {info['ratio_actual']:.6f}\n")
        f.write(f"Back-calculated 'a' (grid) = {info['a_from_grid']:.15f}\n")

        if 'a_from_gamma' in info:
            err = abs(info['a_from_gamma'] - info['a_from_grid'])
            f.write(f"\n# --- Consistency check ---\n")
            f.write(f"|a(analytical) - a(grid)|  = {err:.6e}\n")


# ============================================================================
#  Core computation (the 5-step procedure, vectorised)
# ============================================================================
def compute_delta(y_2d: np.ndarray,
                  z_2d: np.ndarray,
                  L_stream: float) -> Tuple[np.ndarray, np.ndarray]:
    """Compute delta_y, delta_z at every (j, k) grid point.

    Parameters
    ----------
    y_2d, z_2d : np.ndarray, shape (Nz, Ny)
        Mesh coordinates as returned by parse_tecplot_2d_mesh.
        Axis 0 = k (wall-normal), axis 1 = j (stream).
    L_stream : float
        Stream-direction period length, used to wrap y across j-boundary.

    Returns
    -------
    delta_y, delta_z : np.ndarray, shape (Nz, Ny)
        Pointwise mesh spacings at every (j, k).
    """
    Nz, Ny = y_2d.shape

    # ------------------------------------------------------------------
    # Step 2a -- pad axis-1 (j) with periodic wrap.
    #            y_pad[:, 0]      represents j = -1
    #            y_pad[:, Ny+1]   represents j = Ny
    #            y has L_stream offset across the period;
    #            z is genuinely periodic (no offset, but we still need
    #            its boundary neighbours for the k-direction differencing
    #            which doesn't actually use j-neighbours, so z padding in
    #            j is unnecessary -- we only pad y).
    # ------------------------------------------------------------------
    y_pad = np.empty((Nz, Ny + 2))
    y_pad[:, 1:Ny + 1] = y_2d
    y_pad[:, 0]        = y_2d[:, Ny - 2] - L_stream    # j = -1
    y_pad[:, Ny + 1]   = y_2d[:, 1]      + L_stream    # j = Ny

    # ------------------------------------------------------------------
    # Step 2b -- pad axis-0 (k) with linear extrapolation of z.
    #            z_pad[0,    :] represents k = -1
    #            z_pad[Nz+1, :] represents k = Nz
    # ------------------------------------------------------------------
    z_pad = np.empty((Nz + 2, Ny))
    z_pad[1:Nz + 1, :] = z_2d
    z_pad[0,        :] = 2 * z_2d[0,      :] - z_2d[1,      :]   # k = -1
    z_pad[Nz + 1,   :] = 2 * z_2d[Nz - 1, :] - z_2d[Nz - 2, :]   # k = Nz

    # ------------------------------------------------------------------
    # Step 3 -- forward and backward step magnitudes.
    # ------------------------------------------------------------------
    dy_fwd = np.abs(y_pad[:, 2:Ny + 2] - y_pad[:, 1:Ny + 1])   # |y(j+1) - y(j)|
    dy_bwd = np.abs(y_pad[:, 1:Ny + 1] - y_pad[:, 0:Ny])        # |y(j) - y(j-1)|

    dz_fwd = np.abs(z_pad[2:Nz + 2, :] - z_pad[1:Nz + 1, :])   # |z(k+1) - z(k)|
    dz_bwd = np.abs(z_pad[1:Nz + 1, :] - z_pad[0:Nz, :])        # |z(k) - z(k-1)|

    # ------------------------------------------------------------------
    # Step 4 -- delta = (forward + backward) / 2 at every grid point.
    # ------------------------------------------------------------------
    delta_y = (dy_fwd + dy_bwd) / 2.0
    delta_z = (dz_fwd + dz_bwd) / 2.0

    return delta_y, delta_z


# ============================================================================
#  Output writer
# ============================================================================
def write_delta_dat(path: str,
                    mesh_path: str,
                    L_stream: float,
                    delta_y: np.ndarray,
                    delta_z: np.ndarray) -> None:
    """Tecplot POINT format, I=Ny, J=Nz, j-fast k-slow ordering, 4 columns."""
    Nz, Ny = delta_y.shape

    # Build the 4-column data array in j-fast k-slow order.
    # Use np.indices so j and k are integer columns.
    ks, js = np.indices((Nz, Ny))                              # both shape (Nz, Ny)
    data = np.column_stack([
        js.ravel(order="C"),                                    # 0: j
        ks.ravel(order="C"),                                    # 1: k
        delta_y.ravel(order="C"),                               # 2: delta_y
        delta_z.ravel(order="C"),                               # 3: delta_z
    ])
    # C-order ravel of (Nz, Ny) is k-slow j-fast → matches I=Ny J=Nz POINT layout

    with open(path, "w") as f:
        f.write("# Mesh-spacing measures delta_y, delta_z at every (j, k)\n")
        f.write(f"# source mesh : {os.path.basename(mesh_path)}\n")
        f.write(f"# L_stream     = {L_stream:.15e}  (used as y-offset in j wrap)\n")
        f.write("# formulas:\n")
        f.write("#   delta_y(j,k) = ( |y(j+1,k) - y(j,k)|"
                " + |y(j,k) - y(j-1,k)| ) / 2\n")
        f.write("#   delta_z(j,k) = ( |z(j,k+1) - z(j,k)|"
                " + |z(j,k) - z(j,k-1)| ) / 2\n")
        f.write("# boundaries:\n")
        f.write("#   j=0, j=Ny-1 : periodic wrap, y(j=-1)=y(Ny-2)-L_stream,\n")
        f.write("#                                y(j=Ny)=y(1)+L_stream\n")
        f.write("#   k=0, k=Nz-1 : linear extrapolation of z, collapses to\n")
        f.write("#                 one-sided difference (= first/last cell size)\n")
        f.write('TITLE     = "Grid spacing delta_y, delta_z"\n')
        f.write('VARIABLES = "j" "k" "delta_y" "delta_z"\n')
        f.write(f'ZONE T="grid_delta", I={Ny}, J={Nz}, F=POINT\n')
        f.write('DT=(SINGLE SINGLE SINGLE SINGLE)\n')
        np.savetxt(f, data, fmt="%4d %4d %.15e %.15e")


# ============================================================================
#  Extrema-summary text writer (4-row table)
# ============================================================================
def write_extrema_txt(path: str,
                      mesh_path: str,
                      Ny: int, Nz: int,
                      delta_y: np.ndarray,
                      delta_z: np.ndarray,
                      y_2d: np.ndarray,
                      z_2d: np.ndarray,
                      k_dy_min: int, j_dy_min: int,
                      k_dy_max: int, j_dy_max: int,
                      k_dz_min: int, j_dz_min: int,
                      k_dz_max: int, j_dz_max: int) -> None:
    """Plain-text 4-row table of the extrema:

        metric        value          j     k     y             z
        delta_y_min   ...            ...   ...   ...           ...
        delta_y_max   ...
        delta_z_min   ...
        delta_z_max   ...
    """
    n_grid = Ny * Nz
    with open(path, "w") as f:
        f.write("# Mesh-spacing extrema (4 values over the full grid)\n")
        f.write(f"# source mesh : {os.path.basename(mesh_path)}\n")
        f.write(f"# generated by: 7.phase2_grid_delta.py\n")
        f.write(f"# grid        : Ny={Ny} (stream), Nz={Nz} (wall-normal), "
                f"total {n_grid:,} corner points\n")
        f.write("#\n")
        f.write("# Pointwise definitions:\n")
        f.write("#   delta_y(j,k) = ( |y(j+1,k)-y(j,k)| + |y(j,k)-y(j-1,k)| ) / 2\n")
        f.write("#   delta_z(j,k) = ( |z(j,k+1)-z(j,k)| + |z(j,k)-z(j,k-1)| ) / 2\n")
        f.write("# Boundary handling:\n")
        f.write("#   j-direction (stream): periodic wrap with y-offset L_stream\n")
        f.write("#   k-direction (normal): linear extrapolation of z (one-sided)\n")
        f.write("#\n")
        f.write("# Columns:\n")
        f.write("#   metric        value(*)         j     k    y           z\n")
        f.write("#   --------      -------------    ---   ---  ----------  ----------\n")
        f.write("#   (*) value reported with 12 significant digits\n")
        f.write("\n")

        def _row(label, dy_or_dz, k_idx, j_idx):
            return (f"{label:<14s}{dy_or_dz[k_idx, j_idx]:.12e}   "
                    f"{j_idx:4d}  {k_idx:4d}  "
                    f"{y_2d[k_idx, j_idx]:+11.7f}  "
                    f"{z_2d[k_idx, j_idx]:+11.7f}\n")

        f.write(_row("delta_y_min",  delta_y, k_dy_min, j_dy_min))
        f.write(_row("delta_y_max",  delta_y, k_dy_max, j_dy_max))
        f.write(_row("delta_z_min",  delta_z, k_dz_min, j_dz_min))
        f.write(_row("delta_z_max",  delta_z, k_dz_max, j_dz_max))


# ============================================================================
#  Main
# ============================================================================
def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Compute delta_y(j,k) and delta_z(j,k) at every grid "
                    "point of the 2D Periodic-Hill mesh and report the "
                    "four extrema.")
    p.add_argument("--mesh", default=None,
                   help="input mesh dat (default: auto 2.j*_k*_g*_a*.dat)")
    args = p.parse_args(argv)

    folder = OUTPUT_DIR
    mesh_path = args.mesh or auto_detect_mesh(folder)
    print(f"input mesh   : {mesh_path}")

    # ---- Step 1: read mesh ----
    print("\n[1] reading 2D mesh ...")
    t0 = time.time()
    y_2d, z_2d, J, K = parse_tecplot_2d_mesh(mesh_path)
    Ny, Nz = J, K        # rename for clarity within this script
    L_stream = float(y_2d[0, -1] - y_2d[0, 0])
    print(f"  shape (Nz, Ny) = ({Nz}, {Ny})   total points = {Nz*Ny:,}")
    print(f"  y range [{y_2d.min():+.6f}, {y_2d.max():+.6f}]")
    print(f"  z range [{z_2d.min():+.6f}, {z_2d.max():+.6f}]")
    print(f"  L_stream (y(j=Ny-1) - y(j=0)) = {L_stream:.6f}")
    print(f"  ({time.time() - t0:.2f}s)")

    # ---- Grid parameter info (back-calculate tanh_wall 'a') ----
    stem = _stem_from_mesh(mesh_path)
    grid_info = compute_grid_info(z_2d, Ny, Nz, stem)

    print(f"\n[grid params]")
    if 'gamma' in grid_info:
        print(f"  Gamma (filename)          = {grid_info['gamma']}")
    if 'alpha' in grid_info:
        print(f"  Alpha (filename)          = {grid_info['alpha']}")
    if 'stretching_mode' in grid_info:
        print(f"  Stretching mode           = {grid_info['stretching_mode']}")
    if 'dz_norm_analytical' in grid_info:
        print(f"  dz_norm (analytical)      = "
              f"{grid_info['dz_norm_analytical']:.12e}")
    if 'ratio_vinokur' in grid_info:
        print(f"  Stretching ratio (Vinokur)= {grid_info['ratio_vinokur']:.4f}")
    if 'a_from_gamma' in grid_info:
        print(f"  Equivalent tanh_wall 'a'  = "
              f"{grid_info['a_from_gamma']:.12f}")
        if 'gamma' in grid_info and abs(grid_info.get('alpha', 0.5) - 0.5) < 1e-12:
            print(f"    (closed-form: a = tanh(gamma/2) "
                  f"= tanh({grid_info['gamma']/2:.4f}))")

    print(f"\n  [measured at j=0]")
    print(f"  L_column                  = {grid_info['L_col0']:.12f}")
    print(f"  dz_wall (bottom)          = {grid_info['dz_wall_bot']:.12e}")
    print(f"  dz_wall (top)             = {grid_info['dz_wall_top']:.12e}")
    print(f"  dz_center                 = {grid_info['dz_center']:.12e}")
    print(f"  dz_norm (measured)        = "
          f"{grid_info['dz_norm_actual']:.12e}")
    print(f"  Stretching ratio (actual) = {grid_info['ratio_actual']:.4f}")
    print(f"  Back-calculated 'a'       = "
          f"{grid_info['a_from_grid']:.12f}")

    if 'a_from_gamma' in grid_info:
        err = abs(grid_info['a_from_gamma'] - grid_info['a_from_grid'])
        print(f"  |a(analytical) - a(grid)| = {err:.6e}")

    # ---- Steps 2-4: pad + compute deltas ----
    print("\n[2-4] padding + computing delta_y, delta_z ...")
    t0 = time.time()
    delta_y, delta_z = compute_delta(y_2d, z_2d, L_stream)
    print(f"  delta_y shape = {delta_y.shape}   "
          f"range [{delta_y.min():.6e}, {delta_y.max():.6e}]")
    print(f"  delta_z shape = {delta_z.shape}   "
          f"range [{delta_z.min():.6e}, {delta_z.max():.6e}]")
    print(f"  ({time.time() - t0:.2f}s)")

    # ---- Sanity: periodic consistency on j boundary ----
    pdy = float(np.abs(delta_y[:, 0] - delta_y[:, Ny - 1]).max())
    print(f"\n  [sanity] max |delta_y(0,k) - delta_y(Ny-1,k)| = {pdy:.3e}   "
          f"(expect 0 by periodic equivalence)")

    # ---- Sanity: k boundary collapses to one-sided diff ----
    dz0_check  = np.abs(np.abs(z_2d[1, :]      - z_2d[0, :])      - delta_z[0, :]).max()
    dzNm1_check = np.abs(np.abs(z_2d[Nz-1, :] - z_2d[Nz-2, :])    - delta_z[Nz-1, :]).max()
    print(f"  [sanity] max |delta_z(j,0)    - |z(j,1) - z(j,0)||     "
          f"= {dz0_check:.3e}   (expect 0)")
    print(f"  [sanity] max |delta_z(j,Nz-1) - |z(j,Nz-1) - z(j,Nz-2)||"
          f" = {dzNm1_check:.3e}   (expect 0)")

    # ---- Sanity: positivity ----
    print(f"  [sanity] min delta_y = {delta_y.min():.6e}   "
          f"({'OK' if delta_y.min() > 0 else 'FAIL: zero or negative'})")
    print(f"  [sanity] min delta_z = {delta_z.min():.6e}   "
          f"({'OK' if delta_z.min() > 0 else 'FAIL: zero or negative'})")

    # ---- Step 5: extrema + write output ----
    print("\n[5] locating extrema ...")
    # delta_y extrema
    k_dy_min, j_dy_min = np.unravel_index(np.argmin(delta_y), delta_y.shape)
    k_dy_max, j_dy_max = np.unravel_index(np.argmax(delta_y), delta_y.shape)
    # delta_z extrema
    k_dz_min, j_dz_min = np.unravel_index(np.argmin(delta_z), delta_z.shape)
    k_dz_max, j_dz_max = np.unravel_index(np.argmax(delta_z), delta_z.shape)

    print(f"  delta_y_min = {delta_y[k_dy_min, j_dy_min]:.6e}   "
          f"at (j={j_dy_min}, k={k_dy_min})  "
          f"y={y_2d[k_dy_min, j_dy_min]:.4f}, z={z_2d[k_dy_min, j_dy_min]:.4f}")
    print(f"  delta_y_max = {delta_y[k_dy_max, j_dy_max]:.6e}   "
          f"at (j={j_dy_max}, k={k_dy_max})  "
          f"y={y_2d[k_dy_max, j_dy_max]:.4f}, z={z_2d[k_dy_max, j_dy_max]:.4f}")
    print(f"  delta_z_min = {delta_z[k_dz_min, j_dz_min]:.6e}   "
          f"at (j={j_dz_min}, k={k_dz_min})  "
          f"y={y_2d[k_dz_min, j_dz_min]:.4f}, z={z_2d[k_dz_min, j_dz_min]:.4f}")
    print(f"  delta_z_max = {delta_z[k_dz_max, j_dz_max]:.6e}   "
          f"at (j={j_dz_max}, k={k_dz_max})  "
          f"y={y_2d[k_dz_max, j_dz_max]:.4f}, z={z_2d[k_dz_max, j_dz_max]:.4f}")

    out_path = build_output_path(folder, mesh_path)
    print(f"\n[6] writing full grid dat -> {out_path}")
    t0 = time.time()
    write_delta_dat(out_path, mesh_path, L_stream, delta_y, delta_z)
    print(f"  wrote {os.path.getsize(out_path):,} bytes  "
          f"({time.time() - t0:.2f}s)")

    extrema_path = build_extrema_txt_path(folder, mesh_path)
    print(f"\n[7] writing extrema summary -> {extrema_path}")
    t0 = time.time()
    write_extrema_txt(extrema_path, mesh_path, Ny, Nz,
                      delta_y, delta_z, y_2d, z_2d,
                      k_dy_min, j_dy_min, k_dy_max, j_dy_max,
                      k_dz_min, j_dz_min, k_dz_max, j_dz_max)
    print(f"  wrote {os.path.getsize(extrema_path):,} bytes  "
          f"({time.time() - t0:.2f}s)")

    grid_info_path = build_grid_info_path(folder, mesh_path)
    print(f"\n[8] writing grid info -> {grid_info_path}")
    t0 = time.time()
    write_grid_info_txt(grid_info_path, mesh_path, grid_info)
    print(f"  wrote {os.path.getsize(grid_info_path):,} bytes  "
          f"({time.time() - t0:.2f}s)")

    # ================================================================
    #  Step 9: Compute delta_y+, delta_z+ and write 3D VTK
    # ================================================================
    print("\n[9] computing delta_y_plus, delta_z_plus ...")

    # ---- Read niu, Uref from variables.h ----
    # Unit-system note:
    #   variables.h defines  niu = Uref / Re  (lattice kinematic viscosity).
    #   6.py outputs         u_tau_global = sqrt(tau_global/rho) with
    #                        tau = niu * du_t/dn (lattice stress).
    #   So u_tau_global is already in the lattice velocity unit that matches
    #   niu, and we form delta+ directly as delta * u_tau / niu.
    vh_path = auto_detect_variables_h(INPUT_DIR)
    if vh_path is None:
        print("  [SKIP] variables.h not found in Input/ — cannot compute delta+")
        print("\nDone.")
        return 0
    consts = parse_header_constants(vh_path)
    niu  = find_const(consts, ["niu", "nu"], vh_path)
    Uref = find_const(consts, ["Uref"], vh_path)
    LX   = find_const(consts, ["LX"], vh_path)
    NX   = int(find_const(consts, ["NX"], vh_path))   # spanwise node count
    print(f"  niu  (from variables.h, = Uref/Re) = {niu:.15e}")
    print(f"  Uref (from variables.h)            = {Uref:.6e}")
    print(f"  LX   (spanwise length)             = {LX}")
    print(f"  NX   (spanwise nodes)              = {NX}")

    # ---- Read u_tau_global from 9.*.dat (lattice friction velocity) ----
    try:
        global_path = auto_detect_global(folder)
    except (FileNotFoundError, ValueError) as e:
        print(f"  [SKIP] 9.*_tauwall_global.dat not found — "
              f"cannot compute delta+ ({e})")
        print("\nDone.")
        return 0
    verify_lattice_tau_dat(global_path, "global tau input")
    u_tau_global = read_utau_global(global_path)
    re_tok = parse_re_from_global(global_path)
    print(f"  u_tau_global  (lattice, sqrt(tau/rho)) = {u_tau_global:.12e}  "
          f"(from {os.path.basename(global_path)})")

    # ---- Compute delta+ : delta * u_tau / niu  (textbook y+) ----
    scale = u_tau_global / niu
    print(f"  scale (u_tau_global / niu)             = {scale:.6e}")
    delta_y_plus = delta_y * scale
    delta_z_plus = delta_z * scale
    print(f"  delta_y_plus range [{delta_y_plus.min():.4f}, "
          f"{delta_y_plus.max():.4f}]")
    print(f"  delta_z_plus range [{delta_z_plus.min():.4f}, "
          f"{delta_z_plus.max():.4f}]")

    # ---- Write VTK ----
    # Span-direction node count from variables.h NX (data is constant in x,
    # so the field values are simply replicated NX times per (j,k) point).
    Nx_vtk = NX
    vtk_path = build_vtk_output_path(folder, re_tok)
    print(f"\n[10] writing VTK -> {vtk_path}")
    print(f"  STRUCTURED_GRID DIMENSIONS ({Nx_vtk}, {Ny}, {Nz})")
    print(f"  total points = {Nx_vtk * Ny * Nz:,}")
    t0 = time.time()
    write_delta_plus_vtk(vtk_path, y_2d, z_2d, LX, Nx_vtk,
                         delta_y, delta_z, delta_y_plus, delta_z_plus)
    print(f"  wrote {os.path.getsize(vtk_path):,} bytes  ({time.time()-t0:.2f}s)")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
