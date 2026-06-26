// ============================================================================
//  verify_gl3.cpp  -- self-contained (no CUDA, no MPI) numerical validation of
//  the Jacobian 3x3 Gauss-Legendre cell-volume scheme used by
//      /home/s8313697/5.Re10595/Edit13_2800ITBLBM/evolution.h
//  (D3Q19 GILBM body-fitted "Periodic Hill" Re2800 mass-correction weights).
//
//  ##########################################################################
//  #  SNAPSHOT WARNING -- READ BEFORE TRUSTING THIS HARNESS                  #
//  #  The six routines below (GL3_nodes, GL3_weights, Lagrange6Weights,      #
//  #  SelectStencilStart, InterpolateJ2D_Lagrange6, ShoelaceQuadArea) are    #
//  #  HAND-COPIED SNAPSHOTS of the solver kernels at the cited evolution.h   #
//  #  line numbers.  They are host-only reproductions: the originals are     #
//  #  CUDA/MPI-coupled and cannot be linked here, so there is NO shared      #
//  #  header.  If the solver's GL nodes/weights, stencil width, interpolation#
//  #  order, or Shoelace formula EVER change, THIS HARNESS MUST BE UPDATED   #
//  #  MANUALLY -- otherwise it will silently keep validating STALE code.     #
//  ##########################################################################
//
//  Hand-copied snapshots (byte-faithful to the solver -- do NOT alter numerics):
//    * GL3_nodes  = {(1-sqrt(0.6))/2, 0.5, (1+sqrt(0.6))/2}     (evolution.h:180)
//    * GL3_weights= {5/18, 8/18, 5/18}                          (evolution.h:185)
//    * Lagrange6Weights : degree-5 (6-pt) Lagrange basis        (evolution.h:191)
//    * SelectStencilStart : centered/clamped 6-wide stencil     (evolution.h:206)
//    * InterpolateJ2D_Lagrange6 : tensor 6x6 interp of nodal J  (evolution.h:215)
//    * ShoelaceQuadArea (MassCorrectionCellVolume) : planar quad (evolution.h:82)
//
//  Coordinate convention (matches the paper): xi = streamwise, zeta = wall-
//  normal; the in-plane J_2D lives on the (xi,zeta) cross-section plane.
//
//  Three experiments:
//    E1  Polynomial exactness of tensor GL-3 (degree<=5 -> machine zero).
//    E2  h-refinement on a genuinely-2D smooth curvilinear y-z map.  J_2D has a
//        genuine xi*zeta CROSS term (NON-separable) and varies in BOTH xi and
//        zeta, so the wall-normal tensor interpolation direction is genuinely
//        exercised, unlike a separable J(xi) for which the zeta-interpolation
//        would be trivially exact.  A curved (one-signed) top wall keeps
//        Method-0's chord error at O(h^2):
//          Method 0 = straight-chord Shoelace   -> expect O(h^2)
//          Method 1 = GL-3 of 6-pt Lagrange interp of NODAL J2D -> 6th order
//        (interpolate SAMPLED nodal J, exactly as the solver does -- the closed
//         form is never fed to the quadrature).  Method 1 also carries the
//         solver's isfinite/<=0 -> Shoelace fallback guard (evolution.h:300-316);
//         this strictly-positive map never trips it, so the fallback count = 0.
//    E3  One-shot mass restoration + momentum preservation (rest population e0=0).
//
//  Build:
//    g++ -O2 -std=c++17 verify_gl3.cpp -o verify_gl3
// ============================================================================
#include <cstdio>
#include <cmath>
#include <cstring>
#include <cstdlib>
#include <vector>
#include <algorithm>

// ---- EXACT copies of the solver constants (evolution.h:180-189) -------------
static const double GL3_nodes[3] = {
    0.5 * (1.0 - 0.7745966692414834),   // (1 - sqrt(3/5))/2
    0.5,
    0.5 * (1.0 + 0.7745966692414834)    // (1 + sqrt(3/5))/2
};
static const double GL3_weights[3] = {
    5.0 / 18.0,
    8.0 / 18.0,
    5.0 / 18.0
};

// ---- EXACT copy of Lagrange6Weights (evolution.h:191-204) -------------------
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

// ---- EXACT copy of SelectStencilStart (evolution.h:206-213) -----------------
static inline bool SelectStencilStart(int cell_idx, int lo, int hi, int *start_out)
{
    int ideal = cell_idx - 2;
    int max_start = hi - 5;
    if (max_start - lo < 0) return false;
    *start_out = (ideal < lo) ? lo : (ideal > max_start) ? max_start : ideal;
    return true;
}

// ---- EXACT copy of InterpolateJ2D_Lagrange6 (evolution.h:215-231) -----------
//  J_2D is a flat [Nrow x NZ6_local] array of NODAL J2D values; xi_pos/zeta_pos
//  are node-index coordinates; sj/sk are stencil starts.
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

// ============================================================================
//  Genuinely-2D smooth curvilinear (y,z) map on the unit square
//  (xi = streamwise, zeta = wall-normal).  The in-plane Jacobian J_2D varies in
//  BOTH xi and zeta and carries a genuine xi*zeta CROSS term, so it is NON-
//  separable (not a function of the streamwise index alone, and not a pure sum
//  or product of single-variable factors).  The tensor 6-point Lagrange
//  interpolation is therefore exercised in the wall-normal direction too -- a
//  separable J(xi) would make the zeta-interpolation trivially exact and never
//  test that path.
//
//  We prescribe the Jacobian directly and integrate it in zeta to obtain z.
//  With a LINEAR streamwise coordinate y = xi (so y_zeta = 0), the in-plane
//  Jacobian is exactly J_2D = z_zeta:
//      J_2D(xi,zeta) = 1 - a*sin(pi*xi) + C*sin(pi*zeta) - D*sin(pi*xi)*sin(pi*zeta)
//      y(xi,zeta)    = xi
//      z(xi,zeta)    = zeta*(1 - a*sin(pi*xi))
//                        + (C - D*sin(pi*xi)) * (1 - cos(pi*zeta))/pi
//  (z is the zeta-antiderivative of J_2D with a FLAT bottom wall z(xi,0)=0; the
//   top wall z(xi,1) = 1 - a*sin(pi*xi) + (C - D*sin(pi*xi))*2/pi is a CURVED,
//   one-signed sinusoidal wall whose straight chords Method-0 misses at O(h^2).)
//  Amplitudes a=0.30, C=0.20, D=0.10 keep J_2D strictly positive: J_2D in
//  [0.7, 1.2] > 0 everywhere (the minimum 0.7 is attained at xi=1/2, zeta=0);
//  a runtime assert in E2 checks min sampled J > 0.
//  EXACT reference area (closed form; one-line derivation, using
//  int_0^1 sin(pi*t) dt = 2/pi):
//      int_0^1 int_0^1 J_2D dxi dzeta
//        = 1 - a*(2/pi) + C*(2/pi) - D*(2/pi)*(2/pi)
//        = 1 + (2/pi)*(C - a) - D*(4/pi^2)
//        = 1 - 0.2/pi - 0.4/pi^2   (with a=.30, C=.20, D=.10)  ~ 0.8958095.
// ============================================================================
static const double PI    = 3.14159265358979323846;
static const double A_str = 0.30;   // streamwise sin(pi*xi)  amplitude in J_2D
static const double C_wn  = 0.20;   // wall-normal sin(pi*zeta) amplitude in J_2D
static const double D_x   = 0.10;   // genuine xi*zeta CROSS-term amplitude in J_2D

static inline double map_y(double xi, double zeta){ (void)zeta; return xi; }
static inline double map_z(double xi, double zeta){
    const double sx = sin(PI*xi);
    return zeta*(1.0 - A_str*sx) + (C_wn - D_x*sx)*(1.0 - cos(PI*zeta))/PI;
}
// Closed-form in-plane Jacobian J_2D = y_xi*z_zeta - y_zeta*z_xi = z_zeta (y=xi).
// NOTE: the solver-identical Method-1 path below NEVER feeds this closed form to
// the quadrature -- it interpolates SAMPLED nodal J, exactly as the solver does.
static inline double J2D(double xi, double zeta){
    const double sx = sin(PI*xi), sz = sin(PI*zeta);
    return 1.0 - A_str*sx + C_wn*sz - D_x*sx*sz;
}
// Exact closed-form true area (derivation above); THIS is the error metric
// reference.  A coarse composite GL-3 of the closed-form J is computed only as
// an independent cross-check sanity print (M=256 reaches ~1e-12); there is NO
// 37.7M-eval static-init integration -- the exact area is known in closed form.
static const double AREA_ANALYTIC = 1.0 - 0.2/PI - 0.4/(PI*PI);
static double reference_area_check(int M)
{
    const double H = 1.0 / (double)M;
    double area = 0.0;
    for (int p = 0; p < M; p++) {
        double x0 = p * H;
        for (int q = 0; q < M; q++) {
            double z0 = q * H;
            double s = 0.0;
            for (int a = 0; a < 3; a++)
            for (int b = 0; b < 3; b++)
                s += GL3_weights[a]*GL3_weights[b]
                     * J2D(x0 + H*GL3_nodes[a], z0 + H*GL3_nodes[b]);
            area += H*H * s;
        }
    }
    return area;
}

// Shoelace area of the planar quadrilateral with corners (mirrors evolution.h:82)
static inline double ShoelaceQuadArea(
    double y0,double z0, double y1,double z1, double y2,double z2, double y3,double z3)
{
    return 0.5 * std::fabs(
          y0*z1 - z0*y1
        + y1*z2 - z1*y2
        + y2*z3 - z2*y3
        + y3*z0 - z3*y0);
}

// ============================================================================
//  E1 : Polynomial exactness of tensor GL-3 (here shown in 1D; tensor product
//       inherits per-direction exactness).  Integrate xi^m over [0,1].
// ============================================================================
static void run_E1(FILE* out)
{
    fprintf(out, "============================================================\n");
    fprintf(out, " E1  GL-3 polynomial exactness  (integral_0^1 xi^m dxi)\n");
    fprintf(out, "     GL-3 nodes = {%.16f, %.16f, %.16f}\n",
            GL3_nodes[0], GL3_nodes[1], GL3_nodes[2]);
    fprintf(out, "     GL-3 wts   = {5/18, 8/18, 5/18}; exact for degree <= 5\n");
    fprintf(out, "------------------------------------------------------------\n");
    fprintf(out, "   m |     exact 1/(m+1) |    GL-3 quadrature |   abs error\n");
    fprintf(out, "------------------------------------------------------------\n");
    for (int m = 0; m <= 7; m++) {
        double exact = 1.0 / (double)(m + 1);
        double quad  = 0.0;
        for (int a = 0; a < 3; a++)
            quad += GL3_weights[a] * std::pow(GL3_nodes[a], (double)m);
        double err = std::fabs(quad - exact);
        fprintf(out, "  %2d | %.16e | %.16e | %.3e%s\n",
                m, exact, quad, err,
                (m <= 5) ? "   (deg<=5: machine zero)" :
                           "   (deg>=6: nonzero, predicted)");
    }
    // Analytic leading Gauss remainder for n=3 (exact thru degree 5):
    //   E_m = (b-a)^{2n+1} (n!)^4 / [(2n+1)((2n)!)^3] * f^{(2n)}(xi)
    // For [0,1], n=3 -> C = (3!)^4 / (7*(6!)^3) = 1296 / (7*373248000).
    double C = std::pow(6.0,4.0) / (7.0 * std::pow(720.0,3.0));
    fprintf(out, "------------------------------------------------------------\n");
    fprintf(out, "   1D Gauss-3 remainder constant C = (3!)^4/(7*(6!)^3) = %.6e\n", C);
    fprintf(out, "   For f=xi^6: f^{(6)}=720 -> predicted |E_6| = C*720 = %.6e\n", C*720.0);
    fprintf(out, "\n");
}

// ============================================================================
//  E2 : h-refinement convergence study.
//   For N cells per direction (h=1/N), node m at xi=m*h, m=0..N (N+1 nodes).
//   Method 0: straight-chord Shoelace on physical (y,z) corners, sum cells.
//   Method 1: nodal J_2D sampled at nodes; per Gauss point reconstruct J via
//             6-pt tensor Lagrange interp of SAMPLED nodal J (solver-identical),
//             integrate with GL-3.  Per index-cell area = h^2 * sum W_a W_b J.
//             Mirrors the solver guard (evolution.h:300-316): if any Gauss-point
//             J is non-finite or <= 0 the cell falls back to the exact Shoelace
//             area (and a fallback counter is bumped).
//   Stencils clamp near boundaries via SelectStencilStart(lo=0,hi=N).
// ============================================================================
struct E2row { int N; double h; double e0; double e1; double minJ; long fb1; };

static E2row run_E2_one(int N)
{
    const int NN = N + 1;                 // nodes per direction
    const double h = 1.0 / (double)N;     // computational cell width

    // nodal physical positions and nodal metric J_2D (sampled, NOT closed form
    // at the Gauss points -- exactly the solver's "interpolate sampled J" path)
    std::vector<double> Y(NN*NN), Z(NN*NN), Jn(NN*NN);
    double minJ = 1.0e300;
    for (int j = 0; j < NN; j++) {        // xi index (streamwise)
        double xi = j * h;
        for (int k = 0; k < NN; k++) {    // zeta index (wall-normal)
            double zeta = k * h;
            Y [j*NN+k] = map_y(xi, zeta);
            Z [j*NN+k] = map_z(xi, zeta);
            double Jv = J2D(xi, zeta);
            Jn[j*NN+k] = Jv;
            if (Jv < minJ) minJ = Jv;
        }
    }
    // runtime assert: the analytic map must stay strictly positive so the
    // isfinite/<=0 fallback below is genuinely a never-triggered safety path.
    if (!(minJ > 0.0)) {
        fprintf(stderr, "FATAL: sampled min J_2D = %.6e <= 0 (N=%d)\n", minJ, N);
        std::abort();
    }

    // ---- Method 0 : straight-chord Shoelace -------------------------------
    double area0 = 0.0;
    for (int jc = 0; jc < N; jc++) {
    for (int kc = 0; kc < N; kc++) {
        double y0 = Y[ jc   *NN + kc  ], z0 = Z[ jc   *NN + kc  ];
        double y1 = Y[(jc+1)*NN + kc  ], z1 = Z[(jc+1)*NN + kc  ];
        double y2 = Y[(jc+1)*NN + kc+1], z2 = Z[(jc+1)*NN + kc+1];
        double y3 = Y[ jc   *NN + kc+1], z3 = Z[ jc   *NN + kc+1];
        area0 += ShoelaceQuadArea(y0,z0,y1,z1,y2,z2,y3,z3);
    }}

    // ---- Method 1 : GL-3 of 6-pt Lagrange interp of nodal J ----------------
    //  with the solver's isfinite/<=0 -> Shoelace fallback guard.
    double area1 = 0.0;
    long   fb1   = 0;
    for (int jc = 0; jc < N; jc++) {
    for (int kc = 0; kc < N; kc++) {
        int sj, sk;
        bool sj_ok = SelectStencilStart(jc, 0, N, &sj);
        bool sk_ok = SelectStencilStart(kc, 0, N, &sk);
        double cell_area = 0.0;
        bool used_fallback = false;
        if (sj_ok && sk_ok) {
            double a_jac = 0.0;
            for (int a = 0; a < 3 && !used_fallback; a++)
            for (int b = 0; b < 3; b++) {
                double xi_pos   = (double)jc + GL3_nodes[a];
                double zeta_pos = (double)kc + GL3_nodes[b];
                double Jv = InterpolateJ2D_Lagrange6(xi_pos, zeta_pos, sj, sk,
                                                     Jn.data(), NN);
                // solver guard (evolution.h:300-316): reject non-finite / <=0 J
                if (!std::isfinite(Jv) || Jv <= 0.0) { used_fallback = true; break; }
                a_jac += GL3_weights[a] * GL3_weights[b] * Jv;
            }
            if (!used_fallback) cell_area = h*h * a_jac;  // index-cell unit-area * h^2
        } else {
            used_fallback = true;                          // 6-wide stencil unavailable
        }
        if (used_fallback) {
            // Shoelace fallback (matches solver evolution.h:300-316)
            double y0 = Y[ jc   *NN + kc  ], z0 = Z[ jc   *NN + kc  ];
            double y1 = Y[(jc+1)*NN + kc  ], z1 = Z[(jc+1)*NN + kc  ];
            double y2 = Y[(jc+1)*NN + kc+1], z2 = Z[(jc+1)*NN + kc+1];
            double y3 = Y[ jc   *NN + kc+1], z3 = Z[ jc   *NN + kc+1];
            cell_area = ShoelaceQuadArea(y0,z0,y1,z1,y2,z2,y3,z3);
            fb1++;
        }
        area1 += cell_area;
    }}

    E2row r;
    r.N = N; r.h = h;
    r.e0 = std::fabs(area0 - AREA_ANALYTIC);
    r.e1 = std::fabs(area1 - AREA_ANALYTIC);
    r.minJ = minJ;
    r.fb1  = fb1;
    return r;
}

static void run_E2(FILE* out)
{
    fprintf(out, "============================================================\n");
    fprintf(out, " E2  h-refinement convergence on a genuinely-2D smooth y-z map\n");
    fprintf(out, "     y = xi ; z = zeta*(1-a*sin(pi*xi)) + (C-D*sin(pi*xi))*(1-cos(pi*zeta))/pi\n");
    fprintf(out, "     J_2D = 1 - a*sin(pi*xi) + C*sin(pi*zeta) - D*sin(pi*xi)*sin(pi*zeta)\n");
    fprintf(out, "            (a=%.2f C=%.2f D=%.2f ; NON-separable cross term -> varies in xi AND zeta)\n",
            A_str, C_wn, D_x);
    fprintf(out, "     TRUE area (exact closed form 1-0.2/pi-0.4/pi^2) = %.15f\n", AREA_ANALYTIC);
    {
        double chk = reference_area_check(256);
        fprintf(out, "     cross-check (composite GL-3 of closed-form J, M=256) = %.15f  |err|=%.3e\n",
                chk, std::fabs(chk - AREA_ANALYTIC));
    }
    fprintf(out, "     Method 0 = straight-chord Shoelace ; Method 1 = GL-3 of\n");
    fprintf(out, "     6-pt Lagrange interp of SAMPLED nodal J (solver-identical)\n");
    fprintf(out, "------------------------------------------------------------------------------------\n");
    fprintf(out, "    N |    h     |  Method0 err  | ord |  Method1 err  | ord\n");
    fprintf(out, "------------------------------------------------------------------------------------\n");

    int Ns[] = {8, 16, 32, 64, 128, 256};
    std::vector<E2row> rows;
    for (int N : Ns) rows.push_back(run_E2_one(N));

    for (size_t i = 0; i < rows.size(); i++) {
        double ord0 = (i==0) ? 0.0 : std::log2(rows[i-1].e0 / rows[i].e0);
        double ord1 = (i==0) ? 0.0 : std::log2(rows[i-1].e1 / rows[i].e1);
        if (i == 0)
            fprintf(out, " %4d | %.6f | %.6e |  -  | %.6e |  -\n",
                    rows[i].N, rows[i].h, rows[i].e0, rows[i].e1);
        else
            fprintf(out, " %4d | %.6f | %.6e | %4.2f | %.6e | %5.2f\n",
                    rows[i].N, rows[i].h, rows[i].e0, ord0, rows[i].e1, ord1);
    }
    fprintf(out, "------------------------------------------------------------------------------------\n");
    fprintf(out, "  EXPECT: Method 0 ~ O(h^2) ; Method 1 ~ 6th order until the round-off floor\n");
    fprintf(out, "  (Lagrange-6 reconstruction of the SAMPLED metric is the binding error term).\n");

    // min J (assert passed) and Method-1 Shoelace-fallback summary
    {
        double gminJ = 1.0e300; long totfb = 0;
        for (size_t i = 0; i < rows.size(); i++) {
            if (rows[i].minJ < gminJ) gminJ = rows[i].minJ;
            totfb += rows[i].fb1;
        }
        fprintf(out, "  min sampled J_2D over all N = %.6f  (> 0 assert PASSED)\n", gminJ);
        fprintf(out, "  Method-1 Shoelace-fallback count (all N) = %ld"
                     "  (expected 0: smooth map keeps J>0, never-triggered safety path)\n", totfb);
    }
    fprintf(out, "\n");

    // emit a compact machine-readable block for table assembly
    fprintf(out, "[E2-CSV] N,h,err0,ord0,err1,ord1\n");
    for (size_t i = 0; i < rows.size(); i++) {
        double ord0 = (i==0) ? 0.0 : std::log2(rows[i-1].e0 / rows[i].e0);
        double ord1 = (i==0) ? 0.0 : std::log2(rows[i-1].e1 / rows[i].e1);
        fprintf(out, "[E2-CSV] %d,%.8e,%.8e,%.4f,%.8e,%.4f\n",
                rows[i].N, rows[i].h, rows[i].e0, ord0, rows[i].e1, ord1);
    }
    fprintf(out, "\n");
}

// ============================================================================
//  E3 : one-shot mass restoration + momentum preservation.
//   median-dual weights from each volume method; tiny perturbed nodal rho field;
//   <rho>_V = (sum w rho)/V ; delta = 1 - <rho>_V into rest population f0.
//   Confirm new <rho>_V == 1 (one-shot) and sum_q e_q f_q unchanged (e0=0).
// ============================================================================
static void run_E3(FILE* out)
{
    fprintf(out, "============================================================\n");
    fprintf(out, " E3  one-shot mass restoration + momentum preservation\n");
    fprintf(out, "------------------------------------------------------------\n");

    // small curvilinear patch, N=16 cells/dir; build median-dual nodal weights
    const int N = 16, NN = N + 1;
    const double h = 1.0 / (double)N;
    std::vector<double> Y(NN*NN), Z(NN*NN), Jn(NN*NN);
    for (int j=0;j<NN;j++){ double xi=j*h; for(int k=0;k<NN;k++){ double zeta=k*h;
        Y[j*NN+k]=map_y(xi,zeta); Z[j*NN+k]=map_z(xi,zeta); Jn[j*NN+k]=J2D(xi,zeta);} }

    auto cellArea0 = [&](int jc,int kc){
        double y0=Y[jc*NN+kc],z0=Z[jc*NN+kc],y1=Y[(jc+1)*NN+kc],z1=Z[(jc+1)*NN+kc];
        double y2=Y[(jc+1)*NN+kc+1],z2=Z[(jc+1)*NN+kc+1],y3=Y[jc*NN+kc+1],z3=Z[jc*NN+kc+1];
        return ShoelaceQuadArea(y0,z0,y1,z1,y2,z2,y3,z3);
    };
    // Method-1 cell area with the solver's isfinite/<=0 -> Shoelace fallback guard.
    long e3_fallback = 0;
    auto cellArea1 = [&](int jc,int kc){
        int sj,sk;
        bool sj_ok = SelectStencilStart(jc,0,N,&sj);
        bool sk_ok = SelectStencilStart(kc,0,N,&sk);
        bool fb = !(sj_ok && sk_ok);
        double a=0.0;
        for(int p=0;p<3 && !fb;p++)for(int q=0;q<3;q++){
            double Jv=InterpolateJ2D_Lagrange6(jc+GL3_nodes[p],kc+GL3_nodes[q],sj,sk,Jn.data(),NN);
            if(!std::isfinite(Jv)||Jv<=0.0){ fb=true; break; }
            a+=GL3_weights[p]*GL3_weights[q]*Jv;}
        if(fb){ e3_fallback++; return cellArea0(jc,kc); }   // Shoelace fallback (solver guard)
        return h*h*a;
    };

    // median-dual nodal weight: w_p = (1/4)*sum of the (<=4) 2D cells touching p
    // (2D analog of evolution.h's (1/8)*sum of <=8 3D cells).
    auto buildWeights = [&](bool method1, std::vector<double>& W){
        W.assign(NN*NN, 0.0);
        for (int jc=0;jc<N;jc++) for(int kc=0;kc<N;kc++){
            double A = method1 ? cellArea1(jc,kc) : cellArea0(jc,kc);
            int c[4][2] = {{jc,kc},{jc+1,kc},{jc+1,kc+1},{jc,kc+1}};
            for (int c4=0;c4<4;c4++) W[c[c4][0]*NN+c[c4][1]] += 0.25*A;
        }
    };
    std::vector<double> W0, W1; buildWeights(false,W0); buildWeights(true,W1);

    // perturbed nodal rho field (compressibility-like drift around 1)
    auto rhoField = [&](int j,int k){
        double xi=j*h, zeta=k*h;
        return 1.0 + 0.01*sin(2*PI*xi) + 0.007*cos(3*PI*zeta) - 0.003;
    };

    auto massCheck = [&](const std::vector<double>& W, const char* name){
        double V=0.0, S=0.0;
        for (int j=0;j<NN;j++) for(int k=0;k<NN;k++){ double w=W[j*NN+k]; V+=w; S+=w*rhoField(j,k);}
        double avg = S / V;
        double delta = 1.0 - avg;                 // rho_modify (evolution.h:449)
        // apply uniform shift to every node's rho (rho <- rho + delta) and recompute
        double S2=0.0; for(int j=0;j<NN;j++) for(int k=0;k<NN;k++){ double w=W[j*NN+k];
            S2 += w*(rhoField(j,k)+delta); }
        double avg2 = S2 / V;
        fprintf(out, "  %-18s V=%.12e  <rho>_V=%.16f\n", name, V, avg);
        fprintf(out, "  %-18s delta_rho=1-<rho>_V = %+.3e ; AFTER apply <rho>_V=%.16f ; |1-new|=%.3e\n",
                "", delta, avg2, std::fabs(1.0-avg2));
        return std::fabs(1.0-avg2);
    };

    double r0 = massCheck(W0, "Method0 (Shoelace)");
    double r1 = massCheck(W1, "Method1 (Jac GL-3) ");

    // momentum preservation: D3Q19 lattice, e0=(0,0,0); shift only f[0].
    const int NQ=19;
    int ex[NQ]={0, 1,-1, 0, 0, 0, 0, 1,-1, 1,-1, 1,-1, 1,-1, 0, 0, 0, 0};
    int ey[NQ]={0, 0, 0, 1,-1, 0, 0, 1,-1,-1, 1, 0, 0, 0, 0, 1,-1, 1,-1};
    int ez[NQ]={0, 0, 0, 0, 0, 1,-1, 0, 0, 0, 0, 1,-1,-1, 1, 1,-1,-1, 1};
    double Wq[NQ]; for(int q=0;q<NQ;q++){ int s=ex[q]*ex[q]+ey[q]*ey[q]+ez[q]*ez[q];
        Wq[q]=(s==0)?(1.0/3.0):(s==1)?(1.0/18.0):(1.0/36.0);}
    double f[NQ]; for(int q=0;q<NQ;q++) f[q]=Wq[q]*1.0;  // rest state, rho=1, u=0
    auto mom=[&](double M[3]){ M[0]=M[1]=M[2]=0; for(int q=0;q<NQ;q++){
        M[0]+=ex[q]*f[q]; M[1]+=ey[q]*f[q]; M[2]+=ez[q]*f[q]; } };
    double Mb[3]; mom(Mb);
    double delta_demo = 0.0123456789;            // any uniform delta_rho
    f[0] += delta_demo;                          // rho_stream += d ; f_arr[0] += d
    double Ma[3]; mom(Ma);
    double rho_before=1.0, rho_after=0.0; for(int q=0;q<NQ;q++) rho_after+=f[q];

    fprintf(out, "------------------------------------------------------------\n");
    fprintf(out, "  Momentum (D3Q19, e0=0, inject delta_rho=%.10f into f[0]):\n", delta_demo);
    fprintf(out, "    rho:  before=%.16f  after=%.16f  (Delta=%.16f = delta_rho)\n",
            rho_before, rho_after, rho_after-rho_before);
    fprintf(out, "    momentum before = (% .3e, % .3e, % .3e)\n", Mb[0],Mb[1],Mb[2]);
    fprintf(out, "    momentum after  = (% .3e, % .3e, % .3e)\n", Ma[0],Ma[1],Ma[2]);
    fprintf(out, "    |Delta momentum|= (% .3e, % .3e, % .3e)  -> EXACTLY preserved\n",
            std::fabs(Ma[0]-Mb[0]), std::fabs(Ma[1]-Mb[1]), std::fabs(Ma[2]-Mb[2]));
    fprintf(out, "------------------------------------------------------------\n");
    fprintf(out, "  One-shot restoration residual |1-<rho>_V_new|: Method0=%.3e  Method1=%.3e\n",
            r0, r1);
    fprintf(out, "  Method-1 Shoelace-fallback count (E3 patch) = %ld (expected 0: smooth J>0)\n",
            e3_fallback);
    fprintf(out, "  (residual is pure FP rounding of the weighted reduction; ~1e-16)\n\n");
}

int main()
{
    FILE* out = stdout;
    fprintf(out, "############################################################\n");
    fprintf(out, "#  GL-3 Jacobian cell-volume scheme validation             #\n");
    fprintf(out, "#  mirrors evolution.h of Edit13_2800ITBLBM (D3Q19 GILBM)   #\n");
    fprintf(out, "############################################################\n\n");
    run_E1(out);
    run_E2(out);
    run_E3(out);
    fprintf(out, "############################  DONE  #########################\n");
    return 0;
}
