#ifndef GILBM_INTERPOLATION_H
#define GILBM_INTERPOLATION_H

#include "weno7_core.h"

// ============================================================================
// WENO7 INTERPOLATION MODULE (restoring 7-point stencil with WENO)
// ============================================================================
//
// Theory: 7th-order WENO interpolation from point values
//   - 7-point stencil {u_{i-3}, ..., u_{i+3}}
//   - 4 cubic sub-stencils, sigma-dependent linear weights
//   - Nonlinear weights for shock-capturing / non-oscillatory property
//   - WENO7(linear weights) == 7-point Lagrange (proven algebraically)
//
// References (9 papers cross-verified):
//   [1] Shu (2020) Acta Numerica SS3.1      - WENO framework
//   [2] Jiang-Shu (1996)                    - beta_r smoothness indicators
//   [3] Liu-Shu-Zhang (2009)               - d_r(sigma) positivity analysis
//   [4] Sebastian-Shu (2003) Eq.2.10,2.14  - omega_r interpolation + beta_r verification
//   [5] Carrillo-Vecil (2007)              - SL + WENO framework
//   [6] Janett (2019)                       - WENO interpolation validation
//   [7] Qiu-Shu (2011)                     - non-conservative SL confirmation
//   [8] Wang et al. (2024)                  - interpolation < reconstruction
//   [9] Wilde et al. (2021)                 - SLLBM turbulence validation
//
// Memory: 7^3 = 343 per velocity per grid point (same as original Lagrange-7)
// FLOPS:  57 WENO7 calls per 3D interpolation (7x7 + 7 + 1)
// ============================================================================

// -- D3Q19 Equilibrium Distribution Function ---------------------------------
//
// Standard second-order equilibrium:
//   f_eq_q = w_q * rho * [1 + (e_q.u)/cs^2 + (e_q.u)^2/(2*cs^4) - (u.u)/(2*cs^2)]
// where cs^2 = 1/3 for standard LBM lattice.
//
__device__ __forceinline__ double compute_feq_alpha(
    int q, double rho, double u, double v, double w
) {
    double eu = GILBM_e[q][0] * u + GILBM_e[q][1] * v + GILBM_e[q][2] * w;
    double uu = u * u + v * v + w * w;
    // cs^2 = 1/3, so 1/cs^2 = 3, 1/(2*cs^4) = 4.5, 1/(2*cs^2) = 1.5
    return GILBM_W[q] * rho * (1.0 + 3.0 * eu + 4.5 * eu * eu - 1.5 * uu);
}

// -- WENO7-Z 1D Interpolation from Point Values ------------------------------
//
// Input:
//   sigma in [-3, 3]  - fractional displacement from u0
//     sigma > 0: toward up1
//     sigma < 0: toward um1
//   um3..up3          - 7 consecutive point values centered at u0
//   stretch_factor    - grid stretching ratio max(Δz)/min(Δz) over stencil
//                       = 1.0 for uniform grids (default, backward-compatible)
//                       > 1.0 for stretched grids → raises WENO activation threshold
//
// Output:
//   interpolated value at x* = x_i + sigma * dx
//
// WENO7-Z uses the convex interval sigma in (-1, 1) where d_r(sigma) >= 0.
// Outside this interval, it returns pure 7-point Lagrange (linear weights).
//
// In smooth regions: 7th-order accurate (equivalent to 7-point Lagrange)
// Near discontinuities: drops to 4th order, non-oscillatory
// On stretched grids: stretch_factor raises the activation threshold to prevent
//   false WENO activation from grid-metric-induced β asymmetry.
//
// sigma = 0   -> returns u0 exactly
// sigma = +/-1 -> returns u_{i+/-1} exactly
//
__device__ __forceinline__ double weno7_interp_1d(
    double sigma,
    double um3, double um2, double um1, double u0,
    double up1, double up2, double up3,
    double stretch_factor = 1.0
) {
    return gilbm_weno7::interpolate_point_value(
        sigma, um3, um2, um1, u0, up1, up2, up3,
        gilbm_weno7::kDefaultEpsilon, stretch_factor
    );
}

// ── WENO7 interpolation with diagnostic (used_nonlinear flag) ────────
// Same computation as weno7_interp_1d, but returns InterpResult{value, used_nonlinear}.
// Used when USE_WENO7 is enabled to avoid redundant
// compute_nonlinear_weights call.  When USE_WENO7 is off, the compiler
// eliminates this function entirely (not referenced).
//
__device__ __forceinline__ gilbm_weno7::InterpResult weno7_interp_1d_diag(
    double sigma,
    double um3, double um2, double um1, double u0,
    double up1, double up2, double up3,
    double stretch_factor = 1.0
) {
    return gilbm_weno7::interpolate_point_value_diag(
        sigma, um3, um2, um1, u0, up1, up2, up3,
        gilbm_weno7::kDefaultEpsilon, stretch_factor
    );
}

// -- Legacy: 7-point Lagrange (kept for precompute.h and fallback) ----------

#define Intrpl7(f1, a1, f2, a2, f3, a3, f4, a4, f5, a5, f6, a6, f7, a7) \
    ((f1) * (a1) + (f2) * (a2) + (f3) * (a3) + (f4) * (a4) + \
     (f5) * (a5) + (f6) * (a6) + (f7) * (a7))

// Compute 1D 7-point Lagrange interpolation coefficients.
// Nodes at integer positions 0,1,2,3,4,5,6; evaluate at position t.
// Still used by precompute.h for RK2 midpoint dxi, dzeta computation.
//
// ★ 性能優化 (2026-04): 使用共享子積 + 硬編碼倒數分母
//   消除所有除法，22 次乘法取代原始 42 mul + 42 div
//   分母 = Π_{j≠k}(k-j) for nodes {0,1,...,6}:
//     k=0:  720, k=1: -120, k=2:  48, k=3: -36
//     k=4:   48, k=5: -120, k=6: 720
//
__device__ __forceinline__ void lagrange_7point_coeffs(double t, double a[7]) {
    const double t0 = t, t1 = t - 1.0, t2 = t - 2.0, t3 = t - 3.0;
    const double t4 = t - 4.0, t5 = t - 5.0, t6 = t - 6.0;

    // 右側子積鏈: p_56, p_456, p_3456, p_23456, p_123456
    const double p56     = t5 * t6;
    const double p456    = t4 * p56;
    const double p3456   = t3 * p456;
    const double p23456  = t2 * p3456;
    const double p123456 = t1 * p23456;   // = t1*t2*t3*t4*t5*t6

    // 左側子積鏈: p_01, p_012, p_0123, p_01234, p_012345
    const double p01     = t0 * t1;
    const double p012    = p01 * t2;
    const double p0123   = p012 * t3;
    const double p01234  = p0123 * t4;
    const double p012345 = p01234 * t5;   // = t0*t1*t2*t3*t4*t5

    // 組合: 每個 a[k] = (跳過 tk 的乘積) × (1/denom_k)
    a[0] = p123456         * ( 1.0 / 720.0);   // skip t0
    a[1] = (t0 * p23456)  * (-1.0 / 120.0);   // skip t1
    a[2] = (p01 * p3456)  * ( 1.0 /  48.0);   // skip t2
    a[3] = (p012 * p456)  * (-1.0 /  36.0);   // skip t3
    a[4] = (p0123 * p56)  * ( 1.0 /  48.0);   // skip t4
    a[5] = (p01234 * t6)  * (-1.0 / 120.0);   // skip t5
    a[6] = p012345         * ( 1.0 / 720.0);   // skip t6
}

#endif