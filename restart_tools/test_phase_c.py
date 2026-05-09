#!/usr/bin/env python3
"""Phase C unit tests: identity + linear analytic.

Tests:
  Test 1: OLD=NEW grid -> phys interp should be bit-exact (residual < 1e-12)
  Test 2: Different GAMMA OLD vs NEW -> linear field f(x,y,z) = 10y + 100z + 0.5x
          should be recovered to FP precision (< 1e-10) under physical-space
          interpolation, regardless of GAMMA.
  Test 3: Different GAMMA and NX -> same analytic field, exercising spanwise
          interpolation plus periodic endpoint ghost-wrap.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

from interp_checkpoint import (
    GridConfig, BFR, LX, LY, LZ, H_HILL,
    fill_ghost,
    precompute_phys_mapping_2d, interpolate_phys_3d_with_mapping,
)

# ---------------------------------------------------------------
# Helper: synthetic curvilinear grid (mimics build_grid_xyz output
# without requiring a Tecplot .dat file).
# ---------------------------------------------------------------
def vinokur_tanh(eta, gamma, alpha):
    """Two-sided tanh stretching used by grid_zeta_tool."""
    t_neg = np.tanh(gamma * alpha)
    denom = np.tanh(gamma * (1.0 - alpha)) + t_neg
    zeta = (np.tanh(gamma * (eta - alpha)) + t_neg) / denom
    zeta[0] = 0.0
    zeta[-1] = 1.0
    return zeta


def hill_function(y, ly=LY):
    """Periodic-hill bottom-wall profile (same as model.h)."""
    # Simplified version: half-cosine bump centered at LY/2, peak height H_HILL
    # (Frohlich's actual polynomial is more complex; this is sufficient for
    # synthetic Phase C tests that only require a smooth, j-dependent z_bottom.)
    return H_HILL * 0.5 * (1.0 + np.cos(2.0 * np.pi * y / ly))


def build_synthetic_grid_2d(cfg):
    """Construct (NY6, NZ6) y_2d, z_2d arrays without Tecplot file.

    Mimics build_grid_xyz: y is uniform streamwise, z is hill_function bottom +
    Vinokur tanh stretching to LZ top.
    """
    y_2d = np.zeros((cfg.NY6, cfg.NZ6), dtype=np.float64)
    z_2d = np.zeros((cfg.NY6, cfg.NZ6), dtype=np.float64)

    dy = LY / (cfg.NY - 1)
    eta = np.arange(cfg.NZ, dtype=np.float64) / (cfg.NZ - 1)
    zeta_unit = vinokur_tanh(eta, cfg.GAMMA, cfg.ALPHA)

    for j in range(cfg.NY):
        y_phys = j * dy
        z_bot = hill_function(y_phys, LY)
        z_top = LZ
        z_col = z_bot + zeta_unit * (z_top - z_bot)
        y_2d[BFR + j, BFR:BFR+cfg.NZ] = y_phys
        z_2d[BFR + j, BFR:BFR+cfg.NZ] = z_col

    # k-direction ghost: linear extrap (mirror build_grid_xyz)
    nz6 = cfg.NZ6
    for j in range(BFR, BFR + cfg.NY):
        for off in range(BFR):
            # bottom: k=2 from k=3,4; k=1 from k=2,3; k=0 from k=1,2
            kg = BFR - 1 - off
            kref1 = kg + 1; kref2 = kg + 2
            y_2d[j, kg] = 2.0 * y_2d[j, kref1] - y_2d[j, kref2]
            z_2d[j, kg] = 2.0 * z_2d[j, kref1] - z_2d[j, kref2]
            # top: nz6-3 from nz6-4,5; nz6-2 from nz6-3,4; nz6-1 from nz6-2,3
            kg = nz6 - BFR + off
            kref1 = kg - 1; kref2 = kg - 2
            y_2d[j, kg] = 2.0 * y_2d[j, kref1] - y_2d[j, kref2]
            z_2d[j, kg] = 2.0 * z_2d[j, kref1] - z_2d[j, kref2]

    # j-direction ghost: periodic with +/-LY shift on y (mirror build_grid_xyz)
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

    return y_2d, z_2d


def build_linear_field(cfg, y_2d, z_2d, dx):
    """Construct synthetic field f(x, y, z) = 10*y + 100*z + 0.5*x on cfg grid."""
    field = np.zeros((cfg.NY6, cfg.NZ6, cfg.NX6), dtype=np.float64)
    for j in range(cfg.NY):
        for k in range(cfg.NZ):
            y = y_2d[BFR+j, BFR+k]
            z = z_2d[BFR+j, BFR+k]
            for i in range(cfg.NX):
                x = i * dx
                field[BFR+j, BFR+k, BFR+i] = 10.0*y + 100.0*z + 0.5*x
    fill_ghost(field, cfg)
    return field


# ---------------------------------------------------------------
# Test 1: Identity (OLD = NEW, bit-exact)
# ---------------------------------------------------------------
def test_identity():
    print('\n=== Test 1: Identity (OLD = NEW, bit-exact) ===')
    cfg = GridConfig(nx=16, ny=33, nz=33, jp=4, gamma=2.0, alpha=0.5,
                     grid_dat='<synthetic>')
    y2d, z2d = build_synthetic_grid_2d(cfg)
    dx = LX / (cfg.NX - 1)
    field_old = build_linear_field(cfg, y2d, z2d, dx)

    mapping = precompute_phys_mapping_2d(y2d, z2d, y2d, z2d, cfg, cfg)
    field_new = interpolate_phys_3d_with_mapping(field_old, mapping)

    interior_old = field_old[BFR:BFR+cfg.NY, BFR:BFR+cfg.NZ, BFR:BFR+cfg.NX]
    interior_new = field_new[BFR:BFR+cfg.NY, BFR:BFR+cfg.NZ, BFR:BFR+cfg.NX]
    residual = float(np.max(np.abs(interior_new - interior_old)))
    print('   max |field_new - field_old| (interior) = {:.3e}'.format(residual))
    threshold = 1e-12
    ok = residual < threshold
    print('   {} (threshold {:.0e})'.format('PASS' if ok else 'FAIL', threshold))
    return ok


# ---------------------------------------------------------------
# Test 2: Linear analytic across GAMMA change
# ---------------------------------------------------------------
def test_linear_analytic():
    print('\n=== Test 2: Linear analytic f = 10y + 100z + 0.5x (GAMMA change) ===')
    OLD = GridConfig(nx=16, ny=33, nz=33, jp=4, gamma=2.0, alpha=0.5,
                     grid_dat='<synthetic_old>')
    NEW = GridConfig(nx=16, ny=33, nz=33, jp=4, gamma=3.0, alpha=0.5,
                     grid_dat='<synthetic_new>')
    y2d_old, z2d_old = build_synthetic_grid_2d(OLD)
    y2d_new, z2d_new = build_synthetic_grid_2d(NEW)
    dx_old = LX / (OLD.NX - 1)
    dx_new = LX / (NEW.NX - 1)

    # OLD field = analytic linear
    field_old = build_linear_field(OLD, y2d_old, z2d_old, dx_old)

    # Phys interp to NEW grid
    mapping = precompute_phys_mapping_2d(y2d_old, z2d_old, y2d_new, z2d_new, OLD, NEW)
    field_new = interpolate_phys_3d_with_mapping(field_old, mapping)

    # Expected: same analytic formula evaluated at NEW physical coords
    expected = build_linear_field(NEW, y2d_new, z2d_new, dx_new)

    interior_actual   = field_new[BFR:BFR+NEW.NY, BFR:BFR+NEW.NZ, BFR:BFR+NEW.NX]
    interior_expected = expected[BFR:BFR+NEW.NY,  BFR:BFR+NEW.NZ, BFR:BFR+NEW.NX]
    residual = float(np.max(np.abs(interior_actual - interior_expected)))
    print('   GAMMA: OLD={} -> NEW={}'.format(OLD.GAMMA, NEW.GAMMA))
    print('   max |field_new - f(x,y,z)| (interior) = {:.3e}'.format(residual))
    threshold = 1e-10
    ok = residual < threshold
    print('   {} (threshold {:.0e})'.format('PASS' if ok else 'FAIL', threshold))
    return ok


def test_linear_analytic_with_nx_change():
    print('\n=== Test 3: Linear analytic with GAMMA + NX change ===')
    OLD = GridConfig(nx=16, ny=33, nz=33, jp=4, gamma=2.0, alpha=0.5,
                     grid_dat='<synthetic_old>')
    NEW = GridConfig(nx=23, ny=33, nz=33, jp=4, gamma=3.0, alpha=0.5,
                     grid_dat='<synthetic_new>')
    y2d_old, z2d_old = build_synthetic_grid_2d(OLD)
    y2d_new, z2d_new = build_synthetic_grid_2d(NEW)
    field_old = build_linear_field(OLD, y2d_old, z2d_old, LX / (OLD.NX - 1))

    mapping = precompute_phys_mapping_2d(y2d_old, z2d_old, y2d_new, z2d_new, OLD, NEW)
    field_new = interpolate_phys_3d_with_mapping(field_old, mapping)
    expected = build_linear_field(NEW, y2d_new, z2d_new, LX / (NEW.NX - 1))

    interior_actual = field_new[BFR:BFR+NEW.NY, BFR:BFR+NEW.NZ, BFR:BFR+NEW.NX]
    interior_expected = expected[BFR:BFR+NEW.NY, BFR:BFR+NEW.NZ, BFR:BFR+NEW.NX]
    residual = float(np.max(np.abs(interior_actual - interior_expected)))
    print('   GAMMA: OLD={} -> NEW={}, NX: OLD={} -> NEW={}'.format(
        OLD.GAMMA, NEW.GAMMA, OLD.NX, NEW.NX))
    print('   max |field_new - f(x,y,z)| (interior) = {:.3e}'.format(residual))
    threshold = 1e-10
    ok = residual < threshold
    print('   {} (threshold {:.0e})'.format('PASS' if ok else 'FAIL', threshold))
    return ok


if __name__ == '__main__':
    results = []
    results.append(('Test 1: Identity', test_identity()))
    results.append(('Test 2: Linear analytic across GAMMA', test_linear_analytic()))
    results.append(('Test 3: Linear analytic with NX change',
                    test_linear_analytic_with_nx_change()))
    print('\n=== Summary ===')
    for name, ok in results:
        print('   {:<45}  {}'.format(name, 'PASS' if ok else 'FAIL'))
    sys.exit(0 if all(ok for _, ok in results) else 1)
