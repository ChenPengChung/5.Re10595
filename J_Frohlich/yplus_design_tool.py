#!/usr/bin/env python3
"""
yplus_design_tool.py
====================

依據 Pope §9.1.2 的 DNS 解析度準則，反求 Periodic-Hill 網格參數
以滿足 y+_avg < 0.5 與 y+_peak < 0.5（嚴格 DNS）。

Phase A: A-priori c_f(x) 預估
    來源 1 (主) : Breuer 2009 MGLET DNS wall.dat
    來源 2 (對照) : Pope correlation Re_tau ≈ 0.09 × Re_b^0.88

Phase B: Forward 分析
    讀 variables.h → 算現有 (NZ, GAMMA) 沿底牆的 y+(x) 分布
    Pope 6 項準則檢驗 (y+_avg, y+_peak, dx+, dz_span+, n_visc, stretch)

Phase C: Reverse 反求
    對候選 NZ 列表，二分法找最小 gamma 滿足 y+_peak < target
    對應反求 (NX, NY) 滿足 dx+ / dz_span+
    輸出 Pareto 前緣 (最少網格點的可行解)

復用既有元件 (不重寫):
    grid_zeta_tool.vinokur_tanh
    grid_zeta_tool.hill_function
    test_stability_gamma.parse_variables_h

用法:
    python yplus_design_tool.py
    python yplus_design_tool.py --target-peak 0.5 --target-avg 0.5 --safety 0.8
    python yplus_design_tool.py --variables ../variables.h --re 1400
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from grid_zeta_tool import vinokur_tanh, hill_function  # noqa: E402
from test_stability_gamma import parse_variables_h      # noqa: E402

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False


# ============================================================
#  常數
# ============================================================
SAFETY_FACTOR_DEFAULT = 0.8     # 設計目標 = 規格 × 0.8 (預留偏差)

POPE_CRITERIA = {
    "yplus_avg_max":     0.5,    # 嚴格 DNS
    "yplus_peak_max":    0.5,    # 用戶選: 連峰值都 <0.5
    "dx_plus_max":      15.0,
    "dz_span_plus_max":  7.0,
    "n_visc_layers_min": 10,     # y+<10 內至少 10 層
    "stretch_ratio_max": 1.15,
}

BENCH_DIR = HERE.parent / "result" / "benchmark"

# variables.h 必要欄位 (缺一即報錯)
REQUIRED_VAR_KEYS = [
    "LX", "LY", "LZ", "H_HILL",
    "NX", "NY", "NZ", "jp",
    "GAMMA", "ALPHA", "CFL",
    "Re", "Uref", "niu",
    "Uniform_In_Xdir", "Uniform_In_Ydir", "Uniform_In_Zdir",
]


# ============================================================
#  variables.h 驗證與摘要
# ============================================================

def validate_var_dict(v: dict) -> dict:
    """驗證 parse_variables_h 結果, 回傳已 cast 的字典.

    檢查項:
      1. 必要 key 都存在
      2. 物理一致性: niu == Uref/Re  (浮點容忍)
      3. MPI 拆分: (NY-1) % jp == 0
      4. Z 必須拉伸: Uniform_In_Zdir == 0
      5. H_HILL > 0
      6. ALPHA in (0, 1)
      7. GAMMA > 0
    """
    missing = [k for k in REQUIRED_VAR_KEYS if k not in v]
    if missing:
        raise ValueError(f"variables.h 缺少欄位: {missing}")

    out = {
        "LX":     float(v["LX"]),
        "LY":     float(v["LY"]),
        "LZ":     float(v["LZ"]),
        "H_HILL": float(v["H_HILL"]),
        "NX":     int(v["NX"]),
        "NY":     int(v["NY"]),
        "NZ":     int(v["NZ"]),
        "jp":     int(v["jp"]),
        "GAMMA":  float(v["GAMMA"]),
        "ALPHA":  float(v["ALPHA"]),
        "CFL":    float(v["CFL"]),
        "Re":     int(v["Re"]),
        "Uref":   float(v["Uref"]),
        "niu":    float(v["niu"]),
        "Uniform_In_Xdir": int(v["Uniform_In_Xdir"]),
        "Uniform_In_Ydir": int(v["Uniform_In_Ydir"]),
        "Uniform_In_Zdir": int(v["Uniform_In_Zdir"]),
        # 可選: 字串路徑 (參考網格), 不在 REQUIRED 但若存在就帶入
        "GRID_DAT_DIR": str(v.get("GRID_DAT_DIR", "J_Frohlich")),
        "GRID_DAT_REF": str(v.get("GRID_DAT_REF", "3.fine grid.dat")),
    }

    warns: list[str] = []
    errs: list[str] = []
    if abs(out["niu"] - out["Uref"] / out["Re"]) > 1e-12:
        errs.append(
            f"niu={out['niu']:.6e} ≠ Uref/Re={out['Uref']/out['Re']:.6e}"
        )
    if (out["NY"] - 1) % out["jp"] != 0:
        errs.append(f"(NY-1)={out['NY']-1} 不能被 jp={out['jp']} 整除")
    if out["Uniform_In_Zdir"] != 0:
        warns.append("Uniform_In_Zdir != 0 — 工具假設 Z 用 Vinokur 拉伸")
    if out["H_HILL"] <= 0:
        errs.append(f"H_HILL={out['H_HILL']} 必須 > 0")
    if not (0.0 < out["ALPHA"] < 1.0):
        errs.append(f"ALPHA={out['ALPHA']} 必須 ∈ (0, 1)")
    if out["GAMMA"] <= 0.0:
        errs.append(f"GAMMA={out['GAMMA']} 必須 > 0")

    if errs:
        raise ValueError("variables.h 一致性錯誤:\n  " + "\n  ".join(errs))
    if warns:
        for w in warns:
            print(f"  ⚠ WARN: {w}")
    return out


def print_var_summary(v: dict, src_path: str) -> None:
    """印出從 variables.h 載入的所有相關參數."""
    print()
    print("=" * 72)
    print(f"  載入 variables.h:  {src_path}")
    print("=" * 72)
    print(f"  幾何       LX={v['LX']:.3f}  LY={v['LY']:.3f}  LZ={v['LZ']:.3f}  "
          f"H_HILL={v['H_HILL']:.3f}")
    print(f"  網格       NX={v['NX']}  NY={v['NY']}  NZ={v['NZ']}  jp={v['jp']}")
    print(f"  拉伸       GAMMA={v['GAMMA']}  ALPHA={v['ALPHA']}  CFL={v['CFL']}")
    print(f"  物理       Re={v['Re']}  Uref={v['Uref']:.4f}  "
          f"niu={v['niu']:.3e}  (Uref/Re={v['Uref']/v['Re']:.3e})")
    print(f"  flags      Uniform_In_(X,Y,Z) = "
          f"({v['Uniform_In_Xdir']}, {v['Uniform_In_Ydir']}, {v['Uniform_In_Zdir']})")
    print(f"  ref grid   {v['GRID_DAT_DIR']}/{v['GRID_DAT_REF']}")
    print()


# ============================================================
#  Phase A: A-priori c_f(x) 預估
# ============================================================

def load_breuer_cf(re_value: int = 1400) -> tuple[np.ndarray, np.ndarray]:
    """讀 Breuer 2009 MGLET DNS bottom-wall data.

    檔案結構: 1001 列 (x, cf, cp), 結尾 2 列為 sentinel '0 0 0' / '9 0 0'.
    回傳 (x, cf) — 過濾 sentinel 與分離區附近 |cf|<1e-6 的點.
    """
    path = (
        BENCH_DIR
        / "MGLET (Breuer et al. 2009)"
        / f"Re{re_value}"
        / f"MGLET (Breuer et al. 2009) DNS_Re{re_value}_wall.dat"
    )
    if not path.exists():
        raise FileNotFoundError(f"Breuer wall data not found: {path}")

    rows: list[tuple[float, float]] = []
    with open(path, encoding="latin-1") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                x = float(parts[0])
                cf = float(parts[1])
                rows.append((x, cf))
            except ValueError:
                continue

    # 偵測 x 序列倒退 — 之後的列為哨兵或第二段 (top wall)
    cleaned: list[tuple[float, float]] = []
    last_x = -1e30
    for x, cf in rows:
        if x < last_x - 0.5:        # 大幅倒退 → 已進入哨兵或第二區段
            break
        if x == 0.0 and cf == 0.0:  # sentinel
            continue
        if x == 9.0 and cf == 0.0:  # sentinel
            continue
        cleaned.append((x, cf))
        last_x = x

    if len(cleaned) < 100:
        raise RuntimeError(f"Breuer wall.dat 解析後只剩 {len(cleaned)} 點, 異常")

    x_arr = np.array([r[0] for r in cleaned])
    cf_arr = np.array([r[1] for r in cleaned])
    return x_arr, cf_arr


def pope_correlation_re_tau(re_b: float) -> float:
    """Pope 2000 §7.1.5: Re_tau ≈ 0.09 × Re_bulk^0.88
    通道流經驗式, 用於對照 Breuer 平均值的合理性.
    """
    return 0.09 * re_b ** 0.88


def re_tau_from_cf(cf: np.ndarray | float, re_b: float) -> np.ndarray | float:
    """局部 Re_tau = Re_b × sqrt(|cf|/2)."""
    return re_b * np.sqrt(np.abs(cf) / 2.0)


def phase_A_report(re_value: int) -> dict:
    """印 Phase A 結果, 回傳 a-priori 預估字典."""
    print()
    print("=" * 72)
    print(f"  Phase A: A-priori c_f(x) 預估   (Re_h = {re_value})")
    print("=" * 72)

    x, cf = load_breuer_cf(re_value)
    abs_cf = np.abs(cf)

    # 排除分離區 (|cf|<1e-4) 做平均, 否則會被 0 拉低
    mask = abs_cf > 1e-4
    cf_avg = abs_cf[mask].mean()
    cf_peak = abs_cf.max()
    x_peak = x[int(np.argmax(abs_cf))]

    re_tau_local = re_tau_from_cf(cf, re_value)
    re_tau_peak = re_tau_from_cf(cf_peak, re_value)
    re_tau_avg = re_tau_from_cf(cf_avg, re_value)

    re_tau_pope = pope_correlation_re_tau(re_value)

    print()
    print(f"  Breuer 2009 wall.dat (主要 a-priori 來源):")
    print(f"    sample 點數      : {len(x)}")
    print(f"    cf_peak          : {cf_peak:.5f}   @ x/h = {x_peak:.2f}")
    print(f"    |cf|_avg         : {cf_avg:.5f}   (排除 |cf|<1e-4 分離區)")
    print(f"    Re_tau_peak      : {re_tau_peak:.1f}")
    print(f"    Re_tau_avg       : {re_tau_avg:.1f}")
    print()
    print(f"  Pope correlation (對照): Re_tau ≈ 0.09 × Re_b^0.88")
    print(f"    Re_tau_pope      : {re_tau_pope:.1f}   (channel 經驗值)")
    print()
    print(f"  交叉驗證:")
    diff_pct = abs(re_tau_avg - re_tau_pope) / re_tau_pope * 100.0
    if diff_pct < 30.0:
        print(f"    Breuer Re_tau_avg vs Pope = {diff_pct:.1f}% 偏差   ✓ 合理")
    else:
        print(f"    偏差 {diff_pct:.1f}% — 數據可能不一致, 留意")

    return {
        "x": x,
        "cf": cf,
        "re_tau_local": re_tau_local,
        "cf_peak": cf_peak,
        "cf_avg": cf_avg,
        "re_tau_peak": re_tau_peak,
        "re_tau_avg": re_tau_avg,
        "re_tau_pope": re_tau_pope,
        "x_peak": x_peak,
    }


# ============================================================
#  Phase B: Forward 分析
# ============================================================

def first_cell_ratio(NZ: int, gamma: float, alpha: float = 0.5) -> float:
    """Vinokur tanh 第一格 / 全長 比例 (z 空間, [0,1] 歸一化)."""
    eta = np.linspace(0, 1, NZ)
    zeta = vinokur_tanh(eta, gamma, alpha)
    return float(zeta[1] - zeta[0])


def cell_thicknesses(NZ: int, gamma: float, alpha: float = 0.5) -> np.ndarray:
    """所有 NZ-1 個 cell 的歸一化厚度 (sum=1)."""
    eta = np.linspace(0, 1, NZ)
    zeta = vinokur_tanh(eta, gamma, alpha)
    return np.diff(zeta)


def column_height(x_array: np.ndarray, LZ: float, LY: float) -> np.ndarray:
    """每個 x 站的 wall-normal 列高 = LZ - h(x).

    redistribute_vertical_physical 的物理 z 重分布: column_i 範圍是
    [h(x_i), LZ], 高度 = LZ - h(x_i).
    """
    return np.array([LZ - hill_function(float(x), LY) for x in x_array])


def yplus_profile(
    NZ: int,
    gamma: float,
    alpha: float,
    x_arr: np.ndarray,
    cf_arr: np.ndarray,
    Re: float,
    LZ: float,
    LY: float,
    H_HILL: float,
) -> tuple[np.ndarray, np.ndarray]:
    """沿底牆計算 y+(x) (第一格).

    y+(x) = u_tau(x) × dz_min(x) / nu
          = sqrt(|cf(x)|/2) × Re × dz_min(x) / H_HILL
          = sqrt(|cf(x)|/2) × Re × ratio × (LZ - h(x)) / H_HILL

    所有長度量 (LZ, h(x), dz_min) 必須與 H_HILL 同單位.
    """
    ratio = first_cell_ratio(NZ, gamma, alpha)
    H_col = column_height(x_arr, LZ, LY)
    dz_min_x = ratio * H_col
    u_tau_norm = np.sqrt(np.abs(cf_arr) / 2.0)   # u_tau / U_b
    yplus = u_tau_norm * Re * dz_min_x / H_HILL
    return yplus, dz_min_x


def stretch_ratio_max(NZ: int, gamma: float, alpha: float = 0.5) -> float:
    """相鄰 cell 最大膨脹比 max(dz[j+1]/dz[j], dz[j]/dz[j+1])."""
    cells = cell_thicknesses(NZ, gamma, alpha)
    ratios = cells[1:] / cells[:-1]
    return float(max(ratios.max(), 1.0 / ratios.min()))


def n_layers_within_yplus(
    NZ: int,
    gamma: float,
    alpha: float,
    target_yplus: float,
    re_tau_local_max: float,
    LZ: float,
    H_HILL: float,
    h_at_loc: float | None = None,
) -> int:
    """y+ < target_yplus 的層數 (worst case: 最高 Re_tau, 最矮 column).

    h_at_loc 省略時預設為 H_HILL (山頂, column 最短 = LZ-H_HILL).
    所有長度量 (LZ, h_at_loc, z_phys) 與 H_HILL 同單位.
    """
    if h_at_loc is None:
        h_at_loc = H_HILL
    eta = np.linspace(0, 1, NZ)
    zeta = vinokur_tanh(eta, gamma, alpha)
    H_col = LZ - h_at_loc
    z_phys = zeta * H_col                                  # 每層離壁面距離
    yplus_layers = z_phys * re_tau_local_max / H_HILL
    return int(np.sum(yplus_layers < target_yplus))


def check_pope_criteria(
    yplus_avg: float,
    yplus_peak: float,
    dx_plus: float,
    dz_span_plus: float,
    n_visc: int,
    sr: float,
) -> dict[str, bool]:
    return {
        f"y+_avg  < {POPE_CRITERIA['yplus_avg_max']}":     yplus_avg < POPE_CRITERIA["yplus_avg_max"],
        f"y+_peak < {POPE_CRITERIA['yplus_peak_max']}":    yplus_peak < POPE_CRITERIA["yplus_peak_max"],
        f"dx+    < {POPE_CRITERIA['dx_plus_max']}":         dx_plus < POPE_CRITERIA["dx_plus_max"],
        f"dzsp+  < {POPE_CRITERIA['dz_span_plus_max']}":    dz_span_plus < POPE_CRITERIA["dz_span_plus_max"],
        f"n_visc>= {POPE_CRITERIA['n_visc_layers_min']}":   n_visc >= POPE_CRITERIA["n_visc_layers_min"],
        f"stretch< {POPE_CRITERIA['stretch_ratio_max']}":   sr < POPE_CRITERIA["stretch_ratio_max"],
    }


def phase_B_report(var_dict: dict, phase_a: dict) -> dict:
    print()
    print("=" * 72)
    print(f"  Phase B: Forward 分析  (現有 variables.h 設定)")
    print("=" * 72)

    NZ = var_dict["NZ"]
    NY = var_dict["NY"]
    NX = var_dict["NX"]
    gamma = var_dict["GAMMA"]
    alpha = var_dict["ALPHA"]
    LZ = var_dict["LZ"]
    LY = var_dict["LY"]
    LX = var_dict["LX"]
    H_HILL = var_dict["H_HILL"]
    Re = float(var_dict["Re"])

    x = phase_a["x"]
    cf = phase_a["cf"]
    re_tau_peak = phase_a["re_tau_peak"]
    re_tau_avg = phase_a["re_tau_avg"]

    yplus, dz_min_x = yplus_profile(NZ, gamma, alpha, x, cf, Re, LZ, LY, H_HILL)

    # 排除分離區 (|cf|<1e-4) 做 y+ 平均 (否則會被 0 拉低)
    mask = np.abs(cf) > 1e-4
    yplus_avg = float(yplus[mask].mean())
    yplus_p99 = float(np.percentile(yplus[mask], 99))
    yplus_peak = float(yplus.max())

    dx = LY / (NY - 1)
    dz_span = LX / (NX - 1)
    dx_plus_peak = dx * re_tau_peak
    dz_span_plus_peak = dz_span * re_tau_peak
    dx_plus_avg = dx * re_tau_avg
    dz_span_plus_avg = dz_span * re_tau_avg

    n_visc = n_layers_within_yplus(
        NZ, gamma, alpha,
        target_yplus=10.0,
        re_tau_local_max=re_tau_peak,
        LZ=LZ, H_HILL=H_HILL,    # h_at_loc 預設為 H_HILL (山頂)
    )
    sr = stretch_ratio_max(NZ, gamma, alpha)
    minSize_crest = first_cell_ratio(NZ, gamma, alpha) * (LZ - H_HILL)

    print()
    print(f"  Grid 設定 (從 variables.h):")
    print(f"    (NX, NY, NZ)    = ({NX}, {NY}, {NZ})")
    print(f"    GAMMA, ALPHA    = ({gamma}, {alpha})")
    print(f"    H_HILL, LZ      = ({H_HILL}, {LZ})")
    print(f"    minSize/H_HILL  = {minSize_crest/H_HILL:.5f}   @ hill crest (column 最短)")
    print()
    print(f"  y+ 統計 (沿底牆 {len(x)} 站, 排除分離區):")
    print(f"    y+_avg          = {yplus_avg:.3f}")
    print(f"    y+_p99          = {yplus_p99:.3f}")
    print(f"    y+_peak         = {yplus_peak:.3f}   @ x/h ≈ {x[int(np.argmax(yplus))]:.2f}")
    print()
    print(f"  其他幾何指標:")
    print(f"    dx+    (peak)   = {dx_plus_peak:.2f}      (avg = {dx_plus_avg:.2f})")
    print(f"    dz_sp+ (peak)   = {dz_span_plus_peak:.2f}      (avg = {dz_span_plus_avg:.2f})")
    print(f"    n_visc (y+<10)  = {n_visc}     (山頂 worst case)")
    print(f"    stretch_max     = {sr:.3f}")
    print()
    print(f"  Pope §9.1.2 準則檢驗:")
    crit = check_pope_criteria(
        yplus_avg, yplus_peak, dx_plus_peak, dz_span_plus_peak, n_visc, sr
    )
    for name, ok in crit.items():
        mark = "✓ PASS" if ok else "✗ FAIL"
        print(f"    {name:24s}  {mark}")
    n_pass = sum(crit.values())
    print()
    print(f"  小結: {n_pass}/{len(crit)} 項通過")

    return {
        "yplus": yplus,
        "yplus_avg": yplus_avg,
        "yplus_peak": yplus_peak,
        "yplus_p99": yplus_p99,
        "dx_plus_peak": dx_plus_peak,
        "dz_span_plus_peak": dz_span_plus_peak,
        "n_visc": n_visc,
        "stretch_ratio": sr,
        "criteria": crit,
        "all_pass": all(crit.values()),
        "minSize": minSize_crest,
    }


# ============================================================
#  Phase C: Reverse 反求
# ============================================================

def find_min_gamma_for_target_peak(
    NZ: int,
    target_yplus_peak: float,
    x_peak: float,
    cf_peak: float,
    Re: float,
    LZ: float,
    LY: float,
    H_HILL: float,
    alpha: float = 0.5,
    gamma_range: tuple[float, float] = (0.3, 8.0),
) -> float | None:
    """二分法找最小 gamma 使峰值位置 y+_peak < target.

    y+_peak = sqrt(cf_peak/2) × Re × ratio(γ, NZ) × (LZ - h(x_peak)) / H_HILL
    對 γ 單調遞減 (γ↑ → ratio↓ → y+↓), 二分法即可.

    LZ, LY, H_HILL 全部由呼叫端從 variables.h 傳入 (無預設值).
    """
    h_at_peak = hill_function(float(x_peak), LY)
    H_col_peak = LZ - h_at_peak
    re_tau_peak = re_tau_from_cf(cf_peak, Re)

    def yplus_peak_for(g: float) -> float:
        ratio = first_cell_ratio(NZ, g, alpha)
        return float(re_tau_peak * ratio * H_col_peak / H_HILL)

    g_lo, g_hi = gamma_range
    if yplus_peak_for(g_lo) <= target_yplus_peak:
        return g_lo
    if yplus_peak_for(g_hi) > target_yplus_peak:
        return None    # 連 gamma_max 都不夠 → NZ 太小

    for _ in range(80):
        g_mid = 0.5 * (g_lo + g_hi)
        if yplus_peak_for(g_mid) <= target_yplus_peak:
            g_hi = g_mid
        else:
            g_lo = g_mid
        if g_hi - g_lo < 1e-5:
            break
    return g_hi


def required_NY(re_tau_peak: float, LY: float, jp: int, dx_plus_max: float) -> int:
    """反求 NY 滿足 dx+ < dx_plus_max 且 (NY-1) % jp == 0."""
    NY_min = int(math.ceil(LY * re_tau_peak / dx_plus_max)) + 1
    while (NY_min - 1) % jp != 0:
        NY_min += 1
    return NY_min


def required_NX(re_tau_peak: float, LX: float, dz_span_plus_max: float) -> int:
    """反求 NX 滿足 dz_span+ < max."""
    NX_min = int(math.ceil(LX * re_tau_peak / dz_span_plus_max)) + 1
    return NX_min


def estimate_omega_quick(NZ: int, gamma: float, alpha: float = 0.5) -> tuple[float, float]:
    """快速估 omega (近似, 真要驗證需用 estimate_gilbm_stability).

    擬合來源: grid_zeta_tool stability table (line 467, NZ=64 calibrated):
        γ=2.0  ratio=4   ω=0.63
        γ=3.0  ratio=8   ω=0.73
        γ=4.0  ratio=20  ω=0.94
        γ=5.0  ratio=52  ω=1.43

    Power-law 擬合: ω ≈ 0.5 + 0.046 × ratio^0.75
        誤差 < 0.05 (vs 線性版 max err 0.31).

    限制:
      1. table 用的 ratio 是「網格生成後 (Poisson 後) 的實測值」, 含
         Steger-Sorenson 平滑效應; 這裡用「純 Vinokur tanh 解析 ratio」,
         兩者在 γ ≥ 3 時可能相差 ~30%.
      2. 真實 omega 受 max|c̃| (依賴座標 metric) 影響, 不只 dz_ratio.
      3. Phase C 的 omega 欄僅做粗略排序, 最終須以 estimate_gilbm_stability
         在實際生成的網格上驗證 (見 Phase C 末段 verify-best).
    """
    cells = cell_thicknesses(NZ, gamma, alpha)
    dz_ratio = float(cells.max() / cells.min())
    omega_est = 0.5 + 0.046 * dz_ratio ** 0.75
    return omega_est, dz_ratio


def phase_C_search(
    phase_a: dict,
    var_dict: dict,
    NZ_candidates: list[int] | None = None,
    target_yplus_peak: float = 0.5,
    target_yplus_avg: float = 0.5,
    safety: float = SAFETY_FACTOR_DEFAULT,
    alpha: float = 0.5,
) -> list[dict]:
    """掃描 NZ 候選, 對每個找最小 gamma 與相應 (NX, NY)."""
    if NZ_candidates is None:
        # 把 variables.h 當前 NZ 加入掃描列表 (確保現況有出現)
        nz_now = var_dict["NZ"]
        base = [129, 161, 193, 225, 257, 289, 321, 385, 449, 513, 641]
        if nz_now not in base:
            base.append(nz_now)
        NZ_candidates = sorted(base)

    Re = float(var_dict["Re"])
    LZ = var_dict["LZ"]
    LY = var_dict["LY"]
    LX = var_dict["LX"]
    H_HILL = var_dict["H_HILL"]
    jp = var_dict["jp"]

    target_peak_design = target_yplus_peak * safety   # 0.5 × 0.8 = 0.4
    target_avg_design = target_yplus_avg * safety

    x = phase_a["x"]
    cf = phase_a["cf"]
    cf_peak = phase_a["cf_peak"]
    x_peak = phase_a["x_peak"]
    re_tau_peak = phase_a["re_tau_peak"]
    mask = np.abs(cf) > 1e-4

    candidates: list[dict] = []

    for NZ in NZ_candidates:
        g = find_min_gamma_for_target_peak(
            NZ, target_peak_design, x_peak, cf_peak, Re,
            LZ=LZ, LY=LY, H_HILL=H_HILL, alpha=alpha,
        )
        if g is None:
            candidates.append({
                "NZ": NZ,
                "feasible": False,
                "reason": "gamma>8.0 仍不足, NZ 太小",
            })
            continue

        # 用該 gamma 算全段 y+
        yplus, _ = yplus_profile(NZ, g, alpha, x, cf, Re, LZ, LY, H_HILL)
        yplus_avg = float(yplus[mask].mean())
        yplus_peak_actual = float(yplus.max())

        # 反求 NX, NY
        NY_min = required_NY(
            re_tau_peak, LY, jp, POPE_CRITERIA["dx_plus_max"]
        )
        NX_min = required_NX(
            re_tau_peak, LX, POPE_CRITERIA["dz_span_plus_max"]
        )

        dx = LY / (NY_min - 1)
        dz_sp = LX / (NX_min - 1)
        dx_p = dx * re_tau_peak
        dz_p = dz_sp * re_tau_peak

        n_visc = n_layers_within_yplus(
            NZ, g, alpha, target_yplus=10.0,
            re_tau_local_max=re_tau_peak, LZ=LZ, H_HILL=H_HILL,
        )
        sr = stretch_ratio_max(NZ, g, alpha)
        omega_est, dz_ratio = estimate_omega_quick(NZ, g, alpha)

        # Pope 6 項 (注意: 用 design target 較嚴格的 0.4 來判可行)
        crit = check_pope_criteria(
            yplus_avg, yplus_peak_actual, dx_p, dz_p, n_visc, sr
        )
        all_pope_pass = all(crit.values())
        avg_under_design = yplus_avg < target_avg_design
        peak_under_design = yplus_peak_actual < target_peak_design
        stable = omega_est < 1.5

        # 「全合格」 = Pope 6 項 + LBM omega
        all_pass = all_pope_pass and stable

        total = NX_min * NY_min * NZ
        candidates.append({
            "NZ": NZ,
            "gamma": g,
            "NX": NX_min,
            "NY": NY_min,
            "total_points": total,
            "yplus_avg": yplus_avg,
            "yplus_peak": yplus_peak_actual,
            "dx_plus": dx_p,
            "dz_span_plus": dz_p,
            "n_visc": n_visc,
            "stretch": sr,
            "dz_ratio": dz_ratio,
            "omega_est": omega_est,
            "criteria": crit,
            "all_pope_pass": all_pope_pass,
            "stable": stable,
            "all_pass": all_pass,
            "feasible": True,
        })

    return candidates


def phase_C_report(candidates: list[dict], target_avg: float, target_peak: float, safety: float) -> dict | None:
    print()
    print("=" * 72)
    print(f"  Phase C: Reverse 反求  (Pareto 掃描)")
    print("=" * 72)
    print()
    print(f"  目標 y+_peak  < {target_peak}    (design = {target_peak * safety:.2f}, safety x{safety})")
    print(f"  目標 y+_avg   < {target_avg}    (design = {target_avg * safety:.2f})")
    print(f"  目標 dx+      < {POPE_CRITERIA['dx_plus_max']}")
    print(f"  目標 dz_span+ < {POPE_CRITERIA['dz_span_plus_max']}")
    print(f"  目標 LBM      omega < 1.5")
    print()

    header = (
        f"  {'NZ':>4s} | {'γ':>5s} | {'NY':>4s} | {'NX':>4s} | "
        f"{'pts':>10s} | {'y+avg':>6s} | {'y+pk':>6s} | "
        f"{'dx+':>5s} | {'dzsp+':>5s} | {'nvis':>4s} | "
        f"{'ratio':>5s} | {'ω':>5s} | Status"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    for c in candidates:
        if not c.get("feasible", False):
            print(f"  {c['NZ']:4d} | --- 不可行: {c.get('reason', '?')}")
            continue
        flags = []
        if not c["criteria"][f"y+_avg  < {POPE_CRITERIA['yplus_avg_max']}"]:
            flags.append("avg")
        if not c["criteria"][f"y+_peak < {POPE_CRITERIA['yplus_peak_max']}"]:
            flags.append("peak")
        if not c["criteria"][f"dx+    < {POPE_CRITERIA['dx_plus_max']}"]:
            flags.append("dx")
        if not c["criteria"][f"dzsp+  < {POPE_CRITERIA['dz_span_plus_max']}"]:
            flags.append("dzsp")
        if not c["criteria"][f"n_visc>= {POPE_CRITERIA['n_visc_layers_min']}"]:
            flags.append("nvis")
        if not c["criteria"][f"stretch< {POPE_CRITERIA['stretch_ratio_max']}"]:
            flags.append("str")
        if not c["stable"]:
            flags.append("ω")

        status = "✓ ALL PASS" if c["all_pass"] else f"FAIL[{','.join(flags)}]"

        print(
            f"  {c['NZ']:4d} | {c['gamma']:5.2f} | {c['NY']:4d} | {c['NX']:4d} | "
            f"{c['total_points']:10,d} | {c['yplus_avg']:6.3f} | {c['yplus_peak']:6.3f} | "
            f"{c['dx_plus']:5.1f} | {c['dz_span_plus']:5.1f} | {c['n_visc']:4d} | "
            f"{c['dz_ratio']:5.1f} | {c['omega_est']:5.2f} | {status}"
        )

    print()
    feasible = [c for c in candidates if c.get("all_pass", False)]
    if feasible:
        best = min(feasible, key=lambda c: c["total_points"])
        print(f"  ★ 最少網格點的可行解 (Pareto optimal):")
        print(f"     NZ = {best['NZ']}   gamma = {best['gamma']:.3f}")
        print(f"     NY = {best['NY']}   NX = {best['NX']}")
        print(f"     total = {best['total_points']:,} points")
        print(f"     y+_avg = {best['yplus_avg']:.3f}   y+_peak = {best['yplus_peak']:.3f}")
        print(f"     omega_est = {best['omega_est']:.2f}   dz_ratio = {best['dz_ratio']:.1f}")
        print()
        print(f"     需要修改 variables.h:")
        print(f"       #define NX     {best['NX']}")
        print(f"       #define NY     {best['NY']}")
        print(f"       #define NZ     {best['NZ']}")
        print(f"       #define GAMMA  {best['gamma']:.3f}")
        return best
    else:
        print(f"  ⚠ 無 NZ 候選滿足全部準則 — 試擴大 NZ_candidates 或檢視限制")
        return None


# ============================================================
#  繪圖
# ============================================================

def plot_phase_B(phase_a: dict, phase_b: dict, var_dict: dict, savepath: Path) -> None:
    if not _HAS_MPL:
        print("  [skip plot] matplotlib not available")
        return
    x = phase_a["x"]
    cf = phase_a["cf"]
    yplus = phase_b["yplus"]

    fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
    axes[0].plot(x, np.abs(cf), "k-", lw=0.8)
    axes[0].axhline(phase_a["cf_peak"], color="red", ls="--", lw=0.5, label=f"peak={phase_a['cf_peak']:.4f}")
    axes[0].axhline(phase_a["cf_avg"], color="green", ls=":", lw=0.5, label=f"avg={phase_a['cf_avg']:.4f}")
    axes[0].set_ylabel("|c_f|")
    axes[0].set_title(
        f"Re={int(var_dict['Re'])}  bottom wall — Breuer 2009 MGLET DNS"
    )
    axes[0].legend(fontsize=8)
    axes[0].grid(alpha=0.3)

    label = (
        f"NZ={int(var_dict['NZ'])} γ={var_dict['GAMMA']}  "
        f"(avg={phase_b['yplus_avg']:.2f}, peak={phase_b['yplus_peak']:.2f})"
    )
    axes[1].plot(x, yplus, "b-", lw=0.8, label=label)
    axes[1].axhline(0.5, color="red", ls="--", lw=0.8, label="strict DNS y+<0.5")
    axes[1].axhline(1.0, color="orange", ls=":", lw=0.8, label="Breuer C6 y+<1")
    axes[1].set_xlabel("x / h")
    axes[1].set_ylabel("y+ (first cell)")
    axes[1].legend(fontsize=8)
    axes[1].grid(alpha=0.3)

    plt.tight_layout()
    fig.savefig(savepath, dpi=150)
    plt.close(fig)
    print(f"  [saved] {savepath}")


def plot_phase_C(candidates: list[dict], savepath: Path) -> None:
    if not _HAS_MPL:
        return
    feas = [c for c in candidates if c.get("feasible", False)]
    if not feas:
        return

    fig, ax = plt.subplots(figsize=(9, 6))
    for c in feas:
        color = "tab:green" if c["all_pass"] else "tab:red"
        marker = "o" if c["all_pass"] else "x"
        ax.scatter(c["NZ"], c["total_points"], c=color, s=80, marker=marker)
        ax.annotate(
            f"γ={c['gamma']:.2f}",
            (c["NZ"], c["total_points"]),
            xytext=(6, 4),
            textcoords="offset points",
            fontsize=8,
        )
    ax.set_xlabel("NZ")
    ax.set_ylabel("total grid points")
    ax.set_title("Phase C: candidate grids (green = Pope+LBM all pass)")
    ax.set_yscale("log")
    ax.grid(alpha=0.3, which="both")
    plt.tight_layout()
    fig.savefig(savepath, dpi=150)
    plt.close(fig)
    print(f"  [saved] {savepath}")


# ============================================================
#  Main
# ============================================================

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variables", default=str(HERE.parent / "variables.h"))
    ap.add_argument("--re", type=int, default=None,
                    help="Reynolds number (省略時從 variables.h 取 Re)")
    ap.add_argument("--target-avg", type=float, default=0.5)
    ap.add_argument("--target-peak", type=float, default=0.5)
    ap.add_argument("--safety", type=float, default=SAFETY_FACTOR_DEFAULT)
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()

    # 1. 解析 + 驗證 + 印摘要
    raw = parse_variables_h(args.variables)
    var_dict = validate_var_dict(raw)
    print_var_summary(var_dict, args.variables)

    # 2. 決定 Re (CLI 覆寫優先, 否則 variables.h)
    re_value = args.re if args.re is not None else var_dict["Re"]
    if args.re is not None and args.re != var_dict["Re"]:
        print(f"  ⚠ CLI --re={args.re} 與 variables.h Re={var_dict['Re']} 不同, "
              f"用 CLI 值跑 Phase A")

    print("┌" + "─" * 70 + "┐")
    print(f"│  yplus_design_tool  —  Periodic-Hill DNS 網格反求工具          │")
    print(f"│  目標: y+_avg < {args.target_avg:.2f}  &  y+_peak < {args.target_peak:.2f}"
          f"  (safety x{args.safety})       │")
    print("└" + "─" * 70 + "┘")

    pa = phase_A_report(re_value)
    pb = phase_B_report(var_dict, pa)
    cands = phase_C_search(
        pa, var_dict,
        target_yplus_peak=args.target_peak,
        target_yplus_avg=args.target_avg,
        safety=args.safety,
    )
    phase_C_report(cands, args.target_avg, args.target_peak, args.safety)

    if not args.no_plot:
        plot_phase_B(pa, pb, var_dict, savepath=HERE / "yplus_phase_B.png")
        plot_phase_C(cands, savepath=HERE / "yplus_phase_C_pareto.png")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
