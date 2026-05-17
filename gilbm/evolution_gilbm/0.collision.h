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
    // Forward transform: m_B = M · f_B
    double m_B[19];
    for (int n = 0; n < 19; n++) {
        double sum_f = 0.0;
        for (int a = 0; a < 19; a++)
            sum_f += GILBM_M[n][a] * f_B[a];
        m_B[n] = sum_f;
    }

    // Analytical equilibrium moments (d'Humières D3Q19)
    double ux = u_B, uy = v_B, uz = w_B;
    double ux2 = ux*ux, uy2 = uy*uy, uz2 = uz*uz;
    double u2  = ux2 + uy2 + uz2;

    double m_eq[19];
    m_eq[0]  = rho_B;
    m_eq[1]  = rho_B * (-11.0 + 19.0 * u2);
    m_eq[2]  = rho_B * (3.0 - 5.5 * u2);
    m_eq[3]  = rho_B * ux;
    m_eq[4]  = rho_B * (-2.0/3.0) * ux;
    m_eq[5]  = rho_B * uy;
    m_eq[6]  = rho_B * (-2.0/3.0) * uy;
    m_eq[7]  = rho_B * uz;
    m_eq[8]  = rho_B * (-2.0/3.0) * uz;
    m_eq[9]  = rho_B * (2.0*ux2 - uy2 - uz2);
    m_eq[10] = rho_B * (-0.5) * (2.0*ux2 - uy2 - uz2);
    m_eq[11] = rho_B * (uy2 - uz2);
    m_eq[12] = rho_B * (-0.5) * (uy2 - uz2);
    m_eq[13] = rho_B * ux * uy;
    m_eq[14] = rho_B * uy * uz;
    m_eq[15] = rho_B * ux * uz;
    m_eq[16] = 0.0;
    m_eq[17] = 0.0;
    m_eq[18] = 0.0;

    // GTS: C_k = (1 - s_k), R=1 hardcoded
    double C1  = (1.0 - 1.19);
    double C2  = (1.0 - 1.4);
    double C4  = (1.0 - 1.2);
    double C10 = (1.0 - 1.4);
    double C12 = (1.0 - 1.4);
    double C16 = (1.0 - 1.98);
    double C_visc = (1.0 - s_visc);

    // Combined: m*_k = m_eq_k + C_k × (m_k - m_eq_k)
    double m_star[19];

    m_star[0] = m_eq[0];
    m_star[3] = m_eq[3];
    m_star[5] = m_eq[5];
    m_star[7] = m_eq[7];

    m_star[1]  = m_eq[1]  + C1  * (m_B[1]  - m_eq[1]);
    m_star[2]  = m_eq[2]  + C2  * (m_B[2]  - m_eq[2]);
    m_star[4]  = m_eq[4]  + C4  * (m_B[4]  - m_eq[4]);
    m_star[6]  = m_eq[6]  + C4  * (m_B[6]  - m_eq[6]);
    m_star[8]  = m_eq[8]  + C4  * (m_B[8]  - m_eq[8]);
    m_star[10] = m_eq[10] + C10 * (m_B[10] - m_eq[10]);
    m_star[12] = m_eq[12] + C12 * (m_B[12] - m_eq[12]);
    m_star[16] = m_eq[16] + C16 * (m_B[16] - m_eq[16]);
    m_star[17] = m_eq[17] + C16 * (m_B[17] - m_eq[17]);
    m_star[18] = m_eq[18] + C16 * (m_B[18] - m_eq[18]);

    m_star[9]  = m_eq[9]  + C_visc * (m_B[9]  - m_eq[9]);
    m_star[11] = m_eq[11] + C_visc * (m_B[11] - m_eq[11]);
    m_star[13] = m_eq[13] + C_visc * (m_B[13] - m_eq[13]);
    m_star[14] = m_eq[14] + C_visc * (m_B[14] - m_eq[14]);
    m_star[15] = m_eq[15] + C_visc * (m_B[15] - m_eq[15]);

#if USE_GUO_FORCING
    // ═══════════════════════════════════════════════════════════════════
    // Guo forcing — MRT projection pipeline
    //   F_particle → M·F → (1-s/2) relaxation → inject m_star → M⁻¹
    //
    // <<Ref: T. Krüger et al., "The Lattice Boltzmann Method: Principles and Practice",
    //   Springer (2017), §6.3.2, pp.236-237, Eqs. 6.14-6.16>>
    //
    // Hermite 展開階數選擇 (A/B 切換):
    //   - 2nd-order: Σ Fi·ci·cj = F_α·u_β + u_α·F_β ≠ 0 (壓縮性耦合)
    //   - 1st-order: Σ Fi·ci·cj = 0 (不可壓條件相容)
    //
    // ⚠ GILBM 穩定性實測 (D3Q27 Edit1_PeriodicHillchannel, 2025-05-17):
    //   2nd-order Hermite 經 M 變換激發高階模態不穩定性 → step~1051 發散
    //   1st-order Hermite 在相同條件下穩定收斂
    //   理論依據: 不可壓極限 (Ma<<1) 下能量傳輸與動量解耦 (Krüger §4.3.2)
    //   forcing 二階速度矩應為零 → 1st-order 展開即理論正確
    //
    // 本專案 (Re10595) 目前使用 2nd-order — 若發散請切換為 1st-order
    // ═══════════════════════════════════════════════════════════════════

    double F_particle[19];
    //--- 1st-order Hermite (ALTERNATIVE): Fi = wi·(ci_α/cs²)·F_α ---
    // <<Krüger Eq.6.16>>
    // Moments: Σ Fi = 0, Σ Fi·ci = F, Σ Fi·ci·cj = 0
    // 不可壓相容 — GILBM 驗證穩定 (Edit1_PeriodicHillchannel)
    // 若 2nd-order 發散，取消此段註解並註解上方 2nd-order 區塊
    #pragma unroll
    for (int q = 0; q < 19; q++) {
        F_particle[q] = GILBM_W[q] * Force0 * 3.0 * GILBM_e[q][1];
    }
    
    /* 
    // --- 2nd-order Hermite (ACTIVE): Fi = wi·[ci_α/cs² + (ci_α·ci_β - cs²·δ_αβ)·u_β/cs⁴]·F_α ---
    // <<Krüger Eq.6.14>>
    // Moments: Σ Fi = 0, Σ Fi·ci = F, Σ Fi·ci·cj = F_α·u_β + u_α·F_β ≠ 0
    // 非零二階矩引入壓縮性耦合 (Eq.6.15c)
    #pragma unroll
    for (int q = 0; q < 19; q++) {
        double cx = GILBM_e[q][0];
        double cy = GILBM_e[q][1];
        double cz = GILBM_e[q][2];
        double c_dot_u = cx*u_B + cy*v_B + cz*w_B;
        F_particle[q] = GILBM_W[q] * Force0 *
                        ( 3.0 * (cy - v_B) + 9.0 * c_dot_u * cy );
    }
    */
    

    // Forward transform: F_moment = M · F_particle
    double F_moment[19];
    #pragma unroll
    for (int n = 0; n < 19; n++) {
        double sum_F = 0.0;
        #pragma unroll
        for (int a = 0; a < 19; a++) sum_F += GILBM_M[n][a] * F_particle[a];
        F_moment[n] = sum_F;
    }

    // Inject δt · (1 − s_k/2) · F_moment[k] into m_star[k]
    m_star[0]  += dt_global * F_moment[0];                         // s0  = 0
    m_star[3]  += dt_global * F_moment[3];                         // s3  = 0
    m_star[5]  += dt_global * F_moment[5];                         // s5  = 0
    m_star[7]  += dt_global * F_moment[7];                         // s7  = 0
    m_star[1]  += dt_global * (1.0 - 1.19 * 0.5)   * F_moment[1];
    m_star[2]  += dt_global * (1.0 - 1.40 * 0.5)   * F_moment[2];
    m_star[4]  += dt_global * (1.0 - 1.20 * 0.5)   * F_moment[4];
    m_star[6]  += dt_global * (1.0 - 1.20 * 0.5)   * F_moment[6];
    m_star[8]  += dt_global * (1.0 - 1.20 * 0.5)   * F_moment[8];
    m_star[9]  += dt_global * (1.0 - s_visc * 0.5) * F_moment[9];
    m_star[10] += dt_global * (1.0 - 1.40 * 0.5)   * F_moment[10];
    m_star[11] += dt_global * (1.0 - s_visc * 0.5) * F_moment[11];
    m_star[12] += dt_global * (1.0 - 1.40 * 0.5)   * F_moment[12];
    m_star[13] += dt_global * (1.0 - s_visc * 0.5) * F_moment[13];
    m_star[14] += dt_global * (1.0 - s_visc * 0.5) * F_moment[14];
    m_star[15] += dt_global * (1.0 - s_visc * 0.5) * F_moment[15];
    m_star[16] += dt_global * (1.0 - 1.98 * 0.5)   * F_moment[16];
    m_star[17] += dt_global * (1.0 - 1.98 * 0.5)   * F_moment[17];
    m_star[18] += dt_global * (1.0 - 1.98 * 0.5)   * F_moment[18];

    // Inverse transform: f_out = M⁻¹ · m_star  (forcing 已注入 m_star)
    #pragma unroll
    for (int a = 0; a < 19; a++) {
        double sum = 0.0;
        #pragma unroll
        for (int n = 0; n < 19; n++) sum += GILBM_Mi[a][n] * m_star[n];
        f_out[a] = sum;
    }
#else
    // Legacy: 零階 body force (一階精度, 保留作 A/B 比對基準)
    for (int a = 0; a < 19; a++) {
        double sum = 0.0;
        for (int n = 0; n < 19; n++)
            sum += GILBM_Mi[a][n] * m_star[n];
        f_out[a] = sum;
        f_out[a] += GILBM_W[a] * 3.0 * GILBM_e[a][1] * Force0 * dt_global;
    }
#endif
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
    // <<Ref: Krüger et al. (2017) §6.3.2, Eqs. 6.14-6.16>>
    double half_visc = 1.0 - s_visc * 0.5;

    // --- 2nd-order Hermite (ACTIVE) <<Krüger Eq.6.14>> ---
    // Σ Fi·ci·cj = F_α·u_β + u_α·F_β ≠ 0 (壓縮性耦合)
    // ⚠ 若發散，切換為下方 1st-order 區塊
    for (int q = 0; q < 19; q++) {
        double cx = GILBM_e[q][0];
        double cy = GILBM_e[q][1];
        double cz = GILBM_e[q][2];
        double c_dot_u = cx*u_B + cy*v_B + cz*w_B;
        double F_q = GILBM_W[q] * Force0 *
                     ( 3.0 * (cy - v_B) + 9.0 * c_dot_u * cy );
        f_out[q] = feq[q] + C * (f_B[q] - feq[q]) + dt_global * half_visc * F_q;
    }

    /* --- 1st-order Hermite (ALTERNATIVE) <<Krüger Eq.6.16>> ---
    // Σ Fi·ci·cj = 0 — 不可壓相容，GILBM 驗證穩定
    // 若 2nd-order 發散，取消此段註解並註解上方 2nd-order 區塊
    for (int q = 0; q < 19; q++) {
        double F_q = GILBM_W[q] * Force0 * 3.0 * GILBM_e[q][1];
        f_out[q] = feq[q] + C * (f_B[q] - feq[q]) + dt_global * half_visc * F_q;
    }
    */
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
