#ifndef MRT_PROJECTION_HOST_H
#define MRT_PROJECTION_HOST_H

#include <cmath>

#if USE_MRT

struct MrtProjectionVerification {
    double max_identity_error;
    double max_conserved_relax_error;
    double max_force_moment_error;
    double max_collision_abs_error;
    double max_collision_rel_error;
    int samples;
};

static inline void BuildD3Q19RatesHost(double s_visc, double s[19])
{
    s[0] = 0.0;
    s[1] = 1.19;
    s[2] = 1.4;
    s[3] = 0.0;
    s[4] = 1.2;
    s[5] = 0.0;
    s[6] = 1.2;
    s[7] = 0.0;
    s[8] = 1.2;
    s[9] = s_visc;
    s[10] = 1.4;
    s[11] = s_visc;
    s[12] = 1.4;
    s[13] = s_visc;
    s[14] = s_visc;
    s[15] = s_visc;
    s[16] = 1.98;
    s[17] = 1.98;
    s[18] = 1.98;
}

static inline void D3Q19HostVelocityWeight(int q, double *cx, double *cy, double *cz, double *wq)
{
    static const double e[19][3] = {
        {0,0,0},
        {1,0,0},{-1,0,0},{0,1,0},{0,-1,0},{0,0,1},{0,0,-1},
        {1,1,0},{-1,1,0},{1,-1,0},{-1,-1,0},
        {1,0,1},{-1,0,1},{1,0,-1},{-1,0,-1},
        {0,1,1},{0,-1,1},{0,1,-1},{0,-1,-1}
    };
    static const double w[19] = {
        1.0/3.0,
        1.0/18.0, 1.0/18.0, 1.0/18.0, 1.0/18.0, 1.0/18.0, 1.0/18.0,
        1.0/36.0, 1.0/36.0, 1.0/36.0, 1.0/36.0,
        1.0/36.0, 1.0/36.0, 1.0/36.0, 1.0/36.0,
        1.0/36.0, 1.0/36.0, 1.0/36.0, 1.0/36.0
    };
    *cx = e[q][0];
    *cy = e[q][1];
    *cz = e[q][2];
    *wq = w[q];
}

static inline double HostFeqAlpha(int q, double rho, double u0, double v0, double w0)
{
    double cx, cy, cz, wq;
    D3Q19HostVelocityWeight(q, &cx, &cy, &cz, &wq);
    const double eu = cx * u0 + cy * v0 + cz * w0;
    const double uu = u0 * u0 + v0 * v0 + w0 * w0;
    return wq * rho * (1.0 + 3.0 * eu + 4.5 * eu * eu - 1.5 * uu);
}

static inline void HostAnalyticalMrtEquilibrium(
    double rho, double u0, double v0, double w0, double m_eq[19])
{
    const double ux2 = u0 * u0;
    const double uy2 = v0 * v0;
    const double uz2 = w0 * w0;
    const double u2 = ux2 + uy2 + uz2;

    m_eq[0]  = rho;
    m_eq[1]  = rho * (-11.0 + 19.0 * u2);
    m_eq[2]  = rho * (3.0 - 5.5 * u2);
    m_eq[3]  = rho * u0;
    m_eq[4]  = rho * (-2.0/3.0) * u0;
    m_eq[5]  = rho * v0;
    m_eq[6]  = rho * (-2.0/3.0) * v0;
    m_eq[7]  = rho * w0;
    m_eq[8]  = rho * (-2.0/3.0) * w0;
    m_eq[9]  = rho * (2.0 * ux2 - uy2 - uz2);
    m_eq[10] = rho * (-0.5) * (2.0 * ux2 - uy2 - uz2);
    m_eq[11] = rho * (uy2 - uz2);
    m_eq[12] = rho * (-0.5) * (uy2 - uz2);
    m_eq[13] = rho * u0 * v0;
    m_eq[14] = rho * v0 * w0;
    m_eq[15] = rho * u0 * w0;
    m_eq[16] = 0.0;
    m_eq[17] = 0.0;
    m_eq[18] = 0.0;
}

static inline void ProjectForcingBasisHost(
    const double M[19][19],
    const double Mi[19][19],
    const double s[19],
    const double F_unit[19],
    double Fproj_out[19])
{
    double F_mom[19];
    for (int n = 0; n < 19; n++) {
        double sum = 0.0;
        for (int q = 0; q < 19; q++) {
            sum += M[n][q] * F_unit[q];
        }
        F_mom[n] = (1.0 - 0.5 * s[n]) * sum;
    }
    for (int a = 0; a < 19; a++) {
        double sum = 0.0;
        for (int n = 0; n < 19; n++) {
            sum += Mi[a][n] * F_mom[n];
        }
        Fproj_out[a] = sum;
    }
}

static inline void BuildMrtProjectionTablesHost(
    const double M[19][19],
    const double Mi[19][19],
    double s_visc,
    double K[19][19],
    double Fproj[19],
    double Fproj_u[19],
    double Fproj_v[19],
    double Fproj_w[19])
{
    double s[19];
    BuildD3Q19RatesHost(s_visc, s);

    for (int a = 0; a < 19; a++) {
        for (int b = 0; b < 19; b++) {
            double sum = 0.0;
            for (int n = 0; n < 19; n++) {
                sum += Mi[a][n] * s[n] * M[n][b];
            }
            K[a][b] = sum;
        }
    }

    double F0[19], Fu[19], Fv[19], Fw[19];
    for (int q = 0; q < 19; q++) {
        double cx, cy, cz, wq;
        D3Q19HostVelocityWeight(q, &cx, &cy, &cz, &wq);
        F0[q] = wq * 3.0 * cy;
        Fu[q] = wq * 9.0 * cx * cy;
        Fv[q] = wq * (9.0 * cy * cy - 3.0);
        Fw[q] = wq * 9.0 * cz * cy;
    }

    ProjectForcingBasisHost(M, Mi, s, F0, Fproj);
    ProjectForcingBasisHost(M, Mi, s, Fu, Fproj_u);
    ProjectForcingBasisHost(M, Mi, s, Fv, Fproj_v);
    ProjectForcingBasisHost(M, Mi, s, Fw, Fproj_w);
}

static inline void LegacyMrtCollisionHost(
    const double M[19][19],
    const double Mi[19][19],
    double s_visc,
    double dt_val,
    double force,
    const double f_B[19],
    double rho, double u0, double v0, double w0,
    double f_out[19])
{
    double s[19], m_B[19], m_eq[19], m_star[19];
    BuildD3Q19RatesHost(s_visc, s);
    HostAnalyticalMrtEquilibrium(rho, u0, v0, w0, m_eq);

    for (int n = 0; n < 19; n++) {
        double sum_f = 0.0;
        for (int q = 0; q < 19; q++) {
            sum_f += M[n][q] * f_B[q];
        }
        m_B[n] = sum_f;
        m_star[n] = m_eq[n] + (1.0 - s[n]) * (m_B[n] - m_eq[n]);
    }

#if USE_GUO_FORCING
    double F_mom[19];
    for (int n = 0; n < 19; n++) {
        double sum = 0.0;
        for (int q = 0; q < 19; q++) {
            double cx, cy, cz, wq;
            D3Q19HostVelocityWeight(q, &cx, &cy, &cz, &wq);
            const double c_dot_u = cx * u0 + cy * v0 + cz * w0;
            const double Fq = wq * force * (3.0 * (cy - v0) + 9.0 * c_dot_u * cy);
            sum += M[n][q] * Fq;
        }
        F_mom[n] = sum;
    }
    for (int n = 0; n < 19; n++) {
        m_star[n] += dt_val * (1.0 - 0.5 * s[n]) * F_mom[n];
    }
#endif

    for (int a = 0; a < 19; a++) {
        double sum = 0.0;
        for (int n = 0; n < 19; n++) {
            sum += Mi[a][n] * m_star[n];
        }
        f_out[a] = sum;
#if !USE_GUO_FORCING
        double cx, cy, cz, wq;
        D3Q19HostVelocityWeight(a, &cx, &cy, &cz, &wq);
        f_out[a] += wq * 3.0 * cy * force * dt_val;
#endif
    }
}

static inline void ProjectionMrtCollisionHost(
    const double K[19][19],
    const double Fproj[19],
    const double Fproj_u[19],
    const double Fproj_v[19],
    const double Fproj_w[19],
    double dt_val,
    double force,
    const double f_B[19],
    double rho, double u0, double v0, double w0,
    double f_out[19])
{
    double fneq[19];
    for (int q = 0; q < 19; q++) {
        fneq[q] = f_B[q] - HostFeqAlpha(q, rho, u0, v0, w0);
    }
    for (int a = 0; a < 19; a++) {
        double relax = 0.0;
        for (int b = 0; b < 19; b++) {
            relax += K[a][b] * fneq[b];
        }
        f_out[a] = f_B[a] - relax;
#if USE_GUO_FORCING
        f_out[a] += dt_val * force *
            (Fproj[a] + u0 * Fproj_u[a] + v0 * Fproj_v[a] + w0 * Fproj_w[a]);
#else
        double cx, cy, cz, wq;
        D3Q19HostVelocityWeight(a, &cx, &cy, &cz, &wq);
        f_out[a] += wq * 3.0 * cy * force * dt_val;
#endif
    }
}

static inline MrtProjectionVerification VerifyMrtProjectionHost(
    const double M[19][19],
    const double Mi[19][19],
    const double K[19][19],
    const double Fproj[19],
    const double Fproj_u[19],
    const double Fproj_v[19],
    const double Fproj_w[19],
    double s_visc,
    double dt_val)
{
    MrtProjectionVerification v = {0.0, 0.0, 0.0, 0.0, 0.0, 0};

    for (int a = 0; a < 19; a++) {
        for (int b = 0; b < 19; b++) {
            double sum = 0.0;
            for (int n = 0; n < 19; n++) {
                sum += Mi[a][n] * M[n][b];
            }
            const double expect = (a == b) ? 1.0 : 0.0;
            const double err = fabs(sum - expect);
            if (err > v.max_identity_error) v.max_identity_error = err;
        }
    }

    const int conserved[4] = {0, 3, 5, 7};
    for (int ci = 0; ci < 4; ci++) {
        const int n_cons = conserved[ci];
        for (int b = 0; b < 19; b++) {
            double sum = 0.0;
            for (int a = 0; a < 19; a++) {
                sum += M[n_cons][a] * K[a][b];
            }
            const double err = fabs(sum);
            if (err > v.max_conserved_relax_error) v.max_conserved_relax_error = err;
        }
    }

    const double force_expect[4] = {0.0, 0.0, 1.0, 0.0};
    const double *force_tables[4] = {Fproj, Fproj_u, Fproj_v, Fproj_w};
    for (int t = 0; t < 4; t++) {
        for (int ci = 0; ci < 4; ci++) {
            const int n_cons = conserved[ci];
            double sum = 0.0;
            for (int a = 0; a < 19; a++) {
                sum += M[n_cons][a] * force_tables[t][a];
            }
            const double expect = (t == 0) ? force_expect[ci] : 0.0;
            const double err = fabs(sum - expect);
            if (err > v.max_force_moment_error) v.max_force_moment_error = err;
        }
    }

    for (int c = 0; c < 12; c++) {
        const double rho = 0.92 + 0.017 * (double)c;
        const double u0 = -0.010 + 0.0017 * (double)c;
        const double v0 =  0.012 - 0.0013 * (double)c;
        const double w0 = -0.006 + 0.0011 * (double)c;
        const double force = ((c % 5) - 2) * 1.7e-6;
        const double scale = 1.0e-4 * (1.0 + 0.15 * (double)c);

        double m_neq[19], f_B[19], f_legacy[19], f_projection[19];
        for (int n = 0; n < 19; n++) {
            m_neq[n] = scale * sin(0.37 * (double)(c + 1) * (double)(n + 1));
        }
        m_neq[0] = 0.0;
        m_neq[3] = 0.0;
        m_neq[5] = 0.0;
        m_neq[7] = 0.0;

        for (int a = 0; a < 19; a++) {
            double fneq = 0.0;
            for (int n = 0; n < 19; n++) {
                fneq += Mi[a][n] * m_neq[n];
            }
            f_B[a] = HostFeqAlpha(a, rho, u0, v0, w0) + fneq;
        }

        LegacyMrtCollisionHost(M, Mi, s_visc, dt_val, force, f_B, rho, u0, v0, w0, f_legacy);
        ProjectionMrtCollisionHost(K, Fproj, Fproj_u, Fproj_v, Fproj_w,
                                   dt_val, force, f_B, rho, u0, v0, w0, f_projection);

        for (int q = 0; q < 19; q++) {
            const double abs_err = fabs(f_legacy[q] - f_projection[q]);
            const double denom = fmax(fabs(f_legacy[q]), 1.0e-30);
            const double rel_err = abs_err / denom;
            if (abs_err > v.max_collision_abs_error) v.max_collision_abs_error = abs_err;
            if (rel_err > v.max_collision_rel_error) v.max_collision_rel_error = rel_err;
        }
        v.samples++;
    }

    return v;
}

#endif  // USE_MRT

#endif  // MRT_PROJECTION_HOST_H
