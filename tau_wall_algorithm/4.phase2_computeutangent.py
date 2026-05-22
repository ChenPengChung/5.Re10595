# -*- coding: utf-8 -*-
"""
4.phase2_computeutangent.py
===========================

Compute wall-tangent and wall-normal velocity components on the 7 grid
layers nearest each wall.  VTK stores V_mean = V_lattice / Uref
(nondimensional); this script multiplies by Uref at read time so that
u_tangent/u_normal in the output .dat are in physical lattice units.
Downstream tau = niu * du_t/dn is therefore correct without further
Uref correction.

  BOTTOM wall (hill-shaped):
        k = 0, 1, 2, 3, 4, 5, 6
        STRICT-ORTHONORMAL projection onto wall-aligned frame at k=0.
        Frame is built by rotating (y_xi, z_xi) 90 deg CCW to get the
        wall-normal direction; orthogonality of (t_hat, n_hat) is
        guaranteed by construction regardless of mesh non-orthogonality.

  TOP wall (FLAT z=const surface, but mesh non-orthogonal):
        k = NZ-7, NZ-6, ..., NZ-1  (ascending file order; wall row last)
        Skip projection — top wall is horizontal, so global y_hat IS
        the wall tangent and global z_hat IS the wall normal.  Just
        dump V_mean, W_mean into the u_tangent / u_normal columns.

Strict-orthonormal frame at the bottom wall (uses ONLY y_xi, z_xi):

    h_xi(j, 0)  = sqrt(y_xi(j,0)^2 + z_xi(j,0)^2)

    t_hat(j) = (  y_xi(j,0),  z_xi(j,0)) / h_xi(j,0)        (wall tangent)
    n_hat(j) = ( -z_xi(j,0),  y_xi(j,0)) / h_xi(j,0)        (wall normal,
                                                              90 deg CCW)

    t_hat . n_hat = 0   strictly, by construction.

Velocity projections (rotation matrix [t_hat ; n_hat] applied to (V,W)):

    u_tangent(i,j,k) = ( V*y_xi(j,0) + W*z_xi(j,0) ) / h_xi(j,0)
    u_normal (i,j,k) = (-V*z_xi(j,0) + W*y_xi(j,0) ) / h_xi(j,0)

The strictly wall-normal coordinate axis k_n (unit arc-length step):

    y_kn(j) = -z_xi(j,0) / h_xi(j,0)        (= n_hat . y_hat)
    z_kn(j) = +y_xi(j,0) / h_xi(j,0)        (= n_hat . z_hat)
    h_kn    = 1                             (k_n is arc-length parameter)

Extra wall-row metric carried in the output dat (needed for the
non-orthogonal cross-coupling term in tau_wall):

    J(j, 0)             = y_xi*z_zeta - y_zeta*z_xi   (forward Jacobian det)
    e_xi_dot_e_zeta(j)  = y_xi*y_zeta + z_xi*z_zeta   (mesh non-orthogonality)
                                                      (= 0 iff mesh orthogonal)

with V = V_mean(i,j,k) (stream-wise), W = W_mean(i,j,k) (wall-normal in
global Cartesian), and the Jacobian / Lame coefficients evaluated at the
wall row only:

    y_xi(j,wall), y_zeta(j,wall), z_xi(j,wall), z_zeta(j,wall)
    J(j,wall)      = y_xi*z_zeta - y_zeta*z_xi
    h_xi(j,wall)   = sqrt(y_xi^2  + z_xi^2)
    h_zeta(j,wall) = sqrt(y_zeta^2 + z_zeta^2)

Project convention: i,x,u,U = span ; j,y,v,V = stream ; k,z,w,W = normal.
xi = j (stream), zeta = k (wall-normal).

Numerics
--------
6th-order finite differences for the wall-row metric:
    y_xi, z_xi at k=wall      : 6th-order central with periodic wrap
                                (period offset L_stream on y, 0 on z)
    y_zeta, z_zeta at k=0     : 6th-order forward Fornberg (p=0), reads y[0..6]
    y_zeta, z_zeta at k=NZ-1  : 6th-order backward Fornberg (p=6), reads y[NZ-7..NZ-1]

Inputs (auto-detected, must be unique):
    1.*_v2.vtk       3D ASCII VTK (V_mean, W_mean)
    2.j*_*.dat       2D mesh (y, z)

Outputs:
    5.Re<XXX>_utan_i<Nx>_j<Ny>_k0-6.dat                  bottom 7 layers
    6.Re<XXX>_utan_i<Nx>_j<Ny>_k<NZ-7>-<NZ-1>.dat        top 7 layers

Both are Tecplot POINT format with 15 columns:
    i  j  k  x  y  z  V_mean  W_mean  u_tangent  u_normal
    h_xi  J  e_xi.e_zeta  y_kn  z_kn
"""

from __future__ import annotations
import argparse, io, os, re, sys, time
from typing import Dict, List, Tuple
import numpy as np

# ---- I/O directory bootstrap (matches the Input/Output/Reference layout) -
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "Reference"))
OUTPUT_DIR = os.path.join(_HERE, "Output")
INPUT_DIR  = os.path.join(_HERE, "Input")
os.makedirs(OUTPUT_DIR, exist_ok=True)
# --------------------------------------------------------------------------

from phase1_common import (
    FD6_COEFF,
    d_dj_periodic_row,
    parse_tecplot_2d_mesh,
    map_vtk_sections,
    parse_dimensions,
    read_scalar_full,
    read_x_array,
    find_unique_matching,
    parse_re_token,
    auto_detect_variables_h,
    parse_header_constants,
    find_const,
)


# ============================================================================
#  File discovery
# ============================================================================
_VTK_IN_RE = re.compile(r"^1\..+_v2\.vtk$", re.IGNORECASE)
_DAT_IN_RE = re.compile(r"^2\.j.+\.dat$",   re.IGNORECASE)


def auto_detect_vtk(folder: str = ".") -> str:
    return find_unique_matching(folder, "*.vtk", _VTK_IN_RE)


def auto_detect_dat(folder: str = ".") -> str:
    return find_unique_matching(folder, "*.dat", _DAT_IN_RE)


# ============================================================================
#  Wall-row metric: 1D arrays in j at the wall (k=0 or k=K-1)
# ============================================================================
def metric_at_wall(y_2d: np.ndarray, z_2d: np.ndarray,
                   wall: str) -> Dict[str, np.ndarray]:
    """Compute y_xi, y_zeta, z_xi, z_zeta, h_xi, h_zeta, J as 1D arrays in j
    at the specified wall.  6th-order FD throughout.

    wall: 'bottom' for k=0 (Fornberg p=0 forward in zeta)
          'top'    for k=K-1 (Fornberg p=6 backward in zeta)
    """
    K, J = y_2d.shape
    if wall == "bottom":
        k_w = 0
        # ∂/∂zeta at k=0 reads y[0..6] with FD6_COEFF[0] (forward)
        coef = FD6_COEFF[0]
        y_zeta_w = coef @ y_2d[0:7,  :]    # shape (J,)
        z_zeta_w = coef @ z_2d[0:7,  :]
    elif wall == "top":
        k_w = K - 1
        coef = FD6_COEFF[6]
        y_zeta_w = coef @ y_2d[K - 7:K, :]
        z_zeta_w = coef @ z_2d[K - 7:K, :]
    else:
        raise ValueError(f"wall must be 'bottom' or 'top', got {wall!r}")

    # ∂/∂xi at k=k_w: 6th-order central with periodic wrap on the wall row
    L = float(y_2d[k_w, -1] - y_2d[k_w, 0])    # period length in y
    y_xi_w = d_dj_periodic_row(y_2d[k_w], period_offset=L)
    z_xi_w = d_dj_periodic_row(z_2d[k_w], period_offset=0.0)

    # Jacobian + Lame coefficients (1D in j, all at the wall)
    J_w      = y_xi_w * z_zeta_w - y_zeta_w * z_xi_w
    h_xi_w   = np.sqrt(y_xi_w  ** 2 + z_xi_w  ** 2)
    h_zeta_w = np.sqrt(y_zeta_w ** 2 + z_zeta_w ** 2)

    # Mesh non-orthogonality measure: e_xi . e_zeta (= 0 iff mesh orthogonal)
    e_xi_dot_e_zeta_w = y_xi_w * y_zeta_w + z_xi_w * z_zeta_w

    # Strictly wall-normal coordinate axis k_n (unit arc-length).
    # n_hat = (-z_xi, y_xi) / h_xi  in (y, z) plane (90 deg CCW from t_hat)
    y_kn_w = -z_xi_w / h_xi_w
    z_kn_w =  y_xi_w / h_xi_w

    return dict(
        k_wall=k_w,
        y_xi=y_xi_w,    y_zeta=y_zeta_w,
        z_xi=z_xi_w,    z_zeta=z_zeta_w,
        h_xi=h_xi_w,    h_zeta=h_zeta_w,
        J=J_w,
        e_xi_dot_e_zeta=e_xi_dot_e_zeta_w,
        y_kn=y_kn_w,    z_kn=z_kn_w,
    )


# ============================================================================
#  Output writer
# ============================================================================
def write_wall_layers_dat(path: str, label: str,
                          k_layers: List[int], Nx: int, Ny: int,
                          x_arr: np.ndarray, y_2d: np.ndarray, z_2d: np.ndarray,
                          V_layers: np.ndarray, W_layers: np.ndarray,
                          u_tan: np.ndarray, u_norm: np.ndarray,
                          wall_metric: Dict[str, np.ndarray]) -> None:
    """Write Tecplot POINT format. K=len(k_layers), I=Nx, J=Ny.

    Arrays V_layers, W_layers, u_tan, u_norm have shape (n_layers, Ny, Nx).
    wall_metric: dict from metric_at_wall — contains 1D-in-j arrays of
    h_xi, J, e_xi_dot_e_zeta, y_kn, z_kn (all evaluated at the wall row).
    Those metric values are constant in i and k for a given j; they are
    repeated on every output row for self-containedness.
    """
    n_layers = len(k_layers)
    k_lo, k_hi = min(k_layers), max(k_layers)
    h_xi_w   = wall_metric["h_xi"]
    J_w      = wall_metric["J"]
    eXieZ_w  = wall_metric["e_xi_dot_e_zeta"]
    y_kn_w   = wall_metric["y_kn"]
    z_kn_w   = wall_metric["z_kn"]
    chunks: List[str] = []
    chunks.append(f'TITLE     = "Wall-layer u_tangent / u_normal + '
                  f'wall-row metric ({label}, k={k_lo}..{k_hi})"\n')
    chunks.append('VARIABLES = "i" "j" "k" "x" "y" "z" '
                  '"V_mean" "W_mean" "u_tangent" "u_normal" '
                  '"h_xi" "J" "e_xi.e_zeta" "y_kn" "z_kn"\n')
    chunks.append(f'ZONE T="{label}_k{k_lo}-{k_hi}", '
                  f'I={Nx}, J={Ny}, K={n_layers}, F=POINT\n')
    chunks.append('DT=(SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE '
                  'SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE)\n')

    # i-fast j-mid k-slow ordering matches Tecplot POINT format
    for n, k_act in enumerate(k_layers):
        for j in range(Ny):
            y_jk    = y_2d[k_act, j]
            z_jk    = z_2d[k_act, j]
            hxi_j   = h_xi_w[j]
            J_j     = J_w[j]
            eXZ_j   = eXieZ_w[j]
            ykn_j   = y_kn_w[j]
            zkn_j   = z_kn_w[j]
            for i in range(Nx):
                chunks.append(
                    f"{i:4d} {j:4d} {k_act:4d} "
                    f"{x_arr[i]:.15e} {y_jk:.15e} {z_jk:.15e} "
                    f"{V_layers[n, j, i]:.15e} {W_layers[n, j, i]:.15e} "
                    f"{u_tan[n, j, i]:.15e} {u_norm[n, j, i]:.15e} "
                    f"{hxi_j:.15e} {J_j:.15e} {eXZ_j:.15e} "
                    f"{ykn_j:.15e} {zkn_j:.15e}\n"
                )
    with open(path, "w") as f:
        f.writelines(chunks)


# ============================================================================
#  Filename helpers
# ============================================================================
def build_bottom_path(folder: str, re_tok: str, Nx: int, Ny: int) -> str:
    return os.path.join(folder, f"5.{re_tok}_utan_i{Nx}_j{Ny}_k0-6.dat")


def build_top_path(folder: str, re_tok: str, Nx: int, Ny: int, Nz: int) -> str:
    return os.path.join(folder, f"6.{re_tok}_utan_i{Nx}_j{Ny}_k{Nz-7}-{Nz-1}.dat")


# ============================================================================
#  Main
# ============================================================================
def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Compute wall-tangent / wall-normal velocities at the "
                    "7 layers nearest each wall, with metric FROZEN at the "
                    "wall (k=0 or k=NZ-1).")
    p.add_argument("--vtk", default=None,
                   help="input v2.vtk (default: auto-detect 1.*_v2.vtk)")
    p.add_argument("--dat", default=None,
                   help="input mesh dat (default: auto-detect 2.j*_*.dat)")
    p.add_argument("--variables-h", default=None,
                   help="path to variables.h (default: auto-detect)")
    p.add_argument("--Uref", type=float, default=None,
                   help="reference velocity (overrides variables.h)")
    args = p.parse_args(argv)

    folder = OUTPUT_DIR
    vtk_in = args.vtk or auto_detect_vtk(folder)
    dat_in = args.dat or auto_detect_dat(folder)

    # ---- Uref (for converting nondimensional VTK back to lattice units) ----
    var_h = args.variables_h or auto_detect_variables_h(INPUT_DIR)
    if args.Uref is not None:
        Uref = args.Uref
    else:
        consts = parse_header_constants(var_h) if var_h else {}
        Uref = find_const(consts, ["Uref", "U_ref"], var_h or "variables.h")
    print(f"Uref = {Uref:.6e}  (VTK stores V_mean = V_lattice / Uref)")
    print(f"input vtk: {vtk_in}")
    print(f"input dat: {dat_in}")

    # ---- [1] parse 2D mesh ----
    print("\n[1] parse 2D mesh ...")
    t0 = time.time()
    y_2d, z_2d, J, K = parse_tecplot_2d_mesh(dat_in)
    print(f"  shape (K, J) = ({K}, {J})  [K={K} normal, J={J} stream]")
    print(f"  ({time.time() - t0:.1f}s)")

    # ---- [2] wall-row metric (1D in j) — both walls ----
    # Bottom: used for STRICT-ORTHONORMAL projection of velocities.
    # Top:    metric carried into output for completeness, but velocity
    #         is dumped as raw V_mean / W_mean (no projection).
    print("\n[2] computing wall-row metric (1D in j, at k=0 and k=K-1) ...")
    t0 = time.time()
    bot_m = metric_at_wall(y_2d, z_2d, "bottom")
    top_m = metric_at_wall(y_2d, z_2d, "top")

    def _print_m(label, m):
        print(f"  {label}:")
        print(f"    h_xi             range [{m['h_xi'].min():.4e}, "
              f"{m['h_xi'].max():.4e}]")
        print(f"    J                range [{m['J'].min():.4e}, "
              f"{m['J'].max():.4e}]")
        print(f"    e_xi.e_zeta      range [{m['e_xi_dot_e_zeta'].min():+.4e}, "
              f"{m['e_xi_dot_e_zeta'].max():+.4e}]   "
              f"(= 0 iff mesh orthogonal)")
        # mesh-orthogonality angle
        cos_th = m["e_xi_dot_e_zeta"] / (m["h_xi"] * m["h_zeta"] + 1e-30)
        print(f"    cos(theta_xi_z)  range [{cos_th.min():+.4e}, "
              f"{cos_th.max():+.4e}]   |max|={np.abs(cos_th).max():.4e}")

    _print_m("bottom (k=0)",   bot_m)
    _print_m(f"top    (k={K-1})", top_m)
    if (bot_m["J"] <= 0).any() or (top_m["J"] <= 0).any():
        print("  [WARN] some wall-row J <= 0", file=sys.stderr)
    else:
        print("  [OK] both wall rows: J > 0")
    print("  bottom: velocities will be projected onto STRICT-orthonormal frame "
          "(only y_xi, z_xi used)")
    print("  top:    velocity projection SKIPPED; u_tangent=V_mean, u_normal=W_mean")
    print(f"  ({time.time() - t0:.1f}s)")

    # ---- [3] read VTK V_mean, W_mean (full) ----
    print("\n[3] reading VTK V_mean, W_mean ...")
    t0 = time.time()
    Nx, Ny, Nz = parse_dimensions(vtk_in)
    print(f"  DIMENSIONS Nx={Nx} Ny={Ny} Nz={Nz}")
    if (J, K) != (Ny, Nz):
        print(f"  [error] dat (J, K) = ({J}, {K}) != "
              f"vtk (Ny, Nz) = ({Ny}, {Nz})", file=sys.stderr)
        sys.exit(1)
    sections = map_vtk_sections(vtk_in)
    n_total = Nx * Ny * Nz
    x_arr = read_x_array(vtk_in, sections, Nx)
    print(f"  x range [{x_arr.min():.4f}, {x_arr.max():.4f}]  "
          f"(span direction)")
    V_full = read_scalar_full(vtk_in, sections, "V_mean", n_total)
    W_full = read_scalar_full(vtk_in, sections, "W_mean", n_total)
    # VTK stores V_mean = V_lattice / Uref; restore to physical lattice velocity
    V_full *= Uref
    W_full *= Uref
    V_3d = V_full.reshape(Nz, Ny, Nx)    # [k, j, i]
    W_3d = W_full.reshape(Nz, Ny, Nx)
    print(f"  V (lattice) range [{V_3d.min():.6e}, {V_3d.max():.6e}]  (×Uref)")
    print(f"  W (lattice) range [{W_3d.min():.6e}, {W_3d.max():.6e}]  (×Uref)")
    print(f"  ({time.time() - t0:.1f}s)")

    # ---- [4] slice 7-layer slabs near each wall ----
    bot_layers = list(range(0, 7))                  # [0,1,2,3,4,5,6]
    top_layers = list(range(Nz - 7, Nz))            # [NZ-7, ..., NZ-1]
    V_bot = V_3d[bot_layers]                        # shape (7, Ny, Nx)
    W_bot = W_3d[bot_layers]
    V_top = V_3d[top_layers]
    W_top = W_3d[top_layers]

    # ---- [5] compute u_tangent, u_normal with STRICT-ORTHONORMAL frame ----
    def wall_project(V, W, m):
        """Project (V, W) onto the wall-aligned orthonormal frame at k=0.

        Uses ONLY y_xi, z_xi at the wall (no y_zeta, z_zeta, J).
        Frame:
            t_hat = ( y_xi,  z_xi) / h_xi    (wall tangent)
            n_hat = (-z_xi,  y_xi) / h_xi    (wall normal, 90 deg CCW)
            t_hat . n_hat = 0  by construction.

        Output:
            u_tangent(k,j,i) = V . t_hat = ( V*y_xi + W*z_xi) / h_xi
            u_normal (k,j,i) = V . n_hat = (-V*z_xi + W*y_xi) / h_xi
        """
        yxi = m["y_xi"][None, :, None]      # broadcast (1, Ny, 1)
        zxi = m["z_xi"][None, :, None]
        hxi = m["h_xi"][None, :, None]
        u_tan  = ( V * yxi + W * zxi) / hxi
        u_norm = (-V * zxi + W * yxi) / hxi
        return u_tan, u_norm

    print("\n[4] computing u_tangent, u_normal ...")
    print("  bottom: STRICT-orthonormal projection (uses only y_xi, z_xi at k=0)")
    print("  top:    raw V_mean / W_mean (no projection; flat wall)")
    t0 = time.time()
    u_tan_bot, u_norm_bot = wall_project(V_bot, W_bot, bot_m)
    # Top: flat wall, projection skipped — just identity
    u_tan_top  = V_top.copy()
    u_norm_top = W_top.copy()
    # Strict-orthogonality sanity at the bottom: V**2 + W**2 == u_tan**2 + u_norm**2
    # (rotation preserves norm).
    norm_in   = V_bot ** 2 + W_bot ** 2
    norm_out  = u_tan_bot ** 2 + u_norm_bot ** 2
    norm_diff = float(np.max(np.abs(norm_in - norm_out)))
    print(f"  bottom orthonormality: max |V^2+W^2 - u_t^2-u_n^2| = "
          f"{norm_diff:.2e}   (rotation must preserve norm; expect ~1e-15)")
    print(f"  bottom (k=0..6) u_tan  range [{u_tan_bot.min():+.4f}, "
          f"{u_tan_bot.max():+.4f}]")
    print(f"  bottom (k=0..6) u_norm range [{u_norm_bot.min():+.4f}, "
          f"{u_norm_bot.max():+.4f}]")
    print(f"  top    (k={Nz-7}..{Nz-1}) u_tan  range "
          f"[{u_tan_top.min():+.4f}, {u_tan_top.max():+.4f}]   "
          f"(= V_mean)")
    print(f"  top    (k={Nz-7}..{Nz-1}) u_norm range "
          f"[{u_norm_top.min():+.4f}, {u_norm_top.max():+.4f}]   "
          f"(= W_mean)")
    # No-slip sanity at the actual wall layers
    print(f"  no-slip @ k=0:    |u_tan|.max  = {np.abs(u_tan_bot[0]).max():.3e}, "
          f"|u_norm|.max = {np.abs(u_norm_bot[0]).max():.3e}")
    print(f"  no-slip @ k={Nz-1}: |u_tan|.max  = "
          f"{np.abs(u_tan_top[-1]).max():.3e}, "
          f"|u_norm|.max = {np.abs(u_norm_top[-1]).max():.3e}")
    print(f"  ({time.time() - t0:.1f}s)")

    # ---- [6] write outputs ----
    re_tok = parse_re_token(os.path.basename(vtk_in))
    bot_path = build_bottom_path(folder, re_tok, Nx, Ny)
    top_path = build_top_path   (folder, re_tok, Nx, Ny, Nz)

    print(f"\n[5] writing bottom output -> {bot_path}")
    t0 = time.time()
    write_wall_layers_dat(bot_path, "bottom_wall_strict_orth",
                          bot_layers, Nx, Ny, x_arr, y_2d, z_2d,
                          V_bot, W_bot, u_tan_bot, u_norm_bot,
                          wall_metric=bot_m)
    print(f"  wrote {os.path.getsize(bot_path):,} bytes  "
          f"({time.time() - t0:.1f}s)")

    print(f"\n[6] writing top output -> {top_path}")
    print("    (top wall: u_tangent = V_mean, u_normal = W_mean — "
          "no projection)")
    t0 = time.time()
    write_wall_layers_dat(top_path, "top_wall_V_W_direct",
                          top_layers, Nx, Ny, x_arr, y_2d, z_2d,
                          V_top, W_top, u_tan_top, u_norm_top,
                          wall_metric=top_m)
    print(f"  wrote {os.path.getsize(top_path):,} bytes  "
          f"({time.time() - t0:.1f}s)")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
