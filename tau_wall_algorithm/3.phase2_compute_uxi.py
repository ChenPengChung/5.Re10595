# -*- coding: utf-8 -*-
"""
3.phase2_compute_uxi.py
=======================

Compute the time-mean **physical components** of velocity in the unit
curvilinear basis (decomposing V_bar in the orthonormal frame
{e_xi_hat, e_zeta_hat}):

    V_bar  =  u_xi * e_xi_hat  +  u_zeta * e_zeta_hat

where the basis coefficients are derived from solving the linear system

    h_xi  * e_xi_hat   =  y_xi  * e_y_hat  +  z_xi  * e_z_hat
    h_zeta * e_zeta_hat =  y_zeta * e_y_hat +  z_zeta * e_z_hat

for e_y_hat, e_z_hat and substituting into V_bar = V*e_y_hat + W*e_z_hat:

    u_xi   =  h_xi   * (V * z_zeta - W * y_zeta) / J
    u_zeta =  h_zeta * (W * y_xi   - V * z_xi  ) / J

with Lame coefficients and Jacobian determinant

    h_xi   = sqrt(y_xi^2  + z_xi^2)
    h_zeta = sqrt(y_zeta^2 + z_zeta^2)
    J      = y_xi * z_zeta - y_zeta * z_xi

(NOTE: u_xi and u_zeta defined this way are the "basis-coefficient"
components in the unit-basis decomposition.  For ORTHOGONAL mesh the
basis is orthonormal, so these coincide with the physical projections
V_bar.e_xi_hat and V_bar.e_zeta_hat; for non-orthogonal mesh they
differ from the projections by cos(theta_xi_zeta) cross-terms.)

Project convention:
    i, x, u, U  -> span-wise
    j, y, v, V  -> stream-wise
    k, z, w, W  -> wall-normal
and xi = j (stream), zeta = k (wall-normal).

Inputs (auto-detected, must be unique):
    1.*_v2.vtk       renamed 3D ASCII VTK from phase1_transvtk
    2.j*_*.dat       h-normalised 2D mesh from phase1_transdat

Outputs:
    3.Re<XXX>_uxi_<Nx>x<Ny>x<Nz>.vtk          ASCII VTK with grid + V_mean +
                                              W_mean + u_xi + u_zeta scalars.
    4.Re<XXX>_inverseJacobian_j<J>_k<K>.dat   plain-text dump of all metric
                                              terms (incl. h_xi, h_zeta).

Numerics
--------
Forward Jacobian computed from the 2D mesh y(j,k), z(j,k) via 6th-order
finite differences (ported from metric_terms.h):
    j-direction (stream)  : 6th-order central with periodic wrap
                            (with period offset L on y, none on z)
    k-direction (normal)  : 6th-order Fornberg adaptive (forward at k=0..2,
                            central at k=3..K-4, backward at k=K-3..K-1)
    -> uniform 6th order everywhere; no 5th-order buffer.

Sign convention (matching metric_terms.h):
    J_2D    = y_xi * z_zeta - y_zeta * z_xi
    xi_y    =  z_zeta / J_2D       xi_z   = -y_zeta / J_2D
    zeta_y  = -z_xi   / J_2D       zeta_z =  y_xi   / J_2D

Sanity checks: J_2D > 0, J * J^-1 = I (max err < 1e-10), zeta_z > 0.
"""

from __future__ import annotations
import argparse, os, re, sys, time
from typing import Dict, List, Tuple
import numpy as np

# ---- I/O directory bootstrap (matches the Input/Output/Reference layout) -
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "Reference"))
OUTPUT_DIR = os.path.join(_HERE, "Output")
os.makedirs(OUTPUT_DIR, exist_ok=True)
# --------------------------------------------------------------------------

from phase1_common import (
    FD6_COEFF,
    d_dj_periodic_2d as d_dj_periodic,
    d_dk_fornberg,
    parse_tecplot_2d_mesh,
    map_vtk_sections,
    parse_dimensions,
    read_scalar_full as read_scalar_values,
    find_unique_matching,
    parse_re_token,
    _detect_vtk_format,
)


# ============================================================================
#  File discovery
# ============================================================================
_VTK_IN_RE  = re.compile(r"^1\..+_v2\.vtk$",  re.IGNORECASE)
_DAT_IN_RE  = re.compile(r"^2\.j.+\.dat$",    re.IGNORECASE)


def auto_detect_vtk(folder: str = ".") -> str:
    return find_unique_matching(folder, "*.vtk", _VTK_IN_RE)


def auto_detect_dat(folder: str = ".") -> str:
    return find_unique_matching(folder, "*.dat", _DAT_IN_RE)


# ============================================================================
#  VTK output (byte-copy + new u_xi block)
# ============================================================================
def _copy_bytes(fin, fout, n: int) -> None:
    BUF = 8 * 1024 * 1024
    while n > 0:
        chunk = fin.read(min(BUF, n))
        if not chunk:
            raise EOFError("unexpected EOF during byte copy")
        fout.write(chunk)
        n -= len(chunk)


def _write_floats_ascii(fout, arr: np.ndarray, fmt: str = '%.15g') -> None:
    """Write 1D float array as ASCII, one value per line.  Batched format."""
    BATCH = 200_000
    n = len(arr)
    for i in range(0, n, BATCH):
        chunk = arr[i:i + BATCH]
        text = '\n'.join(fmt % v for v in chunk) + '\n'
        fout.write(text.encode('ascii'))


def write_uxi_vtk(out_path: str, in_path: str,
                  sections: Dict[str, Tuple[int, int]],
                  u_xi_flat: np.ndarray,
                  u_zeta_flat: np.ndarray,
                  keep_scalars: List[str]) -> None:
    """Build output VTK by byte-copying header + selected scalars from input,
    then appending newly-computed u_xi and u_zeta scalars."""
    sorted_keys = sorted(sections.keys(), key=lambda k: sections[k][0])
    section_end: Dict[str, int] = {}
    for i, key in enumerate(sorted_keys):
        section_end[key] = (sections[sorted_keys[i + 1]][0]
                            if i + 1 < len(sorted_keys)
                            else os.path.getsize(in_path))

    is_binary = (_detect_vtk_format(in_path) == "BINARY")

    pd_start, pd_end = sections['POINT_DATA']
    with open(in_path, 'rb') as fin, open(out_path, 'wb') as fout:
        # 1. Geometry: bytes [0, end of POINT_DATA line)
        fin.seek(0)
        _copy_bytes(fin, fout, pd_end)
        # 2. Each kept SCALARS section verbatim
        for name in keep_scalars:
            key   = f"SCALARS:{name}"
            start = sections[key][0]
            end   = section_end[key]
            fin.seek(start)
            _copy_bytes(fin, fout, end - start)
        # 3. New u_xi block
        fout.write(b"SCALARS u_xi double 1\n")
        fout.write(b"LOOKUP_TABLE default\n")
        if is_binary:
            fout.write(u_xi_flat.astype('>f8').tobytes())
        else:
            _write_floats_ascii(fout, u_xi_flat)
        # 4. New u_zeta block
        fout.write(b"SCALARS u_zeta double 1\n")
        fout.write(b"LOOKUP_TABLE default\n")
        if is_binary:
            fout.write(u_zeta_flat.astype('>f8').tobytes())
        else:
            _write_floats_ascii(fout, u_zeta_flat)


# ============================================================================
#  Inverse-Jacobian dat output
# ============================================================================
def write_metric_dat(path: str, y_2d: np.ndarray, z_2d: np.ndarray,
                     y_xi: np.ndarray, y_zeta: np.ndarray,
                     z_xi: np.ndarray, z_zeta: np.ndarray,
                     h_xi: np.ndarray, h_zeta: np.ndarray,
                     J_2D: np.ndarray,
                     xi_y: np.ndarray, xi_z: np.ndarray,
                     zeta_y: np.ndarray, zeta_z: np.ndarray) -> None:
    K, J = y_2d.shape
    lines = ["# j  k  y  z  y_xi  y_zeta  z_xi  z_zeta  "
             "h_xi  h_zeta  J_2D  xi_y  xi_z  zeta_y  zeta_z\n"]
    for k in range(K):
        for j in range(J):
            lines.append(
                f"{j:4d} {k:4d} "
                f"{y_2d[k,j]:14.8f} {z_2d[k,j]:14.8f} "
                f"{y_xi[k,j]:14.6e} {y_zeta[k,j]:14.6e} "
                f"{z_xi[k,j]:14.6e} {z_zeta[k,j]:14.6e} "
                f"{h_xi[k,j]:14.6e} {h_zeta[k,j]:14.6e} "
                f"{J_2D[k,j]:14.6e} "
                f"{xi_y[k,j]:14.6e} {xi_z[k,j]:14.6e} "
                f"{zeta_y[k,j]:14.6e} {zeta_z[k,j]:14.6e}\n")
    with open(path, 'w') as f:
        f.writelines(lines)


# ============================================================================
#  Filename helpers
# ============================================================================
def build_uxi_vtk_path(folder: str, re_tok: str,
                       Nx: int, Ny: int, Nz: int) -> str:
    return os.path.join(folder, f"3.{re_tok}_uxi_{Nx}x{Ny}x{Nz}.vtk")


def build_metric_dat_path(folder: str, re_tok: str, J: int, K: int) -> str:
    return os.path.join(folder, f"4.{re_tok}_inverseJacobian_j{J}_k{K}.dat")


# ============================================================================
#  Main
# ============================================================================
def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Compute physical basis-coefficient velocities "
                    "u_xi = h_xi*(V*z_zeta - W*y_zeta)/J  and "
                    "u_zeta = h_zeta*(W*y_xi - V*z_xi)/J, "
                    "and emit them as new scalars in a new VTK.")
    p.add_argument("--vtk", default=None,
                   help="input v2.vtk (default: auto-detect 1.*_v2.vtk in cwd)")
    p.add_argument("--dat", default=None,
                   help="input mesh dat (default: auto-detect 2.j*_*.dat in cwd)")
    args = p.parse_args(argv)

    folder = OUTPUT_DIR
    vtk_in = args.vtk or auto_detect_vtk(folder)
    dat_in = args.dat or auto_detect_dat(folder)
    print(f"input vtk: {vtk_in}")
    print(f"input dat: {dat_in}")

    # ---- [1] parse 2D mesh ----
    print("\n[1] parse 2D mesh ...")
    t0 = time.time()
    y_2d, z_2d, J, K = parse_tecplot_2d_mesh(dat_in)
    print(f"  shape (K, J) = ({K}, {J})  [K={K} normal, J={J} stream]")
    print(f"  y range [{y_2d.min():.4f}, {y_2d.max():.4f}]  (h-units, stream)")
    print(f"  z range [{z_2d.min():.4f}, {z_2d.max():.4f}]  (h-units, normal)")
    print(f"  ({time.time() - t0:.1f}s)")

    # ---- [2] forward Jacobian ----
    print("\n[2] forward Jacobian (6th-order central + Fornberg adaptive) ...")
    t0 = time.time()
    L_stream = float(y_2d[0, -1] - y_2d[0, 0])    # period length from mesh
    print(f"  stream period L = {L_stream:.6f}  (used as offset for y-coord wrap)")
    y_xi   = d_dj_periodic(y_2d, period_offset=L_stream)   # y ramps 0->L
    y_zeta = d_dk_fornberg(y_2d)
    z_xi   = d_dj_periodic(z_2d, period_offset=0.0)         # z is truly periodic
    z_zeta = d_dk_fornberg(z_2d)
    print(f"  y_xi  : range [{y_xi.min():.4e}, {y_xi.max():.4e}]")
    print(f"  y_zeta: range [{y_zeta.min():.4e}, {y_zeta.max():.4e}]")
    print(f"  z_xi  : range [{z_xi.min():.4e}, {z_xi.max():.4e}]")
    print(f"  z_zeta: range [{z_zeta.min():.4e}, {z_zeta.max():.4e}]")
    print(f"  ({time.time() - t0:.1f}s)")

    # ---- [3] inverse Jacobian + sanity ----
    print("\n[3] inverse Jacobian + sanity checks ...")
    t0 = time.time()
    J_2D = y_xi * z_zeta - y_zeta * z_xi
    if (J_2D <= 0).any():
        nbad = int((J_2D <= 0).sum())
        print(f"  [WARN] J_2D <= 0 at {nbad} points (min={J_2D.min():.4e})")
    else:
        print(f"  [OK]   J_2D > 0 everywhere "
              f"(min={J_2D.min():.4e}, max={J_2D.max():.4e})")

    invJ   = 1.0 / J_2D
    xi_y   =  z_zeta * invJ
    xi_z   = -y_zeta * invJ
    zeta_y = -z_xi   * invJ
    zeta_z =  y_xi   * invJ

    # Lame coefficients (magnitudes of covariant basis vectors)
    h_xi   = np.sqrt(y_xi  ** 2 + z_xi  ** 2)
    h_zeta = np.sqrt(y_zeta ** 2 + z_zeta ** 2)

    e11 = y_xi * xi_y + y_zeta * zeta_y - 1.0
    e12 = y_xi * xi_z + y_zeta * zeta_z
    e21 = z_xi * xi_y + z_zeta * zeta_y
    e22 = z_xi * xi_z + z_zeta * zeta_z - 1.0
    max_err = float(np.max(np.abs(e11) + np.abs(e12) + np.abs(e21) + np.abs(e22)))
    status  = "OK" if max_err < 1e-9 else "FAIL"
    print(f"  [{status}] J * J^-1 = I  (max identity err = {max_err:.4e})")

    if (zeta_z <= 0).any():
        nbad = int((zeta_z <= 0).sum())
        print(f"  [WARN] zeta_z <= 0 at {nbad} points (min={zeta_z.min():.4e})")
    else:
        print(f"  [OK]   zeta_z > 0 everywhere (min={zeta_z.min():.4e})")
    print(f"  h_xi   : range [{h_xi.min():.4e}, {h_xi.max():.4e}]")
    print(f"  h_zeta : range [{h_zeta.min():.4e}, {h_zeta.max():.4e}]")
    print(f"  xi_y   : range [{xi_y.min():.4e}, {xi_y.max():.4e}]")
    print(f"  xi_z   : range [{xi_z.min():.4e}, {xi_z.max():.4e}]")

    # Orthogonality diagnostic at wall (k=0)
    cos_th = (y_xi[0] * y_zeta[0] + z_xi[0] * z_zeta[0]) / (h_xi[0] * h_zeta[0])
    print(f"  wall (k=0) cos(theta_xi_zeta): "
          f"min={cos_th.min():.4e}, max={cos_th.max():.4e}, "
          f"|max|={np.abs(cos_th).max():.4e}")
    print(f"  ({time.time() - t0:.1f}s)")

    # ---- [4] write inverse-Jacobian dat ----
    re_tok      = parse_re_token(os.path.basename(vtk_in))
    metric_path = build_metric_dat_path(folder, re_tok, J, K)
    print(f"\n[4] writing inverse-Jacobian dat -> {metric_path}")
    t0 = time.time()
    write_metric_dat(metric_path, y_2d, z_2d,
                     y_xi, y_zeta, z_xi, z_zeta, h_xi, h_zeta, J_2D,
                     xi_y, xi_z, zeta_y, zeta_z)
    print(f"  wrote {os.path.getsize(metric_path):,} bytes  "
          f"({time.time() - t0:.1f}s)")

    # ---- [5] scan VTK + read V_mean / W_mean ----
    print("\n[5] scanning VTK + reading V_mean, W_mean ...")
    t0 = time.time()
    Nx, Ny, Nz = parse_dimensions(vtk_in)
    print(f"  DIMENSIONS Nx={Nx} Ny={Ny} Nz={Nz}  "
          f"(= span x stream x normal)")
    if (J, K) != (Ny, Nz):
        print(f"  [error] dat (J, K) = ({J}, {K}) != "
              f"vtk (Ny, Nz) = ({Ny}, {Nz})", file=sys.stderr)
        sys.exit(1)
    n_points = Nx * Ny * Nz
    print(f"  total points = {n_points:,}")
    sections = map_vtk_sections(vtk_in)
    print(f"  found {len(sections)} sections in VTK")
    V_mean_flat = read_scalar_values(vtk_in, sections, "V_mean", n_points)
    W_mean_flat = read_scalar_values(vtk_in, sections, "W_mean", n_points)
    print(f"  V_mean range [{V_mean_flat.min():.4f}, {V_mean_flat.max():.4f}]")
    print(f"  W_mean range [{W_mean_flat.min():.4f}, {W_mean_flat.max():.4f}]")
    print(f"  ({time.time() - t0:.1f}s)")

    # ---- [6] compute u_xi, u_zeta (Lame-weighted physical contravariant) ----
    # Non-orthogonal curvilinear coordinates: project V_bar onto curvilinear
    # axes weighted by the Lame coefficients h_xi, h_zeta.
    #     u_xi   = h_xi   * (V*z_zeta - W*y_zeta) / J  =  h_xi   * V . grad(xi)
    #     u_zeta = h_zeta * (W*y_xi   - V*z_xi  ) / J  =  h_zeta * V . grad(zeta)
    # For orthogonal mesh these reduce to the usual physical projections;
    # for non-orthogonal mesh they differ from V . e_xi_hat by cos(theta_xi_zeta)
    # cross-coupling, which is the desired "physical contravariant" form.
    print("\n[6] computing u_xi = h_xi * (V*z_zeta - W*y_zeta) / J, "
          "u_zeta = h_zeta * (W*y_xi - V*z_xi) / J ...")
    t0 = time.time()
    V_mean = V_mean_flat.reshape(Nz, Ny, Nx)   # [k, j, i]
    W_mean = W_mean_flat.reshape(Nz, Ny, Nx)
    h_xi_b   = h_xi  [:, :, None]
    h_zeta_b = h_zeta[:, :, None]
    u_xi   = h_xi_b   * (V_mean * xi_y  [:, :, None] + W_mean * xi_z  [:, :, None])
    u_zeta = h_zeta_b * (V_mean * zeta_y[:, :, None] + W_mean * zeta_z[:, :, None])
    u_xi_flat   = u_xi.reshape(-1)             # i-fast, j-mid, k-slow (C-order)
    u_zeta_flat = u_zeta.reshape(-1)
    print(f"  u_xi   range [{u_xi.min():.4f}, {u_xi.max():.4f}]")
    print(f"  u_zeta range [{u_zeta.min():.4f}, {u_zeta.max():.4f}]")
    print(f"  ({time.time() - t0:.1f}s)")

    # ---- [7] write output VTK ----
    out_path = build_uxi_vtk_path(folder, re_tok, Nx, Ny, Nz)
    print(f"\n[7] writing output VTK -> {out_path}")
    t0 = time.time()
    write_uxi_vtk(out_path, vtk_in, sections, u_xi_flat, u_zeta_flat,
                  keep_scalars=["V_mean", "W_mean"])
    print(f"  wrote {os.path.getsize(out_path):,} bytes  "
          f"({time.time() - t0:.1f}s)")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
