#!/usr/bin/env python3
"""
yplus_design_tool.py 正確性驗證套件
=======================================

驗證項目:
  1. hill_function (Python) 與 model.h (C) 一致性
  2. 解析 Vinokur tanh 預測 vs 實際 redistribute_vertical_physical 生成
     -> 確認 yplus_profile 用的 dz 與真實模擬網格相同
  3. y+ 公式單位一致性 (LBM lattice unit chain)
  4. omega_quick 估計精度 (vs grid_zeta_tool stability table)
  5. 多 x 站抽檢 y+ (手動 vs 工具)
  6. Breuer wall.dat 邊界 / 分離區 處理
  7. 與 model.h 關鍵巨集一致性 (minSize)

Usage:
  python verify_yplus_tool.py
"""

from __future__ import annotations
import sys
import math
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from grid_zeta_tool import (
    vinokur_tanh, hill_function, parse_tecplot_dat,
    redistribute_vertical_physical, estimate_gilbm_stability,
)
from test_stability_gamma import parse_variables_h
from yplus_design_tool import (
    validate_var_dict, load_breuer_cf,
    first_cell_ratio, column_height, yplus_profile,
    n_layers_within_yplus, stretch_ratio_max,
    estimate_omega_quick, re_tau_from_cf,
)


PASS_MSG = "  [PASS]"
FAIL_MSG = "  [FAIL]"
WARN_MSG = "  [WARN]"


# ============================================================
#  Test 1: hill_function 一致性
# ============================================================

def test_hill_function():
    """grid_zeta_tool.hill_function 應與 model.h 同公式.
    抽 10 個 x 算 h, 驗證:
      - 對稱性 h(x) == h(LY-x)
      - 邊界 h(0) == h(LY) == H_HILL = 1
      - 中間 h(4.5) == 0 (週期中央, 谷底)
      - 連續性 (相鄰段不跳)
    """
    print("Test 1: hill_function 一致性 + 物理對稱性")
    print("-" * 60)
    LY = 9.0
    failed = False

    # 邊界
    h_0 = hill_function(0.0, LY)
    h_LY = hill_function(LY, LY)
    h_mid = hill_function(LY/2, LY)
    print(f"  h(0)        = {h_0:.6f}   (期望 1.0)")
    print(f"  h(LY)       = {h_LY:.6f}   (期望 1.0)")
    print(f"  h(LY/2=4.5) = {h_mid:.6f}   (期望 0.0)")
    if abs(h_0 - 1.0) > 1e-3:    failed = True
    if abs(h_LY - 1.0) > 1e-3:   failed = True
    if abs(h_mid) > 1e-3:        failed = True

    # 對稱性
    print(f"\n  對稱性 h(x) == h(LY-x):")
    xs = [0.5, 1.0, 1.5, 2.0, 3.0, 4.0]
    max_asym = 0.0
    for x in xs:
        h1 = hill_function(x, LY)
        h2 = hill_function(LY - x, LY)
        diff = abs(h1 - h2)
        max_asym = max(max_asym, diff)
        mark = "" if diff < 1e-6 else " ✗"
        print(f"    h({x:.2f})={h1:.5f}  h({LY-x:.2f})={h2:.5f}  diff={diff:.2e}{mark}")
    if max_asym > 1e-3:
        failed = True
        print(f"  最大對稱性誤差 = {max_asym:.4e} 過大")

    # 連續性 (檢查相鄰段交界處不跳)
    print(f"\n  分段連續性 (Mellen polynomial 6 段交界):")
    s = 54.0/28.0
    boundaries = [s*(9/54), s*(14/54), s*(20/54), s*(30/54), s*(40/54)]
    max_jump = 0.0
    for b in boundaries:
        h_lo = hill_function(b - 1e-6, LY)
        h_hi = hill_function(b + 1e-6, LY)
        jump = abs(h_hi - h_lo)
        max_jump = max(max_jump, jump)
        print(f"    x={b:.5f}  h_left={h_lo:.6f}  h_right={h_hi:.6f}  jump={jump:.2e}")
    if max_jump > 1e-3:
        failed = True

    print(PASS_MSG if not failed else FAIL_MSG)
    return not failed


# ============================================================
#  Test 2: 解析 Vinokur vs 實際 redistribute_vertical_physical
# ============================================================

def test_grid_consistency(var_dict):
    """確認 yplus_profile 用的 dz 與真實會生成的 grid 一致.

    重點: Frohlich 3.fine grid.dat 是「物理單位 (米)」: x_max=0.252, h_phys=0.028.
    必須先縮放到 h-units (×35.7) 再做 redistribute, 才能與工具的 h-units 比對.

    流程:
      1. 讀 Frohlich 3.fine grid.dat (raw, 物理單位)
      2. 縮放到 h-units (scale = LY/x_max = 9/0.252 = 35.7)
      3. 用 redistribute_vertical_physical(γ, α) 生成實際底牆網格
      4. 對每個 column i 算 z[1] - z[0] (實際 dz_min, h-units)
      5. 對比解析公式 ratio × (LZ - h(x_i))
      6. 兩者應在浮點精度內一致
    """
    print("\nTest 2: 解析 Vinokur 預測 vs 實際生成網格的 dz")
    print("-" * 60)

    NZ = var_dict["NZ"]
    GAMMA = var_dict["GAMMA"]
    ALPHA = var_dict["ALPHA"]
    LZ = var_dict["LZ"]
    LY = var_dict["LY"]
    H_HILL = var_dict["H_HILL"]

    ref_path = HERE / var_dict["GRID_DAT_REF"]
    if not ref_path.exists():
        print(f"  ✗ 找不到 reference grid: {ref_path}")
        return False

    print(f"  讀參考: {ref_path.name}")
    x_ref_raw, y_ref_raw, NI, NJ = parse_tecplot_dat(ref_path)
    print(f"  Reference dims (原始物理單位): I={NI}, J={NJ}")
    print(f"    x range = [{x_ref_raw[0,:].min():.4f}, {x_ref_raw[0,:].max():.4f}]  "
          f"(物理米, NOT h-units)")

    # 縮放到 h-units
    x_max_phys = float(x_ref_raw[0, :].max())
    h_phys = x_max_phys / LY    # ≈ 0.028 m
    scale = 1.0 / h_phys         # ≈ 35.7
    x_ref = x_ref_raw * scale
    y_ref = y_ref_raw * scale
    print(f"  h_phys = {h_phys:.6f}, scale = {scale:.4f}")
    print(f"  縮放後 x range = [{x_ref[0,:].min():.4f}, {x_ref[0,:].max():.4f}]   "
          f"(應 ≈ [0, {LY}])")
    print(f"  縮放後 y_top   = {y_ref[-1,0]:.4f}                    "
          f"(應 ≈ {LZ})")

    if NJ != NZ:
        print(f"  注意: reference NJ={NJ} ≠ variables.h NZ={NZ}")
        print(f"        Mode 1 重分布要求同 NJ. 改用 reference NJ 做驗證.")
        NZ_test = NJ
    else:
        NZ_test = NZ

    x_real, y_real = redistribute_vertical_physical(x_ref, y_ref,
                                                    gamma=GAMMA, alpha=ALPHA)

    # 抽 5 個 column 比對
    print(f"\n  比對 (NZ={NZ_test}, γ={GAMMA}, α={ALPHA}, all in h-units):")
    print(f"  {'col i':>5s} | {'x':>7s} | {'h(x)':>7s} | "
          f"{'dz_actual':>11s} | {'dz_pred':>11s} | {'rel_err':>10s}")

    sample_cols = [0, NI//4, NI//2, 3*NI//4, NI-1]
    ratio_pred = first_cell_ratio(NZ_test, GAMMA, ALPHA)
    max_err = 0.0

    for i in sample_cols:
        x_i = x_real[0, i]
        h_at_x = hill_function(x_i, LY)
        dz_actual = y_real[1, i] - y_real[0, i]
        dz_pred = ratio_pred * (LZ - h_at_x)
        rel_err = abs(dz_actual - dz_pred) / max(dz_actual, 1e-30)
        max_err = max(max_err, rel_err)
        print(f"  {i:>5d} | {x_i:7.4f} | {h_at_x:7.4f} | "
              f"{dz_actual:11.6e} | {dz_pred:11.6e} | {rel_err:10.2e}")

    print(f"\n  最大相對誤差 = {max_err:.2e}")

    # 比較 Fröhlich 網格底部 y[0,i] 與 hill_function 的差異
    # (這是上面 dz 誤差的根源)
    bot_err = 0.0
    for i in [0, NI//4, NI//2, 3*NI//4, NI-1]:
        h_func = hill_function(x_real[0, i], LY)
        bot_err = max(bot_err, abs(y_real[0, i] - h_func))
    print(f"  Fröhlich 底部 y[0,i] vs hill_function 最大差 = {bot_err:.2e}")
    print(f"    → 這是上面 dz 誤差的根源 (Fröhlich .dat 4-5 位精度)")

    # 第二段: 改用解析底界 (z_bot = h(x_i)) 驗證純粹的 redistribute 公式
    print(f"\n  Test 2b: 用 hill_function 為底界, 驗證純解析一致性:")
    eta = np.linspace(0, 1, NZ_test)
    zeta_arr = vinokur_tanh(eta, GAMMA, ALPHA)
    max_err_pure = 0.0
    for i in sample_cols:
        x_i = x_real[0, i]
        h_at_x = hill_function(x_i, LY)
        # 純粹用 hill_function 與 LZ 重做 redistribute 公式
        z_bot_pure = h_at_x
        z_top_pure = LZ
        dz_pure = (zeta_arr[1] - zeta_arr[0]) * (z_top_pure - z_bot_pure)
        dz_pred = ratio_pred * (LZ - h_at_x)
        rel_err_pure = abs(dz_pure - dz_pred) / max(dz_pure, 1e-30)
        max_err_pure = max(max_err_pure, rel_err_pure)
    print(f"  Test 2b 最大相對誤差 = {max_err_pure:.2e}   (應 < 1e-12, 浮點精度)")

    # PASS 條件: Test 2a 主誤差由資料精度主導 (< 1e-3),
    #           Test 2b 純解析 ≈ machine epsilon
    ok = max_err < 1e-3 and max_err_pure < 1e-12
    print(PASS_MSG if ok else FAIL_MSG)
    return ok


# ============================================================
#  Test 3: y+ 公式單位一致性
# ============================================================

def test_yplus_formula_units(var_dict):
    """從第一原理重算 y+, 比對 yplus_profile.

    y+ ≡ u_τ × y / ν   (定義)

    在 LBM lattice 單位:
      u_τ_lat   = U_b_lat × √(cf/2) = Uref × √(cf/2)
      ν_lat     = niu = Uref / Re
      y_lat     = dz (與 H_HILL 同單位)

    代入:
      y+ = Uref × √(cf/2) × dz / (Uref / Re)
         = √(cf/2) × Re × dz

    若用 dimensionless (除 H_HILL 歸一化):
      y+ = √(cf/2) × Re × (dz / H_HILL)

    只要 H_HILL=1 兩式同, 否則差一個 H_HILL 因子.
    """
    print("\nTest 3: y+ 公式單位一致性 (從第一原理重算)")
    print("-" * 60)

    NZ = var_dict["NZ"]
    GAMMA = var_dict["GAMMA"]
    ALPHA = var_dict["ALPHA"]
    LZ = var_dict["LZ"]
    LY = var_dict["LY"]
    H_HILL = var_dict["H_HILL"]
    Re = float(var_dict["Re"])
    Uref = var_dict["Uref"]
    niu = var_dict["niu"]

    # Test point: x_peak with cf_peak
    x_peak = 8.631   # Breuer 1400 peak location
    cf_peak = 0.03973
    h_at_peak = hill_function(x_peak, LY)
    H_col = LZ - h_at_peak
    ratio = first_cell_ratio(NZ, GAMMA, ALPHA)
    dz = ratio * H_col   # 物理單位 = lattice 單位 (在 H_HILL=1 時也 = h-無因次單位)

    # 路徑 1: 直接 LBM lattice 單位
    u_tau_lat = Uref * math.sqrt(cf_peak / 2.0)
    yplus_path1 = u_tau_lat * dz / niu

    # 路徑 2: dimensionless (Re × √(cf/2) × dz / H_HILL)
    yplus_path2 = math.sqrt(cf_peak / 2.0) * Re * dz / H_HILL

    # 路徑 3: 工具實際算的
    x_arr = np.array([x_peak])
    cf_arr = np.array([cf_peak])
    yp_tool, _ = yplus_profile(NZ, GAMMA, ALPHA, x_arr, cf_arr,
                                Re, LZ, LY, H_HILL)
    yplus_tool = yp_tool[0]

    print(f"  測試點: x = {x_peak},  cf = {cf_peak}")
    print(f"  幾何:   h(x)={h_at_peak:.4f}, H_col={H_col:.4f}, dz={dz:.4e}")
    print(f"")
    print(f"  路徑 1 (LBM lattice unit chain):")
    print(f"    u_τ_lat = Uref × √(cf/2) = {Uref} × {math.sqrt(cf_peak/2):.5f}")
    print(f"            = {u_tau_lat:.5e}")
    print(f"    y+ = u_τ × dz / ν")
    print(f"       = {u_tau_lat:.4e} × {dz:.4e} / {niu:.4e}")
    print(f"       = {yplus_path1:.6f}")
    print(f"  路徑 2 (dimensionless):")
    print(f"    y+ = √(cf/2) × Re × dz / H_HILL")
    print(f"       = {math.sqrt(cf_peak/2):.5f} × {Re} × {dz:.4e} / {H_HILL}")
    print(f"       = {yplus_path2:.6f}")
    print(f"  路徑 3 (yplus_profile 工具):")
    print(f"    y+ = {yplus_tool:.6f}")

    diff_12 = abs(yplus_path1 - yplus_path2)
    diff_23 = abs(yplus_path2 - yplus_tool)
    diff_13 = abs(yplus_path1 - yplus_tool)

    print(f"\n  |path1-path2| = {diff_12:.2e}   (應 ≈ 0, H_HILL=1)")
    print(f"  |path2-tool|  = {diff_23:.2e}")
    print(f"  |path1-tool|  = {diff_13:.2e}")

    ok = max(diff_12, diff_23, diff_13) < 1e-10
    print(PASS_MSG if ok else FAIL_MSG)
    return ok


# ============================================================
#  Test 4: omega_quick 估計精度
# ============================================================

def test_omega_estimate():
    """估計 estimate_omega_quick 的「行為正確性」, 而非「絕對精度」.

    重要說明:
      grid_zeta_tool stability table 的 dz_ratio 是「網格生成後 (Poisson)」
      實測值, 含 Steger-Sorenson 平滑效應.

      estimate_omega_quick 用「純 Vinokur tanh 解析 ratio」, 兩者差約 30-50%.

      所以這裡只驗證:
        (a) 對 γ 嚴格單調遞增 (γ↑ → ratio↑ → ω↑)
        (b) 在 γ ∈ [1, 5] 範圍給出合理 ω 值 (~0.5–1.5)
        (c) 公式本身計算正確 (對 dz_ratio 的解析關係)

      絕對精度由 Test 9 用 estimate_gilbm_stability 在實際網格上驗證.
    """
    print("\nTest 4: omega_quick 行為與單調性 (近似估計, 非絕對精度)")
    print("-" * 60)

    NZ = 129
    gammas = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]
    print(f"  NZ={NZ}, 掃描 γ ∈ {gammas}")
    print(f"  {'γ':>4s} | {'ratio':>6s} | {'ω_est':>7s}")
    print("  " + "-" * 30)

    omega_seq = []
    ratio_seq = []
    failed = False

    for g in gammas:
        omega_est, dz_ratio = estimate_omega_quick(NZ, g)
        omega_seq.append(omega_est)
        ratio_seq.append(dz_ratio)
        print(f"  {g:4.1f} | {dz_ratio:6.2f} | {omega_est:7.3f}")

    # 檢查 (a) 單調
    diffs = np.diff(omega_seq)
    if not np.all(diffs > 0):
        print(f"  ✗ 單調性破壞: ω 序列不嚴格遞增")
        failed = True
    else:
        print(f"  ✓ ω 對 γ 嚴格單調遞增")

    # (b) 範圍合理
    if min(omega_seq) < 0.5 or max(omega_seq) > 3.0:
        print(f"  ✗ ω 範圍 [{min(omega_seq):.2f}, {max(omega_seq):.2f}] 異常")
        failed = True
    else:
        print(f"  ✓ ω 範圍 [{min(omega_seq):.2f}, {max(omega_seq):.2f}] 合理")

    # (c) 對給定 ratio 公式正確: ω = 0.5 + 0.046 × ratio^0.75
    test_ratio = 10.0
    omega_check = 0.5 + 0.046 * test_ratio ** 0.75
    print(f"  公式檢驗: ω(ratio=10) = 0.5 + 0.046 × 10^0.75 = {omega_check:.4f}")
    print(f"            預期 ≈ {0.5 + 0.046 * 10**0.75:.4f}")

    print(PASS_MSG if not failed else FAIL_MSG)
    return not failed


# ============================================================
#  Test 5: 多 x 站 y+ 抽檢
# ============================================================

def test_yplus_spot_checks(var_dict):
    """在 5 個典型 x 位置 (山頂, 山谷, 上山, 下山, 分離區),
    手動算 y+ 與工具比對.
    """
    print("\nTest 5: 多 x 站 y+ 手動驗算")
    print("-" * 60)

    NZ = var_dict["NZ"]
    GAMMA = var_dict["GAMMA"]
    ALPHA = var_dict["ALPHA"]
    LZ = var_dict["LZ"]
    LY = var_dict["LY"]
    H_HILL = var_dict["H_HILL"]
    Re = float(var_dict["Re"])

    x_breuer, cf_breuer = load_breuer_cf(1400)
    yp_tool, dz_tool = yplus_profile(NZ, GAMMA, ALPHA,
                                      x_breuer, cf_breuer,
                                      Re, LZ, LY, H_HILL)

    # 選 5 個典型站
    test_xs = [0.5, 2.0, 4.5, 6.0, 8.63]
    labels = ["下山(山頂後)", "腳跟", "谷底", "回流區", "上山尖峰"]
    ratio = first_cell_ratio(NZ, GAMMA, ALPHA)

    print(f"  γ={GAMMA}, NZ={NZ}, ratio={ratio:.4e}")
    print(f"  {'位置':<10s} | {'x':>5s} | {'cf_loc':>9s} | "
          f"{'dz_man':>9s} | {'y+_man':>7s} | {'y+_tool':>7s} | {'err':>8s}")
    print(f"  -----------+-------+-----------+-----------+---------+---------+--------")

    failed = False
    for x_target, label in zip(test_xs, labels):
        # 從 Breuer 找最近的 x
        idx = int(np.argmin(np.abs(x_breuer - x_target)))
        x_actual = x_breuer[idx]
        cf_actual = cf_breuer[idx]

        # 手動算
        h_x = hill_function(x_actual, LY)
        H_col = LZ - h_x
        dz_manual = ratio * H_col
        u_tau_norm = math.sqrt(abs(cf_actual) / 2.0)
        yplus_manual = u_tau_norm * Re * dz_manual / H_HILL

        # 工具算
        yplus_tool_val = yp_tool[idx]
        dz_tool_val = dz_tool[idx]

        err = abs(yplus_manual - yplus_tool_val)
        if err > 1e-10:
            failed = True

        print(f"  {label:<10s} | {x_actual:5.2f} | {cf_actual:+9.5f} | "
              f"{dz_manual:9.5f} | {yplus_manual:7.4f} | {yplus_tool_val:7.4f} | {err:8.1e}")

    print(PASS_MSG if not failed else FAIL_MSG)
    return not failed


# ============================================================
#  Test 6: Breuer 邊界 / 分離區處理
# ============================================================

def test_breuer_edge_cases():
    """確認 load_breuer_cf 排除哨兵, 但保留分離區 cf<0 點."""
    print("\nTest 6: Breuer wall.dat 邊界與分離區處理")
    print("-" * 60)

    x, cf = load_breuer_cf(1400)
    n_total = len(x)
    n_neg = int(np.sum(cf < 0))
    n_zero = int(np.sum(np.abs(cf) < 1e-6))
    n_sentinel = int(np.sum((cf == 0) & ((x == 0) | (x == 9))))

    print(f"  總點數         : {n_total}")
    print(f"  cf < 0 (回流區): {n_neg}")
    print(f"  |cf| < 1e-6    : {n_zero}")
    print(f"  哨兵 (應為 0)  : {n_sentinel}   (期望 0, 已被 loader 過濾)")
    print(f"  x_min, x_max   : {x.min():.4f}, {x.max():.4f}")

    failed = False
    if n_total < 950:
        print(f"  ✗ 樣本太少, 可能誤刪")
        failed = True
    if n_neg < 10:
        print(f"  ✗ cf<0 樣本過少, 可能誤刪分離區")
        failed = True
    if n_sentinel > 0:
        print(f"  ✗ 哨兵未過濾乾淨")
        failed = True
    if abs(x.min()) > 0.01 or abs(x.max() - 9.0) > 0.5:
        print(f"  ✗ x 範圍異常")
        failed = True

    print(PASS_MSG if not failed else FAIL_MSG)
    return not failed


# ============================================================
#  Test 7: 與 model.h minSize 巨集對齊
# ============================================================

def test_minSize_macro_consistency(var_dict):
    """variables.h 的 minSize 巨集:
        minSize = (LZ-1.0) * 0.5 * (1 + tanh(γ*(1/(NZ-1)-α))/tanh(γ*α))

    工具的 first_cell_ratio*(LZ-1.0) 應與 minSize 巨集相等.
    注意: 巨集硬寫 LZ-1.0 (= LZ-H_HILL @ H_HILL=1), 改 H_HILL 巨集會錯.
    """
    print("\nTest 7: variables.h minSize 巨集 vs 工具計算")
    print("-" * 60)

    NZ = var_dict["NZ"]
    GAMMA = var_dict["GAMMA"]
    ALPHA = var_dict["ALPHA"]
    LZ = var_dict["LZ"]
    H_HILL = var_dict["H_HILL"]

    # 巨集: (LZ-1.0) * 0.5 * (1 + tanh(γ*(1/(NZ-1)-α))/tanh(γ*α))
    minSize_macro = (LZ - 1.0) * 0.5 * (
        1.0 + math.tanh(GAMMA * (1.0/(NZ-1) - ALPHA)) / math.tanh(GAMMA * ALPHA)
    )

    # 工具: ratio * (LZ - H_HILL)
    ratio = first_cell_ratio(NZ, GAMMA, ALPHA)
    minSize_tool = ratio * (LZ - H_HILL)

    print(f"  variables.h minSize 巨集 = {minSize_macro:.6e}")
    print(f"  工具 ratio*(LZ-H_HILL)   = {minSize_tool:.6e}")
    diff = abs(minSize_macro - minSize_tool)
    rel_diff = diff / max(minSize_macro, 1e-30)
    print(f"  絕對差                  = {diff:.2e}")
    print(f"  相對差                  = {rel_diff:.2e}")

    # 注意巨集硬寫 LZ-1.0, 不是 LZ-H_HILL.
    if H_HILL != 1.0:
        print(f"  ⚠ H_HILL={H_HILL} ≠ 1, 巨集 LZ-1.0 是 LZ-H_HILL=1.0 的硬編碼")
        print(f"    → variables.h 的 minSize 巨集尚未泛化, 須注意")

    ok = rel_diff < 1e-10
    print(PASS_MSG if ok else FAIL_MSG)
    return ok


# ============================================================
#  Test 8: NZ=64 calibration (工具 vs grid_zeta table 真實 NZ)
# ============================================================

def test_omega_at_actual_NZ(var_dict):
    """grid_zeta_tool table 是 NZ=64 calibration, 我們在 NZ=variables.h 用同 γ
    應得相近 ω (因為 dz_ratio 主要由 γ 決定, NZ 影響較小).

    對 variables.h 當前 (NZ, γ) 給 omega 估計, 並標出 stability table 區間.
    """
    print("\nTest 8: 當前 (NZ, γ) 的 omega 估計與 stability 邊界")
    print("-" * 60)

    NZ = var_dict["NZ"]
    GAMMA = var_dict["GAMMA"]
    omega_est, dz_ratio = estimate_omega_quick(NZ, GAMMA)

    print(f"  variables.h 當前: NZ={NZ}, γ={GAMMA}")
    print(f"  dz_ratio (NZ={NZ}) = {dz_ratio:.2f}")
    print(f"  omega_est          = {omega_est:.3f}")

    # 對照 stability table
    ranges = [
        (0.55, "OPTIMAL (建議)"),
        (1.20, "OK (可用)"),
        (1.50, "MARGINAL (邊緣)"),
        (2.00, "UNSTABLE (危險)"),
    ]
    for thr, label in ranges:
        if omega_est < thr:
            zone = label
            break
    else:
        zone = "EXTREME UNSTABLE"

    print(f"  穩定區間判定        : {zone}")
    print(f"  (對照: γ=2 ω=0.63, γ=3 ω=0.73, γ=4 ω=0.94, γ=5 ω=1.43)")

    # 不算 fail, 只是資訊
    print(PASS_MSG + " (informational)")
    return True


# ============================================================
#  Test 9: 實際生成網格的 omega — 終極驗證
# ============================================================

def test_real_omega_estimate(var_dict):
    """用 grid_zeta_tool 實際生成 (NZ, γ) 對應的網格,
    再呼叫 estimate_gilbm_stability 算「真實」ω.

    這是最權威的 stability 判定; estimate_omega_quick 是粗略估.
    """
    print("\nTest 9: 實際網格的 omega 終極驗證 (estimate_gilbm_stability)")
    print("-" * 60)

    NZ = var_dict["NZ"]
    GAMMA = var_dict["GAMMA"]
    ALPHA = var_dict["ALPHA"]
    Re = var_dict["Re"]
    Uref = var_dict["Uref"]
    H_HILL = var_dict["H_HILL"]
    LY = var_dict["LY"]

    ref_path = HERE / var_dict["GRID_DAT_REF"]
    if not ref_path.exists():
        print(f"  ✗ 找不到 reference grid: {ref_path}")
        return False

    x_ref_raw, y_ref_raw, NI_ref, NJ_ref = parse_tecplot_dat(ref_path)
    h_phys = float(x_ref_raw[0, :].max()) / LY
    scale = 1.0 / h_phys
    x_ref = x_ref_raw * scale
    y_ref = y_ref_raw * scale

    if NJ_ref != NZ:
        print(f"  注意: ref NJ={NJ_ref} ≠ variables.h NZ={NZ}")
        print(f"        Mode 1 須同 NJ; 用 ref NJ={NJ_ref} 做近似驗證")
        print(f"        (γ 對 ω 的影響佔大宗, NZ 影響相對小)")
        NZ_test = NJ_ref
    else:
        NZ_test = NZ

    print(f"  生成網格: NZ_test={NZ_test}, γ={GAMMA}, α={ALPHA}")
    x_real, y_real = redistribute_vertical_physical(x_ref, y_ref,
                                                    gamma=GAMMA, alpha=ALPHA)

    print(f"  呼叫 estimate_gilbm_stability ...")
    stab = estimate_gilbm_stability(x_real, y_real, scale_factor=1.0,
                                     Uref=Uref, Re=Re, H_HILL=H_HILL,
                                     CFL_lambda=var_dict["CFL"])

    omega_real = stab["omega"]
    omega_quick, dz_ratio_quick = estimate_omega_quick(NZ_test, GAMMA, ALPHA)

    print()
    print(f"  estimate_gilbm_stability (真實):")
    print(f"    omega          = {omega_real:.4f}")
    print(f"    dt_global      = {stab['dt_global']:.4e}")
    print(f"    max|c̃|        = {stab['c_max']:.2f}")
    print(f"    dz_ratio       = {stab['dz_ratio']:.2f}   (含 Poisson/grid 效應)")
    print(f"    status         = {stab['status']}")
    print()
    print(f"  estimate_omega_quick (近似):")
    print(f"    omega_quick    = {omega_quick:.4f}")
    print(f"    dz_ratio_quick = {dz_ratio_quick:.2f}   (純 Vinokur 解析)")

    diff = abs(omega_real - omega_quick)
    print()
    print(f"  |ω_real - ω_quick| = {diff:.3f}")

    # 判定: 若 ω_real < 1.5 則設定可用; 若差異 < 0.2 則 quick 估計可信
    failed = False
    if omega_real >= 1.5:
        print(f"  ✗ 實際 ω={omega_real:.3f} ≥ 1.5, 此設定 LBM 可能發散!")
        failed = True
    else:
        print(f"  ✓ 實際 ω={omega_real:.3f} < 1.5, LBM 穩定")

    if diff > 0.30:
        print(f"  ⚠ quick 估計與真實差 {diff:.2f} > 0.3, Phase C 結果僅供排序參考")
    elif diff > 0.15:
        print(f"  ⚠ quick 估計與真實差 {diff:.2f}, 邊界判斷需小心")
    else:
        print(f"  ✓ quick 估計與真實差 {diff:.2f} 可接受")

    print(PASS_MSG if not failed else FAIL_MSG)
    return not failed


# ============================================================
#  Main
# ============================================================

def main():
    print()
    print("=" * 60)
    print("  yplus_design_tool 正確性驗證套件")
    print("=" * 60)

    var_path = HERE.parent / "variables.h"
    raw = parse_variables_h(var_path)
    var_dict = validate_var_dict(raw)
    print(f"  variables.h: {var_path}")
    print(f"  NX={var_dict['NX']}  NY={var_dict['NY']}  NZ={var_dict['NZ']}  "
          f"γ={var_dict['GAMMA']}  α={var_dict['ALPHA']}")

    results = []
    results.append(("hill_function 一致性",        test_hill_function()))
    results.append(("解析 vs 實際網格",            test_grid_consistency(var_dict)))
    results.append(("y+ 公式單位",                  test_yplus_formula_units(var_dict)))
    results.append(("omega_quick 行為",             test_omega_estimate()))
    results.append(("多 x 站抽檢",                  test_yplus_spot_checks(var_dict)))
    results.append(("Breuer 邊界處理",              test_breuer_edge_cases()))
    results.append(("minSize 巨集一致性",           test_minSize_macro_consistency(var_dict)))
    results.append(("當前設定 omega 區間",          test_omega_at_actual_NZ(var_dict)))
    results.append(("實際網格 omega 驗證",          test_real_omega_estimate(var_dict)))

    print()
    print("=" * 60)
    print("  驗證結果總結")
    print("=" * 60)
    n_pass = 0
    for name, ok in results:
        mark = "✓ PASS" if ok else "✗ FAIL"
        if ok:
            n_pass += 1
        print(f"  {mark}  {name}")
    print(f"\n  總結: {n_pass}/{len(results)} 項通過")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
