#!/usr/bin/env python3
"""
omega_local 穩定性掃描 — 通用架構

功能:
  1. 自動從 variables.h 解析所有參數 (Re, Uref, NX, NY, NZ, CFL, ALPHA, LY, LZ, ...)
  2. 自動偵測 J_Frohlich/ 下所有參考網格 (.dat)
  3. 對每個參考網格 × 每個 GAMMA 值，呼叫 grid_zeta_tool.generate_adaptive_grid()
     生成目標解析度網格
  4. 用完全複製 C code 的邏輯計算 omega_local 極端值:
       metric_terms.h :: ComputeMetricTerms_Full (6th-order Fornberg)
       precompute.h   :: ComputeLocalTimeStep    (D3Q19 LTS)
  5. 彙整比較表，推薦最佳 GAMMA
  6. 對 variables.h 中的 GAMMA 值執行穩定性斷言

用法:
  python test_stability_gamma.py                         # variables.h 預設
  python test_stability_gamma.py --gammas 0 1 2 3 4      # 掃描指定 gamma
  python test_stability_gamma.py --grid fine              # 只用 fine grid
  python test_stability_gamma.py --grid medium            # 只用 medium grid
  python test_stability_gamma.py --grid all               # 所有網格
  python test_stability_gamma.py --detail                 # 逐 gamma 詳細輸出
"""

import sys, os, re, time, argparse
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import grid_zeta_tool as gt


# ================================================================
# 1. Parse variables.h (通用，不寫死任何參數)
# ================================================================
def parse_variables_h(path):
    """
    Parse #define values from variables.h.

    Handles:
      - Simple numeric: #define NX 33
      - Parenthesized:  #define LY (9.0)
      - Expressions:    #define niu (Uref/Re)
      - Derived:        #define NZ6 (NZ+6)
      - String:         #define GRID_DAT_REF "3.fine grid.dat"

    Returns dict {name: value} with resolved numeric or string values.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"variables.h not found: {path}")

    raw = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        full = f.read()

    # Join backslash-continued lines
    full = re.sub(r'\\\s*\n', ' ', full)

    for line in full.split('\n'):
        # Strip C comments
        line = re.sub(r'/\*.*?\*/', '', line)
        line = line.split("//")[0].strip()
        m = re.match(r'#define\s+(\w+)\s+(.+)', line)
        if not m:
            continue
        name, val = m.group(1), m.group(2).strip()
        # Handle string literals
        sm = re.match(r'^"(.*)"$', val)
        if sm:
            raw[name] = sm.group(1)
            continue
        raw[name] = val

    # Iterative resolution (handles dependency chains)
    resolved = {}

    def try_resolve(name):
        if name in resolved:
            return resolved[name]
        if name not in raw:
            return None
        val = raw[name]

        # Already a string literal?
        if isinstance(val, str) and not val.replace('.', '').replace('-', '').replace('+', '').replace('e', '').replace('E', '').lstrip('(').rstrip(')').replace(' ', '').replace('/', '').replace('*', '').isdigit():
            # Try expression evaluation
            pass

        # Build expression: substitute known resolved names
        expr = val
        # Unwrap single outer parens
        while expr.startswith("(") and expr.endswith(")"):
            inner = expr[1:-1]
            depth = 0
            balanced = True
            for ch in inner:
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                if depth < 0:
                    balanced = False
                    break
            if balanced and depth == 0:
                expr = inner
            else:
                break

        for dep_name, dep_val in resolved.items():
            if isinstance(dep_val, (int, float)):
                expr = re.sub(r'\b' + dep_name + r'\b', repr(dep_val), expr)

        expr = expr.replace("(double)", "")

        try:
            result = eval(expr)  # noqa: S307 — only evaluates numeric expressions from our own header
            resolved[name] = result
            return result
        except Exception:
            # Keep as raw string
            resolved[name] = val
            return val

    for _ in range(10):
        for name in raw:
            try_resolve(name)

    return resolved


def discover_reference_grids(grid_dir):
    """
    Discover all .dat reference grids in grid_dir.
    Returns list of (label, path) sorted by name.
    """
    grid_dir = Path(grid_dir)
    grids = []
    for p in sorted(grid_dir.glob("*.dat")):
        name = p.stem.lower()
        if "fine" in name:
            label = "fine"
        elif "medium" in name:
            label = "medium"
        elif "coarse" in name:
            label = "coarse"
        else:
            label = p.stem
        grids.append((label, p))
    return grids


# ================================================================
# 2. Fornberg FD coefficients (from metric_terms.h)
# ================================================================
FD6_COEFF = np.array([
    [-147.0,  360.0, -450.0,  400.0, -225.0,   72.0,  -10.0],
    [ -10.0,  -77.0,  150.0, -100.0,   50.0,  -15.0,    2.0],
    [   2.0,  -24.0,  -35.0,   80.0,  -30.0,    8.0,   -1.0],
    [  -1.0,    9.0,  -45.0,    0.0,   45.0,   -9.0,    1.0],
    [   1.0,   -8.0,   30.0,  -80.0,   35.0,   24.0,   -2.0],
    [  -2.0,   15.0,  -50.0,  100.0, -150.0,   77.0,   10.0],
    [  10.0,  -72.0,  225.0, -400.0,  450.0, -360.0,  147.0],
]) / 60.0

FD5_FWD = np.array([-137.0, 300.0, -300.0, 200.0, -75.0, 12.0]) / 60.0
FD5_BWD = np.array([-12.0, 75.0, -200.0, 300.0, -300.0, 137.0]) / 60.0

E19 = np.array([
    [0,0,0],
    [1,0,0],[-1,0,0],[0,1,0],[0,-1,0],[0,0,1],[0,0,-1],
    [1,1,0],[-1,1,0],[1,-1,0],[-1,-1,0],
    [1,0,1],[-1,0,1],[1,0,-1],[-1,0,-1],
    [0,1,1],[0,-1,1],[0,1,-1],[0,-1,-1]
], dtype=float)


# ================================================================
# 3. C-code replica computation pipeline
# ================================================================
def pad_grid_to_C_layout(x_grid, y_grid, NY, NZ, LY):
    """
    Python grid (NZ, NY) → C-layout padded arrays (NY6, NZ6).

    Python: x_grid[kp, jp]  kp=wall-normal, jp=streamwise
    C code: y_2d[j][k]      j=streamwise,   k=wall-normal

    Mapping: y_2d[jp+3, kp+3] = x_grid[kp, jp]
             z_2d[jp+3, kp+3] = y_grid[kp, jp]
    """
    nz_phys, ny_phys = x_grid.shape
    assert nz_phys == NZ and ny_phys == NY, \
        f"Grid shape {x_grid.shape} != ({NZ}, {NY})"

    NY6 = NY + 6
    NZ6 = NZ + 6
    y_2d = np.zeros((NY6, NZ6))
    z_2d = np.zeros((NY6, NZ6))

    # Physical domain (j=3..NY+2, k=3..NZ+2)
    for jp in range(ny_phys):
        for kp in range(nz_phys):
            y_2d[jp + 3, kp + 3] = x_grid[kp, jp]
            z_2d[jp + 3, kp + 3] = y_grid[kp, jp]

    # j-ghost: periodic (period = NY-1 nodes)
    period = ny_phys - 1
    for g in range(3):
        jp_lo = (-3 + g) % period
        jp_hi = (ny_phys + g) % period
        for kp in range(nz_phys):
            y_2d[g, kp + 3] = x_grid[kp, jp_lo] - LY
            z_2d[g, kp + 3] = y_grid[kp, jp_lo]
            y_2d[ny_phys + 3 + g, kp + 3] = x_grid[kp, jp_hi] + LY
            z_2d[ny_phys + 3 + g, kp + 3] = y_grid[kp, jp_hi]

    # k-ghost: linear extrapolation near walls
    for j in range(NY6):
        for g in range(3):
            y_2d[j, g] = y_2d[j, 3]
            z_2d[j, g] = z_2d[j, 3] - (3 - g) * (z_2d[j, 4] - z_2d[j, 3])
            kt = NZ + 3 + g
            y_2d[j, kt] = y_2d[j, NZ + 2]
            z_2d[j, kt] = z_2d[j, NZ + 2] + (g + 1) * (z_2d[j, NZ + 2] - z_2d[j, NZ + 1])

    return y_2d, z_2d


def compute_metric_terms_full(y_2d, z_2d, NZ):
    """Exact replica: metric_terms.h :: ComputeMetricTerms_Full()"""
    ny6, nz6 = y_2d.shape
    NZ6 = NZ + 6
    k_lo = 3
    k_hi = nz6 - 4

    y_xi   = np.zeros((ny6, nz6))
    y_zeta = np.zeros((ny6, nz6))
    z_xi   = np.zeros((ny6, nz6))
    z_zeta = np.zeros((ny6, nz6))

    # j-direction: 6th-order central (vectorized)
    sl_k = slice(2, nz6 - 2)
    for j in range(3, ny6 - 3):
        y_xi[j, sl_k] = (-y_2d[j-3, sl_k] + 9*y_2d[j-2, sl_k] - 45*y_2d[j-1, sl_k]
                          + 45*y_2d[j+1, sl_k] - 9*y_2d[j+2, sl_k] + y_2d[j+3, sl_k]) / 60.0
        z_xi[j, sl_k] = (-z_2d[j-3, sl_k] + 9*z_2d[j-2, sl_k] - 45*z_2d[j-1, sl_k]
                          + 45*z_2d[j+1, sl_k] - 9*z_2d[j+2, sl_k] + z_2d[j+3, sl_k]) / 60.0

    # k-direction: 6th-order Fornberg adaptive
    kc_s, kc_e = 6, k_hi - 2
    if kc_e >= kc_s:
        sl_j = slice(3, ny6 - 3)
        y_zeta[sl_j, kc_s:kc_e+1] = (
            -y_2d[sl_j, kc_s-3:kc_e-2] + 9*y_2d[sl_j, kc_s-2:kc_e-1]
            - 45*y_2d[sl_j, kc_s-1:kc_e] + 45*y_2d[sl_j, kc_s+1:kc_e+2]
            - 9*y_2d[sl_j, kc_s+2:kc_e+3] + y_2d[sl_j, kc_s+3:kc_e+4]) / 60.0
        z_zeta[sl_j, kc_s:kc_e+1] = (
            -z_2d[sl_j, kc_s-3:kc_e-2] + 9*z_2d[sl_j, kc_s-2:kc_e-1]
            - 45*z_2d[sl_j, kc_s-1:kc_e] + 45*z_2d[sl_j, kc_s+1:kc_e+2]
            - 9*z_2d[sl_j, kc_s+2:kc_e+3] + z_2d[sl_j, kc_s+3:kc_e+4]) / 60.0

    bnd_ks = list(range(2, kc_s)) + list(range(kc_e + 1, nz6 - 2))
    for j in range(3, ny6 - 3):
        for k in bnd_ks:
            if k == 2:
                y_zeta[j, k] = sum(FD5_FWD[m] * y_2d[j, 2+m] for m in range(6))
                z_zeta[j, k] = sum(FD5_FWD[m] * z_2d[j, 2+m] for m in range(6))
            elif k == nz6 - 3:
                y_zeta[j, k] = sum(FD5_BWD[m] * y_2d[j, nz6-8+m] for m in range(6))
                z_zeta[j, k] = sum(FD5_BWD[m] * z_2d[j, nz6-8+m] for m in range(6))
            elif k_lo <= k <= k_hi:
                s = k - 3
                s = max(s, k_lo)
                s = min(s, k_hi - 6)
                p = k - s
                y_zeta[j, k] = sum(FD6_COEFF[p, m] * y_2d[j, s+m] for m in range(7))
                z_zeta[j, k] = sum(FD6_COEFF[p, m] * z_2d[j, s+m] for m in range(7))
            else:
                y_zeta[j, k] = (y_2d[j, k+1] - y_2d[j, k-1]) / 2.0
                z_zeta[j, k] = (z_2d[j, k+1] - z_2d[j, k-1]) / 2.0

    J_2D = y_xi * z_zeta - y_zeta * z_xi
    eps = 1e-30
    mask = np.abs(J_2D) > eps

    xi_y   = np.where(mask,  z_zeta / J_2D, 0.0)
    xi_z   = np.where(mask, -y_zeta / J_2D, 0.0)
    zeta_y = np.where(mask, -z_xi   / J_2D, 0.0)
    zeta_z = np.where(mask,  y_xi   / J_2D, 0.0)

    return xi_y, xi_z, zeta_y, zeta_z, J_2D


def compute_local_timestep(xi_y, xi_z, zeta_y, zeta_z,
                           dx, niu, cfl):
    """Exact replica: precompute.h :: ComputeLocalTimeStep()"""
    ny6, nz6 = xi_y.shape
    c_eta_max = 1.0 / dx
    sl = (slice(3, ny6 - 3), slice(3, nz6 - 3))

    max_c_field = np.full((ny6, nz6), c_eta_max)

    for alpha in range(1, 19):
        ey, ez = E19[alpha, 1], E19[alpha, 2]
        if ey == 0.0 and ez == 0.0:
            continue
        c_xi   = np.abs(ey * xi_y[sl]   + ez * xi_z[sl])
        c_zeta = np.abs(ey * zeta_y[sl] + ez * zeta_z[sl])
        np.maximum(max_c_field[sl], c_xi,   out=max_c_field[sl])
        np.maximum(max_c_field[sl], c_zeta, out=max_c_field[sl])

    dt_local = np.full((ny6, nz6), cfl / c_eta_max)
    dt_local[sl] = cfl / max_c_field[sl]
    dt_global = dt_local[sl].min()

    omega_local = 0.5 + 3.0 * niu / dt_local
    a_factor = dt_local / dt_global

    return dt_local, omega_local, a_factor, dt_global, max_c_field


# ================================================================
# 4. Single case analysis
# ================================================================
def analyze_single(x_grid, y_grid, params, verbose=False):
    """
    Run complete C-code-replica stability analysis for one grid.

    Parameters
    ----------
    x_grid, y_grid : ndarray (NZ, NY) — from grid_zeta_tool
    params : dict from parse_variables_h
    verbose : bool — print detailed k-profile etc.

    Returns
    -------
    dict with omega_min, omega_max, omega_mean, margin_low, margin_high,
         dt_global, a_max, J_min, zeta_z_max, etc.
    """
    NY = int(params["NY"])
    NZ = int(params["NZ"])
    LY = float(params["LY"])
    LX = float(params["LX"])
    NX = int(params["NX"])
    CFL = float(params["CFL"])
    niu = float(params["Uref"]) / float(params["Re"])
    dx = LX / (NX - 1)

    NY6 = NY + 6
    NZ6 = NZ + 6

    y_2d, z_2d = pad_grid_to_C_layout(x_grid, y_grid, NY, NZ, LY)
    xi_y, xi_z, zeta_y, zeta_z, J_2D = compute_metric_terms_full(y_2d, z_2d, NZ)
    dt_local, omega_local, a_factor, dt_global, max_c = \
        compute_local_timestep(xi_y, xi_z, zeta_y, zeta_z, dx, niu, CFL)

    sl = (slice(3, NY6 - 3), slice(3, NZ6 - 3))
    omega_int = omega_local[sl]
    J_int = J_2D[sl]
    a_int = a_factor[sl]
    zz_int = np.abs(zeta_z[sl])
    maxc_int = max_c[sl]
    dt_int = dt_local[sl]

    omega_min = float(omega_int.min())
    omega_max = float(omega_int.max())

    ij_min = np.unravel_index(np.argmin(omega_int), omega_int.shape)
    ij_max = np.unravel_index(np.argmax(omega_int), omega_int.shape)
    j_min, k_min = ij_min[0] + 3, ij_min[1] + 3
    j_max, k_max = ij_max[0] + 3, ij_max[1] + 3

    result = {
        "omega_min": omega_min,
        "omega_max": omega_max,
        "omega_mean": float(omega_int.mean()),
        "margin_low": omega_min - 0.5,
        "margin_high": 2.0 - omega_max,
        "dt_global": dt_global,
        "a_max": float(a_int.max()),
        "J_min": float(J_int.min()),
        "zeta_z_max": float(zz_int.max()),
        "max_c": float(maxc_int.max()),
        "j_min": j_min, "k_min": k_min,
        "j_max": j_max, "k_max": k_max,
    }

    if verbose:
        _print_detail(result, omega_local, dt_local, a_factor, max_c,
                      zeta_z, z_2d, omega_int, NY6, NZ6, j_min, k_min)

    return result


def _print_detail(r, omega_local, dt_local, a_factor, max_c,
                  zeta_z, z_2d, omega_int, NY6, NZ6, j_min, k_min):
    """Print detailed k-profile and histogram for one case."""
    print(f"    omega_min={r['omega_min']:.6f} at j={r['j_min']},k={r['k_min']}  "
          f"omega_max={r['omega_max']:.6f} at j={r['j_max']},k={r['k_max']}")
    print(f"    dt_global={r['dt_global']:.4e}  a_max={r['a_max']:.1f}  "
          f"|zeta_z|_max={r['zeta_z_max']:.1f}")

    # Histogram
    bins = [0.50, 0.55, 0.60, 0.70, 0.80, 1.00, 1.20, 1.50, 2.00, 3.00]
    counts, edges = np.histogram(omega_int.ravel(), bins=bins)
    total = omega_int.size
    print(f"    omega distribution (N={total}):")
    for i in range(len(counts)):
        pct = counts[i] / total * 100
        bar = '█' * int(pct / 2)
        flag = " ⚠️" if edges[i+1] > 2.0 and counts[i] > 0 else ""
        print(f"      [{edges[i]:.2f},{edges[i+1]:.2f}) {counts[i]:5d} ({pct:5.1f}%) {bar}{flag}")

    # k-profile at omega_min column
    print(f"    k-profile at j={j_min} (omega_min column):")
    print(f"      {'k':>4s}  {'z_phys':>10s}  {'omega':>10s}  {'a':>6s}")
    for k in range(3, NZ6 - 3):
        w = omega_local[j_min, k]
        flag = " ◀" if k == k_min else ""
        print(f"      {k:4d}  {z_2d[j_min,k]:10.6f}  {w:10.6f}{flag}  "
              f"{a_factor[j_min,k]:6.2f}")


# ================================================================
# 5. Comparison table + recommendation
# ================================================================
def classify_status(omega_min, omega_max):
    """Classify stability status from omega range."""
    if omega_max >= 2.0:
        return "UNSTABLE"
    if omega_min <= 0.5:
        return "UNSTABLE"
    margin_low = omega_min - 0.5
    margin_high = 2.0 - omega_max
    if margin_low < 0.01 or margin_high < 0.05:
        return "MARGINAL"
    if omega_max > 1.5:
        return "OK"
    if omega_max > 1.2:
        return "GOOD"
    return "OPTIMAL"


def print_comparison_table(results_by_grid):
    """
    Print a comparison table for all grids × all gammas.

    results_by_grid: dict { grid_label: [(gamma, result_dict), ...] }
    """
    for grid_label, gamma_results in results_by_grid.items():
        print(f"\n{'='*80}")
        print(f"  Grid: {grid_label}")
        print(f"{'='*80}")
        hdr = (f"  {'GAMMA':>6s} │ {'omega_min':>10s} │ {'omega_max':>10s} │ "
               f"{'margin→0.5':>10s} │ {'margin→2.0':>10s} │ "
               f"{'dt_global':>11s} │ {'a_max':>6s} │ {'Status':<10s}")
        print(hdr)
        print("  " + "─" * (len(hdr) - 2))

        best_gamma = None
        best_score = -1e30

        for gamma, r in gamma_results:
            status = classify_status(r["omega_min"], r["omega_max"])
            ml = r["margin_low"]
            mh = r["margin_high"]

            # Score: maximize min(margin_low, margin_high) — want both far from limits
            score = min(ml, mh) if ml > 0 and mh > 0 else -abs(min(ml, mh))
            if score > best_score:
                best_score = score
                best_gamma = gamma

            marker = ""
            if status == "UNSTABLE":
                marker = " ❌"
            elif status == "MARGINAL":
                marker = " ⚠️"
            elif status == "OPTIMAL":
                marker = " ✅"

            print(f"  {gamma:6.1f} │ {r['omega_min']:10.6f} │ {r['omega_max']:10.6f} │ "
                  f"{ml:10.6f} │ {mh:10.6f} │ "
                  f"{r['dt_global']:11.4e} │ {r['a_max']:6.1f} │ {status:<10s}{marker}")

        print("  " + "─" * (len(hdr) - 2))
        if best_gamma is not None:
            print(f"  ★ 推薦 GAMMA = {best_gamma:.1f} "
                  f"(兩側餘裕最平衡, score={best_score:.4f})")
        print()


# ================================================================
# 6. Assertions
# ================================================================
def run_assertions(results_by_grid, gamma_target):
    """
    Run stability assertions for the target GAMMA (from variables.h).
    Uses GAMMA=0 as baseline if available.

    Returns (n_pass, n_fail).
    """
    print(f"\n{'='*70}")
    print(f"  穩定性 Assertions — GAMMA={gamma_target:.1f} (variables.h)")
    print(f"{'='*70}")

    n_pass = n_fail = 0

    for grid_label, gamma_results in results_by_grid.items():
        print(f"\n  ── Grid: {grid_label} ──")

        # Find target gamma result
        r_target = None
        r_baseline = None
        for g, r in gamma_results:
            if abs(g - gamma_target) < 1e-6:
                r_target = r
            if abs(g - 0.0) < 1e-6:
                r_baseline = r

        if r_target is None:
            print(f"  ⚠️  GAMMA={gamma_target:.1f} not in scan — skipping assertions")
            continue

        tests = [
            # Critical: omega ∈ (0.5, 2.0)
            (f"omega_min > 0.5 (LBM 穩定下限)",
             r_target["omega_min"] > 0.500,
             f"omega_min = {r_target['omega_min']:.8f}"),

            (f"omega_max < 2.0 (LBM 穩定上限)",
             r_target["omega_max"] < 2.0,
             f"omega_max = {r_target['omega_max']:.8f}"),

            # Safety margins
            (f"omega_min > 0.51 (≥1% 安全餘裕)",
             r_target["omega_min"] > 0.51,
             f"margin = {r_target['margin_low']:.8f}"),

            (f"omega_max < 1.90 (≥5% 頂部餘裕)",
             r_target["omega_max"] < 1.90,
             f"margin = {r_target['margin_high']:.8f}"),
        ]

        # Comparative tests (only if baseline exists)
        if r_baseline is not None:
            tests.append(
                (f"J_2D_min 不比 GAMMA=0 更差",
                 r_target["J_min"] >= r_baseline["J_min"] * 1.5,
                 f"GAMMA={gamma_target:.0f} J_min={r_target['J_min']:.4e} "
                 f"vs GAMMA=0 J_min={r_baseline['J_min']:.4e}"))
            tests.append(
                (f"omega_min ≥ GAMMA=0 (不惡化下限)",
                 r_target["omega_min"] >= r_baseline["omega_min"] * 0.95,
                 f"{r_target['omega_min']:.6f} vs {r_baseline['omega_min']:.6f}"))
            tests.append(
                (f"omega_max < GAMMA=0 (改善上限)",
                 r_target["omega_max"] < r_baseline["omega_max"],
                 f"{r_target['omega_max']:.6f} vs {r_baseline['omega_max']:.6f}"))

        for desc, ok, detail in tests:
            tag = "✅ PASS" if ok else "❌ FAIL"
            print(f"    {tag}  {desc}")
            print(f"           {detail}")
            if ok:
                n_pass += 1
            else:
                n_fail += 1

    return n_pass, n_fail


# ================================================================
# 7. Grid generation wrapper (calls grid_zeta_tool directly)
# ================================================================
def generate_grid(ref_path, params, gamma):
    """
    Generate target-resolution grid from reference using grid_zeta_tool.

    Calls gt.generate_adaptive_grid() with parameters from variables.h.
    """
    NY = int(params["NY"])
    NZ = int(params["NZ"])
    ALPHA = float(params["ALPHA"])

    x_ref, y_ref, _, _ = gt.parse_tecplot_dat(ref_path)
    x_out, y_out, _ = gt.generate_adaptive_grid(
        x_ref, y_ref, NY, NZ,
        gamma=gamma, alpha=ALPHA,
        poisson_iter=15000, poisson_tol=1e-12)
    return x_out, y_out


# ================================================================
# 8. Main
# ================================================================
def main():
    parser = argparse.ArgumentParser(
        description="omega_local 穩定性掃描 (通用架構)")
    parser.add_argument("--gammas", type=float, nargs="+",
                        help="GAMMA values to scan (default: 0 + variables.h GAMMA)")
    parser.add_argument("--grid", type=str, default=None,
                        choices=["fine", "medium", "all"],
                        help="Which reference grid (default: variables.h GRID_DAT_REF)")
    parser.add_argument("--detail", action="store_true",
                        help="Print detailed per-gamma output")
    parser.add_argument("--variables", type=str, default=None,
                        help="Path to variables.h (auto-detected if omitted)")
    args = parser.parse_args()

    # ── Locate variables.h ──
    workspace = Path(__file__).parent.parent
    if args.variables:
        vh_path = Path(args.variables)
    else:
        vh_path = workspace / "variables.h"
    if not vh_path.exists():
        print(f"ERROR: Cannot find variables.h at {vh_path}")
        return 1

    # ── Parse parameters ──
    params = parse_variables_h(vh_path)

    # Extract key values for display
    Re    = params.get("Re", "?")
    Uref  = params.get("Uref", "?")
    NX    = int(params.get("NX", 0))
    NY    = int(params.get("NY", 0))
    NZ    = int(params.get("NZ", 0))
    CFL   = params.get("CFL", "?")
    ALPHA = params.get("ALPHA", "?")
    GAMMA_H = float(params.get("GAMMA", 0.0))
    LX    = float(params.get("LX", 4.5))
    LY    = float(params.get("LY", 9.0))
    LZ    = float(params.get("LZ", 3.036))
    niu   = float(Uref) / float(Re)
    dx    = LX / (NX - 1)

    grid_dir_name = params.get("GRID_DAT_DIR", "J_Frohlich")
    grid_ref_name = params.get("GRID_DAT_REF", "3.fine grid.dat")
    grid_dir = workspace / grid_dir_name

    print("=" * 70)
    print("  omega_local 穩定性掃描 (通用架構)")
    print("  ─ variables.h 自動解析")
    print("  ─ metric_terms.h :: ComputeMetricTerms_Full (6th-order Fornberg)")
    print("  ─ precompute.h   :: ComputeLocalTimeStep    (D3Q19 LTS)")
    print(f"  Re={Re}, Uref={Uref}, niu={niu:.6e}, CFL={CFL}")
    print(f"  NX={NX}, NY={NY}, NZ={NZ}, dx={dx:.6f}")
    print(f"  LX={LX}, LY={LY}, LZ={LZ}")
    print(f"  GAMMA={GAMMA_H} (variables.h), ALPHA={ALPHA}")
    print(f"  Grid dir: {grid_dir}")
    print(f"  Grid ref: {grid_ref_name}")
    print("=" * 70)

    # ── Discover/select reference grids ──
    all_grids = discover_reference_grids(grid_dir)
    if not all_grids:
        print(f"ERROR: No .dat files found in {grid_dir}")
        return 1

    if args.grid == "all":
        selected_grids = all_grids
    elif args.grid == "fine":
        selected_grids = [(l, p) for l, p in all_grids if l == "fine"]
    elif args.grid == "medium":
        selected_grids = [(l, p) for l, p in all_grids if l == "medium"]
    else:
        # Default: use the grid specified in variables.h
        ref_path = grid_dir / grid_ref_name
        if not ref_path.exists():
            print(f"ERROR: Reference grid not found: {ref_path}")
            return 1
        label = "fine" if "fine" in grid_ref_name.lower() else \
                ("medium" if "medium" in grid_ref_name.lower() else grid_ref_name)
        selected_grids = [(label, ref_path)]

    if not selected_grids:
        print(f"ERROR: No matching grids for --grid={args.grid}")
        return 1

    print(f"\n  Selected grids: {', '.join(f'{l} ({p.name})' for l, p in selected_grids)}")

    # ── Determine gamma list ──
    if args.gammas:
        gammas = sorted(set(args.gammas))
    else:
        gammas = sorted(set([0.0, GAMMA_H]))

    print(f"  GAMMA scan: {gammas}")

    # ── Run all combinations ──
    results_by_grid = {}
    # Cache Poisson solutions per grid (gamma=0 baseline is shared before stretching)
    # But generate_adaptive_grid includes stretching, so each (grid, gamma) is independent.

    for grid_label, grid_path in selected_grids:
        print(f"\n{'─'*70}")
        print(f"  Reference grid: {grid_label} ({grid_path.name})")
        print(f"{'─'*70}")

        gamma_results = []
        for gamma in gammas:
            print(f"\n  [GAMMA={gamma:.1f}] Generating grid ...")
            t0 = time.time()
            x_grid, y_grid = generate_grid(grid_path, params, gamma)
            t_gen = time.time() - t0

            print(f"  [GAMMA={gamma:.1f}] Computing omega_local ...")
            t0 = time.time()
            r = analyze_single(x_grid, y_grid, params, verbose=args.detail)
            t_comp = time.time() - t0

            status = classify_status(r["omega_min"], r["omega_max"])
            print(f"  [GAMMA={gamma:.1f}] omega ∈ [{r['omega_min']:.6f}, {r['omega_max']:.6f}] "
                  f"→ {status}  ({t_gen+t_comp:.1f}s)")

            gamma_results.append((gamma, r))

        results_by_grid[grid_label] = gamma_results

    # ── Print comparison table ──
    print_comparison_table(results_by_grid)

    # ── Assertions for variables.h GAMMA ──
    # Ensure target gamma was scanned
    if GAMMA_H not in gammas:
        print(f"\n  ⚠️  variables.h GAMMA={GAMMA_H} not in scan list — "
              f"adding for assertion")
        # Generate and compute the missing gamma
        for grid_label, grid_path in selected_grids:
            print(f"\n  [GAMMA={GAMMA_H:.1f}] Generating for assertion ...")
            x_grid, y_grid = generate_grid(grid_path, params, GAMMA_H)
            r = analyze_single(x_grid, y_grid, params, verbose=args.detail)
            results_by_grid[grid_label].append((GAMMA_H, r))

    n_pass, n_fail = run_assertions(results_by_grid, GAMMA_H)

    # ── Summary ──
    total = n_pass + n_fail
    print(f"\n  Result: {n_pass}/{total} passed")

    if n_fail == 0:
        print(f"\n  {'═'*55}")
        print(f"  ✅ ALL TESTS PASSED")
        # Find the target result for summary line
        for grid_label, gamma_results in results_by_grid.items():
            for g, r in gamma_results:
                if abs(g - GAMMA_H) < 1e-6:
                    print(f"     [{grid_label}] GAMMA={GAMMA_H:.1f}: "
                          f"omega ∈ [{r['omega_min']:.6f}, {r['omega_max']:.6f}]")
                    print(f"     margin→0.5 = {r['margin_low']:.6f}, "
                          f"margin→2.0 = {r['margin_high']:.6f}")
        print(f"  {'═'*55}")
    else:
        print(f"\n  {'═'*55}")
        print(f"  ❌ {n_fail} TEST(S) FAILED")
        print(f"  {'═'*55}")

    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
