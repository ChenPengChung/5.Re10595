#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#define NX 449
#define NY 897
#define NZ 449
#define jp 128
#define NX6 (NX + 6)
#define NY6 (NY + 6)
#define NYD6 ((NY - 1) / jp + 7)
#define NZ6 (NZ + 6)
#define LX 4.5
#define LY 9.0
#define CFL 0.5
#define GHOST_EXTRAP_ORDER 2
#define ITBLBM_STRICT_PRECOMPUTE 1

#define __device__
#define __forceinline__ inline

#define MPI_COMM_WORLD 0
#define MPI_LONG_LONG 1
#define MPI_DOUBLE 2
#define MPI_SUM 0
#define MPI_MAX 1
#define MPI_MIN 2

static inline int fake_mpi_allreduce(const void *sendbuf, void *recvbuf,
                                     int count, int datatype, int, int)
{
    const size_t elem_size =
        (datatype == MPI_LONG_LONG) ? sizeof(long long) : sizeof(double);
    std::memcpy(recvbuf, sendbuf, (size_t)count * elem_size);
    return 0;
}

static inline int fake_mpi_barrier(int) { return 0; }

#define MPI_Allreduce fake_mpi_allreduce
#define MPI_Barrier fake_mpi_barrier
#define MPI_Abort(comm, code) \
    do { std::fprintf(stderr, "fake MPI_Abort code=%d\n", (code)); std::exit(code); } while (0)
#define CHECK_MPI(expr) \
    do { int err__ = (expr); if (err__ != 0) std::exit(err__); } while (0)

static inline void lagrange_7point_coeffs_host(double x, double w[7])
{
    for (int a = 0; a < 7; a++) {
        const double xa = (double)a;
        double wa = 1.0;
        for (int b = 0; b < 7; b++) {
            if (b == a) continue;
            const double xb = (double)b;
            wa *= (x - xb) / (xa - xb);
        }
        w[a] = wa;
    }
}

#include "../../gilbm/metric_terms.h"
#include "../isoparametric_precompute.h"

static void fail(const char *msg)
{
    std::fprintf(stderr, "%s\n", msg);
    std::exit(1);
}

static void load_global_grid(std::vector<double> *y_global,
                             std::vector<double> *z_global)
{
    const char *path = "J_Frohlich/adaptive_3.fine grid_I897_J449_s0.800000.dat";
    FILE *fp = std::fopen(path, "r");
    if (!fp) fail("cannot open real grid file");

    char line[1024];
    int header_done = 0;
    while (std::fgets(line, sizeof(line), fp)) {
        if (std::strstr(line, "DT=")) {
            header_done = 1;
            break;
        }
    }
    if (!header_done) fail("cannot parse real grid header");

    std::vector<double> x_fro((size_t)NY * NZ);
    std::vector<double> y_fro((size_t)NY * NZ);
    for (int kk = 0; kk < NZ; kk++) {
        for (int jj = 0; jj < NY; jj++) {
            const size_t idx = (size_t)kk * NY + jj;
            if (std::fscanf(fp, "%lf %lf", &x_fro[idx], &y_fro[idx]) != 2) {
                fail("unexpected EOF in real grid file");
            }
        }
    }
    std::fclose(fp);

    const double h_physical = x_fro[NY - 1] / (double)LY;
    const double grid_scale = 1.0 / h_physical;
    for (size_t idx = 0; idx < x_fro.size(); idx++) {
        x_fro[idx] *= grid_scale;
        y_fro[idx] *= grid_scale;
    }

    y_global->assign((size_t)NY6 * NZ6, 0.0);
    z_global->assign((size_t)NY6 * NZ6, 0.0);
    for (int jj = 0; jj < NY; jj++) {
        for (int kk = 0; kk < NZ; kk++) {
            const int j_code = jj + 3;
            const int k_code = kk + 3;
            const size_t idx_fro = (size_t)kk * NY + jj;
            const size_t idx_code = (size_t)j_code * NZ6 + k_code;
            (*y_global)[idx_code] = x_fro[idx_fro];
            (*z_global)[idx_code] = y_fro[idx_fro];
        }
    }

    for (int j = 3; j < 3 + NY; j++) {
        (*y_global)[(size_t)j * NZ6 + 2] = 2.0 * (*y_global)[(size_t)j * NZ6 + 3]
                                         -       (*y_global)[(size_t)j * NZ6 + 4];
        (*z_global)[(size_t)j * NZ6 + 2] = 2.0 * (*z_global)[(size_t)j * NZ6 + 3]
                                         -       (*z_global)[(size_t)j * NZ6 + 4];
        (*y_global)[(size_t)j * NZ6 + 1] = 2.0 * (*y_global)[(size_t)j * NZ6 + 2]
                                         -       (*y_global)[(size_t)j * NZ6 + 3];
        (*y_global)[(size_t)j * NZ6 + 0] = 2.0 * (*y_global)[(size_t)j * NZ6 + 1]
                                         -       (*y_global)[(size_t)j * NZ6 + 2];
        (*z_global)[(size_t)j * NZ6 + 1] = 2.0 * (*z_global)[(size_t)j * NZ6 + 2]
                                         -       (*z_global)[(size_t)j * NZ6 + 3];
        (*z_global)[(size_t)j * NZ6 + 0] = 2.0 * (*z_global)[(size_t)j * NZ6 + 1]
                                         -       (*z_global)[(size_t)j * NZ6 + 2];

        (*y_global)[(size_t)j * NZ6 + NZ6 - 3] = 2.0 * (*y_global)[(size_t)j * NZ6 + NZ6 - 4]
                                               -       (*y_global)[(size_t)j * NZ6 + NZ6 - 5];
        (*z_global)[(size_t)j * NZ6 + NZ6 - 3] = 2.0 * (*z_global)[(size_t)j * NZ6 + NZ6 - 4]
                                               -       (*z_global)[(size_t)j * NZ6 + NZ6 - 5];
        (*y_global)[(size_t)j * NZ6 + NZ6 - 2] = 2.0 * (*y_global)[(size_t)j * NZ6 + NZ6 - 3]
                                               -       (*y_global)[(size_t)j * NZ6 + NZ6 - 4];
        (*y_global)[(size_t)j * NZ6 + NZ6 - 1] = 2.0 * (*y_global)[(size_t)j * NZ6 + NZ6 - 2]
                                               -       (*y_global)[(size_t)j * NZ6 + NZ6 - 3];
        (*z_global)[(size_t)j * NZ6 + NZ6 - 2] = 2.0 * (*z_global)[(size_t)j * NZ6 + NZ6 - 3]
                                               -       (*z_global)[(size_t)j * NZ6 + NZ6 - 4];
        (*z_global)[(size_t)j * NZ6 + NZ6 - 1] = 2.0 * (*z_global)[(size_t)j * NZ6 + NZ6 - 2]
                                               -       (*z_global)[(size_t)j * NZ6 + NZ6 - 3];
    }

    for (int k = 0; k < NZ6; k++) {
        (*y_global)[(size_t)2 * NZ6 + k] = (*y_global)[(size_t)(NY6 - 5) * NZ6 + k] - (double)LY;
        (*y_global)[(size_t)1 * NZ6 + k] = (*y_global)[(size_t)(NY6 - 6) * NZ6 + k] - (double)LY;
        (*y_global)[(size_t)0 * NZ6 + k] = (*y_global)[(size_t)(NY6 - 7) * NZ6 + k] - (double)LY;
        (*z_global)[(size_t)2 * NZ6 + k] = (*z_global)[(size_t)(NY6 - 5) * NZ6 + k];
        (*z_global)[(size_t)1 * NZ6 + k] = (*z_global)[(size_t)(NY6 - 6) * NZ6 + k];
        (*z_global)[(size_t)0 * NZ6 + k] = (*z_global)[(size_t)(NY6 - 7) * NZ6 + k];

        (*y_global)[(size_t)(NY6 - 3) * NZ6 + k] = (*y_global)[(size_t)4 * NZ6 + k] + (double)LY;
        (*y_global)[(size_t)(NY6 - 2) * NZ6 + k] = (*y_global)[(size_t)5 * NZ6 + k] + (double)LY;
        (*y_global)[(size_t)(NY6 - 1) * NZ6 + k] = (*y_global)[(size_t)6 * NZ6 + k] + (double)LY;
        (*z_global)[(size_t)(NY6 - 3) * NZ6 + k] = (*z_global)[(size_t)4 * NZ6 + k];
        (*z_global)[(size_t)(NY6 - 2) * NZ6 + k] = (*z_global)[(size_t)5 * NZ6 + k];
        (*z_global)[(size_t)(NY6 - 1) * NZ6 + k] = (*z_global)[(size_t)6 * NZ6 + k];
    }
}

static void extract_rank_grid(const std::vector<double> &y_global,
                              const std::vector<double> &z_global,
                              int rank,
                              std::vector<double> *y_local,
                              std::vector<double> *z_local)
{
    y_local->assign((size_t)NYD6 * NZ6, 0.0);
    z_local->assign((size_t)NYD6 * NZ6, 0.0);
    const int stride = NYD6 - 7;
    for (int j = 0; j < NYD6; j++) {
        const int j_global = rank * stride + j;
        for (int k = 0; k < NZ6; k++) {
            (*y_local)[(size_t)j * NZ6 + k] = y_global[(size_t)j_global * NZ6 + k];
            (*z_local)[(size_t)j * NZ6 + k] = z_global[(size_t)j_global * NZ6 + k];
        }
    }
}

static double compute_rank_dt(const std::vector<double> &y,
                              const std::vector<double> &z)
{
    const size_t n = (size_t)NYD6 * NZ6;
    std::vector<double> y_xi(n), y_zeta(n), z_xi(n), z_zeta(n);
    std::vector<double> J(n), xi_y(n), xi_z(n), zeta_y(n), zeta_z(n);
    ComputeMetricTerms_Full(y_xi.data(), y_zeta.data(), z_xi.data(), z_zeta.data(),
                            J.data(), xi_y.data(), xi_z.data(), zeta_y.data(), zeta_z.data(),
                            y.data(), z.data(), NYD6, NZ6);

    const double e[19][3] = {
        {0,0,0},
        {1,0,0},{-1,0,0},{0,1,0},{0,-1,0},{0,0,1},{0,0,-1},
        {1,1,0},{-1,1,0},{1,-1,0},{-1,-1,0},
        {1,0,1},{-1,0,1},{1,0,-1},{-1,0,-1},
        {0,1,1},{0,-1,1},{0,1,-1},{0,-1,-1}
    };
    const double dx = (double)LX / (double)(NX6 - 7);
    double max_c = 1.0 / dx;
    for (int j = 3; j < NYD6 - 3; j++) {
        for (int k = 3; k <= NZ6 - 4; k++) {
            const size_t idx = (size_t)j * NZ6 + k;
            for (int q = 1; q < 19; q++) {
                if (e[q][1] == 0.0 && e[q][2] == 0.0) continue;
                const double c_xi = std::fabs(e[q][1] * xi_y[idx] + e[q][2] * xi_z[idx]);
                const double c_zeta = std::fabs(e[q][1] * zeta_y[idx] + e[q][2] * zeta_z[idx]);
                if (c_xi > max_c) max_c = c_xi;
                if (c_zeta > max_c) max_c = c_zeta;
            }
        }
    }
    return (double)CFL / max_c;
}

static void check_seam_pollution_magnitude(const std::vector<double> &y_global,
                                           const std::vector<double> &z_global)
{
    std::vector<double> y0, z0, ylast, zlast;
    extract_rank_grid(y_global, z_global, 0, &y0, &z0);
    extract_rank_grid(y_global, z_global, jp - 1, &ylast, &zlast);

    double max_rank0_left = 0.0;
    double max_last_right = 0.0;
    for (int j = 0; j < 3; j++) {
        for (int k = 0; k < NZ6; k++) {
            const double polluted = y0[(size_t)j * NZ6 + k] + (double)LY;
            const double err = std::fabs(polluted - y0[(size_t)j * NZ6 + k]);
            if (err > max_rank0_left) max_rank0_left = err;
        }
    }
    for (int j = NYD6 - 3; j < NYD6; j++) {
        for (int k = 0; k < NZ6; k++) {
            const double polluted = ylast[(size_t)j * NZ6 + k] - (double)LY;
            const double err = std::fabs(polluted - ylast[(size_t)j * NZ6 + k]);
            if (err > max_last_right) max_last_right = err;
        }
    }

    if (max_rank0_left < 0.99 * (double)LY || max_last_right < 0.99 * (double)LY) {
        fail("seam pollution counterexample did not detect LY jump");
    }
    std::printf("[ITB-REAL-GRID] seam_pollution_jump rank0_left=%.6e last_right=%.6e\n",
                max_rank0_left, max_last_right);
}

int main()
{
    std::vector<double> y_global, z_global;
    load_global_grid(&y_global, &z_global);
    check_seam_pollution_magnitude(y_global, z_global);

    std::vector<std::vector<double> > y_rank(jp);
    std::vector<std::vector<double> > z_rank(jp);
    double dt_global = 1.0e300;
    for (int rank = 0; rank < jp; rank++) {
        extract_rank_grid(y_global, z_global, rank, &y_rank[rank], &z_rank[rank]);
        const double dt_rank = compute_rank_dt(y_rank[rank], z_rank[rank]);
        if (dt_rank < dt_global) dt_global = dt_rank;
    }
    std::printf("[ITB-REAL-GRID] dt_global=%.12e\n", dt_global);

    const size_t coeff_count = (size_t)ITB_YZ_CLASS_COUNT * NYD6 * NZ6;
    std::vector<ITB_YZCoeff> coeff(coeff_count);
    long long total = 0;
    long long nonphys_kidx = 0;
    long long nonfinite_coord = 0;
    double max_sum_err = 0.0;
    double max_reconstruct_res = 0.0;
    double max_abs_weight = 0.0;

    for (int rank = 0; rank < jp; rank++) {
        ITB_PrecomputeCoefficientsHost(coeff.data(),
                                       y_rank[rank].data(),
                                       z_rank[rank].data(),
                                       dt_global, rank);
        for (int yz_id = 1; yz_id < ITB_YZ_CLASS_COUNT; yz_id++) {
            double ey, ez;
            ITB_YZClassVelocityHost(yz_id, &ey, &ez);
            for (int j = 3; j < NYD6 - 3; j++) {
                for (int k = 3; k < NZ6 - 3; k++) {
                    total++;
                    const ITB_YZCoeff &c = coeff[ITB_CoeffIndex(yz_id, j, k)];
                    if (!std::isfinite(c.r) || !std::isfinite(c.s)) {
                        nonfinite_coord++;
                        continue;
                    }
                    double wr[ITB_YZ_ORDER], dwr[ITB_YZ_ORDER];
                    double raw_ws[ITB_YZ_ORDER], dws[ITB_YZ_ORDER];
                    double folded_ws[ITB_YZ_ORDER];
                    int k_idx[ITB_YZ_ORDER];
                    unsigned char flags = 0;
                    ITB_YZShapeHost(c.r, wr, dwr);
                    ITB_YZShapeHost(c.s, raw_ws, dws);
                    ITB_FoldKWeightsHost(k, raw_ws, k_idx, folded_ws, &flags);

                    double sumw = 0.0;
                    double Y = 0.0;
                    double Z = 0.0;
                    for (int a = 0; a < ITB_YZ_ORDER; a++) {
                        const int gj = j - ITB_YZ_ORDER / 2 + a;
                        for (int b = 0; b < ITB_YZ_ORDER; b++) {
                            if (k_idx[b] < 3 || k_idx[b] > NZ6 - 4) nonphys_kidx++;
                            const double w = wr[a] * folded_ws[b];
                            const size_t idx = (size_t)gj * NZ6 + k_idx[b];
                            sumw += w;
                            Y += w * y_rank[rank][idx];
                            Z += w * z_rank[rank][idx];
                            const double aw = std::fabs(w);
                            if (aw > max_abs_weight) max_abs_weight = aw;
                        }
                    }
                    const double sum_err = std::fabs(sumw - 1.0);
                    if (sum_err > max_sum_err) max_sum_err = sum_err;

                    const size_t center = (size_t)j * NZ6 + k;
                    const double yd = y_rank[rank][center] - ey * dt_global;
                    const double zd = z_rank[rank][center] - ez * dt_global;
                    const double res = std::fabs(Y - yd) + std::fabs(Z - zd);
                    if (res > max_reconstruct_res) max_reconstruct_res = res;
                }
            }
        }
    }

    std::printf("[ITB-REAL-GRID] total=%lld nonfinite=%lld nonphys_kidx=%lld\n",
                total, nonfinite_coord, nonphys_kidx);
    std::printf("[ITB-REAL-GRID] max_sum_err=%.3e max_reconstruct_res=%.3e max_abs_weight=%.3e\n",
                max_sum_err, max_reconstruct_res, max_abs_weight);

    if (total != 3678208LL) fail("unexpected coefficient validation count");
    if (nonfinite_coord != 0) fail("nonfinite compact coordinate detected");
    if (nonphys_kidx != 0) fail("nonphysical folded k index detected");
    if (max_sum_err > 1.0e-12) fail("folded weight sum error too large");
    if (max_reconstruct_res > 5.0e-10) fail("real-grid reconstruction residual too large");
    if (max_abs_weight > 3.0) fail("real-grid compact weights unexpectedly large");

    std::printf("[ITB-REAL-GRID] PASS\n");
    return 0;
}
