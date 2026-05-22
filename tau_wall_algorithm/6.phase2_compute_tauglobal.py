# -*- coding: utf-8 -*-
"""
6.phase2_compute_tauglobal.py
=============================

Compute area-weighted GLOBAL wall friction |tau| over both walls.

Procedure (made explicit in 5 numbered steps per layer)
-------------------------------------------------------

For EACH wall layer (bottom = k=0, top = k=NZ-1):

  Step 1 -- Read tau_wall_signed at every grid corner of the wall layer
            (Nx*Ny corner points per layer).

  Step 2 -- Pointwise absolute value:  |tau| = abs(tau_wall_signed).
            This must be done before the area integral because tau_global is
            a magnitude average, not a signed force balance.

  Step 3 -- 4-point cell average of |tau| to produce ONE representative
            magnitude per (Nx-1)*(Ny-1) cell:

                tau_cell[j, i] = 1/4 * ( |tau|[j,   i  ]
                                       + |tau|[j+1, i  ]
                                       + |tau|[j,   i+1]
                                       + |tau|[j+1, i+1] )

  Step 4 -- Cell physical area on the curved wall:

                dA[j, i] = dx_i * sqrt(dy_j**2 + dz_j**2)

            where dx_i is the span-direction step (varies only with i)
            and sqrt(dy^2 + dz^2) is the arc-length step along the wall
            in the (y, z) plane.  This is the EXACT 3D parallelogram
            area for the tensor-product mesh.

  Step 5 -- Discrete area integral and total wall area
            (kept as SEPARATE quantities so the two walls can be combined
             by integral-then-divide rather than averaging averages):

                I_layer = sum_{cells} tau_cell * dA_cell
                A_layer = sum_{cells} dA_cell

After both walls are processed
------------------------------

  Per-wall area-weighted mean (= integral / area, applied per layer):

       tau_bottom_global = I_bottom / A_bottom
       tau_top_global    = I_top    / A_top

  Two-wall global mean (combine integrals first, then divide by the
  combined surface area):

       tau_global  =  (I_bottom + I_top) / (A_bottom + A_top)

  Friction velocity (rho = 1, textbook lattice form):

       u_tau_<X> = sqrt(tau_<X>_global / rho)

Inputs (auto-detected, must be unique):
    7.*_bottomtauwall.dat
    8.*_toptauwall.dat

Output:
    9.Re<XXX>_tauwall_global.dat   plain-text key=value summary
"""

from __future__ import annotations
import argparse, os, re, sys, time
from typing import Tuple
import numpy as np

# ---- I/O directory bootstrap (matches the Input/Output/Reference layout) -
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "Reference"))
OUTPUT_DIR = os.path.join(_HERE, "Output")
os.makedirs(OUTPUT_DIR, exist_ok=True)
# --------------------------------------------------------------------------

from phase1_common import (
    TAU_CONVENTION_LABEL,
    TAUWALL_DAT_COLUMNS,
    TAUWALL_DAT_NCOLS,
    cell_average_2d,
    cell_areas_2d,
    find_unique_matching,
    parse_re_token,
    verify_lattice_tau_dat,
)


# ============================================================================
#  Auto-detection of 7.*_bottomtauwall.dat / 8.*_toptauwall.dat
# ============================================================================
_BOT_RE = re.compile(r"^7\..+_bottomtauwall\.dat$", re.IGNORECASE)
_TOP_RE = re.compile(r"^8\..+_toptauwall\.dat$",    re.IGNORECASE)


def auto_detect_bot(folder: str = ".") -> str:
    return find_unique_matching(folder, "7.*.dat", _BOT_RE)


def auto_detect_top(folder: str = ".") -> str:
    return find_unique_matching(folder, "8.*.dat", _TOP_RE)


# ============================================================================
#  Per-layer area integral of |tau| (the 5-step procedure)
# ============================================================================
def compute_layer_integral(path: str,
                           label: str) -> Tuple[float, float, int, int]:
    """Compute the per-layer area integral I = integral(|tau| dA) and
    total wall area A on a single tau_wall dat file.

    Returns
    -------
    I    : float            sum_{cells} tau_cell * dA_cell
    A    : float            sum_{cells} dA_cell  (= total wall surface area)
    Nx   : int              span-direction grid count
    Ny   : int              stream-direction grid count
    """
    print(f"\n--- LAYER: {label} ({path}) ---")
    t0 = time.time()
    verify_lattice_tau_dat(path, f"{label} tau_wall")

    # ------------------------------------------------------------------
    # Step 1 -- Read grid-corner tau_wall_signed and wall coordinates.
    #
    # Tecplot POINT format storage convention (set by 5.py writer):
    #     row r corresponds to (i = r % Nx, j = r // Nx)
    #     i.e. i is the FAST index, j is the SLOW index.
    #
    # 13-column schema:
    #     col  0 = i        col  1 = j
    #     col  2 = x        col  3 = y         col  4 = z
    #     col  5 = du_t/dxi col  6 = du_t/dzeta
    #     col  7 = h_xi     col  8 = J         col  9 = e_xi.e_zeta
    #     col 10 = du_t/dn  col 11 = tau_wall_signed   col 12 = tau_wall_abs
    #
    # We read col 11 (tau_wall_signed) so that abs (step 2)
    # and cell-averaging (step 3) are performed HERE in Python, not silently
    # carried over from the precomputed col 12 in the dat file.
    # ------------------------------------------------------------------
    data = np.loadtxt(path, skiprows=4)
    if data.shape[1] != TAUWALL_DAT_NCOLS:
        raise ValueError(
            f"expected {TAUWALL_DAT_NCOLS} columns in {path}, "
            f"got {data.shape[1]}")

    i_col = data[:, TAUWALL_DAT_COLUMNS["i"]].astype(int)
    j_col = data[:, TAUWALL_DAT_COLUMNS["j"]].astype(int)
    Nx = int(i_col.max()) + 1
    Ny = int(j_col.max()) + 1
    if data.shape[0] != Nx * Ny:
        raise ValueError(
            f"row count {data.shape[0]} != Nx*Ny = {Nx}*{Ny} = {Nx*Ny}")

    # Reshape the SIGNED tau column from (Nx*Ny,) flat -> (Ny, Nx) grid.
    # The reshape works because the dat is i-fast j-slow (verified in
    # _verify_step6.py: i_col == arange % Nx, j_col == arange // Nx).
    tau_signed = data[:, TAUWALL_DAT_COLUMNS["tau_wall_signed"]].reshape(Ny, Nx)

    # Wall coordinates (1D):
    #     x varies only with i  -> sample at j=0 by reading rows 0..Nx-1
    #     y, z vary only with j -> sample at i=0 by reading rows
    #                              0, Nx, 2*Nx, ..., (Ny-1)*Nx
    x_arr = data[0:Nx,           TAUWALL_DAT_COLUMNS["x"]].copy()
    y_arr = data[0:Ny * Nx:Nx,   TAUWALL_DAT_COLUMNS["y"]].copy()
    z_arr = data[0:Ny * Nx:Nx,   TAUWALL_DAT_COLUMNS["z"]].copy()

    print(f"  [step 1] read {data.shape[0]:,} corner points  "
          f"(grid Nx={Nx}, Ny={Ny})  ({time.time() - t0:.1f}s)")
    print(f"           tau_signed range = "
          f"[{tau_signed.min():+.4e}, {tau_signed.max():+.4e}]")

    # ------------------------------------------------------------------
    # Step 2 -- Pointwise absolute value at every corner.
    #           tau_global is the area average of wall-shear magnitude, so the
    #           absolute value must be applied before the cell quadrature.
    # ------------------------------------------------------------------
    tau_abs = np.abs(tau_signed)
    print(f"  [step 2] |tau| range          = "
          f"[{tau_abs.min():.4e}, {tau_abs.max():.4e}]   "
          f"(after np.abs at every corner)")

    # ------------------------------------------------------------------
    # Step 3 -- 4-point cell average of |tau|.
    #           Output shape (Ny-1, Nx-1) = (Ny-1)*(Nx-1) cell-wise
    #           representative magnitudes:
    #
    #               tau_cell[j, i] = 1/4 * (|tau|[j,i] + |tau|[j+1,i]
    #                                     + |tau|[j,i+1] + |tau|[j+1,i+1])
    # ------------------------------------------------------------------
    tau_cell = cell_average_2d(tau_abs)
    print(f"  [step 3] tau_cell shape       = {tau_cell.shape}   "
          f"({tau_cell.size:,} cells = (Ny-1)*(Nx-1))")
    print(f"           tau_cell range       = "
          f"[{tau_cell.min():.4e}, {tau_cell.max():.4e}]")

    # ------------------------------------------------------------------
    # Step 4 -- Cell areas (curved-wall parallelogram):
    #
    #               dA[j, i] = dx_i * sqrt(dy_j**2 + dz_j**2)
    #
    #           dx_i = x[i+1] - x[i]    (span-direction step)
    #           dy_j = y[j+1] - y[j]    (stream component of arc step)
    #           dz_j = z[j+1] - z[j]    (normal component of arc step)
    # ------------------------------------------------------------------
    dA = cell_areas_2d(x_arr, y_arr, z_arr)
    print(f"  [step 4] dA shape             = {dA.shape}   "
          f"min={dA.min():.4e}, max={dA.max():.4e}")

    # ------------------------------------------------------------------
    # Step 5 -- Discrete area integral and total wall area.
    #           These are kept SEPARATE (not yet divided) so that the
    #           top and bottom walls can be combined as
    #               tau_global = (I_bot + I_top) / (A_bot + A_top)
    # ------------------------------------------------------------------
    I = float((tau_cell * dA).sum())
    A = float(dA.sum())
    print(f"  [step 5] I_layer = sum(tau_cell * dA) = {I:.12e}")
    print(f"           A_layer = sum(dA)            = {A:.12e}")

    return I, A, Nx, Ny


# ============================================================================
#  Output writer
# ============================================================================
def build_output_path(folder: str, re_tok: str) -> str:
    return os.path.join(folder, f"9.{re_tok}_tauwall_global.dat")


def write_summary(path: str,
                  bot_path: str, top_path: str,
                  Nx: int, Ny: int,
                  I_bot: float, A_bot: float,
                  I_top: float, A_top: float,
                  tau_bot: float, tau_top: float,
                  tau_global: float) -> None:
    n_cells = (Nx - 1) * (Ny - 1)
    A_total = A_bot + A_top
    I_total = I_bot + I_top
    with open(path, "w") as f:
        f.write("# Area-weighted global wall shear stress |tau|\n")
        f.write(f"# bottom source : {os.path.basename(bot_path)}\n")
        f.write(f"# top    source : {os.path.basename(top_path)}\n")
        f.write(f"# convention    : {TAU_CONVENTION_LABEL}\n")
        f.write("#\n")
        f.write("# Per-layer procedure (5 steps):\n")
        f.write("#   1. read tau_wall_signed at every (Nx*Ny) corner\n")
        f.write("#   2. tau_abs = |tau_wall_signed|  (pointwise corner abs)\n")
        f.write("#   3. 4-point cell average of tau_abs -> (Nx-1)*(Ny-1) cells\n")
        f.write("#   4. dA = dx * sqrt(dy^2 + dz^2)  per cell\n")
        f.write("#   5. I_layer = sum(|tau_cell| * dA);  A_layer = sum(dA)\n")
        f.write("#\n")
        f.write("# Per-wall mean       : tau_<wall> = I_<wall> / A_<wall>\n")
        f.write("# Two-wall global mean: tau_global = (I_bot + I_top) / (A_bot + A_top)\n")
        f.write(f"# Grid                : Nx={Nx}, Ny={Ny}, "
                f"n_cells_per_wall={n_cells}\n")
        f.write("\n")
        f.write("# ---- area integrals (intermediate values) ----\n")
        f.write(f"I_bottom          = {I_bot:.12e}\n")
        f.write(f"I_top             = {I_top:.12e}\n")
        f.write(f"I_total           = {I_total:.12e}\n")
        f.write("\n")
        f.write("# ---- wall surface areas ----\n")
        f.write(f"A_bottom          = {A_bot:.12e}\n")
        f.write(f"A_top             = {A_top:.12e}\n")
        f.write(f"A_total           = {A_total:.12e}\n")
        f.write("\n")
        f.write("# ---- area-weighted mean |tau| ----\n")
        f.write(f"tau_bottom_global = {tau_bot:.12e}\n")
        f.write(f"tau_top_global    = {tau_top:.12e}\n")
        f.write(f"tau_global        = {tau_global:.12e}\n")
        f.write("\n")
        f.write(f"# u_tau = sqrt(tau / rho)  with rho = 1\n")
        f.write(f"u_tau_bottom      = {np.sqrt(tau_bot):.12e}\n")
        f.write(f"u_tau_top         = {np.sqrt(tau_top):.12e}\n")
        f.write(f"u_tau_global      = {np.sqrt(tau_global):.12e}\n")


# ============================================================================
#  Main
# ============================================================================
def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Area-weighted global wall friction |tau| on bottom + "
                    "top walls, with explicit 5-step per-layer procedure.")
    p.add_argument("--bot", default=None,
                   help="bottom tauwall dat (default: auto 7.*_bottomtauwall.dat)")
    p.add_argument("--top", default=None,
                   help="top tauwall dat (default: auto 8.*_toptauwall.dat)")
    args = p.parse_args(argv)

    folder = OUTPUT_DIR
    bot_path = args.bot or auto_detect_bot(folder)
    top_path = args.top or auto_detect_top(folder)
    print(f"input bottom : {bot_path}")
    print(f"input top    : {top_path}")

    # ---- Per-layer 5-step processing ----
    I_bot, A_bot, Nx,  Ny  = compute_layer_integral(bot_path, "BOTTOM (k=0)")
    I_top, A_top, Nx2, Ny2 = compute_layer_integral(top_path, "TOP    (k=NZ-1)")
    if (Nx, Ny) != (Nx2, Ny2):
        print(f"[error] grid mismatch: bottom ({Nx},{Ny}) vs top "
              f"({Nx2},{Ny2})", file=sys.stderr)
        sys.exit(1)

    # ---- Combine ----
    print("\n--- COMBINE ---")
    tau_bot_global = I_bot / A_bot
    tau_top_global = I_top / A_top
    tau_global     = (I_bot + I_top) / (A_bot + A_top)
    print(f"  bottom : tau = I_bot / A_bot")
    print(f"               = {I_bot:.6e} / {A_bot:.6e}")
    print(f"               = {tau_bot_global:.6e}")
    print(f"  top    : tau = I_top / A_top")
    print(f"               = {I_top:.6e} / {A_top:.6e}")
    print(f"               = {tau_top_global:.6e}")
    print(f"  global : tau = (I_bot + I_top) / (A_bot + A_top)")
    print(f"               = ({I_bot:.6e} + {I_top:.6e}) "
          f"/ ({A_bot:.6e} + {A_top:.6e})")
    print(f"               = {(I_bot + I_top):.6e} / {(A_bot + A_top):.6e}")
    print(f"               = {tau_global:.6e}")
    print(f"  u_tau_global = sqrt(tau_global) = {np.sqrt(tau_global):.6e}")

    # ---- Sanity (top wall is flat -> A_top should equal LX*LY) ----
    top_xy = np.loadtxt(top_path, skiprows=4, usecols=(2, 3))
    LX = float(top_xy[:Nx, 0].max() - top_xy[:Nx, 0].min())
    LY = float(top_xy[0:Ny * Nx:Nx, 1].max() -
               top_xy[0:Ny * Nx:Nx, 1].min())
    print(f"\n--- SANITY ---")
    print(f"  LX (top span)        = {LX:.6f}")
    print(f"  LY (top stream span) = {LY:.6f}")
    print(f"  LX*LY                = {LX*LY:.12e}   (expected A_top)")
    print(f"  A_top                = {A_top:.12e}")
    print(f"  rel err              = {abs(A_top - LX*LY)/A_top:.2e}")
    print(f"  A_bot / A_top        = {A_bot/A_top:.6f}   "
          f"(>1 due to hill curvature)")

    # ---- Write summary ----
    re_tok = parse_re_token(os.path.basename(bot_path), default="ReXXX")
    out_path = build_output_path(folder, re_tok)
    print(f"\n--- WRITING SUMMARY: {out_path} ---")
    write_summary(out_path, bot_path, top_path, Nx, Ny,
                  I_bot, A_bot, I_top, A_top,
                  tau_bot_global, tau_top_global, tau_global)
    print(f"  wrote {os.path.getsize(out_path):,} bytes")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
