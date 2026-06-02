#ifndef ITBLBM_ISOPARAMETRIC_PRECOMPUTE_H
#define ITBLBM_ISOPARAMETRIC_PRECOMPUTE_H

#include "isoparametric_coeff.h"

static inline size_t ITB_CoeffIndex(int yz_id, int j, int k)
{
    return ((size_t)yz_id * (size_t)NYD6 + (size_t)j) * (size_t)NZ6 + (size_t)k;
}

static inline size_t ITB_RawWeightIndex(int yz_id, int j, int k, int a, int b)
{
    return (((size_t)ITB_CoeffIndex(yz_id, j, k)
             * (size_t)(ITB_YZ_ORDER * ITB_YZ_ORDER))
            + (size_t)(a * ITB_YZ_ORDER + b));
}

static inline double ITB_YZNodeHost(int n)
{
    return (double)(n - ITB_YZ_ORDER / 2);
}

static inline void ITB_YZShapeHost(double x,
                                   double L[ITB_YZ_ORDER],
                                   double dL[ITB_YZ_ORDER])
{
    for (int a = 0; a < ITB_YZ_ORDER; a++) {
        const double xa = ITB_YZNodeHost(a);
        double La = 1.0;
        for (int b = 0; b < ITB_YZ_ORDER; b++) {
            if (b == a) continue;
            const double xb = ITB_YZNodeHost(b);
            La *= (x - xb) / (xa - xb);
        }
        L[a] = La;

        double dLa = 0.0;
        for (int m = 0; m < ITB_YZ_ORDER; m++) {
            if (m == a) continue;
            const double xm = ITB_YZNodeHost(m);
            double term = 1.0 / (xa - xm);
            for (int b = 0; b < ITB_YZ_ORDER; b++) {
                if (b == a || b == m) continue;
                const double xb = ITB_YZNodeHost(b);
                term *= (x - xb) / (xa - xb);
            }
            dLa += term;
        }
        dL[a] = dLa;
    }
}

static inline double ITB_GeomEffHost(const double *arr, int j, int k)
{
    const int base = j * NZ6;
    if (k < 3) {
        const double d = (double)(3 - k);
#if GHOST_EXTRAP_ORDER >= 3
        const double d1 = d + 1.0, d2 = d + 2.0, d3 = d + 3.0;
        return ( d1 * d2 * d3 / 6.0) * arr[base + 3]
             + (-d  * d2 * d3 / 2.0) * arr[base + 4]
             + ( d  * d1 * d3 / 2.0) * arr[base + 5]
             + (-d  * d1 * d2 / 6.0) * arr[base + 6];
#else
        return ((d + 1.0) * (d + 2.0) * 0.5) * arr[base + 3]
             + (-d * (d + 2.0))               * arr[base + 4]
             + (d * (d + 1.0) * 0.5)          * arr[base + 5];
#endif
    }
    if (k > NZ6 - 4) {
        const double d = (double)(k - (NZ6 - 4));
#if GHOST_EXTRAP_ORDER >= 3
        const double d1 = d + 1.0, d2 = d + 2.0, d3 = d + 3.0;
        return ( d1 * d2 * d3 / 6.0) * arr[base + NZ6 - 4]
             + (-d  * d2 * d3 / 2.0) * arr[base + NZ6 - 5]
             + ( d  * d1 * d3 / 2.0) * arr[base + NZ6 - 6]
             + (-d  * d1 * d2 / 6.0) * arr[base + NZ6 - 7];
#else
        return ((d + 1.0) * (d + 2.0) * 0.5) * arr[base + NZ6 - 4]
             + (-d * (d + 2.0))               * arr[base + NZ6 - 5]
             + (d * (d + 1.0) * 0.5)          * arr[base + NZ6 - 6];
#endif
    }
    return arr[base + k];
}

static inline void ITB_EvaluateMapHost(
    const double *y_h, const double *z_h,
    int j0, int k0, double r, double s,
    double *Y, double *Z,
    double *Yr, double *Ys, double *Zr, double *Zs)
{
    double Lr[ITB_YZ_ORDER], dLr[ITB_YZ_ORDER];
    double Ls[ITB_YZ_ORDER], dLs[ITB_YZ_ORDER];
    ITB_YZShapeHost(r, Lr, dLr);
    ITB_YZShapeHost(s, Ls, dLs);

    *Y = 0.0; *Z = 0.0;
    *Yr = 0.0; *Ys = 0.0; *Zr = 0.0; *Zs = 0.0;
    for (int a = 0; a < ITB_YZ_ORDER; a++) {
        const int gj = j0 + a;
        for (int b = 0; b < ITB_YZ_ORDER; b++) {
            const int gk = k0 + b;
            const double yv = ITB_GeomEffHost(y_h, gj, gk);
            const double zv = ITB_GeomEffHost(z_h, gj, gk);
            const double N  = Lr[a]  * Ls[b];
            const double Nr = dLr[a] * Ls[b];
            const double Ns = Lr[a]  * dLs[b];
            *Y  += N  * yv;
            *Z  += N  * zv;
            *Yr += Nr * yv;
            *Ys += Ns * yv;
            *Zr += Nr * zv;
            *Zs += Ns * zv;
        }
    }
}

static inline int ITB_NewtonSolveHost(
    const double *y_h, const double *z_h,
    int j0, int k0, double yd, double zd,
    double *r_out, double *s_out,
    int *iters_out, double *res_out, double *update_out, double *min_det_out)
{
    double r = 0.0, s = 0.0;
    double min_det = 1.0e300;
    double last_update = 0.0;
    double res_norm = 1.0e300;
    int converged = 0;
    int it = 0;

    for (it = 1; it <= 12; it++) {
        double Y, Z, Yr, Ys, Zr, Zs;
        ITB_EvaluateMapHost(y_h, z_h, j0, k0, r, s, &Y, &Z, &Yr, &Ys, &Zr, &Zs);
        const double Ry = Y - yd;
        const double Rz = Z - zd;
        const double det = Yr * Zs - Ys * Zr;
        const double abs_det = fabs(det);
        if (abs_det < min_det) min_det = abs_det;
        res_norm = fabs(Ry) + fabs(Rz);
        if (abs_det < 1.0e-14) break;

        double dr = ( Zs * Ry - Ys * Rz) / det;
        double ds = (-Zr * Ry + Yr * Rz) / det;
        double scale = 1.0;
        last_update = fabs(dr) + fabs(ds);
        if (last_update > 1.0) scale = 0.5;

        double best_r = r - scale * dr;
        double best_s = s - scale * ds;
        double best_res = 1.0e300;
        for (int damp = 0; damp < 5; damp++) {
            double Yc, Zc, Yrc, Ysc, Zrc, Zsc;
            ITB_EvaluateMapHost(y_h, z_h, j0, k0, best_r, best_s,
                                &Yc, &Zc, &Yrc, &Ysc, &Zrc, &Zsc);
            best_res = fabs(Yc - yd) + fabs(Zc - zd);
            if (best_res <= res_norm || scale <= 0.0625) break;
            scale *= 0.5;
            best_r = r - scale * dr;
            best_s = s - scale * ds;
        }

        r = best_r;
        s = best_s;
        last_update *= scale;
        res_norm = best_res;
        if (last_update < 1.0e-12 || res_norm < 1.0e-11) {
            converged = 1;
            break;
        }
    }

    *r_out = r;
    *s_out = s;
    *iters_out = it;
    *res_out = res_norm;
    *update_out = last_update;
    *min_det_out = min_det;
    return converged;
}

static inline void ITB_FillCenterCoeff(ITB_YZCoeff *c, int j, int k)
{
    (void)j;
    (void)k;
    c->r = 0.0;
    c->s = 0.0;
}

static inline void ITB_FoldKWeightsHost(
    int k, const double raw_ws[ITB_YZ_ORDER],
    int k_idx[ITB_YZ_ORDER],
    double folded_ws[ITB_YZ_ORDER],
    unsigned char *flags)
{
    const int half = ITB_YZ_ORDER / 2;
    const int raw_k0 = k - half;
    int phys_k0 = raw_k0;
    if (phys_k0 < 3) phys_k0 = 3;
    if (phys_k0 > NZ6 - 3 - ITB_YZ_ORDER)
        phys_k0 = NZ6 - 3 - ITB_YZ_ORDER;

    *flags = 0;
    for (int b = 0; b < ITB_YZ_ORDER; b++) {
        k_idx[b] = phys_k0 + b;
        folded_ws[b] = 0.0;
    }

    for (int b = 0; b < ITB_YZ_ORDER; b++) {
        const int kg = raw_k0 + b;
        const double w = raw_ws[b];
        if (kg < 3) {
            const double d = (double)(3 - kg);
#if GHOST_EXTRAP_ORDER >= 3
            const double d1 = d + 1.0, d2 = d + 2.0, d3 = d + 3.0;
            const double c0 =  d1 * d2 * d3 / 6.0;
            const double c1 = -d  * d2 * d3 / 2.0;
            const double c2 =  d  * d1 * d3 / 2.0;
            const double c3 = -d  * d1 * d2 / 6.0;
            folded_ws[3 - phys_k0] += w * c0;
            folded_ws[4 - phys_k0] += w * c1;
            folded_ws[5 - phys_k0] += w * c2;
            folded_ws[6 - phys_k0] += w * c3;
#else
            const double c0 = (d + 1.0) * (d + 2.0) * 0.5;
            const double c1 = -d * (d + 2.0);
            const double c2 = d * (d + 1.0) * 0.5;
            folded_ws[3 - phys_k0] += w * c0;
            folded_ws[4 - phys_k0] += w * c1;
            folded_ws[5 - phys_k0] += w * c2;
#endif
            *flags |= ITB_COEFF_BOTTOM_FOLDED;
        } else if (kg > NZ6 - 4) {
            const double d = (double)(kg - (NZ6 - 4));
#if GHOST_EXTRAP_ORDER >= 3
            const double d1 = d + 1.0, d2 = d + 2.0, d3 = d + 3.0;
            const double c0 =  d1 * d2 * d3 / 6.0;
            const double c1 = -d  * d2 * d3 / 2.0;
            const double c2 =  d  * d1 * d3 / 2.0;
            const double c3 = -d  * d1 * d2 / 6.0;
            folded_ws[(NZ6 - 4) - phys_k0] += w * c0;
            folded_ws[(NZ6 - 5) - phys_k0] += w * c1;
            folded_ws[(NZ6 - 6) - phys_k0] += w * c2;
            folded_ws[(NZ6 - 7) - phys_k0] += w * c3;
#else
            const double c0 = (d + 1.0) * (d + 2.0) * 0.5;
            const double c1 = -d * (d + 2.0);
            const double c2 = d * (d + 1.0) * 0.5;
            folded_ws[(NZ6 - 4) - phys_k0] += w * c0;
            folded_ws[(NZ6 - 5) - phys_k0] += w * c1;
            folded_ws[(NZ6 - 6) - phys_k0] += w * c2;
#endif
            *flags |= ITB_COEFF_TOP_FOLDED;
        } else {
            folded_ws[kg - phys_k0] += w;
        }
    }
}

static inline void ITB_AccumulateWeightStats(
    ITB_PrecomputeStats *stats,
    const double wr[ITB_YZ_ORDER],
    const double raw_ws[ITB_YZ_ORDER],
    const double folded_ws[ITB_YZ_ORDER])
{
    double sum_raw = 0.0, sum_folded = 0.0;
    for (int a = 0; a < ITB_YZ_ORDER; a++) {
        for (int b = 0; b < ITB_YZ_ORDER; b++) {
            const double raw = wr[a] * raw_ws[b];
            const double folded = wr[a] * folded_ws[b];
            sum_raw += raw;
            sum_folded += folded;
            const double ar = fabs(raw);
            const double af = fabs(folded);
            if (ar > stats->max_abs_weight_raw) stats->max_abs_weight_raw = ar;
            if (af > stats->max_abs_weight_folded) stats->max_abs_weight_folded = af;
            if (raw < 0.0) stats->count_negative_weight_raw++;
            if (ar > 2.0 || af > 2.0) stats->count_large_weight_abs_gt_2++;
        }
    }
    const double eraw = fabs(sum_raw - 1.0);
    const double efold = fabs(sum_folded - 1.0);
    if (eraw > stats->max_abs_sumw_minus_1_raw)
        stats->max_abs_sumw_minus_1_raw = eraw;
    if (efold > stats->max_abs_sumw_minus_1_folded)
        stats->max_abs_sumw_minus_1_folded = efold;
}

static inline void ITB_ReconstructFoldedWeightsHost(
    const ITB_YZCoeff *c,
    int k,
    double wr[ITB_YZ_ORDER],
    double ws_folded[ITB_YZ_ORDER])
{
    double dwr[ITB_YZ_ORDER];
    double ws_raw[ITB_YZ_ORDER], dws[ITB_YZ_ORDER];
    int k_idx[ITB_YZ_ORDER];
    unsigned char flags = 0;
    ITB_YZShapeHost(c->r, wr, dwr);
    ITB_YZShapeHost(c->s, ws_raw, dws);
    ITB_FoldKWeightsHost(k, ws_raw, k_idx, ws_folded, &flags);
}

static inline void ITB_ComputeMirrorDiagnostics(
    const double *raw_w,
    const ITB_YZCoeff *coeff,
    ITB_PrecomputeStats *stats)
{
    const int src_ids[4] = {1, 3, 5, 6};
    const int dst_ids[4] = {2, 4, 8, 7};

    for (int p = 0; p < 4; p++) {
        const int src = src_ids[p];
        const int dst = dst_ids[p];
        for (int j = 3; j < NYD6 - 3; j++) {
            for (int k = 3; k < NZ6 - 3; k++) {
                const ITB_YZCoeff *csrc = &coeff[ITB_CoeffIndex(src, j, k)];
                const ITB_YZCoeff *cdst = &coeff[ITB_CoeffIndex(dst, j, k)];
                double src_wr[ITB_YZ_ORDER], src_ws[ITB_YZ_ORDER];
                double dst_wr[ITB_YZ_ORDER], dst_ws[ITB_YZ_ORDER];
                ITB_ReconstructFoldedWeightsHost(csrc, k, src_wr, src_ws);
                ITB_ReconstructFoldedWeightsHost(cdst, k, dst_wr, dst_ws);
                for (int a = 0; a < ITB_YZ_ORDER; a++) {
                    for (int b = 0; b < ITB_YZ_ORDER; b++) {
                        const double ws = raw_w[ITB_RawWeightIndex(
                            src, j, k, ITB_YZ_ORDER - 1 - a, ITB_YZ_ORDER - 1 - b)];
                        const double wd = raw_w[ITB_RawWeightIndex(dst, j, k, a, b)];
                        const double err = fabs(wd - ws);
                        if (err > stats->mirror_max_abs_raw)
                            stats->mirror_max_abs_raw = err;
                        stats->mirror_sumsq_raw += err * err;
                        if (err > 1.0e-12) stats->mirror_count_raw_gt_1e12++;
                        if (err > 1.0e-10) stats->mirror_count_raw_gt_1e10++;
                        if (err > 1.0e-8)  stats->mirror_count_raw_gt_1e8++;

                        const double fs = src_wr[ITB_YZ_ORDER - 1 - a]
                                        * src_ws[ITB_YZ_ORDER - 1 - b];
                        const double fd = dst_wr[a] * dst_ws[b];
                        const double ferr = fabs(fd - fs);
                        if (ferr > stats->mirror_max_abs_folded)
                            stats->mirror_max_abs_folded = ferr;
                        stats->mirror_sumsq_folded += ferr * ferr;
                        stats->mirror_compare_count++;
                    }
                }
            }
        }
    }
    if (stats->mirror_compare_count > 0) {
        stats->mirror_rms_raw =
            sqrt(stats->mirror_sumsq_raw / (double)stats->mirror_compare_count);
        stats->mirror_rms_folded =
            sqrt(stats->mirror_sumsq_folded / (double)stats->mirror_compare_count);
    }
}

static inline void ITB_PrintPrecomputeStats(
    const ITB_PrecomputeStats *local, int myid)
{
    long long lsum[11] = {
        local->newton_total, local->newton_converged, local->newton_failed,
        local->newton_iter_sum, local->count_abs_r_gt_1, local->count_abs_s_gt_1,
        local->count_abs_r_or_s_gt_1p05, local->count_negative_weight_raw,
        local->count_large_weight_abs_gt_2, local->mirror_count_raw_gt_1e12,
        local->mirror_count_raw_gt_1e10
    };
    long long gsum[11];
    CHECK_MPI(MPI_Allreduce(lsum, gsum, 11, MPI_LONG_LONG, MPI_SUM, MPI_COMM_WORLD));

    long long lgt8 = local->mirror_count_raw_gt_1e8;
    long long ggt8 = 0;
    CHECK_MPI(MPI_Allreduce(&lgt8, &ggt8, 1, MPI_LONG_LONG, MPI_SUM, MPI_COMM_WORLD));

    double lmax[9] = {
        local->max_residual, local->max_update,
        local->max_abs_sumw_minus_1_raw, local->max_abs_sumw_minus_1_folded,
        local->max_abs_weight_raw, local->max_abs_weight_folded,
        local->mirror_max_abs_raw, local->mirror_max_abs_folded,
        (double)local->newton_max_iter_used
    };
    double gmax[9];
    CHECK_MPI(MPI_Allreduce(lmax, gmax, 9, MPI_DOUBLE, MPI_MAX, MPI_COMM_WORLD));

    double gmin_det = 0.0;
    CHECK_MPI(MPI_Allreduce((void*)&local->min_abs_detJ, &gmin_det, 1,
                            MPI_DOUBLE, MPI_MIN, MPI_COMM_WORLD));

    double lsumsq[2] = {local->mirror_sumsq_raw, local->mirror_sumsq_folded};
    double gsumsq[2];
    CHECK_MPI(MPI_Allreduce(lsumsq, gsumsq, 2, MPI_DOUBLE, MPI_SUM, MPI_COMM_WORLD));
    long long lcmp = local->mirror_compare_count;
    long long gcmp = 0;
    CHECK_MPI(MPI_Allreduce(&lcmp, &gcmp, 1, MPI_LONG_LONG, MPI_SUM, MPI_COMM_WORLD));

    if (myid == 0) {
        const double avg_iter = (gsum[0] > 0) ? (double)gsum[3] / (double)gsum[0] : 0.0;
        const double rms_raw = (gcmp > 0) ? sqrt(gsumsq[0] / (double)gcmp) : 0.0;
        const double rms_folded = (gcmp > 0) ? sqrt(gsumsq[1] / (double)gcmp) : 0.0;
        printf("[ITB] compact coordinate table:\n");
        printf("  yz classes                 = %d\n", ITB_YZ_CLASS_COUNT);
        printf("  active moving classes       = 8\n");
        printf("  coeff count per rank        = %d*NYD6*NZ6\n", ITB_YZ_CLASS_COUNT);
        printf("  interpolation order         = x7_yz7x7\n");
        printf("  ghost extrapolation order   = %d\n", GHOST_EXTRAP_ORDER);
        printf("[ITB] Newton diagnostics:\n");
        printf("  total/converged/failed      = %lld / %lld / %lld\n",
               gsum[0], gsum[1], gsum[2]);
        printf("  avg iter / max iter         = %.3f / %.0f\n", avg_iter, gmax[8]);
        printf("  max residual                = %.17e\n", gmax[0]);
        printf("  max update                  = %.17e\n", gmax[1]);
        printf("  min |detJ|                  = %.17e\n", gmin_det);
        printf("  |r|>1 / |s|>1 / >1.05       = %lld / %lld / %lld\n",
               gsum[4], gsum[5], gsum[6]);
        printf("[ITB] Weight diagnostics:\n");
        printf("  max |sum(raw)-1|            = %.17e\n", gmax[2]);
        printf("  max |sum(folded)-1|         = %.17e\n", gmax[3]);
        printf("  max |w_raw| / |w_folded|    = %.17e / %.17e\n", gmax[4], gmax[5]);
        printf("  negative raw / |w|>2 count  = %lld / %lld\n", gsum[7], gsum[8]);
        printf("[ITB] Mirror diagnostics (diagnostic only; direct Newton coefficients used):\n");
        printf("  max raw / folded err        = %.17e / %.17e\n", gmax[6], gmax[7]);
        printf("  rms raw / folded err        = %.17e / %.17e\n", rms_raw, rms_folded);
        printf("  raw err >1e-12/1e-10/1e-8   = %lld / %lld / %lld\n",
               gsum[9], gsum[10], ggt8);
        if (gmax[6] > 1.0e-12) {
            fprintf(stderr,
                "[ITB][WARN] Periodic-hill y-z grid is not mirror-symmetric enough "
                "for coefficient mirroring. Direct Newton coefficients will be used. "
                "max_raw_mirror_err=%.17e\n", gmax[6]);
        }
        if (gmax[6] > 1.0e-10) {
            fprintf(stderr,
                "[ITB][WARN] Strong mirror mismatch: max_raw_mirror_err=%.17e\n", gmax[6]);
        }
        if (gmax[6] > 1.0e-8) {
            fprintf(stderr,
                "[ITB][WARN] Mirror compression must not be used for this grid: "
                "max_raw_mirror_err=%.17e\n", gmax[6]);
        }
    }
}

static inline void ITB_PrecomputeCoefficientsHost(
    ITB_YZCoeff *coeff,
    const double *y_h,
    const double *z_h,
    double dt_val,
    int myid)
{
    ITB_PrecomputeStats stats;
    ITB_ResetStats(&stats);

    const size_t raw_count =
        (size_t)ITB_YZ_CLASS_COUNT * (size_t)NYD6 * (size_t)NZ6
        * (size_t)(ITB_YZ_ORDER * ITB_YZ_ORDER);
    double *raw_w = (double*)malloc(raw_count * sizeof(double));
    if (!raw_w) {
        fprintf(stderr, "[ITB] FATAL: cannot allocate raw weight diagnostics buffer\n");
        MPI_Abort(MPI_COMM_WORLD, 71);
    }
    memset(raw_w, 0, raw_count * sizeof(double));

    for (int yz_id = 0; yz_id < ITB_YZ_CLASS_COUNT; yz_id++) {
        double ey, ez;
        ITB_YZClassVelocityHost(yz_id, &ey, &ez);
        for (int j = 0; j < NYD6; j++) {
            for (int k = 0; k < NZ6; k++) {
                ITB_YZCoeff *c = &coeff[ITB_CoeffIndex(yz_id, j, k)];
                ITB_FillCenterCoeff(c, j, k);
                raw_w[ITB_RawWeightIndex(yz_id, j, k,
                                          ITB_YZ_ORDER / 2,
                                          ITB_YZ_ORDER / 2)] = 1.0;
            }
        }

        if (yz_id == 0) continue;

        for (int j = 3; j < NYD6 - 3; j++) {
            for (int k = 3; k < NZ6 - 3; k++) {
                const int j0 = j - ITB_YZ_ORDER / 2;
                const int k0 = k - ITB_YZ_ORDER / 2;
                const double yd = y_h[j * NZ6 + k] - ey * dt_val;
                const double zd = z_h[j * NZ6 + k] - ez * dt_val;
                double r = 0.0, s = 0.0, residual = 0.0, update = 0.0, min_det = 0.0;
                int iters = 0;
                const int ok = ITB_NewtonSolveHost(y_h, z_h, j0, k0, yd, zd,
                                                   &r, &s, &iters, &residual,
                                                   &update, &min_det);

                stats.newton_total++;
                stats.newton_iter_sum += iters;
                if (ok) stats.newton_converged++;
                else stats.newton_failed++;
                if (iters > stats.newton_max_iter_used) stats.newton_max_iter_used = iters;
                if (residual > stats.max_residual) stats.max_residual = residual;
                if (update > stats.max_update) stats.max_update = update;
                if (min_det < stats.min_abs_detJ) stats.min_abs_detJ = min_det;
                if (fabs(r) > 1.0) stats.count_abs_r_gt_1++;
                if (fabs(s) > 1.0) stats.count_abs_s_gt_1++;

                ITB_YZCoeff *c = &coeff[ITB_CoeffIndex(yz_id, j, k)];
                if (fabs(r) > 1.05 || fabs(s) > 1.05) {
                    stats.count_abs_r_or_s_gt_1p05++;
                }
                if (!ok) {
                    r = 0.0;
                    s = 0.0;
                }
                c->r = r;
                c->s = s;

                double wr[ITB_YZ_ORDER], dwr[ITB_YZ_ORDER];
                double ws_raw[ITB_YZ_ORDER], dws[ITB_YZ_ORDER];
                double ws_folded[ITB_YZ_ORDER];
                ITB_YZShapeHost(r, wr, dwr);
                ITB_YZShapeHost(s, ws_raw, dws);
                int k_idx[ITB_YZ_ORDER];
                unsigned char fold_flags = 0;
                ITB_FoldKWeightsHost(k, ws_raw, k_idx, ws_folded, &fold_flags);

                for (int a = 0; a < ITB_YZ_ORDER; a++) {
                    for (int b = 0; b < ITB_YZ_ORDER; b++) {
                        raw_w[ITB_RawWeightIndex(yz_id, j, k, a, b)] = wr[a] * ws_raw[b];
                    }
                }
                ITB_AccumulateWeightStats(&stats, wr, ws_raw, ws_folded);
            }
        }
    }

    ITB_ComputeMirrorDiagnostics(raw_w, coeff, &stats);
    ITB_PrintPrecomputeStats(&stats, myid);
    free(raw_w);

    if (stats.newton_failed > 0 && ITBLBM_STRICT_PRECOMPUTE) {
        if (myid == 0) {
            fprintf(stderr,
                "[ITB] FATAL: Newton precompute failed for %lld coefficients "
                "(strict mode enabled).\n", stats.newton_failed);
        }
        MPI_Abort(MPI_COMM_WORLD, 72);
    }
    if (stats.max_abs_sumw_minus_1_folded > 1.0e-12) {
        if (myid == 0) {
            fprintf(stderr,
                "[ITB][WARN] folded weight sum error exceeds tolerance: %.17e\n",
                stats.max_abs_sumw_minus_1_folded);
        }
    }
}

static inline void ITB_PrecomputeXWeightsHost(double wx[2][ITB_X_ORDER], double dt_val)
{
    const double dx = (double)LX / (double)(NX6 - 7);
    const double t_plus  = 3.0 - dt_val / dx;
    const double t_minus = 3.0 + dt_val / dx;
    lagrange_7point_coeffs_host(t_plus,  wx[0]);
    lagrange_7point_coeffs_host(t_minus, wx[1]);
}

#endif
