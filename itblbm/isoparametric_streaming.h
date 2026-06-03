#ifndef ITBLBM_ISOPARAMETRIC_STREAMING_H
#define ITBLBM_ISOPARAMETRIC_STREAMING_H

#include "isoparametric_coeff.h"

__constant__ double ITB_WX[2][ITB_X_ORDER];

__device__ __forceinline__ size_t ITB_CoeffIndexDevice(int yz_id, int j, int k)
{
    return ((size_t)yz_id * (size_t)NYD6 + (size_t)j) * (size_t)NZ6 + (size_t)k;
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

    if (ex == 0.0) {
        double out = 0.0;
        for (int sj = 0; sj < ITB_YZ_ORDER; sj++) {
            const int gj = c.j0 + sj;
            const double wj = c.wr[sj];
            for (int sk = 0; sk < ITB_YZ_ORDER; sk++) {
                const int gk = c.k_idx[sk];
                out += wj * c.ws[sk] *
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
            const int gj = c.j0 + sj;
            const double wj = c.wr[sj];
            for (int sk = 0; sk < ITB_YZ_ORDER; sk++) {
                const int gk = c.k_idx[sk];
                out += wx * wj * c.ws[sk] *
                       f_post_read[q_off + gj * nface + gk * NX6 + gi];
            }
        }
    }
    return out;
}

#endif
