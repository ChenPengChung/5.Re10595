#!/usr/bin/env python3
# ============================================================================
#  rk2_rk4_rs_error.py — GILBM Algorithm2 departure (r,s) 誤差比較: RK2 vs RK4
#
#  忠實 port gilbm/precompute2.h 的:
#    - gilbm2_lagrange7        (7 點 Lagrange 基)
#    - gilbm2_sample_contravariant / gilbm2_rk4_step / departure_displacement_rk4
#    - RK2 midpoint (Imamura 2005) — GILBM2_DEPARTURE_RK4=0 路徑
#  在壁法向 (zeta) Vinokur-tanh 拉伸網格上 (= 主導 (r,s) 誤差來源) 比較,
#  每個 k 與每個方向 ez=+/-1:
#    e_rk2  = |RK2 departure  − exact|   (cells)   ← 現行方案誤差
#    e_rk4  = |RK4 departure  − exact|   (cells)   ← 升級後誤差
#    gap    = |RK2 − RK4|                (cells)   ← 精度增益 (= 使用者要的 (r,s) 誤差)
#  exact = tanh 解析逆映射 (物理特徵線是直線: z_d = z(k) − ez·dt)。
#
#  與 departure-accuracy workflow 的 1D 量測對照 (RK2 ~6.27e-6, RK4 ~8e-12)。
#  完整 2D × 9 方向版本需讀 solver 完整 metric dump (見 --metric-file, 待補)。
#
#  用法:  python3 rk2_rk4_rs_error.py [--dt 5.073e-4] [--sweep] [--profile]
# ============================================================================
import math, argparse

# ── 網格參數 (variables.h) ──
NZ       = 321
NZ_CELLS = NZ - 1            # 320
BFR      = 3
NZ6      = NZ + 2 * BFR      # 327 (含 ±3 ghost/buffer)
STRETCH_A = 0.95
LZ       = 3.036
CFL      = 0.5
ERRTOL   = 1e-10            # GILBM2_DEPARTURE_ERRTOL
MAXDEPTH = 6               # GILBM2_DEPARTURE_MAXDEPTH
C        = math.log((1.0 + STRETCH_A) / (1.0 - STRETCH_A))   # GAMMA = ln(39)

# ── tanh_wall 壁法向映射 z(jnode), jnode=k-BFR ∈ [0, NZ_CELLS] (ghost 用解析延伸) ──
def z_of_jnode(jn):
    arg = ((-1.0 + 2.0 * jn / NZ_CELLS) / 2.0) * C
    return LZ / 2.0 + (LZ / 2.0 / STRETCH_A) * math.tanh(arg)

def dz_dk(jn):                                   # dz/djnode = dz/dk (解析導數)
    arg = ((-1.0 + 2.0 * jn / NZ_CELLS) / 2.0) * C
    sech2 = 1.0 - math.tanh(arg) ** 2
    return (LZ / 2.0 / STRETCH_A) * sech2 * (C / NZ_CELLS)

def z_of_k(k):   return z_of_jnode(k - BFR)
def zeta_z(k):   return 1.0 / dz_dk(k - BFR)     # ζ_z = dζ/dz = dk/dz = 1/z'(k)

def z_inv_to_k(zt):                              # 反解 z(k)=zt → k
    a = (zt - LZ / 2.0) * STRETCH_A / (LZ / 2.0)
    a = max(-0.999999999999999, min(0.999999999999999, a))
    t = math.atanh(a)
    return NZ_CELLS * (1.0 + 2.0 * t / C) / 2.0 + BFR

# ── gilbm2_lagrange7 (7 點 cardinal 權重, t∈[0,6]) ──
_DEN = [720.0, -120.0, 48.0, -36.0, 48.0, -120.0, 720.0]
def lagrange7(t):
    a = [0.0] * 7
    for i in range(7):
        p = 1.0
        for m in range(7):
            if m != i:
                p *= (t - m)
        a[i] = p / _DEN[i]
    return a

# ── gilbm2_sample_contravariant (1D zeta 版: 位置 clamp [3,NZ6-4] + stencil-base clamp) ──
def sample_zeta_z(pk):
    if pk < 3.0:        pk = 3.0
    if pk > NZ6 - 4:    pk = float(NZ6 - 4)
    sk = int(math.floor(pk)) - 3
    if sk < 0:          sk = 0
    if sk + 6 > NZ6 - 1: sk = NZ6 - 7
    ak = lagrange7(pk - sk)
    return sum(ak[m] * zeta_z(sk + m) for m in range(7))

def Vz(pk, ez):  return ez * sample_zeta_z(pk)

# ── RK2 midpoint (GILBM2_DEPARTURE_RK4=0) ──
def rk2_displacement(k, ez, dt):
    v0 = Vz(float(k), ez)
    return dt * Vz(k - 0.5 * dt * v0, ez)        # = dt · ζ_z(midpoint)·ez

# ── gilbm2_rk4_step ──
def rk4_step(pk, h, ez):
    v1 = Vz(pk, ez)
    v2 = Vz(pk - 0.5 * h * v1, ez)
    v3 = Vz(pk - 0.5 * h * v2, ez)
    v4 = Vz(pk - h * v3, ez)
    return (h / 6.0) * (v1 + 2.0 * v2 + 2.0 * v3 + v4)

# ── gilbm2_departure_displacement_rk4 (step-doubling + Richardson /15) ──
def rk4_displacement(k, ez, dt):
    coarse = rk4_step(float(k), dt, ez)          # N=1
    fine, emax = coarse, 1e300
    for level in range(1, MAXDEPTH + 1):
        N = 1 << level
        h = dt / N
        q, s = float(k), 0.0
        for _ in range(N):
            d = rk4_step(q, h, ez)
            s += d
            q -= d
        emax  = abs(s - coarse) / 15.0
        fine  = s + (s - coarse) / 15.0
        coarse = s
        if emax < ERRTOL:
            break
    return fine, emax

# ── exact: 物理特徵線直線 z_d = z(k) − ez·dt → k_d = z_inv(z_d) ──
def exact_displacement(k, ez, dt):
    return k - z_inv_to_k(z_of_k(k) - ez * dt)

def clamp_upk(k, dk):                            # solver 壁面 BC: up_k ∈ [3, NZ6-4]
    up = k - dk
    if up < 3.0:        up = 3.0
    if up > NZ6 - 4:    up = float(NZ6 - 4)
    return up

# ── 估 dt (CFL · min_k 1/|ζ_z|, 壁法向限制) ──
def cfl_dt():
    mx = max(abs(zeta_z(k)) for k in range(3, NZ6 - 3))
    return CFL / mx

def run(dt, profile=False):
    wr2 = wr4 = wgap = 0.0
    loc = None
    rows = []
    for k in range(3, NZ6 - 3):                  # 內部 k (build kernel: k ∈ [3, NZ6-3))
        for ez in (+1.0, -1.0):
            ex = exact_displacement(k, ez, dt)
            r2 = rk2_displacement(k, ez, dt)
            r4, emb = rk4_displacement(k, ez, dt)
            # 壁面 BC clamp 後的 up_k 差 (= 折疊權重前的座標 gap, 忠實對應 solver 儲存)
            u_ex = clamp_upk(k, ex); u_r2 = clamp_upk(k, r2); u_r4 = clamp_upk(k, r4)
            e_rk2 = abs(u_r2 - u_ex)
            e_rk4 = abs(u_r4 - u_ex)
            gap   = abs(u_r2 - u_r4)
            if e_rk2 > wr2: wr2 = e_rk2
            if e_rk4 > wr4: wr4 = e_rk4
            if gap  > wgap: wgap = gap; loc = (k, ez, e_rk2, e_rk4, emb)
            rows.append((k, ez, e_rk2, e_rk4, gap, emb))
    print(f"  dt = {dt:.4e}  (CFL 估計 = {cfl_dt():.4e})")
    print(f"  max |RK2 departure − exact|  = {wr2:.3e} cells   ← 現行 RK2 誤差")
    print(f"  max |RK4 departure − exact|  = {wr4:.3e} cells   ← 升級 RK4 誤差")
    print(f"  max |RK2 − RK4|  (r,s) gap   = {wgap:.3e} cells   ← 精度增益 / 使用者要的 (r,s) 誤差")
    if loc:
        k, ez, e2, e4, emb = loc
        print(f"    最壞 gap @ k={k} (壁距 {k-3} cells), ez={ez:+.0f}: "
              f"RK2 err={e2:.3e}, RK4 err={e4:.3e}, RK4 嵌入式自證={emb:.3e}")
    if profile:
        print("\n  近壁剖面 (k, ez, RK2_err, RK4_err, gap):")
        for (k, ez, e2, e4, g, emb) in rows[:16]:
            print(f"    k={k:3d} ez={ez:+.0f}  RK2={e2:.3e}  RK4={e4:.3e}  gap={g:.3e}")
    return wr2, wr4, wgap

# ════════════════════════════════════════════════════════════════════════════
#  完整 2D × 9 方向模式 (--metric-file): 讀 solver GILBM_DUMP_METRIC dump 的真實
#  含-ghost 6 階 FD metric, 對每個內部 (j,k) × 9 class (ey,ez) 比 RK2 vs RK4 的
#  departure (t_xi,t_zeta) gap。含 ey·zeta_y 流向交叉項 (1D 版略過)。
#  RK4 自適應收斂 = 高精度參考; gap(RK2 vs RK4) = RK2 誤差。bk 在 t_zeta gap 中相消, 不需。
# ════════════════════════════════════════════════════════════════════════════
import struct
try:
    import numpy as _np
except ImportError:
    _np = None

# 9-class (ey,ez) — gilbm2_class_velocity (precompute2.h); class 0/1/2 之 eta-only 與 inert 另計
_CLASS_V = {1:(1.0,0.0), 2:(-1.0,0.0), 3:(0.0,1.0), 4:(0.0,-1.0),
            5:(1.0,1.0), 6:(-1.0,1.0), 7:(1.0,-1.0), 8:(-1.0,-1.0)}

def read_metric_file(path):
    with open(path, "rb") as f:
        nyd6, nz6, rank = struct.unpack("3i", f.read(12))
        dt = struct.unpack("d", f.read(8))[0]
        n2d = nyd6 * nz6
        rd = lambda: _np.frombuffer(f.read(8 * n2d), dtype=_np.float64).reshape(nyd6, nz6).copy()
        xi_y, xi_z, zeta_y, zeta_z = rd(), rd(), rd(), rd()
    return nyd6, nz6, rank, dt, (xi_y, xi_z, zeta_y, zeta_z)

def _sample2d(pj, pk, ey, ez, NYD6, NZ6, met):
    xi_y, xi_z, zeta_y, zeta_z = met
    if pj < 0.0:        pj = 0.0
    if pj > NYD6 - 1:   pj = float(NYD6 - 1)
    if pk < 3.0:        pk = 3.0
    if pk > NZ6 - 4:    pk = float(NZ6 - 4)
    sj = int(math.floor(pj)) - 3
    if sj < 0: sj = 0
    if sj + 6 > NYD6 - 1: sj = NYD6 - 7
    sk = int(math.floor(pk)) - 3
    if sk < 0: sk = 0
    if sk + 6 > NZ6 - 1:  sk = NZ6 - 7
    aj = lagrange7(pj - sj); ak = lagrange7(pk - sk)
    vxi = vze = 0.0
    for mj in range(7):
        jj = sj + mj; axi = aze = 0.0
        for mk in range(7):
            kk = sk + mk; w = ak[mk]
            axi += w * (ey * xi_y[jj, kk] + ez * xi_z[jj, kk])
            aze += w * (ey * zeta_y[jj, kk] + ez * zeta_z[jj, kk])
        vxi += aj[mj] * axi; vze += aj[mj] * aze
    return vxi, vze

def _rk4step2d(pj, pk, h, ey, ez, dims, met):
    v1j, v1k = _sample2d(pj, pk, ey, ez, *dims, met)
    v2j, v2k = _sample2d(pj - 0.5*h*v1j, pk - 0.5*h*v1k, ey, ez, *dims, met)
    v3j, v3k = _sample2d(pj - 0.5*h*v2j, pk - 0.5*h*v2k, ey, ez, *dims, met)
    v4j, v4k = _sample2d(pj - h*v3j,     pk - h*v3k,     ey, ez, *dims, met)
    return (h/6.0)*(v1j+2*v2j+2*v3j+v4j), (h/6.0)*(v1k+2*v2k+2*v3k+v4k)

def _rk2disp2d(j, k, ey, ez, dt, dims, met):
    v0j, v0k = _sample2d(float(j), float(k), ey, ez, *dims, met)
    vmj, vmk = _sample2d(j - 0.5*dt*v0j, k - 0.5*dt*v0k, ey, ez, *dims, met)
    return dt*vmj, dt*vmk

def _rk4disp2d(j, k, ey, ez, dt, dims, met):
    cj, ck = _rk4step2d(float(j), float(k), dt, ey, ez, dims, met)   # N=1 baseline
    fj, fk, emax = cj, ck, 1e300
    for level in range(1, MAXDEPTH + 1):
        N = 1 << level; h = dt / N; qj, qk, sj, sk = float(j), float(k), 0.0, 0.0
        for _ in range(N):
            dj, dk = _rk4step2d(qj, qk, h, ey, ez, dims, met)
            sj += dj; sk += dk; qj -= dj; qk -= dk
        emax = max(abs(sj - cj), abs(sk - ck)) / 15.0
        fj, fk = sj + (sj - cj)/15.0, sk + (sk - ck)/15.0
        cj, ck = sj, sk
        if emax < ERRTOL:
            break
    return fj, fk, emax

def run_metric_file(paths, jstride=8):
    if _np is None:
        print("  需要 numpy (pip install numpy)"); return
    wgap = 0.0; worst = None; npts = 0
    for path in paths:
        nyd6, nz6, rank, dt, met = read_metric_file(path)
        dims = (nyd6, nz6)
        print(f"  [{path}] rank={rank} NYD6={nyd6} NZ6={nz6} dt={dt:.4e}")
        for j in range(3, nyd6 - 3, jstride):            # j-stride (流向近均勻, 加速)
            for k in range(3, nz6 - 3):                  # full k (誤差集中壁面)
                for cls, (ey, ez) in _CLASS_V.items():
                    d2j, d2k = _rk2disp2d(j, k, ey, ez, dt, dims, met)
                    d4j, d4k, _e = _rk4disp2d(j, k, ey, ez, dt, dims, met)
                    txi2 = min(6.0, max(0.0, 3.0 - d2j)); txi4 = min(6.0, max(0.0, 3.0 - d4j))
                    upk2 = min(nz6 - 4.0, max(3.0, k - d2k)); upk4 = min(nz6 - 4.0, max(3.0, k - d4k))
                    gap = max(abs(txi2 - txi4), abs(upk2 - upk4))
                    npts += 1
                    if gap > wgap:
                        wgap = gap; worst = (rank, j, k, cls, ey, ez)
    print(f"  掃描 {npts} 點 (j-stride={jstride}, full-k, 9 class)")
    print(f"  ★ max |RK2 − RK4| (r,s) gap = {wgap:.3e} cells")
    if worst:
        r, j, k, cls, ey, ez = worst
        print(f"    最壞 @ rank={r} j={j} k={k} (壁距 {k-3}) class={cls} (ey={ey:+.0f}, ez={ez:+.0f})")
    return wgap

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dt", type=float, default=5.073e-4, help="solver dt_global")
    ap.add_argument("--sweep", action="store_true", help="dt 收斂掃描 (單步 departure: RK2 局部 O(dt^3), RK4 round-off 地板)")
    ap.add_argument("--profile", action="store_true", help="印近壁剖面")
    ap.add_argument("--metric-file", nargs="+", default=None,
                    help="完整 2D×9方向: 讀 solver dump gilbm_metric_full_r*.bin (export GILBM_DUMP_METRIC=1 跑 Edit10 產生)")
    ap.add_argument("--jstride", type=int, default=8, help="2D 模式 j 取樣間隔 (流向近均勻; --jstride 1 = 完整逐格)")
    a = ap.parse_args()
    if a.metric_file:
        print("=" * 72)
        print("  GILBM departure (r,s) 誤差 RK2 vs RK4 — 完整 2D × 9 方向 (真實 FD6 含-ghost metric)")
        print("=" * 72)
        run_metric_file(a.metric_file, jstride=a.jstride)
        return
    print("=" * 72)
    print("  GILBM departure (r,s) 誤差: RK2 vs RK4  (壁法向 Vinokur-tanh, NZ=321, a=0.95)")
    print("=" * 72)
    run(a.dt, profile=a.profile)
    if a.sweep:
        print("\n  dt 收斂掃描 (單步 departure 局部階數: RK2 ~O(dt^3), RK4 在 FP64 round-off 地板 ~常數):")
        print("   dt          max_RK2_err   slope   max_RK4_err   slope")
        prev2 = prev4 = None
        for f in (1.0, 0.5, 0.25, 0.125):
            dt = a.dt * f
            w2 = w4 = 0.0
            for k in range(3, NZ6 - 3):
                for ez in (+1.0, -1.0):
                    ex = exact_displacement(k, ez, dt)
                    e2 = abs(clamp_upk(k, rk2_displacement(k, ez, dt)) - clamp_upk(k, ex))
                    r4, _ = rk4_displacement(k, ez, dt)
                    e4 = abs(clamp_upk(k, r4) - clamp_upk(k, ex))
                    w2 = max(w2, e2); w4 = max(w4, e4)
            s2 = "" if prev2 is None else f"{math.log(prev2/w2)/math.log(2.0):5.2f}"
            s4 = "" if prev4 is None or w4 == 0 else f"{math.log(prev4/w4)/math.log(2.0):5.2f}"
            print(f"   {dt:.3e}   {w2:.3e}   {s2:>5}   {w4:.3e}   {s4:>5}")
            prev2, prev4 = w2, w4

if __name__ == "__main__":
    main()
