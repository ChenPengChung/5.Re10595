#ifndef GILBM_PRECOMPUTE2_H
#define GILBM_PRECOMPUTE2_H
// ════════════════════════════════════════════════════════════════════════════
//  Algorithm2 precompute — ROUND A: GILBM departure-coordinate GENERATOR
// ────────────────────────────────────────────────────────────────────────────
//  Reproduces, EXACTLY, the departure coordinate (t_xi, t_zeta) that Algorithm1
//  computes inside the fused kernel, but ONCE per (yz_class, j, k) instead of
//  per (i,j,k,q,step). Time-invariant under GTS: depends only on
//  (j, k, e_y, e_z, dt_global, the 4 fixed metric arrays, bk_precomp).
//
//  Faithful transcription of:
//    gilbm/evolution_gilbm/1.algorithm1.h
//      - gilbm_rk2_displacement()  (L16-58)   → (d_xi, delta_zeta)
//      - t_xi / t_zeta derivation  (L304-321) → (t_xi, t_zeta)
//
//  STRICT STORAGE: the production table is exactly NCLASS*NYD6*NZ6 * 2 doubles
//  (the "2*9*grid" target) — GILBM2_DepartCoords is 16 bytes, NO flags field.
//  Clamp diagnostics are reported via an OPTIONAL out-param, never stored.
//
//  CRITICAL asymmetry preserved: t_xi IS clamped to [0,6]; t_zeta is
//  up_k(clamped[3,NZ6-4]) - bk and is NOT clamped to [0,6].
//
//  Lagrange: a single local hardcoded-denominator helper (gilbm2_lagrange7),
//  an EXACT copy of interpolation_gilbm.h:117, used by BOTH host and device:
//    - DEVICE: byte-identical to Algorithm1's streaming-kernel lagrange →
//      running the table build as a one-time DEVICE kernel gives diff=0.
//    - HOST:   identical source; differs from device only by optional FMA
//      contraction (~1 ULP), used for diagnostics/unit tests. For strict
//      diff=0, build via the DEVICE kernel path.
//
//  SELF-CONTAINED: no #include of precompute.h (which pulls MPI/CHECK_MPI), so
//  this header is unit-testable standalone with g++. NYD6/NZ6/jp must be
//  provided by the includer (variables.h); bk is passed in (no dependency on
//  PrecomputeGILBM_StencilBaseK here).
//
//  SCOPE (Round A): the 9-class map, the COORDS table struct + indexing, the
//  generator, and the HOST build loop. The COORDS consumer kernel, the WEIGHTS
//  fold, the ITB_NEWTON generator, the Algorithm1 coordinate-extraction hook,
//  and all main.cu/evolution.h wiring are LATER rounds.
// ════════════════════════════════════════════════════════════════════════════

#include <cmath>
#include <cstddef>   // size_t

// ── 9 y-z projection classes (identical grouping to Algorithm1's GILBM_e) ──
//   0:( 0, 0)  1:(+1, 0)  2:(-1, 0)  3:( 0,+1)  4:( 0,-1)
//   5:(+1,+1)  6:(-1,+1)  7:(+1,-1)  8:(-1,-1)
#define GILBM2_NCLASS 9

// ── factorial STORE mode (testing-1: precompute r,s vs precompute weights) ──
//   COORDS  : 表存 (t_xi,t_zeta), kernel 即時 lagrange 算 L_xi/L_zeta   (GILBM-B)
//   WEIGHTS : 表存 L_xi[7]/L_zeta[7] (= 同一 RK2→lagrange 路徑算好), kernel 純讀 (GILBM-A, 仿 ITB)
//   兩模式共用下游 (f-gather/ghost/MAC/per-q 結構) → 唯一差別 = compute vs read weights。
//   build_cell.sh 可 -DGILBM_ALGO2_STORE=1 覆寫; #ifndef 讓 precompute2.h 仍可 standalone 編。
#define GILBM2_STORE_COORDS         0
#define GILBM2_STORE_WEIGHTS        1
#define GILBM2_STORE_WEIGHTS_FOLDED 2   // ITB-style: ghost 折進權重, consumer 純 flat MAC (對標 ITBLBM 3.50ms)
#ifndef GILBM_ALGO2_STORE
#define GILBM_ALGO2_STORE GILBM2_STORE_COORDS
#endif

// q → yz class. q=0 self, q=1,2 (ey=ez=0) eta-only 1D → class 0 (inert).
// Matches the (e_y,e_z) of GILBM_e[19][3] (0.shared_code.h:23) byte-for-byte.
__host__ __device__ __forceinline__ int gilbm2_yz_class_from_q(int q) {
    switch (q) {
        case 3: case 7:  case 8:   return 1;  // (+1, 0): q3 + xy-diagonals 7,8
        case 4: case 9:  case 10:  return 2;  // (-1, 0)
        case 5: case 11: case 12:  return 3;  // ( 0,+1)
        case 6: case 13: case 14:  return 4;  // ( 0,-1)
        case 15:                   return 5;  // (+1,+1)
        case 16:                   return 6;  // (-1,+1)
        case 17:                   return 7;  // (+1,-1)
        case 18:                   return 8;  // (-1,-1)
        default:                   return 0;  // q=0,1,2 → inert (0,0)
    }
}

__host__ __device__ __forceinline__ void gilbm2_class_velocity(int cls, double *ey, double *ez) {
    // 9-class (e_y, e_z) table
    switch (cls) {
        case 1: *ey =  1.0; *ez =  0.0; return;
        case 2: *ey = -1.0; *ez =  0.0; return;
        case 3: *ey =  0.0; *ez =  1.0; return;
        case 4: *ey =  0.0; *ez = -1.0; return;
        case 5: *ey =  1.0; *ez =  1.0; return;
        case 6: *ey = -1.0; *ez =  1.0; return;
        case 7: *ey =  1.0; *ez = -1.0; return;
        case 8: *ey = -1.0; *ez = -1.0; return;
        default: *ey = 0.0; *ez = 0.0; return;   // class 0 (inert)
    }
}

// ── COORDS table (STORE = COORDS / cell "B") — STRICTLY 2 doubles = 16 bytes/entry ──
//   Table footprint = NCLASS * NYD6 * NZ6 * 2 doubles (the "2*9*grid" target).
//   NO flags field in the production struct (a flags byte would pad it to 24 B).
struct GILBM2_DepartCoords {
    double t_xi;     // ∈ [0,6] (clamped)   — xi-direction local Lagrange coordinate
    double t_zeta;   // = up_k - bk; NOT clamped to [0,6] (Algorithm1 asymmetry)
};

// Diagnostic clamp flags — NOT stored in the table; reported only via the
// generator's optional flag_out parameter (used by validation/unit tests).
#define GILBM2_FLAG_TXI_CLAMPED   0x01u   // t_xi hit the [0,6] clamp
#define GILBM2_FLAG_UPK_CLAMPED   0x02u   // up_k hit the [3,NZ6-4] clamp

// ── WEIGHTS table (STORE = WEIGHTS / cell "A", ITB-style) — 14 doubles = 112 B/entry ──
//   存 RAW L_xi/L_zeta (= COORDS 模式 kernel 會即時算的同一組), NO ghost fold
//   (下游 ghost 仍在 kernel, 與 COORDS 完全一致 → 公平比較)。t_zeta 不存
//   (USE_WENO7=0 下 zeta-collapse 線性路徑不用 t_zeta)。
struct GILBM2_DepartWeights {
    double wr[7];    // L_xi   (= lagrange_7point_coeffs(t_xi))
    double ws[7];    // L_zeta (= lagrange_7point_coeffs(t_zeta))
};

// ── FOLDED table (STORE = WEIGHTS_FOLDED / cell "A-fast", 仿 ITB) — 144 B/entry ──
//   ζ 方向 ghost 外插「預先折進」連續 physical k_idx/ws_eff → consumer 純 flat MAC,
//   不再有 interp2/ghost_extrapolate/zeta_collapse (對標 ITBLBM 的 2.37ms Interior)。
//   ξ(wr=L_xi) 與 η(L_eta_shared) 無 ghost, 不折。j0 = j-3 (ξ stencil base)。
struct GILBM2_DepartWeightsFolded {
    int    j0;          // ξ (j) stencil base = j-3
    int    k_idx[7];    // ζ (k) 絕對索引: ITB-style 連續 7 點 physical window
    double wr[7];       // ξ weights = L_xi (不折)
    double ws[7];       // ζ folded weights = ws_eff (ghost 已折入 interior)
};

// ── unified table type: 同一個型別名貫穿 struct/alloc/kernel-param/validator ──
#if   GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS_FOLDED
typedef GILBM2_DepartWeightsFolded GILBM2_Table;
#elif GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS
typedef GILBM2_DepartWeights GILBM2_Table;
#else
typedef GILBM2_DepartCoords  GILBM2_Table;
#endif

// Per-rank table index: [class][j][k], j∈[0,NYD6), k∈[0,NZ6).
__host__ __device__ __forceinline__ size_t gilbm2_coord_index(int cls, int j, int k) {
    return ((size_t)cls * (size_t)NYD6 + (size_t)j) * (size_t)NZ6 + (size_t)k;
}

// ── 7-point Lagrange — EXACT copy of interpolation_gilbm.h:117 ──
//   Hardcoded denominators, division-free (performance-frozen 2026-04).
//   Duplicated locally so this header is self-contained AND host & device use
//   the IDENTICAL expression. KEEP IN SYNC with interpolation_gilbm.h:117.
__host__ __device__ __forceinline__ void gilbm2_lagrange7(double t, double a[7]) {
    const double t0 = t, t1 = t - 1.0, t2 = t - 2.0, t3 = t - 3.0;
    const double t4 = t - 4.0, t5 = t - 5.0, t6 = t - 6.0;
    const double p56     = t5 * t6;
    const double p456    = t4 * p56;
    const double p3456   = t3 * p456;
    const double p23456  = t2 * p3456;
    const double p123456 = t1 * p23456;   // t1*t2*t3*t4*t5*t6
    const double p01     = t0 * t1;
    const double p012    = p01 * t2;
    const double p0123   = p012 * t3;
    const double p01234  = p0123 * t4;
    const double p012345 = p01234 * t5;   // t0*t1*t2*t3*t4*t5
    a[0] = p123456        * ( 1.0 / 720.0);   // skip t0
    a[1] = (t0 * p23456)  * (-1.0 / 120.0);   // skip t1
    a[2] = (p01 * p3456)  * ( 1.0 /  48.0);   // skip t2
    a[3] = (p012 * p456)  * (-1.0 /  36.0);   // skip t3
    a[4] = (p0123 * p56)  * ( 1.0 /  48.0);   // skip t4
    a[5] = (p01234 * t6)  * (-1.0 / 120.0);   // skip t5
    a[6] = p012345        * ( 1.0 / 720.0);   // skip t6
}

// ════════════════════════════════════════════════════════════════════════════
//  GILBM departure-coordinate generator (the 12-step recipe, §4 of the prompt)
//  Mirrors gilbm_rk2_displacement + the t_xi/t_zeta derivation EXACTLY.
//    in : (j,k) cell, (ey,ez) class velocity, dt, 4 metric arrays [NYD6*NZ6],
//         bk = bk_precomp[k]
//    out: (t_xi, t_zeta) returned; clamp flags via optional flag_out (NOT stored)
// ════════════════════════════════════════════════════════════════════════════
__host__ __device__ inline GILBM2_DepartCoords gilbm2_gen_departure_coords(
    int j, int k, double ey, double ez, double dt_val,
    const double *xi_y, const double *xi_z,
    const double *zeta_y, const double *zeta_z,
    int bk, unsigned char *flag_out = nullptr)   // flag_out: optional diagnostic, NOT stored
{
    const int idx_jk = j * (int)NZ6 + k;
    const double xi_y_val   = xi_y[idx_jk];
    const double xi_z_val   = xi_z[idx_jk];
    const double zeta_y_val = zeta_y[idx_jk];
    const double zeta_z_val = zeta_z[idx_jk];

    // ── RK2 midpoint (Imamura 2005 Eq.19-20) == gilbm_rk2_displacement L23-58 ──
    double e_txi_0   = ey * xi_y_val   + ez * xi_z_val;
    double e_tzeta_0 = ey * zeta_y_val + ez * zeta_z_val;
    double j_half = (double)j - 0.5 * dt_val * e_txi_0;
    double k_half = (double)k - 0.5 * dt_val * e_tzeta_0;
    if (j_half < 0.0)                       j_half = 0.0;
    if (j_half > (double)((int)NYD6 - 1))   j_half = (double)((int)NYD6 - 1);
    if (k_half < 3.0)                       k_half = 3.0;
    if (k_half > (double)((int)NZ6 - 4))    k_half = (double)((int)NZ6 - 4);

    int sj_rk = (int)floor(j_half) - 3;
    if (sj_rk < 0)                  sj_rk = 0;
    if (sj_rk + 6 > (int)NYD6 - 1)  sj_rk = (int)NYD6 - 7;
    double tj_rk = j_half - (double)sj_rk;
    double aj_rk[7];
    gilbm2_lagrange7(tj_rk, aj_rk);

    int sk_rk = (int)floor(k_half) - 3;
    if (sk_rk < 0)                 sk_rk = 0;
    if (sk_rk + 6 > (int)NZ6 - 1)  sk_rk = (int)NZ6 - 7;
    double tk_rk = k_half - (double)sk_rk;
    double ak_rk[7];
    gilbm2_lagrange7(tk_rk, ak_rk);

    double e_txi_half = 0.0, e_tzeta_half = 0.0;
    for (int mj = 0; mj < 7; mj++) {
        int jj = sj_rk + mj;
        double acc_xi = 0.0, acc_zeta = 0.0;
        for (int mk = 0; mk < 7; mk++) {
            int kk = sk_rk + mk;
            int idx_rk = jj * (int)NZ6 + kk;
            double w_mk = ak_rk[mk];
            acc_xi   += w_mk * (ey * xi_y[idx_rk]   + ez * xi_z[idx_rk]);
            acc_zeta += w_mk * (ey * zeta_y[idx_rk] + ez * zeta_z[idx_rk]);
        }
        e_txi_half   += aj_rk[mj] * acc_xi;
        e_tzeta_half += aj_rk[mj] * acc_zeta;
    }
    double d_xi       = dt_val * e_txi_half;
    double delta_zeta = dt_val * e_tzeta_half;

    // ── t_xi / t_zeta derivation == algorithm1_step1_GTS L310-321 ──
    //   bj = j-3 (UNCLAMPED, L187) → (j - bj) = 3 → t_xi = 3 - d_xi
    unsigned char fl = 0;

    double t_xi = 3.0 - d_xi;
    if (t_xi < 0.0) { t_xi = 0.0; fl |= GILBM2_FLAG_TXI_CLAMPED; }
    if (t_xi > 6.0) { t_xi = 6.0; fl |= GILBM2_FLAG_TXI_CLAMPED; }

    double up_k = (double)k - delta_zeta;
    if (up_k < 3.0)                     { up_k = 3.0;                     fl |= GILBM2_FLAG_UPK_CLAMPED; }
    if (up_k > (double)((int)NZ6 - 4))  { up_k = (double)((int)NZ6 - 4);  fl |= GILBM2_FLAG_UPK_CLAMPED; }
    double t_zeta = up_k - (double)bk;   // ★ NO [0,6] clamp — preserve Algorithm1 asymmetry

    if (flag_out) *flag_out = fl;
    GILBM2_DepartCoords c;
    c.t_xi   = t_xi;
    c.t_zeta = t_zeta;
    return c;
}

// ── WEIGHTS generator: 同一 RK2→(t_xi,t_zeta) 路徑, 再 lagrange 折成權重 ──
//   = COORDS 生成器 + COORDS 模式 kernel 會做的兩次 lagrange, 預先算好。
__host__ __device__ inline GILBM2_DepartWeights gilbm2_gen_departure_weights(
    int j, int k, double ey, double ez, double dt_val,
    const double *xi_y, const double *xi_z,
    const double *zeta_y, const double *zeta_z,
    int bk, unsigned char *flag_out = nullptr)
{
    GILBM2_DepartCoords c = gilbm2_gen_departure_coords(
        j, k, ey, ez, dt_val, xi_y, xi_z, zeta_y, zeta_z, bk, flag_out);
    GILBM2_DepartWeights w;
    gilbm2_lagrange7(c.t_xi,   w.wr);
    gilbm2_lagrange7(c.t_zeta, w.ws);
    return w;
}

// ── ζ ghost 折疊 (逐項對齊 1.algorithm1.h:67 gilbm_ghost_zone_extrapolate) ──
//   raw_bk = bk_precomp[k] 是 Algorithm1 使用的原始 ζ stencil base。
//   k_idx/ws_eff 採 ITB 同款表示: 先選連續 7 點 physical window, 再把 raw
//   ghost 權重折入該 window。這避免近壁 table 出現重複 k_idx + 乘零項。
//   coeff 同 GHOST_EXTRAP_ORDER (預設 2=quadratic 3-point; 3=cubic 4-point)。
__host__ __device__ inline void gilbm2_fold_zeta_ghost(
    int raw_bk, const double L_zeta[7], int k_idx[7], double ws_eff[7])
{
    int phys_bk = raw_bk;
    if (phys_bk < 3) phys_bk = 3;
    if (phys_bk > (int)NZ6 - 10) phys_bk = (int)NZ6 - 10;

    for (int s = 0; s < 7; s++) {
        k_idx[s] = phys_bk + s;
        ws_eff[s] = 0.0;
    }

    for (int s = 0; s < 7; s++) {
        const int kg = raw_bk + s;
        const double w = L_zeta[s];
        if (kg < 3) {
            const double d = (double)(3 - kg);
#if GHOST_EXTRAP_ORDER >= 3
            const double d1 = d + 1.0, d2 = d + 2.0, d3 = d + 3.0;
            ws_eff[3 - phys_bk] += w * ( d1 * d2 * d3 / 6.0);
            ws_eff[4 - phys_bk] += w * (-d  * d2 * d3 / 2.0);
            ws_eff[5 - phys_bk] += w * ( d  * d1 * d3 / 2.0);
            ws_eff[6 - phys_bk] += w * (-d  * d1 * d2 / 6.0);
#else
            ws_eff[3 - phys_bk] += w * ((d + 1.0) * (d + 2.0) * 0.5);
            ws_eff[4 - phys_bk] += w * (-d * (d + 2.0));
            ws_eff[5 - phys_bk] += w * (d * (d + 1.0) * 0.5);
#endif
        } else if (kg > (int)NZ6 - 4) {
            const double d = (double)(kg - ((int)NZ6 - 4));
#if GHOST_EXTRAP_ORDER >= 3
            const double d1 = d + 1.0, d2 = d + 2.0, d3 = d + 3.0;
            ws_eff[((int)NZ6 - 4) - phys_bk] += w * ( d1 * d2 * d3 / 6.0);
            ws_eff[((int)NZ6 - 5) - phys_bk] += w * (-d  * d2 * d3 / 2.0);
            ws_eff[((int)NZ6 - 6) - phys_bk] += w * ( d  * d1 * d3 / 2.0);
            ws_eff[((int)NZ6 - 7) - phys_bk] += w * (-d  * d1 * d2 / 6.0);
#else
            ws_eff[((int)NZ6 - 4) - phys_bk] += w * ((d + 1.0) * (d + 2.0) * 0.5);
            ws_eff[((int)NZ6 - 5) - phys_bk] += w * (-d * (d + 2.0));
            ws_eff[((int)NZ6 - 6) - phys_bk] += w * (d * (d + 1.0) * 0.5);
#endif
        } else {
            ws_eff[kg - phys_bk] += w;
        }
    }
}

// ── FOLDED generator: RK2→(t_xi,t_zeta)→lagrange→fold ghost into ws_eff + 絕對 k_idx ──
__host__ __device__ inline GILBM2_DepartWeightsFolded gilbm2_gen_departure_weights_folded(
    int j, int k, double ey, double ez, double dt_val,
    const double *xi_y, const double *xi_z,
    const double *zeta_y, const double *zeta_z, int bk)
{
    GILBM2_DepartCoords c = gilbm2_gen_departure_coords(
        j, k, ey, ez, dt_val, xi_y, xi_z, zeta_y, zeta_z, bk);
    double L_xi[7], L_zeta[7];
    gilbm2_lagrange7(c.t_xi,   L_xi);
    gilbm2_lagrange7(c.t_zeta, L_zeta);
    GILBM2_DepartWeightsFolded f;
    f.j0 = j - 3;                                  // ξ stencil base (= bj, 同 legacy)
    gilbm2_fold_zeta_ghost(bk, L_zeta, f.k_idx, f.ws);
    for (int s = 0; s < 7; s++) {
        f.wr[s] = L_xi[s];
    }
    return f;
}

// ── mode-generic table-entry generator + inert default ──
//   build / reference kernels 都呼叫這兩個, 故 STORE 切換只需改一處。
__host__ __device__ inline GILBM2_Table gilbm2_gen_table_entry(
    int j, int k, double ey, double ez, double dt_val,
    const double *xi_y, const double *xi_z,
    const double *zeta_y, const double *zeta_z,
    int bk)
{
#if   GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS_FOLDED
    return gilbm2_gen_departure_weights_folded(j, k, ey, ez, dt_val, xi_y, xi_z, zeta_y, zeta_z, bk);
#elif GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS
    return gilbm2_gen_departure_weights(j, k, ey, ez, dt_val, xi_y, xi_z, zeta_y, zeta_z, bk);
#else
    return gilbm2_gen_departure_coords (j, k, ey, ez, dt_val, xi_y, xi_z, zeta_y, zeta_z, bk);
#endif
}

// inert default for class-0 / ghost rows (never streamed; build & ref 用同一份 → bitwise 一致)
__host__ __device__ inline GILBM2_Table gilbm2_inert_entry()
{
    GILBM2_Table e;
#if   GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS_FOLDED
    e.j0 = 0;
    gilbm2_lagrange7(3.0, e.wr);   // 任意一致值; 不被消費
    gilbm2_lagrange7(3.0, e.ws);
    for (int s = 0; s < 7; s++) e.k_idx[s] = 3;
#elif GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS
    gilbm2_lagrange7(3.0, e.wr);   // 任意一致值; 不被消費
    gilbm2_lagrange7(3.0, e.ws);
#else
    e.t_xi = 3.0; e.t_zeta = 3.0;
#endif
    return e;
}

// ── HOST build loop (mode-generic GILBM2_Table) — diagnostic / 1e-12 比對用 ──
//   時序前置同 COORDS 版: 須在 metric MPI exchange + dt_global Allreduce + bk_precomp 之後。
static inline void BuildGILBM2DepartureTableHost(
    GILBM2_Table *table,
    const double *xi_y_h,  const double *xi_z_h,
    const double *zeta_y_h, const double *zeta_z_h,
    const int *bk_precomp_h, double dt_val)
{
    const size_t N = (size_t)GILBM2_NCLASS * (size_t)NYD6 * (size_t)NZ6;
    const GILBM2_Table inert = gilbm2_inert_entry();
    for (size_t n = 0; n < N; n++) table[n] = inert;
    for (int cls = 1; cls < GILBM2_NCLASS; cls++) {
        double ey, ez;
        gilbm2_class_velocity(cls, &ey, &ez);
        for (int j = 3; j < (int)NYD6 - 3; j++) {
            for (int k = 3; k < (int)NZ6 - 3; k++) {
                table[gilbm2_coord_index(cls, j, k)] =
                    gilbm2_gen_table_entry(j, k, ey, ez, dt_val,
                        xi_y_h, xi_z_h, zeta_y_h, zeta_z_h, bk_precomp_h[k]);
            }
        }
    }
}

// ════════════════════════════════════════════════════════════════════════════
//  HOST build loop — fill the COORDS table for all (class, interior j, k).
//  MUST be called AFTER: (1) the metric MPI ghost-exchange (so ghost rows of
//  xi_y_h.. are valid — the RK2 7×7 stencil reaches them near the j-seam), and
//  (2) dt_global finalization (MPI_Allreduce), and (3) PrecomputeGILBM_StencilBaseK.
//  Interior range matches the kernel guard: j∈[3,NYD6-4], k∈[3,NZ6-4].
//  (Diagnostic/host path — for bit-exact, run the equivalent DEVICE kernel.)
// ════════════════════════════════════════════════════════════════════════════
static inline void BuildGILBM2DepartureTableHost_Coords(
    GILBM2_DepartCoords *table,                 // [GILBM2_NCLASS * NYD6 * NZ6]
    const double *xi_y_h,  const double *xi_z_h,
    const double *zeta_y_h, const double *zeta_z_h,
    const int *bk_precomp_h, double dt_val)
{
    const size_t N = (size_t)GILBM2_NCLASS * (size_t)NYD6 * (size_t)NZ6;
    for (size_t n = 0; n < N; n++) {            // inert default (class 0 + ghost/boundary)
        table[n].t_xi = 3.0; table[n].t_zeta = 3.0;
    }
    for (int cls = 1; cls < GILBM2_NCLASS; cls++) {   // class 0 = (0,0) inert
        double ey, ez;
        gilbm2_class_velocity(cls, &ey, &ez);
        for (int j = 3; j < (int)NYD6 - 3; j++) {     // j ∈ [3, NYD6-4]
            for (int k = 3; k < (int)NZ6 - 3; k++) {  // k ∈ [3, NZ6-4]
                int bk = bk_precomp_h[k];
                table[gilbm2_coord_index(cls, j, k)] =
                    gilbm2_gen_departure_coords(j, k, ey, ez, dt_val,
                        xi_y_h, xi_z_h, zeta_y_h, zeta_z_h, bk);
            }
        }
    }
}

#endif // GILBM_PRECOMPUTE2_H
