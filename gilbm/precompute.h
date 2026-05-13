#ifndef GILBM_PRECOMPUTE_H
#define GILBM_PRECOMPUTE_H

#include <iostream>
#include <iomanip>
#include <cmath>
#include <cstring>

// ============================================================================
// Host-side Lagrange 7-point interpolation coefficients
// ============================================================================
// Identical logic to the __device__ version in interpolation_gilbm.h,
// but callable from host code for precomputation.
// 置於檔案前端，供 host-side 診斷和工具函數使用。
static inline void lagrange_7point_coeffs_host(double t, double a[7]) {
    for (int k = 0; k < 7; k++) {
        double L = 1.0;
        for (int j = 0; j < 7; j++) {
            if (j != k) L *= (t - (double)j) / (double)(k - j);
        }
        a[k] = L;
    }
}

// ════════════════════════════════════════════════════════════════════════════
// [REMOVED] PrecomputeGILBM_DeltaEta, DeltaXi, DeltaZeta, DeltaAll
// ════════════════════════════════════════════════════════════════════════════
// 2026-04 重構: δη, δξ, δζ 全部移至 Step1 kernel 即時計算。
// 預計算只輸出: dt_global (1 constant), inv_dx (1 constant),
//               4 組 Jacobian 陣列 ξ_y, ξ_z, ζ_y, ζ_z [NYD6×NZ6]。
// kernel 中從 Jacobian 出發 → contravariant velocity → RK2 displacement
// → Lagrange weights，全部 on-the-fly。
// ════════════════════════════════════════════════════════════════════════════

// [REMOVED] PrecomputeGILBM_DeltaXi — now computed on-the-fly in Step1 kernel

// [REMOVED] PrecomputeGILBM_DeltaZeta, PrecomputeGILBM_DeltaAll — now on-the-fly in Step1 kernel





// ============================================================================
// Phase 3: Imamura's Global Time Step (Imamura 2005 Eq. 22)
// ============================================================================
// Δt_g = λ · min_{i,α,j,k} [ 1 / |c̃_{i,α}|_{j,k} ]
//      = λ / max_{i,α,j,k} |c̃_{i,α}|_{j,k}
//
// where c̃ is the contravariant velocity in each computational direction:
//   η: |c̃^η_α| = |e_x[α]| / dx                        (uniform x)
//   ξ: |c̃^ξ_α| = |e_y·xi_y + e_z·xi_z|                (space-varying, full Jacobian)
//   ζ: |c̃^ζ_α| = |e_y·zeta_y + e_z·zeta_z|            (space-varying)
//
// This ensures CFL < 1 in ALL directions at ALL grid points.
double ComputeGlobalTimeStep(
    const double *xi_y_h,
    const double *xi_z_h,
    const double *zeta_y_h,
    const double *zeta_z_h,
    double dx_val,
    int NYD6_local,
    int NZ6_local,
    double cfl_lambda,
    int myid_local,
    int nprocs_local
) {
    double e[19][3] = {
        {0,0,0},
        {1,0,0},{-1,0,0},{0,1,0},{0,-1,0},{0,0,1},{0,0,-1},
        {1,1,0},{-1,1,0},{1,-1,0},{-1,-1,0},
        {1,0,1},{-1,0,1},{1,0,-1},{-1,0,-1},
        {0,1,1},{0,-1,1},{0,1,-1},{0,-1,-1}
    };
//目標：求最大的速度分量c̃
    //比較維度：各個空間計算點(j,k),各個速度編號alpha,三個分量，求最大值
    //比較順序：分量->編號->空間點
    //step1:初始化：
    double max_c_tilde = 0.0 ; 
    int max_component = -1 ; //0:eta, 1:xi, 2:zeta
    int max_alpha = -1; //2.
    int max_j = -1, max_k = -1;//3.
    
    // η-direction (uniform x): max|c̃^η| = 1/dx (for |e_x|=1 directions)
    double c_eta = 1.0 / dx_val;
    if (c_eta > max_c_tilde) {
        max_c_tilde = c_eta;
        max_component = 0; //1.
        max_alpha = 1; //2.
        max_j = -1; max_k = -1; //3.個點相同
    }
    // ξ-direction (non-uniform y): scan all fluid points
    // ẽ^ξ_α = e_y·xi_y + e_z·xi_z (space-varying)
    // ζ-direction: ẽ^ζ_α = e_y·zeta_y + e_z·zeta_z (space-varying)
    for (int j = 3 ; j < NYD6_local-3 ; j++){
        for(int k = 3 ; k <= NZ6_local-4 ; k++){
            int idx_jk = j * NZ6_local + k;
            double xi_y_val   = xi_y_h[idx_jk];
            double xi_z_val   = xi_z_h[idx_jk];
            double zeta_y_val = zeta_y_h[idx_jk];
            double zeta_z_val = zeta_z_h[idx_jk];
            for(int alpha = 1 ; alpha <19 ; alpha++){
                if(e[alpha][1] == 0.0 && e[alpha][2] == 0.0) continue;
                // ξ-direction contravariant velocity
                double c_xi = fabs(e[alpha][1] * xi_y_val + e[alpha][2] * xi_z_val);
                if(c_xi > max_c_tilde){
                    max_c_tilde = c_xi;
                    max_component = 1;
                    max_alpha = alpha;
                    max_j = j; max_k = k;
                }
                // ζ-direction contravariant velocity
                double c_zeta = fabs(e[alpha][1] * zeta_y_val + e[alpha][2] * zeta_z_val);
                if(c_zeta > max_c_tilde){
                    max_c_tilde = c_zeta;
                    max_component = 2;
                    max_alpha = alpha;
                    max_j = j; max_k = k;
                }
            }
        }
    }

    double dt_g = cfl_lambda / max_c_tilde;

    // --- Per-rank sequential output (MPI_Barrier 確保輸出順序) ---
    const char *dir_name[] = {"eta (x)", "xi (y)", "zeta (z)"};
    if (myid_local == 0) {
        std::cout << "\n=============================================================\n"
                  << "  Phase 3: Imamura Global Time Step (Eq. 25)\n"
                  << "  CFL lambda = " << std::fixed << std::setprecision(4) << cfl_lambda << "\n"
                  << "=============================================================\n";
    }
    for (int r = 0; r < nprocs_local; r++) {
        CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );
        if (myid_local == r) {
            std::cout << "  Rank " << r << ": max|c_tilde| = "
                      << std::fixed << std::setprecision(6) << max_c_tilde
                      << " in " << dir_name[max_component] << " direction";
            if (max_component == 2) {
                std::cout << " at alpha=" << max_alpha
                          << " (e_y=" << std::showpos << std::setprecision(0) << std::fixed
                          << (double)e[max_alpha][1]
                          << ", e_z=" << (double)e[max_alpha][2] << std::noshowpos
                          << "), j=" << max_j << ", k=" << max_k;
            }
            std::cout << ", dt_rank = " << std::scientific << std::setprecision(6) << dt_g
                      << std::endl;
        }
    }
    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );

    return dt_g;
}


// [REMOVED] PrecomputeGILBM_LagrangeWeights — Lagrange weights now computed on-the-fly in Step1 kernel

// ============================================================================
// PrecomputeGILBM_StencilBaseK: precompute z-direction stencil base with wall clamping
// ============================================================================
// bk depends ONLY on k (not on q, j, or i), so a 1D array [NZ6] suffices.
// Kernel access: bk_precomp_d[k] (direct indexing, no offset).
// bk_h[0,1,2] and bk_h[NZ6-2..NZ6-1] are ghost/buffer — kernel guard skips them.

// z-clamping logic: ALWAYS use centered stencil (bk_min=0)
//
// ★ 2026-04 修正：偏心 stencil (bk_min=3) 是不穩定來源，已永久移除。
//   verify_bk_centering.py 驗證：bk_min=3 導致近壁 t_zeta 偏向 stencil 邊緣，
//   Lebesgue Λ 放大 3.17×，truncation error 放大 7.8× → 在 tanh stretched grid 發散。
//
// 居中策略（bk_min=0）：
//   bk = k - 3
//   if (bk < 0)                bk = 0        (bottom: 允許 stencil 包含 ghost zone)
//   if (bk + 6 >= NZ6)         bk = NZ6 - 7  (top: 陣列邊界保護)
//   近壁層 k=3,4,5: bk=0,1,2 → stencil 中心對準 departure point → t_zeta ≈ 3
//   Ghost zone entries (bk+sk < 3 or bk+sk > NZ6-4) 在各 kernel 中用線性外推修正
//
// 此居中策略是穩定性的必要條件，獨立於 USE_WENO7（WENO 非線性權重）開關。
// USE_WENO7 僅控制 ζ-pass 是否啟用 WENO7-Z 非線性權重，不再影響 stencil 定位。
void PrecomputeGILBM_StencilBaseK(
    int *bk_h,          // output: [NZ6] (indexed directly by k)
    int NZ6_local
) {
    for (int k = 0; k < NZ6_local; k++) {
        int bk = k - 3;
        // 居中 stencil: bk_min=0（穩定性必要條件，所有 Algorithm 共用）
        // k=3: bk=0, center=3, t_zeta≈3 → GOOD
        // k=4: bk=1, center=4, t_zeta≈3 → GOOD
        // k=5: bk=2, center=5, t_zeta≈3 → GOOD
        // Ghost zone entries (bk+sk < 3) 在各 kernel 的 ζ-pass 中用線性外推替換
        if (bk < 0)                    bk = 0;
        if (bk + 6 >= NZ6_local)       bk = NZ6_local - 7;  // 陣列邊界保護
        bk_h[k] = bk;
    }
}

#endif
