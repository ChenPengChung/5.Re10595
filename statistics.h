#ifndef STATISTICS_FILE
#define STATISTICS_FILE

// =====================================================================
// statistics.h — High-order statistics accumulation kernels
// Ported from Edit8_GILBM_advFrolich/statistics.h with curvilinear
// Jacobian derivative formulation (MeanDerivatives).
//
// Contains:
//   MeanVars         — 1st/2nd/3rd-order moment accumulation (24 arrays)
//   MeanDerivatives  — Squared velocity gradient accumulation (9 arrays)
//                      using full 2×2 inverse Jacobian for curvilinear coords
//   Launch_TurbulentSum — Launcher calling both kernels on tbsum_stream[0..1]
//
// ⚠  命名公約: 此檔案的累積陣列 (U, V, W, UU, VV, VW, WW, ...) 採 *code-frame* 命名,
//    對應 code 變數 u/v/w (即 spanwise/streamwise/wall-normal)。它們從不直接
//    寫入 VTK; 由 fileIO.h 消費後映射到 ERCOFTAC 公約輸出:
//       code v (streamwise) → ERCOFTAC u (U_mean, uu_RS=<u'u'>)
//       code w (wall-normal)→ ERCOFTAC v (V_mean, vv_RS=<v'v'>)
//       code u (spanwise)   → ERCOFTAC w (W_mean, ww_RS=<w'w'>)
//    例: GPU 陣列 VV (累積 code v²) 會在 fileIO.h 算成 ERCOFTAC <u'u'>。
// =====================================================================

// MeanVars: accumulate 1st, 2nd, 3rd-order velocity/pressure moments
// Pressure definition: p = (1/3)*rho - 1/3  (LBM equation of state, gauge pressure)
__global__ void MeanVars(
          double *U,        double *V,        double *W,        double *P,
          double *UU,       double *UV,       double *UW,       double *VV,       double *VW,       double *WW,
          double *PU,       double *PV,       double *PW,       double *PP,
          double *UUU,      double *UUV,      double *UUW,
          double *UVW_acc,
          double *VVU,      double *VVV,      double *VVW,
          double *WWU,      double *WWV,      double *WWW,
    const double *u,  const double *v,  const double *w,  const double *rho  )
{
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    const int j = blockIdx.y * blockDim.y + threadIdx.y;
    const int k = blockIdx.z * blockDim.z + threadIdx.z;

    const int index  = j*NX6*NZ6 + k*NX6 + i;

    if( i <= 2 || i >= NX6-3 || j <= 2 || j >= NYD6-3 || k <= 3 || k >= NZ6-4 ) return;

    const double u1 = u[index];
    const double v1 = v[index];
    const double w1 = w[index];
    const double p1 = 1.0/3.0*rho[index] - 1.0/3.0;

    // 1st-order moments (sums)
    U[index] += u1;
    V[index] += v1;
    W[index] += w1;
    P[index] += p1;

    // 2nd-order moments (Reynolds stress + pressure correlations)
    UU[index] += u1 * u1;
    UV[index] += u1 * v1;
    UW[index] += u1 * w1;
    VV[index] += v1 * v1;
    VW[index] += v1 * w1;
    WW[index] += w1 * w1;

    PP[index] += p1 * p1;
    PU[index] += p1 * u1;
    PV[index] += p1 * v1;
    PW[index] += p1 * w1;

    // 3rd-order moments (triple correlations)
    UUU[index] += u1 * u1 * u1;
    UUV[index] += u1 * u1 * v1;
    UUW[index] += u1 * u1 * w1;
    UVW_acc[index] += u1 * v1 * w1;
    VVU[index] += v1 * v1 * u1;
    VVV[index] += v1 * v1 * v1;
    VVW[index] += v1 * v1 * w1;
    WWU[index] += w1 * w1 * u1;
    WWV[index] += w1 * w1 * v1;
    WWW[index] += w1 * w1 * w1;

}

// MeanDerivatives: accumulate squared velocity gradients using full 2×2 inverse Jacobian
// Curvilinear coordinate system (η=i uniform, ξ=j, ζ=k non-uniform):
//   ∂φ/∂x = (1/dx) ∂φ/∂η                        (x uniform, no cross-term)
//   ∂φ/∂y = ξ_y · ∂φ/∂ξ + ζ_y · ∂φ/∂ζ          (full Jacobian)
//   ∂φ/∂z = ξ_z · ∂φ/∂ξ + ζ_z · ∂φ/∂ζ          (full Jacobian)
__global__ void MeanDerivatives(
          double *DUDX2,      double *DUDY2,        double *DUDZ2,
          double *DVDX2,      double *DVDY2,        double *DVDZ2,
          double *DWDX2,      double *DWDY2,        double *DWDZ2,
    const double *xi_y_in,    const double *xi_z_in,
    const double *zeta_y_in,  const double *zeta_z_in,
    const double *u,    const double *v,    const double *w,
    const double *x  )
{
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    const int j = blockIdx.y * blockDim.y + threadIdx.y;
    const int k = blockIdx.z * blockDim.z + threadIdx.z;

    const int index = j*NX6*NZ6 + k*NX6 + i;

    if( i <= 2 || i >= NX6-3 || j <= 2 || j >= NYD6-3 || k <= 3 || k >= NZ6-4 ) return;

    // x-direction: ∂φ/∂x = (1/dx) · ∂φ/∂η  (6th-order central diff)
    // (-f[-3] + 9*f[-2] - 45*f[-1] + 45*f[+1] - 9*f[+2] + f[+3]) / 60
    double dx_inv = 1.0 / (x[i+1] - x[i]);
    double dudx = (-u[index-3] + 9.0*u[index-2] - 45.0*u[index-1] + 45.0*u[index+1] - 9.0*u[index+2] + u[index+3]) / 60.0 * dx_inv;
    double dvdx = (-v[index-3] + 9.0*v[index-2] - 45.0*v[index-1] + 45.0*v[index+1] - 9.0*v[index+2] + v[index+3]) / 60.0 * dx_inv;
    double dwdx = (-w[index-3] + 9.0*w[index-2] - 45.0*w[index-1] + 45.0*w[index+1] - 9.0*w[index+2] + w[index+3]) / 60.0 * dx_inv;

    // ζ-direction finite difference: ∂φ/∂ζ (6th-order)
    double du_dk = (-u[index-3*NX6] + 9.0*u[index-2*NX6] - 45.0*u[index-NX6] + 45.0*u[index+NX6] - 9.0*u[index+2*NX6] + u[index+3*NX6]) / 60.0;
    double dv_dk = (-v[index-3*NX6] + 9.0*v[index-2*NX6] - 45.0*v[index-NX6] + 45.0*v[index+NX6] - 9.0*v[index+2*NX6] + v[index+3*NX6]) / 60.0;
    double dw_dk = (-w[index-3*NX6] + 9.0*w[index-2*NX6] - 45.0*w[index-NX6] + 45.0*w[index+NX6] - 9.0*w[index+2*NX6] + w[index+3*NX6]) / 60.0;

    // ξ-direction finite difference: ∂φ/∂ξ (6th-order)
    const int nface = NX6 * NZ6;
    double du_dj = (-u[index-3*nface] + 9.0*u[index-2*nface] - 45.0*u[index-nface] + 45.0*u[index+nface] - 9.0*u[index+2*nface] + u[index+3*nface]) / 60.0;
    double dv_dj = (-v[index-3*nface] + 9.0*v[index-2*nface] - 45.0*v[index-nface] + 45.0*v[index+nface] - 9.0*v[index+2*nface] + v[index+3*nface]) / 60.0;
    double dw_dj = (-w[index-3*nface] + 9.0*w[index-2*nface] - 45.0*w[index-nface] + 45.0*w[index+nface] - 9.0*w[index+2*nface] + w[index+3*nface]) / 60.0;

    // Inverse Jacobian at (j,k)
    const int jk = j * NZ6 + k;
    double xiy  = xi_y_in[jk];
    double xiz  = xi_z_in[jk];
    double ztay = zeta_y_in[jk];
    double ztaz = zeta_z_in[jk];

    // y-direction: ∂φ/∂y = ξ_y·∂φ/∂ξ + ζ_y·∂φ/∂ζ
    double dudy = du_dj * xiy + du_dk * ztay;
    double dvdy = dv_dj * xiy + dv_dk * ztay;
    double dwdy = dw_dj * xiy + dw_dk * ztay;

    // z-direction: ∂φ/∂z = ξ_z·∂φ/∂ξ + ζ_z·∂φ/∂ζ
    double dudz = du_dj * xiz + du_dk * ztaz;
    double dvdz = dv_dj * xiz + dv_dk * ztaz;
    double dwdz = dw_dj * xiz + dw_dk * ztaz;

    DUDX2[index] += dudx * dudx;
    DUDY2[index] += dudy * dudy;
    DUDZ2[index] += dudz * dudz;

    DVDX2[index] += dvdx * dvdx;
    DVDY2[index] += dvdy * dvdy;
    DVDZ2[index] += dvdz * dvdz;

    DWDX2[index] += dwdx * dwdx;
    DWDY2[index] += dwdy * dwdy;
    DWDZ2[index] += dwdz * dwdz;
}

void Launch_TurbulentSum() {
    dim3 griddimTB( NX6/NT+1, NYD6, NZ6 );
    dim3 blockdimTB(NT,     1,      1);

    MeanVars<<<griddimTB, blockdimTB, 0, tbsum_stream[0]>>>(
        U,   V,   W,   P,
        UU,  UV,  UW,  VV,  VW,  WW,  PU,  PV,  PW,  PP,
        UUU, UUV, UUW, UVW, VVU, VVV, VVW, WWU, WWV, WWW,
        u, v, w, rho_d
    );
    CHECK_CUDA( cudaGetLastError() );

    MeanDerivatives<<<griddimTB, blockdimTB, 0, tbsum_stream[1]>>>(
        DUDX2, DUDY2, DUDZ2, DVDX2, DVDY2, DVDZ2, DWDX2, DWDY2, DWDZ2,
        xi_y_d, xi_z_d, zeta_y_d, zeta_z_d,
        u, v, w,
        x_d
    );
    CHECK_CUDA( cudaGetLastError() );

    for( int i = 0; i < 2; i++ ){
        CHECK_CUDA( cudaStreamSynchronize(tbsum_stream[i]) );
    }

    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );
}

#endif
