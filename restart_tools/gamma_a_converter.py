#!/usr/bin/env python3
"""
gamma_a_converter.py — GAMMA ↔ a 互動查詢工具 (兩端對稱 alpha=0.5)

數學關係 (Vinokur tanh ↔ tanhFunction_wall):
    a     = tanh(GAMMA / 2)
    GAMMA = 2 * atanh(a)

tanhFunction_wall(L, a, j, N):
    z(j) = L/2 + (L/2/a) * tanh((-1 + 2j/N)/2 * ln((1+a)/(1-a)))

Vinokur tanh (alpha=0.5):
    zeta(eta) = 0.5 * (1 + tanh(GAMMA*(eta-0.5)) / tanh(GAMMA/2))

用法:
    python gamma_a_converter.py              # 互動模式
    python gamma_a_converter.py -g 4.0       # 直接查 GAMMA→a
    python gamma_a_converter.py -a 0.964     # 直接查 a→GAMMA
    python gamma_a_converter.py --table      # 印出對照表
"""

import math
import sys


def gamma_to_a(gamma):
    return math.tanh(gamma / 2.0)


def a_to_gamma(a):
    if a <= 0.0 or a >= 1.0:
        raise ValueError(f"a={a} 必須在 (0, 1) 開區間內")
    return 2.0 * math.atanh(a)


def grid_stats(gamma, LZ, NZ, alpha=0.5):
    """計算網格拉伸統計量"""
    total = LZ - 1.0  # LZ - hill_crest
    N = NZ - 1
    if gamma < 1e-14:
        dz_min = dz_max = total / N
    else:
        denom = math.tanh(gamma * alpha)
        def zeta(eta):
            return 0.5 * (1.0 + math.tanh(gamma * (eta - alpha)) / denom)
        spacings = []
        for j in range(N):
            z0 = total * zeta(j / N)
            z1 = total * zeta((j + 1) / N)
            spacings.append(z1 - z0)
        dz_min = min(spacings)
        dz_max = max(spacings)
    return dz_min, dz_max, dz_max / dz_min if dz_min > 0 else float('inf')


def print_result(gamma, a, LZ=None, NZ=None):
    print()
    print("  " + "=" * 56)
    print(f"    GAMMA  = {gamma:.10f}")
    print(f"    a      = {a:.15f}")
    print(f"    alpha  = 0.5 (兩端對稱)")
    print("  " + "-" * 56)
    print(f"    驗證: tanh(GAMMA/2) = tanh({gamma/2:.6f}) = {math.tanh(gamma/2):.15f}")
    print(f"    驗證: 2*atanh(a)    = 2*atanh({a:.6f}) = {2*math.atanh(a):.10f}")
    if LZ is not None and NZ is not None:
        dz_min, dz_max, ratio = grid_stats(gamma, LZ, NZ)
        print("  " + "-" * 56)
        print(f"    LZ={LZ}, NZ={NZ} (cells={NZ-1}) 下:")
        print(f"    min(dz) = {dz_min:.6e}  (壁面)")
        print(f"    max(dz) = {dz_max:.6e}  (中央)")
        print(f"    ratio   = {ratio:.4f}")
        print(f"    dt      = {dz_min:.6e}")
    print("  " + "=" * 56)
    print()


def print_table(LZ=None, NZ=None):
    print()
    print("  " + "=" * 72)
    print("   GAMMA ↔ a 對照表  (alpha=0.5, 兩端對稱)")
    print("  " + "=" * 72)
    header = f"  {'GAMMA':>7s} | {'a':>17s}"
    if LZ and NZ:
        header += f" | {'min(dz)':>12s} | {'max(dz)':>12s} | {'ratio':>8s}"
        print(f"  LZ={LZ}, NZ={NZ}")
    print(header)
    print("  " + "-" * 72)

    gammas = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0,
              5.5, 6.0, 7.0, 8.0, 10.0]
    for g in gammas:
        a = gamma_to_a(g)
        line = f"  {g:7.1f} | {a:17.15f}"
        if LZ and NZ:
            dz_min, dz_max, ratio = grid_stats(g, LZ, NZ)
            line += f" | {dz_min:12.6e} | {dz_max:12.6e} | {ratio:8.2f}"
        print(line)
    print("  " + "-" * 72)
    print()


def interactive():
    print()
    print("  " + "=" * 56)
    print("   GAMMA ↔ a 互動查詢 (兩端對稱 alpha=0.5)")
    print("   關係式: a = tanh(GAMMA/2),  GAMMA = 2*atanh(a)")
    print("  " + "=" * 56)
    print()

    # 詢問是否帶入網格參數
    LZ = None
    NZ = None
    ans = input("  是否帶入網格參數計算 dz? (y/N): ").strip().lower()
    if ans in ("y", "yes"):
        try:
            LZ = float(input("    LZ (法向域高, 預設 3.036): ").strip() or "3.036")
            NZ = int(input("    NZ (法向格點數, 預設 257): ").strip() or "257")
        except ValueError:
            print("    輸入無效，不帶入網格參數。")
            LZ = NZ = None

    while True:
        print()
        print("  選擇查詢方向:")
        print("    1. GAMMA → a")
        print("    2. a → GAMMA")
        print("    3. 印出對照表")
        print("    q. 離開")
        print()
        choice = input("  輸入選項 [1/2/3/q]: ").strip().lower()

        if choice in ("q", "quit", "exit"):
            print("  再見！")
            break
        elif choice == "1":
            try:
                val = float(input("  輸入 GAMMA (> 0): ").strip())
                if val <= 0:
                    print("  ** GAMMA 必須 > 0")
                    continue
                a = gamma_to_a(val)
                print_result(val, a, LZ, NZ)
            except ValueError as e:
                print(f"  ** 輸入錯誤: {e}")
        elif choice == "2":
            try:
                val = float(input("  輸入 a (0 < a < 1): ").strip())
                g = a_to_gamma(val)
                print_result(g, val, LZ, NZ)
            except ValueError as e:
                print(f"  ** 輸入錯誤: {e}")
        elif choice == "3":
            print_table(LZ, NZ)
        else:
            print("  ** 請輸入 1, 2, 3, 或 q")


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--table" in args:
        LZ = NZ = None
        for i, arg in enumerate(args):
            if arg == "--lz" and i + 1 < len(args):
                LZ = float(args[i + 1])
            if arg == "--nz" and i + 1 < len(args):
                NZ = int(args[i + 1])
        print_table(LZ, NZ)
        sys.exit(0)

    if "-g" in args:
        idx = args.index("-g")
        if idx + 1 >= len(args):
            print("用法: python gamma_a_converter.py -g <GAMMA>")
            sys.exit(1)
        gamma = float(args[idx + 1])
        a = gamma_to_a(gamma)
        LZ = NZ = None
        if "--lz" in args:
            LZ = float(args[args.index("--lz") + 1])
        if "--nz" in args:
            NZ = int(args[args.index("--nz") + 1])
        print_result(gamma, a, LZ, NZ)
        sys.exit(0)

    if "-a" in args:
        idx = args.index("-a")
        if idx + 1 >= len(args):
            print("用法: python gamma_a_converter.py -a <a>")
            sys.exit(1)
        a = float(args[idx + 1])
        gamma = a_to_gamma(a)
        LZ = NZ = None
        if "--lz" in args:
            LZ = float(args[args.index("--lz") + 1])
        if "--nz" in args:
            NZ = int(args[args.index("--nz") + 1])
        print_result(gamma, a, LZ, NZ)
        sys.exit(0)

    interactive()
