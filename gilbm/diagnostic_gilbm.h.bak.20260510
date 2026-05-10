#ifndef GILBM_DIAGNOSTIC_H
#define GILBM_DIAGNOSTIC_H

// Phase 1.5 Acceptance Diagnostic (Imamura 2005 GILBM)
// 2026-04 重構版: 不再依賴預計算的 delta_xi/delta_zeta 陣列。
// 改為從 Jacobian (xi_y, xi_z, zeta_y, zeta_z) 即時計算診斷值。
// Call ONCE after initialization, before main time loop.
// Prints: (0) CFL check from Jacobians, (1) per-direction max CFL,
//         (2) interpolation spot-check, (3) C-E BC spot-check.
// All computation is host-side — no GPU kernel needed.

void DiagnoseGILBM_Phase1(
    const double *xi_y_h,        // [NYD6*NZ6] ∂ξ/∂y
    const double *xi_z_h,        // [NYD6*NZ6] ∂ξ/∂z
    const double *zeta_y_h,      // [NYD6*NZ6] ∂ζ/∂y
    const double *zeta_z_h,      // [NYD6*NZ6] ∂ζ/∂z
    double **fh_p_local,         // host distribution pointers [19]
    int NYD6_local,
    int NZ6_local,
    int myid_local,
    double dt_global_val,        // dt_global
    int init_mode                // INIT: 0=cold start, 1=binary restart, 2=VTK restart
) {
    if (myid_local != 0) return;

    // D3Q19 velocity set (host copy)
    double e[19][3] = {
        {0,0,0},
        {1,0,0},{-1,0,0},{0,1,0},{0,-1,0},{0,0,1},{0,0,-1},
        {1,1,0},{-1,1,0},{1,-1,0},{-1,-1,0},
        {1,0,1},{-1,0,1},{1,0,-1},{-1,0,-1},
        {0,1,1},{0,-1,1},{0,1,-1},{0,-1,-1}
    };
    double W[19] = {
        1.0/3.0,
        1.0/18.0, 1.0/18.0, 1.0/18.0, 1.0/18.0, 1.0/18.0, 1.0/18.0,
        1.0/36.0, 1.0/36.0, 1.0/36.0, 1.0/36.0,
        1.0/36.0, 1.0/36.0, 1.0/36.0, 1.0/36.0,
        1.0/36.0, 1.0/36.0, 1.0/36.0, 1.0/36.0
    };

    double dx_val = LX / (double)(NX6 - 7);

    printf("\n");
    printf("=============================================================\n");
    printf("  GILBM Phase 1.5 Acceptance Diagnostic (Rank 0, t=0)\n");
    printf("  NYD6=%d, NZ6=%d, NX6=%d, dt_global=%.6e, tau_Cartesian=%.4f, tau_global=%.4f\n",
           NYD6_local, NZ6_local, (int)NX6, dt_global_val,
           3.0*niu/minSize + 0.5, omega_global);
    printf("  [On-the-fly mode: displacements computed from Jacobians]\n");
    printf("=============================================================\n");

    // ==================================================================
    // TEST 0: CFL check from Jacobian (1st-order, no RK2)
    // ==================================================================
    // CFL_η = dt * |ex| / dx
    // CFL_ξ = dt * |ey·xi_y + ez·xi_z|
    // CFL_ζ = dt * |ey·zeta_y + ez·zeta_z|
    printf("\n[Test 0] CFL check from Jacobians (1st-order, all 3 directions)\n");
    {
        double max_cfl_eta = 0.0, max_cfl_xi = 0.0, max_cfl_zeta = 0.0;
        int max_xi_a = -1, max_xi_j = -1, max_xi_k = -1;
        int max_zeta_a = -1, max_zeta_j = -1, max_zeta_k = -1;

        // η: max CFL is simply dt/dx (for |ex|=1 directions)
        max_cfl_eta = dt_global_val / dx_val;

        for (int j = 3; j < NYD6_local - 3; j++) {
            for (int k = 3; k <= NZ6_local - 4; k++) {
                int idx_jk = j * NZ6_local + k;
                double xiy  = xi_y_h[idx_jk];
                double xiz  = xi_z_h[idx_jk];
                double ztay = zeta_y_h[idx_jk];
                double ztaz = zeta_z_h[idx_jk];

                for (int alpha = 1; alpha < 19; alpha++) {
                    double ey = e[alpha][1], ez = e[alpha][2];
                    if (ey == 0.0 && ez == 0.0) continue;

                    double cfl_xi   = fabs(ey * xiy + ez * xiz) * dt_global_val;
                    double cfl_zeta = fabs(ey * ztay + ez * ztaz) * dt_global_val;

                    if (cfl_xi > max_cfl_xi) {
                        max_cfl_xi = cfl_xi; max_xi_a = alpha; max_xi_j = j; max_xi_k = k;
                    }
                    if (cfl_zeta > max_cfl_zeta) {
                        max_cfl_zeta = cfl_zeta; max_zeta_a = alpha; max_zeta_j = j; max_zeta_k = k;
                    }
                }
            }
        }

        printf("  CFL_eta  = %.6f  (dt/dx, constant)  %s\n",
               max_cfl_eta, max_cfl_eta < 1.0 ? "PASS" : "FAIL");
        printf("  CFL_xi   = %.6f  at alpha=%d, j=%d, k=%d  %s\n",
               max_cfl_xi, max_xi_a, max_xi_j, max_xi_k,
               max_cfl_xi < 0.8 ? "PASS" : (max_cfl_xi < 1.0 ? "WARN" : "FAIL"));
        printf("  CFL_zeta = %.6f  at alpha=%d, j=%d, k=%d  %s\n",
               max_cfl_zeta, max_zeta_a, max_zeta_j, max_zeta_k,
               max_cfl_zeta < 0.8 ? "PASS" : (max_cfl_zeta < 1.0 ? "WARN" : "FAIL"));
    }

    // ==================================================================
    // TEST 1: Per-direction max CFL_ζ breakdown
    // ==================================================================
    printf("\n[Test 1] Per-direction max CFL_zeta (1st-order):\n");
    printf("  %5s  %8s  %12s  %12s\n", "alpha", "e(y,z)", "max_CFL_xi", "max_CFL_zeta");
    for (int alpha = 1; alpha < 19; alpha++) {
        if (e[alpha][1] == 0.0 && e[alpha][2] == 0.0) continue;
        double amax_xi = 0.0, amax_zeta = 0.0;
        for (int j = 3; j < NYD6_local - 3; j++) {
            for (int k = 3; k <= NZ6_local - 4; k++) {
                int idx = j * NZ6_local + k;
                double cfl_xi   = fabs(e[alpha][1] * xi_y_h[idx]   + e[alpha][2] * xi_z_h[idx])   * dt_global_val;
                double cfl_zeta = fabs(e[alpha][1] * zeta_y_h[idx] + e[alpha][2] * zeta_z_h[idx]) * dt_global_val;
                if (cfl_xi > amax_xi) amax_xi = cfl_xi;
                if (cfl_zeta > amax_zeta) amax_zeta = cfl_zeta;
            }
        }
        printf("  %5d  (%+.0f,%+.0f)  %12.6f  %12.6f\n",
               alpha, e[alpha][1], e[alpha][2], amax_xi, amax_zeta);
    }

    // ==================================================================
    // TEST 2: Interpolation spot-check (host-side)
    // ==================================================================
    double max_err = 0.0;
    if (init_mode > 0) {
        printf("\n[Test 2] Interpolation spot-check — SKIP (restart: f != w_alpha)\n");
        printf("  This test assumes cold-start equilibrium (u=0, f=w_alpha).\n");
        printf("  Restart flow has non-zero velocity -> test not applicable.\n");
    } else {
    printf("\n[Test 2] Interpolation spot-check (equilibrium f, u=0, rho=1)\n");
    printf("  At t=0: f_alpha = w_alpha everywhere.\n");
    printf("  Interpolating at any upwind point should return w_alpha exactly.\n\n");

    int ti = (int)NX6 / 2;
    int tj = NYD6_local / 2;
    int tk = NZ6_local / 2;
    printf("  Test point: i=%d, j=%d, k=%d\n", ti, tj, tk);

    printf("  %5s  %14s  %14s  %10s\n", "alpha", "interpolated", "expected(w_a)", "error");
    for (int alpha = 1; alpha < 19; alpha++) {
        int idx_jk = tj * NZ6_local + tk;

        // Compute displacements on-the-fly from Jacobians (1st-order, no RK2 for diagnostic)
        double delta_i = dt_global_val * e[alpha][0] / dx_val;
        double delta_xi_val = dt_global_val * (e[alpha][1] * xi_y_h[idx_jk] + e[alpha][2] * xi_z_h[idx_jk]);
        double delta_zeta_val = dt_global_val * (e[alpha][1] * zeta_y_h[idx_jk] + e[alpha][2] * zeta_z_h[idx_jk]);

        double up_i = (double)ti - delta_i;
        double up_j = (double)tj - delta_xi_val;
        double up_k = (double)tk - delta_zeta_val;

        // Clamp (same as kernel)
        if (up_i < 1.0) up_i = 1.0;
        if (up_i > (double)(NX6 - 3)) up_i = (double)(NX6 - 3);
        if (up_j < 1.0) up_j = 1.0;
        if (up_j > (double)(NYD6_local - 3)) up_j = (double)(NYD6_local - 3);
        if (up_k < 2.0) up_k = 2.0;
        if (up_k > (double)(NZ6_local - 5)) up_k = (double)(NZ6_local - 5);

        // Host-side quadratic interpolation (replica of interpolate_quadratic_3d)
        int bi = (int)floor(up_i);
        int bj = (int)floor(up_j);
        int bk = (int)floor(up_k);
        double fi = up_i - (double)bi;
        double fj = up_j - (double)bj;
        double fk = up_k - (double)bk;

        double ai[3], aj[3], ak[3];
        ai[0] = 0.5*(fi-1.0)*(fi-2.0); ai[1] = -fi*(fi-2.0); ai[2] = 0.5*fi*(fi-1.0);
        aj[0] = 0.5*(fj-1.0)*(fj-2.0); aj[1] = -fj*(fj-2.0); aj[2] = 0.5*fj*(fj-1.0);
        ak[0] = 0.5*(fk-1.0)*(fk-2.0); ak[1] = -fk*(fk-2.0); ak[2] = 0.5*fk*(fk-1.0);

        double result = 0.0;
        for (int n = 0; n < 3; n++) {
            for (int m = 0; m < 3; m++) {
                double wjk = aj[m] * ak[n];
                for (int l = 0; l < 3; l++) {
                    int idx = (bj+m)*NZ6_local*NX6 + (bk+n)*NX6 + (bi+l);
                    result += ai[l] * wjk * fh_p_local[alpha][idx];
                }
            }
        }

        double err = fabs(result - W[alpha]);
        if (err > max_err) max_err = err;

        printf("  %5d  %14.10e  %14.10e  %10.2e\n", alpha, result, W[alpha], err);
    }

    printf("\n  max interpolation error (18 dirs): %.2e\n", max_err);
    if (max_err > 1e-12) {
        printf("  ** WARNING: Error > 1e-12 on uniform equilibrium!\n");
        printf("     Possible cause: array layout mismatch or uninitialized ghost cells.\n");
    } else {
        printf("  PASS: Interpolation reproduces constant field exactly.\n");
    }
    } // end if (init_mode == 0)

    // ==================================================================
    // TEST 3: Chapman-Enskog BC spot-check (bottom wall k=3)
    // ==================================================================
    double sum_f_CE = 0.0, sum_w = 0.0;
    double bc_rho_wall = 1.0;
    if (init_mode > 0) {
        printf("\n[Test 3] Chapman-Enskog BC spot-check — SKIP (restart: u != 0)\n");
        printf("  This test assumes cold-start equilibrium (u=0, du/dk=0, C_alpha=0).\n");
        printf("  Restart flow has non-zero velocity gradients -> test not applicable.\n");
    } else {
    printf("\n[Test 3] Chapman-Enskog BC spot-check (bottom wall, k=3)\n");
    printf("  At t=0: u=0 everywhere -> du/dk=0 -> C_alpha=0\n");
    printf("  Expected: f_CE = w_alpha * rho_wall\n\n");

    int bc_i = (int)NX6 / 2;
    int bc_j = NYD6_local / 2;
    int bc_k = 3;  // bottom wall
    int bc_idx_jk = bc_j * NZ6_local + bc_k;
    double bc_ztay = zeta_y_h[bc_idx_jk];
    double bc_ztaz = zeta_z_h[bc_idx_jk];

    printf("  Wall point: i=%d, j=%d, k=%d\n", bc_i, bc_j, bc_k);
    printf("  Metric: dk/dy = %+.6e, dk/dz = %+.6e\n", bc_ztay, bc_ztaz);

    // Compute macroscopic at k=4,5,6,7,8,9 (6 interior points above wall k=3)
    // 6th-order forward difference with u[wall]=0 Dirichlet BC
    int idx3 = bc_j * NX6 * NZ6_local + 4 * NX6 + bc_i;
    int idx4 = bc_j * NX6 * NZ6_local + 5 * NX6 + bc_i;
    int idx5 = bc_j * NX6 * NZ6_local + 6 * NX6 + bc_i;
    int idx6 = bc_j * NX6 * NZ6_local + 7 * NX6 + bc_i;
    int idx7 = bc_j * NX6 * NZ6_local + 8 * NX6 + bc_i;
    int idx8 = bc_j * NX6 * NZ6_local + 9 * NX6 + bc_i;

    double rho3 = 0.0, rho4 = 0.0, rho5 = 0.0, rho6 = 0.0, rho7 = 0.0, rho8 = 0.0;
    double f3[19], f4[19], f5[19], f6[19], f7[19], f8[19];
    for (int a = 0; a < 19; a++) { f3[a] = fh_p_local[a][idx3]; rho3 += f3[a]; }
    for (int a = 0; a < 19; a++) { f4[a] = fh_p_local[a][idx4]; rho4 += f4[a]; }
    for (int a = 0; a < 19; a++) { f5[a] = fh_p_local[a][idx5]; rho5 += f5[a]; }
    for (int a = 0; a < 19; a++) { f6[a] = fh_p_local[a][idx6]; rho6 += f6[a]; }
    for (int a = 0; a < 19; a++) { f7[a] = fh_p_local[a][idx7]; rho7 += f7[a]; }
    for (int a = 0; a < 19; a++) { f8[a] = fh_p_local[a][idx8]; rho8 += f8[a]; }

    double ux3 = (f3[1]+f3[7]+f3[9]+f3[11]+f3[13] - (f3[2]+f3[8]+f3[10]+f3[12]+f3[14])) / rho3;
    double uy3 = (f3[3]+f3[7]+f3[8]+f3[15]+f3[17] - (f3[4]+f3[9]+f3[10]+f3[16]+f3[18])) / rho3;
    double uz3 = (f3[5]+f3[11]+f3[12]+f3[15]+f3[16] - (f3[6]+f3[13]+f3[14]+f3[17]+f3[18])) / rho3;

    double ux4 = (f4[1]+f4[7]+f4[9]+f4[11]+f4[13] - (f4[2]+f4[8]+f4[10]+f4[12]+f4[14])) / rho4;
    double uy4 = (f4[3]+f4[7]+f4[8]+f4[15]+f4[17] - (f4[4]+f4[9]+f4[10]+f4[16]+f4[18])) / rho4;
    double uz4 = (f4[5]+f4[11]+f4[12]+f4[15]+f4[16] - (f4[6]+f4[13]+f4[14]+f4[17]+f4[18])) / rho4;

    double ux5 = (f5[1]+f5[7]+f5[9]+f5[11]+f5[13] - (f5[2]+f5[8]+f5[10]+f5[12]+f5[14])) / rho5;
    double uy5 = (f5[3]+f5[7]+f5[8]+f5[15]+f5[17] - (f5[4]+f5[9]+f5[10]+f5[16]+f5[18])) / rho5;
    double uz5 = (f5[5]+f5[11]+f5[12]+f5[15]+f5[16] - (f5[6]+f5[13]+f5[14]+f5[17]+f5[18])) / rho5;

    double ux6 = (f6[1]+f6[7]+f6[9]+f6[11]+f6[13] - (f6[2]+f6[8]+f6[10]+f6[12]+f6[14])) / rho6;
    double uy6 = (f6[3]+f6[7]+f6[8]+f6[15]+f6[17] - (f6[4]+f6[9]+f6[10]+f6[16]+f6[18])) / rho6;
    double uz6 = (f6[5]+f6[11]+f6[12]+f6[15]+f6[16] - (f6[6]+f6[13]+f6[14]+f6[17]+f6[18])) / rho6;

    double ux7 = (f7[1]+f7[7]+f7[9]+f7[11]+f7[13] - (f7[2]+f7[8]+f7[10]+f7[12]+f7[14])) / rho7;
    double uy7 = (f7[3]+f7[7]+f7[8]+f7[15]+f7[17] - (f7[4]+f7[9]+f7[10]+f7[16]+f7[18])) / rho7;
    double uz7 = (f7[5]+f7[11]+f7[12]+f7[15]+f7[16] - (f7[6]+f7[13]+f7[14]+f7[17]+f7[18])) / rho7;

    double ux8 = (f8[1]+f8[7]+f8[9]+f8[11]+f8[13] - (f8[2]+f8[8]+f8[10]+f8[12]+f8[14])) / rho8;
    double uy8 = (f8[3]+f8[7]+f8[8]+f8[15]+f8[17] - (f8[4]+f8[9]+f8[10]+f8[16]+f8[18])) / rho8;
    double uz8 = (f8[5]+f8[11]+f8[12]+f8[15]+f8[16] - (f8[6]+f8[13]+f8[14]+f8[17]+f8[18])) / rho8;

    // 6th-order: du/dk = (360*u₁ - 450*u₂ + 400*u₃ - 225*u₄ + 72*u₅ - 10*u₆) / 60
    double du_x_dk = (360.0*ux3 - 450.0*ux4 + 400.0*ux5 - 225.0*ux6 + 72.0*ux7 - 10.0*ux8) / 60.0;
    double du_y_dk = (360.0*uy3 - 450.0*uy4 + 400.0*uy5 - 225.0*uy6 + 72.0*uy7 - 10.0*uy8) / 60.0;
    double du_z_dk = (360.0*uz3 - 450.0*uz4 + 400.0*uz5 - 225.0*uz6 + 72.0*uz7 - 10.0*uz8) / 60.0;

    bc_rho_wall = rho3;
    printf("  rho_wall (from k=4) = %.10f\n", rho3);
    printf("  du/dk at wall: (%.6e, %.6e, %.6e)\n", du_x_dk, du_y_dk, du_z_dk);
    printf("  [At t=0 with u=0 init, du/dk should be ~0]\n");

    printf("\n  C-E BC per direction needing BC at bottom wall:\n");
    printf("  %5s  %6s  %6s  %12s  %12s  %12s  %12s\n",
           "alpha", "e_y", "e_z", "e_tilde_zeta", "C_alpha", "f_CE", "w_alpha");

    int bc_count = 0;
    for (int alpha = 1; alpha < 19; alpha++) {
        double e_tilde_zeta = e[alpha][1] * bc_ztay + e[alpha][2] * bc_ztaz;
        if (e_tilde_zeta <= 0.0) continue;  // doesn't need BC at bottom wall
        bc_count++;

        // Host-side replica of ChapmanEnskogBC
        double ex = e[alpha][0], ey = e[alpha][1], ez = e[alpha][2];
        double C_alpha = 0.0;
        C_alpha += du_x_dk * ((3.0*ex*ey)*bc_ztay + (3.0*ex*ez)*bc_ztaz);
        C_alpha += du_y_dk * ((3.0*ey*ey - 1.0)*bc_ztay + (3.0*ey*ez)*bc_ztaz);
        C_alpha += du_z_dk * ((3.0*ez*ey)*bc_ztay + (3.0*ez*ez - 1.0)*bc_ztaz);
        // [GTS] CE 理論: f^neq ∝ -(τ-0.5)·Δt = -3ν
        C_alpha *= -(omega_global - 0.5) * dt_global_val;

        double f_CE = W[alpha] * rho3 * (1.0 + C_alpha);
        sum_f_CE += f_CE;

        printf("  %5d  %+5.0f  %+5.0f  %+12.4f  %+12.6e  %12.8f  %12.8f\n",
               alpha, ey, ez, e_tilde_zeta, C_alpha, f_CE, W[alpha]);
    }

    printf("\n  Directions needing BC: %d / 18\n", bc_count);
    printf("  Sum(f_CE) across BC directions: %.10f\n", sum_f_CE);
    printf("  Sum(w_alpha) for same directions: ");
    for (int alpha = 1; alpha < 19; alpha++) {
        double e_tilde_zeta = e[alpha][1] * bc_ztay + e[alpha][2] * bc_ztaz;
        if (e_tilde_zeta > 0.0) sum_w += W[alpha];
    }
    printf("%.10f\n", sum_w);
    printf("  Difference: %.2e  (should be ~0 at t=0)\n", fabs(sum_f_CE - sum_w * rho3));
    } // end if (init_mode == 0)

    // Summary
    printf("\n=============================================================\n");
    printf("  Phase 1.5 Acceptance Summary (INIT=%d):\n", init_mode);
    printf("  [0-1] CFL from Jacobians: see above (computed on-the-fly)\n");
    if (init_mode > 0) {
        printf("  [2] Interpolation spot-check   SKIP (restart)\n");
        printf("  [3] C-E BC spot-check          SKIP (restart)\n");
    } else {
        printf("  [2] Interpolation error = %.2e  %s\n", max_err,
               max_err < 1e-12 ? "PASS" : "FAIL");
        printf("  [3] C-E BC consistency = %.2e  %s\n",
               fabs(sum_f_CE - sum_w * bc_rho_wall),
               fabs(sum_f_CE - sum_w * bc_rho_wall) < 1e-12 ? "PASS" : "FAIL");
    }
    printf("=============================================================\n\n");
}

// ==============================================================================
// ValidateDepartureCFL — simplified: compute CFL on-the-fly from Jacobians
// ==============================================================================
// No longer needs precomputed delta arrays. Computes CFL directly from
// contravariant velocity = ey*zeta_y + ez*zeta_z (1st-order, no RK2).
// RK2 correction is small (CFL < 1 guarantees < 0.5 grid spacing shift at midpoint).

bool ValidateDepartureCFL(
    const double *xi_y_h,        // [NYD6*NZ6] ∂ξ/∂y
    const double *xi_z_h,        // [NYD6*NZ6] ∂ξ/∂z
    const double *zeta_y_h,      // [NYD6*NZ6] ∂ζ/∂y
    const double *zeta_z_h,      // [NYD6*NZ6] ∂ζ/∂z
    int NYD6_local,
    int NZ6_local,
    int myid_local,
    double dt_global_val
) {
    if (myid_local != 0) return true;

    double e[19][3] = {
        {0,0,0},
        {1,0,0},{-1,0,0},{0,1,0},{0,-1,0},{0,0,1},{0,0,-1},
        {1,1,0},{-1,1,0},{1,-1,0},{-1,-1,0},
        {1,0,1},{-1,0,1},{1,0,-1},{-1,0,-1},
        {0,1,1},{0,-1,1},{0,1,-1},{0,-1,-1}
    };

    bool valid = true;
    double dx_val = LX / (double)(NX6 - 7);

    printf("\n");
    printf("=============================================================\n");
    printf("  CFL Validation (from Jacobians, 1st-order)\n");
    printf("  dt_global=%.6e, dx=%.6e, NZ6=%d, NYD6=%d\n",
           dt_global_val, dx_val, NZ6_local, NYD6_local);
    printf("=============================================================\n");

    // η CFL
    double cfl_eta = dt_global_val / dx_val;
    printf("  CFL_eta = %.6f  %s\n", cfl_eta, cfl_eta < 1.0 ? "PASS" : "FAIL");
    if (cfl_eta >= 1.0) valid = false;

    // ξ and ζ CFL
    double max_cfl_xi = 0.0, max_cfl_zeta = 0.0;
    int worst_xi_j = -1, worst_xi_k = -1, worst_xi_a = -1;
    int worst_zeta_j = -1, worst_zeta_k = -1, worst_zeta_a = -1;
    int xi_violations = 0, zeta_violations = 0;

    for (int j = 3; j < NYD6_local - 3; j++) {
        for (int k = 3; k <= NZ6_local - 4; k++) {
            int idx = j * NZ6_local + k;
            double xiy  = xi_y_h[idx];
            double xiz  = xi_z_h[idx];
            double ztay = zeta_y_h[idx];
            double ztaz = zeta_z_h[idx];

            for (int alpha = 1; alpha < 19; alpha++) {
                double ey = e[alpha][1], ez = e[alpha][2];
                if (ey == 0.0 && ez == 0.0) continue;

                double cfl_xi   = fabs(ey * xiy + ez * xiz) * dt_global_val;
                double cfl_zeta = fabs(ey * ztay + ez * ztaz) * dt_global_val;

                if (cfl_xi > max_cfl_xi) {
                    max_cfl_xi = cfl_xi; worst_xi_j = j; worst_xi_k = k; worst_xi_a = alpha;
                }
                if (cfl_zeta > max_cfl_zeta) {
                    max_cfl_zeta = cfl_zeta; worst_zeta_j = j; worst_zeta_k = k; worst_zeta_a = alpha;
                }
                if (cfl_xi >= 1.0) xi_violations++;
                if (cfl_zeta >= 1.0) zeta_violations++;
            }
        }
    }

    printf("  CFL_xi   = %.6f at j=%d,k=%d,alpha=%d  violations=%d  %s\n",
           max_cfl_xi, worst_xi_j, worst_xi_k, worst_xi_a, xi_violations,
           max_cfl_xi < 0.8 ? "PASS" : (max_cfl_xi < 1.0 ? "WARN" : "FAIL"));
    printf("  CFL_zeta = %.6f at j=%d,k=%d,alpha=%d  violations=%d  %s\n",
           max_cfl_zeta, worst_zeta_j, worst_zeta_k, worst_zeta_a, zeta_violations,
           max_cfl_zeta < 0.8 ? "PASS" : (max_cfl_zeta < 1.0 ? "WARN" : "FAIL"));

    if (max_cfl_xi >= 1.0 || max_cfl_zeta >= 1.0) valid = false;

    // Wall-adjacent CFL (k=4 bottom, k=NZ6-5 top)
    printf("\n  [Wall-adjacent CFL_zeta]\n");
    for (int wall = 0; wall < 2; wall++) {
        int kw = (wall == 0) ? 4 : NZ6_local - 5;
        const char *label = (wall == 0) ? "Bottom k=4" : "Top";
        double wmax = 0.0;
        int wj = -1, wa = -1;
        for (int j = 3; j < NYD6_local - 3; j++) {
            int idx = j * NZ6_local + kw;
            for (int alpha = 1; alpha < 19; alpha++) {
                double ey = e[alpha][1], ez = e[alpha][2];
                if (ey == 0.0 && ez == 0.0) continue;
                double cfl = fabs(ey * zeta_y_h[idx] + ez * zeta_z_h[idx]) * dt_global_val;
                if (cfl > wmax) { wmax = cfl; wj = j; wa = alpha; }
            }
        }
        printf("  %s (k=%d): max CFL_zeta = %.6f at j=%d, alpha=%d  %s\n",
               label, kw, wmax, wj, wa,
               wmax < 0.8 ? "PASS" : (wmax < 1.0 ? "WARN" : "FAIL"));
    }

    printf("=============================================================\n\n");
    return valid;
}

#endif
