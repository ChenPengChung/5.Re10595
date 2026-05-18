#ifndef COLLISION_GILBM_H
#define COLLISION_GILBM_H

// ════════════════════════════════════════════════════════════════════════════
// 0.collision.h — GILBM GTS 碰撞函數 (MRT + BGK)
// ════════════════════════════════════════════════════════════════════════════
//
// 提供 GTS 碰撞函數 × 2 碰撞模型 (MRT/BGK):
//   gilbm_{mrt,bgk}_collision_GTS()    — GTS 碰撞 (R=1, 一點一值)
//
// 統一介面 alias:
//   gilbm_collision_GTS       → 由 USE_MRT 自動選擇 MRT/BGK
//
// 數學參考:
//   GTS 碰撞公式: f*_A = f^eq_A + (I - M⁻¹·S·M) · (f_A - f^eq_A)
//   R ≡ 1 → one-point-one-value (一點一值)
//
// 用途:
//   Algorithm1 Step3_GTS 呼叫 gilbm_collision_GTS
// ════════════════════════════════════════════════════════════════════════════
//本篇為採用d'Humries的定義 : 定義鬆弛因子為 1/omega_k 其中 omega_k 不是鬆弛時間，而是無因次化鬆弛時間

// ╔════════════════════════════════════════════════════════════════════════╗
// ║  §1. MRT Collision Functions                                          ║
// ╚════════════════════════════════════════════════════════════════════════╝

#if USE_MRT

// ────────────────────────────────────────────────────────────────────────
// §1.1  gilbm_mrt_collision_GTS()
// ────────────────────────────────────────────────────────────────────────
// GTS 碰撞 (R=1, no re-estimation) — gilbm_mrt_combined_LTS 的 R=1 特化
//   m*_k = m_eq_k + (1 - s_k) × (m_k - m_eq_k)
//
// 消除: R_visc, R_const, omegadt_B, dt_B 參數
// 使用者: Algorithm1-GTS Step3
// ────────────────────────────────────────────────────────────────────────
__device__ void gilbm_mrt_collision_GTS(
    double f_out[19],          // output: post-collision distribution
    const double f_B[19],      // input: pre-collision f at stencil node B
    double rho_B,              // density at B
    double u_B,                // velocity x (spanwise)
    double v_B,                // velocity y (streamwise)
    double w_B,                // velocity z (wall-normal)
    double s_visc,             // 1/omega_global (from __constant__)
    double dt_global,          // GILBM_dt (from __constant__)
    double Force0              // body force (streamwise)
) {
    (void)s_visc;  // relaxation is already folded into GILBM_MRT_K and forcing tables

    // Nonequilibrium projection:
    //   f* = f - K(f-feq) + dt*Force0*M^-1(I-S/2)M*F_basis(u)
    // K and the Guo forcing bases are built once on host and stored in constant memory.
    double fneq[19];
    #pragma unroll
    for (int q = 0; q < 19; q++) {
        fneq[q] = f_B[q] - compute_feq_alpha(q, rho_B, u_B, v_B, w_B);
    }

    #pragma unroll
    for (int a = 0; a < 19; a++) {
        double relax = 0.0;
        #pragma unroll
        for (int b = 0; b < 19; b++) {
            relax += GILBM_MRT_K[a][b] * fneq[b];
        }
        f_out[a] = f_B[a] - relax;
#if USE_GUO_FORCING
        f_out[a] += dt_global * Force0 *
            (GILBM_MRT_Fproj[a]
           + u_B * GILBM_MRT_Fproj_u[a]
           + v_B * GILBM_MRT_Fproj_v[a]
           + w_B * GILBM_MRT_Fproj_w[a]);
#else
        f_out[a] += GILBM_W[a] * 3.0 * GILBM_e[a][1] * Force0 * dt_global;
#endif
    }
}


// ╔════════════════════════════════════════════════════════════════════════╗
// ║  §2. BGK Collision Functions                                          ║
// ╚════════════════════════════════════════════════════════════════════════╝

#else  // !USE_MRT → BGK (Single Relaxation Time)

// ────────────────────────────────────────────────────────────────────────
// §2.1  gilbm_bgk_collision_GTS()
// ────────────────────────────────────────────────────────────────────────
// GTS BGK: f* = feq + (1 - 1/tau) · (f - feq) + force
// R=1 特化, 無 re-estimation
// ────────────────────────────────────────────────────────────────────────
__device__ void gilbm_bgk_collision_GTS(
    double f_out[19],
    const double f_B[19],
    double rho_B,
    double u_B, double v_B, double w_B,
    double s_visc,
    double dt_global,
    double Force0
) {
    double feq[19];
    for (int q = 0; q < 19; q++)
        feq[q] = compute_feq_alpha(q, rho_B, u_B, v_B, w_B);

    double C = (1.0 - s_visc);

#if USE_GUO_FORCING
    // BGK Guo (2002): 半力係數 (1 − 1/(2τ)) = (1 − s_visc/2)
    double half_visc = 1.0 - s_visc * 0.5;
    for (int q = 0; q < 19; q++) {
        double cy = GILBM_e[q][1];
#if FORCE_HERMITE_ORDER >= 2
        double cx = GILBM_e[q][0];
        double cz = GILBM_e[q][2];
        double c_dot_u = cx*u_B + cy*v_B + cz*w_B;
        double F_q = GILBM_W[q] * Force0 *
                     ( 3.0 * (cy - v_B) + 9.0 * c_dot_u * cy );
#else
        double F_q = GILBM_W[q] * Force0 * 3.0 * (cy - v_B);
#endif
        f_out[q] = feq[q] + C * (f_B[q] - feq[q]) + dt_global * half_visc * F_q;
    }
#else
    // Legacy: 零階 body force (一階精度)
    for (int q = 0; q < 19; q++) {
        f_out[q] = feq[q] + C * (f_B[q] - feq[q]);
        f_out[q] += GILBM_W[q] * 3.0 * GILBM_e[q][1] * Force0 * dt_global;
    }
#endif
}

#endif  // USE_MRT


// ╔════════════════════════════════════════════════════════════════════════╗
// ║  §3. Unified Alias Interface                                          ║
// ║  Algorithm1 只調用此名稱, 由 USE_MRT 自動選擇 MRT/BGK               ║
// ╚════════════════════════════════════════════════════════════════════════╝

#if USE_MRT
  #define gilbm_collision_GTS       gilbm_mrt_collision_GTS
#else
  #define gilbm_collision_GTS       gilbm_bgk_collision_GTS
#endif


#endif // COLLISION_GILBM_H
