// ════════════════════════════════════════════════════════════════════════════
//  Algorithm2 unit tests (factorial prompt §9) — standalone, NO CUDA, NO MPI.
//
//  Build & run:
//      g++ -std=c++14 -O2 -Wall -o /tmp/algo2_unit tests/algo2_unit_test.cpp
//      /tmp/algo2_unit          (exit 0 = all pass)
//
//  Covers §9 items:
//   1. y-z class mapping vs an INDEPENDENT hardcoded GILBM_e copy
//      (transcribed from 0.shared_code.h:23-29 — deliberately NOT generated
//      from gilbm2_* functions, breaking the validation circularity).
//   2. coordinate-table indexing (formula, bounds, uniqueness).
//   3/4. analytic uniform-metric departures (Lagrange-coordinate level).
//   5. boundary & clamp behavior incl. the t_zeta NO-[0,6]-clamp asymmetry.
//   6. no-weight-storage invariant (compile-time static_assert).
// ════════════════════════════════════════════════════════════════════════════
#define __host__
#define __device__
#define __forceinline__ inline
#define USE_GILBM_ALGORITHM2 1
#define GILBM_ALGO2_VALIDATE 2
#define GHOST_EXTRAP_ORDER 2

// production grid (variables.h values, 2026-06)
#define NX 321
#define NY 641
#define NZ 321
#define jp 32
#define NX6 (NX+6)
#define NYD6 ((NY-1)/jp+7)
#define NZ6 (NZ+6)

#include "../gilbm/precompute2.h"

#include <cstdio>
#include <cstring>
#include <cmath>
#include <vector>

// §9-6: no-weight-storage invariant — fails to COMPILE if anyone adds
// L_xi[7]/L_zeta[7]/49-tensor weights (or even a flags byte) to the table.
static_assert(sizeof(GILBM2_DepartCoords) == 2 * sizeof(double),
              "GILBM2_DepartCoords must stay coordinate-only (2 doubles)");
static_assert(sizeof(GILBM2_DepartWeights) == 14 * sizeof(double),
              "GILBM2_DepartWeights must contain exactly wr[7]+ws[7]");
static_assert(sizeof(GILBM2_DepartWeightsFolded) == 144,
              "GILBM2_DepartWeightsFolded layout must be j0+k_idx[7]+wr[7]+ws[7]");
static_assert(sizeof(GILBM2_Table) == sizeof(GILBM2_DepartWeightsFolded),
              "production Algorithm2 default must be WEIGHTS_FOLDED when enabled");

static int g_fail = 0;
#define CHECK(cond, ...) do { if (!(cond)) { g_fail++; \
    printf("  FAIL %s:%d  ", __FILE__, __LINE__); printf(__VA_ARGS__); printf("\n"); } } while (0)

// INDEPENDENT D3Q19 table — hardcoded transcription of GILBM_e
// (0.shared_code.h:23-29). Do NOT derive from gilbm2_* functions.
static const double E_REF[19][3] = {
    {0,0,0},
    {1,0,0},{-1,0,0},{0,1,0},{0,-1,0},{0,0,1},{0,0,-1},
    {1,1,0},{-1,1,0},{1,-1,0},{-1,-1,0},
    {1,0,1},{-1,0,1},{1,0,-1},{-1,0,-1},
    {0,1,1},{0,-1,1},{0,1,-1},{0,-1,-1}
};

// local replica of PrecomputeGILBM_StencilBaseK (precompute.h:236-251)
static int bk_of(int k) {
    int bk = k - 3;
    if (bk < 0)              bk = 0;
    if (bk + 6 >= (int)NZ6)  bk = (int)NZ6 - 7;
    return bk;
}

int main() {
    // ── §9-1: class map vs independent E_REF, all 19 q ──
    for (int q = 0; q < 19; q++) {
        const int cls = gilbm2_yz_class_from_q(q);
        double ey, ez;
        gilbm2_class_velocity(cls, &ey, &ez);
        CHECK(ey == E_REF[q][1] && ez == E_REF[q][2],
              "q=%d cls=%d got(%g,%g) want(%g,%g)", q, cls, ey, ez, E_REF[q][1], E_REF[q][2]);
        if (q == 0 || q == 1 || q == 2)
            CHECK(cls == 0, "q=%d should map to inert class 0, got %d", q, cls);
    }
    // classes 1..8 mutually distinct, nonzero
    for (int a = 1; a < GILBM2_NCLASS; a++) {
        double ay, az; gilbm2_class_velocity(a, &ay, &az);
        CHECK(!(ay == 0.0 && az == 0.0), "class %d must be a moving class", a);
        for (int b = a + 1; b < GILBM2_NCLASS; b++) {
            double by, bz; gilbm2_class_velocity(b, &by, &bz);
            CHECK(!(ay == by && az == bz), "classes %d,%d collide on (%g,%g)", a, b, ay, az);
        }
    }

    // ── §9-2: table indexing ──
    const size_t N = (size_t)GILBM2_NCLASS * NYD6 * NZ6;
    CHECK(gilbm2_coord_index(0, 0, 0) == 0, "index origin");
    CHECK(gilbm2_coord_index(GILBM2_NCLASS - 1, NYD6 - 1, NZ6 - 1) == N - 1, "index last == N-1");
    CHECK(gilbm2_coord_index(1, 0, 0) == (size_t)NYD6 * NZ6, "class stride");
    CHECK(gilbm2_coord_index(0, 1, 0) == (size_t)NZ6, "j stride");
    CHECK(gilbm2_coord_index(0, 0, 1) == 1, "k stride");

    // ── uniform metric: xi_y=1, xi_z=0, zeta_y=0, zeta_z=1 ──
    std::vector<double> xy(NYD6 * NZ6, 1.0), xz(NYD6 * NZ6, 0.0),
                        zy(NYD6 * NZ6, 0.0), zz(NYD6 * NZ6, 1.0);
    unsigned char fl = 0;

    // ── §9-3/4: analytic departures (interior j=10,k=10, bk=7) ──
    {   // class 1 (+1,0): contravariant (1,0) → t_xi = 3-dt, t_zeta = k-bk = 3
        GILBM2_DepartCoords c = gilbm2_gen_departure_coords(
            10, 10, 1.0, 0.0, 0.5, xy.data(), xz.data(), zy.data(), zz.data(), bk_of(10), &fl);
        CHECK(c.t_xi == 2.5 && c.t_zeta == 3.0 && fl == 0,
              "class(1,0) dt=0.5: got t_xi=%.17g t_zeta=%.17g fl=%u", c.t_xi, c.t_zeta, fl);
    }
    {   // class 3 (0,+1): contravariant (0,1) → t_xi = 3, t_zeta = (k-dt)-bk = 2.5
        GILBM2_DepartCoords c = gilbm2_gen_departure_coords(
            10, 10, 0.0, 1.0, 0.5, xy.data(), xz.data(), zy.data(), zz.data(), bk_of(10), &fl);
        CHECK(c.t_xi == 3.0 && c.t_zeta == 2.5 && fl == 0,
              "class(0,1) dt=0.5: got t_xi=%.17g t_zeta=%.17g fl=%u", c.t_xi, c.t_zeta, fl);
    }

    // ── §9-5: clamp behavior + the t_zeta asymmetry ──
    {   // t_xi clamp: huge dt on (+1,0) → 3-10 = -7 → clamp 0, flag TXI
        GILBM2_DepartCoords c = gilbm2_gen_departure_coords(
            10, 10, 1.0, 0.0, 10.0, xy.data(), xz.data(), zy.data(), zz.data(), bk_of(10), &fl);
        CHECK(c.t_xi == 0.0 && (fl & GILBM2_FLAG_TXI_CLAMPED),
              "t_xi clamp: got t_xi=%.17g fl=%u", c.t_xi, fl);
    }
    {   // ★ asymmetry: huge dt on (0,+1) at k=10 (bk=7): up_k=10-400 → clamp 3
        //   → t_zeta = 3-7 = -4 EXACTLY — must NOT be re-clamped into [0,6]
        GILBM2_DepartCoords c = gilbm2_gen_departure_coords(
            10, 10, 0.0, 1.0, 400.0, xy.data(), xz.data(), zy.data(), zz.data(), bk_of(10), &fl);
        CHECK(c.t_zeta == -4.0 && (fl & GILBM2_FLAG_UPK_CLAMPED),
              "t_zeta asymmetry: got t_zeta=%.17g fl=%u (a [0,6] clamp here is a BUG)", c.t_zeta, fl);
    }
    {   // near-wall k=3 (bk=0), (0,+1) dt=0.5: up_k=2.5 → clamp 3 → t_zeta=3, flag
        GILBM2_DepartCoords c = gilbm2_gen_departure_coords(
            3, 3, 0.0, 1.0, 0.5, xy.data(), xz.data(), zy.data(), zz.data(), bk_of(3), &fl);
        CHECK(c.t_zeta == 3.0 && (fl & GILBM2_FLAG_UPK_CLAMPED),
              "near-wall k=3: got t_zeta=%.17g fl=%u", c.t_zeta, fl);
    }
    {   // folded production path: raw L_zeta ghost terms fold into one physical k window
        const int j = 10, k = 3;
        const int bk = bk_of(k);
        GILBM2_DepartCoords c = gilbm2_gen_departure_coords(
            j, k, 0.0, -1.0, 0.5, xy.data(), xz.data(), zy.data(), zz.data(), bk, &fl);
        double Lxi[7], Lzeta[7], ws_eff[7];
        int k_idx[7];
        gilbm2_lagrange7(c.t_xi, Lxi);
        gilbm2_lagrange7(c.t_zeta, Lzeta);
        gilbm2_fold_zeta_ghost(bk, Lzeta, k_idx, ws_eff);

        GILBM2_DepartWeightsFolded f = gilbm2_gen_departure_weights_folded(
            j, k, 0.0, -1.0, 0.5, xy.data(), xz.data(), zy.data(), zz.data(), bk);
        CHECK(f.j0 == j - 3, "folded j0 got %d want %d", f.j0, j - 3);
        CHECK(f.k_idx[0] >= 3 && f.k_idx[6] <= (int)NZ6 - 4,
              "folded k_idx window out of physical bounds: %d..%d", f.k_idx[0], f.k_idx[6]);
        for (int s = 1; s < 7; s++)
            CHECK(f.k_idx[s] == f.k_idx[0] + s, "folded k_idx not contiguous at s=%d", s);
        double sum_ws = 0.0;
        for (int s = 0; s < 7; s++) {
            sum_ws += f.ws[s];
            CHECK(f.k_idx[s] == k_idx[s], "folded k_idx[%d] got %d want %d", s, f.k_idx[s], k_idx[s]);
            CHECK(fabs(f.wr[s] - Lxi[s]) <= 1.0e-12, "folded wr[%d] mismatch", s);
            CHECK(fabs(f.ws[s] - ws_eff[s]) <= 1.0e-12, "folded ws[%d] mismatch", s);
        }
        CHECK(fabs(sum_ws - 1.0) <= 1.0e-12,
              "folded sum(ws)-1 = %.17e", sum_ws - 1.0);
    }

    // ── host build loop: entry == direct generator; class-0/ghost rows inert ──
    {
        std::vector<int> bk_tab(NZ6);
        for (int k = 0; k < (int)NZ6; k++) bk_tab[k] = bk_of(k);
        std::vector<GILBM2_DepartCoords> table(N);
        BuildGILBM2DepartureTableHost_Coords(table.data(),
            xy.data(), xz.data(), zy.data(), zz.data(), bk_tab.data(), 0.5);

        GILBM2_DepartCoords d = gilbm2_gen_departure_coords(
            10, 10, 1.0, 0.0, 0.5, xy.data(), xz.data(), zy.data(), zz.data(), bk_tab[10]);
        const GILBM2_DepartCoords &t = table[gilbm2_coord_index(1, 10, 10)];
        CHECK(memcmp(&t, &d, sizeof(d)) == 0, "table[cls1,10,10] != direct generator");

        const GILBM2_DepartCoords &z0 = table[gilbm2_coord_index(0, 10, 10)];
        CHECK(z0.t_xi == 3.0 && z0.t_zeta == 3.0, "class-0 entry must stay inert");
        const GILBM2_DepartCoords &g0 = table[gilbm2_coord_index(1, 0, 0)];
        CHECK(g0.t_xi == 3.0 && g0.t_zeta == 3.0, "ghost-row entry must stay inert");
    }
    {   // generic host build uses the production GILBM2_Table alias: WEIGHTS_FOLDED
        std::vector<int> bk_tab(NZ6);
        for (int k = 0; k < (int)NZ6; k++) bk_tab[k] = bk_of(k);
        std::vector<GILBM2_Table> table(N);
        BuildGILBM2DepartureTableHost(table.data(),
            xy.data(), xz.data(), zy.data(), zz.data(), bk_tab.data(), 0.5);

        const GILBM2_Table &f = table[gilbm2_coord_index(4, 10, 3)];
        CHECK(f.k_idx[0] >= 3 && f.k_idx[6] <= (int)NZ6 - 4,
              "host folded k_idx window out of physical bounds");
        for (int s = 1; s < 7; s++)
            CHECK(f.k_idx[s] == f.k_idx[0] + s, "host folded k_idx not contiguous at s=%d", s);
    }

    if (g_fail == 0) printf("ALGO2 UNIT TESTS: ALL PASS\n");
    else             printf("ALGO2 UNIT TESTS: %d FAILURE(S)\n", g_fail);
    return g_fail;
}
