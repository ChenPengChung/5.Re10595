#!/usr/bin/env python3
"""
Poisson velocity projection for LBM checkpoint interpolation.

After cross-grid interpolation, scalar-wise remapping of velocity components
breaks the solenoidal constraint (div(u) != 0).  This module projects the
interpolated velocity field onto a divergence-free space via Helmholtz-Hodge
decomposition:

    u_proj = u_interp - grad(phi)

where phi solves:

    L(phi) = div(u_interp)

with periodic BCs in x (spanwise) and y (streamwise), and homogeneous Neumann
(dphi/dn = 0) in z (wall-normal).

Solver strategy: FFT in x (uniform + periodic), then sparse-direct solve of
independent 2D (j,k) Helmholtz problems for each x-wavenumber.  For wavenumber
ki, the 2D system is:

    [L_jk + lambda_ki * I] * phi_hat = b_hat

where lambda_ki = -(4/dx^2)*sin^2(pi*ki/NI) and L_jk is the compact
conservative Laplacian in the curvilinear (j,k) plane with face-averaged
metric coefficients.  Each 2D system has (NY-1)*NZ DOFs (~8K) and is solved
with sparse LU factorisation in milliseconds.

The velocity correction uses CD2 gradient: u_proj = u - grad_CD2(phi).
Since L != D*G, the projection does not drive div to machine zero, but
achieves orders-of-magnitude reduction without checkerboard instability.

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
from scipy.sparse import coo_matrix, eye as speye
from scipy.sparse.linalg import splu

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
# CD2 building blocks (for RHS divergence and velocity correction)
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

    phi[:, :, 2]       = phi[:, :, nx6 - 5]
    phi[:, :, 1]       = phi[:, :, nx6 - 6]
    phi[:, :, 0]       = phi[:, :, nx6 - 7]
    phi[:, :, nx6 - 3] = phi[:, :, 4]
    phi[:, :, nx6 - 2] = phi[:, :, 5]
    phi[:, :, nx6 - 1] = phi[:, :, 6]

    phi[:, BFR - 1, :] = phi[:, BFR + 1, :]
    phi[:, BFR - 2, :] = phi[:, BFR + 2, :]
    phi[:, BFR - 3, :] = phi[:, BFR + 3, :]
    phi[:, kt + 1, :]  = phi[:, kt - 1, :]
    phi[:, kt + 2, :]  = phi[:, kt - 2, :]
    phi[:, kt + 3, :]  = phi[:, kt - 3, :]

    phi[2, :, :]       = phi[ny6 - 5, :, :]
    phi[1, :, :]       = phi[ny6 - 6, :, :]
    phi[0, :, :]       = phi[ny6 - 7, :, :]
    phi[ny6 - 3, :, :] = phi[4, :, :]
    phi[ny6 - 2, :, :] = phi[5, :, :]
    phi[ny6 - 1, :, :] = phi[6, :, :]


# ---------------------------------------------------------------
# FFT-based Poisson solver
# ---------------------------------------------------------------

def _build_2d_laplacian(gjj_2d, gkk_2d, nj, nk):
    """Build 2D compact conservative Laplacian as sparse CSR.

    Diagonal metric only (g^{jj}, g^{kk}); cross terms g^{jk} omitted
    to preserve strict symmetry.  j is periodic, k has Neumann mirror BCs.

    Returns CSR matrix of size (nj*nk, nj*nk).
    """
    n = nj * nk

    j_loc = np.arange(nj)
    k_loc = np.arange(nk)
    J, K = np.meshgrid(j_loc, k_loc, indexing='ij')
    J_flat = J.ravel()
    K_flat = K.ravel()
    P = J_flat * nk + K_flat

    J_abs = BFR + J_flat
    K_abs = BFR + K_flat

    # j-direction face averages (metric includes ghost/periodic-duplicate nodes)
    gjj_jp = (gjj_2d[J_abs, K_abs] + gjj_2d[J_abs + 1, K_abs]) * 0.5
    gjj_jm = (gjj_2d[J_abs, K_abs] + gjj_2d[J_abs - 1, K_abs]) * 0.5

    jp_idx = ((J_flat + 1) % nj) * nk + K_flat
    jm_idx = ((J_flat - 1) % nj) * nk + K_flat

    # k-direction face averages (ghost nodes available for ±1 of interior)
    gkk_kp = (gkk_2d[J_abs, K_abs] + gkk_2d[J_abs, K_abs + 1]) * 0.5
    gkk_km = (gkk_2d[J_abs, K_abs] + gkk_2d[J_abs, K_abs - 1]) * 0.5

    rows_list = []
    cols_list = []
    data_list = []

    # --- Diagonal: -(gjj_p + gjj_m + gkk_p + gkk_m) ---
    diag_val = -(gjj_jp + gjj_jm + gkk_kp + gkk_km)
    rows_list.append(P)
    cols_list.append(P)
    data_list.append(diag_val)

    # --- j+1 and j-1 neighbours (periodic) ---
    rows_list.append(P)
    cols_list.append(jp_idx)
    data_list.append(gjj_jp)

    rows_list.append(P)
    cols_list.append(jm_idx)
    data_list.append(gjj_jm)

    # --- k-direction: separate interior / boundary treatment ---
    interior = (K_flat > 0) & (K_flat < nk - 1)
    bot = K_flat == 0
    top = K_flat == nk - 1

    # Interior k: k+1 and k-1 neighbours
    kp_int = J_flat[interior] * nk + (K_flat[interior] + 1)
    km_int = J_flat[interior] * nk + (K_flat[interior] - 1)
    rows_list.append(P[interior])
    cols_list.append(kp_int)
    data_list.append(gkk_kp[interior])
    rows_list.append(P[interior])
    cols_list.append(km_int)
    data_list.append(gkk_km[interior])

    # Bottom Neumann (k=0): phi_{k=-1} = phi_{k=1} → k=1 gets (gkk_p + gkk_m)
    k1_bot = J_flat[bot] * nk + 1
    rows_list.append(P[bot])
    cols_list.append(k1_bot)
    data_list.append(gkk_kp[bot] + gkk_km[bot])

    # Top Neumann (k=nk-1): phi_{k=nk} = phi_{k=nk-2} → k=nk-2 gets (gkk_p + gkk_m)
    km2_top = J_flat[top] * nk + (nk - 2)
    rows_list.append(P[top])
    cols_list.append(km2_top)
    data_list.append(gkk_kp[top] + gkk_km[top])

    rows_all = np.concatenate(rows_list)
    cols_all = np.concatenate(cols_list)
    data_all = np.concatenate(data_list)

    return coo_matrix((data_all, (rows_all, cols_all)), shape=(n, n)).tocsr()


def _solve_poisson_fft(b_3d, dx, gjj_2d, gkk_2d, nj, nk, ni, verbose=True):
    """Solve Poisson equation using FFT in x + sparse LU in (j,k).

    For each x-wavenumber ki, factors the real matrix A = L_2d + lambda_ki*I
    once, then forward/back-solves for real and imaginary RHS parts separately.
    This avoids complex-valued LU factorisation (the main bottleneck).

    Parameters
    ----------
    b_3d  : ndarray (nj, nk, ni) — RHS on unique interior DOFs
    dx    : float — uniform x spacing
    gjj_2d, gkk_2d : ndarray (NY6, NZ6) — contravariant diagonal metric
    nj, nk, ni : int — unique DOF counts (NY-1, NZ, NX-1)
    verbose : bool

    Returns
    -------
    phi_3d : ndarray (nj, nk, ni) — solution
    """
    t0 = time.time()

    b_hat = np.fft.rfft(b_3d, axis=2)
    n_modes = b_hat.shape[2]
    phi_hat = np.zeros_like(b_hat)

    ki_arr = np.arange(n_modes)
    # Use exact CD2 D·G x-eigenvalue (not compact Laplacian eigenvalue).
    # D·G: -(1/dx²)*sin²(2π·ki/NI)  vs  compact: -(4/dx²)*sin²(π·ki/NI)
    # This eliminates the x-direction operator mismatch entirely, so outer
    # Richardson iterations only need to correct the (j,k) mismatch.
    lambda_x = -(1.0 / (dx * dx)) * np.sin(2.0 * np.pi * ki_arr / ni) ** 2

    # ki = NI/2 (Nyquist) has lambda_x = 0 → pure Neumann (same as ki=0)
    nyquist_modes = set()
    nyquist_modes.add(0)
    if ni % 2 == 0 and ni // 2 < n_modes:
        nyquist_modes.add(ni // 2)

    L_2d = _build_2d_laplacian(gjj_2d, gkk_2d, nj, nk)
    n_2d = nj * nk
    I_2d = speye(n_2d, format='csc', dtype=np.float64)

    dt_build = time.time() - t0
    if verbose:
        print('      2D Laplacian: {} x {} ({} nnz), built in {:.2f}s'.format(
            n_2d, n_2d, L_2d.nnz, dt_build))

    L_csc = L_2d.tocsc()

    t1 = time.time()
    for ki_idx in range(n_modes):
        A = L_csc + lambda_x[ki_idx] * I_2d

        rhs_re = b_hat[:, :, ki_idx].real.ravel().copy()
        rhs_im = b_hat[:, :, ki_idx].imag.ravel().copy()

        if ki_idx in nyquist_modes:
            rhs_re -= np.mean(rhs_re)
            rhs_im -= np.mean(rhs_im)
            A_reg = A + (1e-12 / (dx * dx)) * I_2d
            lu = splu(A_reg)
            sol_re = lu.solve(rhs_re)
            sol_im = lu.solve(rhs_im)
            sol_re -= np.mean(sol_re)
            sol_im -= np.mean(sol_im)
        else:
            lu = splu(A)
            sol_re = lu.solve(rhs_re)
            sol_im = lu.solve(rhs_im)

        phi_hat[:, :, ki_idx] = (sol_re + 1j * sol_im).reshape(nj, nk)

    dt_solve = time.time() - t1
    if verbose:
        print('      FFT + {} splu factor+solve in {:.2f}s'.format(
            n_modes, dt_solve))

    phi_3d = np.fft.irfft(phi_hat, n=ni, axis=2)
    return phi_3d.real


# ---------------------------------------------------------------
# Public API
# ---------------------------------------------------------------

class PoissonProjectionError(RuntimeError):
    """Raised when the Poisson solver fails to converge."""
    pass


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
                    max_outer=80, div_tol=1e-6, verbose=True,
                    clamp_walls=True):
    """Project velocity onto divergence-free space.

    Uses outer Richardson iterations to close the gap between the compact
    Laplacian L (used for the Poisson solve) and the CD2 D*G operators
    (used for divergence/gradient in the velocity correction).

    Each outer iteration:
      1. r = div_CD2(u_current)              — residual divergence
      2. dphi = L^{-1}(r)                    — FFT Poisson solve
      3. u_current -= grad_CD2(dphi)          — velocity correction
      4. optionally re-apply no-slip wall clamp, then periodic BCs

    Iterations stop when div RMS < div_tol or stagnates.

    Parameters
    ----------
    ux, uy, uz : ndarray (NY6, NZ6, NX6), ghost-filled velocity components
    cfg        : GridConfig from interp_checkpoint
    y_2d, z_2d : ndarray (NY6, NZ6), grid coordinates from build_grid_xyz
    max_outer  : maximum outer Richardson iterations
    div_tol    : convergence tolerance on div RMS
    verbose    : print progress
    clamp_walls : keep physical wall planes at no-slip during the projection

    Returns
    -------
    ux_proj, uy_proj, uz_proj : corrected velocity (ghost-filled)
    info : dict with divergence and solver diagnostics
    """
    dx = LX / (cfg.NX - 1)
    dj_dy, dj_dz, dk_dy, dk_dz = compute_inverse_metric_2d(y_2d, z_2d)

    nj = cfg.NY - 1
    nk = cfg.NZ
    ni = cfg.NX - 1
    n_dof = nj * nk * ni
    shape3 = (cfg.NY6, cfg.NZ6, cfg.NX6)

    uniq = (slice(BFR, BFR + nj),
            slice(BFR, BFR + nk),
            slice(BFR, BFR + ni))
    full_int = (slice(BFR, BFR + cfg.NY),
                slice(BFR, BFR + cfg.NZ),
                slice(BFR, BFR + cfg.NX))

    gjj_2d = dj_dy**2 + dj_dz**2
    gkk_2d = dk_dy**2 + dk_dz**2

    # Working copies
    ux_w = ux.copy()
    uy_w = uy.copy()
    uz_w = uz.copy()

    def _enforce_projection_bc():
        if clamp_walls:
            kt_wall = cfg.NZ6 - 1 - BFR
            for arr in (ux_w, uy_w, uz_w):
                arr[:, BFR, :] = 0.0
                arr[:, kt_wall, :] = 0.0
        for arr in (ux_w, uy_w, uz_w):
            enforce_periodic_physical_duplicates(arr, cfg)
            fill_ghost(arr, cfg)

    _enforce_projection_bc()

    # Initial divergence after enforcing the projection boundary constraints.
    div_u = _divergence_cd2(ux_w, uy_w, uz_w, dx, dj_dy, dj_dz, dk_dy, dk_dz)
    b_arr = div_u[uniq].copy()
    div_rms_before = float(np.sqrt(np.mean(b_arr ** 2)))
    div_max_before = float(np.max(np.abs(b_arr)))

    if verbose:
        print('      Poisson projection: {} DOFs ({} x {} x {})'.format(
            n_dof, nj, nk, ni))
        print('      div(u) BEFORE: RMS = {:.6e}, max = {:.6e}'.format(
            div_rms_before, div_max_before))

    t0_total = time.time()
    n_outer = 0
    prev_rms = div_rms_before

    for outer in range(max_outer):
        # Residual divergence
        if outer == 0:
            r_arr = b_arr.copy()
        else:
            div_r = _divergence_cd2(ux_w, uy_w, uz_w,
                                    dx, dj_dy, dj_dz, dk_dy, dk_dz)
            r_arr = div_r[uniq].copy()

        r_rms = float(np.sqrt(np.mean(r_arr ** 2)))
        r_max = float(np.max(np.abs(r_arr)))

        if verbose:
            print('      outer iter {}: div RMS = {:.6e}, max = {:.6e}'.format(
                outer, r_rms, r_max))

        if r_rms < div_tol:
            if verbose:
                print('      converged (div RMS < {:.0e})'.format(div_tol))
            break

        if outer > 0 and r_rms > 0.99 * prev_rms:
            if verbose:
                print('      stagnated (ratio {:.4f}), stopping'.format(
                    r_rms / prev_rms))
            break

        prev_rms = r_rms

        # Remove mean for Neumann compatibility
        r_arr -= np.mean(r_arr)

        # FFT Poisson solve
        try:
            dphi_int = _solve_poisson_fft(
                r_arr, dx, gjj_2d, gkk_2d, nj, nk, ni,
                verbose=(verbose and outer == 0))
        except Exception as e:
            raise PoissonProjectionError(
                'FFT Poisson solver failed at outer iter {}: {}'.format(outer, e))

        dphi_int -= np.mean(dphi_int)

        if not np.all(np.isfinite(dphi_int)):
            raise PoissonProjectionError(
                'Poisson solution contains NaN/Inf at outer iter {}'.format(outer))

        # Embed phi and compute gradient correction
        phi = np.zeros(shape3, dtype=np.float64)
        phi[BFR:BFR + nj, BFR:BFR + nk, BFR:BFR + ni] = dphi_int
        _fill_phi_bc(phi, cfg)

        gx, gy, gz = _gradient_cd2(phi, dx, dj_dy, dj_dz, dk_dy, dk_dz)

        # Apply correction to working velocity
        ux_w[full_int] -= gx[full_int]
        uy_w[full_int] -= gy[full_int]
        uz_w[full_int] -= gz[full_int]

        _enforce_projection_bc()

        n_outer = outer + 1

    dt_total = time.time() - t0_total

    # Final divergence diagnostic
    div_final = _divergence_cd2(ux_w, uy_w, uz_w,
                                dx, dj_dy, dj_dz, dk_dy, dk_dz)
    d_cons = div_final[uniq]
    div_rms_after = float(np.sqrt(np.mean(d_cons ** 2)))
    div_max_after = float(np.max(np.abs(d_cons)))

    wall_excl = (slice(BFR, BFR + nj),
                 slice(BFR + 1, BFR + nk - 1),
                 slice(BFR, BFR + ni))
    d_int = div_final[wall_excl]
    div_rms_interior = float(np.sqrt(np.mean(d_int ** 2)))
    div_max_interior = float(np.max(np.abs(d_int)))

    if verbose:
        print('      {} outer iterations, {:.1f}s total'.format(n_outer, dt_total))
        print('      div(u) AFTER:')
        print('        all points: RMS = {:.6e}, max = {:.6e}'.format(
            div_rms_after, div_max_after))
        print('        interior (excl walls): RMS = {:.6e}, max = {:.6e}'.format(
            div_rms_interior, div_max_interior))
        if div_rms_before > 0:
            print('      reduction: {:.2e}x (all), {:.2e}x (interior)'.format(
                div_rms_after / div_rms_before,
                div_rms_interior / div_rms_before))
        corr_max = float(max(np.max(np.abs(ux_w[full_int] - ux[full_int])),
                             np.max(np.abs(uy_w[full_int] - uy[full_int])),
                             np.max(np.abs(uz_w[full_int] - uz[full_int]))))
        print('      max |du| correction = {:.6e}'.format(corr_max))

    info = {
        'div_rms_before': div_rms_before,
        'div_max_before': div_max_before,
        'div_rms_after': div_rms_after,
        'div_max_after': div_max_after,
        'div_rms_interior': div_rms_interior,
        'div_max_interior': div_max_interior,
        'solver_info': 0,
        'outer_iters': n_outer,
        'solve_time_s': dt_total,
        'n_dof': n_dof,
    }
    return ux_w, uy_w, uz_w, info
