// ════════════════════════════════════════════════════════════════════════════
//  Algorithm2 A/B field-equivalence GPU harness (Round-C obligation)
//  ─────────────────────────────────────────────────────────────────
//  Runs Algorithm1 and Algorithm2 fused kernels ONE step from IDENTICAL
//  synthetic inputs on a single GPU (no MPI), then per-double memcmp of
//  f_post_write / u / v / w / rho. This closes the consumer-side FP-codegen
//  residual that the §B5 coords-table gate cannot cover (the two kernels are
//  textually identical downstream of the table read, but are separately
//  compiled __global__ functions).
//
//  Wall-BC race fix: wall rows (k=3, NZ6-4) read a read-only macro snapshot
//  and write a separate macro output buffer. Therefore the HARD gate covers
//  the full domain, including wall rows. A1-vs-A1 rerun measures the full
//  determinism floor.
//
//  The MRT __constant__ tables are deliberately left zero-initialized:
//  both kernels read the SAME constants, so collision degenerates to an
//  identical pass-through for both — the A/B compares exactly the STREAMING
//  delta (table-read vs in-kernel RK2), which is the only changed code.
//
//  Build (worktree, login node OK — compile only):
//    nvcc -arch=sm_90 -O3 -I.. tests/algo2_ab_gpu_test.cu -o tests/algo2_ab_gpu
//  Run (needs ONE GPU — coordinate with the user for the slot):
//    ./tests/algo2_ab_gpu        (exit 0 = PASS)
// ════════════════════════════════════════════════════════════════════════════
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>

#include "variables.h"
#include "gilbm/evolution_gilbm/1.algorithm1.h"
#include "gilbm/precompute2.h"
#include "gilbm/evolution_gilbm/2.algorithm2.h"

#define ABCHK(call) do { cudaError_t e_ = (call); if (e_ != cudaSuccess) { \
    fprintf(stderr, "[AB] CUDA error %s @ %s:%d\n", cudaGetErrorString(e_), __FILE__, __LINE__); \
    exit(3); } } while (0)

// local replica of PrecomputeGILBM_StencilBaseK (precompute.h:236-251) — keeps
// this TU free of precompute.h's MPI dependencies
static void bk_table(int *bk_h) {
    for (int k = 0; k < (int)NZ6; k++) {
        int bk = k - 3;
        if (bk < 0)             bk = 0;
        if (bk + 6 >= (int)NZ6) bk = (int)NZ6 - 7;
        bk_h[k] = bk;
    }
}

// deterministic smooth synthetic fields (no RNG)
static inline double smet(double base, double amp, int j, int k, double pj, double pk) {
    return base + amp * sin(pj * j) * cos(pk * k);
}

int main() {
    const size_t NJK   = (size_t)NYD6 * NZ6;
    const size_t GRID  = (size_t)GRID_SIZE;
    const size_t FSZ   = 19 * GRID;
    const int    NTH   = NT;

    printf("[AB] grid: NX6=%d NYD6=%d NZ6=%d  GRID_SIZE=%zu  f-buffer=%.1f MiB\n",
           (int)NX6, (int)NYD6, (int)NZ6, GRID, FSZ * 8.0 / 1048576.0);

    // ── synthetic metric: smooth, NON-constant, contravariant magnitude ~O(40) ──
    double *xi_y_h   = (double*)malloc(NJK * 8), *xi_z_h   = (double*)malloc(NJK * 8);
    double *zeta_y_h = (double*)malloc(NJK * 8), *zeta_z_h = (double*)malloc(NJK * 8);
    for (int j = 0; j < (int)NYD6; j++)
        for (int k = 0; k < (int)NZ6; k++) {
            size_t n = (size_t)j * NZ6 + k;
            xi_y_h[n]   = smet(40.0, 4.0,  j, k, 0.31, 0.071);
            xi_z_h[n]   = smet( 0.0, 1.5,  j, k, 0.17, 0.093);
            zeta_y_h[n] = smet( 0.0, 1.2,  j, k, 0.23, 0.057);
            zeta_z_h[n] = smet(45.0, 5.0,  j, k, 0.13, 0.041);
        }

    // dt: CFL 0.95 against max contravariant over {eta=1/dx, |xi|, |zeta|}
    const double inv_dx = (double)(NX6 - 7) / LX;
    double cmax = inv_dx;
    for (size_t n = 0; n < NJK; n++) {
        double cxi = fabs(xi_y_h[n]) + fabs(xi_z_h[n]);
        double cze = fabs(zeta_y_h[n]) + fabs(zeta_z_h[n]);
        if (cxi > cmax) cmax = cxi;
        if (cze > cmax) cmax = cze;
    }
    const double dt_val = 0.95 / cmax;
    printf("[AB] inv_dx=%.4f cmax=%.4f dt=%.6e (xi/zeta displacement up to ~%.2f cells)\n",
           inv_dx, cmax, dt_val, dt_val * 50.0);

    // ── __constant__ uploads (both kernels read the SAME values) ──
    ABCHK(cudaMemcpyToSymbol(GILBM_dt, &dt_val, sizeof(double)));
    ABCHK(cudaMemcpyToSymbol(GILBM_inv_dx, &inv_dx, sizeof(double)));
    {
        double L_eta[2][7];
        for (int s = 0; s < 2; s++) {
            const double ex = (s == 0) ? 1.0 : -1.0;
            double t_eta = 3.0 - dt_val * ex * inv_dx;
            if (t_eta < 0.0) t_eta = 0.0;
            if (t_eta > 6.0) t_eta = 6.0;
            gilbm2_lagrange7(t_eta, L_eta[s]);   // hardcoded form == kernel's
        }
        ABCHK(cudaMemcpyToSymbol(GILBM_L_eta_shared, L_eta, sizeof(L_eta)));
    }
    {
        double omega = 1.6, s_visc = 1.0 / 1.6;
        ABCHK(cudaMemcpyToSymbol(GILBM_omega_global, &omega, sizeof(double)));
        ABCHK(cudaMemcpyToSymbol(GILBM_s_visc_global, &s_visc, sizeof(double)));
    }
    // MRT K/Fproj tables: intentionally zero (see header note)

    // ── device geometry/state buffers ──
    double *xi_y_d, *xi_z_d, *zeta_y_d, *zeta_z_d;
    int *bk_h = (int*)malloc(NZ6 * sizeof(int)), *bk_d;
    bk_table(bk_h);
    ABCHK(cudaMalloc(&xi_y_d, NJK * 8));   ABCHK(cudaMalloc(&xi_z_d, NJK * 8));
    ABCHK(cudaMalloc(&zeta_y_d, NJK * 8)); ABCHK(cudaMalloc(&zeta_z_d, NJK * 8));
    ABCHK(cudaMalloc(&bk_d, NZ6 * sizeof(int)));
    ABCHK(cudaMemcpy(xi_y_d, xi_y_h, NJK * 8, cudaMemcpyHostToDevice));
    ABCHK(cudaMemcpy(xi_z_d, xi_z_h, NJK * 8, cudaMemcpyHostToDevice));
    ABCHK(cudaMemcpy(zeta_y_d, zeta_y_h, NJK * 8, cudaMemcpyHostToDevice));
    ABCHK(cudaMemcpy(zeta_z_d, zeta_z_h, NJK * 8, cudaMemcpyHostToDevice));
    ABCHK(cudaMemcpy(bk_d, bk_h, NZ6 * sizeof(int), cudaMemcpyHostToDevice));

    // ── Algorithm2 departure table (COORDS 或 WEIGHTS): device build + §B5 gate ──
    printf("[AB] STORE mode = %s (%zu B/entry)\n",
           (GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS_FOLDED ? "WEIGHTS_FOLDED" :
            GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS        ? "WEIGHTS" : "COORDS"),
           sizeof(GILBM2_Table));
    GILBM2_Table *coords_d;
    const size_t NTAB = (size_t)GILBM2_NCLASS * NYD6 * NZ6;
    ABCHK(cudaMalloc(&coords_d, NTAB * sizeof(GILBM2_Table)));
    {
        const int nb = (int)((NTAB + 255) / 256);
        Algorithm2_BuildCoordsTable_Device<<<nb, 256>>>(coords_d,
            xi_y_d, xi_z_d, zeta_y_d, zeta_z_d, bk_d);
        ABCHK(cudaDeviceSynchronize());
        GILBM2_Table *tab_h = (GILBM2_Table*)malloc(NTAB * sizeof(GILBM2_Table));
        BuildGILBM2DepartureTableHost(tab_h, xi_y_h, xi_z_h, zeta_y_h, zeta_z_h, bk_h, dt_val);
        GILBM2_ValidationResult vr;
        int rc = Algorithm2_ValidateCoordsTable(coords_d, xi_y_d, xi_z_d, zeta_y_d, zeta_z_d,
                                                bk_d, tab_h, &vr, 0);
#if GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS_FOLDED
        long long folded_shape_bad = 0;
        int bad_cls = -1, bad_j = -1, bad_k = -1;
        for (int cls = 1; cls < GILBM2_NCLASS; cls++)
            for (int j = 3; j < (int)NYD6 - 3; j++)
                for (int k = 3; k < (int)NZ6 - 3; k++) {
                    const GILBM2_Table &c = tab_h[gilbm2_coord_index(cls, j, k)];
                    bool ok = (c.k_idx[0] >= 3 && c.k_idx[6] <= (int)NZ6 - 4);
                    for (int s = 1; s < 7; s++) {
                        if (c.k_idx[s] != c.k_idx[0] + s) ok = false;
                    }
                    if (!ok) {
                        folded_shape_bad++;
                        if (bad_cls < 0) { bad_cls = cls; bad_j = j; bad_k = k; }
                    }
                }
        printf("[AB] folded table shape: contiguous physical k_idx bad=%lld", folded_shape_bad);
        if (folded_shape_bad)
            printf(" (first cls=%d j=%d k=%d)", bad_cls, bad_j, bad_k);
        printf("\n");
        if (folded_shape_bad != 0) {
            free(tab_h);
            fprintf(stderr, "[AB] FATAL: folded table k_idx is not ITB-style contiguous physical window\n");
            return 5;
        }
#endif
        free(tab_h);
        if (rc != 0) { fprintf(stderr, "[AB] FATAL: coords-table gate failed (rc=%d)\n", rc); return 4; }
    }

    // ── synthetic f + macroscopic state ──
    double *f_in_h = (double*)malloc(FSZ * 8);
    const double Wq[19] = { 1.0/3,
        1.0/18,1.0/18,1.0/18,1.0/18,1.0/18,1.0/18,
        1.0/36,1.0/36,1.0/36,1.0/36,1.0/36,1.0/36,1.0/36,1.0/36,
        1.0/36,1.0/36,1.0/36,1.0/36 };
    for (int q = 0; q < 19; q++)
        for (int j = 0; j < (int)NYD6; j++)
            for (int k = 0; k < (int)NZ6; k++)
                for (int i = 0; i < (int)NX6; i++) {
                    size_t idx = (size_t)q * GRID + (size_t)j * NX6 * NZ6 + (size_t)k * NX6 + i;
                    f_in_h[idx] = Wq[q] * (1.0
                        + 0.04 * sin(0.11 * i + 0.05 * q) * cos(0.07 * j)
                        + 0.03 * sin(0.05 * k + 0.13 * q));
                }
    double *u0_h = (double*)malloc(GRID * 8), *v0_h = (double*)malloc(GRID * 8),
           *w0_h = (double*)malloc(GRID * 8), *r0_h = (double*)malloc(GRID * 8);
    for (int j = 0; j < (int)NYD6; j++)
        for (int k = 0; k < (int)NZ6; k++)
            for (int i = 0; i < (int)NX6; i++) {
                size_t idx = (size_t)j * NX6 * NZ6 + (size_t)k * NX6 + i;
                u0_h[idx] = 0.01 * sin(0.09 * i) * cos(0.06 * k);
                v0_h[idx] = 0.012 * cos(0.08 * j) * sin(0.05 * k);
                w0_h[idx] = 0.008 * sin(0.04 * (i + j + k));
                r0_h[idx] = 1.0 + 0.002 * cos(0.06 * i) * sin(0.07 * j);
            }

    double *f_in_d, *f_out_d;
    double *u_d, *v_d, *w_d, *r_d;
    double *u_out_d, *v_out_d, *w_out_d, *r_out_d;
    double *rho_mod_d, *force_d;
    ABCHK(cudaMalloc(&f_in_d, FSZ * 8));  ABCHK(cudaMalloc(&f_out_d, FSZ * 8));
    ABCHK(cudaMalloc(&u_d, GRID * 8));    ABCHK(cudaMalloc(&v_d, GRID * 8));
    ABCHK(cudaMalloc(&w_d, GRID * 8));    ABCHK(cudaMalloc(&r_d, GRID * 8));
    ABCHK(cudaMalloc(&u_out_d, GRID * 8)); ABCHK(cudaMalloc(&v_out_d, GRID * 8));
    ABCHK(cudaMalloc(&w_out_d, GRID * 8)); ABCHK(cudaMalloc(&r_out_d, GRID * 8));
    ABCHK(cudaMalloc(&rho_mod_d, 8));     ABCHK(cudaMalloc(&force_d, 8));
    ABCHK(cudaMemcpy(f_in_d, f_in_h, FSZ * 8, cudaMemcpyHostToDevice));
    { double z = 0.0, frc = 1.0e-6;
      ABCHK(cudaMemcpy(rho_mod_d, &z, 8, cudaMemcpyHostToDevice));
      ABCHK(cudaMemcpy(force_d, &frc, 8, cudaMemcpyHostToDevice)); }

    // one launch covering ALL interior j=3..NYD6-4 (j-coverage 與 production
    // 的 5-launch 拆分等價 — 同一 device function、同 guard、無跨 launch 依賴)
    dim3 grid_all((unsigned)(NX6 / NTH + 1), (unsigned)(NYD6 - 6), (unsigned)NZ6);
    dim3 block_all((unsigned)NTH, 1, 1);

    auto reset_state = [&]() {
        ABCHK(cudaMemcpy(u_d, u0_h, GRID * 8, cudaMemcpyHostToDevice));
        ABCHK(cudaMemcpy(v_d, v0_h, GRID * 8, cudaMemcpyHostToDevice));
        ABCHK(cudaMemcpy(w_d, w0_h, GRID * 8, cudaMemcpyHostToDevice));
        ABCHK(cudaMemcpy(r_d, r0_h, GRID * 8, cudaMemcpyHostToDevice));
        ABCHK(cudaMemcpy(u_out_d, u0_h, GRID * 8, cudaMemcpyHostToDevice));
        ABCHK(cudaMemcpy(v_out_d, v0_h, GRID * 8, cudaMemcpyHostToDevice));
        ABCHK(cudaMemcpy(w_out_d, w0_h, GRID * 8, cudaMemcpyHostToDevice));
        ABCHK(cudaMemcpy(r_out_d, r0_h, GRID * 8, cudaMemcpyHostToDevice));
        ABCHK(cudaMemset(f_out_d, 0, FSZ * 8));
    };

    double *fA = (double*)malloc(FSZ * 8), *fA2 = (double*)malloc(FSZ * 8), *fB = (double*)malloc(FSZ * 8);
    double *mA = (double*)malloc(4 * GRID * 8), *mB = (double*)malloc(4 * GRID * 8);

    auto fetch = [&](double *fdst, double *mdst) {
        ABCHK(cudaMemcpy(fdst, f_out_d, FSZ * 8, cudaMemcpyDeviceToHost));
        if (mdst) {
            ABCHK(cudaMemcpy(mdst + 0 * GRID, u_out_d, GRID * 8, cudaMemcpyDeviceToHost));
            ABCHK(cudaMemcpy(mdst + 1 * GRID, v_out_d, GRID * 8, cudaMemcpyDeviceToHost));
            ABCHK(cudaMemcpy(mdst + 2 * GRID, w_out_d, GRID * 8, cudaMemcpyDeviceToHost));
            ABCHK(cudaMemcpy(mdst + 3 * GRID, r_out_d, GRID * 8, cudaMemcpyDeviceToHost));
        }
    };

    // RUN 1: Algorithm1
    reset_state();
    Algorithm1_FusedKernel_GTS_Buffer<<<grid_all, block_all>>>(f_in_d, f_out_d,
        zeta_z_d, zeta_y_d, xi_y_d, xi_z_d, bk_d, nullptr,
        u_d, v_d, w_d, r_d,
        u_out_d, v_out_d, w_out_d, r_out_d,
        rho_mod_d, force_d, 3);
    ABCHK(cudaDeviceSynchronize());
    fetch(fA, mA);

    // RUN 2: Algorithm1 again (determinism floor)
    reset_state();
    Algorithm1_FusedKernel_GTS_Buffer<<<grid_all, block_all>>>(f_in_d, f_out_d,
        zeta_z_d, zeta_y_d, xi_y_d, xi_z_d, bk_d, nullptr,
        u_d, v_d, w_d, r_d,
        u_out_d, v_out_d, w_out_d, r_out_d,
        rho_mod_d, force_d, 3);
    ABCHK(cudaDeviceSynchronize());
    fetch(fA2, nullptr);

    // RUN 3: Algorithm2
    reset_state();
    Algorithm2_FusedKernel_GTS_Buffer<<<grid_all, block_all>>>(f_in_d, f_out_d,
        zeta_z_d, zeta_y_d, xi_y_d, xi_z_d, bk_d, nullptr,
        u_d, v_d, w_d, r_d,
        u_out_d, v_out_d, w_out_d, r_out_d,
        rho_mod_d, force_d, coords_d, 3);
    ABCHK(cudaDeviceSynchronize());
    fetch(fB, mB);

    // ── comparison ──
    //   COORDS/WEIGHTS: 期望 bit-exact → TOL=0 (memcmp)。
    //   WEIGHTS_FOLDED: ghost 折疊重結合 FP → 期望 1e-12-equivalent → TOL=1e-12 (Efficiency Rule #2)。
    const double TOL = (GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS_FOLDED) ? 1.0e-12 : 0.0;
#if GILBM2_DEPARTURE_RK4
    //   RK4: A2(RK4 departure) 故意偏離 A1(in-kernel RK2) → 等效性僅 RK2(-DGILBM2_DEPARTURE_RK4=0) 模式成立。
    //   此 A/B 對 RK4 = bounded-delta: TOL 仍小(量到真實 max gap), verdict 接受 max gap < RK4_GAP_SANITY 的
    //   預期 departure gap, 只抓 gross 發散 (NaN / O(0.1) garbage = 壞 RK4 consumer)。SANITY 可依實機量測收緊。
    const bool   RK4_MODE = true;
    const double RK4_GAP_SANITY = 1.0e-2;
#else
    const bool   RK4_MODE = false;
    const double RK4_GAP_SANITY = 0.0;
#endif
    printf("[AB] A2-vs-A1 gate: %s (TOL=%.0e)\n",
           (RK4_MODE ? "RK4 bounded-delta (A2=RK4 故意≠A1=RK2; verdict 用 GAP_SANITY)" :
            TOL > 0.0 ? "1e-12 tolerance (FOLDED)" : "bit-exact (memcmp)"), TOL);
    long long floor_mm = 0, nonwall_mm = 0, wall_mm = 0, macro_mm = 0;
    double max_nonwall = 0.0, max_wall = 0.0;
    for (int q = 0; q < 19; q++)
        for (int j = 3; j < (int)NYD6 - 3; j++)
            for (int k = 3; k < (int)NZ6 - 3; k++)
                for (int i = 3; i < (int)NX6 - 3; i++) {
                    size_t idx = (size_t)q * GRID + (size_t)j * NX6 * NZ6 + (size_t)k * NX6 + i;
                    const bool wall = (k == 3 || k == (int)NZ6 - 4);
                    // 地板永遠 bitwise (A1 自身決定性, 與 TOL 無關), 全域含 wall。
                    if (memcmp(&fA[idx], &fA2[idx], 8) != 0) floor_mm++;
                    const double d = fabs(fA[idx] - fB[idx]);
                    const bool mism = (TOL > 0.0) ? (d > TOL) : (memcmp(&fA[idx], &fB[idx], 8) != 0);
                    if (mism) {
                        if (wall) { wall_mm++;    if (d > max_wall)    max_wall = d; }
                        else      { nonwall_mm++; if (d > max_nonwall) max_nonwall = d; }
                    }
                }
    for (int m = 0; m < 4; m++)
        for (int j = 3; j < (int)NYD6 - 3; j++)
            for (int k = 3; k < (int)NZ6 - 3; k++)   // full domain incl wall rows
                for (int i = 3; i < (int)NX6 - 3; i++) {
                    size_t idx = (size_t)m * GRID + (size_t)j * NX6 * NZ6 + (size_t)k * NX6 + i;
                    const double dm = fabs(mA[idx] - mB[idx]);
                    const bool mm = (TOL > 0.0) ? (dm > TOL) : (memcmp(&mA[idx], &mB[idx], 8) != 0);
                    if (mm) macro_mm++;
                }

    printf("[AB] A1-vs-A1 determinism floor : %lld mismatches (expect 0)\n", floor_mm);
    printf("[AB] A1-vs-A2 f non-wall           : %lld mismatches, max|d|=%.3e (expect 0)\n",
           nonwall_mm, max_nonwall);
    printf("[AB] A1-vs-A2 f wall rows (hard gate): %lld mismatches, max|d|=%.3e (expect 0)\n", wall_mm, max_wall);
    printf("[AB] A1-vs-A2 u/v/w/rho full-domain : %lld mismatches (expect 0)\n", macro_mm);

    int rc;
    if (floor_mm != 0) {
        printf("[AB] RESULT: INCONCLUSIVE — Algorithm1 itself nondeterministic on this device (race floor != 0)\n");
        rc = 2;
    } else if (nonwall_mm == 0 && macro_mm == 0 && wall_mm == 0) {
        printf("[AB] RESULT: PASS — Algorithm2 %s to Algorithm1 (full domain incl wall rows)\n",
               (TOL > 0.0 ? "1e-12-equivalent" : "bit-identical"));
        rc = 0;
    } else if (RK4_MODE && isfinite(max_nonwall) && isfinite(max_wall) &&
               max_nonwall < RK4_GAP_SANITY && max_wall < RK4_GAP_SANITY) {
        printf("[AB] RESULT: PASS (RK4 bounded-delta) — A2(RK4) 故意偏離 A1(RK2) ~departure gap "
               "(max non-wall=%.3e, wall=%.3e < %.0e); 等效性僅 RK2(-DGILBM2_DEPARTURE_RK4=0) 模式成立, "
               "RK4 consumer 未 gross 發散\n",
               max_nonwall, max_wall, RK4_GAP_SANITY);
        rc = 0;
    } else {
        printf("[AB] RESULT: FAIL — Algorithm2 diverges from Algorithm1 beyond %s\n",
               (RK4_MODE ? "RK4 bounded-delta (gross departure error / NaN?)" :
                TOL > 0.0 ? "1e-12" : "bit-exact"));
        rc = 1;
    }
    return rc;
}
