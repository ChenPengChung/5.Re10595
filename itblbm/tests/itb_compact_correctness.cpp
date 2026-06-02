#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#define NX6 12
#define NYD6 12
#define NZ6 13
#define LX 4.5
#define LY 4.5
#define GHOST_EXTRAP_ORDER 2
#define ITBLBM_STRICT_PRECOMPUTE 0

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

#define MPI_Allreduce fake_mpi_allreduce
#define MPI_Abort(comm, code) \
    do { std::fprintf(stderr, "fake MPI_Abort code=%d\n", (code)); std::exit(code); } while (0)
#define CHECK_MPI(expr) \
    do { int err__ = (expr); if (err__ != 0) std::exit(err__); } while (0)

static inline void lagrange_7point_coeffs_host(double x, double w[7])
{
    for (int a = 0; a < 7; a++) {
        const double xa = (double)(a - 3);
        double wa = 1.0;
        for (int b = 0; b < 7; b++) {
            if (b == a) continue;
            const double xb = (double)(b - 3);
            wa *= (x - xb) / (xa - xb);
        }
        w[a] = wa;
    }
}

#include "../isoparametric_precompute.h"

static void require_close(const char *name, double got, double expected, double tol)
{
    const double err = std::fabs(got - expected);
    if (err > tol) {
        std::fprintf(stderr, "%s failed: got=%.17e expected=%.17e err=%.3e tol=%.3e\n",
                     name, got, expected, err, tol);
        std::exit(1);
    }
}

static void check_shape()
{
    double max_sum_err = 0.0;
    double max_node_err = 0.0;
    for (int n = 0; n < ITB_YZ_ORDER; n++) {
        double L[ITB_YZ_ORDER], dL[ITB_YZ_ORDER];
        ITB_YZShapeHost(ITB_YZNodeHost(n), L, dL);
        for (int a = 0; a < ITB_YZ_ORDER; a++) {
            const double expected = (a == n) ? 1.0 : 0.0;
            const double err = std::fabs(L[a] - expected);
            if (err > max_node_err) max_node_err = err;
        }
    }
    for (double x = -2.7; x <= 2.7001; x += 0.3) {
        double L[ITB_YZ_ORDER], dL[ITB_YZ_ORDER];
        ITB_YZShapeHost(x, L, dL);
        double sum = 0.0;
        for (int a = 0; a < ITB_YZ_ORDER; a++) sum += L[a];
        const double err = std::fabs(sum - 1.0);
        if (err > max_sum_err) max_sum_err = err;
    }
    require_close("shape node kronecker", max_node_err, 0.0, 1.0e-14);
    require_close("shape partition", max_sum_err, 0.0, 1.0e-14);
}

static void fill_uniform(std::vector<double> *y, std::vector<double> *z)
{
    y->assign((size_t)NYD6 * NZ6, 0.0);
    z->assign((size_t)NYD6 * NZ6, 0.0);
    for (int j = 0; j < NYD6; j++) {
        for (int k = 0; k < NZ6; k++) {
            (*y)[(size_t)j * NZ6 + k] = (double)j;
            (*z)[(size_t)j * NZ6 + k] = (double)k;
        }
    }
}

static void check_newton_uniform()
{
    std::vector<double> y, z;
    fill_uniform(&y, &z);
    const int j = 5;
    const int k = 6;
    const double dt = 0.05;
    double max_r_err = 0.0;
    double max_s_err = 0.0;
    double max_res = 0.0;
    for (int yz_id = 1; yz_id < ITB_YZ_CLASS_COUNT; yz_id++) {
        double ey, ez;
        ITB_YZClassVelocityHost(yz_id, &ey, &ez);
        double r = 0.0, s = 0.0, res = 0.0, update = 0.0, min_det = 0.0;
        int iters = 0;
        const int ok = ITB_NewtonSolveHost(y.data(), z.data(), j - 3, k - 3,
                                           y[(size_t)j * NZ6 + k] - ey * dt,
                                           z[(size_t)j * NZ6 + k] - ez * dt,
                                           &r, &s, &iters, &res, &update, &min_det);
        if (!ok) {
            std::fprintf(stderr, "uniform Newton failed yz_id=%d\n", yz_id);
            std::exit(1);
        }
        const double er = std::fabs(r + ey * dt);
        const double es = std::fabs(s + ez * dt);
        if (er > max_r_err) max_r_err = er;
        if (es > max_s_err) max_s_err = es;
        if (res > max_res) max_res = res;
    }
    require_close("uniform Newton r", max_r_err, 0.0, 1.0e-12);
    require_close("uniform Newton s", max_s_err, 0.0, 1.0e-12);
    require_close("uniform Newton residual", max_res, 0.0, 1.0e-11);
}

static void check_fold_matches_geom_eff()
{
    std::vector<double> arr((size_t)NYD6 * NZ6, 0.0);
    for (int j = 0; j < NYD6; j++) {
        for (int k = 0; k < NZ6; k++) {
            arr[(size_t)j * NZ6 + k] = 1.0 + 0.2 * k + 0.03 * k * k;
        }
    }

    double raw_ws[ITB_YZ_ORDER], dws[ITB_YZ_ORDER];
    double folded_ws[ITB_YZ_ORDER];
    int k_idx[ITB_YZ_ORDER];
    unsigned char flags = 0;
    ITB_YZShapeHost(0.35, raw_ws, dws);

    for (int k : {3, 4, NZ6 - 5, NZ6 - 4}) {
        ITB_FoldKWeightsHost(k, raw_ws, k_idx, folded_ws, &flags);
        double raw_sum = 0.0;
        double folded_sum = 0.0;
        for (int b = 0; b < ITB_YZ_ORDER; b++) {
            raw_sum += raw_ws[b] * ITB_GeomEffHost(arr.data(), 5, k - 3 + b);
            folded_sum += folded_ws[b] * arr[(size_t)5 * NZ6 + k_idx[b]];
            if (k_idx[b] < 3 || k_idx[b] > NZ6 - 4) {
                std::fprintf(stderr, "nonphysical folded k index: k=%d idx=%d\n", k, k_idx[b]);
                std::exit(1);
            }
        }
        require_close("folded ghost consistency", folded_sum, raw_sum, 1.0e-12);
    }
}

static void check_seam_snapshot_counterexample()
{
    std::vector<double> y_snapshot((size_t)NYD6 * NZ6, 0.0);
    std::vector<double> y_corrupt((size_t)NYD6 * NZ6, 0.0);
    std::vector<double> z((size_t)NYD6 * NZ6, 0.0);
    const double h = 0.01;

    for (int j = 0; j < NYD6; j++) {
        for (int k = 0; k < NZ6; k++) {
            y_snapshot[(size_t)j * NZ6 + k] = (double)(j - 3) * h;
            z[(size_t)j * NZ6 + k] = (double)k;
        }
    }
    y_corrupt = y_snapshot;
    for (int j = 0; j < 3; j++) {
        for (int k = 0; k < NZ6; k++) {
            y_corrupt[(size_t)j * NZ6 + k] += (double)LY;
        }
    }

    double Ys, Zs, Yr, Yt, Zr, Zt;
    double Yc, Zc, Yr_c, Yt_c, Zr_c, Zt_c;
    ITB_EvaluateMapHost(y_snapshot.data(), z.data(), 0, 3,
                        -1.0, 0.0, &Ys, &Zs, &Yr, &Yt, &Zr, &Zt);
    ITB_EvaluateMapHost(y_corrupt.data(), z.data(), 0, 3,
                        -1.0, 0.0, &Yc, &Zc, &Yr_c, &Yt_c, &Zr_c, &Zt_c);
    require_close("seam snapshot geometry", Ys, -h, 1.0e-14);
    require_close("seam corrupt geometry", Yc, (double)LY - h, 1.0e-14);

    double r = 0.0, s = 0.0, res = 0.0, update = 0.0, min_det = 0.0;
    int iters = 0;
    const int ok = ITB_NewtonSolveHost(y_snapshot.data(), z.data(), 0, 3,
                                       -h, 6.0, &r, &s, &iters,
                                       &res, &update, &min_det);
    if (!ok) {
        std::fprintf(stderr, "seam snapshot Newton failed\n");
        std::exit(1);
    }
    require_close("seam snapshot Newton r", r, -1.0, 1.0e-12);
    require_close("seam snapshot Newton s", s, 0.0, 1.0e-12);

    const double corrupt_jump = std::fabs(Yc - Ys);
    if (corrupt_jump < 0.99 * (double)LY) {
        std::fprintf(stderr, "seam counterexample too weak: jump=%.17e LY=%.17e\n",
                     corrupt_jump, (double)LY);
        std::exit(1);
    }
}

static void check_precompute_compact_and_rescue()
{
    const size_t count = (size_t)ITB_YZ_CLASS_COUNT * NYD6 * NZ6;
    std::vector<ITB_YZCoeff> coeff(count);
    std::vector<double> y, z;
    fill_uniform(&y, &z);
    const double dt = 0.05;

    ITB_PrecomputeCoefficientsHost(coeff.data(), y.data(), z.data(), dt, 0);
    if (sizeof(ITB_YZCoeff) != 2 * sizeof(double)) {
        std::fprintf(stderr, "compact coefficient size mismatch: %zu\n", sizeof(ITB_YZCoeff));
        std::exit(1);
    }

    double max_reconstruct_err = 0.0;
    for (int yz_id = 1; yz_id < ITB_YZ_CLASS_COUNT; yz_id++) {
        double ey, ez;
        ITB_YZClassVelocityHost(yz_id, &ey, &ez);
        for (int j = 3; j < NYD6 - 3; j++) {
            for (int k = 3; k < NZ6 - 3; k++) {
                const ITB_YZCoeff &c = coeff[ITB_CoeffIndex(yz_id, j, k)];
                double wr[ITB_YZ_ORDER], dwr[ITB_YZ_ORDER];
                double raw_ws[ITB_YZ_ORDER], dws[ITB_YZ_ORDER];
                double folded_ws[ITB_YZ_ORDER];
                int k_idx[ITB_YZ_ORDER];
                unsigned char flags = 0;
                ITB_YZShapeHost(c.r, wr, dwr);
                ITB_YZShapeHost(c.s, raw_ws, dws);
                ITB_FoldKWeightsHost(k, raw_ws, k_idx, folded_ws, &flags);
                double Y = 0.0, Z = 0.0;
                for (int a = 0; a < ITB_YZ_ORDER; a++) {
                    const int gj = j - 3 + a;
                    for (int b = 0; b < ITB_YZ_ORDER; b++) {
                        const double w = wr[a] * folded_ws[b];
                        Y += w * y[(size_t)gj * NZ6 + k_idx[b]];
                        Z += w * z[(size_t)gj * NZ6 + k_idx[b]];
                    }
                }
                const double err = std::fabs(Y - (y[(size_t)j * NZ6 + k] - ey * dt))
                                 + std::fabs(Z - (z[(size_t)j * NZ6 + k] - ez * dt));
                if (err > max_reconstruct_err) max_reconstruct_err = err;
            }
        }
    }
    require_close("compact precompute reconstruction", max_reconstruct_err, 0.0, 1.0e-11);

    std::vector<double> zero_y((size_t)NYD6 * NZ6, 0.0);
    std::vector<double> zero_z((size_t)NYD6 * NZ6, 0.0);
    std::vector<ITB_YZCoeff> rescue(count);
    ITB_PrecomputeCoefficientsHost(rescue.data(), zero_y.data(), zero_z.data(), dt, 0);
    const ITB_YZCoeff &fallback = rescue[ITB_CoeffIndex(1, 3, 3)];
    require_close("rescue fallback r", fallback.r, 0.0, 0.0);
    require_close("rescue fallback s", fallback.s, 0.0, 0.0);
}

int main()
{
    check_shape();
    check_newton_uniform();
    check_fold_matches_geom_eff();
    check_seam_snapshot_counterexample();
    check_precompute_compact_and_rescue();
    std::printf("[ITB-COMPACT-CORRECTNESS] PASS struct_bytes=%zu\n", sizeof(ITB_YZCoeff));
    return 0;
}
