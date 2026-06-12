#ifndef ALGORITHM2_H
#define ALGORITHM2_H
// ════════════════════════════════════════════════════════════════════════════
//  Algorithm2 — ROUND B: COORDS consumer kernel + device table build +
//                Algorithm1-path reference dump + validation comparator
// ────────────────────────────────────────────────────────────────────────────
//  GEN = GILBM_RK2, STORE = COORDS (factorial cell "GILBM-B").
//
//  algorithm2_step1_GTS is a LINE-PARALLEL copy of algorithm1_step1_GTS
//  (1.algorithm1.h:166-430). The ONLY numerical change: the per-q in-kernel
//  departure computation —
//      gilbm_rk2_displacement(...)            (1.algorithm1.h:302-307)
//      t_xi  = (j-bj) - d_xi, clamp [0,6]     (1.algorithm1.h:309-313)
//      up_k  = k - delta_zeta, clamp [3,NZ6-4]
//      t_zeta = up_k - bk                     (1.algorithm1.h:315-321)
//  — is replaced by ONE table read:
//      dc = coords_d[gilbm2_coord_index(gilbm2_yz_class_from_q(q), j, k)]
//  followed by the SAME device lagrange_7point_coeffs calls. The table stores
//  the post-clamp values, so no clamping is repeated here. Everything else
//  (q=0 self-read, q=1,2 eta-only 1D path, wall-BC branch BEFORE streaming,
//  2D/3D interpolation loops, ghost extrapolation, zeta collapse, mass
//  correction, Guo half-force, collision, writes) is copied verbatim.
//
//  Wall-BC guard: the need_bc branch is checked BEFORE any table use, exactly
//  as Algorithm1 checks it before RK2 — wall-row table entries (k=3, NZ6-4)
//  for BC directions are never consumed (codex Round-A caveat honoured).
//
//  Bit-exactness: with the table built by Algorithm2_BuildCoordsTable_Device
//  (same __constant__ GILBM_dt, same device lagrange, same metric arrays),
//  the streamed f is bit-identical to Algorithm1 for every non-BC direction.
//
//  §7a reference extraction (NO Algorithm1 modification): Algorithm1 never
//  stores t_xi/t_zeta — it consumes them inline. Algorithm2_RefCoords_Algo1Path
//  calls the ORIGINAL __device__ gilbm_rk2_displacement (1.algorithm1.h:16)
//  and replicates L304-321 verbatim, dumping per-(class,j,k) coordinates.
//  Comparing it against the generator-built table is a true cross-
//  implementation check (original function vs transcription): diff must be 0.
//
//  SCOPE (Round B): this header only. No wiring — variables.h flags,
//  memory.h alloc, main.cu calls, evolution.h dispatch are Round C. The smem
//  interior variant stays Algorithm1-only (prompt §6; USE_SMEM_INTERIOR=0).
//
//  INCLUDE ORDER: after 1.algorithm1.h (needs gilbm_rk2_displacement,
//  gilbm_ghost_zone_extrapolate, gilbm_zeta_collapse, NeedsBoundaryCondition,
//  ChapmanEnskogBC, gilbm_collision_GTS, GILBM_e, GILBM_L_eta_shared,
//  lagrange_7point_coeffs, GILBM_dt) and after ../precompute2.h (struct,
//  class map, generator).
// ════════════════════════════════════════════════════════════════════════════

#ifndef ALGORITHM1_H
#error "2.algorithm2.h must be included AFTER 1.algorithm1.h"
#endif
#ifndef GILBM_PRECOMPUTE2_H
#error "2.algorithm2.h must be included AFTER gilbm/precompute2.h"
#endif

#include <cstdio>    // printf, fprintf (§B5 validation report)
#include <cstdlib>   // malloc, free
#include <cstring>   // memcmp (bitwise comparison)

// ════════════════════════════════════════════════════════════════════════════
//  §B1  COORDS consumer — line-parallel twin of algorithm1_step1_GTS
//  Signature = Algorithm1's + coords_d appended. xi_y_d/xi_z_d are KEPT for
//  launch-site signature parity but are UNUSED here (RK2 hoisted to table).
// ════════════════════════════════════════════════════════════════════════════
__device__ void algorithm2_step1_GTS(
    int i, int j, int k,
    const double *f_post_read,   // [19 * GRID_SIZE] — 碰後分佈 (input, 上一步)
    double *f_post_write,        // [19 * GRID_SIZE] — 碰後分佈 (output, 本步)
    const double *zeta_z_d, const double *zeta_y_d,
    const double *xi_y_d,   const double *xi_z_d,   // unused (signature parity; RK2 已預計算)
    const int *bk_precomp_d,
    const double *z_zeta_d, // ∂z/∂ζ for stretch_factor (WENO7 only, NULL when USE_WENO7=0)
    const double *u_bc, const double *v_bc, const double *w_bc, const double *rho_bc,
    double *u_out, double *v_out, double *w_out, double *rho_out_arr,
    double *rho_modify,
    const double *Force,     // body force (streamwise, device pointer)
    const GILBM2_Table * __restrict__ table_d  // ★ Algorithm2: 預計算 departure 表 (COORDS 或 WEIGHTS)
) {
    const int nface = NX6 * NZ6;
    const int index = j * nface + k * NX6 + i;
    const int idx_jk = j * NZ6 + k;

    // ★ GTS: 從 __constant__ 載入 register（全域統一值）
    const double dt_global    = GILBM_dt;
    const double omega_global = GILBM_omega_global;

    const int bi = i - 3;
    const int bk = bk_precomp_d[k];
#if GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS_FOLDED
    (void)bk;   // FOLDED consumer 用 c.k_idx (絕對索引), 不再用 bk
#endif

    // ── Wall BC pre-computation (6th-order one-sided FD) ──
    bool is_bottom = (k == 3);
    bool is_top    = (k == NZ6 - 4);
    double zeta_y_val = zeta_y_d[idx_jk];
    double zeta_z_val = zeta_z_d[idx_jk];

    // ── Wall BC: 6th-order one-sided FD for velocity gradient ──
    //   讀 read-only macro snapshot (前一步值) — 與 Algorithm1 相同
    double rho_wall = 0.0, du_dk = 0.0, dv_dk = 0.0, dw_dk = 0.0;
    if (is_bottom) {
        int idx3 = j * nface + 4 * NX6 + i;
        int idx4 = j * nface + 5 * NX6 + i;
        int idx5 = j * nface + 6 * NX6 + i;
        int idx6 = j * nface + 7 * NX6 + i;
        int idx7 = j * nface + 8 * NX6 + i;
        int idx8 = j * nface + 9 * NX6 + i;
        double u3 = u_bc[idx3], u4 = u_bc[idx4], u5 = u_bc[idx5], u6 = u_bc[idx6];
        double u7 = u_bc[idx7], u8 = u_bc[idx8];
        double v3 = v_bc[idx3], v4 = v_bc[idx4], v5 = v_bc[idx5], v6 = v_bc[idx6];
        double v7 = v_bc[idx7], v8 = v_bc[idx8];
        double w3 = w_bc[idx3], w4 = w_bc[idx4], w5 = w_bc[idx5], w6 = w_bc[idx6];
        double w7 = w_bc[idx7], w8 = w_bc[idx8];
        // 6th-order one-sided FD: (360u₁ - 450u₂ + 400u₃ - 225u₄ + 72u₅ - 10u₆) / 60
        du_dk = (360.0*u3 - 450.0*u4 + 400.0*u5 - 225.0*u6 + 72.0*u7 - 10.0*u8) / 60.0;
        dv_dk = (360.0*v3 - 450.0*v4 + 400.0*v5 - 225.0*v6 + 72.0*v7 - 10.0*v8) / 60.0;
        dw_dk = (360.0*w3 - 450.0*w4 + 400.0*w5 - 225.0*w6 + 72.0*w7 - 10.0*w8) / 60.0;
        rho_wall = rho_bc[idx3];
    } else if (is_top) {
        int idxm1 = j * nface + (NZ6 - 5) * NX6 + i;
        int idxm2 = j * nface + (NZ6 - 6) * NX6 + i;
        int idxm3 = j * nface + (NZ6 - 7) * NX6 + i;
        int idxm4 = j * nface + (NZ6 - 8) * NX6 + i;
        int idxm5 = j * nface + (NZ6 - 9) * NX6 + i;
        int idxm6 = j * nface + (NZ6 - 10) * NX6 + i;
        double um1 = u_bc[idxm1], um2 = u_bc[idxm2], um3 = u_bc[idxm3], um4 = u_bc[idxm4];
        double um5 = u_bc[idxm5], um6 = u_bc[idxm6];
        double vm1 = v_bc[idxm1], vm2 = v_bc[idxm2], vm3 = v_bc[idxm3], vm4 = v_bc[idxm4];
        double vm5 = v_bc[idxm5], vm6 = v_bc[idxm6];
        double wm1 = w_bc[idxm1], wm2 = w_bc[idxm2], wm3 = w_bc[idxm3], wm4 = w_bc[idxm4];
        double wm5 = w_bc[idxm5], wm6 = w_bc[idxm6];
        // 6th-order one-sided FD (reversed sign for top wall)
        du_dk = -(360.0*um1 - 450.0*um2 + 400.0*um3 - 225.0*um4 + 72.0*um5 - 10.0*um6) / 60.0;
        dv_dk = -(360.0*vm1 - 450.0*vm2 + 400.0*vm3 - 225.0*vm4 + 72.0*vm5 - 10.0*vm6) / 60.0;
        dw_dk = -(360.0*wm1 - 450.0*wm2 + 400.0*wm3 - 225.0*wm4 + 72.0*wm5 - 10.0*wm6) / 60.0;
        rho_wall = rho_bc[idxm1];
    }

    // ── STEP 1: Interpolation + Streaming ──
    double rho_stream = 0.0, mx_stream = 0.0, my_stream = 0.0, mz_stream = 0.0;
    double f_arr[19];  // register buffer

#if USE_WENO7
    // Per-step reset: 歸零 per-point WENO activation counter
    g_weno_activation_count_zeta[k][j][i] = 0;
#endif

#if GILBM_ALGO2_STORE != GILBM2_STORE_WEIGHTS_FOLDED
    // ── per-class 權重 cache: q-loop 前「建一次」, 同 class 的 q 共享, 不再 per-q 重算/重讀 ──
    //   ★唯一 STORE-mode 差異 = cache 的「填法」; q-loop 消費端兩模式逐位元相同★
    //   B/COORDS : 每 moving class 算一次 lagrange → 8 class × 2 = 16 lagrange = 7×2×8 coeff/point
    //   A/WEIGHTS: 每 moving class 讀一次 wr/ws → 8 class × 2 讀 (取代原 per-q 32 讀)
    //   FOLDED 不用 cache (per-q 讀折疊 struct, 仿 ITB)。
    double Lxi_cache[GILBM2_NCLASS][7];
    double Lzeta_cache[GILBM2_NCLASS][7];
    for (int cls = 1; cls < GILBM2_NCLASS; cls++) {
#if GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS
        const GILBM2_DepartWeights dw = table_d[gilbm2_coord_index(cls, j, k)];
        #pragma unroll
        for (int s = 0; s < 7; s++) { Lxi_cache[cls][s] = dw.wr[s]; Lzeta_cache[cls][s] = dw.ws[s]; }
#else
        const GILBM2_DepartCoords dc = table_d[gilbm2_coord_index(cls, j, k)];
        lagrange_7point_coeffs(dc.t_xi,   Lxi_cache[cls]);
        lagrange_7point_coeffs(dc.t_zeta, Lzeta_cache[cls]);
#endif
    }
#endif

    for (int q = 0; q < 19; q++) {
        double f_streamed;

        if (q == 0) {
            // q=0: 靜止方向, departure point = center → 直接讀取自身
            f_streamed = f_post_read[0 * GRID_SIZE + index];
        } else {
            bool need_bc = false;
            if (is_bottom) need_bc = NeedsBoundaryCondition(q, zeta_y_val, zeta_z_val, true);
            else if (is_top) need_bc = NeedsBoundaryCondition(q, zeta_y_val, zeta_z_val, false);
            if (need_bc) {
                // ★ wall-BC 在任何查表之前 — wall row 的 BC 方向永不消費表值
                f_streamed = ChapmanEnskogBC(q, rho_wall,
                    du_dk, dv_dk, dw_dk,
                    zeta_y_val, zeta_z_val,
                    omega_global, dt_global);
            } else {
                const double ex = GILBM_e[q][0];
                const double ey = GILBM_e[q][1];
                const double ez = GILBM_e[q][2];
                const int q_off = q * GRID_SIZE;

                if (ey == 0.0 && ez == 0.0) {
                    // ═══════════════════════════════════════════════════════
                    // 1D: q=1,2 (±x) — 僅 η 方向 7-point（與 Algorithm1 相同，不查表）
                    // ═══════════════════════════════════════════════════════
                    const int eta_sign = (ex > 0.0) ? 0 : 1;

                    int base_1d = q_off + j * nface + k * NX6 + bi;
                    f_streamed = 0.0;
                    for (int si = 0; si < 7; si++)
                        f_streamed += GILBM_L_eta_shared[eta_sign][si] * f_post_read[base_1d + si];

                } else {
                    // ═══════════════════════════════════════════════════════
                    // 2D / 3D: ★ Algorithm2 — 用預計算 (t_xi, t_zeta) 取代
                    //   per-q RK2 重算 (Algorithm1 L302-321)。表存 post-clamp
                    //   值，q 共 (e_y,e_z) 類者讀同一 entry (q3,7,8→class1...)
                    // ═══════════════════════════════════════════════════════
                    const int cls = gilbm2_yz_class_from_q(q);
#if GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS_FOLDED
                    // ★★ FOLDED (A-fast, 仿 ITB): ghost 已折進 ws_eff + 絕對 k_idx ★★
                    //   → 純 flat nested MAC, 無 interp2 / ghost_extrapolate / zeta_collapse。
                    //   1e-12-equivalent (非 bit-exact): folding 重結合 FP。
                    const GILBM2_DepartWeightsFolded c = table_d[gilbm2_coord_index(cls, j, k)];
                    double out = 0.0;
                    if (ex == 0.0) {
                        // 2D: ξ×ζ flat MAC (49-term)
                        for (int sj = 0; sj < 7; sj++) {
                            const int gj = c.j0 + sj;
                            const double wj = c.wr[sj];
                            for (int sk = 0; sk < 7; sk++) {
                                out += wj * c.ws[sk] *
                                       f_post_read[q_off + gj * nface + c.k_idx[sk] * NX6 + i];
                            }
                        }
                    } else {
                        // 3D: η×ξ×ζ flat MAC (343-term), loop order intentionally mirrors ITB.
                        const int eta_sign = (ex > 0.0) ? 0 : 1;
                        const int i0 = i - 3;
                        for (int sx = 0; sx < 7; sx++) {
                            const double wx = GILBM_L_eta_shared[eta_sign][sx];
                            const int gi = i0 + sx;
                            for (int sj = 0; sj < 7; sj++) {
                                const int gj = c.j0 + sj;
                                const double wj = c.wr[sj];
                                for (int sk = 0; sk < 7; sk++) {
                                    out += wx * wj * c.ws[sk] *
                                           f_post_read[q_off + gj * nface + c.k_idx[sk] * NX6 + gi];
                                }
                            }
                        }
                    }
                    f_streamed = out;
#else
                    // ★ legacy (COORDS/WEIGHTS): per-class cache → interp2 → ghost → zeta_collapse ★
                    //   同 class 的 q 共用 cache; 下游兩模式逐位元相同 (bit-exact vs Algorithm1)。
                    const double *L_xi   = Lxi_cache[cls];
                    const double *L_zeta = Lzeta_cache[cls];
                    const double  t_zeta = 3.0;   // unused (USE_WENO7=0 → zeta-collapse 線性不讀)

                    double interp2[7];
                    if (ex == 0.0) {
                        for (int sk = 0; sk < 7; sk++) {
                            double acc = 0.0;
                            for (int sj = 0; sj < 7; sj++) {
                                acc += L_xi[sj] * f_post_read[q_off + ((j - 3) + sj) * nface + (bk + sk) * NX6 + i];
                            }
                            interp2[sk] = acc;
                        }
                    } else {
                        const int eta_sign = (ex > 0.0) ? 0 : 1;
                        for (int sk = 0; sk < 7; sk++) {
                            double acc = 0.0;
                            for (int sj = 0; sj < 7; sj++) {
                                double row_val = 0.0;
                                int base_idx = ((j - 3) + sj) * nface + (bk + sk) * NX6 + bi;
                                for (int si = 0; si < 7; si++) {
                                    row_val += GILBM_L_eta_shared[eta_sign][si] * f_post_read[q_off + base_idx + si];
                                }
                                acc += L_xi[sj] * row_val;
                            }
                            interp2[sk] = acc;
                        }
                    }

                    gilbm_ghost_zone_extrapolate(interp2, bk);

                    f_streamed = gilbm_zeta_collapse(interp2, L_zeta,
                        t_zeta, bk, i, j, k, z_zeta_d, q);
#endif
                }  // end 2D/3D branch
            }
        }

        f_arr[q] = f_streamed;
        rho_stream += f_streamed;
        mx_stream  += GILBM_e[q][0] * f_streamed;
        my_stream  += GILBM_e[q][1] * f_streamed;
        mz_stream  += GILBM_e[q][2] * f_streamed;
    }

    // ── STEP 1.5: Macroscopic (mass correction) ──
    if (i < NX6 - 4 && j < NYD6 - 4) {
        rho_stream += rho_modify[0];
        f_arr[0]   += rho_modify[0];
    }
    double rho_local = rho_stream;
#if USE_GUO_FORCING
    // 半力宏觀修正 (Guo 2002): ρu = Σf·c + δt·F/2
    // F_body = (0, Force[0], 0) → 僅 v 方向有修正
    const double half_Fdt = 0.5 * GILBM_dt * Force[0];
    double u_local = mx_stream / rho_local;
    double v_local = (my_stream + half_Fdt) / rho_local;
    double w_local = mz_stream / rho_local;
#else
    double u_local = mx_stream / rho_local;
    double v_local = my_stream / rho_local;
    double w_local = mz_stream / rho_local;
#endif

    // ── Wall no-slip: 壁面速度已知為零，直接強制 ──
    const bool is_wall = (is_bottom || is_top);
#ifndef DISABLE_WALL_MOMENTUM_CORRECTION
    if (is_wall) {
        u_local = 0.0;
        v_local = 0.0;
        w_local = 0.0;
    }
#endif

    // ── STEP 2: Collision (MRT/BGK) — 與 Algorithm1 相同 ──
    double f_out[19];
    gilbm_collision_GTS(f_out, f_arr, rho_local, u_local, v_local, w_local,
                        GILBM_s_visc_global, GILBM_dt, Force[0]);

    // ── Write: 碰後分佈 → f_post_write ──
    for (int q = 0; q < 19; q++)
        f_post_write[q * GRID_SIZE + index] = f_out[q];

    // ── Write: 巨觀量 ──
    u_out[index]       = u_local;
    v_out[index]       = v_local;
    w_out[index]       = w_local;
    rho_out_arr[index] = rho_local;
}

// ════════════════════════════════════════════════════════════════════════════
//  §B2  __global__ wrapper — mirror of Algorithm1_FusedKernel_GTS_Buffer
//  (1.algorithm1.h:769-787) + coords_d。smem interior 變體不做（Algo1-only）。
// ════════════════════════════════════════════════════════════════════════════
__global__ void Algorithm2_FusedKernel_GTS_Buffer(
    const double *f_post_read, double *f_post_write,
    const double *zeta_z_d, const double *zeta_y_d,
    const double *xi_y_d,   const double *xi_z_d,
    const int    *bk_precomp_d,
    const double *z_zeta_d,
    const double *u_bc, const double *v_bc, const double *w_bc, const double *rho_bc,
    double *u_out, double *v_out, double *w_out, double *rho_out,
    double *rho_modify, const double *Force,
    const GILBM2_Table * __restrict__ table_d,
    int start_j)
{
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    const int j = blockIdx.y * blockDim.y + threadIdx.y + start_j;
    const int k = blockIdx.z;
    if (i < 3 || i >= NX6 - 3 || j < 3 || j >= NYD6 - 3 || k < 3 || k >= NZ6 - 3) return;
    algorithm2_step1_GTS(i, j, k,
        f_post_read, f_post_write,
        zeta_z_d, zeta_y_d, xi_y_d, xi_z_d, bk_precomp_d, z_zeta_d,
        u_bc, v_bc, w_bc, rho_bc,
        u_out, v_out, w_out, rho_out, rho_modify, Force, table_d);
}

// ════════════════════════════════════════════════════════════════════════════
//  §B3  Device table build — bit-exact 路徑（一次性，init 階段）
//  讀 __constant__ GILBM_dt（須在 main.cu 上傳 dt 之後 launch）+ device metric
//  陣列（須在 metric H2D copy 之後）。每 thread 一個 (cls,j,k) entry。
//  非 interior / class 0 → inert (3.0, 3.0)，與 host build loop 相同。
// ════════════════════════════════════════════════════════════════════════════
__global__ void Algorithm2_BuildCoordsTable_Device(
    GILBM2_Table *table_d,                   // [GILBM2_NCLASS * NYD6 * NZ6] (COORDS 或 WEIGHTS)
    const double *xi_y_d,  const double *xi_z_d,
    const double *zeta_y_d, const double *zeta_z_d,
    const int *bk_precomp_d)
{
    const size_t N = (size_t)GILBM2_NCLASS * (size_t)NYD6 * (size_t)NZ6;
    size_t n = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    if (n >= N) return;

    const int cls = (int)(n / ((size_t)NYD6 * NZ6));
    const int rem = (int)(n % ((size_t)NYD6 * NZ6));
    const int j   = rem / (int)NZ6;
    const int k   = rem % (int)NZ6;

    GILBM2_Table c = gilbm2_inert_entry();             // inert default (class0 / ghost)
    if (cls >= 1 && j >= 3 && j < (int)NYD6 - 3 && k >= 3 && k < (int)NZ6 - 3) {
        double ey, ez;
        gilbm2_class_velocity(cls, &ey, &ez);
        c = gilbm2_gen_table_entry(j, k, ey, ez, GILBM_dt,
                xi_y_d, xi_z_d, zeta_y_d, zeta_z_d, bk_precomp_d[k]);
    }
    table_d[n] = c;
}

// ════════════════════════════════════════════════════════════════════════════
//  §B4  §7a reference dump — Algorithm1 的「原版」路徑（零改動 Algorithm1）
//  呼叫 1.algorithm1.h:16 的 gilbm_rk2_displacement 本尊，並逐行重現
//  algorithm1_step1_GTS 的 t_xi/t_zeta 推導（L186-188, L310-321）。
//  與 §B3 比對 = 原函式 vs 轉寫的真交叉驗證，要求逐位元 diff = 0。
// ════════════════════════════════════════════════════════════════════════════
__global__ void Algorithm2_RefCoords_Algo1Path(
    GILBM2_Table *ref_d,                     // [GILBM2_NCLASS * NYD6 * NZ6]
    const double *xi_y_d,  const double *xi_z_d,
    const double *zeta_y_d, const double *zeta_z_d,
    const int *bk_precomp_d)
{
    const size_t N = (size_t)GILBM2_NCLASS * (size_t)NYD6 * (size_t)NZ6;
    size_t n = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    if (n >= N) return;

    const int cls = (int)(n / ((size_t)NYD6 * NZ6));
    const int rem = (int)(n % ((size_t)NYD6 * NZ6));
    const int j   = rem / (int)NZ6;
    const int k   = rem % (int)NZ6;

    GILBM2_Table r = gilbm2_inert_entry();             // inert default (同 §B3)
    if (cls >= 1 && j >= 3 && j < (int)NYD6 - 3 && k >= 3 && k < (int)NZ6 - 3) {
        double ey, ez;
        gilbm2_class_velocity(cls, &ey, &ez);

        // —— 以下逐行重現 algorithm1_step1_GTS ——
        const double dt_global = GILBM_dt;               // L183
        const int idx_jk = j * NZ6 + k;                  // L180
        const int bj = j - 3;                            // L187 (unclamped)
        const int bk = bk_precomp_d[k];                  // L188
        const double xi_y_val   = xi_y_d[idx_jk];        // L191-192
        const double xi_z_val   = xi_z_d[idx_jk];
        const double zeta_y_val = zeta_y_d[idx_jk];      // L197-198
        const double zeta_z_val = zeta_z_d[idx_jk];

        double d_xi, delta_zeta_q;                       // L302-307 — 呼叫本尊
        gilbm_rk2_displacement(j, k, ey, ez, dt_global,
            xi_y_val, xi_z_val, zeta_y_val, zeta_z_val,
            xi_y_d, xi_z_d, zeta_y_d, zeta_z_d,
            d_xi, delta_zeta_q);

        double t_xi  = (double)(j - bj) - d_xi;          // L310
        if (t_xi  < 0.0) t_xi  = 0.0; if (t_xi  > 6.0) t_xi  = 6.0;   // L311

        double up_k = (double)k - delta_zeta_q;          // L316
        if (up_k < 3.0)              up_k = 3.0;         // L317
        if (up_k > (double)(NZ6 - 4)) up_k = (double)(NZ6 - 4);       // L318
        double t_zeta = up_k - (double)bk;               // L319 — 無 [0,6] clamp

#if   GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS_FOLDED
        // FOLDED 參照 = fold(lagrange(Algo1 原版 RK2 座標)) — 與 §B3 build (fold(lagrange(generator)))
        // 比對: 座標位元相同(Round A) → L_xi/L_zeta 位元相同 → 折疊 ws_eff/k_idx 位元相同。
        {
            double Lxi_r[7], Lzeta_r[7];
            gilbm2_lagrange7(t_xi,   Lxi_r);
            gilbm2_lagrange7(t_zeta, Lzeta_r);
            r.j0 = j - 3;
            gilbm2_fold_zeta_ghost(bk, Lzeta_r, r.k_idx, r.ws);
            for (int s = 0; s < 7; s++) {
                r.wr[s] = Lxi_r[s];
            }
        }
#elif GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS
        // WEIGHTS 參照 = lagrange(Algo1 原版 RK2 座標) — 與 §B3 build 的
        // lagrange(generator 座標) 比對: 座標位元相同(Round A 已證) → 權重位元相同
        gilbm2_lagrange7(t_xi,   r.wr);
        gilbm2_lagrange7(t_zeta, r.ws);
#else
        r.t_xi = t_xi;
        r.t_zeta = t_zeta;
#endif
    }
    ref_d[n] = r;
}

// ════════════════════════════════════════════════════════════════════════════
//  §B4.5  Class-map independent validation — 打破驗證循環性
//  §B3 build 與 §B4 ref 兩側都經 gilbm2_class_velocity() 取 (ey,ez)，class 表
//  若錯，表-對-表比對仍會通過。本 kernel 用 kernel 自身的 __constant__
//  GILBM_e 當獨立 ground truth：對每個 q 驗證
//      gilbm2_class_velocity(gilbm2_yz_class_from_q(q)) == (GILBM_e[q][1], [2])
//  （q=0,1,2 → class 0 → (0,0)，與 GILBM_e 的 ey=ez=0 一致，19 個 q 全覆蓋。）
//  consumer 走 q→class→表 的同一條映射，故此檢查直接守住 consumer 的取數正確性。
// ════════════════════════════════════════════════════════════════════════════
__global__ void Algorithm2_ValidateClassMap_Device(int *mismatch_d)
{
    const int q = threadIdx.x;
    if (q >= 19) return;
    const int cls = gilbm2_yz_class_from_q(q);
    double ey, ez;
    gilbm2_class_velocity(cls, &ey, &ez);
    if (ey != GILBM_e[q][1] || ez != GILBM_e[q][2])
        atomicAdd(mismatch_d, 1);
}

#if GILBM2_DEPARTURE_RK4
// ── RK4 嵌入式 step-doubling 局部誤差: 每點 E_local (cells) → 自我認證 ──
//   device 重跑 gen_departure_coords 取 err_out (= Richardson |D_2N−D_N|/15)。production 同路徑。
__global__ void Algorithm2_EmbeddedError_Device(
    double *err_d,
    const double *xi_y_d,  const double *xi_z_d,
    const double *zeta_y_d, const double *zeta_z_d,
    const int *bk_precomp_d)
{
    const size_t N = (size_t)GILBM2_NCLASS * (size_t)NYD6 * (size_t)NZ6;
    size_t n = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    if (n >= N) return;
    const int cls = (int)(n / ((size_t)NYD6 * NZ6));
    const int rem = (int)(n % ((size_t)NYD6 * NZ6));
    const int j   = rem / (int)NZ6;
    const int k   = rem % (int)NZ6;
    double e = 0.0;
    if (cls >= 1 && j >= 3 && j < (int)NYD6 - 3 && k >= 3 && k < (int)NZ6 - 3) {
        double ey, ez; gilbm2_class_velocity(cls, &ey, &ez);
        unsigned char fl = 0; double el = 0.0;
        (void)gilbm2_gen_departure_coords(j, k, ey, ez, GILBM_dt,
            xi_y_d, xi_z_d, zeta_y_d, zeta_z_d, bk_precomp_d[k], &fl, &el);
        e = el;
    }
    err_d[n] = e;
}
#endif

// ════════════════════════════════════════════════════════════════════════════
//  §B5  Validation comparator — host 端三方比對（§7b 的 device 部分）
//  (a) device 建表 (§B3) vs Algo1-path 參照 (§B4)：要求逐位元 0 mismatch。
//  (b) device 建表 vs host 建表（呼叫端傳入，可為 NULL 跳過）：容差 1e-12
//      （host naive→已統一硬編 lagrange，僅剩 FMA ~1 ULP；門檻仍取 1e-12）。
//  (c) class map vs GILBM_e (§B4.5)：必須 0 mismatch。
//  max-abs + RMS 皆對 interior 域 (cls 1..8, j∈[3,NYD6-4], k∈[3,NZ6-4]) 計算。
//  自含：不依賴 CHECK_CUDA / MPI。回傳 0 = PASS；呼叫端（Round C wiring）負責
//  MPI_Allreduce 全域聚合與 abort 決策。
// ════════════════════════════════════════════════════════════════════════════
struct GILBM2_ValidationResult {
    long long bitwise_mismatch;   // (a) device vs ref — 必須 0
    double    max_abs_txi;        // (a) 同上 — 必須 0.0
    double    max_abs_tzeta;
    double    rms_txi;            // (a) interior-domain RMS — 必須 0.0
    double    rms_tzeta;
    long long host_tol_fail;      // (b) device vs host 超過 1e-12 的 entry 數
    double    host_max_abs;       // (b) max |device - host|
    double    host_rms;           // (b) interior-domain RMS
    int       class_map_mismatch; // (c) q→class→(ey,ez) vs GILBM_e — 必須 0
    long long folded_shape_bad;   // strict level 2: k_idx must be contiguous physical window
    long long folded_sum_fail;    // strict level 2: sum(ws_eff) must stay within 1e-12 of 1
    double    folded_max_sum_err;
    int       worst_cls, worst_j, worst_k;
    double    max_embedded_err;   // RK4 嵌入式 step-doubling 最大局部誤差 (cells) — 必須 < ERRTOL
    long long nonfinite_count;    // RK4: wr/ws 或 E_local 出現 NaN/Inf 數 — 必須 0
    long long int_field_mismatch; // RK4: folded j0/k_idx vs ref 整數不符數 — 必須 0 (float 容差不可掩蓋)
};

static inline int Algorithm2_ValidateCoordsTable(
    const GILBM2_Table *table_dev_d,          // device ptr — §B3 建好的表 (COORDS 或 WEIGHTS)
    const double *xi_y_d,  const double *xi_z_d,
    const double *zeta_y_d, const double *zeta_z_d,
    const int *bk_precomp_d,
    const GILBM2_Table *table_host_h,         // host 建表 (可 NULL 跳過 (b))
    GILBM2_ValidationResult *out, int myid)
{
    const size_t N = (size_t)GILBM2_NCLASS * (size_t)NYD6 * (size_t)NZ6;
    const size_t bytes = N * sizeof(GILBM2_Table);
    GILBM2_ValidationResult R;
    R.bitwise_mismatch = 0; R.max_abs_txi = 0.0; R.max_abs_tzeta = 0.0;
    R.rms_txi = 0.0; R.rms_tzeta = 0.0;
    R.host_tol_fail = 0; R.host_max_abs = 0.0; R.host_rms = 0.0;
    R.class_map_mismatch = 0;
    R.folded_shape_bad = 0; R.folded_sum_fail = 0; R.folded_max_sum_err = 0.0;
    R.worst_cls = -1; R.worst_j = -1; R.worst_k = -1;
    R.max_embedded_err = 0.0; R.nonfinite_count = 0; R.int_field_mismatch = 0;

    GILBM2_Table *ref_d = nullptr;
    GILBM2_Table *h_dev = (GILBM2_Table*)malloc(bytes);
    GILBM2_Table *h_ref = (GILBM2_Table*)malloc(bytes);
    if (!h_dev || !h_ref) {
        fprintf(stderr, "[ALGO2][rank %d] FATAL: validation host alloc failed\n", myid);
        free(h_dev); free(h_ref);
        return -1;
    }
    if (cudaMalloc(&ref_d, bytes) != cudaSuccess) {
        fprintf(stderr, "[ALGO2][rank %d] FATAL: validation cudaMalloc failed\n", myid);
        free(h_dev); free(h_ref);
        return -1;
    }

    // ── (c) class-map independent check vs GILBM_e (§B4.5) — 打破循環性 ──
    int *cm_d = nullptr;
    if (cudaMalloc(&cm_d, sizeof(int)) == cudaSuccess) {
        cudaMemset(cm_d, 0, sizeof(int));
        Algorithm2_ValidateClassMap_Device<<<1, 32>>>(cm_d);
        if (cudaDeviceSynchronize() != cudaSuccess ||
            cudaMemcpy(&R.class_map_mismatch, cm_d, sizeof(int), cudaMemcpyDeviceToHost) != cudaSuccess) {
            fprintf(stderr, "[ALGO2][rank %d] FATAL: class-map kernel failed\n", myid);
            cudaFree(cm_d); cudaFree(ref_d); free(h_dev); free(h_ref);
            return -1;
        }
        cudaFree(cm_d);
    } else {
        fprintf(stderr, "[ALGO2][rank %d] FATAL: class-map cudaMalloc failed\n", myid);
        cudaFree(ref_d); free(h_dev); free(h_ref);
        return -1;
    }

    const int NT_VAL = 256;
    const int NB = (int)((N + NT_VAL - 1) / NT_VAL);
    Algorithm2_RefCoords_Algo1Path<<<NB, NT_VAL>>>(ref_d,
        xi_y_d, xi_z_d, zeta_y_d, zeta_z_d, bk_precomp_d);
    if (cudaDeviceSynchronize() != cudaSuccess) {
        fprintf(stderr, "[ALGO2][rank %d] FATAL: ref kernel failed\n", myid);
        cudaFree(ref_d); free(h_dev); free(h_ref);
        return -1;
    }

    if (cudaMemcpy(h_dev, table_dev_d, bytes, cudaMemcpyDeviceToHost) != cudaSuccess ||
        cudaMemcpy(h_ref, ref_d,       bytes, cudaMemcpyDeviceToHost) != cudaSuccess) {
        fprintf(stderr, "[ALGO2][rank %d] FATAL: validation cudaMemcpy D2H failed\n", myid);
        cudaFree(ref_d); free(h_dev); free(h_ref);
        return -1;
    }
    cudaFree(ref_d);

    // Mode-specific validation. FOLDED contains int fields (j0/k_idx) plus double
    // fields (wr/ws), so never reinterpret the whole struct as doubles.
#if   GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS_FOLDED
    const int    NDBL       = 14;  // wr[7] + ws[7]
#elif GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS
    const int    NDBL       = 14;  // wr[7] + ws[7]
#else
    const int    NDBL       = 2;   // t_xi + t_zeta
#endif
    const double M_interior = 8.0 * (double)((int)NYD6 - 6) * (double)((int)NZ6 - 6);
    double ssq_dev = 0.0, ssq_host = 0.0, worst_diff = -1.0;

    for (size_t n = 0; n < N; n++) {
        const int cls = (int)(n / ((size_t)NYD6 * NZ6));
        const int rem = (int)(n % ((size_t)NYD6 * NZ6));
        const int jj  = rem / (int)NZ6;
        const int kk  = rem % (int)NZ6;
        const bool active_entry =
            (cls >= 1 && jj >= 3 && jj < (int)NYD6 - 3 && kk >= 3 && kk < (int)NZ6 - 3);
#if GILBM_ALGO2_STORE != GILBM2_STORE_WEIGHTS_FOLDED || GILBM_ALGO2_VALIDATE < 2
        (void)active_entry;
#endif
        bool   bit_eq = true;
        double emax   = 0.0;

#if GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS_FOLDED
        const GILBM2_DepartWeightsFolded &D = h_dev[n];
        const GILBM2_DepartWeightsFolded &Rref = h_ref[n];
        // 整數索引欄位 j0/k_idx RK-independent → 獨立計數 (RK4 gate hard; float 容差不可掩蓋,
        //   off-by-one 當 double 只是 ~4.94e-324 subnormal 差); 仍同時設 bit_eq 供 RK2 bit-exact gate。
        if (D.j0 != Rref.j0) { bit_eq = false; R.int_field_mismatch++; }
        for (int s = 0; s < 7; s++) {
            if (D.k_idx[s] != Rref.k_idx[s]) { bit_eq = false; R.int_field_mismatch++; }
        }
        for (int s = 0; s < 7; s++) {
            const double diff = fabs(D.wr[s] - Rref.wr[s]);
            if (!isfinite(D.wr[s])) R.nonfinite_count++;                    // NaN/Inf 守衛
            if (memcmp(&D.wr[s], &Rref.wr[s], sizeof(double)) != 0) bit_eq = false;
            if (diff > emax) emax = diff;
            ssq_dev += diff * diff;
        }
        for (int s = 0; s < 7; s++) {
            const double diff = fabs(D.ws[s] - Rref.ws[s]);
            if (!isfinite(D.ws[s])) R.nonfinite_count++;
            if (memcmp(&D.ws[s], &Rref.ws[s], sizeof(double)) != 0) bit_eq = false;
            if (diff > emax) emax = diff;
            ssq_dev += diff * diff;
        }

#if GILBM_ALGO2_VALIDATE >= 2
        if (active_entry) {
            bool shape_ok = (D.k_idx[0] >= 3 && D.k_idx[6] <= (int)NZ6 - 4);
            for (int s = 1; s < 7; s++) {
                if (D.k_idx[s] != D.k_idx[0] + s) shape_ok = false;
            }
            if (!shape_ok) R.folded_shape_bad++;

            double sum_ws = 0.0;
            for (int s = 0; s < 7; s++) sum_ws += D.ws[s];
            const double sum_err = fabs(sum_ws - 1.0);
            if (sum_err > R.folded_max_sum_err) R.folded_max_sum_err = sum_err;
            if (sum_err > 1.0e-12) R.folded_sum_fail++;
        }
#endif

        if (table_host_h) {
            const GILBM2_DepartWeightsFolded &H = table_host_h[n];
            bool host_bad = false;
            double hm = 0.0;
            if (D.j0 != H.j0) host_bad = true;
            for (int s = 0; s < 7; s++) {
                if (D.k_idx[s] != H.k_idx[s]) host_bad = true;
            }
            for (int s = 0; s < 7; s++) {
                const double hd = fabs(D.wr[s] - H.wr[s]);
                if (hd > hm) hm = hd;
                ssq_host += hd * hd;
            }
            for (int s = 0; s < 7; s++) {
                const double hd = fabs(D.ws[s] - H.ws[s]);
                if (hd > hm) hm = hd;
                ssq_host += hd * hd;
            }
            if (hm > R.host_max_abs) R.host_max_abs = hm;
            if (host_bad || hm > 1.0e-12) R.host_tol_fail++;
        }
#else
        const double *dv = (const double*)&h_dev[n];
        const double *rf = (const double*)&h_ref[n];
        for (int d = 0; d < NDBL; d++) {
            const double diff = fabs(dv[d] - rf[d]);
            if (memcmp(&dv[d], &rf[d], sizeof(double)) != 0) bit_eq = false;
            if (diff > emax) emax = diff;
            ssq_dev += diff * diff;
        }
        if (table_host_h) {
            const double *ht = (const double*)&table_host_h[n];
            double hm = 0.0;
            for (int d = 0; d < NDBL; d++) {
                const double hd = fabs(dv[d] - ht[d]);
                if (hd > hm) hm = hd;
                ssq_host += hd * hd;
            }
            if (hm > R.host_max_abs) R.host_max_abs = hm;
            if (hm > 1.0e-12) R.host_tol_fail++;
        }
#endif
        if (emax > R.max_abs_txi) R.max_abs_txi = emax;
        if (!bit_eq) {
            R.bitwise_mismatch++;
            if (emax > worst_diff) {
                worst_diff = emax;
                R.worst_cls = cls;
                R.worst_j = jj;
                R.worst_k = kk;
            }
        }
    }
    free(h_dev); free(h_ref);

    R.max_abs_tzeta = 0.0;   // generalized: 合併進 max_abs_txi (over all stored doubles)
    R.rms_txi   = sqrt(ssq_dev  / ((double)NDBL * M_interior));
    R.rms_tzeta = 0.0;
    R.host_rms  = table_host_h ? sqrt(ssq_host / ((double)NDBL * M_interior)) : 0.0;

#if GILBM2_DEPARTURE_RK4
    // ── RK4 嵌入式誤差自我認證: device 重跑 gen_departure_coords 取每點 E_local, 取 max ──
    {
        double *err_d = nullptr;
        double *err_h = (double*)malloc(N * sizeof(double));
        if (err_h && cudaMalloc(&err_d, N * sizeof(double)) == cudaSuccess) {
            Algorithm2_EmbeddedError_Device<<<NB, NT_VAL>>>(err_d,
                xi_y_d, xi_z_d, zeta_y_d, zeta_z_d, bk_precomp_d);
            if (cudaDeviceSynchronize() == cudaSuccess &&
                cudaMemcpy(err_h, err_d, N * sizeof(double), cudaMemcpyDeviceToHost) == cudaSuccess) {
                double me = 0.0;
                for (size_t n = 0; n < N; n++) {
                    if (!isfinite(err_h[n])) R.nonfinite_count++;
                    else if (err_h[n] > me) me = err_h[n];
                }
                R.max_embedded_err = me;
            } else {
                fprintf(stderr, "[ALGO2][rank %d] FATAL: embedded-error kernel failed\n", myid);
                R.max_embedded_err = 1.0e300;
            }
            cudaFree(err_d);
        } else {
            fprintf(stderr, "[ALGO2][rank %d] FATAL: embedded-error alloc failed\n", myid);
            R.max_embedded_err = 1.0e300;
        }
        free(err_h);
    }
#endif

    printf("[ALGO2][rank %d] table validation (%d double fields/entry): dev-vs-Algo1ref bitwise "
           "mismatch = %lld (max|d|=%.3e, rms=%.3e)%s; dev-vs-host max=%.3e rms=%.3e "
           "tol_fail=%lld; class-map mismatch=%d%s\n",
           myid, NDBL, R.bitwise_mismatch, R.max_abs_txi, R.rms_txi,
           (R.bitwise_mismatch == 0 ? " [BIT-EXACT OK]" : " [FAIL]"),
           R.host_max_abs, R.host_rms, R.host_tol_fail,
           R.class_map_mismatch,
           (R.class_map_mismatch == 0 ? " [MAP OK]" : " [MAP FAIL]"));
    if (R.bitwise_mismatch != 0 && R.worst_cls >= 0) {
        printf("[ALGO2][rank %d]   worst at cls=%d j=%d k=%d\n",
               myid, R.worst_cls, R.worst_j, R.worst_k);
    }
#if GILBM_ALGO2_STORE == GILBM2_STORE_WEIGHTS_FOLDED
    if (GILBM_ALGO2_VALIDATE >= 2) {
        printf("[ALGO2][rank %d] folded strict checks: shape_bad=%lld, "
               "sum_fail=%lld, max|sum(ws)-1|=%.3e%s\n",
               myid, R.folded_shape_bad, R.folded_sum_fail, R.folded_max_sum_err,
               (R.folded_shape_bad == 0 && R.folded_sum_fail == 0 ? " [FOLDED OK]" : " [FOLDED FAIL]"));
    }
#endif

#if GILBM2_DEPARTURE_RK4
    printf("[ALGO2][rank %d] RK4 departure: embedded max E_local=%.3e (tol %.1e)%s; "
           "weight gap vs Algo1-RK2 max=%.3e (預期~6e-6, bound 1e-3); int-field=%lld%s; nonfinite=%lld\n",
           myid, R.max_embedded_err, (double)GILBM2_DEPARTURE_ERRTOL,
           (R.max_embedded_err < GILBM2_DEPARTURE_ERRTOL ? " [CONVERGED OK]" : " [NOT CONVERGED]"),
           R.max_abs_txi, R.int_field_mismatch,
           (R.int_field_mismatch == 0 ? " [INT OK]" : " [INT FAIL]"), R.nonfinite_count);
#endif

    if (out) *out = R;
#if GILBM2_DEPARTURE_RK4
    //  RK4 過閘: 嵌入式收斂(<ERRTOL) + 獨立 gap 未爆(<1e-3) + dev==host + 無 NaN + 整數索引 j0/k_idx 相等
    //  + class-map + folded-shape/sum; 不再要求 bitwise(float 權重故意偏離 Algorithm1 RK2 = 精度增益)。
    return (R.max_embedded_err < GILBM2_DEPARTURE_ERRTOL &&
            R.max_abs_txi < 1.0e-3 &&
            R.host_tol_fail == 0 &&
            R.nonfinite_count == 0 &&
            R.int_field_mismatch == 0 &&
            R.class_map_mismatch == 0 &&
            R.folded_shape_bad == 0 &&
            R.folded_sum_fail == 0) ? 0 : 1;
#else
    return (R.bitwise_mismatch == 0 &&
            R.host_tol_fail == 0 &&
            R.class_map_mismatch == 0 &&
            R.folded_shape_bad == 0 &&
            R.folded_sum_fail == 0) ? 0 : 1;
#endif
}

#endif // ALGORITHM2_H
