#ifndef ITBLBM_ISOPARAMETRIC_STREAMING_H
#define ITBLBM_ISOPARAMETRIC_STREAMING_H

#include "isoparametric_coeff.h"

__constant__ double ITB_WX[2][ITB_X_ORDER];

__device__ __forceinline__ size_t ITB_CoeffIndexDevice(int yz_id, int j, int k)
{
    return ((size_t)yz_id * (size_t)NYD6 + (size_t)j) * (size_t)NZ6 + (size_t)k;
}

__device__ __forceinline__ double ITB_YZNodeDevice(int n)
{
    return (double)(n - ITB_YZ_ORDER / 2);
}

__device__ __forceinline__ void itb_yz_shape_device(
    double x,
    double L[ITB_YZ_ORDER])
{
    for (int a = 0; a < ITB_YZ_ORDER; a++) {
        const double xa = ITB_YZNodeDevice(a);
        double La = 1.0;
        for (int b = 0; b < ITB_YZ_ORDER; b++) {
            if (b == a) continue;
            const double xb = ITB_YZNodeDevice(b);
            La *= (x - xb) / (xa - xb);
        }
        L[a] = La;
    }
}

__device__ __forceinline__ void itb_fold_k_weights_device(
    int k,
    const double raw_ws[ITB_YZ_ORDER],
    int k_idx[ITB_YZ_ORDER],
    double folded_ws[ITB_YZ_ORDER])
{
    const int half = ITB_YZ_ORDER / 2;
    const int raw_k0 = k - half;
    int phys_k0 = raw_k0;
    if (phys_k0 < 3) phys_k0 = 3;
    if (phys_k0 > NZ6 - 3 - ITB_YZ_ORDER)
        phys_k0 = NZ6 - 3 - ITB_YZ_ORDER;

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
        } else {
            folded_ws[kg - phys_k0] += w;
        }
    }
}

__device__ __forceinline__ double itb_stream_q(
    int q, int i, int j, int k,
    const double *f_post_read,
    const ITB_YZCoeff *itb_yz_coeff_d)
{
    const int nface = NX6 * NZ6;
    const int q_off = q * GRID_SIZE;

    if (q == 0) {
        const int index = j * nface + k * NX6 + i;
        return f_post_read[index];
    }

    const double ex = GILBM_e[q][0];
    const double ey = GILBM_e[q][1];
    const double ez = GILBM_e[q][2];

    if (ey == 0.0 && ez == 0.0) {
        const int xsign = (ex > 0.0) ? 0 : 1;
        const int base = q_off + j * nface + k * NX6 + (i - 3);
        double out = 0.0;
        for (int sx = 0; sx < ITB_X_ORDER; sx++)
            out += ITB_WX[xsign][sx] * f_post_read[base + sx];
        return out;
    }

    const int yz_id = ITB_YZClassFromQDevice(q);
    const ITB_YZCoeff c = itb_yz_coeff_d[ITB_CoeffIndexDevice(yz_id, j, k)];
    const int j0 = j - ITB_YZ_ORDER / 2;
    double wr[ITB_YZ_ORDER];
    double ws_raw[ITB_YZ_ORDER];
    double ws_folded[ITB_YZ_ORDER];
    int k_idx[ITB_YZ_ORDER];
    itb_yz_shape_device(c.r, wr);
    itb_yz_shape_device(c.s, ws_raw);
    itb_fold_k_weights_device(k, ws_raw, k_idx, ws_folded);

    if (ex == 0.0) {
        double out = 0.0;
        for (int sj = 0; sj < ITB_YZ_ORDER; sj++) {
            const int gj = j0 + sj;
            const double wj = wr[sj];
            for (int sk = 0; sk < ITB_YZ_ORDER; sk++) {
                const int gk = k_idx[sk];
                out += wj * ws_folded[sk] *
                       f_post_read[q_off + gj * nface + gk * NX6 + i];
            }
        }
        return out;
    }

    const int xsign = (ex > 0.0) ? 0 : 1;
    const int i0 = i - 3;
    double out = 0.0;
    for (int sx = 0; sx < ITB_X_ORDER; sx++) {
        const double wx = ITB_WX[xsign][sx];
        const int gi = i0 + sx;
        for (int sj = 0; sj < ITB_YZ_ORDER; sj++) {
            const int gj = j0 + sj;
            const double wj = wr[sj];
            for (int sk = 0; sk < ITB_YZ_ORDER; sk++) {
                const int gk = k_idx[sk];
                out += wx * wj * ws_folded[sk] *
                       f_post_read[q_off + gj * nface + gk * NX6 + gi];
            }
        }
    }
    return out;
}

#endif
