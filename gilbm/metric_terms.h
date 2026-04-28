#ifndef METRIC_TERMS_FILE
#define METRIC_TERMS_FILE

#include <iostream>
#include <fstream>
#include <iomanip>
#include <cmath>
#include <cstdlib>

using namespace std ;

// Phase 0: 座標轉換度量項計算（Imamura 2005 左側元素）
// 計算座標 = 網格索引 (i, j, k)，均勻間距 Δ=1
//
// 度量項（物理→計算空間映射，用於逆變速度公式）：
//   ∂ζ/∂z = dk_dz = 1 / (∂z/∂k)
//   ∂ζ/∂y = dk_dy = -(∂z/∂j) / (dy · ∂z/∂k)
//
// 計算方法：先用中心差分求 ∂z/∂k, ∂z/∂j（正 Jacobian），再求逆得到度量項
// 最終存儲的是文獻所需的左側元素 ∂ζ/∂z, ∂ζ/∂y
//
// 差分精度 (v2 — 六階升級)：
//   k 方向內部：六階精度中心差分（7-point stencil, Fornberg coefficients）
//   k 方向邊界：六階精度偏斜 stencil（forward/backward, 自適應偏移）
//   k=2, k=NZ6-3 緩衝區：五階精度單邊差分（6-point stencil）
//   j 方向全域：六階精度中心差分（buffer=3 足夠支撐 ±3 stencil）

// ─────────────────────────────────────────────────────────────────
//  Fornberg 係數表：7-point stencil, 1st derivative, unit spacing
//  FD6_COEFF[p][m] : 評估點在 stencil 中偏移 p (0=最左, 6=最右)
//                     m = stencil 內第 m 個點的係數
//  實際導數 = Σ FD6_COEFF[p][m] * f[s+m] / 60      (s = stencil 起始索引)
// ─────────────────────────────────────────────────────────────────
static const double FD6_COEFF[7][7] = {
    {-147.0,  360.0, -450.0,  400.0, -225.0,   72.0,  -10.0},  // p=0 forward
    { -10.0,  -77.0,  150.0, -100.0,   50.0,  -15.0,    2.0},  // p=1
    {   2.0,  -24.0,  -35.0,   80.0,  -30.0,    8.0,   -1.0},  // p=2
    {  -1.0,    9.0,  -45.0,    0.0,   45.0,   -9.0,    1.0},  // p=3 central
    {   1.0,   -8.0,   30.0,  -80.0,   35.0,   24.0,   -2.0},  // p=4
    {  -2.0,   15.0,  -50.0,  100.0, -150.0,   77.0,   10.0},  // p=5
    {  10.0,  -72.0,  225.0, -400.0,  450.0, -360.0,  147.0},  // p=6 backward
};

// 五階單邊差分：6-point stencil（用於 k=2, k=NZ6-3 緩衝區）
// 實際導數 = Σ coeff[m] * f[start+m] / 60
static const double FD5_FWD[6] = {-137.0, 300.0, -300.0, 200.0, -75.0, 12.0};
static const double FD5_BWD[6] = {-12.0, 75.0, -200.0, 300.0, -300.0, 137.0};

// ═══════════════════════════════════════════════════════════════════
//  ComputeMetricTerms_Full — 完整 2×2 Jacobian (y-z 平面)
// ═══════════════════════════════════════════════════════════════════
//  Forward Jacobian:
//    | y_xi   y_zeta |   | ∂y/∂ξ  ∂y/∂ζ |
//    | z_xi   z_zeta | = | ∂z/∂ξ  ∂z/∂ζ |
//
//  Determinant: J_2D = y_xi * z_zeta - y_zeta * z_xi
//
//  Inverse (used for contravariant velocity):
//    xi_y   =  z_zeta / J_2D       xi_z  = -y_zeta / J_2D
//    zeta_y = -z_xi   / J_2D       zeta_z =  y_xi   / J_2D
//
//  ξ = j (streamwise), ζ = k (wall-normal)
//  差分: j 方向六階中心 (buffer=3), k 方向六階自適應 Fornberg
// ═══════════════════════════════════════════════════════════════════

// ---------- 內部輔助: k 方向六階自適應差分 ----------
static inline double FD6_k_adaptive(const double *field, int base_j,
                                     int k, int k_lo, int k_hi, int NZ6_local)
{
    double deriv;
    if (k == 2) {
        // 底部緩衝層: 五階前向 (6 pt)
        deriv = 0.0;
        for (int m = 0; m < 6; m++)
            deriv += FD5_FWD[m] * field[base_j + 2 + m];
        deriv /= 60.0;
    } else if (k == NZ6_local - 3) {
        // 頂部緩衝層: 五階後向 (6 pt)
        deriv = 0.0;
        for (int m = 0; m < 6; m++)
            deriv += FD5_BWD[m] * field[base_j + (NZ6_local - 8) + m];
        deriv /= 60.0;
    } else if (k >= k_lo && k <= k_hi) {
        // 物理域: 六階 Fornberg 自適應偏斜
        int s = k - 3;
        if (s < k_lo)     s = k_lo;
        if (s > k_hi - 6) s = k_hi - 6;
        int p = k - s;
        deriv = 0.0;
        for (int m = 0; m < 7; m++)
            deriv += FD6_COEFF[p][m] * field[base_j + s + m];
        deriv /= 60.0;
    } else {
        // fallback: 二階中心差分
        deriv = (field[base_j + k + 1] - field[base_j + k - 1]) / 2.0;
    }
    return deriv;
}

// ---------- 內部輔助: j 方向六階中心差分 ----------
static inline double FD6_j_central(const double *field, int j, int k, int NZ6_local)
{
    return ( -field[(j-3)*NZ6_local + k]
        + 9.0*field[(j-2)*NZ6_local + k]
       - 45.0*field[(j-1)*NZ6_local + k]
       + 45.0*field[(j+1)*NZ6_local + k]
        - 9.0*field[(j+2)*NZ6_local + k]
            + field[(j+3)*NZ6_local + k] ) / 60.0;
}

void ComputeMetricTerms_Full(
    double *y_xi_out,       // output [NYD6*NZ6] ∂y/∂ξ
    double *y_zeta_out,     // output [NYD6*NZ6] ∂y/∂ζ
    double *z_xi_out,       // output [NYD6*NZ6] ∂z/∂ξ
    double *z_zeta_out,     // output [NYD6*NZ6] ∂z/∂ζ
    double *J_2D_out,       // output [NYD6*NZ6] Jacobian determinant
    double *xi_y_out,       // output [NYD6*NZ6] inverse: ∂ξ/∂y
    double *xi_z_out,       // output [NYD6*NZ6] inverse: ∂ξ/∂z
    double *zeta_y_out,     // output [NYD6*NZ6] inverse: ∂ζ/∂y
    double *zeta_z_out,     // output [NYD6*NZ6] inverse: ∂ζ/∂z
    const double *y_2d_h,   // input  [NYD6*NZ6] 2D y-coordinates
    const double *z_h,      // input  [NYD6*NZ6] 2D z-coordinates
    int NYD6_local,
    int NZ6_local
) {
    int k_lo = 3;
    int k_hi = NZ6_local - 4;

    // === Pass 1: 正 Jacobian (forward metric terms) ===
    for (int j = 3; j < NYD6_local - 3; j++) {
        int base_j = j * NZ6_local;
        for (int k = 2; k < NZ6_local - 2; k++) {
            int idx = base_j + k;

            // ∂y/∂ξ  (j 方向)
            y_xi_out[idx] = FD6_j_central(y_2d_h, j, k, NZ6_local);

            // ∂y/∂ζ  (k 方向)
            y_zeta_out[idx] = FD6_k_adaptive(y_2d_h, base_j, k, k_lo, k_hi, NZ6_local);

            // ∂z/∂ξ  (j 方向)
            z_xi_out[idx] = FD6_j_central(z_h, j, k, NZ6_local);

            // ∂z/∂ζ  (k 方向)
            z_zeta_out[idx] = FD6_k_adaptive(z_h, base_j, k, k_lo, k_hi, NZ6_local);
        }
    }

    // === Pass 2: Jacobian 行列式 + 逆 Jacobian ===
    for (int j = 3; j < NYD6_local - 3; j++) {
        int base_j = j * NZ6_local;
        for (int k = 2; k < NZ6_local - 2; k++) {
            int idx = base_j + k;

            double y_xi   = y_xi_out[idx];
            double y_zeta = y_zeta_out[idx];
            double z_xi   = z_xi_out[idx];
            double z_zeta = z_zeta_out[idx];

            double J = y_xi * z_zeta - y_zeta * z_xi;
            J_2D_out[idx] = J;

            // 安全檢查: J 不應為零或負
            if (fabs(J) < 1.0e-30) {
                cerr << "[ComputeMetricTerms_Full] FATAL: J_2D ~ 0 at j="
                     << j << " k=" << k
                     << " (y_xi=" << y_xi << " y_zeta=" << y_zeta
                     << " z_xi=" << z_xi << " z_zeta=" << z_zeta << ")" << endl;
                MPI_Abort(MPI_COMM_WORLD, 1);
            }

            double invJ = 1.0 / J;
            xi_y_out[idx]   =  z_zeta * invJ;   // ∂ξ/∂y  =  z_ζ / J
            xi_z_out[idx]   = -y_zeta * invJ;   // ∂ξ/∂z  = -y_ζ / J
            zeta_y_out[idx] = -z_xi   * invJ;   // ∂ζ/∂y  = -z_ξ / J
            zeta_z_out[idx] =  y_xi   * invJ;   // ∂ζ/∂z  =  y_ξ / J
        }
    }

    // === Pass 3: 逆 Jacobian ghost zone 二次外推 ===
    // k=0,1,2 (底部 ghost/buffer) 和 k=NZ6-3, NZ6-2, NZ6-1 (頂部 ghost/buffer) 的逆 Jacobian
    // 用 3 個內部點做二次 Lagrange 外推（直接計算，不級聯）
    // 二次外推: c0 = (d+1)(d+2)/2, c1 = -d(d+2), c2 = d(d+1)/2
    // 底部內部點: k=3,4,5; 頂部內部點: k=NZ6-4, NZ6-5, NZ6-6
    // 目的: 讓 RK2 midpoint 的居中 stencil (sk_rk clamp [0, NZ6-7]) 有合法值可讀
    for (int j = 3; j < NYD6_local - 3; j++) {
        int base_j = j * NZ6_local;
        double *arrays[] = { xi_y_out, xi_z_out, zeta_y_out, zeta_z_out };
        for (int a = 0; a < 4; a++) {
            double *arr = arrays[a];
            // 底部: 從 k=3,4,5 直接外推到 k=2,1,0
            double f3 = arr[base_j + 3];
            double f4 = arr[base_j + 4];
            double f5 = arr[base_j + 5];
            // k=2: d=1 → c0=3, c1=-3, c2=1
            arr[base_j + 2] =  3.0 * f3 - 3.0 * f4 + 1.0 * f5;
            // k=1: d=2 → c0=6, c1=-8, c2=3
            arr[base_j + 1] =  6.0 * f3 - 8.0 * f4 + 3.0 * f5;
            // k=0: d=3 → c0=10, c1=-15, c2=6
            arr[base_j + 0] = 10.0 * f3 - 15.0 * f4 + 6.0 * f5;
            // 頂部: 從 k=NZ6-4, NZ6-5, NZ6-6 直接外推到 k=NZ6-3, NZ6-2, NZ6-1
            double fN4 = arr[base_j + NZ6_local - 4];
            double fN5 = arr[base_j + NZ6_local - 5];
            double fN6 = arr[base_j + NZ6_local - 6];
            // k=NZ6-3: d=1 → c0=3, c1=-3, c2=1
            arr[base_j + NZ6_local - 3] =  3.0 * fN4 - 3.0 * fN5 + 1.0 * fN6;
            // k=NZ6-2: d=2 → c0=6, c1=-8, c2=3
            arr[base_j + NZ6_local - 2] =  6.0 * fN4 - 8.0 * fN5 + 3.0 * fN6;
            // k=NZ6-1: d=3 → c0=10, c1=-15, c2=6
            arr[base_j + NZ6_local - 1] = 10.0 * fN4 - 15.0 * fN5 + 6.0 * fN6;
        }
    }
}

// ═══════════════════════════════════════════════════════════════════
//  DiagnoseMetricTerms_Full — 全 Jacobian 版診斷 (rank 0 only)
// ═══════════════════════════════════════════════════════════════════
void DiagnoseMetricTerms_Full(
    const double *y_xi_h, const double *y_zeta_h,
    const double *z_xi_h, const double *z_zeta_h,
    const double *J_2D_h,
    const double *xi_y_h, const double *xi_z_h,
    const double *zeta_y_h, const double *zeta_z_h,
    const double *y_2d_h, const double *z_h,
    int NYD6_local, int NZ6_local, int myid)
{
    if (myid != 0) return;

    int bfr = 3;

    // ====== 判據 1: J_2D > 0 全場 ======
    int pass1 = 1;
    double J_min = 1e30, J_max = -1e30;
    for (int j = bfr; j < NYD6_local - bfr; j++) {
        for (int k = bfr; k < NZ6_local - bfr; k++) {
            int idx = j * NZ6_local + k;
            double J = J_2D_h[idx];
            if (J < J_min) J_min = J;
            if (J > J_max) J_max = J;
            if (J <= 0.0) {
                pass1 = 0;
                if (pass1 == 0)  // 只印前幾個
                    cout << "  FAIL: J_2D <= 0 at j=" << j << " k=" << k
                         << " J=" << scientific << J << endl;
            }
        }
    }

    // ====== 判據 2: Jacobian identity J × J^{-1} = I (抽樣) ======
    int pass2 = 1;
    double max_err_identity = 0.0;
    for (int j = bfr; j < NYD6_local - bfr; j++) {
        for (int k = bfr; k < NZ6_local - bfr; k++) {
            int idx = j * NZ6_local + k;
            // A = forward, B = inverse → A*B should = I
            // row1: (y_xi, y_zeta) . (xi_y, zeta_y) should = 1
            double e11 = y_xi_h[idx]*xi_y_h[idx] + y_zeta_h[idx]*zeta_y_h[idx] - 1.0;
            // row1: (y_xi, y_zeta) . (xi_z, zeta_z) should = 0
            double e12 = y_xi_h[idx]*xi_z_h[idx] + y_zeta_h[idx]*zeta_z_h[idx];
            // row2: (z_xi, z_zeta) . (xi_y, zeta_y) should = 0
            double e21 = z_xi_h[idx]*xi_y_h[idx] + z_zeta_h[idx]*zeta_y_h[idx];
            // row2: (z_xi, z_zeta) . (xi_z, zeta_z) should = 1
            double e22 = z_xi_h[idx]*xi_z_h[idx] + z_zeta_h[idx]*zeta_z_h[idx] - 1.0;

            double err = fabs(e11) + fabs(e12) + fabs(e21) + fabs(e22);
            if (err > max_err_identity) max_err_identity = err;
            if (err > 1.0e-10) {
                pass2 = 0;
            }
        }
    }

    // ====== 判據 3: zeta_z > 0 (壁面法向映射正定) ======
    int pass3 = 1;
    for (int j = bfr; j < NYD6_local - bfr; j++) {
        for (int k = bfr; k < NZ6_local - bfr; k++) {
            int idx = j * NZ6_local + k;
            if (zeta_z_h[idx] <= 0.0) {
                pass3 = 0;
                cout << "  FAIL: zeta_z <= 0 at j=" << j << " k=" << k
                     << " val=" << scientific << zeta_z_h[idx] << endl;
            }
        }
    }

    // ====== 輸出摘要 ======
    cout << "\n===== Stage 2: Full Jacobian Diagnostics (rank 0 local) =====\n";
    cout << "[" << (pass1 ? "PASS" : "FAIL") << "] Criteria 1: J_2D > 0 everywhere"
         << "  (J_min=" << scientific << setprecision(4) << J_min
         << ", J_max=" << J_max << ")\n";
    cout << "[" << (pass2 ? "PASS" : "FAIL") << "] Criteria 2: J * J^{-1} = I"
         << "  (max |err|=" << scientific << setprecision(4) << max_err_identity << ")\n";
    cout << "[" << (pass3 ? "PASS" : "FAIL") << "] Criteria 3: zeta_z > 0 everywhere\n";

    // ====== 輸出完整度量項檔案 ======
    ofstream fout("gilbm_metrics_full.dat");
    fout << "# j  k  y  z  y_xi  y_zeta  z_xi  z_zeta  J_2D"
         << "  xi_y  xi_z  zeta_y  zeta_z\n";
    for (int j = bfr; j < NYD6_local - bfr; j++) {
        for (int k = bfr; k < NZ6_local - bfr; k++) {
            int idx = j * NZ6_local + k;
            fout << setw(4) << j << " " << setw(4) << k << " "
                 << setw(14) << fixed << setprecision(8) << y_2d_h[idx] << " "
                 << setw(14) << z_h[idx] << " "
                 << setw(14) << scientific << setprecision(6)
                 << y_xi_h[idx] << " " << y_zeta_h[idx] << " "
                 << z_xi_h[idx] << " " << z_zeta_h[idx] << " "
                 << J_2D_h[idx] << " "
                 << xi_y_h[idx] << " " << xi_z_h[idx] << " "
                 << zeta_y_h[idx] << " " << zeta_z_h[idx] << "\n";
        }
    }
    fout.close();
    cout << "Diagnostic file written: gilbm_metrics_full.dat\n";
    cout << "===== End Stage 2 Diagnostics =====\n\n";
}


// ═══════════════════════════════════════════════════════════════════
//  Legacy: ComputeMetricTerms + DiagnoseMetricTerms (舊版 2-term)
//  已被 ComputeMetricTerms_Full + DiagnoseMetricTerms_Full 完全取代
//  保留供參考，但不再編譯
// ═══════════════════════════════════════════════════════════════════
#if 0  // Dead code: legacy 2-term metric functions (replaced by Full Jacobian versions)
void ComputeMetricTerms(
    double *dk_dz_h,    // output [NYD6*NZ6]
    double *dk_dy_h,    // output [NYD6*NZ6]
    const double *z_h,  // input  [NYD6*NZ6]
    const double *y_h,  // input  [NYD6]
    int NYD6_local,
    int NZ6_local
) {
    // 公式用 Jacobian 轉換
    double dy = y_h[4] - y_h[3];  // 均勻 Y 格距

 
    int k_lo = 3;                   // 底壁（第一個 tanh 節點）
    int k_hi = NZ6_local - 4;

    for (int j = 3; j < NYD6_local - 3; j++) {
        int base_j = j * NZ6_local;

        for (int k = 2; k < NZ6_local - 2; k++) {     // 擴展至緩衝層 k=2 和 k=NZ6-3
            int idx = base_j + k;
            double dz_dk, dz_dj;

            // ═══════ ∂z/∂k：k 方向差分（六階精度，自適應 stencil）═══════
            if (k == 2) {
                // 底部緩衝層：五階前向差分（6 points: k=2..7）
                dz_dk = 0.0;
                for (int m = 0; m < 6; m++)
                    dz_dk += FD5_FWD[m] * z_h[base_j + 2 + m];
                dz_dk /= 60.0;

            } else if (k == NZ6_local - 3) {
                // 頂部緩衝層：五階後向差分（6 points: k=NZ6-8..NZ6-3）
                dz_dk = 0.0;
                for (int m = 0; m < 6; m++)
                    dz_dk += FD5_BWD[m] * z_h[base_j + (NZ6_local - 8) + m];
                dz_dk /= 60.0;

            } else if (k >= k_lo && k <= k_hi) {
                //  物理域：六階精度 7-point Fornberg stencil（自適應偏斜）
                //   s = stencil 起始索引（在物理域內選取 7 個連續點）
                //   p = 評估點在 stencil 中的偏移 (0..6)
                //   當 k 遠離邊界 → p=3（中心差分）
                //   靠近下壁面 → p<3（前向偏斜）
                //   靠近上壁面 → p>3（後向偏斜）
                int s = k - 3;                      // 理想起點（centered）
                if (s < k_lo)     s = k_lo;         // 下壁面夾限
                if (s > k_hi - 6) s = k_hi - 6;    // 上壁面夾限
                int p = k - s;                      // 偏移量 (保證 0 ≤ p ≤ 6)

                dz_dk = 0.0;
                for (int m = 0; m < 7; m++)
                    dz_dk += FD6_COEFF[p][m] * z_h[base_j + s + m];
                dz_dk /= 60.0;

            } else {
                // 安全 fallback（正常流程不應進入此分支）
                dz_dk = (z_h[base_j + k + 1] - z_h[base_j + k - 1]) / 2.0;
            }

            // ═══════ ∂z/∂j：j 方向六階中心差分 ═══════
            // j buffer = 3（MPI halo / periodic），足夠支撐 ±3 stencil
            dz_dj = (  -z_h[(j-3)*NZ6_local + k]
                    + 9.0*z_h[(j-2)*NZ6_local + k]
                   - 45.0*z_h[(j-1)*NZ6_local + k]
                   + 45.0*z_h[(j+1)*NZ6_local + k]
                    - 9.0*z_h[(j+2)*NZ6_local + k]
                        + z_h[(j+3)*NZ6_local + k] ) / 60.0;

            // 度量項（左側元素）
            dk_dz_h[idx] = 1.0 / dz_dk;
            dk_dy_h[idx] = -dz_dj / (dy * dz_dk);
        }
    }
}


// ======== Phase 0 診斷輸出 ========
// 在 rank 0 內部重建全域座標，計算全域度量項並輸出診斷文件
// 在 GenerateMesh_Z() 之後調用
void DiagnoseMetricTerms(int myid) {
    // 只在 rank 0 輸出
    if (myid != 0) return;

    int bfr = 3;
    double dy = LY / (double)(NY6 - 2*bfr - 1);
    double dx = LX / (double)(NX6 - 2*bfr - 1);

    // ====== 重建全域座標（與 GenerateMesh_Y/Z 相同公式）======
    double *y_g  = (double *)malloc(NY6 * sizeof(double));
    double *z_g  = (double *)malloc(NY6 * NZ6 * sizeof(double));
    double *dk_dz_g = (double *)malloc(NY6 * NZ6 * sizeof(double));
    double *dk_dy_g = (double *)malloc(NY6 * NZ6 * sizeof(double));

    // Y 座標
    for (int j = 0; j < NY6; j++) {
        y_g[j] = dy * ((double)(j - bfr));
    }

    // Z 座標（Buffer=3: tanh 起點=壁面 k=3, 終點=LZ k=NZ6-4）
    double a = GetNonuniParameter();
    for (int j = 0; j < NY6; j++) {
        double total = LZ - HillFunction(y_g[j]);
        for (int k = bfr; k < NZ6 - bfr; k++) {
            z_g[j*NZ6+k] = tanhFunction_wall(total, a, (k-3), (NZ6-7))
                         + HillFunction(y_g[j]);
        }
        // 外插 buffer + ghost 層（二次 Lagrange，直接從 3 內部點，不級聯）
        // 底部: 從 k=3,4,5 外推
        {
            double f3 = z_g[j*NZ6+3], f4 = z_g[j*NZ6+4], f5 = z_g[j*NZ6+5];
            z_g[j*NZ6+2] =  3.0 * f3 -  3.0 * f4 + 1.0 * f5;  // d=1
            z_g[j*NZ6+1] =  6.0 * f3 -  8.0 * f4 + 3.0 * f5;  // d=2
            z_g[j*NZ6+0] = 10.0 * f3 - 15.0 * f4 + 6.0 * f5;  // d=3
        }
        // 頂部: 從 k=NZ6-4, NZ6-5, NZ6-6 外推
        {
            double fN4 = z_g[j*NZ6+(NZ6-4)], fN5 = z_g[j*NZ6+(NZ6-5)], fN6 = z_g[j*NZ6+(NZ6-6)];
            z_g[j*NZ6+(NZ6-3)] =  3.0 * fN4 -  3.0 * fN5 + 1.0 * fN6;  // d=1
            z_g[j*NZ6+(NZ6-2)] =  6.0 * fN4 -  8.0 * fN5 + 3.0 * fN6;  // d=2
            z_g[j*NZ6+(NZ6-1)] = 10.0 * fN4 - 15.0 * fN5 + 6.0 * fN6;  // d=3
        }
    }

    // ====== 計算全域度量項 ======
    ComputeMetricTerms(dk_dz_g, dk_dy_g, z_g, y_g, NY6, NZ6);

    // ====== 輸出 1: 全場Jacibian轉換係數 ======
    ofstream fout("gilbm_metrics.dat");
    fout << "# j  k  y  z  H(y)  dz_dk  dk_dz  dz_dj  dk_dy  J\n";
    for (int j = bfr; j < NY6 - bfr; j++) {
        double Hy = HillFunction(y_g[j]);
        for (int k = 3; k < NZ6 - 3; k++) {        
            int idx = j * NZ6 + k;
            double dz_dk = 1.0 / dk_dz_g[idx];
            double dz_dj = -dk_dy_g[idx] * dy * dz_dk;
            double J = dx * dy * dz_dk;  // Jacobian 行列式

            fout << setw(4) << j << " "
                 << setw(4) << k << " "
                 << setw(12) << fixed << setprecision(6) << y_g[j] << " "
                 << setw(12) << z_g[idx] << " "
                 << setw(12) << Hy << " "
                 << setw(12) << scientific << setprecision(6) << dz_dk << " "
                 << setw(12) << dk_dz_g[idx] << " "
                 << setw(12) << dz_dj << " "
                 << setw(12) << dk_dy_g[idx] << " "
                 << setw(12) << J << "\n";
        }
    }
    fout.close();

    // ====== 輸出 2: 選定位置的剖面 ======
    // 找出三個特徵 j 值（在全域範圍搜索）
    int j_flat = -1, j_peak = -1, j_slope = -1;
    double H_max = 0.0, dH_max = 0.0;

    for (int j = bfr ; j < NY6 - bfr - 1; j++) {
        double Hy = HillFunction(y_g[j]);
        //discrete dirivative of H(y) for slope detection
        double dHdy = (HillFunction(y_g[j + 1]) - HillFunction(y_g[j - 1])) / (2.0 * dy);
        
        if (Hy < 0.01 && j_flat < 0) j_flat = j; //尋找第一個平坦點 (H≈0)
        /*山丘高度為零，底壁是平的值
        預期：∂z/∂j ≈ 0 → dk_dy ≈ 0，座標系退化為正交（無扭曲）
        驗證用途：判據 3 檢查 dk_dy ≈ 0；判據 5 檢查壁面 BC 方向恰好是標準的 5 個*/
        if (Hy > H_max) { H_max = Hy; j_peak = j; } //尋找山丘最高點 (argmax H)
        /*物理空間被壓縮最嚴重（天花板到山丘頂的間距最小）
        預期：dz_dk 最小（格點擠在一起）、dk_dz 最大
        驗證用途：輸出 2 的剖面圖，確認度量項在極端壓縮處的數值是否合理*/
        if (fabs(dHdy) > dH_max && Hy > 0.1) { dH_max = fabs(dHdy); j_slope = j; } //尋找最陡斜面 (argmax |H'|, 排除平坦段)的j值（正規化）
        /*座標系扭曲最嚴重的位置（z 網格線不再垂直，而是傾斜）
        預期 ：|dk_dy| 最大（座標耦合最強）；壁面 BC 需要額外方向（判據 6）
        驗證用途：如果這個最極端的位置度量項都正確，其他位置更不會有問題*/
    }

    cout << "\n===== Phase 0: Metric Terms Diagnostics (Global) =====\n";
    if (j_flat >= 0)
        cout << "j_flat  = " << setw(4) << j_flat << "  (y=" << fixed << setprecision(4)
             << setw(8) << y_g[j_flat] << ", H=" << setw(8) << HillFunction(y_g[j_flat]) << ")\n";
    else
        cout << "j_flat  = NOT FOUND\n";

    if (j_peak >= 0)
        cout << "j_peak  = " << setw(4) << j_peak << "  (y=" << fixed << setprecision(4)
             << setw(8) << y_g[j_peak] << ", H=" << setw(8) << HillFunction(y_g[j_peak]) << ")\n";
    else
        cout << "j_peak  = NOT FOUND\n";

    if (j_slope >= 0)
        cout << "j_slope = " << setw(4) << j_slope << "  (y=" << fixed << setprecision(4)
             << setw(8) << y_g[j_slope] << ", H=" << setw(8) << HillFunction(y_g[j_slope])
             << ", |H'|=" << setw(8) << dH_max << ")\n";
    else
        cout << "j_slope = NOT FOUND\n";


    // ====== 輸出 3: 壁面方向判別 ======
    // D3Q19 速度集
    double e[19][3] = {
        {0,0,0},
        {1,0,0},{-1,0,0},{0,1,0},{0,-1,0},{0,0,1},{0,0,-1},
        {1,1,0},{-1,1,0},{1,-1,0},{-1,-1,0},
        {1,0,1},{-1,0,1},{1,0,-1},{-1,0,-1},
        {0,1,1},{0,-1,1},{0,1,-1},{0,-1,-1}
    };

    ofstream fwall;
    fwall.open("gilbm_contravariant_wall.dat");

    fwall << "# j  y  H(y)  dH/dy  num_BC_dirs  BC_directions\n";

    int pass_flat_5dirs = 1;  // 用於判據 5
    int fail_count_flat = 0;  // 統計判據 5 失敗點數
    int pass_slope_extra = 1; // 用於判據 6（預設 PASS，任何斜面點 num_bc<=5 則 FAIL=0）
    int found_any_slope = 0;  // 是否找到任何斜面點 (|dHdy| > 0.1)
    int fail_count_slope = 0; // 統計判據 6 失敗點數

    for (int j = bfr ; j < NY6 - bfr - 1; j++) { //由左到右掃描
        int k_wall = 3 ;  //壁面位置（k=3，對應 z=H(y)，wet-node on wall）
        int idx = j * NZ6 + k_wall;//計算空間最下面一排的計算點
        double Hy = HillFunction(y_g[j]);
        double dHdy = (HillFunction(y_g[j + 1]) - HillFunction(y_g[j - 1])) / (2.0 * dy);

        //分別印出 編號：全域y座標：H(y)：dH/dy
        fwall << setw(4) << j << " "
              << setw(10) << fixed << setprecision(5) << y_g[j] << " "
              << setw(10) << Hy << " "
              << setw(10) << dHdy << "  ";
        //====外迴圈：掃描各個底層計算點，內迴圈：掃描不同離散速度的方向
        // 暫存需要 BC 的方向
        int num_bc = 0;
        int bc_dirs[19];
        //不同離散速度分開計算
        for (int alpha = 0; alpha < 19; alpha++) {
            //計算座標變換下的離散速度集k分量(k = zeta分量)
            double e_tilde_k = e[alpha][1] * dk_dy_g[idx] + e[alpha][2] * dk_dz_g[idx];//k = zeta 為方向座標變換
            if (e_tilde_k > 0.0) {
                //取<0作為面壁方向;取>0作為反彈後方向
                //在哪一個y列的下面一點上，離散速度的zeta分量不為零，則此點為該編號alpha的邊界計算點
                // 將當前方向索引 alpha 記錄到邊界條件方向陣列 bc_dirs 中，
                // 同時將邊界條件計數器 num_bc 遞增 1。
                bc_dirs[num_bc] = alpha;
                num_bc = num_bc + 1;
            }
        }
        fwall << setw(2) << num_bc << "  "; //一共有幾個需要邊界處理 
        for (int n = 0; n < num_bc; n++) {
            fwall << bc_dirs[n] << " "; //引出哪一些編號需要邊界處理
        }
        fwall << "\n";
        // ============================================================================
        // 判據 5 (Criteria 5): 平坦區域方向數驗證
        // ----------------------------------------------------------------------------
        // 目的：驗證在幾何形狀的平坦段（水平區域），邊界條件應恰好使用 5 個方向
        // 
        // 判斷條件：
        //   - |Hy| < 0.01     : 高度值接近零，表示位於平坦區域
        //   - |dHdy| < 0.01   : 高度梯度接近零，表示無斜率變化
        // 
        // 預期結果：
        //   - 平坦段的邊界條件方向數應為 5，對應 D3Q27 中的方向 {5, 11, 12, 15, 16}
        //   - 這些方向代表與平坦底面相交的離散速度方向
        // 
        // 物理意義：
        //   在 LBM 的曲線邊界處理中，平坦表面只需處理垂直於表面的速度分量，
        //   因此邊界條件方向數是固定且已知的
        // ============================================================================
        // 判據 5：平坦段應恰好 5 方向 {5, 11, 12, 15, 16}
        double H_stencil_diff_c5 = fabs(HillFunction(y_g[j + 1]) - HillFunction(y_g[j - 1]));
        if (fabs(Hy) < 0.01 && fabs(dHdy) < 0.01
            && fabs(HillFunction(y_g[j - 1])) < 0.01
            && fabs(HillFunction(y_g[j + 1])) < 0.01
            && H_stencil_diff_c5 < 1e-6) { //排除山丘-平坦過渡帶（鄰居 H 差值必須接近零）
            if (num_bc != 5) {
                pass_flat_5dirs = 0;
                fail_count_flat++;
                cout << " FAIL criteria 5: j=" << j << " (flat, H=" << fixed << setprecision(4) << Hy << "), num_BC=" << num_bc << " (expected 5)\n";
            }
        }//需要做邊界處理的編號為反向牆面編號
        // ============================================================================
        // 判據 6 (Criteria 6): 斜面額外方向驗證
        // ----------------------------------------------------------------------------
        // 目的：驗證在幾何形狀的斜面段，邊界條件應包含額外的方向
        // 
        // 判斷條件：
        //   - |dHdy| > 0.1    : 高度梯度顯著，表示存在斜率
        //   - num_bc > 5      : 邊界條件方向數超過平坦段的 5 個
        // 
        // 預期結果：
        //   - 斜面區域需要處理更多的離散速度方向
        //   - 因為傾斜表面會與更多的速度向量相交
        // 
        // 物理意義：
        //   在 GILBM（Generalized Interpolation-based LBM）中，曲線/斜面邊界
        //   需要額外的插值方向來準確表示流體與傾斜壁面的相互作用
        // ============================================================================
        // 判據 6：斜面應有額外方向 (num_bc > 5)
        if (fabs(dHdy) > 0.1) {  // 這是一個斜面點（山坡梯度顯著）
            found_any_slope = 1 ; //斜面區域的計算點 此值 = 1
            if (num_bc <= 5) {  // 斜面點卻只有 ≤5 個方向 → 該下邊界計算點的某一個度量項可能有誤
                pass_slope_extra = 0;
                fail_count_slope++;//統計下邊界斜面區域不通過Pass的計算點個數
                cout << "  FAIL criteria 6: j=" << j
                     << " (slope, |dH/dy|=" << fixed << setprecision(4) << fabs(dHdy)
                     << "), num_BC=" << num_bc << " (expected >5)\n"; 
            }
        }
    }
    fwall.close();







    // ====== Pass/Fail 判據匯總 ======
    cout << "\n----- Pass/Fail Criteria -----\n";

    // 判據 1: dk_dz > 0 全場 //因為每一個點都應該存在hyperbolic tangent 伸縮程度
    int pass1 = 1;
    for (int j = bfr; j < NY6 - bfr; j++) {
        for (int k = 3; k < NZ6 - 3; k++) {          
            if (dk_dz_g[j * NZ6 + k] <= 0.0) {
                pass1 = 0;
                cout << "  FAIL: dk_dz <= 0 at j=" << j << ", k=" << k << "\n";
            }
        }
    }
    cout << "[" << (pass1 ? "PASS" : "FAIL") << "] Criteria 1: dk_dz > 0 everywhere\n";

    // 判據 2: 下壁面 dz_dk 與解析值一致（10% 容差）
    // Buffer=3: k=3 為壁面, 中心差分 dz_dk = (z[4]-z[2])/2
    // z[2] 為外插: 2*z[3]-z[4], 故 dz_dk = z[4]-z[3] = first tanh spacing
    // 注意: tanhFunction_wall 使用單一 a 參數，first spacing 隨 total 線性縮放
    //   expected(j) = minSize * (LZ - Hill(y_j)) / (LZ - Hill(0))
    int pass2 = 1;
    double total_peak = LZ - HillFunction(0.0);  // = LZ - 1.0 (hill peak at y=0)
    for (int j = bfr; j < NY6 - bfr; j++) {
        double total_j = LZ - HillFunction(y_g[j]);
        double expected_dz_dk = minSize * total_j / total_peak;
        double dz_dk_wall = 1.0 / dk_dz_g[j * NZ6 + 3];
        double rel_err = fabs(dz_dk_wall - expected_dz_dk) / expected_dz_dk;
        if (rel_err > 0.1) {
            pass2 = 0;
            cout << "  FAIL: j=" << j
                 << ", dz_dk[wall]=" << scientific << setprecision(6) << dz_dk_wall
                 << ", expected=" << expected_dz_dk
                 << ", rel_err=" << fixed << setprecision(2) << rel_err * 100 << "%\n";
        }
    }
    cout << "[" << (pass2 ? "PASS" : "FAIL") << "] Criteria 2: dz_dk(wall) ~ minSize*total(j)/total_peak (within 10%)\n";

    // 判據 3: 平坦段 dk_dy ≈ 0（掃描所有平坦 j，與判據 5 同條件）
    //  由於山坡曲率存在所引起的度量係數，在平坦區域應趨近於零
    int pass3 = 1;
    for (int j = bfr; j < NY6 - bfr - 1; j++) {
        double Hy_c3 = HillFunction(y_g[j]);
        double dHdy_c3 = (HillFunction(y_g[j + 1]) - HillFunction(y_g[j - 1])) / (2.0 * dy);
        double H_stencil_diff_c3 = fabs(HillFunction(y_g[j + 1]) - HillFunction(y_g[j - 1]));
        if (fabs(Hy_c3) < 0.01 && fabs(dHdy_c3) < 0.01
            && fabs(HillFunction(y_g[j - 1])) < 0.01
            && fabs(HillFunction(y_g[j + 1])) < 0.01
            && H_stencil_diff_c3 < 1e-6) {//排除山丘-平坦過渡帶（鄰居 H 差值必須接近零）
            for (int k = 3; k < NZ6 - 3; k++) {         
                if (fabs(dk_dy_g[j * NZ6 + k]) > 0.1) {
                    pass3 = 0;
                    cout << "  FAIL: flat region j=" << j << " k=" << k
                         << ", dk_dy=" << scientific << setprecision(6) << dk_dy_g[j * NZ6 + k]
                         << " (expected ~0)\n";
                }
            }
        }
    }
    cout << "[" << (pass3 ? "PASS" : "FAIL") << "] Criteria 3: dk_dy ≈ 0 at flat region\n";

    // 判據 4: 斜面 dk_dy 符號正確//dk_dy為因為山坡曲率而存在的度量係數
    int pass4 = 1;
    if (j_slope >= 0) { // j_slope 為最陡峭區域的 j 值
        double dHdy_slope = (HillFunction(y_g[j_slope + 1]) -
                             HillFunction(y_g[j_slope - 1])) / (2.0 * dy); //該j值點的山坡導數
        int k_mid = NZ6 / 2;//選最陡峭ｊ點的垂直中點
        double dk_dy_val = dk_dy_g[j_slope * NZ6 + k_mid];
        // 當 H'(y)>0（山丘上升段），dz_dj>0 → dk_dy<0
        if (dHdy_slope > 0 && dk_dy_val > 0) { //如果最陡峭j值為上升階段，則度量係數應該要<0（因為Jacobian Determination <0)
            pass4 = 0;
            cout << "  FAIL: slope j=" << j_slope << ", H'>0 but dk_dy>0 (sign wrong)\n";
        }
        if (dHdy_slope < 0 && dk_dy_val < 0) { //如果最陡峭區域j值處在下降階段 , 則度量係數>0 (因為因為Jacobian Determination <0)
            pass4 = 0;
            cout << "  FAIL: slope j=" << j_slope << ", H'<0 but dk_dy<0 (sign wrong)\n";
        }
    }
    //判斷4的意義：判斷因為山坡曲率而存在的度量係數是否計算錯誤 。
    cout << "[" << (pass4 ? "PASS" : "FAIL") << "] Criteria 4: dk_dy sign consistent with -H'(y)\n";

    // 判據 5：平坦段壁面恰好 5 個方向需要 BC
    if (pass_flat_5dirs) {
        cout << "[PASS] Criteria 5: flat wall has exactly 5 BC directions\n";
    } else {
        cout << "[FAIL] Criteria 5: flat wall has exactly 5 BC directions ("
             << fail_count_flat << " flat points failed)\n";
    }

    // 判據 6：斜面有額外方向（三態：PASS / FAIL / SKIP）
    if (found_any_slope == 0) {
        // 情況 A：整個計算域內沒有顯著斜面 (所有點的 |dH/dy| ≤ 0.1)
        cout << "[SKIP] Criteria 6: no significant slope found (|dH/dy| > 0.1)\n";
    } else if (pass_slope_extra == 1) {
        // 情況 B：有斜面，且所有斜面點的 BC 方向數都 > 5
        //因為預設pass_extra_slope為1所以當你掃描完所有的計算點後仍然保持為 1，則代表所有的計算點都符合判段
        cout << "[PASS] Criteria 6: slope wall has >5 BC directions\n";
    } else {
        // 情況 C：有斜面，但至少一個斜面點的 BC 方向數 ≤ 5
        cout << "[FAIL] Criteria 6: slope wall has >5 BC directions ("
             << fail_count_slope << " slope points failed)\n";
    }

    cout << "\nDiagnostic files written:\n";
    cout << "  gilbm_metrics.dat           — full field metric terms\n";
    cout << "  gilbm_metrics_selected.dat  — profiles at 3 characteristic j\n";
    cout << "  gilbm_contravariant_wall.dat — wall direction classification\n";
    cout << "===== End Phase 0 Diagnostics =====\n\n";

    free(y_g);
    free(z_g);
    free(dk_dz_g);
    free(dk_dy_g);
}
#endif  // Dead code: legacy 2-term metric functions

#endif

//Pass1: 驗證dk_dz計算正確 ： 全場dk_dz >0（含壁面 k=3 和 k=NZ6-4）
//Pass2: 驗證dk_dz計算正確 ： k=3壁面 dz_dk = minSize（central diff with extrapolated k=2）
//Pass3: 驗證dk_dy計算正確 ： 利用雙重for迴圈計算平坦區段的j值的dk_dy都等於0
//Pass4: 驗證dk_dy計算正確 ： 取最陡峭的j列的垂直中點 ，檢查該點的dk_dy是否與山坡斜率反號
//取k=3計算空間下邊界壁面計算點做判斷
//Pass5: 驗證e_alpha_k的計算正確性：下邊界&&平坦區段：需要做邊界處理的編號個數 = 5
//Pass6: 驗證e_alpha_k計算正確 ： 所有的斜面計算點都應該要有6個以上的編號需要邊界處理

