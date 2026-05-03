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
  6. Pre-simulation sensitivity analysis (Mode 3)
  7. Post-simulation z+ verification (--verify)

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

  Validation: P,Q reverse-computation is self-consistent to ~1e-16
  (one Poisson step from original grid).  Full TFI-seeded solve at
  same (Ni,Nj) converges to ~1e-5 at 15k iterations; increase
  iterations or use original grid as initial guess for machine
  precision.
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
    Compute minSize from GAMMA using Vinokur tanh stretching.

    Uses the same vinokur_tanh() that redistribute_vertical_physical()
    uses, so the reported minSize always matches the actual grid.

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

    NJ = NZ_cells + 1   # number of nodes
    eta = np.linspace(0, 1, NJ)
    zeta = vinokur_tanh(eta, gamma, alpha)
    dz = np.diff(zeta)
    minSize = total * np.min(dz)

    return minSize


def vinokur_tanh(eta, gamma, alpha=0.5):
    """
    Vinokur two-sided tanh clustering.  eta in [0,1] -> zeta in [0,1].
    gamma=0 => identity.  Monotonic for all gamma >= 0 and alpha in (0,1).

    General formula (valid for any alpha):
      zeta(eta) = [tanh(gamma*(eta - alpha)) + tanh(gamma*alpha)]
                / [tanh(gamma*(1 - alpha)) + tanh(gamma*alpha)]

    This guarantees zeta(0)=0, zeta(1)=1 exactly for all alpha.
    """
    if gamma < 1e-14:
        return eta.copy()
    t_neg = np.tanh(gamma * alpha)
    t_pos = np.tanh(gamma * (1.0 - alpha))
    denom = t_pos + t_neg
    if abs(denom) < 1e-30:
        return eta.copy()
    zeta = (np.tanh(gamma * (eta - alpha)) + t_neg) / denom
    zeta[0] = 0.0
    zeta[-1] = 1.0
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

def estimate_gilbm_stability(x_grid, y_grid, scale_factor=1.0,
                             Uref=0.0503, Re=150, H_HILL=1.0,
                             CFL_lambda=0.5):
    """
    Estimate GILBM (Generalized Interpolation LBM) stability parameters
    for a given body-fitted grid.

    The LBM MRT collision operator requires omega in approximately [0.5, 2.0].
    omega = 0.5 + 3 * niu / dt_global, where dt_global = CFL_lambda / max|c_tilde|.

    Parameters
    ----------
    x_grid, y_grid : ndarray (nj, ni)
        Grid coordinates (raw Frohlich or code units).
    scale_factor : float
        Multiply grid coords to get code units (=1 if already in code units).
    Uref, Re, H_HILL : float
        Flow parameters. niu = Uref * H_HILL / Re.
    CFL_lambda : float
        CFL number (default 0.5).

    Returns
    -------
    dict with keys:
        omega, dt_global, c_max, dz_min, dz_max, dz_ratio, a_max, status
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
    for alpha in range(3, 19):
        c_zeta = np.abs(zeta_y[sl] * e_y[alpha] + zeta_z[sl] * e_z[alpha])
        c_xi   = np.abs(xi_y[sl]   * e_y[alpha] + xi_z[sl]   * e_z[alpha])
        max_c = max(max_c, c_zeta.max(), c_xi.max())

    # Wall-normal spacing
    dz_min = 1e30
    dz_max = 0.0
    for j in range(ni):
        dz = np.diff(y_c[:, j])
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
    if omega > 2.0:
        status = "UNSTABLE"
    elif omega > 1.5:
        status = "MARGINAL"
    elif omega > 1.2:
        status = "OK"
    elif omega >= 0.55:
        status = "OPTIMAL"
    else:
        status = "GOOD"

    return {
        "omega": omega, "dt_global": dt_global, "c_max": max_c,
        "dz_min": dz_min, "dz_max": dz_max, "dz_ratio": dz_ratio,
        "a_max": a_max, "status": status, "niu": niu,
    }


def print_gilbm_stability_table():
    """
    Print the pre-computed GILBM stability reference table.

    This table was calibrated for:
      Reference grid : Frohlich 3.fine (197x129)
      Target grid    : I=129, J=64 (NY=129 nodes, NZ=64 nodes)
      Grid method    : Mode 2 Poisson + physical-z redistribution
      Flow params    : Re=150, Uref=0.0503, H_HILL=1.0
      CFL lambda     : 0.5

    NOTE: physical-z redistribution REPLACES Frohlich's native wall
    clustering with Vinokur tanh in physical z-space (symmetric when
    alpha=0.5).  GAMMA=0 means UNIFORM spacing (no clustering).
    """
    print()
    print("  " + "=" * 72)
    print("   GILBM Stability Reference  (Poisson + physical-z redistribution)")
    print("   3.fine ref -> 129x64, Re=150, Uref=0.0503, CFL=0.5, ALPHA=0.5")
    print("  " + "=" * 72)
    print(f"  {'GAMMA':>6s} | {'omega':>8s} | {'max|c~|':>10s} | {'dz_ratio':>8s} | {'Status':<12s} | Note")
    print("  " + "-" * 72)
    #                GAMMA  omega   c_max   ratio  status         note
    # Calibrated with redistribute_vertical_physical (2026-03)
    table = [
        (0.0,  0.92,  209,  31, "OPTIMAL",  "UNIFORM z (no clustering) + minSize=NaN!"),
        (0.5,  0.58,   38,   2, "OPTIMAL",  "Very mild symmetric clustering"),
        (1.0,  0.59,   42,   2, "OPTIMAL",  "Mild symmetric clustering"),
        (1.5,  0.60,   50,   3, "OPTIMAL",  "Moderate symmetric clustering"),
        (2.0,  0.63,   63,   4, "OPTIMAL",  "Recommended (good clustering, very stable)"),
        (2.5,  0.67,   83,   5, "OPTIMAL",  "Good clustering"),
        (3.0,  0.73,  112,   8, "OPTIMAL",  "Strong clustering, still optimal"),
        (3.5,  0.81,  156,  12, "OPTIMAL",  "Strong clustering"),
        (4.0,  0.94,  221,  20, "OPTIMAL",  "Very strong (approaching Frohlich-level)"),
        (5.0,  1.43,  463,  52, "OK",       "Extreme clustering, omega > 1.2"),
    ]
    for gamma, omega, c_max, ratio, status, note in table:
        marker = ""
        if gamma == 2.0:
            marker = " <--"
        elif status in ("MARGINAL", "UNSTABLE"):
            marker = " ***"
        print(f"  {gamma:6.1f} | {omega:8.2f} | {c_max:10d} | {ratio:8d} | {status:<12s} | {note}{marker}")
    print("  " + "-" * 72)
    print()
    print("  Physical-z redistribution: GAMMA controls Vinokur tanh in z-space.")
    print("  GAMMA=0 = uniform (NO wall clustering, minSize macro = NaN!).")
    print("  GAMMA=2.0 is recommended: symmetric, ratio=3.5, omega=0.63.")
    print("  All GAMMA <= 4.0 are in OPTIMAL range (omega < 1.0).")
    print()


def print_gilbm_stability_warning(gamma, omega, c_max, dt_global, a_max, status):
    """
    Print a concise GILBM stability warning for the chosen parameters.
    Called after grid generation to alert the user.
    """
    print()
    print("  " + "=" * 62)
    print("   GILBM Stability Check")
    print("  " + "=" * 62)
    print(f"    GAMMA        = {gamma:.4f}")
    print(f"    omega_global = {omega:.4f}", end="")
    if omega > 2.0:
        print("  *** UNSTABLE (omega > 2.0) ***")
    elif omega > 1.5:
        print("  ** MARGINAL (omega > 1.5) **")
    elif omega > 1.2:
        print("  * OK (omega > 1.2)")
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
        print("  !! MRT collision requires omega < 2.0 for stability. !!")
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
        z_col = z_bot + zeta * (z_top - z_bot)
        y_new[:, i] = z_col
        x_new[:, i] = np.interp(z_col, y[:, i], x[:, i])

    ok, min_area, n_bad = _check_cell_areas(x_new, y_new)
    if not ok:
        raise ValueError(
            f"Stretching (gamma={gamma}, alpha={alpha}) created {n_bad} "
            f"non-positive cells (min area = {min_area:.2e}). "
            f"Reduce gamma or move alpha closer to 0.5.")

    return x_new, y_new


# Default: use physical-space redistribution (fixes Frolich asymmetry)
redistribute_vertical = redistribute_vertical_physical


def _dz_norm_closed_form(gamma, N, alpha=0.5):
    """
    Bottom-wall first-cell normalized spacing: zeta(1/N) - zeta(0).

    For alpha=0.5 this equals the top-wall spacing by symmetry.
    For alpha!=0.5 use _dz_norm_top_closed_form() for the top wall.
    """
    if gamma < 1e-14:
        return 1.0 / N
    t_neg = np.tanh(gamma * alpha)
    t_pos = np.tanh(gamma * (1.0 - alpha))
    denom = t_pos + t_neg
    if abs(denom) < 1e-30:
        return 1.0 / N
    return (np.tanh(gamma * (1.0/N - alpha)) + t_neg) / denom


def _dz_norm_top_closed_form(gamma, N, alpha=0.5):
    """
    Top-wall first-cell normalized spacing: zeta(1) - zeta((N-1)/N).

    = 1 - zeta((N-1)/N)
    = [tanh(gamma*(1-alpha)) - tanh(gamma*((N-1)/N - alpha))] / denom
    """
    if gamma < 1e-14:
        return 1.0 / N
    t_neg = np.tanh(gamma * alpha)
    t_pos = np.tanh(gamma * (1.0 - alpha))
    denom = t_pos + t_neg
    if abs(denom) < 1e-30:
        return 1.0 / N
    return (t_pos - np.tanh(gamma * ((N - 1.0)/N - alpha))) / denom


def _gamma_from_dz_norm(target_dz_norm, N, alpha=0.5, tol=1e-12):
    """
    Invert bottom-wall dz_norm(gamma) = target via bisection.
    """
    if target_dz_norm >= 1.0 / N:
        return 0.0
    g_lo, g_hi = 0.0, 25.0
    for _ in range(200):
        g = 0.5 * (g_lo + g_hi)
        if _dz_norm_closed_form(g, N, alpha) > target_dz_norm:
            g_lo = g
        else:
            g_hi = g
        if g_hi - g_lo < tol:
            break
    return 0.5 * (g_lo + g_hi)


def _gamma_from_dz_norm_top(target_dz_norm, N, alpha=0.5, tol=1e-12):
    """
    Invert top-wall dz_norm_top(gamma) = target via bisection.
    """
    if target_dz_norm >= 1.0 / N:
        return 0.0
    g_lo, g_hi = 0.0, 25.0
    for _ in range(200):
        g = 0.5 * (g_lo + g_hi)
        if _dz_norm_top_closed_form(g, N, alpha) > target_dz_norm:
            g_lo = g
        else:
            g_hi = g
        if g_hi - g_lo < tol:
            break
    return 0.5 * (g_lo + g_hi)


def compute_gamma_field(utau_bottom, utau_top, L_column,
                        Re, NZ_cells, alpha=0.5,
                        zp_target=0.9,
                        smooth_max_width=9, smooth_sigma=3):
    """
    Compute streamwise-varying gamma(y) that achieves z+ <= zp_target
    at both walls simultaneously.

    Mathematical basis
    ------------------
    z+(y) = Re * u_tau(y) * d_n(y)

    For Vinokur tanh with symmetric alpha:
        d_n(y) = L(y) * dz_norm(gamma(y), N)

    Setting z+ = zp_target and inverting:
        dz_norm_required(y) = zp_target / (Re * u_tau_design(y) * L(y))
        gamma(y) = dz_norm^{-1}(dz_norm_required)

    Smoothing strategy (one-sided safe)
    ------------------------------------
    Raw u_tau can have sharp local features (separation, reattachment).
    Naive Gaussian smoothing of gamma would REDUCE peaks, causing
    under-resolution.  Instead:

    1. u_tau_design = max(u_tau_bottom, u_tau_top)   at each station
    2. max-filter (morphological dilation) with width W
       -> expands peaks so neighboring columns inherit strong clustering
    3. Gaussian smooth the max-filtered u_tau
       -> removes staircase artifacts from the max-filter
    4. Clamp: u_tau_design = max(u_tau_smooth, u_tau_raw)
       -> guarantees gamma never drops below the required value

    This produces a smooth gamma(y) that is everywhere >= the raw
    requirement, so z+ <= zp_target is guaranteed.

    Parameters
    ----------
    utau_bottom, utau_top : 1D array (NY,)
        Friction velocity at bottom/top wall at each streamwise station.
    L_column : 1D array (NY,)
        Wall-normal column height at each station: z_top - z_bottom.
    Re : float
        Reynolds number.
    NZ_cells : int
        Wall-normal cell count (= NZ_nodes - 1).
    alpha : float
        Vinokur symmetry parameter (0.5 = symmetric).
    zp_target : float
        Target z+ (use < 1.0 for safety margin; default 0.9).
    smooth_max_width : int
        Max-filter window width (odd, in streamwise grid points).
    smooth_sigma : float
        Gaussian smoothing sigma (in grid points).

    Returns
    -------
    gamma_y : 1D array (NY,)
        Stretching parameter at each streamwise station.
    info : dict
        Diagnostic fields: utau_design, dz_norm_required, zp_achieved, etc.
    """
    try:
        from scipy.ndimage import maximum_filter1d, gaussian_filter1d
    except ImportError:
        raise ImportError("scipy is required for compute_gamma_field "
                          "(max-filter + Gaussian smoothing)")

    NY = len(utau_bottom)
    N = NZ_cells

    # Smooth u_tau for each wall independently (one-sided safe)
    def _smooth_utau(utau):
        expanded = maximum_filter1d(utau, size=smooth_max_width, mode="wrap")
        smoothed = gaussian_filter1d(expanded, sigma=smooth_sigma, mode="wrap")
        return np.maximum(smoothed, utau)

    utau_raw_bot = utau_bottom.copy()
    utau_raw_top = utau_top.copy()
    utau_design_bot = _smooth_utau(utau_raw_bot)
    utau_design_top = _smooth_utau(utau_raw_top)

    # For each station, compute gamma required by EACH wall, take the max.
    # Bottom wall uses _dz_norm_closed_form / _gamma_from_dz_norm.
    # Top wall uses _dz_norm_top_closed_form / _gamma_from_dz_norm_top.
    dzn_req_bot = zp_target / (Re * utau_design_bot * L_column)
    dzn_req_top = zp_target / (Re * utau_design_top * L_column)

    gamma_bot = np.array([_gamma_from_dz_norm(d, N, alpha) for d in dzn_req_bot])
    gamma_top = np.array([_gamma_from_dz_norm_top(d, N, alpha) for d in dzn_req_top])
    gamma_y = np.maximum(gamma_bot, gamma_top)

    # Compute actual z+ at both walls using the correct wall spacing
    dzn_bot = np.array([_dz_norm_closed_form(g, N, alpha) for g in gamma_y])
    dzn_top = np.array([_dz_norm_top_closed_form(g, N, alpha) for g in gamma_y])
    dn_bot = dzn_bot * L_column
    dn_top = dzn_top * L_column

    zp_bot = Re * utau_bottom * dn_bot
    zp_top = Re * utau_top * dn_top
    zp_max = np.maximum(zp_bot, zp_top)

    info = {
        "utau_raw_bot": utau_raw_bot,
        "utau_raw_top": utau_raw_top,
        "utau_raw": np.maximum(utau_raw_bot, utau_raw_top),
        "utau_design_bot": utau_design_bot,
        "utau_design_top": utau_design_top,
        "utau_design": np.maximum(utau_design_bot, utau_design_top),
        "dzn_required_bot": dzn_req_bot,
        "dzn_required_top": dzn_req_top,
        "dzn_bot": dzn_bot,
        "dzn_top": dzn_top,
        "dn_bot": dn_bot,
        "dn_top": dn_top,
        "zp_bot": zp_bot,
        "zp_top": zp_top,
        "zp_max": zp_max,
        "zp_target": zp_target,
    }
    return gamma_y, info


def redistribute_vertical_adaptive(x, y, gamma_y, alpha=0.5):
    """
    Redistribute vertical points with streamwise-varying gamma(y).

    Unlike redistribute_vertical_physical() which uses a single global
    gamma for all columns, this applies a different gamma at each
    streamwise station i, as computed by compute_gamma_field().

    Parameters
    ----------
    x, y : ndarray (nj, ni)
        Grid coordinates.  y is wall-normal.
    gamma_y : 1D array (ni,)
        Vinokur gamma at each streamwise station.
    alpha : float
        Clustering symmetry.

    Returns
    -------
    x_new, y_new : ndarray (nj, ni)
    """
    nj, ni = x.shape
    if len(gamma_y) != ni:
        raise ValueError(f"gamma_y length {len(gamma_y)} != ni {ni}")

    eta = np.linspace(0, 1, nj)
    x_new = np.empty_like(x)
    y_new = np.empty_like(y)

    for i in range(ni):
        zeta_i = vinokur_tanh(eta, gamma_y[i], alpha)
        z_bot = y[0, i]
        z_top = y[-1, i]
        z_col = z_bot + zeta_i * (z_top - z_bot)
        y_new[:, i] = z_col
        x_new[:, i] = np.interp(z_col, y[:, i], x[:, i])

    ok, min_area, n_bad = _check_cell_areas(x_new, y_new)
    if not ok:
        raise ValueError(
            f"Adaptive stretching (alpha={alpha}) created {n_bad} "
            f"non-positive cells (min area = {min_area:.2e}). "
            f"Reduce gamma range or move alpha closer to 0.5.")

    return x_new, y_new


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
    nj_new = len(eta_new)
    ni_new = len(xi_new)
    nj_old = len(eta_old)
    ni_old = len(xi_old)
    result = np.empty((nj_new, ni_new))

    for jj in range(nj_new):
        e = eta_new[jj]
        j0 = np.searchsorted(eta_old, e, side='right') - 1
        j0 = max(0, min(j0, nj_old - 2))
        j1 = j0 + 1
        te = (e - eta_old[j0]) / (eta_old[j1] - eta_old[j0]) if eta_old[j1] != eta_old[j0] else 0.0

        for ii in range(ni_new):
            x = xi_new[ii]
            i0 = np.searchsorted(xi_old, x, side='right') - 1
            i0 = max(0, min(i0, ni_old - 2))
            i1 = i0 + 1
            tx = (x - xi_old[i0]) / (xi_old[i1] - xi_old[i0]) if xi_old[i1] != xi_old[i0] else 0.0

            result[jj, ii] = ((1-te)*(1-tx)*data[j0, i0] + (1-te)*tx*data[j0, i1]
                             + te*(1-tx)*data[j1, i0] + te*tx*data[j1, i1])
    return result


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


def _check_cell_areas(x, y):
    """
    Check that all cell areas (cross-product Jacobian) are positive.
    Returns (ok, min_area, n_bad) where n_bad counts non-positive or NaN cells.
    """
    dx_xi = x[:-1, 1:] - x[:-1, :-1]
    dy_xi = y[:-1, 1:] - y[:-1, :-1]
    dx_eta = x[1:, :-1] - x[:-1, :-1]
    dy_eta = y[1:, :-1] - y[:-1, :-1]
    areas = dx_xi * dy_eta - dy_xi * dx_eta
    n_bad = int(np.sum(~(areas > 0)))  # catches <= 0 AND NaN
    return n_bad == 0, float(np.nanmin(areas)), n_bad


class PoissonConvergenceError(RuntimeError):
    """Raised when Poisson solver fails to converge."""
    pass


def generate_adaptive_grid(x_ref, y_ref, ni_new, nj_new,
                           gamma=0.0, alpha=0.5,
                           poisson_iter=15000, poisson_tol=1e-10):
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

    Raises PoissonConvergenceError if the solver diverges, produces
    NaN, or does not reach poisson_tol within poisson_iter iterations.
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

    # ── Convergence gate ──
    if len(conv) == 0:
        raise PoissonConvergenceError("Poisson solver returned no iterations")
    last_corr = conv[-1]
    if np.isnan(last_corr):
        raise PoissonConvergenceError("Poisson solver produced NaN")
    if last_corr > 1e10:
        raise PoissonConvergenceError(
            f"Poisson solver diverged (last correction = {last_corr:.2e})")
    if last_corr > poisson_tol:
        print(f"    WARNING: Poisson NOT converged "
              f"(last_corr={last_corr:.2e} > tol={poisson_tol:.2e})")
        print(f"    Increase poisson_iter (currently {poisson_iter}) "
              f"or relax poisson_tol.")
        raise PoissonConvergenceError(
            f"Poisson solver did not converge: last_corr={last_corr:.2e}, "
            f"tol={poisson_tol:.2e}, iterations={len(conv)}")

    # ── NaN check ──
    if np.any(np.isnan(x_out)) or np.any(np.isnan(y_out)):
        raise PoissonConvergenceError("Poisson grid contains NaN coordinates")

    # ── Positive-area check (before stretching) ──
    ok, min_area, n_bad = _check_cell_areas(x_out, y_out)
    if not ok:
        raise PoissonConvergenceError(
            f"Poisson grid has {n_bad} non-positive cells "
            f"(min area = {min_area:.2e})")
    print(f"    Poisson grid: all cells positive (min area = {min_area:.2e})")

    if gamma > 1e-14:
        print(f"    [6/6] Applying physical-z stretching (gamma={gamma}, alpha={alpha}) ...")
        x_out, y_out = redistribute_vertical_physical(x_out, y_out, gamma=gamma, alpha=alpha)

        ok2, min_area2, n_bad2 = _check_cell_areas(x_out, y_out)
        if not ok2:
            raise PoissonConvergenceError(
                f"Stretching (gamma={gamma}, alpha={alpha}) created "
                f"{n_bad2} non-positive cells (min area = {min_area2:.2e}). "
                f"Reduce gamma or move alpha closer to 0.5.")
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
        f.write('DT=(SINGLE SINGLE )\n')
        for j in range(nj):
            for i in range(ni):
                f.write(f" {x[j, i]: .9E} {y[j, i]: .9E}\n")
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


def verify_zplus(grid_dat, utau_bot_dat, utau_top_dat, Re,
                 alpha=0.5, report_path=None):
    """
    Post-simulation z+ verification.

    Given a grid .dat and NEW u_tau data from the CFD run on that grid,
    compute the ACTUAL z+ at every streamwise station and report
    whether z+ < 1.0 everywhere.

    This is the ground-truth check: it uses the grid's real first-cell
    spacing d_n (not the predicted value from gamma inversion).

    Parameters
    ----------
    grid_dat       : str/Path  grid .dat file
    utau_bot_dat   : str/Path  bottom wall u_tau .dat
    utau_top_dat   : str/Path  top wall u_tau .dat
    Re             : float     Reynolds number
    alpha          : float     Vinokur symmetry parameter
    report_path    : str/Path  optional output report file

    Returns
    -------
    result : dict with keys:
        ok       : bool   True if ALL z+ < 1.0
        zp_max   : float  worst z+
        j_worst  : int    station index of worst z+
        wall     : str    "bottom" or "top" (which wall is worst)
        zp_bot   : 1D array  z+ at each station (bottom)
        zp_top   : 1D array  z+ at each station (top)
        gamma_back : 1D array  back-calculated gamma at each column
    """
    x, y, ni, nj = parse_tecplot_dat(grid_dat)
    _, z_bot, utau_b, n_bot = parse_utau_dat(utau_bot_dat)
    _, z_top, utau_t, n_top = parse_utau_dat(utau_top_dat)

    if n_bot != ni or n_top != ni:
        print(f"  WARNING: u_tau points ({n_bot}/{n_top}) != grid I={ni}")

    # Auto-detect physical vs code units (same logic as write_grid_data)
    x_max = float(x[0, -1])
    LY = 9.0
    if x_max < 0.5 * LY:
        grid_scale = 1.0 / (x_max / LY)
    else:
        grid_scale = 1.0

    # Actual first-cell spacing from grid (both walls)
    dn_bot = np.abs(y[1, :] - y[0, :]) * grid_scale
    dn_top = np.abs(y[-1, :] - y[-2, :]) * grid_scale

    zp_bot = Re * utau_b * dn_bot
    zp_top = Re * utau_t * dn_top
    zp_all = np.maximum(zp_bot, zp_top)

    # Back-calculate gamma at each column
    NZ_cells = nj - 1
    gamma_back_bot = np.empty(ni)
    gamma_back_top = np.empty(ni)
    for i in range(ni):
        L_i = (y[-1, i] - y[0, i]) * grid_scale
        if L_i > 1e-30:
            gamma_back_bot[i] = _gamma_from_dz_norm(dn_bot[i] / L_i, NZ_cells, alpha)
            gamma_back_top[i] = _gamma_from_dz_norm_top(dn_top[i] / L_i, NZ_cells, alpha)
        else:
            gamma_back_bot[i] = 0.0
            gamma_back_top[i] = 0.0
    gamma_back = np.maximum(gamma_back_bot, gamma_back_top)

    # Find worst
    j_worst_b = np.argmax(zp_bot)
    j_worst_t = np.argmax(zp_top)
    if zp_bot[j_worst_b] >= zp_top[j_worst_t]:
        j_worst = j_worst_b
        wall_worst = "bottom"
        zp_worst = zp_bot[j_worst_b]
    else:
        j_worst = j_worst_t
        wall_worst = "top"
        zp_worst = zp_top[j_worst_t]

    ok = bool(zp_worst < 1.0)

    n_over_b = int(np.sum(zp_bot > 1.0))
    n_over_t = int(np.sum(zp_top > 1.0))

    # Print report
    print()
    print("  " + "=" * 62)
    print("   z+ VERIFICATION REPORT")
    print("  " + "=" * 62)
    print(f"    Grid:   {Path(grid_dat).name}  (I={ni}, J={nj})")
    print(f"    Re:     {Re}")
    print(f"    Scale:  {grid_scale:.6f}")
    print()
    print(f"    BOTTOM WALL:")
    print(f"      z+  range: [{zp_bot.min():.4f}, {zp_bot.max():.4f}]")
    print(f"      d_n range: [{dn_bot.min():.6e}, {dn_bot.max():.6e}]")
    print(f"      Stations z+ > 1.0: {n_over_b}/{ni}")
    print()
    print(f"    TOP WALL:")
    print(f"      z+  range: [{zp_top.min():.4f}, {zp_top.max():.4f}]")
    print(f"      d_n range: [{dn_top.min():.6e}, {dn_top.max():.6e}]")
    print(f"      Stations z+ > 1.0: {n_over_t}/{ni}")
    print()
    if ok:
        print(f"    >>> PASS: z+_max = {zp_worst:.4f} < 1.0 <<<")
    else:
        print(f"    >>> FAIL: z+_max = {zp_worst:.4f} > 1.0 <<<")
        print(f"        Worst: j={j_worst}, {wall_worst} wall")
        print(f"        Action: re-run Mode 3 with updated u_tau files")
    print("  " + "=" * 62)

    # Optional: write detailed report
    if report_path is not None:
        report_path = Path(report_path)
        with open(report_path, "w", encoding="utf-8") as rf:
            rf.write(f'TITLE = "z+ verification"\n')
            rf.write('VARIABLES = "j" "zp_bot" "zp_top" "zp_max" '
                      '"dn_bot" "dn_top" "gamma_back"\n')
            rf.write(f'ZONE T="verify", I={ni}, F=POINT\n')
            for i in range(ni):
                rf.write(f"  {i:4d} {zp_bot[i]:10.6f} {zp_top[i]:10.6f} "
                         f"{zp_all[i]:10.6f} {dn_bot[i]:14.8e} "
                         f"{dn_top[i]:14.8e} {gamma_back[i]:10.6f}\n")
        print(f"  [written] {report_path}")

    return {
        "ok": ok,
        "zp_max": zp_worst,
        "j_worst": j_worst,
        "wall": wall_worst,
        "zp_bot": zp_bot,
        "zp_top": zp_top,
        "zp_all": zp_all,
        "gamma_back": gamma_back,
        "n_over_bot": n_over_b,
        "n_over_top": n_over_t,
    }


def sensitivity_analysis(gamma_field, gamma_field_info, L_column,
                          Re, NZ_cells, alpha=0.5, report_path=None):
    """
    Pre-simulation sensitivity analysis.

    Quantifies how much u_tau can increase at each streamwise station
    before z+ exceeds 1.0, given the designed gamma(y) grid.

    This answers the question: "The grid was designed with OLD u_tau.
    How much can u_tau change on the NEW grid before z+ > 1.0?"

    At each station j, the grid has a fixed first-cell spacing:
        d_n(j) = L(j) * dz_norm(gamma(j), N)

    The designed z+ was:
        z+_designed(j) = Re * u_tau_old(j) * d_n(j)

    z+ reaches 1.0 when:
        u_tau_critical(j) = 1.0 / (Re * d_n(j))

    The safety margin is:
        margin(j) = u_tau_critical(j) / u_tau_old(j) - 1

    If margin = 0.11, u_tau can increase by 11% before z+ > 1.0.

    Returns
    -------
    result : dict with keys:
        margin_min     : float   worst-case margin (smallest across all j)
        margin_mean    : float   average margin
        j_weakest      : int     station with smallest margin
        utau_critical  : 1D array  u_tau that would cause z+ = 1.0
        margin         : 1D array  fractional margin at each station
        dn_grid        : 1D array  actual first-cell spacing from gamma(y)
    """
    gi = gamma_field_info
    NY = len(gamma_field)
    N = NZ_cells

    # Compute first-cell spacing at BOTH walls
    dzn_bot = np.array([_dz_norm_closed_form(g, N, alpha) for g in gamma_field])
    dzn_top = np.array([_dz_norm_top_closed_form(g, N, alpha) for g in gamma_field])
    dn_bot = dzn_bot * L_column
    dn_top = dzn_top * L_column

    # Critical u_tau at each wall (the u_tau that would make z+=1.0)
    utau_crit_bot = 1.0 / (Re * dn_bot)
    utau_crit_top = 1.0 / (Re * dn_top)

    # Per-wall u_tau values (raw = unsmoothed, design = smoothed)
    utau_raw_bot = gi.get("utau_raw_bot", gi["utau_raw"])
    utau_raw_top = gi.get("utau_raw_top", gi["utau_raw"])
    utau_des_bot = gi.get("utau_design_bot", gi["utau_design"])
    utau_des_top = gi.get("utau_design_top", gi["utau_design"])

    # Margin = how much u_tau can grow before z+ > 1.0
    # Each wall compared against its OWN u_tau and spacing
    margin_bot_raw = utau_crit_bot / utau_raw_bot - 1.0
    margin_top_raw = utau_crit_top / utau_raw_top - 1.0
    margin_vs_raw = np.minimum(margin_bot_raw, margin_top_raw)

    margin_bot_des = utau_crit_bot / utau_des_bot - 1.0
    margin_top_des = utau_crit_top / utau_des_top - 1.0
    margin_vs_design = np.minimum(margin_bot_des, margin_top_des)

    j_weakest_d = int(np.argmin(margin_vs_design))
    j_weakest_r = int(np.argmin(margin_vs_raw))

    print()
    print("  " + "=" * 62)
    print("   PRE-SIMULATION SENSITIVITY ANALYSIS")
    print("  " + "=" * 62)
    print()
    print("  Question: how much can u_tau increase before z+ > 1.0?")
    print()
    print("  vs. DESIGN u_tau (smoothed, conservative envelope):")
    print(f"    Minimum margin:  {margin_vs_design[j_weakest_d]*100:+.1f}%  "
          f"(station j={j_weakest_d})")
    print(f"    Mean margin:     {margin_vs_design.mean()*100:+.1f}%")
    print(f"    u_tau can increase by at least "
          f"{margin_vs_design[j_weakest_d]*100:.1f}% everywhere")
    print()
    print("  vs. RAW u_tau (actual measured values):")
    print(f"    Minimum margin:  {margin_vs_raw[j_weakest_r]*100:+.1f}%  "
          f"(station j={j_weakest_r})")
    print(f"    Mean margin:     {margin_vs_raw.mean()*100:+.1f}%")
    print(f"    u_tau can increase by at least "
          f"{margin_vs_raw[j_weakest_r]*100:.1f}% everywhere")
    print()
    print("  " + "-" * 62)
    print("  Physical interpretation:")
    print("  " + "-" * 62)
    print("    - Wall shear stress is a GLOBAL flow quantity determined")
    print("      by Re, geometry, and boundary conditions.")
    print("    - Refining from z+ ~ 3 to z+ < 1 improves FD gradient")
    print("      accuracy but does NOT change the flow physics.")
    print("    - Expected u_tau change from grid refinement: 2-5%")
    print("    - Published DNS data (Breuer 2009, Krank 2018) show")
    print("      u_tau variation between grids < 3%.")
    print(f"    - Your safety margin ({margin_vs_raw[j_weakest_r]*100:.1f}%) "
          f"covers this comfortably.")
    print("  " + "=" * 62)

    if report_path is not None:
        report_path = Path(report_path)
        with open(report_path, "w", encoding="utf-8") as rf:
            rf.write(f'TITLE = "Sensitivity analysis"\n')
            rf.write('VARIABLES = "j" "gamma" "dn_bot" "dn_top" '
                      '"utau_crit_bot" "utau_crit_top" '
                      '"margin_vs_raw_pct" "margin_vs_design_pct"\n')
            rf.write(f'ZONE T="sensitivity", I={NY}, F=POINT\n')
            for i in range(NY):
                rf.write(f"  {i:4d} {gamma_field[i]:10.6f} "
                         f"{dn_bot[i]:14.8e} {dn_top[i]:14.8e} "
                         f"{utau_crit_bot[i]:14.8e} {utau_crit_top[i]:14.8e} "
                         f"{margin_vs_raw[i]*100:10.4f} "
                         f"{margin_vs_design[i]*100:10.4f}\n")
        print(f"  [written] {report_path}")

    return {
        "margin_min_raw": float(margin_vs_raw.min()),
        "margin_min_design": float(margin_vs_design.min()),
        "margin_mean_raw": float(margin_vs_raw.mean()),
        "margin_mean_design": float(margin_vs_design.mean()),
        "j_weakest_raw": j_weakest_r,
        "j_weakest_design": j_weakest_d,
        "utau_crit_bot": utau_crit_bot,
        "utau_crit_top": utau_crit_top,
        "margin_vs_raw": margin_vs_raw,
        "margin_vs_design": margin_vs_design,
        "dn_bot": dn_bot,
        "dn_top": dn_top,
    }


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
    skip_prefixes = ("zeta_", "adaptive_", "gamma_field", "sensitivity")
    skip_keywords = ("zplus", "utau", "tauwall", "tau_wall", "verify")
    candidates = []
    for f in sorted(folder.glob("*.dat")):
        name_lower = f.name.lower()
        if any(name_lower.startswith(p) for p in skip_prefixes):
            continue
        if any(k in name_lower for k in skip_keywords):
            continue
        candidates.append(f)
    return candidates


def parse_utau_dat(filepath):
    """
    Parse a Tecplot .dat file containing wall u_tau distribution.

    Auto-detects columns by searching the VARIABLES header for
    'u_tau' and 'z' keywords.  Works with both top-wall (7 col)
    and bottom-wall normal-projection (11 col) formats.

    Returns
    -------
    y_arr    : 1D array  streamwise coordinate
    z_arr    : 1D array  wall-normal coordinate (wall location)
    utau_arr : 1D array  local friction velocity
    ni       : int       number of streamwise stations
    """
    filepath = Path(filepath)
    with open(filepath, "r", encoding="latin-1") as f:
        lines = f.readlines()

    # --- find VARIABLES line and parse column names ---
    col_names = []
    header_end = 0
    ni = None
    for idx, line in enumerate(lines):
        up = line.upper().strip()
        if up.startswith("VARIABLES"):
            raw = line.split("=", 1)[1] if "=" in line else line[9:]
            import shlex
            try:
                col_names = shlex.split(raw.replace('"', '"').replace('"', '"'))
            except ValueError:
                col_names = [t.strip('" ') for t in raw.split('"') if t.strip('" ,')]
        if "I=" in up:
            m = re.search(r'I\s*=\s*(\d+)', up)
            if m:
                ni = int(m.group(1))
        if up.startswith("DT=") or up.startswith("DT ="):
            header_end = idx + 1
            break
        if col_names and ni is not None and not up.startswith("DT"):
            if not any(up.startswith(k) for k in ("TITLE", "VARIABLES", "ZONE", "DT")):
                header_end = idx
                break

    if not col_names:
        raise ValueError(f"Cannot find VARIABLES in {filepath}")

    # --- identify column indices ---
    col_lower = [c.lower() for c in col_names]
    i_y = None
    i_z = None
    i_utau = None
    for ci, name in enumerate(col_lower):
        if name == "y":
            i_y = ci
        if name == "z":
            i_z = ci
        if "u_tau" in name:
            i_utau = ci

    if i_utau is None:
        raise ValueError(f"Cannot find u_tau column in VARIABLES: {col_names}")
    if i_y is None:
        i_y = 1
    if i_z is None:
        i_z = 2

    # --- parse data ---
    data_lines = lines[header_end:]
    y_vals, z_vals, utau_vals = [], [], []
    for dl in data_lines:
        dl = dl.strip()
        if not dl:
            continue
        vals = dl.split()
        if len(vals) < max(i_y, i_z, i_utau) + 1:
            continue
        try:
            y_vals.append(float(vals[i_y]))
            z_vals.append(float(vals[i_z]))
            utau_vals.append(float(vals[i_utau]))
        except (ValueError, IndexError):
            continue

    if ni is not None and len(y_vals) != ni:
        print(f"  WARNING: parsed {len(y_vals)} rows but header says I={ni}")

    return (np.array(y_vals), np.array(z_vals),
            np.array(utau_vals), len(y_vals))


def detect_utau_files(folder):
    """Find u_tau .dat files (files with 'zplus' or 'utau' or 'tau' in name)."""
    candidates = []
    for f in sorted(folder.rglob("*.dat")):
        name_lower = f.name.lower()
        if any(k in name_lower for k in ("zplus", "utau", "tauwall", "tau_wall")):
            candidates.append(f)
    return candidates


def ask_file(prompt, candidates, allow_path=True):
    """Interactive file selector with optional free-path input."""
    print(f"\n  {prompt}")
    if candidates:
        for idx, fp in enumerate(candidates):
            print(f"    {idx + 1}. {fp}")
    if allow_path:
        print(f"    {len(candidates)+1}. Enter path manually")
    print()
    while True:
        raw = input("  Selection: ").strip()
        if not raw:
            continue
        try:
            ci = int(raw) - 1
            if 0 <= ci < len(candidates):
                return candidates[ci]
            if ci == len(candidates) and allow_path:
                pth = input("  Enter file path: ").strip().strip('"')
                p = Path(pth)
                if p.exists():
                    return p
                print(f"    ** File not found: {pth}")
                continue
        except ValueError:
            p = Path(raw.strip('"'))
            if p.exists():
                return p
            print("    ** Invalid selection or file not found.")


# ============================================================
#  9.  Auto-mode: parse variables.h and generate grid
# ============================================================

def parse_variables_h(path):
    """
    Parse #define macros from variables.h.
    Returns dict with keys: NY, NZ, LZ, LY, CFL, ALPHA, GRID_DAT_DIR, GRID_DAT_REF.
    GAMMA is optional (auto-computed from bisection if missing).

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
    for key in ("GAMMA", "ALPHA", "CFL", "ZP_TARGET"):
        m = re.search(rf'#define\s+{key}\s+\(?([\d.eE+\-]+)\)?', text)
        if m:
            result[key] = float(m.group(1))
    # Float defines that may be in parentheses like (3.036)
    for key in ("LZ", "LY"):
        m = re.search(rf'#define\s+{key}\s+\(?([\d.eE+\-]+)\)?', text)
        if m:
            result[key] = float(m.group(1))
    # Integer defines (Re variants)
    for key in ("UTAU_RE",):
        m = re.search(rf'#define\s+{key}\s+(\d+)', text)
        if m:
            result[key] = int(m.group(1))
    # String defines
    for key in ("GRID_DAT_DIR", "GRID_DAT_REF",
                "UTAU_BOT_DAT", "UTAU_TOP_DAT"):
        m = re.search(rf'#define\s+{key}\s+"([^"]+)"', text)
        if m:
            result[key] = m.group(1)
    return result


def _find_existing_adaptive_grid(grid_dir, NI, NJ):
    """Search GRID_DAT_DIR for an existing adaptive grid at NI x NJ."""
    import glob
    pattern = str(grid_dir / f"adaptive_*_I{NI}_J{NJ}_*.dat")
    candidates = sorted(glob.glob(pattern))
    for c in candidates:
        try:
            with open(c) as f:
                for line in f:
                    up = line.upper()
                    if 'I=' in up and 'J=' in up:
                        m_i = re.search(r'I\s*=\s*(\d+)', up)
                        m_j = re.search(r'J\s*=\s*(\d+)', up)
                        if m_i and m_j:
                            if int(m_i.group(1)) == NI and int(m_j.group(1)) == NJ:
                                return Path(c)
                        break
        except Exception:
            continue
    return None


def auto_generate(variables_h_path, script_dir=None):
    """
    Fully automatic grid generation from variables.h.

    Auto-selects mode:
      - UTAU_BOT_DAT + UTAU_TOP_DAT defined → Mode 3 (variable gamma from u_tau)
      - Otherwise → Mode 2 (Poisson + uniform GAMMA)

    Mode 2 pipeline:
      1. Load GRID_DAT_REF (Frohlich reference)
      2. Poisson solve + uniform GAMMA stretching
      3. Write output

    Mode 3 pipeline:
      1. Find existing adaptive grid at NI×NJ as base topology
         (if not found, run Poisson from GRID_DAT_REF with gamma=0)
      2. Load u_tau data from UTAU_BOT_DAT / UTAU_TOP_DAT
      3. Compute gamma(y) field for z+ < ZP_TARGET
      4. Redistribute vertically with variable gamma
      5. Write output

    Output filename matches C code snprintf:
      GRID_DAT_DIR/adaptive_{GRID_DAT_REF stem}_I{NY}_J{NZ}_a{ALPHA:.1f}.dat
    """
    if script_dir is None:
        script_dir = Path(__file__).parent

    params = parse_variables_h(variables_h_path)
    vh_dir = Path(variables_h_path).parent

    required = ["NY", "NZ", "ALPHA", "GAMMA", "GRID_DAT_REF"]
    for k in required:
        if k not in params:
            raise ValueError(f"Missing #define {k} in {variables_h_path}")

    NY = params["NY"]
    NZ = params["NZ"]
    alpha = params["ALPHA"]
    gamma = params["GAMMA"]
    ref_name = params["GRID_DAT_REF"]
    LZ = params.get("LZ", 3.036)
    LY = params.get("LY", 9.0)

    NI = NY
    NJ = NZ
    NZ_cells = NZ - 1

    # Resolve GRID_DAT_DIR (output directory, relative to variables.h)
    grid_dir_name = params.get("GRID_DAT_DIR", "J_Frohlich")
    grid_dir = vh_dir / grid_dir_name
    if not grid_dir.is_dir():
        grid_dir = script_dir  # fallback

    # Output filename: must match C code snprintf
    grid_key = Path(ref_name).stem   # "3.fine grid"
    out_name = f"adaptive_{grid_key}_I{NI}_J{NJ}_a{alpha:.1f}.dat"
    out_path = grid_dir / out_name

    # Resolve GRID_DAT_REF (Frohlich reference, may not exist for Mode 3)
    ref_path = grid_dir / ref_name
    if not ref_path.exists():
        ref_path = script_dir / ref_name

    # ── Detect mode ──
    has_utau = ("UTAU_BOT_DAT" in params and "UTAU_TOP_DAT" in params)

    if has_utau:
        return _auto_mode3(params, vh_dir, grid_dir, ref_path,
                           out_path, grid_key, script_dir)
    else:
        if not ref_path.exists():
            raise FileNotFoundError(f"Reference grid not found: {ref_path}")
        return _auto_mode2(params, grid_dir, ref_path,
                           out_path, grid_key, script_dir)


def _auto_mode2(params, grid_dir, ref_path, out_path, grid_key, script_dir):
    """Auto Mode 2: Poisson + uniform GAMMA."""
    NY, NZ = params["NY"], params["NZ"]
    alpha, gamma = params["ALPHA"], params["GAMMA"]
    LZ = params.get("LZ", 3.036)
    LY = params.get("LY", 9.0)
    NI, NJ = NY, NZ
    NZ_cells = NZ - 1

    if gamma > 0:
        minSize_val = gamma_to_minSize(gamma, LZ, NZ_cells, LY)

    x_ref, y_ref, ni_ref, nj_ref = parse_tecplot_dat(ref_path)

    print(f"  [auto] Mode 2: Poisson + uniform GAMMA")
    print(f"  [auto] Reference grid: {ref_path.name} (I={ni_ref} x J={nj_ref})")
    print(f"  [auto] variables.h: NY={NY}, NZ={NZ}, LZ={LZ}, GAMMA={gamma}, ALPHA={alpha}")
    if gamma > 0:
        print(f"  [auto] minSize={minSize_val:.6e} (from GAMMA)")
    print(f"  [auto] Target: I={NI} x J={NJ}")

    print_gilbm_stability_table()

    x_out, y_out, conv = generate_adaptive_grid(
        x_ref, y_ref, NI, NJ,
        gamma=gamma, alpha=alpha,
        poisson_iter=15000, poisson_tol=1e-12)

    nj_out, ni_out = x_out.shape
    if ni_out != NI or nj_out != NJ:
        sys.exit(f"  !! INTERNAL ERROR: {ni_out}x{nj_out} ≠ {NI}x{NJ}")
    print(f"  [auto] Generated: I={ni_out} x J={nj_out} [OK]")

    x_fro_max = x_ref[0, -1]
    h_phys = x_fro_max / LY if x_fro_max < 1.0 else 1.0
    scale = 1.0 / h_phys if h_phys < 0.5 else 1.0
    stab = estimate_gilbm_stability(x_out, y_out, scale_factor=scale)
    print_gilbm_stability_warning(
        gamma, stab["omega"], stab["c_max"],
        stab["dt_global"], stab["a_max"], stab["status"])

    if stab["status"] == "UNSTABLE":
        print("  !! UNSTABLE: omega > 2.0, reduce GAMMA !!")

    write_tecplot_dat(out_path, x_out, y_out,
                      title=f"Periodic hill {NI}x{NJ}",
                      zone_title=f"I{NI}_J{NJ}_a{alpha}")

    grid_data_path = grid_dir / f"grid_data_I{NI}_J{NJ}_a{alpha:.1f}.txt"
    write_grid_data(grid_data_path, x_out, y_out,
                    NY=NY, NZ=NZ, GAMMA=gamma, ALPHA=alpha, LZ=LZ,
                    source_dat=out_path.name)

    ok, ni_a, nj_a, ni_e, nj_e = validate_grid_dimensions(str(out_path), NY, NZ)
    if not ok:
        sys.exit("  !! Output dimension mismatch !!")
    print(f"  [auto] Validated: I={ni_a} J={nj_a} [OK]")

    tag = f"I{NI}_J{NJ}_a{alpha}"
    plot_compare(x_ref, y_ref, x_out, y_out,
                 labels=["Reference", f"New ({NI}x{NJ})"],
                 title=f"Mode 2: GAMMA={gamma:.4f}, ALPHA={alpha}",
                 savepath=grid_dir / f"compare_auto_{tag}.png")

    print(f"  [auto] Output: {out_path}")
    return str(out_path)


def _auto_mode3(params, vh_dir, grid_dir, ref_path, out_path, grid_key, script_dir):
    """Auto Mode 3: variable gamma(y) from u_tau data."""
    NY, NZ = params["NY"], params["NZ"]
    alpha = params["ALPHA"]
    gamma_vh = params["GAMMA"]
    LZ = params.get("LZ", 3.036)
    LY = params.get("LY", 9.0)
    NI, NJ = NY, NZ
    NZ_cells = NZ - 1

    utau_re = params.get("UTAU_RE", params.get("Re", 5600))
    if isinstance(utau_re, str):
        utau_re = int(utau_re)
    zp_target = params.get("ZP_TARGET", 0.9)

    bot_name = params["UTAU_BOT_DAT"]
    top_name = params["UTAU_TOP_DAT"]
    bot_path = grid_dir / bot_name
    top_path = grid_dir / top_name
    if not bot_path.exists():
        raise FileNotFoundError(f"Bottom u_tau not found: {bot_path}")
    if not top_path.exists():
        raise FileNotFoundError(f"Top u_tau not found: {top_path}")

    print(f"  [auto] Mode 3: variable gamma(y) from u_tau")
    print(f"  [auto] variables.h: NY={NY}, NZ={NZ}, ALPHA={alpha}, GAMMA={gamma_vh}")
    print(f"  [auto] u_tau Re={utau_re}, z+_target={zp_target}")

    # ── Step 1: obtain base topology grid at NI × NJ ──
    base_path = _find_existing_adaptive_grid(grid_dir, NI, NJ)
    if base_path and base_path != out_path:
        print(f"  [auto] Base topology found: {base_path.name}")
        x_base, y_base, ni_b, nj_b = parse_tecplot_dat(base_path)
        print(f"  [auto] Base grid: I={ni_b} x J={nj_b} (skip Poisson)")
    elif ref_path.exists():
        print(f"  [auto] No existing base grid; running Poisson from {ref_path.name}")
        x_ref, y_ref, ni_ref, nj_ref = parse_tecplot_dat(ref_path)
        print(f"  [auto] Reference: I={ni_ref} x J={nj_ref}")
        x_base, y_base, _ = generate_adaptive_grid(
            x_ref, y_ref, NI, NJ,
            gamma=0.0, alpha=alpha,
            poisson_iter=15000, poisson_tol=1e-12)
        print(f"  [auto] Poisson base grid: I={NI} x J={NJ} (gamma=0, no stretching)")
    else:
        raise FileNotFoundError(
            f"Mode 3 needs a base grid: no adaptive grid at I={NI} J={NJ} "
            f"in {grid_dir}, and GRID_DAT_REF '{ref_path}' not found")

    # ── Step 2: load u_tau data ──
    y_bot_ut, z_bot_ut, utau_bot, n_bot = parse_utau_dat(bot_path)
    y_top_ut, z_top_ut, utau_top, n_top = parse_utau_dat(top_path)
    print(f"  [auto] Bottom u_tau: {bot_name} ({n_bot} pts, "
          f"[{utau_bot.min():.6f}, {utau_bot.max():.6f}])")
    print(f"  [auto] Top u_tau:    {top_name} ({n_top} pts, "
          f"[{utau_top.min():.6f}, {utau_top.max():.6f}])")

    if n_bot != NI:
        print(f"  WARNING: bottom u_tau {n_bot} pts ≠ NI={NI}")
    if n_top != NI:
        print(f"  WARNING: top u_tau {n_top} pts ≠ NI={NI}")

    L_col = z_top_ut - z_bot_ut
    print(f"  [auto] Column height L(y): [{L_col.min():.4f}, {L_col.max():.4f}]")

    # ── Step 3: compute gamma(y) field ──
    gamma_field, gamma_info = compute_gamma_field(
        utau_bot, utau_top, L_col,
        Re=utau_re, NZ_cells=NZ_cells, alpha=alpha,
        zp_target=zp_target)

    gi = gamma_info
    print(f"  [auto] gamma(y): [{gamma_field.min():.4f}, {gamma_field.max():.4f}], "
          f"mean={gamma_field.mean():.4f}")
    print(f"  [auto] z+ achieved: max={gi['zp_max'].max():.4f}, "
          f"mean={gi['zp_max'].mean():.4f}")

    n_over = int(np.sum(gi["zp_max"] > 1.0))
    if n_over == 0:
        print(f"  [auto] ALL {NI} stations: z+ < 1.0 [OK]")
    else:
        print(f"  [auto] WARNING: {n_over}/{NI} stations z+ > 1.0")

    # ── Step 4: redistribute with variable gamma ──
    x_out, y_out = redistribute_vertical_adaptive(
        x_base, y_base, gamma_field, alpha=alpha)
    print(f"  [auto] Generated: I={NI} x J={NJ} (variable gamma)")

    # ── Step 5: GILBM stability check ──
    x_fro_max = x_base[0, -1]
    h_phys = x_fro_max / LY if x_fro_max < 1.0 else 1.0
    scale = 1.0 / h_phys if h_phys < 0.5 else 1.0
    stab = estimate_gilbm_stability(x_out, y_out, scale_factor=scale,
                                     Re=utau_re)
    print_gilbm_stability_warning(
        gamma_field.max(), stab["omega"], stab["c_max"],
        stab["dt_global"], stab["a_max"], stab["status"])

    if stab["status"] == "UNSTABLE":
        print("  !! UNSTABLE: omega > 2.0 !!")
        sys.exit(1)

    # ── Step 6: write output grid ──
    write_tecplot_dat(out_path, x_out, y_out,
                      title=f"Periodic hill {NI}x{NJ} variable-gamma",
                      zone_title=f"I{NI}_J{NJ}_a{alpha}")

    ok, ni_a, nj_a, ni_e, nj_e = validate_grid_dimensions(str(out_path), NY, NZ)
    if not ok:
        sys.exit("  !! Output dimension mismatch !!")
    print(f"  [auto] Validated: I={ni_a} J={nj_a} [OK]")

    # ── Step 7: diagnostics ──
    tag = f"I{NI}_J{NJ}_a{alpha:.1f}"

    grid_data_path = grid_dir / f"grid_data_{tag}.txt"
    write_grid_data(grid_data_path, x_out, y_out,
                    NY=NY, NZ=NZ, GAMMA=gamma_field.max(),
                    ALPHA=alpha, LZ=LZ,
                    source_dat=out_path.name)

    sens_path = grid_dir / f"sensitivity_{tag}.dat"
    sensitivity_analysis(gamma_field, gamma_info, L_col,
                         Re=utau_re, NZ_cells=NZ_cells, alpha=alpha,
                         report_path=sens_path)

    gamma_table = grid_dir / f"gamma_field_{tag}.dat"
    with open(gamma_table, "w") as gf:
        gf.write(f'TITLE = "gamma(y) for z+ < {zp_target}"\n')
        gf.write('VARIABLES = "j" "y" "gamma" "zp_bot" "zp_top" "zp_max"\n')
        gf.write(f'ZONE T="gamma_field", I={NI}, F=POINT\n')
        gf.write('DT=(SINGLE SINGLE SINGLE SINGLE SINGLE SINGLE)\n')
        for i in range(NI):
            gf.write(f"  {i:4d} {y_bot_ut[i]:14.8e} {gamma_field[i]:10.6f} "
                     f"{gi['zp_bot'][i]:10.6f} {gi['zp_top'][i]:10.6f} "
                     f"{gi['zp_max'][i]:10.6f}\n")
    print(f"  [written] {gamma_table}")

    plot_compare(x_base, y_base, x_out, y_out,
                 labels=["Base topology", f"Variable gamma (z+<{zp_target})"],
                 title=f"Mode 3: Re={utau_re}, z+_target={zp_target}",
                 savepath=grid_dir / f"compare_auto_{tag}.png")

    # ── Summary ──
    print()
    print(f"  [auto] Output: {out_path}")
    print(f"  [auto] gamma range: [{gamma_field.min():.4f}, {gamma_field.max():.4f}]")
    print(f"  [auto] omega={stab['omega']:.4f} ({stab['status']})")
    print(f"  [auto] variables.h GAMMA={gamma_vh} "
          f"(max gamma_field={gamma_field.max():.4f})")
    if abs(gamma_vh - gamma_field.max()) > 0.01:
        print(f"  [auto] NOTE: consider updating GAMMA in variables.h to "
              f"{gamma_field.max():.4f} (max of gamma field)")

    return str(out_path)


# ============================================================
#  MAIN
# ============================================================

if __name__ == "__main__":

    script_dir = Path(__file__).resolve().parent
    base = Path.cwd().resolve()

    # --verify mode: check z+ from an existing grid + u_tau data
    if "--verify" in sys.argv:
        print()
        print("=" * 62)
        print("  Periodic Hill Grid -- VERIFY MODE (POST-SIMULATION)")
        print("  Check z+ < 1.0 from grid + NEW u_tau data")
        print("  NOTE: u_tau must come from CFD run on THIS grid,")
        print("        not from a previous grid simulation.")
        print("=" * 62)

        # Collect file arguments after --verify
        # Usage: grid_zeta_tool.py --verify <grid.dat> <bot_utau.dat> <top_utau.dat> <Re>
        verify_args = []
        found = False
        for a in sys.argv:
            if found:
                verify_args.append(a)
            if a == "--verify":
                found = True

        if len(verify_args) >= 4:
            v_grid = verify_args[0]
            v_bot = verify_args[1]
            v_top = verify_args[2]
            v_re = float(verify_args[3])
        else:
            # Interactive fallback
            utau_cands = detect_utau_files(script_dir)
            if not utau_cands:
                utau_cands = detect_utau_files(base)
            dat_list = detect_dat_files(script_dir)

            print("\n  Select grid .dat file:")
            for idx, fp in enumerate(dat_list):
                print(f"    {idx + 1}. {fp.name}")
            while True:
                raw = input(f"  Grid file [1-{len(dat_list)}]: ").strip()
                try:
                    ci = int(raw) - 1
                    if 0 <= ci < len(dat_list):
                        v_grid = str(dat_list[ci])
                        break
                except ValueError:
                    if Path(raw.strip('"')).exists():
                        v_grid = raw.strip('"')
                        break
                print("    ** Invalid.")

            v_bot = str(ask_file("Select BOTTOM wall u_tau file:", utau_cands))
            v_top = str(ask_file("Select TOP wall u_tau file:", utau_cands))
            v_re = ask_float("Re", default=5600, lo=1)

        report_out = base / "zplus_verify_report.dat"
        result = verify_zplus(v_grid, v_bot, v_top, v_re,
                              report_path=report_out)

        if result["ok"]:
            print()
            print("  z+ < 1.0 CONFIRMED. Grid is DNS-ready.")
        else:
            print()
            print("  z+ > 1.0 detected. Recommended action:")
            print("    1. Run Mode 3 with the updated u_tau files")
            print("    2. Re-run CFD with the new grid")
            print("    3. Run --verify again")

        print()
        print("=" * 62)
        sys.exit(0 if result["ok"] else 1)

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
        print(f"  DONE: {out}")
        print("=" * 62)
        sys.exit(0)

    print()
    print("=" * 62)
    print("  Periodic Hill Grid -- Steger-Sorenson Poisson + Zeta")
    print("  (Interactive Mode)")
    print("=" * 62)

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
    print("    1. Zeta-only      -- keep Ni x Nj, uniform GAMMA")
    print()
    print("    2. Adaptive       -- new Ni x Nj, Poisson + uniform GAMMA")
    print()
    print("    3. Variable gamma -- keep Ni x Nj, load u_tau(y) from CFD,")
    print("                         compute gamma(y) for z+ < 1 everywhere")
    print()

    while True:
        raw = input("  Mode [1, 2 or 3] (default=1): ").strip()
        if raw == "":
            mode = 1
            break
        if raw in ("1", "2", "3"):
            mode = int(raw)
            break
        print("    ** Enter 1, 2 or 3.")

    # -----------------------------------------------------------
    #  Step 3b -- (Mode 3) load u_tau data
    # -----------------------------------------------------------
    gamma_field = None       # 1D array for Mode 3, None otherwise
    gamma_field_info = None

    if mode == 3:
        print("\n" + "-" * 62)
        print("  [Step 3b] Load wall u_tau data for variable gamma(y)")
        print("-" * 62)

        utau_candidates = detect_utau_files(script_dir)
        if not utau_candidates:
            utau_candidates = detect_utau_files(base)

        # ── bottom wall ──
        bot_path = ask_file("Select BOTTOM wall u_tau file:", utau_candidates)
        y_bot_ut, z_bot_ut, utau_bot_arr, n_bot = parse_utau_dat(bot_path)
        print(f"  Bottom: {bot_path.name}  ({n_bot} points)")
        print(f"    u_tau range: [{utau_bot_arr.min():.6f}, {utau_bot_arr.max():.6f}]")

        # ── top wall ──
        top_path = ask_file("Select TOP wall u_tau file:", utau_candidates)
        y_top_ut, z_top_ut, utau_top_arr, n_top = parse_utau_dat(top_path)
        print(f"  Top:    {top_path.name}  ({n_top} points)")
        print(f"    u_tau range: [{utau_top_arr.min():.6f}, {utau_top_arr.max():.6f}]")

        if n_bot != ni_ref or n_top != ni_ref:
            print(f"\n  WARNING: u_tau data ({n_bot}/{n_top} pts) vs grid I={ni_ref}")
            print("  Dimensions should match. Proceeding anyway ...")

        # ── column heights from u_tau data (code units, not grid units) ──
        # u_tau files contain z-coordinates in code units (H_HILL=1),
        # while the grid .dat may be in physical units (Frohlich ~0.028 m).
        # L_col must be in the same unit system as u_tau.
        L_col = z_top_ut - z_bot_ut
        print(f"  Column height L(y): [{L_col.min():.4f}, {L_col.max():.4f}] "
              f"(from u_tau files, code units)")

        # ── Re ──
        RE_val = ask_float("Re (Reynolds number)", default=5600, lo=1)

        # ── z+ target ──
        print()
        print("  z+_target -- DNS target (set < 1.0 for safety margin)")
        print("               0.9 = 10% margin (recommended)")
        print("               1.0 = exact DNS limit")
        ZP_TARGET = ask_float("z+_target", default=0.9, lo=0.1, hi=1.5)

        # ── ALPHA ──
        print()
        print("  ALPHA -- Vertical symmetry (0.5 = symmetric)")
        ALPHA = ask_float("ALPHA", default=0.5, lo=0.01, hi=0.99)

        # ── smoothing (advanced, with defaults) ──
        print()
        if ask_yes_no("Adjust smoothing parameters?", default_yes=False):
            SMOOTH_W = ask_int("Max-filter width (odd)", default=9, lo=3, hi=31)
            SMOOTH_S = ask_float("Gaussian sigma", default=3.0, lo=0.5, hi=10.0)
        else:
            SMOOTH_W = 9
            SMOOTH_S = 3.0

        NI = ni_ref
        NJ = nj_ref
        NZ_cells = NJ - 1

        # ── compute gamma(y) ──
        print("\n" + "-" * 62)
        print("  [Step 3c] Computing gamma(y) field ...")
        print("-" * 62)

        gamma_field, gamma_field_info = compute_gamma_field(
            utau_bot_arr, utau_top_arr, L_col,
            Re=RE_val, NZ_cells=NZ_cells, alpha=ALPHA,
            zp_target=ZP_TARGET,
            smooth_max_width=SMOOTH_W, smooth_sigma=SMOOTH_S)

        gi = gamma_field_info
        print(f"  gamma(y) range:  [{gamma_field.min():.3f}, {gamma_field.max():.3f}]")
        print(f"  z+ achieved:     max={gi['zp_max'].max():.4f}  "
              f"mean={gi['zp_max'].mean():.4f}  min={gi['zp_max'].min():.4f}")
        n_over = np.sum(gi["zp_max"] > 1.0)
        if n_over == 0:
            print(f"  ALL {NI} stations: z+ < 1.0")
        else:
            print(f"  WARNING: {n_over}/{NI} stations have z+ > 1.0")
            print(f"  Consider lowering z+_target or increasing NZ.")

        # stretching ratio at gamma_max
        eta_tmp = np.linspace(0, 1, NJ)
        zeta_tmp = vinokur_tanh(eta_tmp, gamma_field.max(), ALPHA)
        dz_tmp = np.diff(zeta_tmp)
        ratio_max = dz_tmp.max() / dz_tmp.min()
        print(f"  Max stretching ratio: {ratio_max:.1f}:1 "
              f"(at gamma={gamma_field.max():.2f})")

        print()
        print(f"  -> Mode:  Variable gamma(y)")
        print(f"  -> Grid:  I={NI} x J={NJ}")
        print(f"  -> Re:    {RE_val}")
        print(f"  -> z+_target: {ZP_TARGET}")
        print(f"  -> ALPHA: {ALPHA}")
        print(f"  -> Smoothing: W={SMOOTH_W}, sigma={SMOOTH_S}")

        GAMMA = gamma_field.mean()
        POISSON_ITER = 15000

        # ── Pre-simulation sensitivity analysis ──
        tag_str = f"I{NI}_J{NJ}_adaptive_a{ALPHA}"
        sens_report = base / f"sensitivity_{tag_str}.dat"
        sens = sensitivity_analysis(
            gamma_field, gamma_field_info, L_col,
            Re=RE_val, NZ_cells=NZ_cells, alpha=ALPHA,
            report_path=sens_report)

    # -----------------------------------------------------------
    #  Step 4 -- set parameters (Mode 1 & 2 only)
    # -----------------------------------------------------------
    if mode in (1, 2):
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

        print_gilbm_stability_table()

        print()
        print("  GAMMA -- Vinokur stretching in physical z-space")
        print("           0.0 = UNIFORM spacing (no wall clustering)")
        print("           2.0 = recommended (good clustering, stable)")
        print("           >=5.0 = extreme (use with caution)")
        print()
        GAMMA = ask_float("GAMMA", default=2.0, lo=0.0, hi=10.0)

        print()
        print("  ALPHA -- Vertical symmetry (0.5 = symmetric)")
        print()
        ALPHA = ask_float("ALPHA", default=0.5, lo=0.01, hi=0.99)

        if mode == 2:
            print()
            print("  Poisson solver iterations")
            POISSON_ITER = ask_int("Poisson iterations",
                                   default=15000, lo=1000, hi=100000)
        else:
            POISSON_ITER = 15000

        print()
        print(f"  -> Mode:  {'Zeta-only' if mode == 1 else 'Adaptive (Poisson + P,Q)'}")
        print(f"  -> Grid:  I={NI} x J={NJ}")
        print(f"  -> GAMMA: {GAMMA}  |  ALPHA: {ALPHA}")
        if mode == 2:
            print(f"  -> Poisson iterations: {POISSON_ITER}")

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

    if mode == 2 and NI == ni_ref and NJ == nj_ref:
        print()
        print("  Poisson P/Q self-consistency check (1-step from original) ...")
        metrics_chk = _compute_metrics(x_ref, y_ref)
        P_chk, Q_chk = _compute_PQ(metrics_chk)
        x_1step, y_1step, _ = _poisson_solve(
            x_ref.copy(), y_ref.copy(), P_chk, Q_chk,
            n_iter=1, omega=1.0, tol=0, print_every=0)
        dx_pq = np.max(np.abs(x_1step - x_ref))
        dy_pq = np.max(np.abs(y_1step - y_ref))
        tag_pq = "PASS" if max(dx_pq, dy_pq) < 1e-12 else "FAIL"
        print(f"  P/Q 1-step:  max|dx| = {dx_pq:.2e},  max|dy| = {dy_pq:.2e}  ->  {tag_pq}")
        print(f"  (Note: full TFI-seeded Poisson solve converges slower;"
              f" increase iterations for higher accuracy)")

    # -----------------------------------------------------------
    #  Step 6 -- generate new grid
    # -----------------------------------------------------------
    print("\n" + "-" * 62)
    print("  [Step 6] Generating new grid ...")
    print("-" * 62)

    if mode == 1:
        x_new, y_new = redistribute_vertical_physical(x_ref, y_ref,
                                              gamma=GAMMA, alpha=ALPHA)
    elif mode == 2:
        try:
            x_new, y_new, poisson_conv = generate_adaptive_grid(
                x_ref, y_ref, NI, NJ,
                gamma=GAMMA, alpha=ALPHA,
                poisson_iter=POISSON_ITER, poisson_tol=1e-12)
        except PoissonConvergenceError as e:
            print(f"\n  ERROR: {e}")
            print(f"  The Poisson solver did not produce a valid grid.")
            print(f"  Try increasing iterations or relaxing tolerance.")
            sys.exit(1)
    else:
        x_new, y_new = redistribute_vertical_adaptive(
            x_ref, y_ref, gamma_field, alpha=ALPHA)

    print(f"  Generated grid: I={NI}, J={NJ}")

    if mode == 3:
        print(f"  gamma(y): [{gamma_field.min():.3f} .. {gamma_field.max():.3f}]")

    # ── GILBM stability post-check ──
    x_fro_max = x_ref[0, -1]
    h_phys = x_fro_max / 9.0 if x_fro_max < 1.0 else 1.0
    scale = 1.0 / h_phys if h_phys < 0.5 else 1.0
    stab = estimate_gilbm_stability(x_new, y_new, scale_factor=scale)
    gamma_for_report = gamma_field.max() if mode == 3 else GAMMA
    print_gilbm_stability_warning(
        gamma_for_report, stab["omega"], stab["c_max"],
        stab["dt_global"], stab["a_max"], stab["status"])

    # -----------------------------------------------------------
    #  Step 7 -- output
    # -----------------------------------------------------------
    print("\n" + "-" * 62)
    print("  [Step 7] Saving outputs ...")
    print("-" * 62)

    if mode == 3:
        tag_str = f"I{NI}_J{NJ}_adaptive_a{ALPHA}"
    else:
        tag_str = f"I{NI}_J{NJ}_g{GAMMA}_a{ALPHA}"

    out_cmp = base / f"compare_{grid_key}_{tag_str}.png"
    plot_compare(x_ref, y_ref, x_new, y_new,
                 labels=["Reference", f"New ({NI}x{NJ})"],
                 title=(f"Variable gamma(y), ALPHA={ALPHA}" if mode == 3
                        else f"GAMMA={GAMMA}, ALPHA={ALPHA}, Grid={NI}x{NJ}"),
                 savepath=out_cmp)

    mid_col = NI // 2
    out_sp = base / f"spacing_{grid_key}_{tag_str}.png"
    plot_vertical_spacing(y_ref, y_new, icol=min(mid_col, ni_ref//2),
                          labels=["Reference", f"New ({NI}x{NJ})"],
                          savepath=out_sp)

    out_dat = base / f"adaptive_{grid_key}_{tag_str}.dat"
    write_tecplot_dat(out_dat, x_new, y_new,
                      title=f"Periodic hill {NI}x{NJ}",
                      zone_title=f"I{NI}_J{NJ}_a{ALPHA}")

    # ── Write grid_data.txt ──
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
        print("  [grid_data] variables.h not found — LZ from grid")

    out_grid_data = base / f"grid_data_{tag_str}.txt"
    write_grid_data(out_grid_data, x_new, y_new,
                    NY=NI, NZ=NJ, GAMMA=gamma_for_report, ALPHA=ALPHA,
                    LZ=LZ_for_report, source_dat=out_dat.name)

    out_new = base / f"grid_{grid_key}_{tag_str}.png"
    plot_grid(x_new, y_new,
              title=(f"Variable gamma(y) [{gamma_field.min():.1f}..{gamma_field.max():.1f}]"
                     if mode == 3 else f"New grid {NI}x{NJ}  GAMMA={GAMMA}"),
              savepath=out_new)

    # ── Mode 3: save gamma(y) table ──
    if mode == 3:
        gamma_table_path = base / f"gamma_field_{tag_str}.dat"
        with open(gamma_table_path, "w") as gf:
            gf.write(f'TITLE = "gamma(y) field for z+ < {ZP_TARGET}"\n')
            gf.write('VARIABLES = "j" "y" "gamma" "utau_design" '
                      '"zp_bot" "zp_top" "zp_max"\n')
            gf.write(f'ZONE T="gamma_field", I={NI}, F=POINT\n')
            gi = gamma_field_info
            for i in range(NI):
                gf.write(f"  {i:4d} {y_bot_ut[i]:14.8e} {gamma_field[i]:10.6f} "
                         f"{gi['utau_design'][i]:14.8e} "
                         f"{gi['zp_bot'][i]:10.6f} {gi['zp_top'][i]:10.6f} "
                         f"{gi['zp_max'][i]:10.6f}\n")
        print(f"  [saved] {gamma_table_path}")

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

    # ── Mode 3: plot gamma(y) distribution ──
    if mode == 3 and _HAS_MPL:
        gi = gamma_field_info
        fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)

        axes[0].plot(range(NI), gamma_field, 'b-', lw=1.5)
        axes[0].set_ylabel("gamma(y)")
        axes[0].set_title("Variable gamma(y) field")
        axes[0].grid(True, ls='--', alpha=0.4)

        axes[1].plot(range(NI), gi["utau_raw"], 'k-', lw=0.8, label="u_tau raw")
        axes[1].plot(range(NI), gi["utau_design"], 'r-', lw=1.2,
                     label="u_tau design (smoothed)")
        axes[1].set_ylabel("u_tau")
        axes[1].legend(fontsize=9)
        axes[1].grid(True, ls='--', alpha=0.4)

        axes[2].plot(range(NI), gi["zp_bot"], 'b-', lw=0.8, label="z+ bottom")
        axes[2].plot(range(NI), gi["zp_top"], 'r-', lw=0.8, label="z+ top")
        axes[2].plot(range(NI), gi["zp_max"], 'k-', lw=1.2, label="z+ max")
        axes[2].axhline(1.0, color='gray', ls='--', lw=0.8)
        axes[2].set_ylabel("z+")
        axes[2].set_xlabel("streamwise index j")
        axes[2].legend(fontsize=9)
        axes[2].grid(True, ls='--', alpha=0.4)

        plt.tight_layout()
        gplot_path = base / f"gamma_field_{tag_str}.png"
        fig.savefig(gplot_path, dpi=200)
        print(f"  [saved] {gplot_path}")
        plt.close(fig)

        # ── Sensitivity margin plot ──
        fig_s, ax_s = plt.subplots(figsize=(14, 5))
        ax_s.fill_between(range(NI), sens["margin_vs_raw"] * 100,
                          color='green', alpha=0.3, label="margin vs raw u_tau")
        ax_s.plot(range(NI), sens["margin_vs_raw"] * 100,
                  'g-', lw=1.0)
        ax_s.plot(range(NI), sens["margin_vs_design"] * 100,
                  'b--', lw=1.0, label="margin vs design u_tau")
        ax_s.axhline(0, color='red', ls='-', lw=1.5)
        ax_s.set_xlabel("streamwise index j")
        ax_s.set_ylabel("margin before z+ > 1.0 (%)")
        ax_s.set_title("Sensitivity: u_tau increase tolerance at each station")
        ax_s.legend(fontsize=9)
        ax_s.grid(True, ls='--', alpha=0.4)
        plt.tight_layout()
        splot_path = base / f"sensitivity_{tag_str}.png"
        fig_s.savefig(splot_path, dpi=200)
        print(f"  [saved] {splot_path}")
        plt.close(fig_s)

    # -----------------------------------------------------------
    #  Step 8 -- optional parametric sweep (Mode 1 & 2 only)
    # -----------------------------------------------------------
    if mode in (1, 2):
        print("\n" + "-" * 62)
        print("  [Step 8] Parametric sweep (optional)")
        print("-" * 62)

        do_sweep = ask_yes_no("Generate parametric sweep plots?",
                              default_yes=False)

        if do_sweep and _HAS_MPL:
            print("  Generating sweep ...")
            gammas = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]

            fig, axes = plt.subplots(len(gammas), 1,
                                     figsize=(18, 3.2 * len(gammas)),
                                     sharex=True)
            for ax, g in zip(axes, gammas):
                if mode == 1:
                    xn, yn = redistribute_vertical(x_ref, y_ref,
                                                   gamma=g, alpha=ALPHA)
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
