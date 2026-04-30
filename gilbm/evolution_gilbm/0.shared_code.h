#ifndef GILBM_SHARED_CODE_H
#define GILBM_SHARED_CODE_H
// ════════════════════════════════════════════════════════════════════════════
// §0  Shared Code — Algorithm1 GTS 共用的常數、helper、Init kernels
// ════════════════════════════════════════════════════════════════════════════
//
// 本檔案被 1.algorithm1.h 在開頭 #include。
// 包含:
//   - D3Q19 __constant__ memory (e, W, dt, inv_dx, M, Mi)
//   - GTS __constant__ memory (s_visc_global, omega_global)
//   - Compile-time safety checks
//   - Sub-module includes (interpolation_gilbm.h, boundary_conditions.h, 0.collision.h)
//   - Stencil / macroscopic helpers
//   - WENO7 diagnostic infrastructure
//   - [已移除] Init_Feq_Kernel — feq_d 不再配置 (方案A)
// ════════════════════════════════════════════════════════════════════════════


// ────────────────────────────────────────────────────────────────────────────
// §0.1  D3Q19 __constant__ Memory
// ────────────────────────────────────────────────────────────────────────────

__constant__ double GILBM_e[19][3] = {
    {0,0,0},
    {1,0,0},{-1,0,0},{0,1,0},{0,-1,0},{0,0,1},{0,0,-1},
    {1,1,0},{-1,1,0},{1,-1,0},{-1,-1,0},
    {1,0,1},{-1,0,1},{1,0,-1},{-1,0,-1},
    {0,1,1},{0,-1,1},{0,1,-1},{0,-1,-1}
};

__constant__ double GILBM_W[19] = {
    1.0/3.0,
    1.0/18.0, 1.0/18.0, 1.0/18.0, 1.0/18.0, 1.0/18.0, 1.0/18.0,
    1.0/36.0, 1.0/36.0, 1.0/36.0, 1.0/36.0,
    1.0/36.0, 1.0/36.0, 1.0/36.0, 1.0/36.0,
    1.0/36.0, 1.0/36.0, 1.0/36.0, 1.0/36.0
};

__constant__ double GILBM_dt;             // global time step
__constant__ double GILBM_inv_dx;         // 1/dx (uniform x-grid spacing, for on-the-fly δη = dt·ex·inv_dx)


// ────────────────────────────────────────────────────────────────────────────
// §0.2  MRT __constant__ Memory
// ────────────────────────────────────────────────────────────────────────────

#if USE_MRT
__constant__ double GILBM_M[19][19];
__constant__ double GILBM_Mi[19][19];
#endif  // USE_MRT


// ────────────────────────────────────────────────────────────────────────────
// §0.3  GTS __constant__ Memory
// ────────────────────────────────────────────────────────────────────────────

__constant__ double GILBM_s_visc_global;   // 1/omega_global (碰撞用)
__constant__ double GILBM_omega_global;    // omega_global   (壁面 BC 用)


// ────────────────────────────────────────────────────────────────────────────
// §0.3a  [moved to communication.h]
//   GILBM_MPI_XI_DIRS __constant__ 宣告已移至 communication.h，
//   因為 communication.h 在 main.cu 中先於 evolution.h 被 #include，
//   且 Pack/Unpack kernel 就定義在 communication.h 中。
// ────────────────────────────────────────────────────────────────────────────


// ────────────────────────────────────────────────────────────────────────────
// §0.4  [REMOVED] — L_eta_precomp moved to on-the-fly kernel computation
// ────────────────────────────────────────────────────────────────────────────
// δη, δξ, δζ 以及所有 Lagrange 權重均在 Step1 kernel 中即時計算。
// 預計算僅輸出: dt_global (1 scalar), inv_dx (1 scalar), 4 Jacobian 陣列 [NYD6×NZ6]
// 不再存儲: delta_eta[19], delta_xi[19×NYD6×NZ6], delta_zeta[19×NYD6×NZ6],
//           L_eta_precomp[19][7], contravariant velocity 陣列
// ────────────────────────────────────────────────────────────────────────────


// ────────────────────────────────────────────────────────────────────────────
// §0.5  Sub-module Includes (依賴上面的 __constant__ 宣告)
// ────────────────────────────────────────────────────────────────────────────

#include "../interpolation_gilbm.h"
#include "../boundary_conditions.h"
#include "0.collision.h"


// ────────────────────────────────────────────────────────────────────────────
// §0.6  Macros
// ────────────────────────────────────────────────────────────────────────────

#define STENCIL_SIZE 7
#define STENCIL_VOL  343  // 7*7*7
#define GRID_SIZE (NX6 * NYD6 * NZ6)


// ────────────────────────────────────────────────────────────────────────────
// §0.7  WENO7 Diagnostic Infrastructure
// ────────────────────────────────────────────────────────────────────────────

#if USE_WENO7
__device__ unsigned int g_weno_diag_zeta[19][NZ6];
__device__ unsigned char g_weno_activation_count_zeta[NZ6][NYD6][NX6];

__device__ __forceinline__ double gilbm_clamp_weno_sigma(double sigma) {
    return gilbm_weno7::clamp_sigma_to_stencil(sigma);
}
#endif // USE_WENO7


// ────────────────────────────────────────────────────────────────────────────
// §0.8  Helper: Stencil Base + Macroscopic
// ────────────────────────────────────────────────────────────────────────────

__device__ __forceinline__ void compute_stencil_base(
    int i, int j, int k,
    int &bi, int &bj, int &bk
) {
    bi = i - 3;
    bj = j - 3;
    bk = k - 3;
    if (bi < 0)           bi = 0;
    if (bi + 6 >= NX6)    bi = NX6 - STENCIL_SIZE;
    if (bj < 0)           bj = 0;
    if (bj + 6 >= NYD6)   bj = NYD6 - STENCIL_SIZE;
    // ★ 居中 stencil (bk_min=0): 與 PrecomputeGILBM_StencilBaseK 一致
    //   偏心 stencil (bk_min=3) 是不穩定來源，已永久移除。
    if (bk < 0)           bk = 0;
    if (bk + 6 >= NZ6)    bk = NZ6 - STENCIL_SIZE;
}

template <typename FPtr>
__device__ __forceinline__ void compute_macroscopic_at(
    FPtr f_ptrs, int idx,
    double &rho_out, double &u_out, double &v_out, double &w_out
) {
    double f[19];
    for (int q = 0; q < 19; q++) f[q] = __ldg(&f_ptrs[q][idx]);

    rho_out = f[0]+f[1]+f[2]+f[3]+f[4]+f[5]+f[6]+f[7]+f[8]+f[9]
             +f[10]+f[11]+f[12]+f[13]+f[14]+f[15]+f[16]+f[17]+f[18];
    u_out = (f[1]+f[7]+f[9]+f[11]+f[13] - (f[2]+f[8]+f[10]+f[12]+f[14])) / rho_out;
    v_out = (f[3]+f[7]+f[8]+f[15]+f[17] - (f[4]+f[9]+f[10]+f[16]+f[18])) / rho_out;
    w_out = (f[5]+f[11]+f[12]+f[15]+f[16] - (f[6]+f[13]+f[14]+f[17]+f[18])) / rho_out;
}


// [方案A] Init_Feq_Kernel 已移除 — feq_d 不再配置
// Step3 collision 自行從 f 計算 macroscopic → feq，不需預計算 feq 陣列


#endif // GILBM_SHARED_CODE_H
