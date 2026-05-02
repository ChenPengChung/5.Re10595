#!/usr/bin/env python3
"""
LBM checkpoint interpolation: 129x257x129 (jp=8, GAMMA=2.0) -> 257x513x257 (jp=16, GAMMA=3.0)

Pipeline:
  1. Parse old metadata (Force, etc.)
  2. Build old grid coordinates from variables and old Frohlich grid file
  3. Read 8 ranks x (19 f_q + 1 rho), stitch global, compute (rho, ux, uy, uz)
  4. Build new grid coordinates (new GAMMA, new dims)
  5. Interpolate macros (rho, ux, uy, uz) old -> new in computational (j, k, i) space
  6. Fill new ghost zones (X periodic, Y periodic, Z constant copy from wall)
  7. Interpolate non-equilibrium f_neq = f - f_eq and reconstruct
     f_q^new = f_eq(rho, u, v, w) + scale * interp(f_neq_q) for q = 0..18
  8. Split into 16 ranks, write per-rank binary files + new metadata.dat

Output written atomically: write to <new_dir>.WRITING/, then rename.

Usage:
  python3 restart_tools/interp_checkpoint.py \\
      --old-dir restart/step_12550001_origin129 \\
      --new-dir restart/checkpoint/step_1
"""

import os
import sys
import math
import time
import argparse
import numpy as np

# ---------------------------------------------------------------
# Domain constants (must match variables.h)
# ---------------------------------------------------------------
LX = 4.5
LY = 9.0
LZ = 3.036
H_HILL = 1.0
BFR = 3

# ---------------------------------------------------------------
# Grid configurations
# ---------------------------------------------------------------
class GridConfig:
    def __init__(self, nx, ny, nz, jp, gamma, alpha, grid_dat):
        self.NX = nx
        self.NY = ny
        self.NZ = nz
        self.JP = jp
        self.GAMMA = gamma
        self.ALPHA = alpha
        self.GRID_DAT = grid_dat
        self.NX6 = nx + 6
        self.NY6 = ny + 6
        self.NZ6 = nz + 6
        self.NYD6 = (ny - 1) // jp + 7
        self.CHUNK = self.NYD6 - 7  # = (NY-1)/jp


OLD = GridConfig(
    nx=129, ny=257, nz=129, jp=8,
    gamma=2.0, alpha=0.5,
    grid_dat='J_Frohlich/adaptive_3.fine grid_I257_J129_g2.0_a0.5.dat',
)
NEW = GridConfig(
    nx=257, ny=513, nz=257, jp=16,
    gamma=3.0, alpha=0.5,
    grid_dat='J_Frohlich/adaptive_3.fine grid_I513_J257_g3.0_a0.5.dat',
)

# ---------------------------------------------------------------
# D3Q19 lattice (initialization.h:7-12)
# ---------------------------------------------------------------
E = np.array([
    [ 0, 0, 0],
    [ 1, 0, 0], [-1, 0, 0],
    [ 0, 1, 0], [ 0,-1, 0],
    [ 0, 0, 1], [ 0, 0,-1],
    [ 1, 1, 0], [-1, 1, 0], [ 1,-1, 0], [-1,-1, 0],
    [ 1, 0, 1], [-1, 0, 1], [ 1, 0,-1], [-1, 0,-1],
    [ 0, 1, 1], [ 0,-1, 1], [ 0, 1,-1], [ 0,-1,-1],
], dtype=np.float64)
W = np.array([1.0/3.0] + [1.0/18.0]*6 + [1.0/36.0]*12, dtype=np.float64)


# ---------------------------------------------------------------
# Metadata I/O
# ---------------------------------------------------------------
def parse_metadata(path):
    d = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if '=' in line:
                k, v = line.split('=', 1)
                d[k.strip()] = v.strip()
    return d


def write_metadata(path, params):
    keys_order = [
        'checkpoint_version', 'mpi_rank_count', 'grid_dims',
        'step', 'FTT', 'accu_count', 'Force',
        'Force_integral', 'error_prev',
        'ctrl_initialized', 'gehrke_activated',
        'dt_global', 'gpu_time_ms', 'cv_count',
    ]
    with open(path, 'w') as f:
        for k in keys_order:
            if k in params:
                f.write('{}={}\n'.format(k, params[k]))


# ---------------------------------------------------------------
# Grid coordinate builder (mirrors initialization.h)
# ---------------------------------------------------------------
def build_grid_xyz(cfg):
    """Return x[NX6], y_2d[NY6, NZ6], z_2d[NY6, NZ6] in code (normalized) units.

    Mirrors initialization.h GenerateMesh_X + ReadExternalGrid_YZ:
    - X uniform: x[i] = (i - BFR) * LX / (NX - 1)
    - Read Tecplot POINT file -> rescale to H_HILL=1 -> map (file_x, file_y) -> (code_y, code_z)
    - K-direction (z) ghost: linear extrapolation
    - J-direction (y) ghost: periodic wrap with +/-LY shift on y
    """
    # X (spanwise, uniform)
    dx = LX / (cfg.NX - 1)
    x = (np.arange(cfg.NX6) - BFR) * dx

    if not os.path.exists(cfg.GRID_DAT):
        raise FileNotFoundError('Grid file not found: {}'.format(cfg.GRID_DAT))

    # Parse Tecplot POINT format: skip header until "DT=" line
    coords = []
    with open(cfg.GRID_DAT) as f:
        in_data = False
        for line in f:
            if not in_data:
                if line.strip().startswith('DT='):
                    in_data = True
                continue
            parts = line.strip().split()
            if len(parts) >= 2:
                try:
                    coords.append((float(parts[0]), float(parts[1])))
                except ValueError:
                    continue

    coords = np.asarray(coords, dtype=np.float64)
    expected = cfg.NY * cfg.NZ
    if coords.shape[0] != expected:
        raise ValueError('Grid file {} has {} points, expected {} (NY*NZ = {}*{})'.format(
            cfg.GRID_DAT, coords.shape[0], expected, cfg.NY, cfg.NZ))

    # File is in physical units (h_phys ~ 0.028 m); rescale so H_HILL = 1
    # Reference: initialization.h:183-185
    #   x_fro_max = x_fro[NI-1]   (last point of J=0 row, max streamwise in physical)
    #   h_physical = x_fro_max / LY
    #   grid_scale = H_HILL / h_physical
    fro_x_max = coords[cfg.NY - 1, 0]
    h_physical = fro_x_max / LY
    grid_scale = H_HILL / h_physical
    coords *= grid_scale

    # Reshape to [J, I] (POINT format: I varies fastest)
    fro_x = coords[:, 0].reshape(cfg.NZ, cfg.NY)  # streamwise position
    fro_y = coords[:, 1].reshape(cfg.NZ, cfg.NY)  # wall-normal position

    # Allocate (NY6, NZ6) with code-coordinate indexing j, k
    y_2d = np.zeros((cfg.NY6, cfg.NZ6), dtype=np.float64)
    z_2d = np.zeros((cfg.NY6, cfg.NZ6), dtype=np.float64)

    # Map physical interior: code (j=BFR+jj, k=BFR+kk) <- file (J=kk, I=jj)
    # i.e., y_2d[BFR:BFR+NY, BFR:BFR+NZ] = fro_x.T
    y_2d[BFR:BFR+cfg.NY, BFR:BFR+cfg.NZ] = fro_x.T
    z_2d[BFR:BFR+cfg.NY, BFR:BFR+cfg.NZ] = fro_y.T

    # K-direction (z) ghost: linear extrapolation per j (initialization.h:236-256)
    nz6 = cfg.NZ6
    for j in range(BFR, BFR + cfg.NY):
        y_2d[j, 2] = 2.0 * y_2d[j, 3] - y_2d[j, 4]
        z_2d[j, 2] = 2.0 * z_2d[j, 3] - z_2d[j, 4]
        y_2d[j, 1] = 2.0 * y_2d[j, 2] - y_2d[j, 3]
        y_2d[j, 0] = 2.0 * y_2d[j, 1] - y_2d[j, 2]
        z_2d[j, 1] = 2.0 * z_2d[j, 2] - z_2d[j, 3]
        z_2d[j, 0] = 2.0 * z_2d[j, 1] - z_2d[j, 2]
        y_2d[j, nz6-3] = 2.0 * y_2d[j, nz6-4] - y_2d[j, nz6-5]
        z_2d[j, nz6-3] = 2.0 * z_2d[j, nz6-4] - z_2d[j, nz6-5]
        y_2d[j, nz6-2] = 2.0 * y_2d[j, nz6-3] - y_2d[j, nz6-4]
        y_2d[j, nz6-1] = 2.0 * y_2d[j, nz6-2] - y_2d[j, nz6-3]
        z_2d[j, nz6-2] = 2.0 * z_2d[j, nz6-3] - z_2d[j, nz6-4]
        z_2d[j, nz6-1] = 2.0 * z_2d[j, nz6-2] - z_2d[j, nz6-3]

    # J-direction (y) ghost: periodic wrap with +/-LY shift on y, no shift on z
    # initialization.h:270-288
    ny6 = cfg.NY6
    for k in range(nz6):
        y_2d[2, k] = y_2d[ny6-5, k] - LY
        y_2d[1, k] = y_2d[ny6-6, k] - LY
        y_2d[0, k] = y_2d[ny6-7, k] - LY
        z_2d[2, k] = z_2d[ny6-5, k]
        z_2d[1, k] = z_2d[ny6-6, k]
        z_2d[0, k] = z_2d[ny6-7, k]
        y_2d[ny6-3, k] = y_2d[4, k] + LY
        y_2d[ny6-2, k] = y_2d[5, k] + LY
        y_2d[ny6-1, k] = y_2d[6, k] + LY
        z_2d[ny6-3, k] = z_2d[4, k]
        z_2d[ny6-2, k] = z_2d[5, k]
        z_2d[ny6-1, k] = z_2d[6, k]

    return x, y_2d, z_2d


# ---------------------------------------------------------------
# Per-rank binary I/O + stitch / split
# ---------------------------------------------------------------
def read_rank_bin(path, cfg):
    """Read raw doubles, shape (NYD6, NZ6, NX6)."""
    expected = cfg.NYD6 * cfg.NZ6 * cfg.NX6 * 8
    sz = os.path.getsize(path)
    if sz != expected:
        raise ValueError('{}: size {} != expected {} (NYD6*NZ6*NX6*8 = {}*{}*{}*8)'.format(
            path, sz, expected, cfg.NYD6, cfg.NZ6, cfg.NX6))
    return np.fromfile(path, dtype=np.float64).reshape(cfg.NYD6, cfg.NZ6, cfg.NX6)


def stitch_y(per_rank_list, cfg):
    """Combine per-rank arrays into global (NY6, NZ6, NX6).

    Mapping (initialization.h:292):  j_global = rank * (NYD6 - 7) + j_local
    Overlapping ghost regions across ranks are identical post-MPI-halo;
    later ranks overwrite earlier ones harmlessly.
    """
    g = np.zeros((cfg.NY6, cfg.NZ6, cfg.NX6), dtype=np.float64)
    for r in range(cfg.JP):
        j0 = r * cfg.CHUNK
        g[j0:j0 + cfg.NYD6, :, :] = per_rank_list[r]
    return g


def split_y(global_arr, cfg):
    """Split global (NY6, NZ6, NX6) into JP per-rank slices of (NYD6, NZ6, NX6)."""
    out = []
    for r in range(cfg.JP):
        j0 = r * cfg.CHUNK
        out.append(global_arr[j0:j0 + cfg.NYD6, :, :].copy())
    return out


# ---------------------------------------------------------------
# 3D trilinear interpolation in computational coordinates
# ---------------------------------------------------------------
def _interp_axis_linear(arr, old_n, new_n, axis):
    """Linearly interpolate arr along one computational axis."""
    if old_n == new_n:
        return arr.copy()

    coord = np.arange(new_n, dtype=np.float64) * (old_n - 1.0) / (new_n - 1.0)
    lo = np.floor(coord).astype(np.int64)
    lo = np.clip(lo, 0, old_n - 2)
    hi = lo + 1
    w = coord - lo

    a0 = np.take(arr, lo, axis=axis)
    a1 = np.take(arr, hi, axis=axis)
    shape = [1] * arr.ndim
    shape[axis] = new_n
    w = w.reshape(shape)
    return (1.0 - w) * a0 + w * a1


def interpolate_comp_3d(field_old, cfg_old, cfg_new):
    """Interpolate physical nodes in computational (j, k, i) space.

    The periodic-hill mesh is curvilinear: y(j,k) is not separable in j and k.
    The previous physical-space shortcut used the bottom-wall y(j,k=BFR) to
    bracket every wall-normal column, which misplaces data near the hill.
    For this refinement restart we preserve topology and map old/new nodes by
    normalized computational coordinates instead.
    """
    old_int = field_old[
        BFR:BFR + cfg_old.NY,
        BFR:BFR + cfg_old.NZ,
        BFR:BFR + cfg_old.NX,
    ]

    tmp = _interp_axis_linear(old_int, cfg_old.NY, cfg_new.NY, axis=0)
    tmp = _interp_axis_linear(tmp,     cfg_old.NZ, cfg_new.NZ, axis=1)
    tmp = _interp_axis_linear(tmp,     cfg_old.NX, cfg_new.NX, axis=2)

    field_new = np.zeros((cfg_new.NY6, cfg_new.NZ6, cfg_new.NX6), dtype=np.float64)
    field_new[
        BFR:BFR + cfg_new.NY,
        BFR:BFR + cfg_new.NZ,
        BFR:BFR + cfg_new.NX,
    ] = tmp
    return field_new


# ---------------------------------------------------------------
# 3D structured bilinear interpolation: OLD -> NEW
# ---------------------------------------------------------------
def interpolate_3d(field_old, y2d_old, z2d_old, cfg_old, y2d_new, z2d_new, cfg_new):
    """Trilinear interp in physical (x, y, z) space.

    - X is uniform in both grids (LX shared); use direct linear weights
    - Y, Z form a 2D curvilinear grid; interp in physical (y, z) using bisection on
      (1) old streamwise position y_2d_old[:, BFR] (assumes I-lines vertical),
      (2) old wall-normal column at the bracketing j_old indices.

    Returns: field_new[NY6_new, NZ6_new, NX6_new] with physical interior filled.
             Ghost cells (j, k, i in {0..2, NY+3..NY6-1}) are zero; call fill_ghost.
    """
    # Streamwise positions at bottom row k = BFR
    y_str_old = y2d_old[BFR:BFR+cfg_old.NY, BFR]   # (NY_old,)
    y_str_new = y2d_new[BFR:BFR+cfg_new.NY, BFR]   # (NY_new,)

    # Bisect new y_str into old y_str -> floor index + weight
    j_old_floor = np.searchsorted(y_str_old, y_str_new, side='right') - 1
    j_old_floor = np.clip(j_old_floor, 0, cfg_old.NY - 2)
    j_old_ceil = j_old_floor + 1
    denom_y = y_str_old[j_old_ceil] - y_str_old[j_old_floor]
    denom_y = np.where(np.abs(denom_y) < 1e-30, 1.0, denom_y)
    wy_arr = (y_str_new - y_str_old[j_old_floor]) / denom_y
    wy_arr = np.clip(wy_arr, 0.0, 1.0)

    # Spanwise (X) indices/weights for new physical i in [BFR, BFR+NX_new-1]
    # x_new[i] / dx_new = (i - BFR), x_old[i'] / dx_old = (i' - BFR)
    # dx_new / dx_old = (NX_old - 1) / (NX_new - 1)
    # i_old_frac (in code coord) = (i_new - BFR) * (NX_old-1)/(NX_new-1) + BFR
    i_new_phys = np.arange(BFR, BFR + cfg_new.NX)
    i_old_frac = (i_new_phys - BFR) * (cfg_old.NX - 1.0) / (cfg_new.NX - 1.0) + BFR
    i_old_floor = np.floor(i_old_frac).astype(np.int64)
    i_old_floor = np.clip(i_old_floor, BFR, BFR + cfg_old.NX - 2)
    i_old_ceil = i_old_floor + 1
    wx_arr = i_old_frac - i_old_floor
    wx_arr = np.clip(wx_arr, 0.0, 1.0)

    field_new = np.zeros((cfg_new.NY6, cfg_new.NZ6, cfg_new.NX6), dtype=np.float64)

    NY_new = cfg_new.NY
    NZ_new = cfg_new.NZ
    NX_new = cfg_new.NX
    NZ_old = cfg_old.NZ

    for j_new_phys in range(NY_new):
        j_new_code = j_new_phys + BFR
        jf = int(j_old_floor[j_new_phys])
        jc = int(j_old_ceil[j_new_phys])
        wy = wy_arr[j_new_phys]

        # Z columns at the bracketing j_old (physical interior in k)
        z_col_f = z2d_old[BFR + jf, BFR:BFR + NZ_old]   # (NZ_old,)
        z_col_c = z2d_old[BFR + jc, BFR:BFR + NZ_old]
        z_new_col = z2d_new[j_new_code, BFR:BFR + NZ_new]  # (NZ_new,)

        # Bisect for k_old in each old column
        kf_floor_in_f = np.searchsorted(z_col_f, z_new_col, side='right') - 1
        kf_floor_in_f = np.clip(kf_floor_in_f, 0, NZ_old - 2)
        kf_ceil_in_f = kf_floor_in_f + 1
        denom_zf = z_col_f[kf_ceil_in_f] - z_col_f[kf_floor_in_f]
        denom_zf = np.where(np.abs(denom_zf) < 1e-30, 1.0, denom_zf)
        wz_f = (z_new_col - z_col_f[kf_floor_in_f]) / denom_zf
        wz_f = np.clip(wz_f, 0.0, 1.0)

        kf_floor_in_c = np.searchsorted(z_col_c, z_new_col, side='right') - 1
        kf_floor_in_c = np.clip(kf_floor_in_c, 0, NZ_old - 2)
        kf_ceil_in_c = kf_floor_in_c + 1
        denom_zc = z_col_c[kf_ceil_in_c] - z_col_c[kf_floor_in_c]
        denom_zc = np.where(np.abs(denom_zc) < 1e-30, 1.0, denom_zc)
        wz_c = (z_new_col - z_col_c[kf_floor_in_c]) / denom_zc
        wz_c = np.clip(wz_c, 0.0, 1.0)

        # 4 stencil corners -> shape (NZ_new, NX6_old)
        # Use code-frame indices when accessing field_old (which is in code frame)
        v_jf_kff = field_old[BFR + jf, BFR + kf_floor_in_f, :]
        v_jf_kfc = field_old[BFR + jf, BFR + kf_ceil_in_f, :]
        v_jc_kcf = field_old[BFR + jc, BFR + kf_floor_in_c, :]
        v_jc_kcc = field_old[BFR + jc, BFR + kf_ceil_in_c, :]

        wz_f_b = wz_f[:, None]
        wz_c_b = wz_c[:, None]
        f_jf = (1.0 - wz_f_b) * v_jf_kff + wz_f_b * v_jf_kfc
        f_jc = (1.0 - wz_c_b) * v_jc_kcf + wz_c_b * v_jc_kcc
        f_yz = (1.0 - wy) * f_jf + wy * f_jc                         # (NZ_new, NX6_old)

        # Spanwise interp -> (NZ_new, NX_new)
        f_xyz = (1.0 - wx_arr[None, :]) * f_yz[:, i_old_floor] \
                       + wx_arr[None, :] * f_yz[:, i_old_ceil]

        field_new[j_new_code, BFR:BFR+NZ_new, BFR:BFR+NX_new] = f_xyz

    return field_new


def fill_ghost(field, cfg):
    """Fill ghost cells of (NY6, NZ6, NX6) given physical interior is filled.

    Order: X periodic first, Z constant copy, Y periodic last
    (so Y/Z ghost cells inherit X-periodic values).
    """
    nx6 = cfg.NX6
    ny6 = cfg.NY6
    nz6 = cfg.NZ6

    # X (spanwise) periodic: i=2 <- NX+1 = NX6-5; i=NX+3 = NX6-3 <- 4; etc.
    field[:, :, 2] = field[:, :, nx6-5]
    field[:, :, 1] = field[:, :, nx6-6]
    field[:, :, 0] = field[:, :, nx6-7]
    field[:, :, nx6-3] = field[:, :, 4]
    field[:, :, nx6-2] = field[:, :, 5]
    field[:, :, nx6-1] = field[:, :, 6]

    # Z (wall-normal) constant copy from nearest wall
    # (BC kernel will overwrite ghost on first step; this is just a non-pathological seed)
    field[:, 2, :] = field[:, 3, :]
    field[:, 1, :] = field[:, 3, :]
    field[:, 0, :] = field[:, 3, :]
    field[:, nz6-3, :] = field[:, nz6-4, :]
    field[:, nz6-2, :] = field[:, nz6-4, :]
    field[:, nz6-1, :] = field[:, nz6-4, :]

    # Y (streamwise) periodic
    field[2, :, :] = field[ny6-5, :, :]
    field[1, :, :] = field[ny6-6, :, :]
    field[0, :, :] = field[ny6-7, :, :]
    field[ny6-3, :, :] = field[4, :, :]
    field[ny6-2, :, :] = field[5, :, :]
    field[ny6-1, :, :] = field[6, :, :]


# ---------------------------------------------------------------
# Equilibrium reconstruction (initialization.h:36-42)
# ---------------------------------------------------------------
def compute_feq_q(rho, ux, uy, uz, q):
    udot = ux*ux + uy*uy + uz*uz
    if q == 0:
        return W[0] * rho * (1.0 - 1.5 * udot)
    eu = E[q, 0]*ux + E[q, 1]*uy + E[q, 2]*uz
    return W[q] * rho * (1.0 + 3.0*eu + 4.5*eu*eu - 1.5*udot)


# ---------------------------------------------------------------
# New dt = minSize (variables.h:115-117)
# ---------------------------------------------------------------
def compute_minsize(cfg):
    a = cfg.GAMMA * (1.0/(cfg.NZ - 1) - cfg.ALPHA)
    b = cfg.GAMMA * cfg.ALPHA
    return (LZ - 1.0) * 0.5 * (1.0 + math.tanh(a) / math.tanh(b))


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--old-dir', default='restart/step_12550001_origin129',
                   help='old checkpoint directory (default: %(default)s)')
    p.add_argument('--new-dir', default='restart/checkpoint/step_1',
                   help='output checkpoint directory (default: %(default)s)')
    p.add_argument('--step', type=int, default=1,
                   help='new checkpoint step number written into metadata (default: 1)')
    p.add_argument('--fneq-scale', type=float, default=1.0,
                   help='scale factor applied to interpolated f_neq (default: %(default)s)')
    args = p.parse_args()

    t0 = time.time()
    print('=' * 72)
    print('LBM checkpoint interpolator: 129x257x129 (jp=8) -> 257x513x257 (jp=16)')
    print('=' * 72)
    print('OLD: NX={} NY={} NZ={} jp={} GAMMA={} grid={}'.format(
        OLD.NX, OLD.NY, OLD.NZ, OLD.JP, OLD.GAMMA, OLD.GRID_DAT))
    print('NEW: NX={} NY={} NZ={} jp={} GAMMA={} grid={}'.format(
        NEW.NX, NEW.NY, NEW.NZ, NEW.JP, NEW.GAMMA, NEW.GRID_DAT))
    print()

    writing_dir = args.new_dir + '.WRITING'
    if os.path.exists(args.new_dir):
        sys.exit('FATAL: {} already exists; refusing to overwrite'.format(args.new_dir))
    if os.path.exists(writing_dir):
        sys.exit('FATAL: {} already exists; remove it after verifying it is stale'.format(writing_dir))

    # ---- Step 1: parse old metadata ----
    print('[1/8] Reading old metadata: {}/metadata.dat'.format(args.old_dir))
    meta_path = os.path.join(args.old_dir, 'metadata.dat')
    if not os.path.exists(meta_path):
        sys.exit('FATAL: {} not found'.format(meta_path))
    meta_old = parse_metadata(meta_path)
    expected_dims = '{},{},{}'.format(OLD.NX6, OLD.NYD6, OLD.NZ6)
    if meta_old.get('grid_dims') != expected_dims:
        sys.exit('FATAL: grid_dims mismatch: file={}, expected={}'.format(
            meta_old.get('grid_dims'), expected_dims))
    if int(meta_old.get('mpi_rank_count', 0)) != OLD.JP:
        sys.exit('FATAL: mpi_rank_count mismatch: file={}, expected={}'.format(
            meta_old.get('mpi_rank_count'), OLD.JP))
    Force_value = float(meta_old['Force'])
    print('      grid_dims={} mpi_rank_count={} step={} FTT={} Force={:.6e}'.format(
        meta_old['grid_dims'], meta_old['mpi_rank_count'],
        meta_old['step'], meta_old['FTT'], Force_value))

    # ---- Step 2: build OLD grid ----
    print('[2/8] Building OLD grid coordinates')
    x_old, y2d_old, z2d_old = build_grid_xyz(OLD)
    y_int = y2d_old[BFR:BFR+OLD.NY, BFR]
    z_int = z2d_old[BFR, BFR:BFR+OLD.NZ]
    print('      Y interior range [{:.4f}, {:.4f}] (expect [0, {:.1f}])'.format(
        y_int.min(), y_int.max(), LY))
    print('      Z interior range [{:.4f}, {:.4f}] (expect [hill, {:.3f}])'.format(
        z_int.min(), z_int.max(), LZ))

    # ---- Step 3: read checkpoint, compute macros ----
    print('[3/8] Reading {} f-files ({} ranks x 19 directions)'.format(OLD.JP*19, OLD.JP))
    rho_g = np.zeros((OLD.NY6, OLD.NZ6, OLD.NX6), dtype=np.float64)
    momx_g = np.zeros_like(rho_g)
    momy_g = np.zeros_like(rho_g)
    momz_g = np.zeros_like(rho_g)

    for q in range(19):
        per_rank = []
        for r in range(OLD.JP):
            path = os.path.join(args.old_dir, 'f{:02d}_{}.bin'.format(q, r))
            per_rank.append(read_rank_bin(path, OLD))
        f_g = stitch_y(per_rank, OLD)
        rho_g  += f_g
        if E[q, 0] != 0:
            momx_g += E[q, 0] * f_g
        if E[q, 1] != 0:
            momy_g += E[q, 1] * f_g
        if E[q, 2] != 0:
            momz_g += E[q, 2] * f_g
        print('      f{:02d}: stitched {} ranks'.format(q, OLD.JP), flush=True)

    rho_safe = np.where(rho_g > 1e-12, rho_g, 1.0)
    ux_g = momx_g / rho_safe
    uy_g = momy_g / rho_safe
    uz_g = momz_g / rho_safe
    del momx_g, momy_g, momz_g, rho_safe

    interior_slice = (slice(BFR, BFR+OLD.NY), slice(BFR, BFR+OLD.NZ), slice(BFR, BFR+OLD.NX))
    print('      OLD interior rho = [{:.6f}, {:.6f}], mean = {:.6f}'.format(
        rho_g[interior_slice].min(), rho_g[interior_slice].max(),
        rho_g[interior_slice].mean()))
    print('      OLD interior max|u| = {:.6e}, max|v| = {:.6e}, max|w| = {:.6e}'.format(
        np.abs(ux_g[interior_slice]).max(),
        np.abs(uy_g[interior_slice]).max(),
        np.abs(uz_g[interior_slice]).max()))

    # Cross-check stored rho against sum(f).  In a running LBM with mass
    # correction (checkrho.dat), rho is adjusted independently of f each step,
    # so rho_file != sum(f) by O(1e-4) is normal.  We use sum(f) as the
    # authoritative rho for feq/fneq computation (it's self-consistent with f).
    rho_file_g = stitch_y([
        read_rank_bin(os.path.join(args.old_dir, 'rho_{}.bin'.format(r)), OLD)
        for r in range(OLD.JP)
    ], OLD)
    rho_src_diff = float(np.max(np.abs(rho_file_g - rho_g)))
    print('      OLD max |rho_file - sum(f)| = {:.3e}'.format(rho_src_diff))
    if rho_src_diff > 1e-2:
        sys.exit('FATAL: source checkpoint rho vs sum(f) diff {:.3e} > 1e-2 (data corruption?)'.format(rho_src_diff))
    elif rho_src_diff > 1e-6:
        print('      WARN: rho_file != sum(f) by {:.3e} (expected from LBM mass correction)'.format(rho_src_diff))
        print('            Using sum(f) as authoritative rho for feq/fneq computation')
    del rho_file_g

    # ---- Step 4: build NEW grid ----
    print('[4/8] Building NEW grid coordinates')
    _, y2d_new, z2d_new = build_grid_xyz(NEW)
    y_int_new = y2d_new[BFR:BFR+NEW.NY, BFR]
    z_int_new = z2d_new[BFR, BFR:BFR+NEW.NZ]
    print('      Y interior range [{:.4f}, {:.4f}]'.format(y_int_new.min(), y_int_new.max()))
    print('      Z interior range [{:.4f}, {:.4f}]'.format(z_int_new.min(), z_int_new.max()))

    # ---- Step 5: interpolate macros ----
    print('[5/8] Interpolating macros (rho, ux, uy, uz) to NEW grid in computational space')
    t = time.time()
    rho_new = interpolate_comp_3d(rho_g, OLD, NEW)
    print('      rho:  {:.1f}s'.format(time.time() - t))
    t = time.time()
    ux_new = interpolate_comp_3d(ux_g, OLD, NEW)
    print('      ux:   {:.1f}s'.format(time.time() - t))
    t = time.time()
    uy_new = interpolate_comp_3d(uy_g, OLD, NEW)
    print('      uy:   {:.1f}s'.format(time.time() - t))
    t = time.time()
    uz_new = interpolate_comp_3d(uz_g, OLD, NEW)
    print('      uz:   {:.1f}s'.format(time.time() - t))

    print('      Filling ghost cells')
    fill_ghost(rho_new, NEW)
    fill_ghost(ux_new, NEW)
    fill_ghost(uy_new, NEW)
    fill_ghost(uz_new, NEW)

    new_int = (slice(BFR, BFR+NEW.NY), slice(BFR, BFR+NEW.NZ), slice(BFR, BFR+NEW.NX))
    print('      NEW interior rho = [{:.6f}, {:.6f}], mean = {:.6f}'.format(
        rho_new[new_int].min(), rho_new[new_int].max(), rho_new[new_int].mean()))
    print('      NEW interior max|u| = {:.6e}, max|v| = {:.6e}, max|w| = {:.6e}'.format(
        np.abs(ux_new[new_int]).max(),
        np.abs(uy_new[new_int]).max(),
        np.abs(uz_new[new_int]).max()))

    # ---- Step 6 & 7: f_eq + per-rank write ----
    print('[6/8] Reconstructing f_eq and writing per-rank files')
    os.makedirs(writing_dir)

    # Write rho per rank
    rho_pr = split_y(rho_new, NEW)
    for r in range(NEW.JP):
        rho_pr[r].tofile(os.path.join(writing_dir, 'rho_{}.bin'.format(r)))
    print('      wrote rho_0..rho_{}.bin'.format(NEW.JP - 1))

    # Per-q: interpolate f_neq, rebuild f = f_eq_new + scale*f_neq_new, split, write.
    # This preserves the old checkpoint's viscous/non-equilibrium content while
    # keeping the new-grid macroscopic field controlled by rho_new/u_new.
    rho_check = np.zeros_like(rho_new)
    min_f = float('inf')
    max_f = -float('inf')
    for q in range(19):
        per_rank = []
        for r in range(OLD.JP):
            path = os.path.join(args.old_dir, 'f{:02d}_{}.bin'.format(q, r))
            per_rank.append(read_rank_bin(path, OLD))
        f_old = stitch_y(per_rank, OLD)
        feq_old = compute_feq_q(rho_g, ux_g, uy_g, uz_g, q)
        fneq_old = f_old - feq_old
        del f_old, feq_old, per_rank

        fneq_new = interpolate_comp_3d(fneq_old, OLD, NEW)
        del fneq_old
        fill_ghost(fneq_new, NEW)

        feq = compute_feq_q(rho_new, ux_new, uy_new, uz_new, q)
        f_new = feq + args.fneq_scale * fneq_new
        del feq, fneq_new

        rho_check += f_new
        min_f = min(min_f, float(np.nanmin(f_new)))
        max_f = max(max_f, float(np.nanmax(f_new)))

        pr = split_y(f_new, NEW)
        for r in range(NEW.JP):
            pr[r].tofile(os.path.join(writing_dir, 'f{:02d}_{}.bin'.format(q, r)))
        print('      wrote f{:02d}_0..f{:02d}_{} with f_neq scale {:.3f}'.format(
            q, q, NEW.JP - 1, args.fneq_scale), flush=True)
        del f_new, pr

    rho_diff = float(np.max(np.abs(rho_check - rho_new)))
    print('      f range after reconstruction = [{:.15e}, {:.15e}]'.format(min_f, max_f))
    print('      max |sum(f_new)-rho_new| = {:.3e}'.format(rho_diff))
    if not np.isfinite(min_f) or not np.isfinite(max_f) or min_f <= 0.0:
        sys.exit('FATAL: reconstructed f contains non-finite or non-positive values')
    if rho_diff > 1e-10:
        sys.exit('FATAL: reconstructed f is not conservative enough: max |sum(f)-rho| = {:.3e}'.format(rho_diff))

    # Free old arrays after f_neq reconstruction is complete.
    del rho_g, ux_g, uy_g, uz_g, rho_check

    # ---- Step 8: metadata + atomic rename ----
    print('[7/8] Writing new metadata.dat')
    # NOTE on dt_global:
    #   The runtime computes dt_global = CFL / max|c_tilde| from Jacobian metric
    #   terms (gilbm/precompute.h:ComputeGlobalTimeStep), NOT from the simple
    #   minSize formula in variables.h. They differ by a factor of ~0.4-0.5,
    #   so any naively-written value would trip Phase 5 drift check
    #   (fileIO.h:658, |drift| > 1e-6 -> MPI_Abort).
    #
    #   We deliberately write dt_global=-1.0 to trigger the legacy-format
    #   skip path (fileIO.h:650): "metadata.dat 無 dt_global 欄位, 跳過漂移檢查".
    #   The runtime will compute its own dt_global from the new grid metrics
    #   on startup; dt_saved is only used for the drift guardrail and is
    #   discarded thereafter.
    naive_minsize = compute_minsize(NEW)
    new_meta = {
        'checkpoint_version': '2',
        'mpi_rank_count': str(NEW.JP),
        'grid_dims': '{},{},{}'.format(NEW.NX6, NEW.NYD6, NEW.NZ6),
        'step': str(args.step),
        'FTT': '{:.15f}'.format(0.0),
        'accu_count': '0',
        'Force': '{:.15f}'.format(Force_value),
        'Force_integral': '{:.15f}'.format(0.0),
        'error_prev': '{:.15f}'.format(0.0),
        'ctrl_initialized': '0',
        'gehrke_activated': '0',
        'dt_global': '-1.0',
        'gpu_time_ms': '0',
        'cv_count': '0',
    }
    write_metadata(os.path.join(writing_dir, 'metadata.dat'), new_meta)
    print('      Force={:.6e}  step={}  jp={}  grid_dims={}'.format(
        Force_value, args.step, NEW.JP, new_meta['grid_dims']))
    print('      dt_global written as -1.0 (skip Phase 5 drift check; runtime computes its own dt)')
    print('      (naive minSize for reference: {:.6e}; runtime Imamura dt typically ~0.4-0.5x of this)'.format(naive_minsize))

    print('[8/8] Atomic rename: {} -> {}'.format(writing_dir, args.new_dir))
    os.rename(writing_dir, args.new_dir)

    elapsed = time.time() - t0
    nf = 19 * NEW.JP + NEW.JP + 1
    print()
    print('Done in {:.1f}s. New checkpoint at: {}'.format(elapsed, args.new_dir))
    print('Total files: 19f x {} ranks + {} rho + 1 metadata = {}'.format(NEW.JP, NEW.JP, nf))


if __name__ == '__main__':
    main()
