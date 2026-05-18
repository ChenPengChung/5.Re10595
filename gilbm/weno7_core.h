#ifndef GILBM_WENO7_CORE_H
#define GILBM_WENO7_CORE_H

#include <math.h>

#ifdef __CUDACC__
#define GILBM_WENO7_INLINE __host__ __device__ __forceinline__
#else
#define GILBM_WENO7_INLINE inline
#endif

// ========================================================================
// WENO7-Z Point-Value Interpolation (7-point stencil, 4 cubic sub-stencils)
// ========================================================================
//
// Stencil: {f_{-3}, f_{-2}, f_{-1}, f_0, f_{+1}, f_{+2}, f_{+3}}
//   σ > 0: departure to the right of center (toward f_{+1})
//   σ < 0: departure to the left  of center (toward f_{-1})
//
// 4 sub-stencils (each 4 points, cubic polynomial):
//   S₀ = {f_{-3}, f_{-2}, f_{-1}, f_0 }
//   S₁ = {f_{-2}, f_{-1}, f_0,    f_{+1}}
//   S₂ = {f_{-1}, f_0,    f_{+1}, f_{+2}}
//   S₃ = {f_0,    f_{+1}, f_{+2}, f_{+3}}
//
// Linear weights d₀..d₃ are σ-dependent cubics chosen so that
//   d₀·p₀(σ) + d₁·p₁(σ) + d₂·p₂(σ) + d₃·p₃(σ) ≡ Lagrange-7(σ)
// → WENO7(linear weights) = Lagrange-7 (6th-degree polynomial, 7th-order)
//
// Convex interval: σ ∈ [-1, 1]  — all d_k ≥ 0
//   At σ = ±1: one weight is exactly 0 (d₃ or d₀), others > 0
//   Outside [-1, 1]: one or more d_k < 0 → use linear weights directly
//
// ===== Stretched-Grid-Aware Adaptive Activation (NEW) =====
//
// Problem: On stretched grids (e.g. Fröhlich tanh, 56.5× ratio),
//   standard smoothness indicators β_k assume uniform spacing.
//   Left and right sub-stencils span very different physical distances,
//   causing |β₀ - β₃| >> 0 even for smooth data → false WENO activation.
//
// Solution: 3-level adaptive strategy:
//   (1) Stretching-scaled gate: threshold × max(1, R²)
//       R = stretch_factor = max(Δz)/min(Δz) over the 7-point stencil
//   (2) WENO-Z weights (Borges et al. 2008): α_k = d_k(1 + (τ/β_k)²)
//       Scale-invariant: τ/β ratio cancels common grid-stretching factors
//   (3) Standard τ₇ = |β₀ - β₃| (Borges et al. 2008)
//       Smooth: τ₇ = O(Δx⁵), combined with R²-scaled gate → robust discrimination
//
// Result:
//   - Smooth regions on ANY grid → gate blocks nonlinear → pure Lagrange-7
//   - Hill crest / separation with genuine oscillations → WENO-Z suppresses
//   - Intermediate regions → WENO-Z with gentle, scale-invariant correction
//
// Derived with SymPy from Lagrange point-value interpolation.
// Smoothness indicators: ∫_{-1/2}^{1/2} Σ_{l=1}^{3} (d^l p_k / dx^l)² dx
// ========================================================================

namespace gilbm_weno7 {

static const double kStencilSigmaMin = -3.0;
static const double kStencilSigmaMax =  3.0;
static const double kConvexSigmaMin  = -1.0;
static const double kConvexSigmaMax  =  1.0;
// ── β normalization factor ───────────────────────────────────────────
// Taylor expansion of β_k for our point-value interpolation formulation:
//   β_k = γ₁·(f')² + (13/12)·(f'')² + (1/12)·f'·f''' + O(higher derivatives)
// where γ₁ = 1 for point-value interpolation in σ-space (Δσ = 1).
//
// IMPORTANT: Shen-Zha (2010) reports γ₁ = 240 for WENO7, but that value
// applies to RECONSTRUCTION from cell-averages with explicit Δx scaling:
//   β_JS = Σ Δx^{2l-1} ∫ (d^l q/dx^l)² dx
// Our formulation is INTERPOLATION through point values in computational
// coordinates (σ-space, Δσ=1), where the Δx^{2l-1} factors cancel:
//   β_ours = ∫_{-1/2}^{+1/2} Σ_{l=1}^{3} (d^l p_k/dσ^l)² dσ
// SymPy verification confirms γ₁ = 1 for all 4 sub-stencils.
//
// Since γ₁ = 1, normalization is a no-op. We keep the factor as 1.0
// for documentation and potential future use with different β definitions.
static const double kBetaNormFactor  =  1.0;     // γ₁ = 1 for point-value interpolation
static const double kDefaultEpsilon  =  1.0e-6;  // ε (standard, no normalization needed)

// ── WENO-Z power parameter q ────────────────────────────────────────
// α_k = d_k × (1 + (τ₇/(β_k+ε))^q)
//   q = 1: minimal dissipation, but critical points (f'=0,f''≠0) drop to 6th order
//   q = 2: full 7th order even at critical points (Shen-Zha 2010 Eq.25)
// For Periodic Hill turbulence (strong shear layers with many critical points),
// q = 2 is the correct choice. Change to 1 only for smoother flows.
#define GILBM_WENO7_Q 2
static const int kWENOZ_q = GILBM_WENO7_Q;

// ── Adaptive activation threshold (base value for uniform grid) ─────
// τ₇ = |β₀ - β₃| measures left-right asymmetry across the full stencil.
// When τ₇_norm / β_ref_norm < threshold_adapted → use linear weights (= Lagrange-7)
// threshold_adapted = kActivationThreshold * max(1, stretch_factor²)
//
// Since kBetaNormFactor = 1 (no-op for point-value interpolation),
// the threshold operates directly on raw β values.
static const double kActivationThreshold = 0.5;

struct LinearWeights       { double d0, d1, d2, d3; };
struct SmoothnessIndicators{ double beta0, beta1, beta2, beta3; };
struct CandidatePolynomials{ double p0, p1, p2, p3; };
struct NonlinearWeights    { double w0, w1, w2, w3; bool used_nonlinear; };
struct InterpResult        { double value; bool used_nonlinear; };

// ── Clamp σ to stencil range ─────────────────────────────────────────
GILBM_WENO7_INLINE double clamp_sigma_to_stencil(double sigma) {
    return sigma < kStencilSigmaMin ? kStencilSigmaMin
         : sigma > kStencilSigmaMax ? kStencilSigmaMax
         : sigma;
}

// ── Check if σ is in convex interval ─────────────────────────────────
// 使用閉區間 [-1, 1]：σ = ±1 時恰好一個 d_k = 0（d₃ or d₀），
// 其餘 d_k > 0 → 仍為有效凸組合，WENO 非線性公式可正常運作。
// 這使得近壁層 k=4,5 的 e_z=±1 方向（σ = ±1）也能啟動 WENO。
GILBM_WENO7_INLINE bool sigma_is_in_convex_interval(double sigma) {
    return sigma >= kConvexSigmaMin && sigma <= kConvexSigmaMax;
}

// ── Linear weights d₀..d₃ (σ-dependent cubics) ──────────────────────
// d₀ = (-σ³ + 6σ² - 11σ + 6) / 120  = ((-1/120)s + 1/20)s - 11/120)s + 1/20
// d₁ = ( 3σ³ - 6σ² - 27σ + 54) / 120
// d₂ = (-3σ³ - 6σ² + 27σ + 54) / 120
// d₃ = ( σ³ + 6σ² + 11σ + 6) / 120
//
// Symmetry: d₃(σ) = d₀(-σ), d₂(σ) = d₁(-σ)
// At σ=0: d₀=d₃=1/20, d₁=d₂=9/20
GILBM_WENO7_INLINE LinearWeights compute_linear_weights(double s) {
    LinearWeights d;
    // Horner form for each cubic
    d.d0 = ((-0.00833333333333333333 * s + 0.05) * s - 0.09166666666666666667) * s + 0.05;
    d.d1 = (( 0.025                  * s - 0.05) * s - 0.225                 ) * s + 0.45;
    d.d2 = ((-0.025                  * s - 0.05) * s + 0.225                 ) * s + 0.45;
    d.d3 = (( 0.00833333333333333333 * s + 0.05) * s + 0.09166666666666666667) * s + 0.05;
    return d;
}
//上述線性權重作為可返為lagranbge內插多項式的工具尚未開啟非線性權重季ˋ算
// ── Candidate cubic polynomials p₀..p₃ ──────────────────────────────
// Each pₖ(σ) is a 4-point Lagrange interpolant through its sub-stencil.
//
// Coefficients (Horner form in σ, multiply by corresponding stencil value):
//
// p₀(s) = um3·c₀₀(s) + um2·c₀₁(s) + um1·c₀₂(s) + u0·c₀₃(s)
//   c₀₀ = ((-1/6)s - 1/2)s - 1/3)s + 0
//   c₀₁ = (( 1/2)s + 2  )s + 3/2)s + 0
//   c₀₂ = ((-1/2)s - 5/2)s - 3  )s + 0
//   c₀₃ = (( 1/6)s + 1  )s + 11/6)s + 1
//
// p₃(s) = p₀(-s) with um3↔up3, um2↔up2, um1↔up1 (mirror symmetry)
GILBM_WENO7_INLINE CandidatePolynomials compute_candidate_polynomials(
    double s,
    double um3, double um2, double um1, double u0,
    double up1, double up2, double up3
) {
    CandidatePolynomials p;

    // p₀: sub-stencil S₀ = {um3, um2, um1, u0}
    const double c00 = ((-0.16666666666666667 * s - 0.5 ) * s - 0.33333333333333333) * s;
    const double c01 = (( 0.5                 * s + 2.0 ) * s + 1.5               ) * s;
    const double c02 = ((-0.5                 * s - 2.5 ) * s - 3.0               ) * s;
    const double c03 = (( 0.16666666666666667 * s + 1.0 ) * s + 1.83333333333333333) * s + 1.0;
    p.p0 = um3 * c00 + um2 * c01 + um1 * c02 + u0 * c03;

    // p₁: sub-stencil S₁ = {um2, um1, u0, up1}
    const double c10 = ((-0.16666666666666667 * s + 0.0 ) * s + 0.16666666666666667) * s;
    const double c11 = (( 0.5                 * s + 0.5 ) * s - 1.0               ) * s;
    const double c12 = ((-0.5                 * s - 1.0 ) * s + 0.5               ) * s + 1.0;
    const double c13 = (( 0.16666666666666667 * s + 0.5 ) * s + 0.33333333333333333) * s;
    p.p1 = um2 * c10 + um1 * c11 + u0 * c12 + up1 * c13;

    // p₂: sub-stencil S₂ = {um1, u0, up1, up2}
    const double c20 = ((-0.16666666666666667 * s + 0.5 ) * s - 0.33333333333333333) * s;
    const double c21 = (( 0.5                 * s - 1.0 ) * s - 0.5               ) * s + 1.0;
    const double c22 = ((-0.5                 * s + 0.5 ) * s + 1.0               ) * s;
    const double c23 = (( 0.16666666666666667 * s + 0.0 ) * s - 0.16666666666666667) * s;
    p.p2 = um1 * c20 + u0 * c21 + up1 * c22 + up2 * c23;

    // p₃: sub-stencil S₃ = {u0, up1, up2, up3}
    const double c30 = ((-0.16666666666666667 * s + 1.0 ) * s - 1.83333333333333333) * s + 1.0;
    const double c31 = (( 0.5                 * s - 2.5 ) * s + 3.0               ) * s;
    const double c32 = ((-0.5                 * s + 2.0 ) * s - 1.5               ) * s;
    const double c33 = (( 0.16666666666666667 * s - 0.5 ) * s + 0.33333333333333333) * s;
    p.p3 = u0 * c30 + up1 * c31 + up2 * c32 + up3 * c33;

    return p;
}

// ── Pure linear 7-point combination ─────────────────────────────────
// This is the exact 7-point Lagrange interpolant written in WENO7 form:
//   I_lin = d0*p0 + d1*p1 + d2*p2 + d3*p3
// It reproduces any polynomial of degree <= 6 exactly.
GILBM_WENO7_INLINE double evaluate_linear_combination(
    double sigma,
    double um3, double um2, double um1, double u0,
    double up1, double up2, double up3
) {
    const LinearWeights d = compute_linear_weights(sigma);
    const CandidatePolynomials p = compute_candidate_polynomials(
        sigma, um3, um2, um1, u0, up1, up2, up3
    );
    return d.d0 * p.p0 + d.d1 * p.p1 + d.d2 * p.p2 + d.d3 * p.p3;
}

// ── Smoothness indicators β₀..β₃ ────────────────────────────────────
// β_k = ∫_{-1/2}^{1/2} [ (p'_k)² + (p''_k)² + (p'''_k)² ] dx
// These are σ-independent (depend only on stencil values).
//
// Computed via SymPy point-value Lagrange polynomials.
// Each β_k is a positive semi-definite bilinear form in 4 stencil values.
GILBM_WENO7_INLINE SmoothnessIndicators compute_smoothness_indicators(
    double um3, double um2, double um1, double u0,
    double up1, double up2, double up3
) {
    SmoothnessIndicators beta;

    // β₀: S₀ = {um3, um2, um1, u0}
    beta.beta0 = um3 * (  2.30868055555556 * um3
                        - 16.39375          * um2
                        + 19.68541666666667 * um1
                        -  7.90902777777778 * u0)
               + um2 * ( 29.73645833333333 * um2
                        - 73.01458333333333 * um1
                        + 29.93541666666667 * u0)
               + um1 * ( 46.61145833333333 * um1
                        - 39.89375          * u0)
               + u0  * (  8.93368055555556 * u0);

    // β₁: S₁ = {um2, um1, u0, up1}
    beta.beta1 = um2 * (  1.10034722222222 * um2
                        -  6.72708333333333 * um1
                        +  6.60208333333333 * u0
                        -  2.07569444444444 * up1)
               + um1 * ( 11.61145833333333 * um1
                        - 24.51458333333333 * u0
                        +  8.01875          * up1)
               + u0  * ( 14.23645833333333 * u0
                        - 10.56041666666667 * up1)
               + up1 * (  2.30868055555556 * up1);

    // β₂: S₂ = {um1, u0, up1, up2}
    beta.beta2 = um1 * (  2.30868055555556 * um1
                        - 10.56041666666667 * u0
                        +  8.01875          * up1
                        -  2.07569444444444 * up2)
               + u0  * ( 14.23645833333333 * u0
                        - 24.51458333333333 * up1
                        +  6.60208333333333 * up2)
               + up1 * ( 11.61145833333333 * up1
                        -  6.72708333333333 * up2)
               + up2 * (  1.10034722222222 * up2);

    // β₃: S₃ = {u0, up1, up2, up3}
    beta.beta3 = u0  * (  8.93368055555556 * u0
                        - 39.89375          * up1
                        + 29.93541666666667 * up2
                        -  7.90902777777778 * up3)
               + up1 * ( 46.61145833333333 * up1
                        - 73.01458333333333 * up2
                        + 19.68541666666667 * up3)
               + up2 * ( 29.73645833333333 * up2
                        - 16.39375          * up3)
               + up3 * (  2.30868055555556 * up3);

    return beta;
}

// ── Nonlinear weights: WENO-Z with stretching-adaptive gate ──────────
//
// Gate 1: σ outside convex interval [-1,1] → linear weights (Lagrange-7)
//
// Gate 2: Stretching-adaptive activation
//   τ₇ = |β₀ - β₃|  (Borges 2008, 1st-order difference across full stencil)
//   Smooth: τ₇ = O(Δx⁵), Oscillatory: τ₇ = O(1)
//   Note: 3rd-order difference |β₀-3β₁+3β₂-β₃| was tested but REVERTED —
//         it amplifies non-uniform spacing artifacts, making gate LESS selective.
//   threshold_adapted = kActivationThreshold × max(1, R²)
//   where R = stretch_factor = max(Δz)/min(Δz) over stencil (1.0 for uniform)
//   If τ₇ < threshold_adapted × (β_ref + ε) → use linear weights
//
// Gate 3: WENO-Z nonlinear weights (Borges et al. 2008)
//   α_k = d_k × (1 + (τ₇/(β_k + ε))²)
//   ω_k = α_k / Σα_k
//   Scale-invariant: the ratio τ₇/β_k cancels common scaling from grid stretching
//
// Parameters:
//   stretch_factor: max(Δz)/min(Δz) across the 7-point stencil physical spacing
//                   = 1.0 for uniform grids (default, backward-compatible)
//                   > 1.0 for stretched grids → raises activation threshold
//
GILBM_WENO7_INLINE NonlinearWeights compute_nonlinear_weights(
    double sigma,
    double um3, double um2, double um1, double u0,
    double up1, double up2, double up3,
    double epsilon = kDefaultEpsilon,
    double stretch_factor = 1.0
) {
    NonlinearWeights w;
    const LinearWeights d = compute_linear_weights(sigma);

    // Gate 1: outside convex interval → linear weights
    if (!sigma_is_in_convex_interval(sigma)) {
        w.w0 = d.d0; w.w1 = d.d1; w.w2 = d.d2; w.w3 = d.d3;
        w.used_nonlinear = false;
        return w;
    }

    // Gate 2: stretching-adaptive activation
    const SmoothnessIndicators beta = compute_smoothness_indicators(
        um3, um2, um1, u0, up1, up2, up3
    );

    // β normalization (kBetaNormFactor = 1.0 for point-value interpolation).
    // For our formulation, γ₁ = 1, so this is a no-op.
    // Kept for structural clarity and potential future use.
    const double inv_norm = 1.0 / kBetaNormFactor;
    const double bn0 = beta.beta0 * inv_norm;
    const double bn1 = beta.beta1 * inv_norm;
    const double bn2 = beta.beta2 * inv_norm;
    const double bn3 = beta.beta3 * inv_norm;

    // Global smoothness indicator (Borges et al. 2008, WENO-Z):
    //   τ₇ = |β'₀ - β'₃|  (using normalized β')
    // Measures left-right asymmetry across the full stencil.
    // Smooth data: τ₇ = O(Δx⁵) → τ₇/β'_k → 0 → recovers linear weights
    // Oscillatory data: τ₇ = O(1) → triggers nonlinear suppression
    const double tau7 = fabs(bn0 - bn3);

    const double beta_ref = (bn0 + bn1 + bn2 + bn3) * 0.25;

    // Stretching-scaled threshold:
    //   R = stretch_factor (= max(Δz)/min(Δz) over stencil)
    //   On uniform grid (R=1): threshold = kActivationThreshold (original behavior)
    //   On stretched grid (R>>1): threshold ∝ R² → much harder to activate WENO
    //   Physical meaning: the β difference from grid stretching scales as R²,
    //   so we raise the threshold by the same factor to compensate.
    //   NOTE: The ratio τ₇/β_ref is invariant under β normalization (÷240 cancels),
    //   so the threshold value 0.5 needs no recalibration.
    const double R2 = stretch_factor * stretch_factor;
    const double threshold_adapted = kActivationThreshold * (R2 > 1.0 ? R2 : 1.0);

    if (tau7 < threshold_adapted * (beta_ref + epsilon)) {
        w.w0 = d.d0; w.w1 = d.d1; w.w2 = d.d2; w.w3 = d.d3;
        w.used_nonlinear = false;
        return w;
    }

    // Gate 3: WENO-Z nonlinear weights (Borges et al. 2008)
    //
    //   α_k = d_k × (1 + (τ₇ / (β_k + ε))^q)
    //   q = kWENOZ_q (default 2, configurable)
    //
    // Why WENO-Z instead of WENO-JS:
    //   - WENO-JS: α_k = d_k/(β_k+ε)²  → weights depend on ABSOLUTE β values
    //     On a stretched grid, all β are large → all α are small → numerical issues
    //   - WENO-Z: uses the RATIO τ₇/β_k, which is dimensionless and scale-invariant
    //     If all β scale by factor C from stretching, τ₇ also scales by ≈C → ratio stable
    //   - Smooth regions: τ₇/(β_k+ε) → 0 ⇒ α_k ≈ d_k ⇒ ω_k ≈ d_k (optimal order)
    //   - Oscillatory sub-stencil: β_k >> others ⇒ τ₇/β_k small ⇒ α_k ≈ d_k
    //     but τ₇/β_other >> 1 ⇒ α_other >> d_other → good sub-stencils dominate
    //
    const double eb0 = bn0 + epsilon;
    const double eb1 = bn1 + epsilon;
    const double eb2 = bn2 + epsilon;
    const double eb3 = bn3 + epsilon;

    // Compute (τ₇/(β'_k+ε))^q with compile-time q selection (no runtime branch)
    double a0, a1, a2, a3;
#if GILBM_WENO7_Q == 2
    {
        // q=2: full 7th order at critical points (Shen-Zha Eq.25)
        const double tau7_sq = tau7 * tau7;
        a0 = d.d0 * (1.0 + tau7_sq / (eb0 * eb0));
        a1 = d.d1 * (1.0 + tau7_sq / (eb1 * eb1));
        a2 = d.d2 * (1.0 + tau7_sq / (eb2 * eb2));
        a3 = d.d3 * (1.0 + tau7_sq / (eb3 * eb3));
    }
#else
    {
        // q=1: less dissipation, 6th order at critical points
        a0 = d.d0 * (1.0 + tau7 / eb0);
        a1 = d.d1 * (1.0 + tau7 / eb1);
        a2 = d.d2 * (1.0 + tau7 / eb2);
        a3 = d.d3 * (1.0 + tau7 / eb3);
    }
#endif
    const double sum = a0 + a1 + a2 + a3;

    if (sum <= 0.0) {
        w.w0 = d.d0; w.w1 = d.d1; w.w2 = d.d2; w.w3 = d.d3;
        w.used_nonlinear = false;
        return w;
    }
    //將抑制蕩蕩的內插權重歸一化
    const double inv_sum = 1.0 / sum;
    w.w0 = a0 * inv_sum;
    w.w1 = a1 * inv_sum;
    w.w2 = a2 * inv_sum;
    w.w3 = a3 * inv_sum;
    w.used_nonlinear = true;
    return w;
}

// ── Main interpolation function ──────────────────────────────────────
// Returns the WENO7-Z interpolated value at position σ.
// With linear weights: identical to 7-point Lagrange (6th degree, 7th order).
// With nonlinear weights: oscillation-suppressing, still up to 7th order in smooth regions.
//
// stretch_factor: grid stretching ratio max(Δz)/min(Δz) over the stencil.
//   = 1.0 for uniform grids (default, backward-compatible with old behavior)
//   > 1.0 for stretched grids (raises WENO activation threshold)
//
GILBM_WENO7_INLINE double interpolate_point_value(
    double sigma,
    double um3, double um2, double um1, double u0,
    double up1, double up2, double up3,
    double epsilon = kDefaultEpsilon,
    double stretch_factor = 1.0
) {
    sigma = clamp_sigma_to_stencil(sigma);

    const CandidatePolynomials p = compute_candidate_polynomials(
        sigma, um3, um2, um1, u0, up1, up2, up3
    );
    const NonlinearWeights w = compute_nonlinear_weights(
        sigma, um3, um2, um1, u0, up1, up2, up3,
        epsilon, stretch_factor
    );

    return w.w0 * p.p0 + w.w1 * p.p1 + w.w2 * p.p2 + w.w3 * p.p3;
}

// ── Interpolation with diagnostic output ────────────────────────────
// Same as interpolate_point_value, but also returns whether WENO
// nonlinear weights were used.  This avoids calling
// compute_nonlinear_weights twice when diagnostics are enabled.
//
// Usage (USE_WENO7 diagnostics):
//   InterpResult r = interpolate_point_value_diag(sigma, ...);
//   f_streamed[q] = r.value;
//   if (r.used_nonlinear) { /* update counters */ }
//
GILBM_WENO7_INLINE InterpResult interpolate_point_value_diag(
    double sigma,
    double um3, double um2, double um1, double u0,
    double up1, double up2, double up3,
    double epsilon = kDefaultEpsilon,
    double stretch_factor = 1.0
) {
    sigma = clamp_sigma_to_stencil(sigma);

    const CandidatePolynomials p = compute_candidate_polynomials(
        sigma, um3, um2, um1, u0, up1, up2, up3
    );
    const NonlinearWeights w = compute_nonlinear_weights(
        sigma, um3, um2, um1, u0, up1, up2, up3,
        epsilon, stretch_factor
    );

    InterpResult r;
    r.value = w.w0 * p.p0 + w.w1 * p.p1 + w.w2 * p.p2 + w.w3 * p.p3;
    r.used_nonlinear = w.used_nonlinear;
    return r;
}

} // namespace gilbm_weno7

#endif // GILBM_WENO7_CORE_H