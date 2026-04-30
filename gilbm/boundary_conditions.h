#ifndef GILBM_BOUNDARY_CONDITIONS_H
#define GILBM_BOUNDARY_CONDITIONS_H

// Phase 1: Chapman-Enskog BC for GILBM (Imamura 2005 Eq. A.9, no-slip wall u=0)
//
// Direction criterion (Section 1.1.E):
//   Bottom wall k=3: e_tilde_k > 0 -> upwind point outside domain -> need C-E BC
//   Top wall k=NZ6-4: e_tilde_k < 0 -> upwind point outside domain -> need C-E BC
//
// C-E BC formula at no-slip wall (u=v=w=0), Imamura Eq.(A.9):
//   f_i|wall = w_i * rho_wall * (1 + C_i)
//
//   CE non-equilibrium (NS level): f^neq = -ρ w_i (τ-0.5)Δt / c_s² × Σ(e·e - c_s² δ) × S
//   c=1, c_s²=1/3 → 1/c_s² = 3 → tensor coeff: 3·c_{iα}·c_{iβ} - δ_{αβ}
//   α = 1~3 (x,y,z 速度分量),  β = 2~3 (ξ,ζ 方向; β=1(η) 因 dk/dx=0 消去)
//
//   C_i = -(omega - 0.5)·Δt · Σ_α Σ_{β=y,z} [3·c_{iα}·c_{iβ} - δ_{αβ}] · (∂u_α/∂x_β)
//
//   壁面 chain rule: ∂u_α/∂x_β = (du_α/dk)·(dk/dx_β)，展開 3α × 2β = 6 項：
//
//   C_i = -(omega - 0.5)·Δt × {              [= -(τ-0.5Δt), where τ-0.5Δt = 3ν]
//     ① 3·c_{ix}·c_{iy} · (du/dk)·(dk/dy)        α=x, β=y  (δ_{xy}=0)
//   + ② 3·c_{ix}·c_{iz} · (du/dk)·(dk/dz)        α=x, β=z  (δ_{xz}=0)
//   + ③ (3·c_{iy}²−1)   · (dv/dk)·(dk/dy)        α=y, β=y  (δ_{yy}=1)
//   + ④ 3·c_{iy}·c_{iz} · (dv/dk)·(dk/dz)        α=y, β=z  (δ_{yz}=0)
//   + ⑤ 3·c_{iz}·c_{iy} · (dw/dk)·(dk/dy)        α=z, β=y  (δ_{zy}=0)
//   + ⑥ (3·c_{iz}²−1)   · (dw/dk)·(dk/dz)        α=z, β=z  (δ_{zz}=1)
//   }
//
// Wall velocity gradient: 4th-order one-sided finite difference (u[wall]=0):
//   du/dk|wall = (48*u₁ - 36*u₂ + 16*u₃ - 3*u₄) / 12 + O(h⁴)
//   Bottom: u₁=u[k=4], u₂=u[k=5], u₃=u[k=6], u₄=u[k=7]
//   Top:    u₁=u[k=NZ6-5], ..., u₄=u[k=NZ6-8] (reversed sign)
//
//   Derivation: solve c₁·n + c₂·2n + c₃·3n + c₄·4n = f' with f'',f''',f'''' = 0
//   Coefficients: c = {4, -3, 4/3, -1/4} → common denominator (48,-36,16,-3)/12
//   Leading error: -(1/5)·h⁴·f⁽⁵⁾(0) (h=1 in computational space)
//
//   [v1 was 2nd-order: (4*u₁ - u₂)/2, error = -(1/3)·f'''(0), insufficient for Re≥700]
//
// Wall density: rho_wall = rho[k=3] (zero normal pressure gradient, Imamura S3.2)

// Check if direction alpha needs BC at this wall point
// Uses GILBM_e from __constant__ memory (defined in evolution_gilbm.h)
//
// 判定準則：ẽ^ζ_α = e_y[α]·dk_dy + e_z[α]·dk_dz（ζ 方向逆變速度分量）
//   底壁 (k=3):   ẽ^ζ_α > 0 → streaming 出發點 k_dep = k - δζ < 3（壁外）→ 需要 BC
//   頂壁 (k=NZ6-4): ẽ^ζ_α < 0 → 出發點 k_dep > NZ6-4（壁外）→ 需要 BC
//
// 返回 true 時：該 α 由 Chapman-Enskog BC 處理，跳過 streaming。
// 對應的 delta_eta[α] / delta_xi[α] / delta_zeta[α,j,k] 不被讀取。
//
// 平坦底壁 BC 方向: α={5,11,12,15,16}（共 5 個，皆 e_z > 0）
// 斜面底壁 (slope<45°): 額外加入 e_y 分量方向，共 8 個 BC 方向
__device__ __forceinline__ bool NeedsBoundaryCondition(
    int alpha,
    double zeta_y_val, double zeta_z_val,   // was dk_dy_val, dk_dz_val
    bool is_bottom_wall
) {
    // ẽ^ζ_α = e_y·(∂ζ/∂y) + e_z·(∂ζ/∂z)
    double e_tilde_zeta = GILBM_e[alpha][1] * zeta_y_val + GILBM_e[alpha][2] * zeta_z_val;
    return is_bottom_wall ? (e_tilde_zeta > 0.0) : (e_tilde_zeta < 0.0);
    // 底壁 (is_bottom_wall=true):  ẽ^ζ > 0 → 出發點在壁外 → 回傳 true (需要 BC)
    // 頂壁 (is_bottom_wall=false): ẽ^ζ < 0 → 出發點在壁外 → 回傳 true (需要 BC)
}

// Chapman-Enskog BC: compute f_alpha at no-slip wall
//
// 全 Jacobian 展開：∂u_α/∂x_β = (∂u_α/∂ξ)·(∂ξ/∂x_β) + (∂u_α/∂ζ)·(∂ζ/∂x_β)
// 但在 no-slip 壁面 (u=v=w=0 at all j for k=wall):
//   ∂u/∂ξ|_wall = 0（壁面處沿 ξ 方向速度恆為零）
//   → ∂u_α/∂x_β = (∂u_α/∂ζ)·(∂ζ/∂x_β) = (du_α/dk)·zeta_{x_β}
// 因此公式僅需 zeta_y, zeta_z（同舊版 dk/dy, dk/dz）
__device__ double ChapmanEnskogBC(
    int alpha,
    double rho_wall,
    double du_dk, double dv_dk, double dw_dk,  // ∂u/∂ζ at wall (= du/dk)
    double zeta_y_val, double zeta_z_val,       // was dk_dy_val, dk_dz_val
    double omega_global, double dt_global           // GTS: 全域統一 omega 與 dt
) {
    double ex = GILBM_e[alpha][0];
    double ey = GILBM_e[alpha][1];
    double ez = GILBM_e[alpha][2];

    // 展開 6 項 (η 方向 ∂η/∂y=∂η/∂z=0 因 x 均勻; ξ 方向 du/dξ|_wall=0)
    // 僅 β=y,z 存活，且僅 ζ-gradient 項:
    double C_alpha = 0.0;

    // velocity component u (x): ①② 項
    C_alpha += (
        (3.0 * ex * ey) * du_dk * zeta_y_val +       // ① 3·c_x·c_y · (du/dζ)·(ζ_y)
        (3.0 * ex * ez) * du_dk * zeta_z_val          // ② 3·c_x·c_z · (du/dζ)·(ζ_z)
    );

    // velocity component v (y): ③④ 項
    C_alpha += (
        (3.0 * ey * ey - 1.0) * dv_dk * zeta_y_val + // ③ (3·c_y²−1) · (dv/dζ)·(ζ_y)
        (3.0 * ey * ez) * dv_dk * zeta_z_val          // ④ 3·c_y·c_z · (dv/dζ)·(ζ_z)
    );

    // velocity component w (z): ⑤⑥ 項
    C_alpha += (
        (3.0 * ez * ey) * dw_dk * zeta_y_val +       // ⑤ 3·c_z·c_y · (dw/dζ)·(ζ_y)
        (3.0 * ez * ez - 1.0) * dw_dk * zeta_z_val   // ⑥ (3·c_z²−1) · (dw/dζ)·(ζ_z)
    );

    // CE 理論: f^neq ∝ -(τ-0.5)·Δt = -3ν (Navier-Stokes 一致)
    // omega_global = τ = 0.5 + 3ν/Δt, 因此 (τ-0.5)·Δt = 3ν
    C_alpha *= -(omega_global - 0.5) * dt_global;
    double f_eq_atwall = GILBM_W[alpha] * rho_wall;
    return f_eq_atwall * (1.0 + C_alpha);
}

#endif
