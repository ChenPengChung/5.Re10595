#ifndef ITBLBM_ISOPARAMETRIC_COEFF_H
#define ITBLBM_ISOPARAMETRIC_COEFF_H

#define ITB_YZ_CLASS_COUNT 9
#define ITB_YZ_ORDER       3
#define ITB_X_ORDER        7

#define ITB_COEFF_BOTTOM_FOLDED   0x01u
#define ITB_COEFF_TOP_FOLDED      0x02u
#define ITB_COEFF_NEWTON_FAILED   0x04u
#define ITB_COEFF_OUTSIDE_WARN    0x08u

struct ITB_YZCoeff {
    int j0;
    int k_idx[ITB_YZ_ORDER];
    double wr[ITB_YZ_ORDER];
    double ws[ITB_YZ_ORDER];
    unsigned char flags;
};

struct ITB_PrecomputeStats {
    long long newton_total;
    long long newton_converged;
    long long newton_failed;
    long long newton_iter_sum;
    long long count_abs_r_gt_1;
    long long count_abs_s_gt_1;
    long long count_abs_r_or_s_gt_1p05;
    long long count_negative_weight_raw;
    long long count_large_weight_abs_gt_2;
    int newton_max_iter_used;
    double max_residual;
    double max_update;
    double min_abs_detJ;
    double max_abs_sumw_minus_1_raw;
    double max_abs_sumw_minus_1_folded;
    double max_abs_weight_raw;
    double max_abs_weight_folded;
    double mirror_max_abs_raw;
    double mirror_rms_raw;
    double mirror_sumsq_raw;
    double mirror_max_abs_folded;
    double mirror_rms_folded;
    double mirror_sumsq_folded;
    long long mirror_compare_count;
    long long mirror_count_raw_gt_1e12;
    long long mirror_count_raw_gt_1e10;
    long long mirror_count_raw_gt_1e8;
};

static inline void ITB_ResetStats(ITB_PrecomputeStats *s)
{
    memset(s, 0, sizeof(*s));
    s->min_abs_detJ = 1.0e300;
}

static inline int ITB_YZClassFromQHost(int q)
{
    static const int yz_id[19] = {
        0, 0, 0,
        1, 2, 3, 4,
        1, 1, 2, 2,
        3, 3, 4, 4,
        5, 6, 7, 8
    };
    return yz_id[q];
}

static inline void ITB_YZClassVelocityHost(int yz_id, double *ey, double *ez)
{
    static const double cls[ITB_YZ_CLASS_COUNT][2] = {
        { 0.0,  0.0},
        { 1.0,  0.0},
        {-1.0,  0.0},
        { 0.0,  1.0},
        { 0.0, -1.0},
        { 1.0,  1.0},
        {-1.0,  1.0},
        { 1.0, -1.0},
        {-1.0, -1.0}
    };
    *ey = cls[yz_id][0];
    *ez = cls[yz_id][1];
}

__device__ __forceinline__ int ITB_YZClassFromQDevice(int q)
{
    switch (q) {
        case 3: case 7: case 8:   return 1;
        case 4: case 9: case 10:  return 2;
        case 5: case 11: case 12: return 3;
        case 6: case 13: case 14: return 4;
        case 15:                  return 5;
        case 16:                  return 6;
        case 17:                  return 7;
        case 18:                  return 8;
        default:                  return 0;
    }
}

#endif
