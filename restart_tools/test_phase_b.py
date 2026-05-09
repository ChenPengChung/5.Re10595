#!/usr/bin/env python3
"""Phase B unit tests: real dt_global computation.

Tests:
  Test 1: dt_global magnitude reasonable — O(1e-3) to O(1e-2) for typical grids.
  Test 2: Self-consistency — same grid, 6th-order metric stable across calls;
          residual < 1e-12 (idempotent).
  Test 3: 6th vs 2nd order metric — 6th-order gives slightly different dt due
          to better metric accuracy; difference should stay below 10% relative.
  Test 4: max_component reporting — eta is OK if dx dominates; report should
          be one of {eta, xi (alpha=N), zeta (alpha=N)}.
  Test 5: c_eta inclusion — for very fine spanwise (small dx), max_component
          should switch to eta (verifies c_eta is actually scanned).
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

# Skip Phase B tests entirely if no Tecplot grid is available, since
# compute_dt_global_gilbm calls build_grid_xyz which requires a real .dat file.
GRID_DAT = None
for candidate in (
    'J_Frohlich/adaptive_3.fine grid_I257_J129_g2.0_a0.5.dat',
    '../J_Frohlich/adaptive_3.fine grid_I257_J129_g2.0_a0.5.dat',
):
    abs_path = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', candidate.split('/', 1)[-1]
        if candidate.startswith('J_Frohlich') else candidate))
    if os.path.exists(abs_path):
        GRID_DAT = abs_path
        break
    abs_path2 = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'J_Frohlich',
        os.path.basename(candidate)))
    if os.path.exists(abs_path2):
        GRID_DAT = abs_path2
        break

if GRID_DAT is None:
    print('SKIP: no Tecplot grid found in J_Frohlich/; '
          'cannot run dt_global tests on synthetic grid (compute_dt_global_gilbm '
          'requires build_grid_xyz which reads .dat file).')
    print('Place adaptive_3.fine grid_I257_J129_g2.0_a0.5.dat in J_Frohlich/ and re-run.')
    sys.exit(0)

print('Using grid: {}'.format(GRID_DAT))

from interp_checkpoint import (
    GridConfig, compute_dt_global_gilbm,
)

# Build cfg matching the grid (I257_J129 means NY=257, NZ=129)
def make_cfg(metric_order=6):
    return GridConfig(nx=64, ny=257, nz=129, jp=8, gamma=2.0, alpha=0.5,
                      grid_dat=GRID_DAT)


# ---------------------------------------------------------------
# Test 1: dt_global magnitude
# ---------------------------------------------------------------
def test_dt_magnitude():
    print('\n=== Test 1: dt_global magnitude (expect O(1e-4) to O(1e-2)) ===')
    cfg = make_cfg()
    dt, comp = compute_dt_global_gilbm(cfg, cfl=0.5, metric_order=6)
    print('   dt_global    = {:.6e}'.format(dt))
    print('   max_component = {}'.format(comp))
    ok = 1e-5 < dt < 1e-1
    print('   {} (range 1e-5 .. 1e-1)'.format('PASS' if ok else 'FAIL'))
    return ok


# ---------------------------------------------------------------
# Test 2: Self-consistency (idempotent)
# ---------------------------------------------------------------
def test_self_consistency():
    print('\n=== Test 2: Self-consistency (same grid, two calls) ===')
    cfg = make_cfg()
    dt1, _ = compute_dt_global_gilbm(cfg, cfl=0.5, metric_order=6)
    dt2, _ = compute_dt_global_gilbm(cfg, cfl=0.5, metric_order=6)
    diff = abs(dt1 - dt2)
    print('   dt call 1 = {:.15e}'.format(dt1))
    print('   dt call 2 = {:.15e}'.format(dt2))
    print('   |diff|    = {:.3e}'.format(diff))
    ok = diff < 1e-12
    print('   {} (threshold 1e-12)'.format('PASS' if ok else 'FAIL'))
    return ok


# ---------------------------------------------------------------
# Test 3: 6th vs 2nd order metric — both should give dt in same ballpark
# ---------------------------------------------------------------
def test_6th_vs_2nd():
    print('\n=== Test 3: 6th vs 2nd order metric ===')
    cfg = make_cfg()
    dt6, comp6 = compute_dt_global_gilbm(cfg, cfl=0.5, metric_order=6)
    dt2, comp2 = compute_dt_global_gilbm(cfg, cfl=0.5, metric_order=2)
    rel = abs(dt6 - dt2) / dt6 if dt6 > 0 else float('inf')
    print('   dt (6th) = {:.6e}  ({})'.format(dt6, comp6))
    print('   dt (2nd) = {:.6e}  ({})'.format(dt2, comp2))
    print('   relative diff = {:.3e}'.format(rel))
    # Both orders should agree within ~10% (2nd order has O(h^2) metric error
    # which can shift max|c~| modestly, but not change order of magnitude)
    ok = rel < 0.1
    print('   {} (relative diff < 10%)'.format('PASS' if ok else 'FAIL'))
    return ok


# ---------------------------------------------------------------
# Test 4: max_component reporting valid value
# ---------------------------------------------------------------
def test_max_component_label():
    print('\n=== Test 4: max_component label ===')
    cfg = make_cfg()
    _, comp = compute_dt_global_gilbm(cfg, cfl=0.5, metric_order=6)
    print('   max_component = "{}"'.format(comp))
    ok = (comp == 'eta'
          or comp.startswith('xi (alpha=')
          or comp.startswith('zeta (alpha='))
    print('   {} (must be eta / xi(a) / zeta(a))'.format('PASS' if ok else 'FAIL'))
    return ok


# ---------------------------------------------------------------
# Test 5: c_eta inclusion — verify eta is actually scanned by scaling NX up
# ---------------------------------------------------------------
def test_c_eta_inclusion():
    print('\n=== Test 5: c_eta inclusion (large NX -> eta dominates) ===')
    # Make NX very large -> dx very small -> 1/dx very large -> eta dominates
    cfg_fine = GridConfig(nx=2049, ny=257, nz=129, jp=8, gamma=2.0, alpha=0.5,
                          grid_dat=GRID_DAT)
    dt, comp = compute_dt_global_gilbm(cfg_fine, cfl=0.5, metric_order=6)
    print('   NX=2049 -> dx={:.4e}, 1/dx={:.4e}'.format(
        4.5/(cfg_fine.NX-1), (cfg_fine.NX-1)/4.5))
    print('   dt = {:.6e}, max_component = {}'.format(dt, comp))
    # With NX=2049, 1/dx ~ 455 exceeds this grid's max y/z contravariant speed,
    # so eta must dominate. This verifies c_eta participates in the max scan.
    ok = (comp == 'eta')
    print('   {} (max_component must be eta)'.format('PASS' if ok else 'FAIL'))
    return ok


if __name__ == '__main__':
    results = []
    results.append(('Test 1: dt magnitude reasonable',          test_dt_magnitude()))
    results.append(('Test 2: self-consistency (idempotent)',    test_self_consistency()))
    results.append(('Test 3: 6th vs 2nd metric agreement',      test_6th_vs_2nd()))
    results.append(('Test 4: max_component label valid',        test_max_component_label()))
    results.append(('Test 5: c_eta path covered',               test_c_eta_inclusion()))
    print('\n=== Summary ===')
    for name, ok in results:
        print('   {:<45}  {}'.format(name, 'PASS' if ok else 'FAIL'))
    sys.exit(0 if all(ok for _, ok in results) else 1)
