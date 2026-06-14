#ifndef EVOLUTION_FILE
#define EVOLUTION_FILE

#include "MRT_Process.h"
#include "MRT_Matrix.h"
#include "gilbm/evolution_gilbm/1.algorithm1.h"   // Algorithm1_FusedKernel_GTS (方案B 融合 kernel)
#include "gilbm/evolution_gilbm/2.algorithm2.h"   // Algorithm2 folded table consumer (dispatch below)


// ===== GPU reduction kernel: sum rho_d over interior points =====
// Legacy unweighted path retained for quick A/B diagnostics.
__global__ void ReduceRhoSum_Kernel(const double *rho_d, double *partial_sums_d) {
    extern __shared__ double sdata[];
    const int tid = threadIdx.x;
    const int gid = blockIdx.x * blockDim.x + threadIdx.x;

    // Interior dimensions: i∈[3,NX6-4), j∈[3,NYD6-4), k∈[3,NZ6-3)
    const int ni = NX6 - 7;
    const int nk = NZ6 - 6;
    const int nj = NYD6 - 7;
    const int total = ni * nj * nk;

    double val = 0.0;
    if (gid < total) {
        int j = gid / (ni * nk) + 3;
        int rem = gid % (ni * nk);
        int k = rem / ni + 3;
        int i = rem % ni + 3;
        val = rho_d[j * NX6 * NZ6 + k * NX6 + i];
    }

    sdata[tid] = val;
    __syncthreads();

    for (unsigned int s = blockDim.x / 2; s > 0; s >>= 1) {
        if (tid < s) sdata[tid] += sdata[tid + s];
        __syncthreads();
    }

    if (tid == 0) partial_sums_d[blockIdx.x] = sdata[0];
}

// ===== GPU reduction kernel: sum rho_d * control-volume weight =====
// Uses the same unique-node domain as the mass correction actually modifies:
//   i = 3..NX6-5, j = 3..NYD6-5, k = 3..NZ6-4.
// Weights are built from true curvilinear cell volumes on the host at startup.
__global__ void ReduceRhoWeightedSum_Kernel(
    const double *rho_d,
    const double *rho_weight_d,
    double *partial_sums_d)
{
    extern __shared__ double sdata[];
    const int tid = threadIdx.x;
    const int gid = blockIdx.x * blockDim.x + threadIdx.x;

    const int ni = NX6 - 7;
    const int nk = NZ6 - 6;
    const int nj = NYD6 - 7;
    const int total = ni * nj * nk;

    double val = 0.0;
    if (gid < total) {
        int j = gid / (ni * nk) + 3;
        int rem = gid % (ni * nk);
        int k = rem / ni + 3;
        int i = rem % ni + 3;
        const int idx = j * NX6 * NZ6 + k * NX6 + i;
        val = rho_d[idx] * rho_weight_d[idx];
    }

    sdata[tid] = val;
    __syncthreads();

    for (unsigned int s = blockDim.x / 2; s > 0; s >>= 1) {
        if (tid < s) sdata[tid] += sdata[tid + s];
        __syncthreads();
    }

    if (tid == 0) partial_sums_d[blockIdx.x] = sdata[0];
}

static inline double MassCorrectionCellVolume(const int i_cell, const int j_cell, const int k_cell)
{
    const double dx = x_h[i_cell + 1] - x_h[i_cell];

    const int jk00 = j_cell * NZ6 + k_cell;
    const int jk10 = (j_cell + 1) * NZ6 + k_cell;
    const int jk11 = (j_cell + 1) * NZ6 + (k_cell + 1);
    const int jk01 = j_cell * NZ6 + (k_cell + 1);

    const double y0 = y_2d_h[jk00], z0 = z_h[jk00];
    const double y1 = y_2d_h[jk10], z1 = z_h[jk10];
    const double y2 = y_2d_h[jk11], z2 = z_h[jk11];
    const double y3 = y_2d_h[jk01], z3 = z_h[jk01];

    const double area_yz = 0.5 * fabs(
          y0 * z1 - z0 * y1
        + y1 * z2 - z1 * y2
        + y2 * z3 - z2 * y3
        + y3 * z0 - z3 * y0);

    return fabs(dx) * area_yz;
}

void InitializeMassCorrectionWeights()
{
    const size_t grid_size = (size_t)NX6 * NYD6 * NZ6;
    memset(rho_cv_weight_h, 0, grid_size * sizeof(double));

    double local_weight_sum = 0.0;
    double local_min_w = 1.0e300;
    double local_max_w = 0.0;

    for (int j = 3; j < NYD6 - 4; j++) {
    for (int k = 3; k < NZ6  - 3; k++) {
    for (int i = 3; i < NX6  - 4; i++) {
        const int idx = j * NX6 * NZ6 + k * NX6 + i;

        const int i_cells[2] = {
            (i == 3) ? (NX6 - 5) : (i - 1),
            i
        };
        const int j_cells[2] = { j - 1, j };
        int k_cells[2];
        int nk_cells = 0;
        if (k > 3)       k_cells[nk_cells++] = k - 1;
        if (k < NZ6 - 4) k_cells[nk_cells++] = k;

        double weight = 0.0;
        for (int jj = 0; jj < 2; jj++) {
        for (int ii = 0; ii < 2; ii++) {
        for (int kk = 0; kk < nk_cells; kk++) {
            const double vol = MassCorrectionCellVolume(i_cells[ii], j_cells[jj], k_cells[kk]);
            if (!(vol > 0.0) || !std::isfinite(vol)) {
                fprintf(stderr,
                        "[MASS-CORR] FATAL: invalid cell volume at rank=%d cell(i=%d,j=%d,k=%d): %.17e\n",
                        myid, i_cells[ii], j_cells[jj], k_cells[kk], vol);
                MPI_Abort(MPI_COMM_WORLD, 1);
            }
            weight += 0.125 * vol;
        }}}

        rho_cv_weight_h[idx] = weight;
        local_weight_sum += weight;
        if (weight < local_min_w) local_min_w = weight;
        if (weight > local_max_w) local_max_w = weight;
    }}}

    CHECK_MPI( MPI_Allreduce(&local_weight_sum, &rho_cv_global_volume, 1, MPI_DOUBLE, MPI_SUM, MPI_COMM_WORLD) );

    double global_min_w = 0.0, global_max_w = 0.0;
    CHECK_MPI( MPI_Allreduce(&local_min_w, &global_min_w, 1, MPI_DOUBLE, MPI_MIN, MPI_COMM_WORLD) );
    CHECK_MPI( MPI_Allreduce(&local_max_w, &global_max_w, 1, MPI_DOUBLE, MPI_MAX, MPI_COMM_WORLD) );

    if (!(rho_cv_global_volume > 0.0) || !std::isfinite(rho_cv_global_volume)) {
        if (myid == 0) {
            fprintf(stderr, "[MASS-CORR] FATAL: invalid global control volume %.17e\n",
                    rho_cv_global_volume);
        }
        MPI_Abort(MPI_COMM_WORLD, 1);
    }

    CHECK_CUDA( cudaMemcpy(rho_cv_weight_d, rho_cv_weight_h,
                           grid_size * sizeof(double), cudaMemcpyHostToDevice) );

    if (myid == 0) {
        printf("[MASS-CORR] Volume-weighted density correction ON\n");
        printf("[MASS-CORR]   global control volume = %.15e\n", rho_cv_global_volume);
        printf("[MASS-CORR]   node weight range     = [%.6e, %.6e]\n",
               global_min_w, global_max_w);
    }
}

// ═══════════════════════════════════════════════════════════════════
//  Jacobian-based volume weighting: 3×3 Gauss-Legendre quadrature
//  with 6th-order Lagrange interpolation of J_2D at Gauss points.
//  Compile-time switch: CELL_VOLUME_METHOD (variables.h)
// ═══════════════════════════════════════════════════════════════════

static const double GL3_nodes[3] = {
    0.5 * (1.0 - 0.7745966692414834),   // (1 - sqrt(3/5))/2 ≈ 0.1127
    0.5,
    0.5 * (1.0 + 0.7745966692414834)    // (1 + sqrt(3/5))/2 ≈ 0.8873
};
static const double GL3_weights[3] = {
    5.0 / 18.0,    // ≈ 0.27778
    8.0 / 18.0,    // ≈ 0.44444
    5.0 / 18.0
};

static inline void Lagrange6Weights(double x, int start, double w[6])
{
    for (int m = 0; m < 6; m++) {
        double L = 1.0;
        double xm = (double)(start + m);
        for (int r = 0; r < 6; r++) {
            if (r != m) {
                double xr = (double)(start + r);
                L *= (x - xr) / (xm - xr);
            }
        }
        w[m] = L;
    }
}

static inline bool SelectStencilStart(int cell_idx, int lo, int hi, int *start_out)
{
    int ideal = cell_idx - 2;
    int max_start = hi - 5;
    if (max_start - lo < 0) return false;
    *start_out = (ideal < lo) ? lo : (ideal > max_start) ? max_start : ideal;
    return true;
}

static inline double InterpolateJ2D_Lagrange6(
    double xi_pos, double zeta_pos, int sj, int sk,
    const double *J_2D, int NZ6_local)
{
    double wj[6], wk[6];
    Lagrange6Weights(xi_pos,   sj, wj);
    Lagrange6Weights(zeta_pos, sk, wk);

    double result = 0.0;
    for (int m = 0; m < 6; m++) {
        double row_sum = 0.0;
        for (int n = 0; n < 6; n++)
            row_sum += wk[n] * J_2D[(sj + m) * NZ6_local + (sk + n)];
        result += wj[m] * row_sum;
    }
    return result;
}

#if CELL_VOLUME_METHOD == 1
void ComputeJacobianMassCorrectionWeights(
    const double *J_2D, double *shoelace_global_volume_out)
{
    // After MPI exchange, J_2D ghost rows (j=0..2, j=NYD6-3..NYD6-1) are valid.
    // Keep k-stencils on physical wall-normal nodes only. k=2 and k=NZ6-3 are
    // buffer-side metric rows, so near-wall GL cells use one-sided physical
    // stencils k=3..8 and k=NZ6-9..NZ6-4.
    const int j_lo_J = 0;
    const int j_hi_J = NYD6 - 1;
    const int k_lo_J = 3;
    const int k_hi_J = NZ6  - 4;

    double *shoelace_backup = nullptr;
    const size_t grid_size = (size_t)NX6 * NYD6 * NZ6;

    shoelace_backup = (double *)malloc(grid_size * sizeof(double));
    memcpy(shoelace_backup, rho_cv_weight_h, grid_size * sizeof(double));
    double shoelace_vol = rho_cv_global_volume;
    if (shoelace_global_volume_out) *shoelace_global_volume_out = shoelace_vol;

    memset(rho_cv_weight_h, 0, grid_size * sizeof(double));

    double local_weight_sum = 0.0;
    int local_fallback_count = 0;
    double local_max_rel_diff = 0.0;
    double local_sum_rel_diff = 0.0;
    int    local_cell_count   = 0;

    for (int j = 3; j < NYD6 - 4; j++) {
    for (int k = 3; k < NZ6  - 3; k++) {
    for (int i = 3; i < NX6  - 4; i++) {
        const int idx = j * NX6 * NZ6 + k * NX6 + i;

        const int i_cells[2] = {
            (i == 3) ? (NX6 - 5) : (i - 1),
            i
        };
        const int j_cells[2] = { j - 1, j };
        int k_cells[2];
        int nk_cells = 0;
        if (k > 3)       k_cells[nk_cells++] = k - 1;
        if (k < NZ6 - 4) k_cells[nk_cells++] = k;

        double weight = 0.0;
        for (int jj = 0; jj < 2; jj++) {
        for (int ii = 0; ii < 2; ii++) {
        for (int kk = 0; kk < nk_cells; kk++) {
            const int jc = j_cells[jj];
            const int kc = k_cells[kk];
            const double dx = x_h[i_cells[ii] + 1] - x_h[i_cells[ii]];

            int sj, sk;
            bool sj_ok = SelectStencilStart(jc, j_lo_J, j_hi_J, &sj);
            bool sk_ok = SelectStencilStart(kc, k_lo_J, k_hi_J, &sk);

            double vol_jac;
            bool used_fallback = false;

            if (sj_ok && sk_ok) {
                double area_jac = 0.0;
                for (int a = 0; a < 3; a++) {
                for (int b = 0; b < 3; b++) {
                    double xi_pos   = (double)jc + GL3_nodes[a];
                    double zeta_pos = (double)kc + GL3_nodes[b];
                    double J_val = InterpolateJ2D_Lagrange6(
                        xi_pos, zeta_pos, sj, sk, J_2D, NZ6);
                    if (!std::isfinite(J_val) || J_val <= 0.0) {
                        used_fallback = true;
                        break;
                    }
                    area_jac += GL3_weights[a] * GL3_weights[b] * J_val;
                }
                if (used_fallback) break;
                }
                vol_jac = fabs(dx) * area_jac;
            } else {
                used_fallback = true;
            }

            if (used_fallback) {
                vol_jac = MassCorrectionCellVolume(i_cells[ii], jc, kc);
                local_fallback_count++;
            }

            double vol_shoe = MassCorrectionCellVolume(i_cells[ii], jc, kc);
            if (vol_shoe > 0.0) {
                double rd = fabs(vol_jac - vol_shoe) / vol_shoe;
                if (rd > local_max_rel_diff) local_max_rel_diff = rd;
                local_sum_rel_diff += rd;
                local_cell_count++;
            }

            weight += 0.125 * vol_jac;
        }}}

        rho_cv_weight_h[idx] = weight;
        local_weight_sum += weight;
    }}}

    CHECK_MPI( MPI_Allreduce(&local_weight_sum, &rho_cv_global_volume,
               1, MPI_DOUBLE, MPI_SUM, MPI_COMM_WORLD) );

    CHECK_CUDA( cudaMemcpy(rho_cv_weight_d, rho_cv_weight_h,
                           grid_size * sizeof(double), cudaMemcpyHostToDevice) );

    int    global_fallback = 0;
    double global_max_rd   = 0.0;
    double global_sum_rd   = 0.0;
    int    global_cells    = 0;
    CHECK_MPI( MPI_Reduce(&local_fallback_count, &global_fallback, 1, MPI_INT,    MPI_SUM, 0, MPI_COMM_WORLD) );
    CHECK_MPI( MPI_Reduce(&local_max_rel_diff,   &global_max_rd,   1, MPI_DOUBLE, MPI_MAX, 0, MPI_COMM_WORLD) );
    CHECK_MPI( MPI_Reduce(&local_sum_rel_diff,   &global_sum_rd,   1, MPI_DOUBLE, MPI_SUM, 0, MPI_COMM_WORLD) );
    CHECK_MPI( MPI_Reduce(&local_cell_count,     &global_cells,    1, MPI_INT,    MPI_SUM, 0, MPI_COMM_WORLD) );

    if (myid == 0) {
        printf("[MASS-CORR] Jacobian 3x3 GL volume weighting ON\n");
        printf("[MASS-CORR]   Shoelace  global volume = %.15e\n", shoelace_vol);
        printf("[MASS-CORR]   Jacobian  global volume = %.15e\n", rho_cv_global_volume);
        printf("[MASS-CORR]   Δ(Jac-Shoe)/Shoe        = %.6e\n",
               fabs(rho_cv_global_volume - shoelace_vol) / shoelace_vol);
        printf("[MASS-CORR]   Per-cell max  rel diff   = %.6e\n", global_max_rd);
        printf("[MASS-CORR]   Per-cell mean rel diff   = %.6e\n",
               (global_cells > 0) ? global_sum_rd / global_cells : 0.0);
        printf("[MASS-CORR]   Fallback to Shoelace     = %d cells\n", global_fallback);
    }

    free(shoelace_backup);
}
#endif

static inline double ComputeGlobalDiscreteShoelaceVolume3D()
{
    double local_volume = 0.0;

    // Unique physical cells per rank:
    //   i_cell = 3..NX6-5  (NX-1 spanwise cells)
    //   j_cell = 3..NYD6-5 ((NY-1)/jp streamwise cells; periodic endpoint excluded)
    //   k_cell = 3..NZ6-5  (NZ-1 wall-normal cells)
    // This is the discrete mesh volume that Shoelace guarantees exactly.
    for (int j_cell = 3; j_cell < NYD6 - 4; j_cell++) {
    for (int k_cell = 3; k_cell < NZ6  - 4; k_cell++) {
    for (int i_cell = 3; i_cell < NX6  - 4; i_cell++) {
        local_volume += MassCorrectionCellVolume(i_cell, j_cell, k_cell);
    }}}

    double global_volume = 0.0;
    CHECK_MPI( MPI_Allreduce(&local_volume, &global_volume, 1,
                             MPI_DOUBLE, MPI_SUM, MPI_COMM_WORLD) );
    return global_volume;
}

// ═══════════════════════════════════════════════════════════════════
//  3D 物理域體積驗證:
//    primary: Σ dV vs LX × A_yz_discrete_mesh from unique Shoelace cells
//    reference only: analytic HillFunction integral
// ═══════════════════════════════════════════════════════════════════
void VerifyPhysicalDomainVolume3D(double cv_global_volume, const char *method_name)
{
    const double V_discrete_mesh = ComputeGlobalDiscreteShoelaceVolume3D();

    if (myid != 0) return;

    const int N_QUAD = 10000;
    const double dy = (double)LY / N_QUAD;
    double hill_integral = 0.0;
    for (int m = 0; m <= N_QUAD; m++) {
        double y_val = m * dy;
        double w_simp = (m == 0 || m == N_QUAD) ? 1.0 :
                        (m % 2 == 1) ? 4.0 : 2.0;
        hill_integral += w_simp * HillFunction(y_val);
    }
    hill_integral *= dy / 3.0;

    double V_physical = (double)LX * ((double)LY * (double)LZ - hill_integral);

    double rel_err_discrete = fabs(cv_global_volume - V_discrete_mesh) / V_discrete_mesh;
    double rel_err_analytic = fabs(cv_global_volume - V_physical) / V_physical;
    double mesh_vs_analytic = fabs(V_discrete_mesh - V_physical) / V_physical;

    printf("[VOL-CHECK] === 3D Physical Domain Volume Verification (%s) ===\n", method_name);
    printf("[VOL-CHECK]   V_discrete_mesh = Σ unique Shoelace cells = %.15e\n", V_discrete_mesh);
    printf("[VOL-CHECK]   Σ weights (%s)                       = %.15e\n", method_name, cv_global_volume);
    printf("[VOL-CHECK]   RelErr vs discrete mesh              = %.6e\n", rel_err_discrete);
    printf("[VOL-CHECK]   %s\n", (rel_err_discrete < 1e-12) ? "PASS" : "WARNING: differs from discrete mesh volume");
    printf("[VOL-CHECK]   Reference: ∫₀^LY h(y)dy               = %.15e  (Simpson N=%d)\n", hill_integral, N_QUAD);
    printf("[VOL-CHECK]   Reference: LX×(LY×LZ − ∫h)           = %.15e\n", V_physical);
    printf("[VOL-CHECK]   RelErr vs analytic HillFunction      = %.6e\n", rel_err_analytic);
    printf("[VOL-CHECK]   Discrete mesh vs analytic reference  = %.6e\n", mesh_vs_analytic);
}

static inline double ComputeVolumeWeightedRhoAverageRoot()
{
    const int rho_total = (NX6 - 7) * (NYD6 - 7) * (NZ6 - 6);
    const int rho_threads = 256;
    const int rho_blocks = (rho_total + rho_threads - 1) / rho_threads;

    ReduceRhoWeightedSum_Kernel<<<rho_blocks, rho_threads, rho_threads * sizeof(double)>>>(
        rho_d, rho_cv_weight_d, rho_partial_d);
    CHECK_CUDA( cudaMemcpy(rho_partial_h, rho_partial_d,
                           rho_blocks * sizeof(double), cudaMemcpyDeviceToHost) );

    double rho_LocalWeightedMass = 0.0;
    for (int b = 0; b < rho_blocks; b++) rho_LocalWeightedMass += rho_partial_h[b];

    double rho_GlobalWeightedMass = 0.0;
    MPI_Reduce(&rho_LocalWeightedMass, &rho_GlobalWeightedMass,
               1, MPI_DOUBLE, MPI_SUM, 0, MPI_COMM_WORLD);

    return (myid == 0) ? (rho_GlobalWeightedMass / rho_cv_global_volume) : 0.0;
}

static inline void UpdateVolumeWeightedMassCorrection()
{
    const double rho_avg = ComputeVolumeWeightedRhoAverageRoot();
    if (myid == 0) {
        rho_modify_h[0] = 1.0 - rho_avg;
    }
    MPI_Bcast(rho_modify_h, 1, MPI_DOUBLE, 0, MPI_COMM_WORLD);
    CHECK_CUDA( cudaMemcpy(rho_modify_d, rho_modify_h, sizeof(double), cudaMemcpyHostToDevice) );
}

// ===== Unpack f_post (flat interleaved) → fh_p[q] (host) for D2H transfer =====
// [FIX] Direct GPU→Host copy: f_post[q*GRID_SIZE .. (q+1)*GRID_SIZE] → fh_p[q]
//   Bypasses both the Unpack_FPost_To_FDir kernel AND the ft[] intermediary.
//   f_post is laid out as [q0_all_points][q1_all_points]...[q18_all_points],
//   so f_post + q*GRID_SIZE is a contiguous block of GRID_SIZE doubles = fh_p[q].
//
// [OLD BUG] The previous path was:
//   Launch_UnpackFPost(f_post_read)  → kernel writes ft[q][index] = f_post_read[q*GSIZE+index]
//   SendDataToCPU(ft)               → cudaMemcpy(fh_p[q], ft[q], nBytes, D2H)
//   But fh_p[q] on disk was always W[q] (pure equilibrium), even though f_post_read
//   contained non-equilibrium data. The kernel code appeared correct on static analysis
//   but the ft[] arrays were never updated from their initialization values.
//   Root cause: unknown (possibly compiler/driver issue with 20-pointer kernel param).
//
// [NEW] Direct D2H copy eliminates 2 potential failure points (kernel + ft[]):
//   cudaMemcpy(fh_p[q], f_post_src + q*GRID_SIZE, nBytes, D2H)
//   Simpler, faster (no kernel launch overhead), and immune to ft[] issues.

void Launch_UnpackFPost_Direct(double *f_post_src) {
    const size_t nBytes = (size_t)NX6 * NYD6 * NZ6 * sizeof(double);
    const size_t grid_size = (size_t)NX6 * NYD6 * NZ6;

    // Ensure all GPU work (collision-streaming, MPI, periodicSW) is finished
    CHECK_CUDA( cudaDeviceSynchronize() );

    // Direct D2H: f_post_src[q*grid_size..(q+1)*grid_size] → fh_p[q][0..grid_size]
    for (int q = 0; q < 19; q++) {
        CHECK_CUDA( cudaMemcpy(fh_p[q], f_post_src + q * grid_size, nBytes, cudaMemcpyDeviceToHost) );
    }

    // === DIAGNOSTIC (first VTK only): verify f_post_src contains non-equilibrium ===
    {
        static int diag_count = 0;
        if (diag_count < 2) {
            const int diag_idx = 10 * NX6 * NZ6 + 35 * NX6 + 20;
            if (myid == 0) {
                printf("[DIAG-DIRECT] fh_p[0][%d]=%.15e (W[0]=%.15e %s)\n",
                       diag_idx, fh_p[0][diag_idx], 1.0/3.0,
                       (fh_p[0][diag_idx] == 1.0/3.0) ? "EQUILIBRIUM!" : "OK-nonEq");
                printf("[DIAG-DIRECT] fh_p[3][%d]=%.15e (W[3]=%.15e %s)\n",
                       diag_idx, fh_p[3][diag_idx], 1.0/18.0,
                       (fh_p[3][diag_idx] == 1.0/18.0) ? "EQUILIBRIUM!" : "OK-nonEq");
                printf("[DIAG-DIRECT] fh_p[18][%d]=%.15e (W[18]=%.15e %s)\n",
                       diag_idx, fh_p[18][diag_idx], 1.0/36.0,
                       (fh_p[18][diag_idx] == 1.0/36.0) ? "EQUILIBRIUM!" : "OK-nonEq");
            }
            diag_count++;
        }
    }
}

// Legacy kernel (kept for reference/debugging, not called in normal path)
__global__ void Unpack_FPost_To_FDir(
    double *f0,  double *f1,  double *f2,  double *f3,  double *f4,
    double *f5,  double *f6,  double *f7,  double *f8,  double *f9,
    double *f10, double *f11, double *f12, double *f13, double *f14,
    double *f15, double *f16, double *f17, double *f18,
    const double *f_post)
{
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    const int j = blockIdx.y * blockDim.y + threadIdx.y;
    const int k = blockIdx.z * blockDim.z + threadIdx.z;
    if (i >= NX6 || j >= NYD6 || k >= NZ6) return;
    const int index = j * NX6 * NZ6 + k * NX6 + i;
    const int GSIZE = NX6 * NYD6 * NZ6;
    double *fd_arr[19] = {f0,f1,f2,f3,f4,f5,f6,f7,f8,f9,
                          f10,f11,f12,f13,f14,f15,f16,f17,f18};
    for (int q = 0; q < 19; q++)
        fd_arr[q][index] = f_post[q * GSIZE + index];
}

// ===== Time-average accumulation kernel (GPU-side, FTT-gated in main.cu) =====
// Accumulates all 3 velocity components: u(spanwise), v(streamwise), w(wall-normal)
__global__ void AccumulateTavg_Kernel(double *u_tavg, double *v_tavg, double *w_tavg,
                                      const double *u_src, const double *v_src, const double *w_src, int N) {
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx < N) {
        u_tavg[idx] += u_src[idx];
        v_tavg[idx] += v_src[idx];
        w_tavg[idx] += w_src[idx];
    }
}

void Launch_AccumulateTavg() {
    const int N = NX6 * NYD6 * NZ6;
    const int block = 256;
    const int grid = (N + block - 1) / block;
    AccumulateTavg_Kernel<<<grid, block>>>(u_tavg_d, v_tavg_d, w_tavg_d, u, v, w, N);
}

// ===== Vorticity accumulation kernel (FTT >= FTT_STATS_START, same window as velocity mean) =====
// Full 2×2 inverse Jacobian vorticity:
// ω_x = ∂w/∂y − ∂v/∂z = (dw_dj·ξ_y + dw_dk·ζ_y) − (dv_dj·ξ_z + dv_dk·ζ_z)
// ω_y = ∂u/∂z − ∂w/∂x = (du_dj·ξ_z + du_dk·ζ_z) − (1/dx)·dw_di
// ω_z = ∂v/∂x − ∂u/∂y = (1/dx)·dv_di − (du_dj·ξ_y + du_dk·ζ_y)
__global__ void AccumulateVorticity_Kernel(
    double *ox_tavg, double *oy_tavg, double *oz_tavg,
    const double *u_in, const double *v_in, const double *w_in,
    const double *xi_y_in, const double *xi_z_in,
    const double *zeta_y_in, const double *zeta_z_in,
    double dx_inv)
{
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    const int j = blockIdx.y * blockDim.y + threadIdx.y;
    const int k = blockIdx.z * blockDim.z + threadIdx.z;

    if (i <= 2 || i >= NX6-3 || j <= 2 || j >= NYD6-3 || k <= 3 || k >= NZ6-4) return;

    const int nface = NX6 * NZ6;
    const int index = j * nface + k * NX6 + i;
    const int jk    = j * NZ6 + k;

    // 6th-order central differences in computational coordinates (η=i, ξ=j, ζ=k)
    // (-f[-3] + 9*f[-2] - 45*f[-1] + 45*f[+1] - 9*f[+2] + f[+3]) / 60
    double du_dj = (-u_in[index - 3*nface] + 9.0*u_in[index - 2*nface] - 45.0*u_in[index - nface]
                   + 45.0*u_in[index + nface] - 9.0*u_in[index + 2*nface] + u_in[index + 3*nface]) / 60.0;
    double du_dk = (-u_in[index - 3*NX6] + 9.0*u_in[index - 2*NX6] - 45.0*u_in[index - NX6]
                   + 45.0*u_in[index + NX6] - 9.0*u_in[index + 2*NX6] + u_in[index + 3*NX6]) / 60.0;

    double dv_di = (-v_in[index - 3] + 9.0*v_in[index - 2] - 45.0*v_in[index - 1]
                   + 45.0*v_in[index + 1] - 9.0*v_in[index + 2] + v_in[index + 3]) / 60.0;
    double dv_dj = (-v_in[index - 3*nface] + 9.0*v_in[index - 2*nface] - 45.0*v_in[index - nface]
                   + 45.0*v_in[index + nface] - 9.0*v_in[index + 2*nface] + v_in[index + 3*nface]) / 60.0;
    double dv_dk = (-v_in[index - 3*NX6] + 9.0*v_in[index - 2*NX6] - 45.0*v_in[index - NX6]
                   + 45.0*v_in[index + NX6] - 9.0*v_in[index + 2*NX6] + v_in[index + 3*NX6]) / 60.0;

    double dw_di = (-w_in[index - 3] + 9.0*w_in[index - 2] - 45.0*w_in[index - 1]
                   + 45.0*w_in[index + 1] - 9.0*w_in[index + 2] + w_in[index + 3]) / 60.0;
    double dw_dj = (-w_in[index - 3*nface] + 9.0*w_in[index - 2*nface] - 45.0*w_in[index - nface]
                   + 45.0*w_in[index + nface] - 9.0*w_in[index + 2*nface] + w_in[index + 3*nface]) / 60.0;
    double dw_dk = (-w_in[index - 3*NX6] + 9.0*w_in[index - 2*NX6] - 45.0*w_in[index - NX6]
                   + 45.0*w_in[index + NX6] - 9.0*w_in[index + 2*NX6] + w_in[index + 3*NX6]) / 60.0;

    // Inverse Jacobian at this (j,k) point
    double xiy  = xi_y_in[jk];
    double xiz  = xi_z_in[jk];
    double ztay = zeta_y_in[jk];
    double ztaz = zeta_z_in[jk];

    // Full curvilinear vorticity
    ox_tavg[index] += (dw_dj * xiy + dw_dk * ztay) - (dv_dj * xiz + dv_dk * ztaz);
    oy_tavg[index] += (du_dj * xiz + du_dk * ztaz) - dx_inv * dw_di;
    oz_tavg[index] += dx_inv * dv_di - (du_dj * xiy + du_dk * ztay);
}

void Launch_AccumulateVorticity() {
    dim3 grid(NX6/NT+1, NYD6, NZ6);
    dim3 block(NT, 1, 1);
    double dx_inv = (double)(NX6 - 7) / (double)LX;
    AccumulateVorticity_Kernel<<<grid, block>>>(
        ox_tavg_d, oy_tavg_d, oz_tavg_d,
        u, v, w, xi_y_d, xi_z_d, zeta_y_d, zeta_z_d, dx_inv);
}

__global__ void AccumulateUbulk(double *Ub_avg, double *v)
{
    const int i = blockIdx.x*blockDim.x + threadIdx.x;
    const int j = blockIdx.y*blockDim.y + threadIdx.y + 3;
    const int k = blockIdx.z*blockDim.z + threadIdx.z;

    if( i <= 2 || i >= NX6-3 || k <= 2 || k >= NZ6-3 ) return;

    // Store pure velocity — area weighting done on host with correct 2D z_h
    Ub_avg[k*NX6+i] = v[j*NZ6*NX6+k*NX6+i];
}

// ────────────────────────────────────────────────────────────────────────────
// periodicSW_fpost: η-direction (x) periodic BC for packed f_post[19*GRID]
//
// [P1-η] 選擇性方向交換：只交換 δη≠0 的 10 個方向 (e_x≠0)
//   q=1,2 (±x), q=7-14 (±x±y, ±x±z) → δη = dt·e_x/dx ≠ 0
//   跳過 q=0 (rest), q=3-6 (±y,±z), q=15-18 (±y±z) → δη = 0
//
// 數學依據：δη=0 時 t_eta=3.0 → Lagrange 權重 = [0,0,0,1,0,0,0]
//   buffer zone (i=0,1,2 或 NX6-3..NX6-1) 的 weight=0 → stale 值不影響結果
//   與 MPI P1 (δξ≠0 → 16/19 方向) 相同邏輯
//
// 不搬 u/v/w/rho — Step1 會從正確的 f_post 重新計算
// ────────────────────────────────────────────────────────────────────────────

// δη≠0 directions: e_x ≠ 0 → q=1,2,7,8,9,10,11,12,13,14
// __constant__ memory: broadcast 給同一 warp 的所有 thread (40 bytes, 1 cache line)
__constant__ int GILBM_PSW_ETA_DIRS[10] = {1, 2, 7, 8, 9, 10, 11, 12, 13, 14};

__global__ void periodicSW_fpost(double *f_post, const int grid_size)
{
    const int i = blockIdx.x * blockDim.x + threadIdx.x;   // i = 0,1,2 (buffer width)
    const int j = blockIdx.y * blockDim.y + threadIdx.y;
    const int k = blockIdx.z * blockDim.z + threadIdx.z;
    const int buffer = 3;

    if (j >= NYD6 || k >= NZ6) return;

    const int nface = NX6 * NZ6;

    // Left buffer ← right interior (10 directions only)
    {
        int idx_buf = j * nface + k * NX6 + i;
        int idx_src = idx_buf + (NX6 - 2 * buffer - 1);
        for (int d = 0; d < 10; d++) {
            size_t q_off = (size_t)GILBM_PSW_ETA_DIRS[d] * grid_size;
            f_post[q_off + idx_buf] = f_post[q_off + idx_src];
        }
    }

    // Right buffer ← left interior (10 directions only)
    {
        int idx_buf = j * nface + k * NX6 + (NX6 - 1 - i);
        int idx_src = idx_buf - (NX6 - 2 * buffer - 1);
        for (int d = 0; d < 10; d++) {
            size_t q_off = (size_t)GILBM_PSW_ETA_DIRS[d] * grid_size;
            f_post[q_off + idx_buf] = f_post[q_off + idx_src];
        }
    }
}

// ────────────────────────────────────────────────────────────────────────────
// periodicSW_macro: η-direction (x) periodic BC for macroscopic fields
//
// AccumulateVorticity_Kernel 在 j=3 處讀取 u[j=2] (ghost zone)
// MPI_Exchange_Macro_Packed 只交換 ξ 方向 ghost → 此 kernel 補 η 方向
//
// 4 fields: rho, u, v, w — ALL j, k 都搬 (不篩選方向, 巨觀量全搬)
// Left buffer  i=0,1,2      ← right interior i=NX6-7, NX6-6, NX6-5
// Right buffer i=NX6-3..NX6-1 ← left interior i=3, 4, 5
// ────────────────────────────────────────────────────────────────────────────
__global__ void periodicSW_macro(
    double *rho_d, double *u_d, double *v_d, double *w_d)
{
    const int i = blockIdx.x * blockDim.x + threadIdx.x;   // i = 0,1,2 (buffer width)
    const int j = blockIdx.y * blockDim.y + threadIdx.y;
    const int k = blockIdx.z * blockDim.z + threadIdx.z;
    const int buffer = 3;

    if (i >= buffer || j >= NYD6 || k >= NZ6) return;

    const int nface = NX6 * NZ6;

    // Left buffer ← right interior
    {
        int idx_buf = j * nface + k * NX6 + i;
        int idx_src = idx_buf + (NX6 - 2 * buffer - 1);
        rho_d[idx_buf] = rho_d[idx_src];
        u_d[idx_buf]   = u_d[idx_src];
        v_d[idx_buf]   = v_d[idx_src];
        w_d[idx_buf]   = w_d[idx_src];
    }

    // Right buffer ← left interior
    {
        int idx_buf = j * nface + k * NX6 + (NX6 - 1 - i);
        int idx_src = idx_buf - (NX6 - 2 * buffer - 1);
        rho_d[idx_buf] = rho_d[idx_src];
        u_d[idx_buf]   = u_d[idx_src];
        v_d[idx_buf]   = v_d[idx_src];
        w_d[idx_buf]   = w_d[idx_src];
    }
}

// ════════════════════════════════════════════════════════════════════════════
// Launch_CollisionStreaming — GTS Algorithm1 主迴圈 (方案B 融合版)
// ════════════════════════════════════════════════════════════════════════════
//
// [方案B] Step1+Step3 融合: f_post_read → 插值 → register → 碰撞 → f_post_write
//   省掉: 19 f_new writes + 19 f_new reads (38 DRAM 存取/格點)
//   省掉: Step3 重複巨觀量計算
//
// 三層優化流水線:
//    [P0 v3] Buffer-先行 (精簡版) + Interior-expanded：
//     Phase 1: Buffer kernel 只算 MPI 需要的 6 行（省去 j=3, j=NYD6-4）
//              獨佔 GPU → ~0.15-0.4 ms 完成 (vs v2: ~0.5 ms)
//     Phase 2: Interior 3 launches (j=3, j=7..NYD6-8, j=NYD6-4) 與 MPI overlap
//              j=3 和 j=NYD6-4 與 MPI 並行 → 幾乎免費
//     → Buffer ↓25%, 不增加 critical path
//   [P1] 選擇性方向交換 (16/19 方向, 跳過 q=0,1,2)
//   [P2] Packed MPI + Persistent Communication
//     16 方向打包成 1 連續 buffer → 4 個 MPI persistent request
//
// 時間線:
//   stream1: [Buf-L][Buf-R]→sync→─────────[Pack]→sync→[MPI Start+Wait]→[Unpack]
//   stream0: ─────────────────────[j=3][=== Interior j=7..15 ===][j=NYD6-4]→sync→[periodicSW]
//   重點: Buffer 只算 MPI 需要的行 → 更快完成 → 更早啟動 MPI overlap
//
// [P0 v3 vs v2] Buffer 精簡:
//   v2: Buffer j=3..6 + j=NYD6-7..NYD6-4 = 8 行 (含 2 行 MPI 不需要)
//   v3: Buffer j=4..6 + j=NYD6-7..NYD6-5 = 6 行 (精確 = MPI 打包範圍)
//   j=3, j=NYD6-4 移到 Interior → 與 MPI 重疊 → Buffer ↓25%, Iter 不變或略降
//
// Interior-only: Buffer 已算完 MPI 需要的 6 rows → Interior 算剩餘 11 rows
//   (j=3 + j=7..15 + j=NYD6-4，3 launches 背靠背)
//
// 碰撞後直接交換 f_post_write → ghost zone 被鄰居正確 interior 覆蓋。
// u/v/w/rho 不交換 — 下一次 kernel 從正確的 f_post 重新計算。
// ════════════════════════════════════════════════════════════════════════════

void Launch_CollisionStreaming(double *f_post_read, double *f_post_write) {
    dim3 griddimSW(  1,      NYD6/NT+1, NZ6);
    dim3 blockdimSW( 3, NT,        1 );

    // [P0 v3] Buffer grid: 3 rows of j per launch, blockDim.y=3
    //   只算 MPI 實際需要打包的行（省去 j=3 和 j=NYD6-4 的白做）
    //   j=3, j=NYD6-4 移到 Interior (stream0) 與 MPI 重疊
    dim3 griddimBuf(NX6/NT+1, 1, NZ6);
    dim3 blockdimBuf(NT, 3, 1);

    // [P0 v3] Interior grid: j=7..NYD6-8, 每 block 1 row (blockDim.y=1)
    //   NYD6-14 interior rows (NYD6=23 → 9 rows → 639 blocks)
    //   start_j=7, gridDim.y=NYD6-14 (每 block 1 row j)
    dim3 griddimInt(NX6/NT+1, NYD6 - 14, NZ6);
    dim3 blockdimInt(NT, 1, 1);

    // [P0 v3] 從 Buffer 移出的 2 行：j=3 和 j=NYD6-4，各 1 row
    dim3 griddimRow1(NX6/NT+1, 1, NZ6);

#if USE_TIMING && TIMING_DETAIL
    if (g_timing_sample) cudaEventRecord(g_timing.ev_iter_start, stream0);
    // ev_step1_start 移到 Phase 1 完成後（見下方 sync 後）
    // 這樣 S1_ms = 純 Interior kernel 時間，不含 Buffer 等待
    double t_buf_wtime_start = 0.0;
    if (g_timing_sample) t_buf_wtime_start = MPI_Wtime();
#endif

    // ═══════════════════════════════════════════════════════════════
    // [P0 v3] Phase 1: Buffer kernel — 只算 MPI 打包需要的行
    //   此時 stream0 無工作 → Buffer 獨享全部 56 SMs 和 DRAM 頻寬
    //
    //   [P0 v3 vs v2] Buffer 只算 MPI 送出區，省去 2 行白做:
    //     左邊界 j=4..6  (3 rows, = MPI 送出 iToLeft 的精確範圍)
    //     右邊界 j=NYD6-7..NYD6-5 (3 rows, = MPI 送出 iToRight 的精確範圍)
    //     j=3 和 j=NYD6-4 移到 Interior (stream0) → Buffer ↓25%
    //
    //   106 blocks / 112 per wave ≈ 1 wave → ~0.15-0.4 ms
    // ═══════════════════════════════════════════════════════════════

    // Left boundary: j = 4..6 (3 rows, MPI iToLeft 精確範圍)
#if USE_GILBM_ALGORITHM2
    Algorithm2_FusedKernel_GTS_Buffer<<<griddimBuf, blockdimBuf, 0, stream1>>>(
        f_post_read, f_post_write,
        zeta_z_d, zeta_y_d,
        xi_y_d, xi_z_d, bk_precomp_d,
        z_zeta_d,
        u, v, w, rho_d,
        rho_modify_d, Force_d,
        gilbm2_coords_d,
        4);
#else
    Algorithm1_FusedKernel_GTS_Buffer<<<griddimBuf, blockdimBuf, 0, stream1>>>(
        f_post_read, f_post_write,
        zeta_z_d, zeta_y_d,
        xi_y_d, xi_z_d, bk_precomp_d,
        z_zeta_d,
        u, v, w, rho_d,
        rho_modify_d, Force_d,
#if USE_ITBLBM_STREAMING
        itb_yz_coeff_d,
#endif
        4);
#endif

    // Right boundary: j = NYD6-7..NYD6-5 (3 rows, MPI iToRight 精確範圍)
#if USE_GILBM_ALGORITHM2
    Algorithm2_FusedKernel_GTS_Buffer<<<griddimBuf, blockdimBuf, 0, stream1>>>(
        f_post_read, f_post_write,
        zeta_z_d, zeta_y_d,
        xi_y_d, xi_z_d, bk_precomp_d,
        z_zeta_d,
        u, v, w, rho_d,
        rho_modify_d, Force_d,
        gilbm2_coords_d,
        NYD6 - 7);
#else
    Algorithm1_FusedKernel_GTS_Buffer<<<griddimBuf, blockdimBuf, 0, stream1>>>(
        f_post_read, f_post_write,
        zeta_z_d, zeta_y_d,
        xi_y_d, xi_z_d, bk_precomp_d,
        z_zeta_d,
        u, v, w, rho_d,
        rho_modify_d, Force_d,
#if USE_ITBLBM_STREAMING
        itb_yz_coeff_d,
#endif
        NYD6 - 7);
#endif

    // ── 等 Buffer 獨佔完成（GPU 空閒 → 快速 sync）──
    CHECK_CUDA( cudaStreamSynchronize(stream1) );

#if USE_TIMING && TIMING_DETAIL
    // Buffer 完成: 記錄 Buffer 獨佔時間 (launch + kernel + sync)
    if (g_timing_sample)
        g_timing.last_buf_ms = (float)((MPI_Wtime() - t_buf_wtime_start) * 1000.0);
    // ev_step1_start 在 Buffer sync 之後: S1_ms = 純 Interior kernel 時間
    if (g_timing_sample) cudaEventRecord(g_timing.ev_step1_start, stream0);
#endif

    // ═══════════════════════════════════════════════════════════════
    // [P0 v3] Phase 2: Interior kernel (stream0) + MPI (stream1/host)
    //   兩者真正並行，互不干擾:
    //     stream0: 3 launches — j=3 (1行) + j=7..NYD6-8 (9行) + j=NYD6-4 (1行)
    //              共 11 行 (vs v2 的 9 行)，同一 stream 依序排隊、背靠背執行
    //              額外 2 kernel launch overhead ≈ 10-20 μs，遠小於 Buffer 省下的時間
    //     stream1/host: Pack → MPI_Startall → MPI_Waitall → Unpack
    //              Pack/Unpack kernel 極輕量 (~0.005 ms)，不影響 Interior
    //              MPI 阻塞在 host 端，不佔 GPU 資源
    //
    //   j=3, j=NYD6-4 從 Buffer 移入 Interior：
    //     MPI 不需要這 2 行 → Buffer 不必等它們 → Buffer ↓25%
    //     這 2 行與 MPI 並行執行 → 幾乎免費
    //     不與 Buffer 區域重疊 (左:j=4..6, 右:j=NYD6-7..NYD6-5)
    // ═══════════════════════════════════════════════════════════════

    // [P0 v3] Launch 1: j=3 (從 Buffer 移出的左邊界行)
#if USE_GILBM_ALGORITHM2
    Algorithm2_FusedKernel_GTS_Buffer<<<griddimRow1, blockdimInt, 0, stream0>>>(
        f_post_read, f_post_write,
        zeta_z_d, zeta_y_d,
        xi_y_d, xi_z_d, bk_precomp_d,
        z_zeta_d,
        u, v, w, rho_d,
        rho_modify_d, Force_d,
        gilbm2_coords_d,
        3);
#else
    Algorithm1_FusedKernel_GTS_Buffer<<<griddimRow1, blockdimInt, 0, stream0>>>(
        f_post_read, f_post_write,
        zeta_z_d, zeta_y_d,
        xi_y_d, xi_z_d, bk_precomp_d,
        z_zeta_d,
        u, v, w, rho_d,
        rho_modify_d, Force_d,
#if USE_ITBLBM_STREAMING
        itb_yz_coeff_d,
#endif
        3);
#endif

    // [P0 v3] Launch 2: j=7..NYD6-8 (主 Interior)
#if USE_SMEM_INTERIOR
    //   P100 路徑: Shared Memory Cooperative η-Row Loading
    //     smem_eta[7][NT+6] 消除 3D 方向 85% DRAM reads
    //     P100 L1 僅 24KB → 49 η-rows (46.6KB) 會 thrash → smem 有效
    //     blockDim.y=1 (每 block 1 row j), 所有 thread 參與 syncthreads
    Algorithm1_FusedKernel_GTS_Interior_SMEM<<<griddimInt, blockdimInt, 0, stream0>>>(
        f_post_read, f_post_write,
        zeta_z_d, zeta_y_d,
        xi_y_d, xi_z_d, bk_precomp_d,
        z_zeta_d,
        u, v, w, rho_d,
        rho_modify_d, Force_d,
        7);
#else
    //   V100 高速路徑 (預設): non-smem, 無 __syncthreads 開銷
    //     V100 128KB L1 已在硬體層級處理 η-row overlap
    //     實測: non-smem 10.3 ms vs smem 17.3 ms → non-smem 快 67%
#if USE_GILBM_ALGORITHM2
    Algorithm2_FusedKernel_GTS_Buffer<<<griddimInt, blockdimInt, 0, stream0>>>(
        f_post_read, f_post_write,
        zeta_z_d, zeta_y_d,
        xi_y_d, xi_z_d, bk_precomp_d,
        z_zeta_d,
        u, v, w, rho_d,
        rho_modify_d, Force_d,
        gilbm2_coords_d,
        7);
#else
    Algorithm1_FusedKernel_GTS_Buffer<<<griddimInt, blockdimInt, 0, stream0>>>(
        f_post_read, f_post_write,
        zeta_z_d, zeta_y_d,
        xi_y_d, xi_z_d, bk_precomp_d,
        z_zeta_d,
        u, v, w, rho_d,
        rho_modify_d, Force_d,
#if USE_ITBLBM_STREAMING
        itb_yz_coeff_d,
#endif
        7);
#endif
#endif

    // [P0 v3] Launch 3: j=NYD6-4 (從 Buffer 移出的右邊界行)
#if USE_GILBM_ALGORITHM2
    Algorithm2_FusedKernel_GTS_Buffer<<<griddimRow1, blockdimInt, 0, stream0>>>(
        f_post_read, f_post_write,
        zeta_z_d, zeta_y_d,
        xi_y_d, xi_z_d, bk_precomp_d,
        z_zeta_d,
        u, v, w, rho_d,
        rho_modify_d, Force_d,
        gilbm2_coords_d,
        NYD6 - 4);
#else
    Algorithm1_FusedKernel_GTS_Buffer<<<griddimRow1, blockdimInt, 0, stream0>>>(
        f_post_read, f_post_write,
        zeta_z_d, zeta_y_d,
        xi_y_d, xi_z_d, bk_precomp_d,
        z_zeta_d,
        u, v, w, rho_d,
        rho_modify_d, Force_d,
#if USE_ITBLBM_STREAMING
        itb_yz_coeff_d,
#endif
        NYD6 - 4);
#endif

#if USE_TIMING && TIMING_DETAIL
    if (g_timing_sample) cudaEventRecord(g_timing.ev_step1_stop, stream0);
#endif

#if USE_TIMING && TIMING_DETAIL
    // ev_mpi_start: Buffer 已完成, 記錄 MPI phase 起點
    if (g_timing_sample) cudaEventRecord(g_timing.ev_mpi_start, stream1);
#endif

    // ── MPI_Wtime: 精確量測 Pack+MPI+Unpack 的 host 阻塞時間 ──
    // Interior kernel 在 stream0 上同時跑 → overlap 成功時 MPI 免費
#if USE_TIMING && TIMING_DETAIL
    double t_mpi_wtime_start = 0.0;
    if (g_timing_sample) t_mpi_wtime_start = MPI_Wtime();
#endif

    MPI_Exchange_FPost_Packed(
        f_post_write,
        mpi_send_buf_left_d,  mpi_send_buf_right_d,
        mpi_recv_buf_left_d,  mpi_recv_buf_right_d,
        req_persist, stream1);

#if USE_TIMING && TIMING_DETAIL
    if (g_timing_sample)
        g_timing.last_mpi_wtime_ms = (float)((MPI_Wtime() - t_mpi_wtime_start) * 1000.0);
#endif

    // ═══════════════════════════════════════════════════════════════
    // [P0 v3] Phase 3: 同步雙流 → periodicSW
    //   periodicSW 需要:
    //     1. Interior 3 launches (stream0) 完成 → j=3 + j=7..15 + j=NYD6-4 就緒
    //     2. Unpack (stream1) 完成 → ξ-ghost zones 正確填入
    //   兩者都完成後才執行 periodicSW (η-periodic BC)
    //   j=3 和 j=NYD6-4 在 stream0 已算完 → periodicSW 可安全讀取
    // ═══════════════════════════════════════════════════════════════

    CHECK_CUDA( cudaStreamSynchronize(stream0) );
    CHECK_CUDA( cudaStreamSynchronize(stream1) );

    // ───── periodicSW_fpost: x-direction periodic BC on f_post_write ─────
#if USE_TIMING && TIMING_DETAIL
    if (g_timing_sample) cudaEventRecord(g_timing.ev_psw_start, stream0);
#endif
    periodicSW_fpost<<<griddimSW, blockdimSW, 0, stream0>>>(f_post_write, GRID_SIZE);
#if USE_TIMING && TIMING_DETAIL
    if (g_timing_sample) cudaEventRecord(g_timing.ev_psw_stop, stream0);
#endif

#if USE_TIMING && TIMING_DETAIL
    if (g_timing_sample) cudaEventRecord(g_timing.ev_mpi_stop, stream0);
    if (g_timing_sample) cudaEventRecord(g_timing.ev_iter_stop, stream0);
#endif

}

void Launch_ModifyForcingTerm()
{
    // ====== Instantaneous Ub: zero → accumulate once → read ======
    const size_t nBytes = NX6 * NZ6 * sizeof(double);
    CHECK_CUDA( cudaMemset(Ub_avg_d, 0, nBytes) );   // always clean before single-shot

    dim3 griddim_Ubulk(NX6/NT+1, 1, NZ6);
    dim3 blockdim_Ubulk(NT, 1, 1);
    AccumulateUbulk<<<griddim_Ubulk, blockdim_Ubulk>>>(Ub_avg_d, v);
    CHECK_CUDA( cudaDeviceSynchronize() );

    CHECK_CUDA( cudaMemcpy(Ub_avg_h, Ub_avg_d, nBytes, cudaMemcpyDeviceToHost) );

    // ★ BUG G FIX: 使用物理 dz (z_h 差值) 而非 J_2D Jacobian
    // 與 main.cu self-test / monitor.h / VTK 輸出一致
    // J_2D 包含 y_ξ (流向格距)，會隨 k 變化，引入 ~1-5% 偏差
    double Ub_avg = 0.0;               //在這邊，因為第一排座標點為直壁，所以不需要做曲面下的變換
    double A_total = 0.0;
    for( int k = 3; k < NZ6-4; k++ ){   // k=3..NZ6-5 (cell centers between walls, top wall at NZ6-4)
    for( int i = 3; i < NX6-4; i++ ){
        double v_cell = (Ub_avg_h[k*NX6+i] + Ub_avg_h[(k+1)*NX6+i]
                       + Ub_avg_h[k*NX6+i+1] + Ub_avg_h[(k+1)*NX6+i+1]) / 4.0;
        double dx_cell = x_h[i+1] - x_h[i];
        double dz_cell = z_h[3*NZ6+k+1] - z_h[3*NZ6+k];  // ★ FIX: 物理 dz (was J_2D_h)
        Ub_avg  += v_cell * dx_cell * dz_cell;
        A_total += dx_cell * dz_cell;
    }}
    Ub_avg /= A_total;

    // ★ 只有 rank 0 的 j=3 = 山丘頂入口截面，具有物理意義
    CHECK_MPI( MPI_Bcast(&Ub_avg, 1, MPI_DOUBLE, 0, MPI_COMM_WORLD) );
    Ub_avg_global = Ub_avg;

    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );

    double Ma_now = Ub_avg / (double)cs;
    double Ma_max = ComputeMaMax();  // all ranks participate (MPI_Allreduce)

#if FORCE_CTRL_MODE == 0
    // ====================================================================
    // Mode 0: Simple Proportional Controller (C.A. Lin, original)
    // ====================================================================
    double beta = fmax(0.001, 1.0/(double)Re);
    Force_h[0] = Force_h[0] + beta * ((double)Uref - Ub_avg) * (double)Uref / (double)LZ;

    // MPI average Force across all ranks
    double force_avg = 0.0;
    CHECK_MPI( MPI_Reduce( (void*)Force_h, (void*)&force_avg, 1, MPI_DOUBLE,
                           MPI_SUM, 0, MPI_COMM_WORLD ) );
    CHECK_MPI( MPI_Barrier( MPI_COMM_WORLD ) );
    if( myid == 0 ){
        force_avg = force_avg / (double)jp;
        Force_h[0] = force_avg;
    }
    CHECK_MPI( MPI_Bcast( (void*)Force_h, 1, MPI_DOUBLE, 0, MPI_COMM_WORLD ) );
    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );

    const char *ctrl_mode = "SIMPLE-PROP";

    double Re_pct = (Ub_avg - (double)Uref) / (double)Uref * 100.0;

#elif FORCE_CTRL_MODE == 1
    // ====================================================================
    // Mode 1: Hybrid Dual-Stage Force Controller (PID + Gehrke multiplicative)
    // ====================================================================
    // Phase 1 (PID):    |Re%| > SWITCH_THRESHOLD — 冷啟動/遠離目標安全加速
    // Phase 2 (Gehrke): |Re%| ≤ SWITCH_THRESHOLD — 穩態乘法微調
    // Gehrke ref: Gehrke & Rung (2020) Int J Numer Meth Fluids, Sec 3.1
    //   原文: F *= (1 - 0.1 × Re%)  當 |Re%| > 1.5%, 每 FTT 更新 10 次
    // 連續 Mach brake 在兩模式之上統一適用
    // ====================================================================

    double error = (double)Uref - Ub_avg;  // 正 = 需加速, 負 = 需減速
    double Re_pct = (Ub_avg - (double)Uref) / (double)Uref * 100.0;
    const char *ctrl_mode;

    // ── 持久狀態 (跨 force update, 且必須跨 restart) ──
    // [RESTART-FIX] 改用 extern 全域, 由 fileIO.h 讀寫 metadata.dat
    //   g_ctrl_initialized=false → 冷啟動 or 老 checkpoint 缺欄位, 用本地初值
    //   g_ctrl_initialized=true  → 從 metadata 載入, 維持連續 PID
    extern double g_force_integral;
    extern double g_error_prev;
    extern bool   g_ctrl_initialized;
    extern bool   g_gehrke_activated;
    double &Force_integral = g_force_integral;
    double &error_prev     = g_error_prev;
    bool   &controller_initialized = g_ctrl_initialized;
    bool   &gehrke_activated       = g_gehrke_activated;
    if (!controller_initialized) {
        Force_integral = 0.0;
        error_prev = error;
        controller_initialized = true;
    }

    // ── 控制器參數 (從 variables.h #define 讀取) ──
    double Kp = (double)FORCE_KP;
    double Ki = (double)FORCE_KI;
    double Kd = (double)FORCE_KD;
    double norm = (double)Uref * (double)Uref / (double)LY;

    // Poiseuille force 估計 (Gehrke floor + Force cap 用)
    double h_eff = (double)LZ - (double)H_HILL;
    double F_Poiseuille = 8.0 * (double)niu * (double)Uref / (h_eff * h_eff);
    double F_floor = (double)FORCE_GEHRKE_FLOOR * F_Poiseuille;
    double F_cap  = (double)FORCE_CAP_MULT * F_Poiseuille;  // Force 上限

    // ── 模式選擇 ──
    bool use_gehrke = (fabs(Re_pct) <= (double)FORCE_SWITCH_THRESHOLD);

    // Phase transition logging
    if (use_gehrke && !gehrke_activated) {
        gehrke_activated = true;
        if (myid == 0)
            printf("\n=== [Step %d | FTT=%.2f] Gehrke ACTIVATED (Re%%=%.2f%%) ===\n\n",
                   step, step * dt_global / (double)flow_through_time, Re_pct);
    } else if (!use_gehrke && gehrke_activated) {
        gehrke_activated = false;
        // ★ Gehrke → PID 回切: 同步積分項 = 當前 Force, 避免跳變
        Force_integral = fmax(0.0, Force_h[0]);
        if (myid == 0)
            printf("\n=== [Step %d | FTT=%.2f] Gehrke DEACTIVATED -> PID (Re%%=%.2f%%) ===\n\n",
                   step, step * dt_global / (double)flow_through_time, Re_pct);
    }

    if (use_gehrke) {
        // ============================================================
        // Phase 2: Gehrke 乘法控制器
        // F *= (1 - GEHRKE_GAIN × Re%)
        // Re% > 0 → Ub 太高 → correction < 1 → 減力
        // Re% < 0 → Ub 太低 → correction > 1 → 加力
        // ============================================================
        if (fabs(Re_pct) < (double)FORCE_GEHRKE_DEADZONE) {
            ctrl_mode = "GEHRKE-HOLD";
            // 死區: 不調整, 維持現有 Force
        } else {
            double correction = 1.0 - (double)FORCE_GEHRKE_GAIN * Re_pct;
            // 安全 clamp: SWITCH_THRESHOLD=5% 時理論極值 = [0.5, 1.5]
            // ★ 上界 1.5 而非 2.0: 防止 Re%=-5% 時每步 ×1.5 造成指數增長
            //   (舊 2.0 上界 + threshold 10% → correction=1.9 → 每步翻倍 → 發散!)
            if (correction < 0.5) correction = 0.5;
            if (correction > 1.5) correction = 1.5;
            Force_h[0] *= correction;
            ctrl_mode = (Re_pct > 0) ? "GEHRKE-DEC" : "GEHRKE-INC";
        }

        // Gehrke floor: 防止 Force → 0 陷阱
        if (Force_h[0] < F_floor) {
            Force_h[0] = F_floor;
            if (myid == 0)
                printf("[GEHRKE-FLOOR] Force clamped to %.1f%% Poiseuille = %.5E\n",
                       (double)FORCE_GEHRKE_FLOOR * 100.0, F_floor);
        }

        // ★ 同步 PID 積分項: 追蹤 Gehrke 的 Force 值
        // 這樣如果切回 PID, 積分項 = Gehrke 最後設定的力, 無跳變
        Force_integral = Force_h[0];
        error_prev = error;  // 同步微分項基準

    } else {
        // ============================================================
        // Phase 1: PID 控制器 (冷啟動 / 遠離目標)
        // Force = Kp*error*norm + integral + Kd*d_error*norm
        // ============================================================

        // 微分項
        double d_error = error - error_prev;
        error_prev = error;

        // 積分項累加
        Force_integral += Ki * error * norm;

        // Conditional decay: overshoot 時快速衰減
        if (error < 0.0 && Force_integral > 0.0) {
            Force_integral *= 0.5;
        }

        // Anti-windup: integral ∈ [0, 10×norm]
        double Force_max = 10.0 * norm;
        if (Force_integral > Force_max) Force_integral = Force_max;
        if (Force_integral < 0.0) Force_integral = 0.0;

        // PID 合成
        Force_h[0] = Kp * error * norm + Force_integral + Kd * d_error * norm;

        // Back-calculation anti-windup: Force < 0 → clamp + 回算 integral
        if (Force_h[0] < 0.0) {
            Force_h[0] = 0.0;
            double integral_target = fmax(0.0, -Kp * error * norm);
            if (Force_integral > integral_target)
                Force_integral = integral_target;
        }

        ctrl_mode = (fabs(Re_pct) < 1.5) ? "PID-steady" :
                    (error > 0)           ? "PID-accel"  : "PID-decel";
    }

    // ====== Force Magnitude Cap (兩模式共用) ======
    // 防止任何模式下 Force 失控 (e.g., Gehrke 指數增長, PID windup 殘留)
    if (Force_h[0] > F_cap) {
        if (myid == 0)
            printf("[FORCE-CAP] Force=%.5E > cap=%.5E (%.0f×Poiseuille), clamped!\n",
                   Force_h[0], F_cap, (double)FORCE_CAP_MULT);
        Force_h[0] = F_cap;
        Force_integral = fmin(Force_integral, F_cap);  // 同步 integral
    }

    // ====== Continuous Mach Safety Brake (兩模式共用) ======
    // 閾值自動跟隨 Uref 縮放
    double Ma_bulk_ref  = (double)Uref / (double)cs;       // 目標 bulk Ma
    double Ma_threshold = (double)MA_BRAKE_MULT_THRESHOLD * Ma_bulk_ref;  // 連續二次衰減開始
    double Ma_critical  = (double)MA_BRAKE_MULT_CRITICAL  * Ma_bulk_ref;  // 緊急歸零

    // Ma 增長率偵測
    static double Ma_max_prev = 0.0;
    double Ma_growth_rate = 0.0;
    if (Ma_max_prev > 1e-10) {
        Ma_growth_rate = (Ma_max - Ma_max_prev) / Ma_max_prev;
    }
    Ma_max_prev = Ma_max;

    double Ma_factor = 1.0;

    // 連續二次衰減
    if (Ma_max > Ma_threshold && Ma_max <= Ma_critical) {
        double excess = (Ma_max - Ma_threshold) / (Ma_critical - Ma_threshold);
        Ma_factor = (1.0 - excess) * (1.0 - excess);
        if (myid == 0)
            printf("[Ma-BRAKE] Ma_max=%.4f > %.3f, factor=%.4f\n",
                   Ma_max, Ma_threshold, Ma_factor);
    }

    // 緊急歸零 + integral reset
    if (Ma_max > Ma_critical) {
        Ma_factor = 0.0;
        Force_integral = 0.0;
        if (myid == 0)
            printf("[CRITICAL] Ma_max=%.4f > %.3f, Force=0, integral reset!\n",
                   Ma_max, Ma_critical);
    }

    // 急速增長率煞車
    if (Ma_growth_rate > (double)MA_BRAKE_GROWTH_LIMIT && Ma_max > Ma_bulk_ref * 1.5) {
        Ma_factor *= 0.3;
        Force_integral *= 0.5;
        if (myid == 0)
            printf("[RATE-BRAKE] Ma growth=%.1f%%, extra brake applied\n",
                   Ma_growth_rate * 100.0);
    }

    Force_h[0] *= Ma_factor;
    Force_integral *= Ma_factor;

#endif  // FORCE_CTRL_MODE

    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );

    double FTT    = step * dt_global / (double)flow_through_time;
    double U_star = Ub_avg / (double)Uref;
    double F_star = Force_h[0] * (double)LY / ((double)Uref * (double)Uref);
    double Re_now = Ub_avg / ((double)Uref / (double)Re);

    const char *status_tag = "";
    if (Ma_max > 0.35)       status_tag = " [WARNING: Ma_max>0.35, reduce Uref]";
    else if (U_star > 1.2)   status_tag = " [OVERSHOOT!]";
    else if (U_star > 1.05)  status_tag = " [OVERSHOOT]";

    extern double g_eps_current;
    if (myid == 0) {
        printf("[Step %d | FTT=%.2f] Ub=%.6f  U*=%.4f  Re%%=%.2f%%  Force=%.5E  F*=%.4f  Re=%.1f  Ma=%.4f  Ma_max=%.4f  Error=%.2e  [%s]%s\n",
               step, FTT, Ub_avg, U_star, Re_pct, Force_h[0], F_star, Re_now, Ma_now, Ma_max, g_eps_current, ctrl_mode, status_tag);
    }

    if (Ma_max > 0.35 && myid == 0) {
        printf("  >>> BGK stability limit: Ma < 0.3. Current Ma_max=%.4f at hill crest.\n", Ma_max);
        printf("  >>> Recommended: reduce Uref to %.4f (target Ma_max<0.25)\n", (double)Uref * 0.25 / Ma_max);
    }

    CHECK_CUDA( cudaMemcpy(Force_d, Force_h, sizeof(double), cudaMemcpyHostToDevice) );

    CHECK_CUDA( cudaDeviceSynchronize() );
    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );
}

#endif
