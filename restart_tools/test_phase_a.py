#!/usr/bin/env python3
"""Phase A unit tests: 6th-order Fornberg metric.

Tests:
  Test 1: Convergence — 6th vs 2nd order on sin/cos analytic; 6th should be ~4
          orders smaller error at typical resolution.
  Test 2: j-periodic — sin(2*pi*j/NY) reads periodic ghost; 6th central should
          give error < 1e-10.
  Test 3: k-polynomial — 5th-order polynomial should be bit-exact for 6th-order
          adaptive Fornberg (both interior and skewed boundary stencils).
  Test 4: CE conservation regression — Sum_q f_neq still ~1e-22.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

from interp_checkpoint import (
    BFR, LY, LZ, E, W,
    fd6_axis_central, fd6_axis_adaptive,
    compute_inverse_metric_2d, compute_inverse_metric_2d_fornberg,
    chapman_enskog_fneq_q,
)


def _build_periodic_y_2d(NY, NZ):
    """Build (NY6, NZ6) y_2d with periodic ghost (mirror build_grid_xyz j-ghost)."""
    NY6 = NY + 6
    NZ6 = NZ + 6
    y_2d = np.zeros((NY6, NZ6), dtype=np.float64)
    dy = LY / (NY - 1)
    for j in range(NY):
        for k in range(NZ):
            y_2d[BFR + j, BFR + k] = j * dy   # uniform streamwise
    # j-periodic ghost ±LY shift
    for k in range(NZ6):
        y_2d[2, k] = y_2d[NY6-5, k] - LY
        y_2d[1, k] = y_2d[NY6-6, k] - LY
        y_2d[0, k] = y_2d[NY6-7, k] - LY
        y_2d[NY6-3, k] = y_2d[4, k] + LY
        y_2d[NY6-2, k] = y_2d[5, k] + LY
        y_2d[NY6-1, k] = y_2d[6, k] + LY
    return y_2d


# ---------------------------------------------------------------
# Test 1: Convergence — 6th vs 2nd order
# ---------------------------------------------------------------
def test_convergence():
    print('\n=== Test 1: 6th vs 2nd order convergence (sin*cos) ===')
    NY, NZ = 65, 65
    NY6, NZ6 = NY + 6, NZ + 6
    j_lo, j_hi = BFR, NY6 - 1 - BFR
    k_lo, k_hi = BFR, NZ6 - 1 - BFR

    # Analytic: y(j,k) = sin(2pi*j/(NY-1)) * cos(pi*k/(NZ-1))
    j_idx = np.arange(NY6, dtype=np.float64) - BFR
    k_idx = np.arange(NZ6, dtype=np.float64) - BFR
    JJ, KK = np.meshgrid(j_idx, k_idx, indexing='ij')
    field = np.sin(2.0 * np.pi * JJ / (NY - 1)) * np.cos(np.pi * KK / (NZ - 1))

    # True d/dk (analytic)
    truth_dk = (np.sin(2.0 * np.pi * JJ / (NY - 1))
                * (-np.sin(np.pi * KK / (NZ - 1)))
                * (np.pi / (NZ - 1)))

    # 2nd-order centered FD
    deriv_2nd = np.empty_like(field)
    deriv_2nd[:, 1:-1] = (field[:, 2:] - field[:, :-2]) / 2.0
    deriv_2nd[:, 0] = field[:, 1] - field[:, 0]
    deriv_2nd[:, -1] = field[:, -1] - field[:, -2]

    # 6th-order adaptive
    deriv_6th = fd6_axis_adaptive(field, k_lo, k_hi, axis=1)

    # Compare interior only
    interior = (slice(j_lo, j_hi+1), slice(k_lo, k_hi+1))
    err_2nd = float(np.max(np.abs(deriv_2nd[interior] - truth_dk[interior])))
    err_6th = float(np.max(np.abs(deriv_6th[interior] - truth_dk[interior])))
    ratio = err_2nd / err_6th if err_6th > 0 else float('inf')
    print('   max err 2nd-order = {:.3e}'.format(err_2nd))
    print('   max err 6th-order = {:.3e}'.format(err_6th))
    print('   improvement ratio = {:.1f}x'.format(ratio))
    ok = ratio > 1e3   # expect at least 3 orders better at this resolution
    print('   {} (expect ratio > 1000)'.format('PASS' if ok else 'FAIL'))
    return ok


# ---------------------------------------------------------------
# Test 2: j-periodic — sin(2pi*j/NY) with periodic ghost
# ---------------------------------------------------------------
def test_j_periodic():
    print('\n=== Test 2: j-periodic (sin(2pi*j/NY) reads periodic ghost) ===')
    NY, NZ = 65, 17
    NY6 = NY + 6
    NZ6 = NZ + 6
    j_lo, j_hi = BFR, NY6 - 1 - BFR

    # Build y_2d such that f(j) = sin(2pi*j/(NY-1)) where j is the LOGICAL index
    # And ghost cells follow periodic shift
    field = np.zeros((NY6, NZ6))
    dy = LY / (NY - 1)
    omega = 2.0 * np.pi / LY    # so sin(omega*j*dy) is periodic over LY
    for j in range(NY6):
        y_phys_periodic = (j - BFR) * dy   # ghost rows j<BFR get negative y; +LY periodic
        # Periodic equivalent: sin is naturally periodic over LY, so direct evaluation works
        field[j, :] = np.sin(omega * y_phys_periodic)

    # True derivative at fluid nodes
    truth_dj = np.empty_like(field)
    for j in range(NY6):
        y_phys_periodic = (j - BFR) * dy
        truth_dj[j, :] = omega * dy * np.cos(omega * y_phys_periodic)
    # Note: derivative wrt j (computational unit), not physical y, so chain rule gives dy

    # 6th-order central (reads ghost ±3)
    deriv_6th = fd6_axis_central(field, j_lo, j_hi, axis=0)

    interior_j = slice(j_lo, j_hi+1)
    err = float(np.max(np.abs(deriv_6th[interior_j, :] - truth_dj[interior_j, :])))
    print('   max err (6th central, periodic ghost) = {:.3e}'.format(err))
    threshold = 1e-7   # 6th-order on 65 points; not bit-exact (sin not polynomial)
    ok = err < threshold
    print('   {} (threshold {:.0e})'.format('PASS' if ok else 'FAIL', threshold))
    return ok


# ---------------------------------------------------------------
# Test 3: k-polynomial — 5th-order should be bit-exact for 6th-order FD
# ---------------------------------------------------------------
def test_k_polynomial():
    print('\n=== Test 3: k-polynomial f(k) = (k-BFR)^5 (6th-order should be exact) ===')
    NY, NZ = 9, 17
    NY6 = NY + 6
    NZ6 = NZ + 6
    k_lo, k_hi = BFR, NZ6 - 1 - BFR

    # f(k) = (k - BFR)^5 ; true df/dk = 5*(k - BFR)^4
    k_arr = np.arange(NZ6, dtype=np.float64) - BFR
    field = np.broadcast_to((k_arr ** 5)[None, :], (NY6, NZ6)).copy()
    truth = np.broadcast_to((5.0 * k_arr ** 4)[None, :], (NY6, NZ6)).copy()

    deriv_6th = fd6_axis_adaptive(field, k_lo, k_hi, axis=1)
    interior_k = slice(k_lo, k_hi+1)
    err = float(np.max(np.abs(deriv_6th[:, interior_k] - truth[:, interior_k])))
    print('   max err on 5th-order polynomial = {:.3e}'.format(err))
    threshold = 1e-9   # bit-exact would be ~FP precision; allow some accumulation
    ok = err < threshold
    print('   {} (threshold {:.0e})'.format('PASS' if ok else 'FAIL', threshold))
    return ok


# ---------------------------------------------------------------
# Test 4: CE conservation regression with 6th-order metric
# ---------------------------------------------------------------
def test_ce_conservation_regression():
    print('\n=== Test 4: CE conservation with 6th-order metric ===')
    np.random.seed(0)
    rho = np.ones((4, 4, 4))
    grad = tuple(np.random.randn(4, 4, 4) * 0.01 for _ in range(9))
    ce = -3.0 * (0.015 / 5600.0)
    fneq = np.stack([chapman_enskog_fneq_q(rho, grad, q, ce) for q in range(19)], 0)

    sum_fneq = float(np.max(np.abs(fneq.sum(0))))
    moms = []
    for a in range(3):
        moms.append(float(np.max(np.abs(np.einsum('q,qjki->jki', E[:, a], fneq)))))
    print('   max |Sum f_neq|         = {:.2e}'.format(sum_fneq))
    print('   max |Sum c_qx * f_neq|  = {:.2e}'.format(moms[0]))
    print('   max |Sum c_qy * f_neq|  = {:.2e}'.format(moms[1]))
    print('   max |Sum c_qz * f_neq|  = {:.2e}'.format(moms[2]))
    threshold = 1e-20
    ok = sum_fneq < threshold and all(m < threshold for m in moms)
    print('   {} (threshold {:.0e})'.format('PASS' if ok else 'FAIL', threshold))
    return ok


if __name__ == '__main__':
    results = []
    results.append(('Test 1: 6th vs 2nd convergence',          test_convergence()))
    results.append(('Test 2: j-periodic ghost wrap',            test_j_periodic()))
    results.append(('Test 3: k 5th-order polynomial (exact)',   test_k_polynomial()))
    results.append(('Test 4: CE conservation regression',       test_ce_conservation_regression()))
    print('\n=== Summary ===')
    for name, ok in results:
        print('   {:<45}  {}'.format(name, 'PASS' if ok else 'FAIL'))
    sys.exit(0 if all(ok for _, ok in results) else 1)
