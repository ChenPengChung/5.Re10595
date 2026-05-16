#!/usr/bin/env python3
"""
Poisson velocity projection for LBM checkpoint interpolation.

After cross-grid interpolation, scalar-wise remapping of velocity components
breaks the solenoidal constraint (div(u) != 0).  This module projects the
interpolated velocity field onto a divergence-free space via Helmholtz-Hodge
decomposition:

    u_proj = u_interp - grad(phi)

where phi solves:

    div(grad(phi)) = div(u_interp)

with periodic BCs in x (spanwise) and y (streamwise), and homogeneous Neumann
(dphi/dn = 0) in z (wall-normal).

The divergence (D) and gradient (G) operators use identical 2nd-order central
difference stencils on the collocated grid, so the Laplacian L = D*G is
self-consistent: div(u_proj) = div(u) - D*G*phi = b - L*phi = 0 to solver
tolerance.

Grid layout follows interp_checkpoint.py conventions:
  - Arrays shape (NY6, NZ6, NX6) with BFR=3 ghost layers per side
  - Interior slice [BFR:BFR+NY, BFR:BFR+NZ, BFR:BFR+NX]
  - Periodic: x (i, spanwise), y (j, streamwise)
  - Walls:    z (k, wall-normal) at k=BFR (bottom) and k=BFR+NZ-1 (top)
  - Curvilinear (j,k) -> (y,z) with inverse metric (dj_dy, dj_dz, dk_dy, dk_dz)
"""

import os
import sys
import time
import numpy as np
from scipy.sparse.linalg import LinearOperator, bicgstab

_pkg_dir = os.path.dirname(os.path.abspath(__file__))
if _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)

from interp_checkpoint import (
    BFR, LX,
    compute_inverse_metric_2d,
    enforce_periodic_physical_duplicates,
    fill_ghost,
)


# ---------------------------------------------------------------
# CD2 building blocks (full ghost-padded arrays)
# ---------------------------------------------------------------

def _gradient_cd2(phi, dx, dj_dy, dj_dz, dk_dy, dk_dz):
    """CD2 gradient of scalar phi.  All arrays are full (NY6, NZ6, NX6)."""
    dphi_di = np.zeros_like(phi)
    dphi_di[:, :, 1:-1] = (phi[:, :, 2:] - phi[:, :, :-2]) * 0.5

    dphi_dj = np.zeros_like(phi)
    dphi_dj[1:-1, :, :] = (phi[2:, :, :] - phi[:-2, :, :]) * 0.5

    dphi_dk = np.zeros_like(phi)
    dphi_dk[:, 1:-1, :] = (phi[:, 2:, :] - phi[:, :-2, :]) * 0.5

    jy = dj_dy[:, :, np.newaxis]
    jz = dj_dz[:, :, np.newaxis]
    ky = dk_dy[:, :, np.newaxis]
    kz = dk_dz[:, :, np.newaxis]

    gx = dphi_di / dx
    gy = jy * dphi_dj + ky * dphi_dk
    gz = jz * dphi_dj + kz * dphi_dk
    return gx, gy, gz


def _divergence_cd2(vx, vy, vz, dx, dj_dy, dj_dz, dk_dy, dk_dz):
    """CD2 divergence of vector (vx, vy, vz).  All arrays full-sized."""
    dvx_di = np.zeros_like(vx)
    dvx_di[:, :, 1:-1] = (vx[:, :, 2:] - vx[:, :, :-2]) * 0.5

    dvy_dj = np.zeros_like(vy)
    dvy_dj[1:-1, :, :] = (vy[2:, :, :] - vy[:-2, :, :]) * 0.5
    dvy_dk = np.zeros_like(vy)
    dvy_dk[:, 1:-1, :] = (vy[:, 2:, :] - vy[:, :-2, :]) * 0.5

    dvz_dj = np.zeros_like(vz)
    dvz_dj[1:-1, :, :] = (vz[2:, :, :] - vz[:-2, :, :]) * 0.5
    dvz_dk = np.zeros_like(vz)
    dvz_dk[:, 1:-1, :] = (vz[:, 2:, :] - vz[:, :-2, :]) * 0.5

    jy = dj_dy[:, :, np.newaxis]
    jz = dj_dz[:, :, np.newaxis]
    ky = dk_dy[:, :, np.newaxis]
    kz = dk_dz[:, :, np.newaxis]

    return (dvx_di / dx
            + jy * dvy_dj + ky * dvy_dk
            + jz * dvz_dj + kz * dvz_dk)


# ---------------------------------------------------------------
# Phi boundary conditions: periodic x,y + Neumann mirror z
# ---------------------------------------------------------------

def _fill_phi_bc(phi, cfg):
    """Set periodic duplicates, then ghost: X periodic, Z Neumann mirror, Y periodic."""
    enforce_periodic_physical_duplicates(phi, cfg)

    nx6, ny6 = cfg.NX6, cfg.NY6
    kt = BFR + cfg.NZ - 1

    # X periodic ghost (same as fill_ghost)
    phi[:, :, 2]       = phi[:, :, nx6 - 5]
    phi[:, :, 1]       = phi[:, :, nx6 - 6]
    phi[:, :, 0]       = phi[:, :, nx6 - 7]
    phi[:, :, nx6 - 3] = phi[:, :, 4]
    phi[:, :, nx6 - 2] = phi[:, :, 5]
    phi[:, :, nx6 - 1] = phi[:, :, 6]

    # Z Neumann mirror ghost: dphi/dk = 0 at walls
    phi[:, BFR - 1, :] = phi[:, BFR + 1, :]
    phi[:, BFR - 2, :] = phi[:, BFR + 2, :]
    phi[:, BFR - 3, :] = phi[:, BFR + 3, :]
    phi[:, kt + 1, :]  = phi[:, kt - 1, :]
    phi[:, kt + 2, :]  = phi[:, kt - 2, :]
    phi[:, kt + 3, :]  = phi[:, kt - 3, :]

    # Y periodic ghost (same as fill_ghost)
    phi[2, :, :]       = phi[ny6 - 5, :, :]
    phi[1, :, :]       = phi[ny6 - 6, :, :]
    phi[0, :, :]       = phi[ny6 - 7, :, :]
    phi[ny6 - 3, :, :] = phi[4, :, :]
    phi[ny6 - 2, :, :] = phi[5, :, :]
    phi[ny6 - 1, :, :] = phi[6, :, :]


# ---------------------------------------------------------------
# Public API
# ---------------------------------------------------------------

def divergence_diagnostic(ux, uy, uz, cfg, y_2d, z_2d):
    """Return (div_rms, div_max) on unique interior points using CD2."""
    dx = LX / (cfg.NX - 1)
    dj_dy, dj_dz, dk_dy, dk_dz = compute_inverse_metric_2d(y_2d, z_2d)
    div_full = _divergence_cd2(ux, uy, uz, dx, dj_dy, dj_dz, dk_dy, dk_dz)
    s = (slice(BFR, BFR + cfg.NY - 1),
         slice(BFR, BFR + cfg.NZ),
         slice(BFR, BFR + cfg.NX - 1))
    d = div_full[s]
    return float(np.sqrt(np.mean(d ** 2))), float(np.max(np.abs(d)))


def poisson_project(ux, uy, uz, cfg, y_2d, z_2d,
                    tol=1e-6, maxiter=2000, verbose=True):
    """Project velocity onto divergence-free space.

    Solves  div(grad(phi)) = div(u)  with periodic x,y / Neumann z,
    then returns  u_proj = u - grad(phi).

    D and G use identical CD2 stencils, so div(u_proj) = 0 to solver tol.

    Parameters
    ----------
    ux, uy, uz : ndarray (NY6, NZ6, NX6), ghost-filled velocity components
    cfg        : GridConfig from interp_checkpoint
    y_2d, z_2d : ndarray (NY6, NZ6), grid coordinates from build_grid_xyz
    tol        : BiCGSTAB relative tolerance
    maxiter    : maximum BiCGSTAB iterations
    verbose    : print progress

    Returns
    -------
    ux_proj, uy_proj, uz_proj : corrected velocity (ghost-filled)
    info : dict with divergence and solver diagnostics
    """
    dx = LX / (cfg.NX - 1)
    dj_dy, dj_dz, dk_dy, dk_dz = compute_inverse_metric_2d(y_2d, z_2d)

    # Unique DOF counts (exclude periodic duplicates in x, y)
    nj = cfg.NY - 1
    nk = cfg.NZ
    ni = cfg.NX - 1
    n_dof = nj * nk * ni
    shape3 = (cfg.NY6, cfg.NZ6, cfg.NX6)

    # Slices: unique interior DOFs vs full interior
    uniq = (slice(BFR, BFR + nj),
            slice(BFR, BFR + nk),
            slice(BFR, BFR + ni))
    full_int = (slice(BFR, BFR + cfg.NY),
                slice(BFR, BFR + cfg.NZ),
                slice(BFR, BFR + cfg.NX))

    # ---- RHS: b = div(u) on unique DOFs ----
    div_u = _divergence_cd2(ux, uy, uz, dx, dj_dy, dj_dz, dk_dy, dk_dz)
    b_arr = div_u[uniq]
    div_rms_before = float(np.sqrt(np.mean(b_arr ** 2)))
    div_max_before = float(np.max(np.abs(b_arr)))

    if verbose:
        print('      Poisson projection: {} DOFs ({} x {} x {})'.format(
            n_dof, nj, nk, ni))
        print('      div(u) BEFORE: RMS = {:.6e}, max = {:.6e}'.format(
            div_rms_before, div_max_before))

    b_vec = b_arr.ravel().astype(np.float64, copy=True)
    b_mean = float(np.mean(b_vec))
    b_vec -= b_mean

    # ---- Matrix-free Laplacian A = D . G ----
    def matvec(x_vec):
        phi = np.zeros(shape3, dtype=np.float64)
        phi[BFR:BFR + nj, BFR:BFR + nk, BFR:BFR + ni] = \
            x_vec.reshape(nj, nk, ni)
        _fill_phi_bc(phi, cfg)
        gx, gy, gz = _gradient_cd2(phi, dx, dj_dy, dj_dz, dk_dy, dk_dz)
        lap = _divergence_cd2(gx, gy, gz, dx, dj_dy, dj_dz, dk_dy, dk_dz)
        return lap[uniq].ravel()

    A = LinearOperator((n_dof, n_dof), matvec=matvec, dtype=np.float64)

    # ---- Solve with BiCGSTAB ----
    iters = [0]

    def _cb(xk):
        iters[0] += 1
        if verbose and iters[0] % 200 == 0:
            print('        BiCGSTAB iter {}...'.format(iters[0]), flush=True)

    t0 = time.time()
    phi_vec, info_code = bicgstab(A, b_vec, tol=tol, maxiter=maxiter,
                                  callback=_cb)
    dt_solve = time.time() - t0

    if verbose:
        print('      BiCGSTAB: {} iters, {:.1f}s, info={}'.format(
            iters[0], dt_solve, info_code))
    if info_code > 0:
        print('      WARN: BiCGSTAB did not converge in {} iterations'.format(
            maxiter))
    elif info_code < 0:
        print('      WARN: BiCGSTAB breakdown (info={})'.format(info_code))

    # Remove constant null-space mode
    phi_vec -= np.mean(phi_vec)

    # ---- Apply velocity correction: u_proj = u - grad(phi) ----
    phi = np.zeros(shape3, dtype=np.float64)
    phi[BFR:BFR + nj, BFR:BFR + nk, BFR:BFR + ni] = \
        phi_vec.reshape(nj, nk, ni)
    _fill_phi_bc(phi, cfg)

    gx, gy, gz = _gradient_cd2(phi, dx, dj_dy, dj_dz, dk_dy, dk_dz)

    # Consistent diagnostic: full-array subtraction u - grad(phi),
    # computed BEFORE re-ghost-fill so D and G ghost treatments match
    # the Laplacian matvec (guarantees div = b - L*phi ~ 0).
    ux_cons = ux - gx
    uy_cons = uy - gy
    uz_cons = uz - gz
    div_cons = _divergence_cd2(ux_cons, uy_cons, uz_cons,
                               dx, dj_dy, dj_dz, dk_dy, dk_dz)
    d_cons = div_cons[uniq]
    div_rms_consistent = float(np.sqrt(np.mean(d_cons ** 2)))
    div_max_consistent = float(np.max(np.abs(d_cons)))

    # Interior-only (exclude wall rows k=BFR and k=kt):
    # wall divergence is constrained by no-slip and Neumann BC interaction
    wall_excl = (slice(BFR, BFR + nj),
                 slice(BFR + 1, BFR + nk - 1),
                 slice(BFR, BFR + ni))
    d_int = div_cons[wall_excl]
    div_rms_interior = float(np.sqrt(np.mean(d_int ** 2)))
    div_max_interior = float(np.max(np.abs(d_int)))

    # Build output velocity (interior correction + standard ghost fill)
    ux_proj = ux.copy()
    uy_proj = uy.copy()
    uz_proj = uz.copy()
    ux_proj[full_int] -= gx[full_int]
    uy_proj[full_int] -= gy[full_int]
    uz_proj[full_int] -= gz[full_int]

    for arr in (ux_proj, uy_proj, uz_proj):
        enforce_periodic_physical_duplicates(arr, cfg)
        fill_ghost(arr, cfg)

    if verbose:
        print('      div(u) AFTER (consistent D.G):')
        print('        all points: RMS = {:.6e}, max = {:.6e}'.format(
            div_rms_consistent, div_max_consistent))
        print('        interior (excl walls): RMS = {:.6e}, max = {:.6e}'.format(
            div_rms_interior, div_max_interior))
        if div_rms_before > 0:
            print('      reduction: {:.2e}x (all), {:.2e}x (interior)'.format(
                div_rms_consistent / div_rms_before,
                div_rms_interior / div_rms_before))
        corr_max = float(max(np.max(np.abs(gx[full_int])),
                             np.max(np.abs(gy[full_int])),
                             np.max(np.abs(gz[full_int]))))
        print('      max |grad(phi)| correction = {:.6e}'.format(corr_max))

    del ux_cons, uy_cons, uz_cons

    info = {
        'div_rms_before': div_rms_before,
        'div_max_before': div_max_before,
        'div_rms_after': div_rms_consistent,
        'div_max_after': div_max_consistent,
        'div_rms_interior': div_rms_interior,
        'div_max_interior': div_max_interior,
        'solver_info': info_code,
        'solver_iters': iters[0],
        'solve_time_s': dt_solve,
        'n_dof': n_dof,
        'b_mean_removed': b_mean,
    }
    return ux_proj, uy_proj, uz_proj, info
