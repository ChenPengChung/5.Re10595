"""
Periodic Hill Grid Tool -- Steger-Sorenson Poisson + Zeta Stretching
=====================================================================
Capabilities:
  1. Parse original Tecplot .dat grid
  2. Mode 1 (Zeta-only): keep Ni x Nj, adjust vertical stretching
  3. Mode 2 (Adaptive):  freely set Ni x Nj, then re-solve the
     Poisson grid equation with control functions P,Q reversed
     from the reference grid -- true Steger-Sorenson method
  4. Export new grid in Tecplot format
  5. Identity verification at original resolution

Mode 2 mathematical basis:
  The TTM-Poisson equation (physical-space form):
    alpha * r_xixi - 2*beta * r_xieta + gamma * r_etaeta
        = -J^2 * (P * r_xi + Q * r_eta)

  Given a reference grid r(xi,eta):
    1. Compute all metric terms and Jacobian
    2. Solve the 2x2 linear system for P,Q at each point
    3. Interpolate P,Q to new (Ni,Nj) via bicubic spline
    4. Resample boundaries, create TFI initial guess
    5. Iteratively solve the Poisson equation with the
       interpolated P,Q as source terms

  Validation: at same (Ni,Nj) the method recovers the original
  grid to ~1e-11 absolute error (near machine precision).
"""

import sys
import re
import numpy as np
try:
    import matplotlib
    matplotlib.use('Agg')  # non-interactive backend (safe for headless servers)
    import matplotlib.pyplot as plt
    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False
from pathlib import Path
# scipy is optional — used for higher-order interpolation if available
try:
    from scipy.interpolate import RectBivariateSpline, interp1d
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False

# ============================================================
#  1.  Parser
# ============================================================

def parse_tecplot_dat(filepath):
    filepath = Path(filepath)
    with open(filepath, "r", encoding="latin-1") as f:
        lines = f.readlines()

    ni = nj = None
    header_lines = 0
    for idx, line in enumerate(lines):
        if "I=" in line.upper():
            parts = line.replace(",", " ").replace("=", " ").upper().split()
            for k, tok in enumerate(parts):
                if tok == "I":
                    ni = int(parts[k + 1])
                if tok == "J":
                    nj = int(parts[k + 1])
            header_lines = idx + 2
            break

    if ni is None or nj is None:
        raise ValueError("Cannot find I/J dimensions in header")

    data_lines = lines[header_lines:]
    x_flat, y_flat = [], []
    for dl in data_lines:
        dl = dl.strip()
        if not dl:
            continue
        vals = dl.split()
        if len(vals) >= 2:
            x_flat.append(float(vals[0]))
            y_flat.append(float(vals[1]))

    expected = ni * nj
    if len(x_flat) != expected:
        raise ValueError(
            f"Expected {expected} points (I={ni} x J={nj}), got {len(x_flat)}"
        )

    x = np.array(x_flat).reshape(nj, ni)
    y = np.array(y_flat).reshape(nj, ni)
    return x, y, ni, nj


# ============================================================
#  2.  Visualiser
# ============================================================

def plot_grid(x, y, title="Grid", savepath=None, figsize=(18, 6)):
    if not _HAS_MPL:
        if savepath: print(f"  [skip plot] matplotlib not available: {savepath}")
        return
    nj, ni = x.shape
    fig, ax = plt.subplots(figsize=figsize)
    for j in range(nj):
        ax.plot(x[j, :], y[j, :], "k-", lw=0.3)
    for i in range(ni):
        ax.plot(x[:, i], y[:, i], "k-", lw=0.3)
    ax.set_aspect("equal")
    ax.set_xlabel("x  [m]"); ax.set_ylabel("y  [m]")
    ax.set_title(title)
    plt.tight_layout()
    if savepath:
        fig.savefig(savepath, dpi=200)
        print(f"  [saved] {savepath}")
    plt.close(fig)


def plot_compare(x1, y1, x2, y2, labels=("Original", "New"),
                 title="Comparison", savepath=None, figsize=(18, 12)):
    if not _HAS_MPL:
        if savepath: print(f"  [skip plot] matplotlib not available: {savepath}")
        return
    fig, axes = plt.subplots(2, 1, figsize=figsize, sharex=True)
    for ax, xg, yg, lbl in zip(axes, [x1, x2], [y1, y2], labels):
        nj, ni = xg.shape
        for j in range(nj):
            ax.plot(xg[j, :], yg[j, :], "k-", lw=0.25)
        for i in range(ni):
            ax.plot(xg[:, i], yg[:, i], "k-", lw=0.25)
        ax.set_aspect("equal"); ax.set_ylabel("y  [m]"); ax.set_title(lbl)
    axes[-1].set_xlabel("x  [m]")
    fig.suptitle(title, fontsize=14, y=1.01)
    plt.tight_layout()
    if savepath:
        fig.savefig(savepath, dpi=200, bbox_inches="tight")
        print(f"  [saved] {savepath}")
    plt.close(fig)


def plot_vertical_spacing(y1, y2, icol, labels=("Original", "New"),
                          savepath=None):
    if not _HAS_MPL:
        if savepath: print(f"  [skip plot] matplotlib not available: {savepath}")
        return
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(range(y1.shape[0]-1), np.diff(y1[:, icol])*1e3, "o-", ms=3, label=labels[0])
    ax.plot(range(y2.shape[0]-1), np.diff(y2[:, icol])*1e3, "s-", ms=3, label=labels[1])
    ax.set_xlabel("j index"); ax.set_ylabel("dy  [mm]")
    ax.set_title(f"Vertical spacing at i = {icol}")
    ax.legend(); ax.grid(True, ls="--", alpha=0.4)
    plt.tight_layout()
    if savepath:
        fig.savefig(savepath, dpi=200)
        print(f"  [saved] {savepath}")
    plt.close(fig)


# ============================================================
#  3.  Stretching functions
# ============================================================

def hill_function(Y, LY=9.0):
    """Periodic hill profile (same polynomial as model.h)."""
    Yb = Y % LY if Y >= 0 else (Y + LY) % LY
    model = 0.0
    s = 54.0 / 28.0
    # left half
    if Yb <= s * (9.0/54.0):
        v = Yb * 28.0
        model = (1.0/28.0) * min(28.0, 28.0 + 0.006775070969851*v*v - 0.0021245277758000*v*v*v)
    elif Yb <= s * (14.0/54.0):
        v = Yb * 28.0
        model = (1.0/28.0) * (25.07355893131 + 0.9754803562315*v - 0.1016116352781*v*v + 0.001889794677828*v*v*v)
    elif Yb <= s * (20.0/54.0):
        v = Yb * 28.0
        model = (1.0/28.0) * (25.79601052357 + 0.8206693007457*v - 0.09055370274339*v*v + 0.001626510569859*v*v*v)
    elif Yb <= s * (30.0/54.0):
        v = Yb * 28.0
        model = (1.0/28.0) * (40.46435022819 - 1.379581654948*v + 0.019458845041284*v*v - 0.0002070318932190*v*v*v)
    elif Yb <= s * (40.0/54.0):
        v = Yb * 28.0
        model = (1.0/28.0) * (17.92461334664 + 0.8743920332081*v - 0.05567361123058*v*v + 0.0006277731764683*v*v*v)
    elif Yb <= s * (54.0/54.0):
        v = Yb * 28.0
        model = (1.0/28.0) * max(0.0, 56.39011190988 - 2.010520359035*v + 0.01644919857549*v*v + 0.00002674976141766*v*v*v)
    # right half (mirror)
    r = LY - Yb
    if r >= 0 and Yb >= LY - s * (54.0/54.0):
        if Yb >= LY - s * (9.0/54.0):
            v = r * 28.0
            model = (1.0/28.0) * min(28.0, 28.0 + 0.006775070969851*v*v - 0.0021245277758000*v*v*v)
        elif Yb >= LY - s * (14.0/54.0):
            v = r * 28.0
            model = (1.0/28.0) * (25.07355893131 + 0.9754803562315*v - 0.1016116352781*v*v + 0.001889794677828*v*v*v)
        elif Yb >= LY - s * (20.0/54.0):
            v = r * 28.0
            model = (1.0/28.0) * (25.79601052357 + 0.8206693007457*v - 0.09055370274339*v*v + 0.001626510569859*v*v*v)
        elif Yb >= LY - s * (30.0/54.0):
            v = r * 28.0
            model = (1.0/28.0) * (40.46435022819 - 1.379581654948*v + 0.019458845041284*v*v - 0.0002070318932190*v*v*v)
        elif Yb >= LY - s * (40.0/54.0):
            v = r * 28.0
            model = (1.0/28.0) * (17.92461334664 + 0.8743920332081*v - 0.05567361123058*v*v + 0.0006277731764683*v*v*v)
        elif Yb >= LY - s * (54.0/54.0):
            v = r * 28.0
            model = (1.0/28.0) * max(0.0, 56.39011190988 - 2.010520359035*v + 0.01644919857549*v*v + 0.00002674976141766*v*v*v)
    return model


_HILL_LY = 9.0


def hill_function_array(Y_arr, LY=_HILL_LY):
    """Vectorized hill_function: evaluate the analytical hill profile at an array of positions."""
    return np.array([hill_function(float(y), LY=LY) for y in Y_arr])


def tanh_wall(L, a, j, N):
    """tanhFunction_wall macro from initializationTool.h (Python version)."""
    import math
    return L/2.0 + (L/2.0/a) * math.tanh((-1.0 + 2.0*j/N) / 2.0 * math.log((1.0+a)/(1.0-a)))


def get_nonuni_parameter(LZ, NZ_cells, CFL, LY=9.0):
    """
    Legacy wrapper (kept for backward compatibility).
    Old behavior: bisection to find 'a' from CFL-based minSize.
    New workflow: GAMMA is user input; use gamma_to_minSize() instead.

    NZ_cells : int  wall-normal cell count (格子數).
               Caller must pass (NZ-1) if NZ is node count.
    """
    minSize = (LZ - 1.0) / NZ_cells * CFL
    total = LZ - hill_function(0.0, LY)

    a_lo, a_hi = 0.1, 1.0 - 1e-15
    while True:
        a_mid = (a_lo + a_hi) / 2.0
        x0 = tanh_wall(total, a_mid, 0, NZ_cells)
        x1 = tanh_wall(total, a_mid, 1, NZ_cells)
        dx = x1 - x0
        if dx - minSize >= 0.0:
            a_lo = a_mid
        else:
            a_hi = a_mid
        if abs(dx - minSize) < 1e-14:
            break
    return a_mid


def gamma_to_minSize(gamma, LZ, NZ_cells, LY=9.0, alpha=0.5):
    """
    Compute minSize analytically from GAMMA (stretching parameter).

    Supports two regimes:
      - GAMMA in (0, 1): legacy atanh-based tanh_wall formula
      - GAMMA >= 1:      Vinokur tanh stretching (used by redistribute_vertical)

    Parameters
    ----------
    gamma    : float  (> 0)
    LZ       : float  wall-normal domain height
    NZ_cells : int    wall-normal cell count (格子數)
                      Caller must pass (NZ-1) if NZ is node count.
    LY       : float  streamwise length (for hill_function)
    alpha    : float  stretching symmetry parameter (default 0.5)

    Returns
    -------
    minSize : float  minimum (wall-nearest) grid spacing
    """
    if gamma <= 0.0:
        raise ValueError(f"GAMMA={gamma} must be > 0")
    total = LZ - hill_function(0.0, LY)   # = LZ - 1.0

    if gamma < 1.0:
        # Legacy atanh-based stretching
        N = NZ_cells
        minSize = tanh_wall(total, gamma, 1, N) - tanh_wall(total, gamma, 0, N)
    else:
        # Vinokur tanh stretching: compute spacing from redistributed eta
        NJ = NZ_cells + 1   # number of nodes
        eta = np.linspace(0, 1, NJ)
        zeta = vinokur_tanh(eta, gamma, alpha)
        # minSize = total * min(delta_zeta)
        dz = np.diff(zeta)
        minSize = total * np.min(dz)

    return minSize


def estimate_omega_pregrid(gamma, LZ, NZ_cells, Uref, Re,
                           H_HILL=1.0, CFL=0.5, LY=9.0, alpha=0.5,
                           non_ortho_factor=1.17):
    """
    Pre-grid omega estimate — no generated grid needed.

    Breaks the circular dependency:
      "need grid to compute omega, need omega to choose GAMMA"

    Approximation chain:
      1. dz_min     = gamma_to_minSize(GAMMA, LZ, NZ-1)  [analytic]
      2. max|c̃|_1D  ≈ 1 / dz_min                         [orthogonal]
      3. max|c̃|_est ≈ max|c̃|_1D × non_ortho_factor       [correction]
      4. dt          = CFL / max|c̃|_est
      5. omega       = 3·niu / dt + 0.5

    Why 1/dz_min underestimates max|c̃|:
      max|c̃| occurs at hill foot (j=1, i≈27) in D3Q19 edge direction
      (e_y=1, e_z=1).  At that location:
        c̃_ζ = |ζ_y·e_y + ζ_z·e_z| = |(-z_ξ/J)·1 + (y_ξ/J)·1|
      The ζ_z term (70%) is well captured by 1/dz_min, but the
      ζ_y term (30%) comes from the hill slope (z_ξ ≠ 0) and is
      missing in the 1D estimate.

    Calibration (Periodic Hill, physical-z redistribution):
      factor = max|c̃|_2D / (1/dz_min), measured over multiple configs:
        GAMMA=2.0 129×64  → 1.15
        GAMMA=3.0 129×64  → 1.13
        GAMMA=4.0 129×64  → 1.11
        GAMMA=4.0 513×257 → 1.17
        GAMMA=5.0 129×64  → 1.09
      Range: 1.09 – 1.18.  Default 1.17 ≈ upper bound → conservative
      (over-predicts omega, safe for stability screening).

    Parameters
    ----------
    gamma    : float  Vinokur stretching parameter (> 0)
    LZ       : float  wall-normal domain height
    NZ_cells : int    wall-normal cell count (NZ - 1)
    Uref, Re, H_HILL : float  flow parameters
    CFL      : float  CFL number
    LY       : float  streamwise length (for hill_function)
    alpha    : float  stretching symmetry (0.5 = symmetric)
    non_ortho_factor : float
        Periodic Hill non-orthogonality correction (default 1.17).
        Source: hill slope z_ξ ≠ 0 + D3Q19 edge direction stacking.
        Varies 1.09–1.18 across GAMMA/resolution; 1.17 is conservative.
        Set to 1.0 to revert to pure orthogonal assumption.

    Returns
    -------
    dict with keys:
        omega_est, dt_est, max_c_est, max_c_1d, dz_min, niu,
        non_ortho_factor
    """
    niu = Uref * H_HILL / Re
    dz_min = gamma_to_minSize(gamma, LZ, NZ_cells, LY, alpha)
    max_c_1d = 1.0 / dz_min
    max_c_est = max_c_1d * non_ortho_factor
    dt_est = CFL / max_c_est
    omega_est = 0.5 + 3.0 * niu / dt_est

    return {
        "omega_est": omega_est,
        "dt_est": dt_est,
        "max_c_est": max_c_est,
        "max_c_1d": max_c_1d,
        "dz_min": dz_min,
        "niu": niu,
        "non_ortho_factor": non_ortho_factor,
    }


def vinokur_tanh(eta, gamma, alpha=0.5):
    """
    Vinokur two-sided tanh clustering.  eta in [0,1].
    gamma=0 => identity.  Monotonic for all gamma >= 0.
    """

    if gamma < 1e-14:
        return eta.copy()
    denom = np.tanh(gamma * alpha)
    if abs(denom) < 1e-30:
        return eta.copy()
    zeta = 0.5 * (1.0 + np.tanh(gamma * (eta - alpha)) / denom)
    zeta[0] = 0.0; zeta[-1] = 1.0
    return zeta


def get_vinokur_gamma_from_ref(x_ref, y_ref, nj_new, alpha=0.5):
    """
    Auto-compute Vinokur gamma by matching the reference grid's
    wall-normal stretching ratio.

    The reference grid (e.g. Frohlich 3.fine, 197x129) has a built-in
    stretching ratio (max_dy / min_dy).  We find the Vinokur gamma that
    reproduces the same ratio at the target resolution nj_new.

    This is physically meaningful: it preserves the reference grid's
    near-wall clustering quality regardless of the target resolution.
    """
    # Measure reference grid's stretching ratio at hill crest (i=0)
    dy_ref = np.diff(y_ref[:, 0])
    ratio_ref = dy_ref.max() / dy_ref.min()

    eta = np.linspace(0, 1, nj_new)

    # Bisection: gamma in [0.1, 20]
    g_lo, g_hi = 0.1, 20.0
    for _ in range(200):
        g_mid = 0.5 * (g_lo + g_hi)
        zeta = vinokur_tanh(eta, g_mid, alpha)
        dz = np.diff(zeta)
        ratio = dz.max() / dz.min()
        if ratio < ratio_ref:
            g_lo = g_mid      # need stronger clustering -> larger gamma
        else:
            g_hi = g_mid
        if abs(ratio - ratio_ref) / ratio_ref < 1e-8:
            break
    return g_mid


# ============================================================
#  3b. GILBM Stability Estimation (LBM-specific)
# ============================================================
#
#  omega_global 計算流程 (兩階段)
#  ================================
#
#  階段 A — 無網格 1D 預估 (estimate_omega_pregrid)
#  ------------------------------------------------
#  輸入: GAMMA, LZ, NZ, Uref, Re, H_HILL, CFL
#  流程:
#    1. dz_min = gamma_to_minSize(GAMMA, LZ, NZ-1)     ← 1D Vinokur 解析
#    2. max|c̃| ≈ 1/dz_min                               ← 壁面法向主導
#    3. dt     = CFL / max|c̃| = CFL × dz_min
#    4. omega  ≈ 0.5 + 3·niu / dt
#  優點: < 1ms, 不需要生成網格, 可提前攔截不穩定的 GAMMA
#  修正: non_ortho_factor=1.17 補償 hill 斜面 ζ_y + D3Q19 edge 疊加
#  精度: 修正後 omega 誤差 < 0.1% (Re=10595), < 0.2% (Re=150)
#
#  階段 B — 有網格 2D 精確計算 (estimate_gilbm_stability)
#  -------------------------------------------------------
#  輸入: 已生成的 2D 網格 x_grid(nj,ni), y_grid(nj,ni)
#  流程:
#    1. 正向度量 (中央差分):
#         y_xi   = ∂y/∂ξ  (axis=1, 流向)
#         y_zeta = ∂y/∂ζ  (axis=0, 壁面法向)
#         z_xi   = ∂z/∂ξ  (axis=1)
#         z_zeta = ∂z/∂ζ  (axis=0)
#    2. Jacobian:  J = y_xi·z_zeta − y_zeta·z_xi
#    3. 逆度量:
#         ζ_y = −z_xi / J,   ζ_z =  y_xi / J
#         ξ_y =  z_zeta / J, ξ_z = −y_zeta / J
#    4. Contravariant velocity (D3Q19 全部方向):
#         c̃_ζ = |ζ_y·e_y + ζ_z·e_z|
#         c̃_ξ = |ξ_y·e_y + ξ_z·e_z|
#         max|c̃| = max over all (ξ,ζ) 格點 and all 19 lattice directions
#    5. dt_global = CFL / max|c̃|
#    6. omega = 0.5 + 3·niu / dt_global
#  精度: 完整 2D 度量, 為最終穩定性判斷
#
#  座標對應:
#    x_grid → y (LBM 流向)     ξ  → axis=1 (i, streamwise)
#    y_grid → z (LBM 壁面法向) ζ  → axis=0 (j, wall-normal)
#
#  omega 物理意義 (此程式碼的 omega = 教科書的 τ):
#    標準 LBM (dt=1):  τ = 0.5 + 3·ν
#    GILBM (dt<1):     τ = 0.5 + 3·ν/dt   (sub-cycling 時每步需更多碰撞)
#    穩定範圍:  0.5 < omega < 2.0
#      omega → 0.5 : 黏度 → 0 (數值不穩定)
#      omega → 2.0 : s_visc = 1/omega → 0.5 (過度鬆弛, GILBM 插值不穩定)
#
#  建議工作流程:
#    1. estimate_omega_pregrid   → 快速預判, omega_est > 2.0 則提前攔截
#    2. 生成網格 (Poisson solve)
#    3. estimate_gilbm_stability → 最終精確確認
# ============================================================

def estimate_gilbm_stability(x_grid, y_grid, scale_factor=1.0,

                             Uref=0.0503, Re=150.0, H_HILL=1.0,
                             CFL_lambda=0.5):
    """
    [階段 B] 有網格後的精確 omega 計算.

    omega (= tau in textbooks) = 3*niu/dt_global + 0.5.
    Practical stable range: omega in (0.5, 2.0).
      omega < 0.5 : negative viscosity (mathematically forbidden)
      omega > 2.0 : s_visc = 1/omega < 0.5, GILBM interpolation unstable
    dt_global = CFL_lambda / max|c_tilde|.

    Parameters
    ----------
    x_grid, y_grid : ndarray (nj, ni)
        Grid coordinates (raw Frohlich or code units).
        x_grid = streamwise (= y in LBM), y_grid = wall-normal (= z in LBM).
    scale_factor : float
        Multiply grid coords to get code units (=1 if already in code units).
    Uref, Re, H_HILL : float
        Flow parameters. niu = Uref * H_HILL / Re.
        Defaults match the legacy Re=150 calibration table.  Auto mode
        overrides these values from variables.h.
    CFL_lambda : float
        CFL number (default 0.5).

    Returns
    -------
    dict with keys:
        omega, dt_global, c_max, dz_min, dz_max, dz_ratio, a_max, status,
        niu, Uref, Re, H_HILL, CFL_lambda, scale_factor
    """
    niu = Uref * H_HILL / Re

    x_c = x_grid * scale_factor
    y_c = y_grid * scale_factor
    nj, ni = x_c.shape

    # D3Q19 velocity set (e_y, e_z components)
    e_y = [0,0,0, 1,-1,0,0, 1,1,-1,-1, 0,0,0,0, 1,-1,1,-1]
    e_z = [0,0,0, 0,0,1,-1, 0,0,0,0, 1,1,-1,-1, 1,1,-1,-1]

    # Forward metrics (central FD, one-sided at boundaries)
    y_xi = np.zeros_like(x_c); y_zeta = np.zeros_like(x_c)
    z_xi = np.zeros_like(y_c); z_zeta = np.zeros_like(y_c)

    y_xi[:, 1:-1] = (x_c[:, 2:] - x_c[:, :-2]) / 2.0
    y_zeta[1:-1, :] = (x_c[2:, :] - x_c[:-2, :]) / 2.0
    z_xi[:, 1:-1] = (y_c[:, 2:] - y_c[:, :-2]) / 2.0
    z_zeta[1:-1, :] = (y_c[2:, :] - y_c[:-2, :]) / 2.0

    y_xi[:, 0] = x_c[:, 1] - x_c[:, 0]
    y_xi[:, -1] = x_c[:, -1] - x_c[:, -2]
    z_xi[:, 0] = y_c[:, 1] - y_c[:, 0]
    z_xi[:, -1] = y_c[:, -1] - y_c[:, -2]
    y_zeta[0, :] = x_c[1, :] - x_c[0, :]
    y_zeta[-1, :] = x_c[-1, :] - x_c[-2, :]
    z_zeta[0, :] = y_c[1, :] - y_c[0, :]
    z_zeta[-1, :] = y_c[-1, :] - y_c[-2, :]

    J = y_xi * z_zeta - y_zeta * z_xi
    sl = (slice(1, -1), slice(1, -1))
    eps = 1e-30

    zeta_y = np.where(np.abs(J) > eps, -z_xi / J, 0)
    zeta_z = np.where(np.abs(J) > eps,  y_xi / J, 0)
    xi_y   = np.where(np.abs(J) > eps,  z_zeta / J, 0)
    xi_z   = np.where(np.abs(J) > eps, -y_zeta / J, 0)

    # Max contravariant velocity over all D3Q19 directions
    max_c = 0.0
    for iq in range(3, 19):
        c_zeta = np.abs(zeta_y[sl] * e_y[iq] + zeta_z[sl] * e_z[iq])
        c_xi   = np.abs(xi_y[sl]   * e_y[iq] + xi_z[sl]   * e_z[iq])
        max_c = max(max_c, c_zeta.max(), c_xi.max())

    # Wall-normal spacing
    dz_min = 1e30
    dz_max = 0.0
    for i in range(ni):
        dz = np.diff(y_c[:, i])
        dz_pos = dz[dz > 0]
        if len(dz_pos) > 0:
            dz_min = min(dz_min, dz_pos.min())
            dz_max = max(dz_max, dz_pos.max())
    dz_ratio = dz_max / dz_min if dz_min > 0 else float('inf')

    # LBM parameters
    dt_global = CFL_lambda / max_c if max_c > 0 else 1.0
    omega = 0.5 + 3.0 * niu / dt_global
    a_max = dz_ratio  # rough LTS acceleration estimate

    # Status classification
    #   omega = 3*niu/dt_global + 0.5  (relaxation time, = tau)
    #   Hard limits: omega < 0.5 → negative viscosity (forbidden)
    #                omega > 2.0 → s_visc = 1/omega < 0.5 (overly stiff)
    #   Practical range: [0.5, ~2.0]
    if omega > 2.0:
        status = "UNSTABLE"
    elif omega > 1.5:
        status = "MARGINAL"
    elif omega > 1.2:
        status = "OK"
    elif omega >= 0.55:
        status = "OPTIMAL"
    elif omega >= 0.505:
        status = "GOOD"
    else:
        status = "DANGEROUS"

    return {
        "omega": omega, "dt_global": dt_global, "c_max": max_c,
        "dz_min": dz_min, "dz_max": dz_max, "dz_ratio": dz_ratio,
        "a_max": a_max, "status": status, "niu": niu,
        "Uref": Uref, "Re": Re, "H_HILL": H_HILL,
        "CFL_lambda": CFL_lambda, "scale_factor": scale_factor,
    }


def print_gilbm_stability_table():
    """
    Print the pre-computed GILBM stability calibration table.

    This table was calibrated for:
      Reference grid : Frohlich 3.fine (197x129)
      Target grid    : I=129, J=64 (NY=129 nodes, NZ=64 nodes)
      Grid method    : Mode 2 Poisson + physical-z redistribution
      Flow params    : Re=150, Uref=0.0503, H_HILL=1.0
      CFL lambda     : 0.5

    The table is reference-only.  Actual omega depends on the target
    grid resolution, scale factor, Uref, Re, H_HILL, and CFL; auto mode
    recomputes it from the generated grid and variables.h.

    NOTE: physical-z redistribution REPLACES Frohlich's native wall
    clustering with Vinokur tanh in physical z-space (symmetric when
    alpha=0.5).  GAMMA=0 means UNIFORM spacing (no clustering).
    """
    print()
    print("  " + "=" * 72)
    print("   GILBM Stability Calibration Reference")
    print("   REFERENCE ONLY: 3.fine -> 129x64, Re=150, Uref=0.0503, CFL=0.5")
    print("   Actual omega is recomputed from variables.h after grid generation.")
    print("  " + "=" * 72)
    print(f"  {'GAMMA':>6s} | {'omega':>8s} | {'max|c~|':>10s} | {'dz_ratio':>8s} | {'RefStatus':<12s} | Note")
    print("  " + "-" * 72)
    #                GAMMA  omega   c_max   ratio  status         note
    # Calibrated with redistribute_vertical_physical (2026-03)
    table = [
        (0.0,  0.92,  209,  31, "OPTIMAL",  "UNIFORM z (no clustering) + minSize=NaN!"),
        (0.5,  0.58,   38,   2, "OPTIMAL",  "Very mild symmetric clustering"),
        (1.0,  0.59,   42,   2, "OPTIMAL",  "Mild symmetric clustering"),
        (1.5,  0.60,   50,   3, "OPTIMAL",  "Moderate symmetric clustering"),
        (2.0,  0.63,   63,   4, "OPTIMAL",  "Ref case: recommended"),
        (2.5,  0.67,   83,   5, "OPTIMAL",  "Good clustering"),
        (3.0,  0.73,  112,   8, "OPTIMAL",  "Ref case: strong clustering"),
        (3.5,  0.81,  156,  12, "OPTIMAL",  "Strong clustering"),
        (4.0,  0.94,  221,  20, "OPTIMAL",  "Very strong (approaching Frohlich-level)"),
        (5.0,  1.43,  463,  52, "OK",       "Extreme clustering, omega > 1.2"),
    ]
    for gamma, omega, c_max, ratio, status, note in table:
        marker = ""
        if gamma == 2.0:
            marker = " <-- ref rec"
        elif status in ("MARGINAL", "UNSTABLE"):
            marker = " ***"
        print(f"  {gamma:6.1f} | {omega:8.2f} | {c_max:10d} | {ratio:8d} | {status:<12s} | {note}{marker}")
    print("  " + "-" * 72)
    print()
    print("  Physical-z redistribution: GAMMA controls Vinokur tanh in z-space.")
    print("  GAMMA=0 = uniform (NO wall clustering, minSize macro = NaN!).")
    print("  GAMMA=2.0 was the reference-case recommendation only.")
    print("  Do not infer current-grid stability from this table; use the")
    print("  post-generation check computed with variables.h flow parameters.")
    print()


def print_gilbm_stability_warning(gamma, omega, c_max, dt_global, a_max, status,
                                  Uref=None, Re=None, H_HILL=None,
                                  CFL_lambda=None, niu=None):
    """
    Print a concise GILBM stability warning for the chosen parameters.
    Called after grid generation to alert the user.
    """
    print()
    print("  " + "=" * 62)
    print("   GILBM Stability Check")
    print("  " + "=" * 62)
    print(f"    GAMMA        = {gamma:.4f}")
    if Uref is not None and Re is not None:
        h_val = 1.0 if H_HILL is None else H_HILL
        cfl_val = 0.5 if CFL_lambda is None else CFL_lambda
        print(f"    flow params  = Uref={Uref:g}, Re={Re:g}, H_HILL={h_val:g}, CFL={cfl_val:g}")
        if niu is not None:
            print(f"    niu          = {niu:.6e}")
    print(f"    omega_global = {omega:.4f}", end="")
    if omega > 2.0:
        print("  *** UNSTABLE (omega > 2.0) ***")
    elif omega > 1.5:
        print("  ** MARGINAL (omega > 1.5) **")
    elif omega > 1.2:
        print("  * OK (omega > 1.2)")
    elif omega < 0.505:
        print("  *** DANGEROUS (omega ≈ 0.5, near negative-viscosity limit) ***")
    else:
        print("  [OPTIMAL]")
    print(f"    max|c_tilde| = {c_max:.1f}")
    print(f"    dt_global    = {dt_global:.4e}")
    print(f"    a_max (LTS)  = {a_max:.1f}")
    print(f"    Status       = {status}")

    if omega > 2.0:
        print()
        print("  !! WARNING: This grid WILL DIVERGE in GILBM !!")
        print("  !! Reduce GAMMA (try 2.0~3.0 for safe symmetric clustering) !!")
        print("  !! omega (= 3*niu/dt_global + 0.5) > 2.0 → s_visc < 0.5, overly stiff !!")
    elif omega < 0.505:
        print()
        print("  !! DANGEROUS: omega ≈ 0.5 → niu ≈ 0 (near negative-viscosity limit) !!")
        print("  !! Check Uref, Re, and grid scale. Increase Uref or reduce Re. !!")
    elif omega > 1.5:
        print()
        print("  ** CAUTION: Marginal stability. May diverge under")
        print("     transient conditions. Consider reducing GAMMA.")
    print("  " + "=" * 62)
    print()


# ============================================================
#  4.  Zeta-only redistribution (Mode 1)
# ============================================================

def redistribute_vertical_arclength(x, y, gamma=0.0, alpha=0.5):
    """
    [LEGACY] Redistribute vertical points in arc-length space.
    gamma=0 => identity (reproduces original exactly).

    WARNING: This function preserves the Frolich reference grid's
    inherent bottom-wall bias.  With alpha=0.5, the redistribution
    is symmetric in arc-length but NOT in physical z-space.
    Increasing GAMMA actually WORSENS the top/bottom asymmetry.
    Use redistribute_vertical_physical() instead for symmetric grids.
    """
    nj, ni = x.shape
    eta = np.linspace(0, 1, nj)
    zeta = vinokur_tanh(eta, gamma, alpha)

    x_new = np.empty_like(x)
    y_new = np.empty_like(y)

    for i in range(ni):
        xc, yc = x[:, i], y[:, i]
        ds = np.sqrt(np.diff(xc)**2 + np.diff(yc)**2)
        s = np.concatenate(([0.0], np.cumsum(ds)))
        s_norm = s / s[-1]
        s_new = np.interp(zeta, eta, s_norm)
        x_new[:, i] = np.interp(s_new, s_norm, xc)
        y_new[:, i] = np.interp(s_new, s_norm, yc)

    return x_new, y_new


def redistribute_vertical_physical(x, y, gamma=0.0, alpha=0.5):
    """
    Redistribute vertical points in physical z-coordinate space.

    Unlike redistribute_vertical_arclength() which operates in arc-length
    space (preserving the reference grid's inherent bottom-wall bias),
    this function redistributes in physical z-space, ensuring truly
    symmetric wall clustering when alpha=0.5.

    Parameters
    ----------
    x, y : ndarray (nj, ni)
        Grid coordinates.  y is wall-normal (z in code).
    gamma : float
        Vinokur tanh stretching parameter.
        gamma=0 => uniform spacing in z (no wall clustering).
        gamma>0 => wall clustering, symmetric when alpha=0.5.
    alpha : float
        Clustering symmetry.  0.5 = both walls equal.

    Returns
    -------
    x_new, y_new : ndarray (nj, ni)
        Redistributed grid coordinates.
    """
    nj, ni = x.shape
    eta = np.linspace(0, 1, nj)
    zeta = vinokur_tanh(eta, gamma, alpha)

    x_new = np.empty_like(x)
    y_new = np.empty_like(y)

    for i in range(ni):
        z_bot = y[0, i]
        z_top = y[-1, i]
        # New wall-normal positions: Vinokur distribution in physical z
        z_col = z_bot + zeta * (z_top - z_bot)
        y_new[:, i] = z_col
        # Interpolate streamwise coordinate to maintain grid topology
        x_new[:, i] = np.interp(z_col, y[:, i], x[:, i])

    return x_new, y_new


# Default: use physical-space redistribution (fixes Frolich asymmetry)
redistribute_vertical = redistribute_vertical_physical


# ============================================================
#  5.  Steger-Sorenson Poisson grid generation (Mode 2)
# ============================================================

def _compute_metrics(x, y):
    """Compute all metric terms using 2nd-order finite differences."""
    nj, ni = x.shape

    x_xi = np.zeros_like(x)
    x_xi[:, 1:-1] = 0.5 * (x[:, 2:] - x[:, :-2])
    x_xi[:, 0]  = -1.5*x[:,0] + 2.0*x[:,1] - 0.5*x[:,2]
    x_xi[:, -1] =  0.5*x[:,-3] - 2.0*x[:,-2] + 1.5*x[:,-1]

    y_xi = np.zeros_like(y)
    y_xi[:, 1:-1] = 0.5 * (y[:, 2:] - y[:, :-2])
    y_xi[:, 0]  = -1.5*y[:,0] + 2.0*y[:,1] - 0.5*y[:,2]
    y_xi[:, -1] =  0.5*y[:,-3] - 2.0*y[:,-2] + 1.5*y[:,-1]

    x_eta = np.zeros_like(x)
    x_eta[1:-1,:] = 0.5 * (x[2:,:] - x[:-2,:])
    x_eta[0,:]  = -1.5*x[0,:] + 2.0*x[1,:] - 0.5*x[2,:]
    x_eta[-1,:] =  0.5*x[-3,:] - 2.0*x[-2,:] + 1.5*x[-1,:]

    y_eta = np.zeros_like(y)
    y_eta[1:-1,:] = 0.5 * (y[2:,:] - y[:-2,:])
    y_eta[0,:]  = -1.5*y[0,:] + 2.0*y[1,:] - 0.5*y[2,:]
    y_eta[-1,:] =  0.5*y[-3,:] - 2.0*y[-2,:] + 1.5*y[-1,:]

    x_xixi = np.zeros_like(x)
    x_xixi[:, 1:-1] = x[:, 2:] - 2.0*x[:, 1:-1] + x[:, :-2]
    x_xixi[:, 0]  = x[:,0] - 2.0*x[:,1] + x[:,2]
    x_xixi[:, -1] = x[:,-3] - 2.0*x[:,-2] + x[:,-1]

    y_xixi = np.zeros_like(y)
    y_xixi[:, 1:-1] = y[:, 2:] - 2.0*y[:, 1:-1] + y[:, :-2]
    y_xixi[:, 0]  = y[:,0] - 2.0*y[:,1] + y[:,2]
    y_xixi[:, -1] = y[:,-3] - 2.0*y[:,-2] + y[:,-1]

    x_etaeta = np.zeros_like(x)
    x_etaeta[1:-1,:] = x[2:,:] - 2.0*x[1:-1,:] + x[:-2,:]
    x_etaeta[0,:]  = x[0,:] - 2.0*x[1,:] + x[2,:]
    x_etaeta[-1,:] = x[-3,:] - 2.0*x[-2,:] + x[-1,:]

    y_etaeta = np.zeros_like(y)
    y_etaeta[1:-1,:] = y[2:,:] - 2.0*y[1:-1,:] + y[:-2,:]
    y_etaeta[0,:]  = y[0,:] - 2.0*y[1,:] + y[2,:]
    y_etaeta[-1,:] = y[-3,:] - 2.0*y[-2,:] + y[-1,:]

    x_pad = np.pad(x, ((1,1),(1,1)), mode='edge')
    y_pad = np.pad(y, ((1,1),(1,1)), mode='edge')
    x_xieta = 0.25*(x_pad[2:,2:] - x_pad[2:,:-2]
                    - x_pad[:-2,2:] + x_pad[:-2,:-2])[:nj,:ni]
    y_xieta = 0.25*(y_pad[2:,2:] - y_pad[2:,:-2]
                    - y_pad[:-2,2:] + y_pad[:-2,:-2])[:nj,:ni]

    return {
        "x_xi": x_xi, "x_eta": x_eta, "y_xi": y_xi, "y_eta": y_eta,
        "x_xixi": x_xixi, "x_etaeta": x_etaeta, "x_xieta": x_xieta,
        "y_xixi": y_xixi, "y_etaeta": y_etaeta, "y_xieta": y_xieta,
        "alpha": x_eta**2 + y_eta**2,
        "beta": x_xi*x_eta + y_xi*y_eta,
        "gamma": x_xi**2 + y_xi**2,
        "J": x_xi*y_eta - x_eta*y_xi,
    }


def _compute_PQ(metrics):
    """Reverse-compute control functions P,Q from a known grid."""
    m = metrics
    RHS_x = (m["alpha"]*m["x_xixi"] - 2.0*m["beta"]*m["x_xieta"]
             + m["gamma"]*m["x_etaeta"])
    RHS_y = (m["alpha"]*m["y_xixi"] - 2.0*m["beta"]*m["y_xieta"]
             + m["gamma"]*m["y_etaeta"])
    J2 = m["J"]**2
    det = m["J"]
    safe = np.abs(det) > 1e-30

    P = np.zeros_like(RHS_x)
    Q = np.zeros_like(RHS_x)
    b1 = np.zeros_like(RHS_x)
    b2 = np.zeros_like(RHS_x)
    b1[safe] = RHS_x[safe] / (-J2[safe])
    b2[safe] = RHS_y[safe] / (-J2[safe])
    P[safe] = ( m["y_eta"][safe]*b1[safe] - m["x_eta"][safe]*b2[safe]) / det[safe]
    Q[safe] = (-m["y_xi"][safe]*b1[safe]  + m["x_xi"][safe]*b2[safe])  / det[safe]
    return P, Q


def _poisson_solve(x_init, y_init, P, Q,
                   n_iter=15000, omega=1.0, tol=1e-10, print_every=2000):
    """Row-vectorised Gauss-Seidel Poisson solver. Boundaries fixed."""
    nj, ni = x_init.shape
    x = x_init.copy()
    y = y_init.copy()
    convergence = []
    si = slice(1, -1)

    for it in range(n_iter):
        max_corr = 0.0

        for j in range(1, nj - 1):
            xxi  = 0.5 * (x[j, 2:] - x[j, :-2])
            xeta = 0.5 * (x[j+1, si] - x[j-1, si])
            yxi  = 0.5 * (y[j, 2:] - y[j, :-2])
            yeta = 0.5 * (y[j+1, si] - y[j-1, si])

            al = xeta**2 + yeta**2
            be = xxi*xeta + yxi*yeta
            ga = xxi**2 + yxi**2
            jac = xxi*yeta - xeta*yxi
            j2 = jac**2

            denom = 2.0 * (al + ga)
            safe = denom > 1e-30

            x_cross = 0.25*(x[j+1,2:] - x[j+1,:-2] - x[j-1,2:] + x[j-1,:-2])
            y_cross = 0.25*(y[j+1,2:] - y[j+1,:-2] - y[j-1,2:] + y[j-1,:-2])

            Pj = P[j, si]; Qj = Q[j, si]
            Sx = -j2 * (Pj*xxi + Qj*xeta)
            Sy = -j2 * (Pj*yxi + Qj*yeta)

            x_new = np.where(safe,
                (al*(x[j,2:]+x[j,:-2]) + ga*(x[j+1,si]+x[j-1,si])
                 - 2.0*be*x_cross - Sx) / np.where(safe, denom, 1.0),
                x[j, si])
            y_new = np.where(safe,
                (al*(y[j,2:]+y[j,:-2]) + ga*(y[j+1,si]+y[j-1,si])
                 - 2.0*be*y_cross - Sy) / np.where(safe, denom, 1.0),
                y[j, si])

            dx = omega * (x_new - x[j, si])
            dy = omega * (y_new - y[j, si])
            x[j, si] += dx
            y[j, si] += dy

            row_max = max(np.max(np.abs(dx)), np.max(np.abs(dy)))
            if row_max > max_corr:
                max_corr = row_max

        convergence.append(max_corr)

        if np.isnan(max_corr) or max_corr > 1e10:
            print(f"    DIVERGED at iter {it}")
            break

        if print_every and (it % print_every == 0 or it == n_iter - 1):
            print(f"    iter {it:5d}:  max_corr = {max_corr:.4e}")

        if max_corr < tol:
            print(f"    Converged at iter {it}, max_corr = {max_corr:.4e}")
            break

    return x, y, convergence


def _tfi(x_bot, y_bot, x_top, y_top, x_lft, y_lft, x_rgt, y_rgt):
    """Transfinite Interpolation (vectorised)."""
    ni = len(x_bot); nj = len(x_lft)
    xi = np.linspace(0, 1, ni)[np.newaxis, :]
    eta = np.linspace(0, 1, nj)[:, np.newaxis]
    x = ((1-eta)*x_bot + eta*x_top
       + (1-xi)*x_lft[:, np.newaxis] + xi*x_rgt[:, np.newaxis]
       - (1-xi)*(1-eta)*x_bot[0] - xi*(1-eta)*x_bot[-1]
       - (1-xi)*eta*x_top[0] - xi*eta*x_top[-1])
    y = ((1-eta)*y_bot + eta*y_top
       + (1-xi)*y_lft[:, np.newaxis] + xi*y_rgt[:, np.newaxis]
       - (1-xi)*(1-eta)*y_bot[0] - xi*(1-eta)*y_bot[-1]
       - (1-xi)*eta*y_top[0] - xi*eta*y_top[-1])
    return x, y


def _bilinear_interp_2d(data, eta_old, xi_old, eta_new, xi_new):
    """
    Bilinear interpolation of 2D data from (eta_old, xi_old) grid
    to (eta_new, xi_new) grid. Pure numpy, no scipy required.
    """
    nj_old = len(eta_old)
    ni_old = len(xi_old)
    if nj_old < 2 or ni_old < 2:
        raise ValueError("Bilinear interpolation requires at least 2x2 source points")

    j0 = np.searchsorted(eta_old, eta_new, side='right') - 1
    i0 = np.searchsorted(xi_old, xi_new, side='right') - 1
    j0 = np.clip(j0, 0, nj_old - 2)
    i0 = np.clip(i0, 0, ni_old - 2)
    j1 = j0 + 1
    i1 = i0 + 1

    eta_den = eta_old[j1] - eta_old[j0]
    xi_den = xi_old[i1] - xi_old[i0]
    te = np.divide(eta_new - eta_old[j0], eta_den,
                   out=np.zeros_like(eta_new, dtype=float),
                   where=eta_den != 0.0)
    tx = np.divide(xi_new - xi_old[i0], xi_den,
                   out=np.zeros_like(xi_new, dtype=float),
                   where=xi_den != 0.0)

    w00 = (1.0 - te)[:, np.newaxis] * (1.0 - tx)[np.newaxis, :]
    w01 = (1.0 - te)[:, np.newaxis] * tx[np.newaxis, :]
    w10 = te[:, np.newaxis] * (1.0 - tx)[np.newaxis, :]
    w11 = te[:, np.newaxis] * tx[np.newaxis, :]

    return (w00 * data[j0[:, np.newaxis], i0[np.newaxis, :]]
            + w01 * data[j0[:, np.newaxis], i1[np.newaxis, :]]
            + w10 * data[j1[:, np.newaxis], i0[np.newaxis, :]]
            + w11 * data[j1[:, np.newaxis], i1[np.newaxis, :]])


def _interpolate_PQ(P, Q, ni_old, nj_old, ni_new, nj_new):
    """
    Interpolate P,Q from old to new resolution,
    with proper scaling for the changed computational grid spacing.

    Uses bicubic spline (scipy) if available, bilinear (numpy) otherwise.
    """
    xi_o = np.linspace(0, 1, ni_old); eta_o = np.linspace(0, 1, nj_old)
    xi_n = np.linspace(0, 1, ni_new); eta_n = np.linspace(0, 1, nj_new)

    if _HAS_SCIPY:
        P_n = RectBivariateSpline(eta_o, xi_o, P, kx=3, ky=3)(eta_n, xi_n)
        Q_n = RectBivariateSpline(eta_o, xi_o, Q, kx=3, ky=3)(eta_n, xi_n)
    else:
        P_n = _bilinear_interp_2d(P, eta_o, xi_o, eta_n, xi_n)
        Q_n = _bilinear_interp_2d(Q, eta_o, xi_o, eta_n, xi_n)

    scale_P = (ni_new - 1) / (ni_old - 1)
    scale_Q = (nj_new - 1) / (nj_old - 1)
    P_n *= scale_P
    Q_n *= scale_Q

    return P_n, Q_n


def _resample_boundary(xb, yb, n_new):
    """Resample boundary to n_new points preserving arc-length pattern.
    Uses cubic interp (scipy) if available, linear (numpy) otherwise."""
    n_old = len(xb)
    if n_new == n_old:
        return xb.copy(), yb.copy()
    ds = np.sqrt(np.diff(xb)**2 + np.diff(yb)**2)
    s = np.concatenate(([0], np.cumsum(ds))); s /= s[-1]
    s_norm_old = np.linspace(0, 1, n_old)
    s_new = np.interp(np.linspace(0, 1, n_new), s_norm_old, s)

    if _HAS_SCIPY:
        return (interp1d(s, xb, kind='cubic')(s_new),
                interp1d(s, yb, kind='cubic')(s_new))
    else:
        return (np.interp(s_new, s, xb),
                np.interp(s_new, s, yb))


def generate_adaptive_grid(x_ref, y_ref, ni_new, nj_new,
                           gamma=0.0, alpha=0.5,
                           poisson_iter=15000, poisson_tol=1e-10,
                           LZ=None):
    """
    Full Steger-Sorenson adaptive grid generation.

    Strategy:
      1. Reverse-compute P,Q from reference grid
      2. Interpolate P,Q to new (ni_new, nj_new)
      3. Resample boundaries at new resolution (NO stretching here)
      4. TFI initial guess
      5. Poisson solve with interpolated P,Q
      6. Apply vertical stretching (gamma/alpha) as post-processing
         on the converged Poisson grid -- same logic as Mode 1

    The stretching is applied AFTER the Poisson solve to avoid
    boundary inconsistency: Poisson needs all 4 boundaries to be
    geometrically consistent, which breaks if only the vertical
    boundaries are stretched while horizontal boundaries are not.
    """
    nj_ref, ni_ref = x_ref.shape

    print("    [1/6] Computing P,Q from reference ...")
    metrics = _compute_metrics(x_ref, y_ref)
    P_ref, Q_ref = _compute_PQ(metrics)

    print(f"    [2/6] Interpolating P,Q: ({ni_ref}x{nj_ref}) -> ({ni_new}x{nj_new}) ...")
    if ni_new == ni_ref and nj_new == nj_ref:
        P_new, Q_new = P_ref.copy(), Q_ref.copy()
    else:
        P_new, Q_new = _interpolate_PQ(P_ref, Q_ref,
                                        ni_ref, nj_ref, ni_new, nj_new)

    print("    [3/6] Resampling boundaries ...")
    xb, yb = _resample_boundary(x_ref[0, :],  y_ref[0, :],  ni_new)
    xt, yt = _resample_boundary(x_ref[-1, :], y_ref[-1, :], ni_new)
    xl, yl = _resample_boundary(x_ref[:, 0],  y_ref[:, 0],  nj_new)
    xr, yr = _resample_boundary(x_ref[:, -1], y_ref[:, -1], nj_new)

    # Analytical overwrite: bottom boundary must lie exactly on the
    # Mellen-Fröhlich-Rodi hill polynomial.  _resample_boundary uses cubic
    # interpolation from the reference grid, which introduces O(1e-6) error
    # at different target resolutions.  Overwrite yb (wall-normal heights)
    # with analytically evaluated hill_function values.
    fro_x_max = x_ref[0, -1]
    _h_phys = fro_x_max / _HILL_LY
    _scale = 1.0 / _h_phys if _h_phys < 0.5 else 1.0
    yb_old = yb.copy()
    yb = hill_function_array(xb * _scale, LY=_HILL_LY) / _scale
    _max_correction = float(np.max(np.abs(yb - yb_old)))
    print(f"    [3/6] Bottom boundary: analytical hill overwrite "
          f"(max correction = {_max_correction:.3e})")

    # Analytical overwrite: top boundary must be at exactly LZ (in physical
    # units).  The reference grid may have z_top != LZ*h_phys due to the
    # original Frohlich data being rounded (e.g. 0.085 m vs 3.036*0.028).
    if LZ is not None:
        yt_exact = LZ / _scale   # LZ in code units → physical
        yt_old = yt.copy()
        yt[:] = yt_exact
        _top_correction = float(np.max(np.abs(yt - yt_old)))
        print(f"    [3/6] Top boundary: analytical LZ={LZ} overwrite "
              f"(max correction = {_top_correction:.3e})")

    xl[0] = xb[0];   yl[0] = yb[0]
    xl[-1] = xt[0];  yl[-1] = yt[0]
    xr[0] = xb[-1];  yr[0] = yb[-1]
    xr[-1] = xt[-1]; yr[-1] = yt[-1]

    print("    [4/6] TFI initial guess ...")
    x_tfi, y_tfi = _tfi(xb, yb, xt, yt, xl, yl, xr, yr)

    print(f"    [5/6] Poisson solve (max {poisson_iter} iter) ...")
    x_out, y_out, conv = _poisson_solve(
        x_tfi, y_tfi, P_new, Q_new,
        n_iter=poisson_iter, omega=1.0, tol=poisson_tol, print_every=2000)

    if gamma > 1e-14:
        print(f"    [6/6] Applying physical-z stretching (gamma={gamma}, alpha={alpha}) ...")
        x_out, y_out = redistribute_vertical_physical(x_out, y_out, gamma=gamma, alpha=alpha)
    else:
        print("    [6/6] No stretching (gamma=0) — Frolich Poisson spacing preserved")

    return x_out, y_out, conv


# ============================================================
#  6.  Export to Tecplot .dat
# ============================================================

def write_tecplot_dat(filepath, x, y, title="Generated grid",
                      zone_title="Adaptive"):
    nj, ni = x.shape
    with open(filepath, "w") as f:
        f.write(f'TITLE     = "{title}"\n')
        f.write('VARIABLES = "x corner"\n')
        f.write('"y corner"\n')
        f.write(f'ZONE T="{zone_title}"\n')
        f.write(f' I={ni}, J={nj}, K=1,F=POINT\n')
        f.write('DT=(DOUBLE DOUBLE )\n')
        for j in range(nj):
            for i in range(ni):
                f.write(f" {x[j, i]: .15E} {y[j, i]: .15E}\n")
    print(f"  [written] {filepath}")


def _fit_a_from_dz_min(L, dz_min_obs, N_cells, tol=1e-12, max_iter=200):
    """
    Bisection: find tanh_wall stretching parameter `a` in (0,1) such that
    the first-cell wall spacing produced by tanh_wall(L, a, j, N_cells)
    matches the observed dz_min on the i=0 column.

    Monotonicity: as a -> 1, near-wall clustering strengthens, so
    dz_min(a) decreases monotonically.

    Returns
    -------
    a_fit       : float — fitted stretching parameter
    dz_min_fit  : float — dz_min(a_fit) for cross-check
    """
    def first_dz(a):
        return tanh_wall(L, a, 1, N_cells) - tanh_wall(L, a, 0, N_cells)

    a_lo, a_hi = 1e-6, 1.0 - 1e-12
    a_mid = 0.5 * (a_lo + a_hi)
    for _ in range(max_iter):
        a_mid = 0.5 * (a_lo + a_hi)
        dz_mid = first_dz(a_mid)
        if dz_mid > dz_min_obs:
            # clustering too weak → push a closer to 1
            a_lo = a_mid
        else:
            a_hi = a_mid
        if abs(dz_mid - dz_min_obs) / max(abs(dz_min_obs), 1e-30) < tol:
            break
    return a_mid, first_dz(a_mid)


def write_grid_data(out_path, x, y, NY, NZ, GAMMA, ALPHA,
                    LZ=None, LY=9.0, source_dat=None):
    """
    Write a human-readable grid_data text report for the i=0 stream-wise
    column (hill crest line, where the zeta-direction grid line is vertical).

    Reports 5 items, all measured / derived on the i=0 column:
      1. dz_max (absolute + multiples of lattice_size)
      2. dz_min (absolute + multiples of lattice_size)
      3. dz_max / dz_min               (stretching ratio)
      4. tanh_wall stretching parameter `a`  (bisection-fit to observed dz_min)
      5. GAMMA                          (Vinokur tanh from variables.h or user input)

    Definitions:
      lattice_size = LZ / (NZ - 1)     (uniform partition over the whole channel)
      L_eff        = LZ - hill_crest   (wall-normal extent above the hill crest;
                                        used as L in tanh_wall back-fit)

    Unit handling:
      The Frohlich reference .dat is in physical units (h_phys ≈ 0.028 m).
      The C simulation rescales it to code units (H_HILL = 1.0) on read
      via grid_scale = H_HILL / h_physical, where h_physical = x_max / LY.
      This routine auto-detects physical-unit grids and rescales to code
      units so the report matches what the simulation actually sees. If
      LZ is provided in code units (e.g. from variables.h LZ=3.036), the
      report stays consistent with that scale.

      If LZ is None, it is inferred from the grid after rescaling.
    """
    import datetime

    out_path = Path(out_path)

    # --- Auto-detect physical vs code units and rescale to code units ---
    # Heuristic: if streamwise span << LY, grid is in physical units.
    x_max_obs = float(x[0, -1])
    if x_max_obs < 0.5 * LY:
        h_physical = x_max_obs / LY
        grid_scale = 1.0 / h_physical            # H_HILL = 1.0 in code units
        units_note = (f"physical units detected (h_phys = {h_physical:.6e}); "
                      f"rescaled by {grid_scale:.6f} to code units")
    else:
        h_physical = None
        grid_scale = 1.0
        units_note = "grid already in code units (H_HILL = 1.0)"

    z_col = y[:, 0] * grid_scale
    dz = np.diff(z_col)
    dz_pos = dz[dz > 0]
    if len(dz_pos) == 0:
        raise ValueError("No positive dz on i=0 column — grid invalid?")
    dz_max = float(dz_pos.max())
    dz_min = float(dz_pos.min())

    if LZ is None:
        LZ = float(z_col[-1])

    hill_crest = float(z_col[0])
    L_eff = LZ - hill_crest
    NZ_cells = NZ - 1
    lattice_size = LZ / NZ_cells

    try:
        a_fit, dz_min_fit = _fit_a_from_dz_min(L_eff, dz_min, NZ_cells)
        a_err_pct = 100.0 * abs(dz_min_fit - dz_min) / dz_min
        a_str = f"{a_fit:.6f}"
        a_check = (f"     dz_min(a_fit) = {dz_min_fit:.6e}"
                   f"  (fit error vs observed = {a_err_pct:.4f}%)\n")
    except Exception as exc:                    # pragma: no cover
        a_str = f"NaN  ({exc})"
        a_check = "     dz_min(a_fit) = N/A\n"

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("=" * 72 + "\n")
        f.write("  Grid Data Analysis  (stream-wise i=0 column / hill crest line)\n")
        f.write("=" * 72 + "\n")
        f.write(f"  Generated  : {datetime.datetime.now().isoformat(timespec='seconds')}\n")
        if source_dat:
            f.write(f"  Source .dat: {source_dat}\n")
        f.write(f"  Units      : {units_note}\n")
        f.write("\n")
        f.write("[Geometry]\n")
        f.write(f"  NY (streamwise nodes)  = {NY}\n")
        f.write(f"  NZ (wall-normal nodes) = {NZ}   (cells = NZ-1 = {NZ_cells})\n")
        f.write(f"  LZ (channel height)    = {LZ:.6f}\n")
        f.write(f"  hill_crest at (i=0,j=0)= {hill_crest:.6f}\n")
        f.write(f"  lattice_size = LZ/(NZ-1) = {LZ:.6f} / {NZ_cells} "
                f"= {lattice_size:.6e}\n")
        f.write("\n")
        f.write("[Wall-normal spacing on i=0 column]\n")
        f.write(f"  1. dz_max         = {dz_max:.6e}"
                f"  =  {dz_max/lattice_size:.4f} * lattice_size\n")
        f.write(f"  2. dz_min         = {dz_min:.6e}"
                f"  =  {dz_min/lattice_size:.4f} * lattice_size\n")
        f.write(f"  3. dz_max/dz_min  = {dz_max/dz_min:.4f}\n")
        f.write("\n")
        f.write("[Stretching parameters]\n")
        f.write(f"  4. tanh_wall a (bisection-fit to dz_min) = {a_str}\n")
        f.write("     formula: z(j) = L/2 + (L/(2a)) * "
                "tanh( ((-1+2j/N)/2) * log((1+a)/(1-a)) )\n")
        f.write(f"     L = LZ - hill_crest = {L_eff:.6f},  N = NZ-1 = {NZ_cells}\n")
        f.write(a_check)
        f.write(f"  5. GAMMA (Vinokur, from variables.h or user input) = {GAMMA:.4f}\n")
        f.write(f"     ALPHA                                            = {ALPHA:.4f}\n")
        f.write("\n")
        f.write("=" * 72 + "\n")

    print(f"  [written] {out_path}")


# ============================================================
#  7.  Verification
# ============================================================

def verify_identity(x_orig, y_orig, x_new, y_new, tol=1e-10):
    dx = np.max(np.abs(x_orig - x_new))
    dy = np.max(np.abs(y_orig - y_new))
    ok = (dx < tol) and (dy < tol)
    return ok, dx, dy


def validate_grid_dimensions(dat_path, NY, NZ):
    """
    Validate that a grid .dat file has the expected dimensions.

    Naming convention:
      NY = streamwise node count  → expected I = NY (nodes)
      NZ = wall-normal node count → expected J = NZ (nodes)

    Returns (ok, ni_actual, nj_actual, ni_expected, nj_expected).
    Raises FileNotFoundError if dat_path does not exist.
    """
    path = Path(dat_path)
    if not path.exists():
        raise FileNotFoundError(f"Grid file not found: {dat_path}")

    ni_expected = NY        # streamwise nodes (I = NY)
    nj_expected = NZ        # wall-normal nodes (J = NZ)

    # Parse I, J from Tecplot header
    ni_actual, nj_actual = None, None
    with open(path) as f:
        for line in f:
            m = re.search(r'I\s*=\s*(\d+)', line)
            if m:
                ni_actual = int(m.group(1))
            m = re.search(r'J\s*=\s*(\d+)', line)
            if m:
                nj_actual = int(m.group(1))
            if ni_actual is not None and nj_actual is not None:
                break

    if ni_actual is None or nj_actual is None:
        raise ValueError(f"Cannot parse I,J from {dat_path}")

    ok = (ni_actual == ni_expected) and (nj_actual == nj_expected)

    if not ok:
        print()
        print("  " + "!" * 62)
        print("  !! GRID DIMENSION MISMATCH — ABORTING !!")
        print("  " + "!" * 62)
        print(f"    Grid file: {path.name}")
        print(f"    Expected:  I={ni_expected} (=NY={NY}), "
              f"J={nj_expected} (=NZ={NZ})")
        print(f"    Actual:    I={ni_actual}, J={nj_actual}")
        if ni_actual != ni_expected:
            print(f"    → xi (streamwise) 格點數不吻合: "
                  f"檔案 I={ni_actual} ≠ NY={ni_expected}")
        if nj_actual != nj_expected:
            print(f"    → zeta (wall-normal) 格點數不吻合: "
                  f"檔案 J={nj_actual} ≠ NZ={nj_expected}")
        print()
        print("    因為輸入之格點與使用者設定不同，不執行程式碼。")
        print("    請確認 variables.h 中 NY, NZ 的值與網格檔案一致。")
        print("  " + "!" * 62)
        print()

    return ok, ni_actual, nj_actual, ni_expected, nj_expected


# ============================================================
#  7b. Grid Spacing Ratio Limiter
# ============================================================
#
#  網格間距比率限制器
#  ==================
#
#  計算公式:
#    Δy(ξ,ζ) = (|y(ξ+1,ζ) - y(ξ,ζ)| + |y(ξ,ζ) - y(ξ-1,ζ)|) / 2
#    Δz(ξ,ζ) = (|z(ξ,ζ+1) - z(ξ,ζ)| + |z(ξ,ζ) - z(ξ,ζ-1)|) / 2
#
#  限制:
#    max(Δy,Δz) / min(Δy,Δz) ∈ [RATIO_LO, RATIO_HI]
#    全場全域搜索，兩方向合併計算
#
#  流程:
#    1. 生成 Poisson 基礎網格 (gamma=0, 無拉伸)
#    2. 對候選 GAMMA 套用 Vinokur 重分布 (O(NI×NJ), ~ms)
#    3. 計算間距比率
#    4. 二分法調整 GAMMA 直到比率落入範圍
#    5. 逐步紀錄調整過程
#
#  Poisson solve 只執行一次，GAMMA 調整只做重分布 + 比率計算
# ============================================================

RATIO_LO_DEFAULT = 12.0
RATIO_HI_DEFAULT = 20.0


def compute_local_spacing(x_grid, y_grid, scale_factor=1.0):
    """
    Compute local grid spacing using central averaging (dimensionless code units).

    Parameters
    ----------
    x_grid : ndarray (nj, ni)  — streamwise coordinate (= y in LBM)
    y_grid : ndarray (nj, ni)  — wall-normal coordinate (= z in LBM)
    scale_factor : float — multiply to convert to code units (H_HILL=1.0)

    Returns
    -------
    delta_y : ndarray (nj, ni) — local streamwise spacing
    delta_z : ndarray (nj, ni) — local wall-normal spacing
    """
    x_c = x_grid * scale_factor
    y_c = y_grid * scale_factor

    # Δy: streamwise (i-direction differences of x_grid)
    dy_fwd = np.abs(np.diff(x_c, axis=1))          # (nj, ni-1)
    delta_y = np.empty_like(x_c)
    delta_y[:, 0] = dy_fwd[:, 0]
    delta_y[:, -1] = dy_fwd[:, -1]
    delta_y[:, 1:-1] = 0.5 * (dy_fwd[:, 1:] + dy_fwd[:, :-1])

    # Δz: wall-normal (j-direction differences of y_grid)
    dz_fwd = np.abs(np.diff(y_c, axis=0))           # (nj-1, ni)
    delta_z = np.empty_like(y_c)
    delta_z[0, :] = dz_fwd[0, :]
    delta_z[-1, :] = dz_fwd[-1, :]
    delta_z[1:-1, :] = 0.5 * (dz_fwd[1:, :] + dz_fwd[:-1, :])

    return delta_y, delta_z


def compute_spacing_ratio(delta_y, delta_z):
    """
    Compute combined global spacing ratio: max(Δy,Δz) / min(Δy,Δz).

    Returns dict with ratio, extremes, and source labels.
    """
    dy_pos = delta_y[delta_y > 1e-30]
    dz_pos = delta_z[delta_z > 1e-30]

    dy_min = float(dy_pos.min()) if len(dy_pos) > 0 else float('inf')
    dy_max = float(dy_pos.max()) if len(dy_pos) > 0 else 0.0
    dz_min = float(dz_pos.min()) if len(dz_pos) > 0 else float('inf')
    dz_max = float(dz_pos.max()) if len(dz_pos) > 0 else 0.0

    max_val = max(dy_max, dz_max)
    min_val = min(dy_min, dz_min)
    max_src = 'Δy' if dy_max >= dz_max else 'Δz'
    min_src = 'Δy' if dy_min <= dz_min else 'Δz'

    ratio = max_val / min_val if min_val > 0 else float('inf')

    return {
        "ratio": ratio,
        "max_val": max_val, "min_val": min_val,
        "dy_min": dy_min, "dy_max": dy_max,
        "dz_min": dz_min, "dz_max": dz_max,
        "max_src": max_src, "min_src": min_src,
    }


def _ratio_at_gamma(x_base, y_base, gamma, alpha, scale_factor):
    """Apply Vinokur redistribution at candidate gamma, return spacing ratio info."""
    if gamma > 1e-14:
        x_t, y_t = redistribute_vertical_physical(
            x_base, y_base, gamma=gamma, alpha=alpha)
    else:
        x_t, y_t = x_base, y_base
    dy, dz = compute_local_spacing(x_t, y_t, scale_factor)
    return compute_spacing_ratio(dy, dz)


def auto_adjust_gamma(x_base, y_base, gamma_init, alpha, scale_factor,
                      ratio_lo=RATIO_LO_DEFAULT, ratio_hi=RATIO_HI_DEFAULT,
                      max_iter=50):
    """
    Auto-adjust GAMMA via bisection on a base grid (Poisson-solved, no stretching).

    The base grid's topology is fixed; only the Vinokur redistribution is varied.
    Each iteration is O(NI×NJ) — no Poisson re-solve.

    Returns
    -------
    gamma_adjusted : float
    log_entries : list of str — per-step adjustment log
    final_info : dict — compute_spacing_ratio result for the adjusted GAMMA
    """
    log = []

    if alpha < 0.5:
        log.append(f"  ⚠ alpha={alpha:.2f} < 0.5: vinokur_tanh 可能非單調，結果需人工確認")

    # Step 0: evaluate initial GAMMA
    info0 = _ratio_at_gamma(x_base, y_base, gamma_init, alpha, scale_factor)
    r0 = info0["ratio"]
    log.append(f"Step 0: GAMMA={gamma_init:.4f} → ratio={r0:.2f} "
               f"(Δy=[{info0['dy_min']:.4e}, {info0['dy_max']:.4e}], "
               f"Δz=[{info0['dz_min']:.4e}, {info0['dz_max']:.4e}])")

    if not np.isfinite(r0):
        log.append(f"  → ratio={r0} 非有限值，無法調整 ✗")
        return gamma_init, log, info0

    if ratio_lo <= r0 <= ratio_hi:
        log.append(f"  → ratio {r0:.2f} ∈ [{ratio_lo}, {ratio_hi}] — 無需調整 ✓")
        return gamma_init, log, info0

    if r0 < ratio_lo:
        log.append(f"  → ratio {r0:.2f} < {ratio_lo}: 拉伸不足，需增加 GAMMA")
        g_lo, g_hi = gamma_init, 20.0
        target = ratio_lo
    else:
        log.append(f"  → ratio {r0:.2f} > {ratio_hi}: 拉伸過強，需減少 GAMMA")
        if gamma_init < 0.1:
            log.append(f"  → GAMMA≈0 但 ratio 已超標 — "
                       f"Poisson 基礎網格本身間距比率過大，無法透過調整 GAMMA 修正")
            return gamma_init, log, info0
        g_lo, g_hi = 0.01, gamma_init
        target = ratio_hi

    best_gamma = None
    best_info = None
    best_dist = float('inf')

    for step in range(1, max_iter + 1):
        g_mid = 0.5 * (g_lo + g_hi)
        info_mid = _ratio_at_gamma(x_base, y_base, g_mid, alpha, scale_factor)
        r_mid = info_mid["ratio"]

        if not np.isfinite(r_mid):
            g_hi = g_mid
            log.append(f"Step {step}: GAMMA={g_mid:.6f} → ratio=非有限值，收縮上界")
            continue

        in_range = ratio_lo <= r_mid <= ratio_hi
        mark = "✓" if in_range else "✗"
        log.append(f"Step {step}: GAMMA={g_mid:.6f} → ratio={r_mid:.4f} "
                   f"(Δz_min={info_mid['dz_min']:.4e}) {mark}")

        if in_range:
            dist = abs(r_mid - target)
            if dist < best_dist:
                best_gamma = g_mid
                best_info = info_mid
                best_dist = dist

        if r_mid < target:
            g_lo = g_mid
        else:
            g_hi = g_mid

        if abs(g_hi - g_lo) < 1e-6:
            break

    if best_gamma is not None:
        stretch_a = float(np.tanh(best_gamma / 2.0))
        log.append(f"  → 收斂: GAMMA={best_gamma:.6f} → ratio={best_info['ratio']:.4f} "
                   f"(STRETCH_A={stretch_a:.6f}, 目標={target:.1f})")
        return best_gamma, log, best_info

    # Fallback: no in-range result found
    g_final = 0.5 * (g_lo + g_hi)
    info_final = _ratio_at_gamma(x_base, y_base, g_final, alpha, scale_factor)
    r_final = info_final["ratio"]
    stretch_a = float(np.tanh(g_final / 2.0))
    in_range = ratio_lo <= r_final <= ratio_hi
    mark = "✓" if in_range else "✗ (未收斂)"
    log.append(f"  → 最大迭代: GAMMA={g_final:.6f} → ratio={r_final:.4f} "
               f"(STRETCH_A={stretch_a:.6f}) {mark}")
    return g_final, log, info_final


def print_spacing_ratio_result(gamma_original, gamma_adjusted, ratio_lo, ratio_hi,
                               final_info, log_entries, NZ, alpha):
    """Print formatted spacing ratio adjustment report."""
    print()
    print("  " + "=" * 68)
    print("   Grid Spacing Ratio Limiter")
    print("  " + "=" * 68)
    print(f"    Constraint: max(Δy,Δz)/min(Δy,Δz) ∈ [{ratio_lo:.1f}, {ratio_hi:.1f}]")
    print(f"    NZ = {NZ}, ALPHA = {alpha:.2f}")
    print()
    print("  Adjustment Log:")
    for entry in log_entries:
        print(f"    {entry}")
    print()
    print(f"    Δy range: [{final_info['dy_min']:.6e}, {final_info['dy_max']:.6e}]")
    print(f"    Δz range: [{final_info['dz_min']:.6e}, {final_info['dz_max']:.6e}]")
    print(f"    Combined ratio: {final_info['ratio']:.4f}")
    print(f"      max from: {final_info['max_src']} = {final_info['max_val']:.6e}")
    print(f"      min from: {final_info['min_src']} = {final_info['min_val']:.6e}")

    if abs(gamma_original - gamma_adjusted) > 1e-6:
        sa_old = float(np.tanh(gamma_original / 2.0))
        sa_new = float(np.tanh(gamma_adjusted / 2.0))
        print()
        print(f"    ⚠ GAMMA 已調整: {gamma_original:.4f} → {gamma_adjusted:.6f}")
        print(f"      STRETCH_A:    {sa_old:.6f} → {sa_new:.6f}")
        print()
        print(f"    請更新 variables.h:")
        print(f"      #define  STRETCH_A  {sa_new:.6f}")
    else:
        print()
        print(f"    ✓ GAMMA={gamma_adjusted:.4f} 滿足限制（無需調整）")

    print("  " + "=" * 68)
    print()


# ============================================================
#  8.  Interactive helpers
# ============================================================

def ask_float(prompt, default, lo=None, hi=None):
    while True:
        raw = input(f"  {prompt} [default={default}]: ").strip()
        if raw == "":
            return default
        try:
            val = float(raw)
        except ValueError:
            print("    ** Invalid number, try again.")
            continue
        if lo is not None and val < lo:
            print(f"    ** Must be >= {lo}, try again.")
            continue
        if hi is not None and val > hi:
            print(f"    ** Must be <= {hi}, try again.")
            continue
        return val


def ask_int(prompt, default, lo=None, hi=None):
    while True:
        raw = input(f"  {prompt} [default={default}]: ").strip()
        if raw == "":
            return default
        try:
            val = int(raw)
        except ValueError:
            print("    ** Invalid integer, try again.")
            continue
        if lo is not None and val < lo:
            print(f"    ** Must be >= {lo}, try again.")
            continue
        if hi is not None and val > hi:
            print(f"    ** Must be <= {hi}, try again.")
            continue
        return val


def ask_yes_no(prompt, default_yes=True):
    hint = "Y/n" if default_yes else "y/N"
    raw = input(f"  {prompt} [{hint}]: ").strip().lower()
    if raw == "":
        return default_yes
    return raw in ("y", "yes")


def detect_dat_files(folder):
    return sorted(f for f in folder.glob("*.dat")
                  if not f.name.startswith("zeta_")
                  and not f.name.startswith("adaptive_"))


# ============================================================
#  9.  Auto-mode: parse variables.h and generate grid
# ============================================================

def parse_variables_h(path):
    """
    Parse #define macros from variables.h.
    Returns dict with keys such as:
      NY, NZ, LZ, LY, H_HILL, CFL, ALPHA, GAMMA, Uref, Re,
      GRID_DAT_DIR, GRID_DAT_REF.
    GAMMA is parsed when present; auto_generate requires it.

    Naming convention (enforced):
      NX, NY, NZ = node count  (格點數)  → cells = NX-1, NY-1, NZ-1
      Grid .dat: I = NY (streamwise nodes), J = NZ (wall-normal nodes)
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    result = {}
    # Integer defines
    for key in ("NY", "NZ"):
        m = re.search(rf'#define\s+{key}\s+(\d+)', text)
        if m:
            result[key] = int(m.group(1))
    # Float defines (may have parentheses)
    for key in ("GAMMA", "ALPHA", "CFL", "Uref", "Re", "STRETCH_A"):
        m = re.search(rf'#define\s+{key}\s+\(?([\d.eE+\-]+)\)?', text)
        if m:
            result[key] = float(m.group(1))
    # Float defines that may be in parentheses like (3.036)
    for key in ("LZ", "LY", "H_HILL", "RATIO_LO", "RATIO_HI"):
        m = re.search(rf'#define\s+{key}\s+\(?([\d.eE+\-]+)\)?', text)
        if m:
            result[key] = float(m.group(1))
    # Derive GAMMA from STRETCH_A when GAMMA is an expression (not a literal number)
    if "GAMMA" not in result and "STRETCH_A" in result:
        a = result["STRETCH_A"]
        if 0.0 < a < 1.0:
            result["GAMMA"] = float(np.log((1.0 + a) / (1.0 - a)))
    # String defines
    for key in ("GRID_DAT_DIR", "GRID_DAT_REF"):
        m = re.search(rf'#define\s+{key}\s+"([^"]+)"', text)
        if m:
            result[key] = m.group(1)
    return result


def update_stretch_a_in_variables_h(path, new_stretch_a):
    """
    Atomically update #define STRETCH_A in variables.h.

    Replaces the numeric literal on the STRETCH_A line while preserving
    all surrounding text, comments, and whitespace.  Only writes the file
    if the value actually changed (avoids unnecessary recompilation).

    Returns True if the file was modified, False if unchanged.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")

    pattern = r'(#define\s+STRETCH_A\s+)[\d.eE+\-]+'
    m = re.search(pattern, text)
    if m is None:
        print(f"  [WARNING] Cannot find #define STRETCH_A in {path}")
        return False

    old_val_str = text[m.start(0) + len(m.group(1)):m.end(0)]
    new_val_str = f"{new_stretch_a:.6f}"

    if old_val_str.strip() == new_val_str.strip():
        return False

    new_text = text[:m.start(0)] + m.group(1) + new_val_str + text[m.end(0):]
    p.write_text(new_text, encoding="utf-8")
    print(f"  [auto-update] variables.h: STRETCH_A {old_val_str} → {new_val_str}")
    return True


def auto_generate(variables_h_path, script_dir=None):
    """
    Fully automatic grid generation:
      1. Parse NY, NZ, LZ, LY, GAMMA, ALPHA from variables.h
      2. GAMMA is required (user design parameter in variables.h)
      3. Compute minSize from GAMMA analytically (gamma_to_minSize)
      4. Load reference grid from GRID_DAT_REF
      5. Run Steger-Sorenson adaptive grid generation (Mode 2)
      6. Export Tecplot .dat with filename matching C code sprintf format
      7. Print GILBM stability check
    Returns: output filepath

    Naming convention:
      NY = streamwise node count  → NI = NY  nodes (grid .dat I dimension)
      NZ = wall-normal node count → NJ = NZ  nodes (grid .dat J dimension)
      Streamwise cells = NY-1,  Wall-normal cells = NZ-1
    """
    if script_dir is None:
        script_dir = Path(__file__).parent

    params = parse_variables_h(variables_h_path)
    required = ["NY", "NZ", "LZ", "ALPHA", "GAMMA", "STRETCH_A",
                "GRID_DAT_REF", "Uref", "Re"]
    for k in required:
        if k not in params:
            raise ValueError(f"Missing #define {k} in {variables_h_path}")

    NY = params["NY"]
    NZ = params["NZ"]          # node count (格點數)
    alpha = params["ALPHA"]
    gamma = params["GAMMA"]
    stretch_a = params["STRETCH_A"]
    ref_name = params["GRID_DAT_REF"]
    LZ = params["LZ"]
    LY = params.get("LY", 9.0)
    H_HILL = params.get("H_HILL", 1.0)
    CFL_val = params.get("CFL", 0.5)
    Uref = params["Uref"]
    Re_val = params["Re"]

    NZ_cells = NZ - 1          # wall-normal cell count (格子數 = NZ-1)

    # Compute minSize from GAMMA (analytic, no bisection)
    # gamma_to_minSize expects cell count, not node count
    if gamma > 0:
        minSize_val = gamma_to_minSize(gamma, LZ, NZ_cells, LY)
    else:
        minSize_val = 0.0  # gamma=0 means no extra stretching

    # Grid .dat dimensions:
    #   I = NY  (streamwise nodes, NY is already node count)
    #   J = NZ  (wall-normal nodes)
    NI = NY
    NJ = NZ

    ref_path = script_dir / ref_name
    if not ref_path.exists():
        raise FileNotFoundError(f"Reference grid not found: {ref_path}")

    # Load reference grid
    x_ref, y_ref, ni_ref, nj_ref = parse_tecplot_dat(ref_path)

    # ── Validate reference grid dimensions ──
    # Reference grid may have different resolution (e.g. Frohlich 129x197)
    # We only log its dimensions; the output will be re-gridded to NI x NJ
    print(f"  [auto] Reference grid: I={ni_ref} x J={nj_ref}")

    print(f"  [auto] variables.h: NY={NY} (nodes), NZ={NZ} (nodes), LZ={LZ}, ALPHA={alpha}")
    print(f"  [auto] Flow: Re={Re_val:g}, Uref={Uref:g}, H_HILL={H_HILL:g}, CFL={CFL_val:g}")
    print(f"  [auto] Wall-normal: {NZ} nodes = {NZ_cells} cells")
    print(f"  [auto] GAMMA={gamma} (user input)")
    if gamma > 0:
        print(f"  [auto] minSize={minSize_val:.6e} (derived from GAMMA)")
    print(f"  [auto] Reference: {ref_path.name}")
    print(f"  [auto] Target grid: I={NI} (=NY) x J={NJ} (=NZ)")

    # ── GILBM stability pre-check (階段 A: 1D 無網格預估) ──
    print_gilbm_stability_table()
    if gamma > 0:
        pre = estimate_omega_pregrid(gamma, LZ, NZ_cells, Uref, Re_val,
                                     H_HILL=H_HILL, CFL=CFL_val, LY=LY,
                                     alpha=alpha)
        print(f"  [pre-check] 1D 預估 (無網格, estimate_omega_pregrid):")
        print(f"    dz_min     = {pre['dz_min']:.6e}")
        print(f"    max|c̃| est = {pre['max_c_est']:.1f}")
        print(f"    dt_est     = {pre['dt_est']:.4e}")
        print(f"    omega_est  = {pre['omega_est']:.4f}", end="")
        if pre["omega_est"] > 2.0:
            print("  *** 預估不穩定 (omega > 2.0) ***")
            print()
            print("  !! WARNING: 1D 預估 omega > 2.0, 網格很可能不穩定 !!")
            print("  !! 建議減小 GAMMA 再試. 繼續生成以取得精確值... !!")
        elif pre["omega_est"] > 1.5:
            print("  ** 預估邊際 (omega > 1.5)")
        else:
            print(f"  [預估穩定]")
        print()

    # ── 讀取間距比率限制 ──
    ratio_lo = params.get("RATIO_LO", RATIO_LO_DEFAULT)
    ratio_hi = params.get("RATIO_HI", RATIO_HI_DEFAULT)
    if ratio_lo > ratio_hi:
        print(f"  [ERROR] RATIO_LO ({ratio_lo}) > RATIO_HI ({ratio_hi}), 請修正 variables.h")
        return None
    print(f"  [auto] Spacing ratio constraint: [{ratio_lo}, {ratio_hi}]")

    # ── 計算 scale factor (物理單位 → 無因次 code 單位) ──
    x_fro_max = x_ref[0, -1]
    h_phys = x_fro_max / LY if x_fro_max < 1.0 else 1.0
    scale = 1.0 / h_phys if h_phys < 0.5 else 1.0

    # ── 生成基礎網格 (Poisson solve, gamma=0 無拉伸) ──
    #    Poisson solve 只執行一次; GAMMA 調整只做 Vinokur 重分布
    x_base, y_base, _ = generate_adaptive_grid(
        x_ref, y_ref, NI, NJ,
        gamma=0.0, alpha=alpha,
        poisson_iter=50000, poisson_tol=1e-12,
        LZ=LZ)

    # ── Validate base grid dimensions ──
    nj_out, ni_out = x_base.shape
    if ni_out != NI or nj_out != NJ:
        print(f"  !! INTERNAL ERROR: generated grid {ni_out}x{nj_out} "
              f"≠ expected {NI}x{NJ} !!")
        sys.exit(1)
    print(f"  [auto] Base grid: I={ni_out} x J={nj_out} ✓")

    # ── 網格間距比率限制器：自動調整 GAMMA ──
    gamma_original = gamma
    gamma, adjust_log, ratio_info = auto_adjust_gamma(
        x_base, y_base, gamma, alpha, scale, ratio_lo, ratio_hi)
    print_spacing_ratio_result(
        gamma_original, gamma, ratio_lo, ratio_hi,
        ratio_info, adjust_log, NZ, alpha)

    # ── 套用調整後的 Vinokur 拉伸 ──
    if gamma > 1e-14:
        print(f"  [auto] Applying Vinokur stretching: GAMMA={gamma:.6f}, ALPHA={alpha}")
        x_out, y_out = redistribute_vertical_physical(
            x_base, y_base, gamma=gamma, alpha=alpha)
    else:
        print(f"  [auto] No stretching (gamma≈0)")
        x_out, y_out = x_base.copy(), y_base.copy()

    # ── 更新 minSize (使用調整後的 GAMMA) ──
    if gamma > 0:
        minSize_val = gamma_to_minSize(gamma, LZ, NZ_cells, LY)
        print(f"  [auto] minSize={minSize_val:.6e} (adjusted GAMMA={gamma:.6f})")

    # ── GILBM stability post-check (階段 B: 2D 精確計算) ──
    stab = estimate_gilbm_stability(
        x_out, y_out, scale_factor=scale,
        Uref=Uref, Re=Re_val, H_HILL=H_HILL,
        CFL_lambda=CFL_val)
    print_gilbm_stability_warning(
        gamma, stab["omega"], stab["c_max"],
        stab["dt_global"], stab["a_max"], stab["status"],
        Uref=stab["Uref"], Re=stab["Re"], H_HILL=stab["H_HILL"],
        CFL_lambda=stab["CFL_lambda"], niu=stab["niu"])

    # 比較階段 A 預估 vs 階段 B 精確值
    if gamma > 0 and 'pre' in dir():
        pre_g = estimate_omega_pregrid(gamma, LZ, NZ_cells, Uref, Re_val,
                                       H_HILL=H_HILL, CFL=CFL_val, LY=LY,
                                       alpha=alpha)
        print(f"  [對照] 調整後 1D預估 omega={pre_g['omega_est']:.4f} vs "
              f"2D精確 omega={stab['omega']:.4f} "
              f"(誤差 {abs(pre_g['omega_est']-stab['omega'])/stab['omega']*100:.1f}%)")

    if stab["status"] == "UNSTABLE":
        print("  !! Grid generation completed but omega > 2.0 !!")
        print("  !! The GILBM simulation WILL DIVERGE with this grid. !!")
        print("  !! Reduce GAMMA in variables.h and regenerate. !!")
        print()

    # ── Post-generation 間距比率驗證 ──
    dy_post, dz_post = compute_local_spacing(x_out, y_out, scale)
    post_info = compute_spacing_ratio(dy_post, dz_post)
    print(f"  [post-verify] Final spacing ratio: {post_info['ratio']:.4f} "
          f"(Δy=[{post_info['dy_min']:.4e}, {post_info['dy_max']:.4e}], "
          f"Δz=[{post_info['dz_min']:.4e}, {post_info['dz_max']:.4e}])")
    if not (ratio_lo <= post_info['ratio'] <= ratio_hi):
        print(f"  !! WARNING: Post-generation ratio {post_info['ratio']:.4f} "
              f"outside [{ratio_lo}, {ratio_hi}] !!")
        print(f"  !! Grid .dat NOT written. !!")
        return None

    # Recompute STRETCH_A from (possibly adjusted) gamma for filename
    sa_for_file = float(np.tanh(gamma / 2.0))
    grid_key = ref_path.stem          # "3.fine grid"
    out_name = f"adaptive_{grid_key}_I{NI}_J{NJ}_s{sa_for_file:.6f}.dat"
    out_path = script_dir / out_name

    write_tecplot_dat(out_path, x_out, y_out,
                      title=f"Periodic hill {NI}x{NJ}",
                      zone_title=f"I{NI}_J{NJ}_s{sa_for_file:.6f}")

    # ── Write grid_data.txt analysis (i=0 column / hill crest line) ──
    grid_data_path = script_dir / f"grid_data_I{NI}_J{NJ}_s{sa_for_file:.6f}.txt"
    write_grid_data(grid_data_path, x_out, y_out,
                    NY=NY, NZ=NZ, GAMMA=gamma, ALPHA=alpha, LZ=LZ,
                    source_dat=out_path.name)

    # ── Validate written .dat file matches NY x NZ ──
    ok, ni_a, nj_a, ni_e, nj_e = validate_grid_dimensions(
        str(out_path), NY, NZ)
    if not ok:
        print("  !! Output .dat file dimension mismatch — ABORTING !!")
        sys.exit(1)
    print(f"  [auto] Output validated: I={ni_a} J={nj_a} ✓ (matches NY={ni_e}, NZ={nj_e})")

    # Also save comparison plot
    tag = f"I{NI}_J{NJ}_s{sa_for_file:.6f}"
    plot_compare(x_ref, y_ref, x_out, y_out,
                 labels=["Reference", f"New ({NI}x{NJ})"],
                 title=f"Auto: GAMMA={gamma:.4f}, STRETCH_A={sa_for_file:.6f}, Grid={NI}x{NJ}",
                 savepath=script_dir / f"compare_auto_{tag}.png")

    if abs(gamma_original - gamma) > 1e-6:
        sa_new = sa_for_file
        print()
        print(f"  ★ GAMMA 已從 {gamma_original:.4f} 自動調整為 {gamma:.6f}")
        print(f"    對應 STRETCH_A = {sa_new:.6f}")
        updated = update_stretch_a_in_variables_h(variables_h_path, sa_new)
        if updated:
            print(f"    ✓ variables.h 已自動更新 STRETCH_A = {sa_new:.6f}")
            print(f"      → main.cu 重編譯後 STRETCH_A 將匹配網格檔名")
        else:
            print(f"    ⚠ variables.h 自動更新失敗，請手動修改:")
            print(f"      #define STRETCH_A {sa_new:.6f}")
        print(f"    輸出檔名使用調整後的 STRETCH_A")

    print(f"  [auto] Output: {out_path}")
    return str(out_path)


# ============================================================
#  MAIN
# ============================================================

if __name__ == "__main__":

    script_dir = Path(__file__).resolve().parent
    base = Path.cwd().resolve()

    # --auto mode: parse variables.h and generate grid non-interactively
    if "--auto" in sys.argv:
        # Find variables.h: search project root (parent of script_dir),
        # current working directory, and relative parent (for cd-based invocation)
        vh_candidates = [
            script_dir.parent / "variables.h",
            Path.cwd() / "variables.h",
            Path("..") / "variables.h",
            Path.cwd().parent / "variables.h",
        ]
        variables_h = None
        for c in vh_candidates:
            if c.exists():
                variables_h = c
                break
        if variables_h is None:
            print("ERROR: Cannot find variables.h")
            print(f"  Searched: {[str(c) for c in vh_candidates]}")
            sys.exit(1)

        print("=" * 62)
        print("  Periodic Hill Grid -- AUTO MODE")
        print(f"  Reading from: {variables_h}")
        print("=" * 62)

        out = auto_generate(str(variables_h), script_dir)
        print("=" * 62)
        if out is None:
            print("  FAILED: 網格生成未通過驗證，未寫入檔案")
            print("=" * 62)
            sys.exit(1)
        print(f"  DONE: {out}")
        print("=" * 62)
        sys.exit(0)

    print()
    print("=" * 62)
    print("  Periodic Hill Grid -- Steger-Sorenson Poisson + Zeta")
    print("  (Interactive Mode)")
    print("=" * 62)

    stability_flow = {}
    stability_LY = 9.0
    variables_h_for_flow = script_dir.parent / "variables.h"
    if variables_h_for_flow.exists():
        try:
            vh_params = parse_variables_h(variables_h_for_flow)
            stability_LY = vh_params.get("LY", stability_LY)
            if "Uref" in vh_params and "Re" in vh_params:
                stability_flow = {
                    "Uref": vh_params["Uref"],
                    "Re": vh_params["Re"],
                    "H_HILL": vh_params.get("H_HILL", 1.0),
                    "CFL_lambda": vh_params.get("CFL", 0.5),
                }
                print(f"  Stability flow params from variables.h: "
                      f"Re={stability_flow['Re']:g}, Uref={stability_flow['Uref']:g}, "
                      f"H_HILL={stability_flow['H_HILL']:g}, CFL={stability_flow['CFL_lambda']:g}")
        except Exception as exc:
            print(f"  [warn] Could not parse stability flow params from variables.h: {exc}")

    # -----------------------------------------------------------
    #  Step 1 -- select reference grid
    # -----------------------------------------------------------
    print("\n" + "-" * 62)
    print("  [Step 1] Select reference grid file")
    print("-" * 62)

    dat_list = detect_dat_files(script_dir)
    if len(dat_list) == 0:
        print("  ERROR: No .dat files found in", script_dir)
        sys.exit(1)

    for idx, fp in enumerate(dat_list):
        print(f"    {idx + 1}. {fp.name}")

    while True:
        raw = input(f"\n  Enter file number [1-{len(dat_list)}] (default=1): ").strip()
        if raw == "":
            choice = 0
            break
        try:
            choice = int(raw) - 1
            if 0 <= choice < len(dat_list):
                break
        except ValueError:
            pass
        print("    ** Invalid choice, try again.")

    dat_path = dat_list[choice]
    grid_key = dat_path.stem

    # -----------------------------------------------------------
    #  Step 2 -- parse reference
    # -----------------------------------------------------------
    print("\n" + "-" * 62)
    print("  [Step 2] Parsing reference grid ...")
    print("-" * 62)

    x_ref, y_ref, ni_ref, nj_ref = parse_tecplot_dat(dat_path)
    print(f"  Reference: {dat_path.name}")
    print(f"  Dimensions: I={ni_ref} (streamwise)  x  J={nj_ref} (vertical)")

    out_orig = base / f"original_{grid_key}.png"
    plot_grid(x_ref, y_ref,
              title=f"Reference: {dat_path.name}  (I={ni_ref}, J={nj_ref})",
              savepath=out_orig)

    # -----------------------------------------------------------
    #  Step 3 -- choose mode
    # -----------------------------------------------------------
    print("\n" + "-" * 62)
    print("  [Step 3] Choose operation mode")
    print("-" * 62)
    print()
    print("    1. Zeta-only  -- keep original Ni x Nj,")
    print("                     adjust vertical stretching (GAMMA/ALPHA)")
    print()
    print("    2. Adaptive   -- freely set new Ni x Nj,")
    print("                     Poisson solve with Steger-Sorenson P,Q")
    print("                     (true elliptic grid generation)")
    print()

    while True:
        raw = input("  Mode [1 or 2] (default=1): ").strip()
        if raw == "":
            mode = 1
            break
        if raw in ("1", "2"):
            mode = int(raw)
            break
        print("    ** Enter 1 or 2.")

    # -----------------------------------------------------------
    #  Step 4 -- set parameters
    # -----------------------------------------------------------
    print("\n" + "-" * 62)
    print("  [Step 4] Set parameters")
    print("-" * 62)

    if mode == 2:
        print()
        print(f"  Reference grid: I={ni_ref}, J={nj_ref}")
        print()
        print("  Ni -- streamwise grid points")
        print(f"         (original = {ni_ref})")
        NI = ask_int("Ni", default=ni_ref, lo=10, hi=2000)
        print()
        print("  Nj -- vertical grid points")
        print(f"         (original = {nj_ref})")
        NJ = ask_int("Nj", default=nj_ref, lo=10, hi=2000)
    else:
        NI = ni_ref
        NJ = nj_ref

    # ── Print GILBM stability reference table before GAMMA selection ──
    print_gilbm_stability_table()

    print()
    print("  GAMMA -- Vinokur stretching in physical z-space")
    print("           0.0 = UNIFORM spacing (no wall clustering, minSize=NaN!)")
    print("           larger value = stronger symmetric wall clustering")
    print("           reference-table omega/status is not valid for every Re/grid")
    print("           actual omega is printed after this grid is generated")
    print()
    GAMMA = ask_float("GAMMA", default=2.0,
                      lo=0.0, hi=10.0)

    print()
    print("  ALPHA -- Vertical symmetry")
    print("           0.5  = symmetric (both walls equal)")
    print("           <0.5 = bottom wall denser")
    print("           >0.5 = top wall denser")
    print()
    ALPHA = ask_float("ALPHA", default=0.5, lo=0.01, hi=0.99)

    # ── 間距比率限制 ──
    ratio_lo_default = RATIO_LO_DEFAULT
    ratio_hi_default = RATIO_HI_DEFAULT
    if variables_h_for_flow.exists():
        try:
            _vhp = parse_variables_h(variables_h_for_flow)
            ratio_lo_default = _vhp.get("RATIO_LO", ratio_lo_default)
            ratio_hi_default = _vhp.get("RATIO_HI", ratio_hi_default)
        except Exception:
            pass
    print()
    print("  RATIO_LO / RATIO_HI -- 網格間距比率限制")
    print("    max(Δy,Δz)/min(Δy,Δz) must be in [RATIO_LO, RATIO_HI]")
    print("    GAMMA will be auto-adjusted to satisfy this constraint")
    print()
    RATIO_LO_VAL = ask_float("RATIO_LO", default=ratio_lo_default, lo=1.0, hi=100.0)
    RATIO_HI_VAL = ask_float("RATIO_HI", default=ratio_hi_default, lo=RATIO_LO_VAL, hi=200.0)

    if mode == 2:
        print()
        print("  Poisson solver iterations")
        print("    (more = more accurate, slower)")
        print("    Typical: 10000~30000 for high accuracy")
        POISSON_ITER = ask_int("Poisson iterations", default=15000, lo=1000, hi=100000)
    else:
        POISSON_ITER = 15000

    print()
    print(f"  -> Mode:  {'Zeta-only' if mode == 1 else 'Adaptive (Poisson + P,Q)'}")
    print(f"  -> Grid:  I={NI} x J={NJ}")
    print(f"  -> GAMMA: {GAMMA}  |  ALPHA: {ALPHA}")
    print(f"  -> Spacing ratio: [{RATIO_LO_VAL}, {RATIO_HI_VAL}]")
    if mode == 2:
        print(f"  -> Poisson iterations: {POISSON_ITER}")

    # ── GILBM stability pre-check (階段 A: 1D 無網格預估) ──
    pre_interactive = None
    if GAMMA > 0 and stability_flow:
        LZ_est = y_ref[-1, 0] - y_ref[0, 0]
        NZ_est = NJ
        pre_interactive = estimate_omega_pregrid(
            GAMMA, LZ_est, NZ_est - 1,
            stability_flow["Uref"], stability_flow["Re"],
            H_HILL=stability_flow["H_HILL"],
            CFL=stability_flow["CFL_lambda"],
            LY=stability_LY, alpha=ALPHA)
        print()
        print(f"  [階段A] 1D 預估 (無網格, estimate_omega_pregrid):")
        print(f"    dz_min     = {pre_interactive['dz_min']:.6e}")
        print(f"    max|c̃| est = {pre_interactive['max_c_est']:.1f}")
        print(f"    omega_est  = {pre_interactive['omega_est']:.4f}", end="")
        if pre_interactive["omega_est"] > 2.0:
            print("  *** 預估不穩定 ***")
        elif pre_interactive["omega_est"] > 1.5:
            print("  ** 預估邊際")
        else:
            print("  [預估穩定]")

    # -----------------------------------------------------------
    #  Step 5 -- identity verification
    # -----------------------------------------------------------
    print("\n" + "-" * 62)
    print("  [Step 5] Identity verification (gamma=0, original size)")
    print("-" * 62)

    x_id, y_id = redistribute_vertical_arclength(x_ref, y_ref, gamma=0.0)
    ok, dx_err, dy_err = verify_identity(x_ref, y_ref, x_id, y_id, tol=1e-10)
    tag = "PASS" if ok else "FAIL"
    print(f"  Arclength identity:  max|dx| = {dx_err:.2e},  max|dy| = {dy_err:.2e}  ->  {tag}")

    # -----------------------------------------------------------
    #  Step 6 -- generate base grid + auto-adjust GAMMA
    # -----------------------------------------------------------
    print("\n" + "-" * 62)
    print("  [Step 6] Generating grid (base Poisson + GAMMA auto-adjust) ...")
    print("-" * 62)

    # Compute scale factor
    x_fro_max = x_ref[0, -1]
    h_phys = x_fro_max / stability_LY if x_fro_max < 1.0 else 1.0
    scale = 1.0 / h_phys if h_phys < 0.5 else 1.0

    poisson_conv = None
    if mode == 1:
        x_base, y_base = x_ref.copy(), y_ref.copy()
    else:
        x_base, y_base, poisson_conv = generate_adaptive_grid(
            x_ref, y_ref, NI, NJ,
            gamma=0.0, alpha=ALPHA,
            poisson_iter=POISSON_ITER, poisson_tol=1e-12)

    print(f"  Base grid: I={NI}, J={NJ}")

    # ── 自動調整 GAMMA ──
    GAMMA_original = GAMMA
    GAMMA, adjust_log, ratio_info = auto_adjust_gamma(
        x_base, y_base, GAMMA, ALPHA, scale, RATIO_LO_VAL, RATIO_HI_VAL)
    print_spacing_ratio_result(
        GAMMA_original, GAMMA, RATIO_LO_VAL, RATIO_HI_VAL,
        ratio_info, adjust_log, NJ, ALPHA)

    # ── 套用調整後的拉伸 ──
    if GAMMA > 1e-14:
        x_new, y_new = redistribute_vertical_physical(
            x_base, y_base, gamma=GAMMA, alpha=ALPHA)
    else:
        x_new, y_new = x_base.copy(), y_base.copy()

    print(f"  Final grid: I={NI}, J={NJ}, GAMMA={GAMMA:.6f}")

    # ── GILBM stability post-check (階段 B: 2D 精確計算) ──
    stab = estimate_gilbm_stability(
        x_new, y_new, scale_factor=scale, **stability_flow)
    print_gilbm_stability_warning(
        GAMMA, stab["omega"], stab["c_max"],
        stab["dt_global"], stab["a_max"], stab["status"],
        Uref=stab["Uref"], Re=stab["Re"], H_HILL=stab["H_HILL"],
        CFL_lambda=stab["CFL_lambda"], niu=stab["niu"])

    if pre_interactive is not None:
        print(f"  [對照] 階段A預估 omega={pre_interactive['omega_est']:.4f} vs "
              f"階段B精確 omega={stab['omega']:.4f} "
              f"(誤差 {abs(pre_interactive['omega_est']-stab['omega'])/max(stab['omega'],1e-30)*100:.1f}%)")

    # ── Post-generation 間距比率驗證 ──
    dy_post, dz_post = compute_local_spacing(x_new, y_new, scale)
    post_info = compute_spacing_ratio(dy_post, dz_post)
    print(f"  [post-verify] ratio={post_info['ratio']:.4f}")
    if not (RATIO_LO_VAL <= post_info['ratio'] <= RATIO_HI_VAL):
        print(f"  !! ERROR: ratio {post_info['ratio']:.4f} outside "
              f"[{RATIO_LO_VAL}, {RATIO_HI_VAL}] !!")
        ans = input("  仍然寫入網格？(y/N): ").strip().lower()
        if ans != 'y':
            print("  [aborted] 網格未寫入。請調整 GAMMA/ALPHA 後重試。")
            sys.exit(1)

    # -----------------------------------------------------------
    #  Step 7 -- output
    # -----------------------------------------------------------
    print("\n" + "-" * 62)
    print("  [Step 7] Saving outputs ...")
    print("-" * 62)

    SA_val = float(np.tanh(GAMMA / 2.0))
    tag_str = f"I{NI}_J{NJ}_s{SA_val:.6f}"

    out_cmp = base / f"compare_{grid_key}_{tag_str}.png"
    plot_compare(x_ref, y_ref, x_new, y_new,
                 labels=["Reference", f"New ({NI}x{NJ})"],
                 title=f"GAMMA={GAMMA}, STRETCH_A={SA_val:.6f}, Grid={NI}x{NJ}",
                 savepath=out_cmp)

    mid_col = NI // 2
    out_sp = base / f"spacing_{grid_key}_{tag_str}.png"
    plot_vertical_spacing(y_ref, y_new, icol=min(mid_col, ni_ref//2),
                          labels=["Reference", f"New ({NI}x{NJ})"],
                          savepath=out_sp)

    out_dat = base / f"adaptive_{grid_key}_{tag_str}.dat"
    write_tecplot_dat(out_dat, x_new, y_new,
                      title=f"Periodic hill {NI}x{NJ}",
                      zone_title=f"I{NI}_J{NJ}_s{SA_val:.6f}")

    # ── Write grid_data.txt analysis (i=0 column / hill crest line) ──
    # Try to read LZ from variables.h (same search paths as auto mode);
    # fall back to grid-derived LZ if variables.h is not found.
    LZ_for_report = None
    vh_candidates = [
        script_dir.parent / "variables.h",
        Path.cwd() / "variables.h",
        Path("..") / "variables.h",
        Path.cwd().parent / "variables.h",
    ]
    for c in vh_candidates:
        if c.exists():
            try:
                vh_params = parse_variables_h(c)
                if "LZ" in vh_params:
                    LZ_for_report = vh_params["LZ"]
                    print(f"  [grid_data] LZ = {LZ_for_report} (from {c})")
                    break
            except Exception:
                pass
    if LZ_for_report is None:
        print("  [grid_data] variables.h not found — LZ will be derived from grid")

    out_grid_data = base / f"grid_data_{tag_str}.txt"
    write_grid_data(out_grid_data, x_new, y_new,
                    NY=NI, NZ=NJ, GAMMA=GAMMA, ALPHA=ALPHA, LZ=LZ_for_report,
                    source_dat=out_dat.name)

    out_new = base / f"grid_{grid_key}_{tag_str}.png"
    plot_grid(x_new, y_new,
              title=f"New grid {NI}x{NJ}  GAMMA={GAMMA}",
              savepath=out_new)

    if mode == 2 and _HAS_MPL:
        fig_cv, ax_cv = plt.subplots(figsize=(8, 5))
        ax_cv.semilogy(poisson_conv, 'k-', lw=0.6)
        ax_cv.set_xlabel("Iteration"); ax_cv.set_ylabel("Max correction")
        ax_cv.set_title(f"Poisson convergence ({NI}x{NJ})")
        ax_cv.grid(True, ls='--', alpha=0.4)
        plt.tight_layout()
        conv_path = base / f"convergence_{grid_key}_{tag_str}.png"
        fig_cv.savefig(conv_path, dpi=200)
        print(f"  [saved] {conv_path}")
        plt.close()

    # -----------------------------------------------------------
    #  Step 8 -- optional parametric sweep
    # -----------------------------------------------------------
    print("\n" + "-" * 62)
    print("  [Step 8] Parametric sweep (optional)")
    print("-" * 62)

    do_sweep = ask_yes_no("Generate parametric sweep plots?", default_yes=False)

    if do_sweep and _HAS_MPL:
        print("  Generating sweep ...")
        gammas = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]

        fig, axes = plt.subplots(len(gammas), 1,
                                 figsize=(18, 3.2 * len(gammas)),
                                 sharex=True)
        for ax, g in zip(axes, gammas):
            if mode == 1:
                xn, yn = redistribute_vertical(x_ref, y_ref, gamma=g, alpha=ALPHA)
            else:
                xn, yn, _ = generate_adaptive_grid(
                    x_ref, y_ref, NI, NJ,
                    gamma=g, alpha=ALPHA, poisson_iter=POISSON_ITER)
            nj_n, ni_n = xn.shape
            for jj in range(nj_n):
                ax.plot(xn[jj, :], yn[jj, :], "k-", lw=0.2)
            for ii in range(0, ni_n, max(1, ni_n//40)):
                ax.plot(xn[:, ii], yn[:, ii], "k-", lw=0.2)
            ax.set_aspect("equal"); ax.set_ylabel("y")
            ax.set_title(f"gamma = {g:.1f}", fontsize=10, loc="left")

        axes[-1].set_xlabel("x  [m]")
        fig.suptitle(f"Parametric sweep (alpha={ALPHA})", fontsize=14)
        plt.tight_layout()
        sweep_path = base / f"sweep_{grid_key}_{tag_str}.png"
        fig.savefig(sweep_path, dpi=200, bbox_inches="tight")
        print(f"  [saved] {sweep_path}")
        plt.close(fig)

        fig3, ax3 = plt.subplots(figsize=(7, 5))
        eta = np.linspace(0, 1, NJ)
        for g in gammas:
            z = vinokur_tanh(eta, g, ALPHA)
            ax3.plot(range(NJ), z, "-", lw=1.2, label=f"gamma={g:.1f}")
        ax3.set_xlabel("j index"); ax3.set_ylabel("zeta (normalised)")
        ax3.set_title(f"Zeta distribution (alpha={ALPHA})")
        ax3.legend(fontsize=8); ax3.grid(True, ls="--", alpha=0.4)
        plt.tight_layout()
        zeta_path = base / "zeta_curves.png"
        fig3.savefig(zeta_path, dpi=200)
        print(f"  [saved] {zeta_path}")
        plt.close(fig3)

    print()
    print("=" * 62)
    print("  ALL DONE")
    print("=" * 62)
