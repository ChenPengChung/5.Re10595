# -*- coding: utf-8 -*-
"""
5.phase2_compute_tauwall.py
===========================

Compute wall shear stress tau_wall on bottom (k=0) and top (k=NZ-1) walls
from the 7-layer u_tangent slabs produced by 4.phase2_computeutangent.py.

Inputs (auto-detected, must be unique):
    5.*_utan_*_k0-6.dat            bottom 7 layers + wall-row metric
    6.*_utan_*_k*-*.dat            top    7 layers + wall-row metric
    variables.h                    flat text file with niu = ...
                                   regex tries: niu, NIU, NU, nu_value, ...
    --niu <float>                  CLI override (preferred when easy)

Output:
    7.Re<XXX>_i<Nx>j<Ny>_bottomtauwall.dat   shape Nx*Ny = 33,153 points
    8.Re<XXX>_i<Nx>j<Ny>_toptauwall.dat      same

Format (Tecplot POINT, K=1):
    VARIABLES = "i" "j" "x" "y" "z" "du_t_dxi" "du_t_dzeta" "h_xi" "J"
                "e_xi.e_zeta" "du_t_dn" "tau_wall_signed" "tau_wall_abs"
    ZONE I=Nx, J=Ny, F=POINT

Formula (textbook lattice form)
-------------------------------
At each (i, j) wall point:

    du_t/dn   =  (h_xi / J) * du_t/dzeta
               - (e_xi.e_zeta / (h_xi * J)) * du_t/dxi

    tau_wall  =  niu * du_t/dn            (rho = 1)

Step 4 already rescaled VTK velocity from V_mean=V_lat/Uref back to
physical lattice units (×Uref), so u_t in 5/6.dat is V_lattice
projected onto the wall tangent.  No extra Uref correction needed.
The matching textbook friction velocity (used in step 6/8) is

    u_tau     =  sqrt(tau_wall / rho)
    z+        =  u_tau * d_n / niu        (= u_tau*y/nu)

Numerics
--------
    du_t/dzeta : 6th-order single-sided Fornberg FD across the 7 layers.
                 Bottom: p=0 forward    (reads u_t at k=0..6)
                 Top:    p=6 backward   (reads u_t at k=K-7..K-1)

    du_t/dxi   : 6th-order central FD with periodic wrap on j, applied to
                 u_t at the wall row only (k=0 for bottom, k=K-1 for top).
                 No period offset (u_t is a periodic field, not a coordinate).

The cross-coupling term (-(e_xi.e_zeta) / (h_xi*J) * du_t/dxi) corrects
for the fact that the mesh zeta direction is NOT exactly perpendicular to
the wall (mesh non-orthogonality, cos theta up to ~0.11 at top wall).
The term is kept in the formula for correctness, but for the present
*time-mean* tau_wall, no-slip enforces u_t ~ 0 along the entire wall row
=> du_t/dxi ~ 0 (~1e-5 magnitude), which makes the cross-coupling
contribution numerically negligible (~1e-7).  It can become significant
for *instantaneous* tau or fluctuation analysis.

Output is *signed* (preserves direction along wall tangent), suitable for
detecting reattachment / separation by sign change.  For magnitude
analysis (u_tau, log-law fitting), use |tau_wall|.
"""

from __future__ import annotations
import argparse, os, re, sys, time
from typing import List
import numpy as np

# ---- I/O directory bootstrap (matches the Input/Output/Reference layout) -
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "Reference"))
OUTPUT_DIR    = os.path.join(_HERE, "Output")
INPUT_DIR     = os.path.join(_HERE, "Input")
REFERENCE_DIR = os.path.join(_HERE, "Reference")
os.makedirs(OUTPUT_DIR, exist_ok=True)
# --------------------------------------------------------------------------

from phase1_common import (
    FD6_FWD,
    FD6_BWD,
    d_dxi_periodic_2d_axis0 as d_dxi_periodic_2d,
    find_unique_matching,
    parse_re_token,
    auto_detect_variables_h,
    parse_header_constants,
    find_const,
    TAU_CONVENTION_LABEL,
    WALL_DAT_COLUMNS,
    WALL_DAT_NCOLS,
)


# ============================================================================
#  Auto-detection
# ============================================================================
_BOT_RE = re.compile(r"^5\..+utan.*_k0-6\.dat$",   re.IGNORECASE)
_TOP_RE = re.compile(r"^6\..+utan.*_k\d+-\d+\.dat$", re.IGNORECASE)


def auto_detect_bot(folder=".") -> str:
    return find_unique_matching(folder, "5.*.dat", _BOT_RE)


def auto_detect_top(folder=".") -> str:
    return find_unique_matching(folder, "6.*.dat", _TOP_RE)


# ============================================================================
#  variables.h parser
# ============================================================================
#  Single source of truth lives in phase1_common: auto_detect_variables_h,
#  parse_header_constants, find_const (all imported above).  No local copy.


# ============================================================================
#  Read 5/6 dat (15-column Tecplot POINT)
# ============================================================================
class WallSlab:
    """Container for the seven-layer u_t slab + wall-row metric (1D in j)."""
    def __init__(self,
                 u_t: np.ndarray,
                 x_arr: np.ndarray,
                 y_2d: np.ndarray,    # (n_layers, Ny)
                 z_2d: np.ndarray,
                 k_layers: List[int],
                 h_xi: np.ndarray,    # (Ny,)
                 J: np.ndarray,
                 e_xi_dot_e_zeta: np.ndarray,
                 y_kn: np.ndarray,
                 z_kn: np.ndarray):
        self.u_t = u_t
        self.x_arr = x_arr
        self.y_2d = y_2d
        self.z_2d = z_2d
        self.k_layers = k_layers
        self.h_xi = h_xi
        self.J = J
        self.eXZ = e_xi_dot_e_zeta
        self.y_kn = y_kn
        self.z_kn = z_kn
        self.n_layers, self.Ny, self.Nx = u_t.shape


def load_utan_dat(path: str) -> WallSlab:
    """Read 4 header lines, then n_layers*Ny*Nx data rows of 15 columns each.

    Column layout (set by 4.script):
        0:i  1:j  2:k  3:x  4:y  5:z  6:V_mean  7:W_mean
        8:u_tangent  9:u_normal  10:h_xi  11:J  12:e_xi.e_zeta  13:y_kn  14:z_kn

    Data ordering: i-fast, j-mid, k-slow.
    """
    print(f"  reading {path} ...")
    t0 = time.time()
    data = np.loadtxt(path, skiprows=4)
    print(f"    {data.shape[0]:,} rows x {data.shape[1]} cols  "
          f"({time.time() - t0:.1f}s)")
    if data.shape[1] != WALL_DAT_NCOLS:
        raise ValueError(
            f"expected {WALL_DAT_NCOLS} columns, got {data.shape[1]}")

    k_col = data[:, 2].astype(int)
    j_col = data[:, 1].astype(int)
    i_col = data[:, 0].astype(int)
    k_layers = sorted(set(k_col.tolist()))
    n_layers = len(k_layers)
    Ny = int(j_col.max()) + 1
    Nx = int(i_col.max()) + 1
    if data.shape[0] != n_layers * Ny * Nx:
        raise ValueError(
            f"row count {data.shape[0]} != n_layers*Ny*Nx "
            f"({n_layers}*{Ny}*{Nx} = {n_layers*Ny*Nx})")

    u_t = data[:, 8].reshape(n_layers, Ny, Nx)
    # x, y, z (per (k_layer, j, i)). x varies only with i; y, z with (k, j).
    x_arr  = data[0:Nx, 3].copy()           # first row block: i = 0..Nx-1, j=k=0
    y_2d   = data[:, 4].reshape(n_layers, Ny, Nx)[:, :, 0]  # any i works
    z_2d   = data[:, 5].reshape(n_layers, Ny, Nx)[:, :, 0]

    # Wall-row metric: constant in i and k.  Read from first layer first column.
    # Strided read at i=0 over j=0..Ny-1 of layer 0.
    base = data[0:Ny*Nx:Nx]                  # rows where i=0, layer 0
    h_xi = base[:, 10].copy()
    J    = base[:, 11].copy()
    eXZ  = base[:, 12].copy()
    y_kn = base[:, 13].copy()
    z_kn = base[:, 14].copy()

    return WallSlab(u_t, x_arr, y_2d, z_2d, k_layers,
                    h_xi, J, eXZ, y_kn, z_kn)


# ============================================================================
#  Output writer
# ============================================================================
def write_tauwall_dat(path: str, label: str,
                      Nx: int, Ny: int,
                      x_arr: np.ndarray,
                      y_wall: np.ndarray,        # (Ny,)
                      z_wall: np.ndarray,        # (Ny,)
                      du_t_dxi: np.ndarray,      # (Ny, Nx)
                      du_t_dzeta: np.ndarray,    # (Ny, Nx)
                      h_xi: np.ndarray,          # (Ny,)
                      J: np.ndarray,             # (Ny,)
                      eXZ: np.ndarray,           # (Ny,)
                      du_t_dn: np.ndarray,       # (Ny, Nx)
                      tau_wall: np.ndarray,      # (Ny, Nx) — signed
                      convention_label: str = TAU_CONVENTION_LABEL) -> None:
    """Tecplot POINT format, K=1 ZONE, 13 columns.

    Columns 12 and 13:
        tau_wall_signed : signed tau (preserves direction; use for separation /
                                       reattachment by sign change)
        tau_wall_abs    : |tau|        (use for magnitude analysis: u_tau,
                                        log-law fitting)
    """
    tau_abs = np.abs(tau_wall)
    chunks: List[str] = []
    chunks.append(f'TITLE     = "tau_wall on {label}, '
                  f'h_n=1 arc-length, {convention_label} '
                  f'(signed + abs columns)"\n')
    chunks.append('VARIABLES = "i" "j" "x" "y" "z" '
                  '"du_t_dxi" "du_t_dzeta" "h_xi" "J" "e_xi.e_zeta" '
                  '"du_t_dn" "tau_wall_signed" "tau_wall_abs"\n')
    chunks.append(f'ZONE T="{label}", I={Nx}, J={Ny}, F=POINT\n')
    chunks.append('DT=(SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE '
                  'SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE)\n')
    for j in range(Ny):
        y_j = y_wall[j]
        z_j = z_wall[j]
        hxi = h_xi[j]
        Jj  = J[j]
        eXZj= eXZ[j]
        for i in range(Nx):
            chunks.append(
                f"{i:4d} {j:4d} "
                f"{x_arr[i]:.15e} {y_j:.15e} {z_j:.15e} "
                f"{du_t_dxi[j,i]:.15e} {du_t_dzeta[j,i]:.15e} "
                f"{hxi:.15e} {Jj:.15e} {eXZj:.15e} "
                f"{du_t_dn[j,i]:.15e} "
                f"{tau_wall[j,i]:.15e} {tau_abs[j,i]:.15e}\n"
            )
    with open(path, "w") as f:
        f.writelines(chunks)


# ============================================================================
#  Filename helpers
# ============================================================================
def build_bot_path(folder, re_tok, Nx, Ny):
    return os.path.join(folder, f"7.{re_tok}_i{Nx}j{Ny}_bottomtauwall.dat")


def build_top_path(folder, re_tok, Nx, Ny):
    return os.path.join(folder, f"8.{re_tok}_i{Nx}j{Ny}_toptauwall.dat")


# ============================================================================
#  Main
# ============================================================================
def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Compute tau_wall on bottom and top walls.")
    p.add_argument("--bot", default=None,
                   help="bottom 7-layer dat (default: auto 5.*_k0-6.dat)")
    p.add_argument("--top", default=None,
                   help="top 7-layer dat (default: auto 6.*_k*-*.dat)")
    p.add_argument("--variables-h", default=None,
                   help="path to variables.h (default: auto-detect)")
    p.add_argument("--niu", type=float, default=None,
                   help="kinematic viscosity nu = Uref/Re "
                        "(overrides variables.h)")
    p.add_argument("--Uref", type=float, default=None,
                   help="reference velocity (auto-detected from variables.h; "
                        "used only as a sanity print, since the multiplier is "
                        "niu = Uref/Re directly)")
    p.add_argument("--rho", type=float, default=1.0,
                   help="density (default 1.0)")
    args = p.parse_args(argv)

    folder = OUTPUT_DIR
    bot_path = args.bot or auto_detect_bot(folder)
    top_path = args.top or auto_detect_top(folder)

    # ---- niu, Uref ----
    var_h = args.variables_h or auto_detect_variables_h(INPUT_DIR)
    consts = parse_header_constants(var_h) if var_h else {}

    if args.niu is not None:
        niu, niu_src = args.niu, "CLI --niu"
    elif consts:
        niu, niu_src = find_const(consts, ["niu", "nu"], var_h), f"file {var_h}"
    else:
        raise FileNotFoundError(
            "need --niu or Input/variables.h containing niu")

    if args.Uref is not None:
        Uref, Uref_src = args.Uref, "CLI --Uref"
    elif consts:
        Uref, Uref_src = find_const(consts, ["Uref", "U_ref"], var_h), f"file {var_h}"
    else:
        Uref, Uref_src = float("nan"), "not found"

    # Step 4 already rescaled velocity to physical lattice units (×Uref),
    # so u_t in 5/6.dat is V_lattice projected onto the wall tangent.
    multiplier = niu                       # mu = niu = Uref/Re
    convention_label = TAU_CONVENTION_LABEL

    rho = args.rho

    print(f"input bottom : {bot_path}")
    print(f"input top    : {top_path}")
    print(f"niu (= mu)   = {niu:.6e}    (source: {niu_src})")
    print(f"Uref         = {Uref:.6e}   (source: {Uref_src})")
    print(f"rho          = {rho}")
    print(f"convention   : {convention_label}")
    print(f"multiplier   = niu = {multiplier:.6e}")

    # ---- [1] Load slabs ----
    print("\n[1] loading 7-layer slabs ...")
    bot = load_utan_dat(bot_path)
    top = load_utan_dat(top_path)
    if bot.u_t.shape != top.u_t.shape:
        print(f"[error] bot/top shape mismatch: {bot.u_t.shape} vs {top.u_t.shape}",
              file=sys.stderr)
        sys.exit(1)
    Nx, Ny = bot.Nx, bot.Ny
    print(f"  shape (n_layers, Ny, Nx) = ({bot.n_layers}, {Ny}, {Nx})")
    print(f"  bottom k_layers = {bot.k_layers}")
    print(f"  top    k_layers = {top.k_layers}")

    # ---- [2] du_t / dzeta at walls (single-sided Fornberg) ----
    print("\n[2] du_t/dzeta at walls (6th-order single-sided Fornberg) ...")
    t0 = time.time()
    dut_dzeta_bot = np.einsum('m,mji->ji', FD6_FWD, bot.u_t)   # k=0   forward
    dut_dzeta_top = np.einsum('m,mji->ji', FD6_BWD, top.u_t)   # k=K-1 backward
    print(f"  bottom (k=0)   range [{dut_dzeta_bot.min():+.4e}, "
          f"{dut_dzeta_bot.max():+.4e}]")
    print(f"  top    (k={top.k_layers[-1]}) range [{dut_dzeta_top.min():+.4e}, "
          f"{dut_dzeta_top.max():+.4e}]")
    print(f"  ({time.time() - t0:.1f}s)")

    # ---- [3] du_t / dxi at walls (central + periodic wrap) ----
    print("\n[3] du_t/dxi at walls (6th-order central + periodic wrap) ...")
    t0 = time.time()
    # u_t at the actual wall row: k=0 for bottom, k=K-1 (= last layer in slab) for top
    u_t_bot_wall = bot.u_t[0]                 # shape (Ny, Nx)
    u_t_top_wall = top.u_t[-1]
    dut_dxi_bot = d_dxi_periodic_2d(u_t_bot_wall)
    dut_dxi_top = d_dxi_periodic_2d(u_t_top_wall)
    print(f"  bottom (k=0)   range [{dut_dxi_bot.min():+.4e}, "
          f"{dut_dxi_bot.max():+.4e}]")
    print(f"  top    (k={top.k_layers[-1]}) range [{dut_dxi_top.min():+.4e}, "
          f"{dut_dxi_top.max():+.4e}]")
    print(f"  ({time.time() - t0:.1f}s)")

    # ---- [4] du_t / dn = (h_xi/J) du/dzeta - (e/J/h_xi) du/dxi ----
    print("\n[4] du_t/dn  =  (h_xi/J) * du_t/dzeta  -  (e_xi.e_zeta / (h_xi*J)) * du_t/dxi")
    t0 = time.time()
    # 1D wall metric -> broadcast to (Ny, Nx)
    A_bot = (bot.h_xi / bot.J)[:, None]
    B_bot = (bot.eXZ / (bot.h_xi * bot.J))[:, None]
    A_top = (top.h_xi / top.J)[:, None]
    B_top = (top.eXZ / (top.h_xi * top.J))[:, None]
    dut_dn_bot = A_bot * dut_dzeta_bot - B_bot * dut_dxi_bot
    dut_dn_top = A_top * dut_dzeta_top - B_top * dut_dxi_top
    print(f"  bottom (k=0)   range [{dut_dn_bot.min():+.4e}, "
          f"{dut_dn_bot.max():+.4e}]")
    print(f"  top    (k={top.k_layers[-1]}) range [{dut_dn_top.min():+.4e}, "
          f"{dut_dn_top.max():+.4e}]")
    # contribution magnitudes
    main_bot  = np.max(np.abs(A_bot * dut_dzeta_bot))
    cross_bot = np.max(np.abs(B_bot * dut_dxi_bot))
    main_top  = np.max(np.abs(A_top * dut_dzeta_top))
    cross_top = np.max(np.abs(B_top * dut_dxi_top))
    print(f"  bottom: main term .max = {main_bot:.4e}, "
          f"cross-coupling.max = {cross_bot:.4e}, "
          f"ratio = {cross_bot/main_bot*100:.2f}%")
    print(f"  top:    main term .max = {main_top:.4e}, "
          f"cross-coupling.max = {cross_top:.4e}, "
          f"ratio = {cross_top/main_top*100:.2f}%")
    print(f"  ({time.time() - t0:.1f}s)")

    # ---- [5] tau_wall = niu * du_t/dn  (rho = 1, lattice stress) ----
    print(f"\n[5] tau_wall  =  niu * du_t/dn   "
          f"({convention_label}, multiplier=niu={multiplier:.4e})")
    t0 = time.time()
    tau_bot = multiplier * dut_dn_bot
    tau_top = multiplier * dut_dn_top
    print(f"  bottom tau_wall_signed range [{tau_bot.min():+.4e}, "
          f"{tau_bot.max():+.4e}]")
    print(f"  bottom tau_wall_abs    range [{np.abs(tau_bot).min():.4e}, "
          f"{np.abs(tau_bot).max():.4e}]")
    print(f"  top    tau_wall_signed range [{tau_top.min():+.4e}, "
          f"{tau_top.max():+.4e}]")
    print(f"  top    tau_wall_abs    range [{np.abs(tau_top).min():.4e}, "
          f"{np.abs(tau_top).max():.4e}]")
    # u_tau estimates (textbook: sqrt(|tau|/rho), lattice friction velocity)
    utau_bot_max = np.sqrt(np.abs(tau_bot).max() / rho)
    utau_top_max = np.sqrt(np.abs(tau_top).max() / rho)
    print(f"  u_tau peak (= sqrt(|tau|/rho)): "
          f"bottom = {utau_bot_max:.6e}, top = {utau_top_max:.6e}")
    print(f"  ({time.time() - t0:.1f}s)")

    # ---- [6] write outputs ----
    re_tok = parse_re_token(os.path.basename(bot_path), default="ReXXX")
    bot_out = build_bot_path(folder, re_tok, Nx, Ny)
    top_out = build_top_path(folder, re_tok, Nx, Ny)

    print(f"\n[6] writing bottom -> {bot_out}")
    t0 = time.time()
    # Wall-row coordinates: y/z at k=0 for bottom, k=K-1 for top
    y_wall_bot = bot.y_2d[0]
    z_wall_bot = bot.z_2d[0]
    y_wall_top = top.y_2d[-1]
    z_wall_top = top.z_2d[-1]
    write_tauwall_dat(bot_out, "bottom_tauwall",
                      Nx, Ny, bot.x_arr, y_wall_bot, z_wall_bot,
                      dut_dxi_bot, dut_dzeta_bot,
                      bot.h_xi, bot.J, bot.eXZ,
                      dut_dn_bot, tau_bot, convention_label=convention_label)
    print(f"  wrote {os.path.getsize(bot_out):,} bytes  "
          f"({time.time() - t0:.1f}s)")

    print(f"\n[7] writing top    -> {top_out}")
    t0 = time.time()
    write_tauwall_dat(top_out, "top_tauwall",
                      Nx, Ny, top.x_arr, y_wall_top, z_wall_top,
                      dut_dxi_top, dut_dzeta_top,
                      top.h_xi, top.J, top.eXZ,
                      dut_dn_top, tau_top, convention_label=convention_label)
    print(f"  wrote {os.path.getsize(top_out):,} bytes  "
          f"({time.time() - t0:.1f}s)")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
