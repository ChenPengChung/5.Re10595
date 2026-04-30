#!/usr/bin/env python3
"""
Monitor Convergence Plot — Periodic Hill Flow
==============================================
Reads Ustar_Force_record.dat (4-col legacy or 7-col new format)
and checkrho.dat (density conservation check).

Layout (3 or 4 subplots, top → bottom):
  Panel 1 (top):     Ma_max
  Panel 2 (middle):  Ub/Uref (left y-axis, blue) + F* (right y-axis, green)
  Panel 3:           <ρ> mass conservation  (purple, zoomed y-axis)
  Panel 4 (turbulent + FTT > FTT_STATS_START only):
                     RS/TKE convergence statistics
                     x-axis: FTT_STATS_START → end (accumulated data only)

Flow Regime Classification (§1 — Re-based):
  Re ≤ 150  →  Laminar    (3 panels; convergence by iterative residual ε)
  Re >  150  →  Turbulent  (4 panels when FTT > FTT_STATS_START;
                             convergence by CV of RS/TKE statistics)

  WHY Re = 150 as threshold:
    Periodic hill flow transitions around Re ≈ 150.  Below this value
    the solution is steady (no turbulence), so iterative convergence
    (ε → 0) is the only meaningful criterion.  Above it, turbulent
    fluctuations appear and statistical stationarity of Reynolds
    stresses / TKE must be verified over accumulated samples.

Accumulation Rules (§2–§3 — FTT_STATS_START):
  - RS/TKE statistics accumulate ONLY after FTT > FTT_STATS_START.
  - WHY: the initial transient (FTT ≤ FTT_STATS_START) contaminates
    statistics.  Waiting ensures samples represent the statistically-
    stationary state, not startup artefacts.
  - If FTT ≤ FTT_STATS_START:
      • No RS/TKE panel is created  (no empty plot, no placeholder)
      • No 'accumulate start' label is shown
  - If FTT > FTT_STATS_START:
      • RS/TKE panel appears, plotting ONLY post-accumulation data
      • 'accumulate start' label appears on the Ub/F* panel

RS/TKE Plot Range (§5):
  The bottom RS/TKE panel's x-axis starts at FTT_STATS_START, NOT at 0.
  This ensures the chart reflects "statistics from accumulation onset"
  and pre-accumulation data is excluded from the view.

Convergence Criteria (§6 — separated by flow regime):
  Laminar (Re ≤ 150):
    ε = max|ΔU*| over last 10% of data.
    Thresholds:  ε < 1e-8 → CONVERGED,  ε < 1e-7 → NEAR,  else NOT.
    Applied on Ub/Uref in the middle panel.
    (simple iterative steady-state check — appropriate for laminar)

  Turbulent (Re > 150):
    CV = σ/|μ| over last 10 FTT, on RS and TKE.
    Thresholds:  CV < 1% → CONVERGED,  CV < 5% → NEAR,  else NOT.
    Applied ONLY on post-accumulation data (FTT > FTT_STATS_START).
    (statistical stationarity check — appropriate for turbulence)

  These two criteria are NEVER mixed: laminar never uses CV,
  turbulent never uses ε on primary variables.

Auto-Parameters:
  Re, Uref, FTT_STATS_START are parsed from ../variables.h automatically.

Usage:
  python3 4.Ma_U_Time.py                # auto-detect Re from variables.h
  python3 4.Ma_U_Time.py --Re 5600      # override Re

Output:
  monitor_convergence_Re{N}.pdf / .png
"""

import os, sys, re as _re, argparse
import numpy as np

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import matplotlib as mpl
if not os.environ.get('DISPLAY') and sys.platform != 'win32':
    mpl.use('Agg')
import matplotlib.pyplot as plt

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in dir() else os.getcwd()

# ─── Convergence parameters (parsed from variables.h at startup) ───
# These module-level defaults are OVERWRITTEN by parse_variables_h().
# The C++ variables.h is the SINGLE SOURCE OF TRUTH.
# DO NOT edit these values here — change variables.h instead.
LAMINAR_RE_THRESHOLD = 150    # overwritten by parse_variables_h()
EPS_CONVERGED        = 1e-8   # overwritten by parse_variables_h()
EPS_NEAR             = 1e-7   # overwritten by parse_variables_h()
CV_CONVERGED         = 1.0    # overwritten by parse_variables_h()
CV_NEAR              = 5.0    # overwritten by parse_variables_h()
CV_WINDOW_FTT        = 10.0   # overwritten by parse_variables_h()

# ═══════════════════════════════════════════════════════════════
#  1. Flow Regime Classification (§1)
#
#  Re ≤ 150  →  LAMINAR   — no turbulence, solution converges
#                           iteratively to a unique steady state.
#                           Convergence criterion: iterative residual ε.
#  Re >  150  →  TURBULENT — time-dependent fluctuations present,
#                           convergence assessed via statistical
#                           stationarity of accumulated RS/TKE (CV).
# ═══════════════════════════════════════════════════════════════

def is_laminar(Re):
    """Laminar if Re ≤ 150 (periodic hill transition threshold)."""
    return Re <= LAMINAR_RE_THRESHOLD

def should_show_turbulence_panel(Re):
    """RS/TKE statistics panel is relevant only for turbulent flow (Re > 150)."""
    return not is_laminar(Re)

# ═══════════════════════════════════════════════════════════════
#  2. Parse variables.h
# ═══════════════════════════════════════════════════════════════

def parse_variables_h():
    """Extract ALL convergence-related parameters from ../variables.h.

    This is the SINGLE synchronisation point between C++ and Python.
    Every convergence threshold used by this script is parsed here;
    no value should be hardcoded elsewhere.

    Parsed keys (with fallback defaults matching the C++ header):
      Re, Uref, FTT_STATS_START,
      LAMINAR_RE_THRESHOLD, EPS_CONVERGED, EPS_NEAR,
      CV_CONVERGED, CV_NEAR, CV_WINDOW_FTT
    """
    vh_path = os.path.join(SCRIPT_DIR, '..', 'variables.h')
    result = {
        'Re':                   None,
        'Uref':                 None,
        'FTT_STATS_START':      20.0,
        'LAMINAR_RE_THRESHOLD': 150,
        'EPS_CONVERGED':        1e-8,
        'EPS_NEAR':             1e-7,
        'CV_CONVERGED':         1.0,
        'CV_NEAR':              5.0,
        'CV_WINDOW_FTT':        10.0,
    }
    if not os.path.isfile(vh_path):
        print('[WARN] variables.h not found — using defaults')
        return result
    with open(vh_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            for key in result:
                m = _re.match(rf'^\s*#define\s+{key}\s+([\d.eE+\-]+)', line)
                if m:
                    result[key] = float(m.group(1))
    return result


def _apply_parsed_convergence_params(vh):
    """Push parsed values from variables.h into module-level globals.

    Called once in main() so that every function in this module sees
    the C++-authoritative thresholds without explicit parameter passing.
    """
    global LAMINAR_RE_THRESHOLD, EPS_CONVERGED, EPS_NEAR
    global CV_CONVERGED, CV_NEAR, CV_WINDOW_FTT
    LAMINAR_RE_THRESHOLD = int(vh['LAMINAR_RE_THRESHOLD'])
    EPS_CONVERGED        = float(vh['EPS_CONVERGED'])
    EPS_NEAR             = float(vh['EPS_NEAR'])
    CV_CONVERGED         = float(vh['CV_CONVERGED'])
    CV_NEAR              = float(vh['CV_NEAR'])
    CV_WINDOW_FTT        = float(vh['CV_WINDOW_FTT'])

# ═══════════════════════════════════════════════════════════════
#  3. Data Loading — Ustar_Force_record.dat
# ═══════════════════════════════════════════════════════════════

def load_monitor_data(filepath):
    """
    Load Ustar_Force_record.dat (mixed 4/7/10/12-col safe).

    Columns (12-col new format from monitor.h, 2026-04):
      0: FTT
      1: Ub/Uref
      2: F*  (= Force_h × Ly / Uref²,  already dimensionless)
      3: Ma_max
      4: accu_count
      5: <u'u'>/Uref²  (RS check point value)
      6: k/Uref²        (TKE check point value)
      7: GPU_min        (GPU timing)
      8: Error          (Ub 迭代殘差 δ, 層流/紊流皆計算)
      9: CV_uu%         (紊流 <u'u'> 的 CV%; 層流=-1)
     10: CV_k%          (紊流 k 的 CV%; 層流=-1)
     11: Conv           (C++ convergence status: 0/1/2)

    Legacy (10-col format):
      8: Error = laminar δ OR max(CV_uu, CV_k) [混用, deprecated]
      9: Conv

    The loader auto-detects column count and handles both formats.
    """
    if not os.path.isfile(filepath):
        sys.exit(f"[ERROR] File not found: {filepath}")

    rows = []
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            vals = line.split()
            try:
                rows.append([float(v) for v in vals])
            except ValueError:
                continue
    if not rows:
        sys.exit(f"[ERROR] No valid data in {filepath}")

    max_cols = max(len(r) for r in rows)
    data = np.array([r + [np.nan] * (max_cols - len(r)) for r in rows])
    if data.ndim == 1:
        data = data.reshape(1, -1)

    assert data.shape[1] >= 4, \
        f"[ERROR] Ustar_Force_record.dat needs ≥4 columns, got {data.shape[1]}"

    result = {
        'FTT':     data[:, 0],
        'Ub_Uref': data[:, 1],
        'F_star':  data[:, 2],
        'Ma_max':  data[:, 3],
        'has_rs':  max_cols >= 7,
        'has_error': max_cols >= 10,
        'has_cv_cols': max_cols >= 12,   # new 12-col format
    }
    if result['has_rs']:
        result['accu_count'] = data[:, 4]
        result['uu_RS']      = data[:, 5]
        result['k_TKE']      = data[:, 6]
    if result['has_cv_cols']:
        # New 12-col format: Error=δ, CV_uu%, CV_k%, Conv
        result['cpp_error']  = data[:, 8]    # Ub 迭代殘差 (always valid)
        result['cpp_cv_uu']  = data[:, 9]    # CV_uu% (-1 = laminar)
        result['cpp_cv_k']   = data[:, 10]   # CV_k% (-1 = laminar)
        result['cpp_conv']   = data[:, 11]   # convergence status
    elif result['has_error']:
        # Legacy 10-col format: Error=mixed, Conv
        result['cpp_error']  = data[:, 8]
        result['cpp_conv']   = data[:, 9]
    return result

# ═══════════════════════════════════════════════════════════════
#  3b. Data Loading — checkrho.dat  (density conservation)
# ═══════════════════════════════════════════════════════════════

def load_checkrho(filepath):
    """
    Load checkrho.dat for mass conservation monitoring.

    Expected columns (whitespace-separated, no header):
      col 0: timestep number
      col 1: FTT
      col 2: field value (e.g. rho_min or another check)
      col 3: instantaneous volume-averaged density <ρ>

    Returns dict {'FTT': array, 'rho_avg': array} or None if file missing.
    """
    if not os.path.isfile(filepath):
        print(f"[WARN] checkrho.dat not found at {filepath} — skipping density panel")
        return None

    rows = []
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            vals = line.split()
            try:
                rows.append([float(v) for v in vals])
            except ValueError:
                continue

    if not rows:
        print(f"[WARN] No valid data in checkrho.dat")
        return None

    # Filter to rows with ≥4 columns (skip malformed / ragged lines)
    rows = [r for r in rows if len(r) >= 4]
    if not rows:
        print(f"[WARN] No rows with ≥4 columns in checkrho.dat")
        return None
    # Trim all rows to same length (minimum across rows) to avoid ragged array
    ncol = min(len(r) for r in rows)
    data = np.array([r[:ncol] for r in rows])
    if data.ndim < 2 or data.shape[1] < 4:
        print(f"[ERROR] checkrho.dat needs ≥4 columns, got {data.shape[1]}")
        return None

    result = {
        'FTT':     data[:, 1],   # col 1 = FTT
        'rho_avg': data[:, 3],   # col 3 = volume-averaged density
    }

    # Validation: print summary
    rho = result['rho_avg']
    finite = rho[np.isfinite(rho)]
    if len(finite) > 0:
        rho_min, rho_max = finite.min(), finite.max()
        rho_mean = finite.mean()
        rho_std  = finite.std()
        print(f"[INFO] checkrho.dat: {len(finite)} rows, "
              f"<ρ> ∈ [{rho_min:.10f}, {rho_max:.10f}], "
              f"mean={rho_mean:.10f}, σ={rho_std:.2e}")
    return result

# ═══════════════════════════════════════════════════════════════
#  4. Label Anti-Overlap Engine (Smart Placement)
# ═══════════════════════════════════════════════════════════════

def _data_to_axes_frac(ax, y_data_values):
    """Convert data-space y values to axes-fraction [0,1] coordinates."""
    ylo, yhi = ax.get_ylim()
    rng = yhi - ylo
    if abs(rng) < 1e-30:
        return np.full_like(y_data_values, 0.5, dtype=float)
    return (np.asarray(y_data_values, dtype=float) - ylo) / rng


def find_clear_2d(ax, data_series_list,
                  label_w=0.30, label_h=0.18,
                  margin=0.03, nx=20, ny=20,
                  exclude_y_bands=None,
                  extra_occupied_xy_fracs=None):
    """
    2D grid search for the best (x, y) position for a label box.

    Unlike find_clear_y (1D vertical scan at fixed x), this scans the
    ENTIRE axes area to find the rectangle with the fewest data points
    inside. This correctly detects large empty regions like the space
    below stabilised curves in panel (d).

    Parameters
    ----------
    ax : matplotlib Axes
    data_series_list : list of (ftt_array, y_array) in DATA coordinates
    label_w, label_h : float
        Label width and height in axes fraction.
    margin : float
        Minimum distance from axes edge.
    nx, ny : int
        Grid resolution for x and y scanning.
    exclude_y_bands : list of (y_lo, y_hi) in axes fraction
    extra_occupied_xy_fracs : list of (x_frac_arr, y_frac_arr)
        Pre-converted twin-axis data in axes-fraction space.

    Returns
    -------
    (x_frac, y_frac, ha, va) : best position + alignment
    """
    xlo, xhi = ax.get_xlim()
    x_range = xhi - xlo if abs(xhi - xlo) > 1e-30 else 1.0

    # Collect all data as (x_frac, y_frac) points.
    # CRITICAL: densify via linear interpolation so continuous curves are
    # reliably detected — sparse discrete samples can miss overlaps.
    n_dense = 300   # interpolation target per series
    pts_xf, pts_yf = [], []

    def _densify(xf, yf):
        """Linearly interpolate to n_dense evenly spaced x-frac points."""
        if len(xf) < 2:
            return xf, yf
        x_dense = np.linspace(float(xf.min()), float(xf.max()), n_dense)
        y_dense = np.interp(x_dense, xf, yf)
        return x_dense, y_dense

    for ftt_arr, y_arr in (data_series_list or []):
        ftt_arr = np.asarray(ftt_arr, dtype=float)
        y_arr = np.asarray(y_arr, dtype=float)
        mask = np.isfinite(ftt_arr) & np.isfinite(y_arr)
        if np.any(mask):
            xf = (ftt_arr[mask] - xlo) / x_range
            yf = _data_to_axes_frac(ax, y_arr[mask])
            # Sort by x before interpolation
            order = np.argsort(xf)
            xf_d, yf_d = _densify(xf[order], yf[order])
            pts_xf.append(xf_d)
            pts_yf.append(yf_d)

    for xf_a, yf_a in (extra_occupied_xy_fracs or []):
        xf_a = np.asarray(xf_a, dtype=float)
        yf_a = np.asarray(yf_a, dtype=float)
        mask = np.isfinite(xf_a) & np.isfinite(yf_a)
        if np.any(mask):
            order = np.argsort(xf_a[mask])
            xf_d, yf_d = _densify(xf_a[mask][order], yf_a[mask][order])
            pts_xf.append(xf_d)
            pts_yf.append(yf_d)

    if pts_xf:
        all_xf = np.concatenate(pts_xf)
        all_yf = np.concatenate(pts_yf)
    else:
        # No data → bottom-left
        return (margin, margin, 'left', 'bottom')

    _exclude = exclude_y_bands or []

    # Generate candidate anchor positions (bottom-left corner of label)
    x_candidates = np.linspace(margin, 1.0 - label_w - margin, nx)
    y_candidates = np.linspace(margin, 1.0 - label_h - margin, ny)

    best_x, best_y = margin, margin
    best_score = -1e30

    buf = 0.04  # buffer zone around box for "near miss" penalty

    for cx in x_candidates:
        box_x0, box_x1 = cx, cx + label_w
        # Pre-filter: points in x-band (with buffer)
        x_mask = (all_xf >= box_x0 - buf) & (all_xf <= box_x1 + buf)

        for cy in y_candidates:
            box_y0, box_y1 = cy, cy + label_h

            # Count data points INSIDE the box (strict)
            inside = int(np.sum(
                (all_xf >= box_x0) & (all_xf <= box_x1) &
                (all_yf >= box_y0) & (all_yf <= box_y1)))

            # Count data points in the buffer zone (near-miss)
            near = int(np.sum(
                x_mask &
                (all_yf >= box_y0 - buf) & (all_yf <= box_y1 + buf))) - inside

            # Minimum distance from box centre to any data point
            bcx, bcy = (box_x0 + box_x1) / 2, (box_y0 + box_y1) / 2
            dists = np.sqrt((all_xf - bcx)**2 + (all_yf - bcy)**2)
            min_dist = float(np.min(dists))

            # Score: heavily penalize overlap, moderately penalize near-miss
            score = -inside * 50.0 - near * 5.0 + min_dist

            # Exclusion zone penalty (legend bbox)
            for band_lo, band_hi in _exclude:
                if box_y0 < band_hi and box_y1 > band_lo:
                    overlap = min(box_y1, band_hi) - max(box_y0, band_lo)
                    score -= overlap * 50.0

            if score > best_score:
                best_score = score
                best_x, best_y = cx, cy

    # Determine alignment for matplotlib text
    ha = 'left'
    va = 'bottom'

    return (best_x, best_y, ha, va)


def find_clear_y(ax, data_series_list, x_frac_region=(0.55, 1.0),
                 label_height=0.12, margin=0.06, n_candidates=40,
                 va='center', exclude_y_bands=None,
                 extra_occupied_y_fracs=None,
                 extra_occupied_xy_fracs=None):
    """
    Find the best y-position (axes fraction) for a label.

    PRIORITY ORDER (strict):
      1. The ENTIRE label (including bbox) MUST stay inside axes [0, 1].
         This is the HARD constraint — never violated.
      2. TRY to avoid overlapping data curves AND exclude_y_bands.
      3. If all positions overlap, choose best clearance inside bounds.

    Parameters
    ----------
    ax : matplotlib Axes
    data_series_list : list of (ftt_array, y_array)
        Data in primary-axis DATA coordinates.  Converted internally via
        ``_data_to_axes_frac(ax, ...)``.
    x_frac_region : tuple (lo, hi)
    label_height : float
        Estimated FULL label height in axes fraction (including bbox).
    margin : float
        Extra safety margin from top/bottom edges.
    n_candidates : int
    va : str
        Vertical alignment of the label ('top', 'center', 'bottom').
    exclude_y_bands : list of (y_lo, y_hi) in axes fraction
        Rectangular exclusion zones (e.g., legend bbox).
        The engine treats these as occupied regions to avoid.
    extra_occupied_y_fracs : list of np.ndarray, optional
        **Pre-converted** y-fraction arrays (already in [0, 1] axes space).
        Merged without x-filtering. Use only when x-region doesn't matter.
    extra_occupied_xy_fracs : list of (x_frac_array, y_frac_array), optional
        **Pre-converted** (x, y) fraction pairs. These ARE filtered by
        x_frac_region, giving accurate collision detection for twin-axis
        data. Preferred over extra_occupied_y_fracs.

    Returns
    -------
    float : best y in axes fraction, guaranteed to keep label inside.
    """
    # Compute safe anchor bounds based on va so the ENTIRE label stays in [0,1]
    # Use small margin from edges — the axes already have generous data padding,
    # so label can safely reach near the edges where the empty space is.
    half_h = label_height / 2.0
    safe_margin = max(margin, 0.03)  # 3% from edge (was 8%, too restrictive)
    if va == 'top':
        y_lo = label_height + safe_margin
        y_hi = 1.0 - safe_margin
    elif va == 'bottom':
        y_lo = safe_margin
        y_hi = 1.0 - label_height - safe_margin
    else:  # 'center'
        y_lo = half_h + safe_margin
        y_hi = 1.0 - half_h - safe_margin

    # Minimal floor/ceiling — just keep bbox from touching axes frame.
    # Old values (0.12, 0.88) were too restrictive and prevented labels
    # from reaching the empty padded regions below/above data curves.
    y_lo = max(y_lo, 0.03)
    y_hi = min(y_hi, 0.97)
    if y_hi < y_lo:
        y_hi = y_lo  # degenerate: label is nearly as tall as axes

    fallback = (y_lo + y_hi) / 2.0

    if not data_series_list:
        return fallback

    xlo, xhi = ax.get_xlim()
    x_range = xhi - xlo if abs(xhi - xlo) > 1e-30 else 1.0
    x_frac_lo, x_frac_hi = x_frac_region

    occupied_y_fracs = []
    for ftt_arr, y_arr in data_series_list:
        ftt_arr = np.asarray(ftt_arr, dtype=float)
        y_arr = np.asarray(y_arr, dtype=float)
        x_fracs = (ftt_arr - xlo) / x_range
        mask = (x_fracs >= x_frac_lo) & (x_fracs <= x_frac_hi) & np.isfinite(y_arr)
        if np.any(mask):
            y_fracs = _data_to_axes_frac(ax, y_arr[mask])
            occupied_y_fracs.append(y_fracs)

    # Merge pre-converted twin-axis fractions (if any)
    if extra_occupied_y_fracs:
        for yf_arr in extra_occupied_y_fracs:
            yf_arr = np.asarray(yf_arr, dtype=float)
            if len(yf_arr) > 0:
                occupied_y_fracs.append(yf_arr)

    # Merge twin-axis (x, y) fraction pairs WITH x-region filtering
    if extra_occupied_xy_fracs:
        for xf_arr, yf_arr in extra_occupied_xy_fracs:
            xf_arr = np.asarray(xf_arr, dtype=float)
            yf_arr = np.asarray(yf_arr, dtype=float)
            mask = ((xf_arr >= x_frac_lo) & (xf_arr <= x_frac_hi)
                    & np.isfinite(yf_arr))
            if np.any(mask):
                occupied_y_fracs.append(yf_arr[mask])

    if not occupied_y_fracs:
        return fallback

    all_occupied = np.concatenate(occupied_y_fracs)
    all_occupied = all_occupied[(all_occupied >= -0.1) & (all_occupied <= 1.1)]
    if len(all_occupied) == 0:
        return fallback

    # Collect exclusion zones (legend bbox, etc.)
    _exclude = exclude_y_bands or []

    # Sample candidates ONLY within safe bounds
    candidates = np.linspace(y_hi, y_lo, n_candidates)
    best_y = fallback
    best_clearance = -1.0

    n_total = len(all_occupied)

    for cy in candidates:
        # Check clearance from label's full vertical span, not just anchor
        if va == 'top':
            label_top, label_bot = cy, cy - label_height
        elif va == 'bottom':
            label_top, label_bot = cy + label_height, cy
        else:
            label_top, label_bot = cy + half_h, cy - half_h

        # Minimum distance from any occupied point to the label band
        dists_to_band = np.where(
            all_occupied > label_top,
            all_occupied - label_top,
            np.where(all_occupied < label_bot,
                     label_bot - all_occupied,
                     0.0)  # inside the band → distance = 0
        )
        # Count data points INSIDE the label band → primary penalty
        n_inside = int(np.sum(dists_to_band == 0.0))
        # Fraction of data inside: 0.0 = perfect, 1.0 = all data overlaps
        frac_inside = n_inside / max(n_total, 1)

        # Minimum distance from label CENTER to any data point
        min_dist = float(np.min(np.abs(all_occupied - cy)))

        # Score: heavily penalize overlap density, reward distance
        # frac_inside penalty dominates so the engine strongly avoids
        # regions where data curves pass through.
        effective_clearance = min_dist - frac_inside * 2.0

        # ── Exclusion zone penalty (legend, other labels, etc.) ──
        for band_lo, band_hi in _exclude:
            if label_bot < band_hi and label_top > band_lo:
                # Overlap with exclusion zone → heavy penalty
                overlap = min(label_top, band_hi) - max(label_bot, band_lo)
                effective_clearance -= overlap * 5.0  # strong repulsion

        if effective_clearance > best_clearance:
            best_clearance = effective_clearance
            best_y = cy

    # Final hard clamp
    return float(np.clip(best_y, y_lo, y_hi))


class LabelManager:
    """
    Tracks text labels on an axes and resolves vertical overlaps.
    Supports smart auto-placement that avoids data curves AND
    exclusion zones (legend bboxes, etc.).
    """
    def __init__(self, ax, min_gap_frac=0.06):
        self.ax = ax
        self.min_gap = min_gap_frac
        self._entries = []
        self._data_series = []    # [(ftt, y_data), ...] for clearance
        self._exclude_bands = []  # [(y_lo, y_hi), ...] exclusion zones

    def register_data(self, ftt, y_data):
        """Register a plotted data series for smart avoidance."""
        self._data_series.append((ftt, y_data))

    def register_exclusion(self, y_lo, y_hi):
        """Register a y-band exclusion zone (axes fraction).
        Use for legend boxes or other fixed UI elements."""
        self._exclude_bands.append((y_lo, y_hi))

    def add_label(self, x_frac, y_frac, text, *, auto_place=False, **kwargs):
        """
        Add a label.  If auto_place=True, y_frac is treated as a
        fallback only — the actual position is computed by
        find_clear_y() during resolve() to avoid data curves.
        """
        self._entries.append({'x': x_frac, 'y': y_frac,
                              'text': text, 'kwargs': kwargs,
                              'auto': auto_place})

    def resolve(self):
        if not self._entries:
            return []

        # Auto-place entries that requested it
        for entry in self._entries:
            if entry['auto'] and self._data_series:
                entry_va = entry['kwargs'].get('va', 'center')
                entry['y'] = find_clear_y(
                    self.ax, self._data_series,
                    x_frac_region=(0.50, 1.0),
                    label_height=0.10, margin=0.05,
                    va=entry_va,
                    exclude_y_bands=self._exclude_bands,
                )

        self._entries.sort(key=lambda e: e['y'], reverse=True)
        placed_y = []
        text_objs = []
        for entry in self._entries:
            target_y = entry['y']
            for py in placed_y:
                if abs(target_y - py) < self.min_gap:
                    target_y = py - self.min_gap
            placed_y.append(target_y)
            # HARD CLAMP: generous inset so bbox doesn't touch axes frame
            target_y = max(0.12, min(0.86, target_y))
            kw = dict(entry['kwargs'])
            kw['clip_on'] = True  # safety: clip any overflow at axes edge
            kw['zorder'] = 20    # labels always on topmost layer
            t = self.ax.text(entry['x'], target_y, entry['text'],
                             transform=self.ax.transAxes, **kw)
            text_objs.append(t)
        return text_objs

# ═══════════════════════════════════════════════════════════════
#  5. Convergence Analysis
# ═══════════════════════════════════════════════════════════════

def convergence_cv(ax, ftt, values, label_name,
                   window_ftt=None):
    """
    Turbulent convergence criterion: Coefficient of Variation (CV).
    CV = σ / |μ| over last *window_ftt* FTT.
    Draws mean line + ±1σ band.

    Thresholds are read from module-level globals (synced from variables.h):
      CV_CONVERGED  — CV < this → CONVERGED
      CV_NEAR       — CV < this → NEAR
      CV_WINDOW_FTT — default window width in FTT

    Returns (cv_percent, state_str) for use by the caller's summary box.
    Does NOT place any text label itself — the caller consolidates
    all CV info into a single summary box to avoid duplicates.
    """
    if window_ftt is None:
        window_ftt = CV_WINDOW_FTT  # from variables.h

    if len(ftt) < 2:
        return (0.0, 'N/A')
    ftt_end = ftt[-1]
    mask = (ftt >= ftt_end - window_ftt) & np.isfinite(values)
    last = values[mask]
    if len(last) < 10:
        return (0.0, 'N/A')

    mean_val = np.mean(last)
    std_val  = np.std(last)
    cv = std_val / abs(mean_val) * 100 if abs(mean_val) > 1e-30 else 0.0

    ax.axhline(mean_val, color='0.5', ls=':', alpha=0.4, lw=0.5)
    ftt_band = ftt[mask]
    ax.fill_between(ftt_band, mean_val - std_val, mean_val + std_val,
                    alpha=0.08, color='0.5')

    if cv < CV_CONVERGED:
        state = 'CONVERGED'
    elif cv < CV_NEAR:
        state = 'NEAR'
    else:
        state = 'NOT CONVERGED'

    return (cv, state)


def convergence_epsilon(label_mgr, ax, ftt, values, text_y=0.55,
                        cpp_error=None):
    """
    Laminar steady-state criterion: relative iterative residual δ (Paper Eq.37).

    When cpp_error is provided (10-col format), use the C++ Error column
    directly — this guarantees the Python display matches the C++ state
    EXACTLY, with no rounding or methodological discrepancy.

    Fallback (4/7-col legacy): compute δ from consecutive Ub/Uref differences.

    Thresholds are read from module-level globals (synced from variables.h):
      EPS_CONVERGED  — δ < this → CONVERGED
      EPS_NEAR       — δ < this → NEAR CONVERGED
    """
    if len(values) < 20:
        return

    n_tail = max(10, len(values) // 10)
    finite_tail = values[-n_tail:]
    finite_tail = finite_tail[np.isfinite(finite_tail)]
    if len(finite_tail) < 5:
        return

    # Use C++ Error column if available (exact match with C++ convergence)
    if cpp_error is not None:
        cpp_tail = cpp_error[-n_tail:]
        cpp_finite = cpp_tail[np.isfinite(cpp_tail)]
        if len(cpp_finite) > 0:
            eps = float(cpp_finite[-1])  # latest C++ per-step δ
        else:
            eps = 1.0  # fallback: assume not converged
    else:
        # Legacy fallback: recompute from Ub/Uref differences
        diffs = np.abs(np.diff(finite_tail))
        denominators = np.maximum(np.abs(finite_tail[1:]), 1e-30)
        eps = float((diffs / denominators).max())

    mean_val = float(np.mean(finite_tail))
    ax.axhline(mean_val, color='0.5', ls=':', alpha=0.4, lw=0.5)

    if eps < EPS_CONVERGED:
        txt = f'CONVERGED ($\\delta$={eps:.1e})'
    elif eps < EPS_NEAR:
        txt = f'NEAR ($\\delta$={eps:.1e})'
    else:
        txt = f'NOT CONVERGED ($\\delta$={eps:.1e})'

    label_mgr.add_label(
        0.98, text_y, txt, auto_place=True,
        ha='right', va='top', color='black', fontsize=7, fontweight='bold',
        bbox=dict(facecolor='white', alpha=0.90, edgecolor='0.3',
                  pad=1.5, boxstyle='round,pad=0.3',
                  linewidth=0.3),
    )

# ═══════════════════════════════════════════════════════════════
#  6. FTT Start Marker
# ═══════════════════════════════════════════════════════════════

def mark_ftt_start(ax, ftt_val, current_ftt):
    """
    Draw vertical reference line at FTT accumulation start.

    Accumulate label display rules (§3):
      - The label appears ONLY when current_ftt > ftt_val, meaning
        accumulation has truly begun.  Before that, only the
        reference vertical line is drawn (no text).
      - This ensures the label represents "accumulation IS active",
        not "accumulation WILL start eventually".

    Label positioning strategy (§4 — no axis overlap):
      - Uses blended transform (data-x, axes-y) so the label
        tracks the vertical line in data space regardless of zoom.
      - Horizontal: placed to the RIGHT of the line by default.
        Switches to LEFT if the line is in the right 35% of the
        plot, preventing the label from extending beyond the frame.
      - Vertical: placed at y = 0.82 (axes fraction) — high enough
        to be visible but safely below the top border (≤ 0.90).
      - A white bbox prevents visual merging with grid lines or
        data curves underneath.
      - The label never sits ON the y-axis (x_frac > 0) or ON
        the x-axis (y = 0.82 >> 0).
    """
    ax.axvline(ftt_val, color='0.3', ls='-', lw=0.6, alpha=0.7)

    if current_ftt <= ftt_val:
        return

    trans = mpl.transforms.blended_transform_factory(
        ax.transData, ax.transAxes
    )

    x_lo, x_hi = ax.get_xlim()
    total_range = x_hi - x_lo if x_hi > x_lo else 1.0
    x_frac = (ftt_val - x_lo) / total_range

    x_pad = total_range * 0.015
    if x_frac < 0.65:
        x_pos = ftt_val + x_pad
        ha = 'left'
    else:
        x_pos = ftt_val - x_pad
        ha = 'right'

    ax.text(x_pos, 0.82,
            f'FTT={ftt_val:.0f}\naccum. start',
            transform=trans, ha=ha, va='top',
            fontsize=6.5, color='0.2',
            bbox=dict(facecolor='white', alpha=0.8, edgecolor='0.3',
                      pad=1.5, boxstyle='round,pad=0.2',
                      linewidth=0.3),
            zorder=20)

# ═══════════════════════════════════════════════════════════════
#  7. Tight axis helpers
# ═══════════════════════════════════════════════════════════════

def tight_axis_range(ax, ftt, padding_frac=0.03):
    finite = ftt[np.isfinite(ftt)]
    if len(finite) < 2:
        return
    lo, hi = float(finite.min()), float(finite.max())
    pad = (hi - lo) * padding_frac
    ax.set_xlim(lo - pad, hi + pad)

def _ensure_ref_visible(ax, value):
    lo, hi = ax.get_ylim()
    margin = 0.10 * max(abs(value), abs(hi - lo), 1e-6)
    changed = False
    if value < lo:
        lo = value - margin; changed = True
    if value > hi:
        hi = value + margin; changed = True
    if changed:
        ax.set_ylim(lo, hi)

# ═══════════════════════════════════════════════════════════════
#  8. Colours
# ═══════════════════════════════════════════════════════════════

# ── Okabe-Ito colorblind-safe palette (Wong 2011) ──
# Designed to be distinguishable for all common forms of
# color vision deficiency; also readable in B&W greyscale.
COLOR_UB  = '#0072B2'   # blue     — Ub/Uref
COLOR_MA  = '#D55E00'   # vermilion — Ma_max
COLOR_FC  = '#009E73'   # bluish green — F*
COLOR_RHO = '#CC79A7'   # reddish purple — <ρ>
COLOR_UU  = '#009E73'   # bluish green — RS
COLOR_K   = '#E69F00'   # orange — TKE
COLOR_MLUPS = '#56B4E9'  # sky blue — MLUPS
COLOR_MPI   = '#D55E00'  # vermilion — MPI

# ═══════════════════════════════════════════════════════════════
#  8b. Data Loading — timing_log.dat  (GPU performance)
# ═══════════════════════════════════════════════════════════════

def load_timing_log(filepath):
    """
    Load timing_log.dat for performance monitoring.

    Expected columns (15-col with TIMING_DETAIL, whitespace-separated):
      col 0: Step        col 1: FTT         col 2: GPU_min    col 3: Wall_min
      col 4: MLUPS       col 5: MLUPS/GPU   col 6: MLUPS_avg  col 7: MLUPSa/GPU
      col 8: GPU_int_s   col 9: S1_ms       col 10: S2_ms     col 11: MPI_ms
      col 12: MPI_wt     col 13: Iter_ms    col 14: MC_ms

    Returns dict or None if file not found.
    """
    if not os.path.isfile(filepath):
        print(f"[WARN] timing_log.dat not found at {filepath} — skipping perf panel")
        return None

    rows = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            vals = line.split()
            try:
                rows.append([float(v) for v in vals])
            except ValueError:
                continue

    if not rows:
        print(f"[WARN] No valid data in timing_log.dat")
        return None

    data = np.array(rows)
    if data.shape[1] < 9:
        print(f"[ERROR] timing_log.dat needs ≥9 columns, got {data.shape[1]}")
        return None

    result = {
        'FTT':       data[:, 1],
        'GPU_min':   data[:, 2],
        'MLUPS_rec': data[:, 4],
        'MPI_ms':    data[:, 11] if data.shape[1] > 11 else np.zeros(len(data)),
    }

    # Skip the very first row (step=1) — cold-start outlier
    if len(result['FTT']) > 1 and result['MLUPS_rec'][0] < 5:
        for k in result:
            result[k] = result[k][1:]

    print(f"[INFO] timing_log.dat: {len(result['FTT'])} rows, "
          f"MLUPS ∈ [{result['MLUPS_rec'].min():.1f}, {result['MLUPS_rec'].max():.1f}], "
          f"MPI ∈ [{result['MPI_ms'].min():.1f}, {result['MPI_ms'].max():.1f}] ms")
    return result


# ═══════════════════════════════════════════════════════════════
#  9. Panel Builders
# ═══════════════════════════════════════════════════════════════

def _panel_label(ax, label_text):
    """Place journal-standard panel label (a), (b), etc. in upper-left."""
    ax.text(-0.04, 1.06, label_text, transform=ax.transAxes,
            fontsize=10, fontweight='bold', va='bottom', ha='right')

def _best_legend_loc(ax, data_series_list,
                     legend_w_frac=0.35, legend_h_frac=0.22,
                     extra_frac_series=None):
    """
    Choose the best legend location by evaluating data density
    in each of the four corners.

    Parameters
    ----------
    ax : matplotlib Axes
    data_series_list : list of (x_array, y_array) in DATA coordinates
        Converted to axes fraction via ax's xlim/ylim.
    legend_w_frac : approximate legend width as axes fraction
    legend_h_frac : approximate legend height as axes fraction
    extra_frac_series : list of (x_frac_array, y_frac_array), optional
        Pre-converted axes-fraction data (e.g., twin-axis data that
        cannot be converted through ax's y-limits).

    Returns
    -------
    str : one of 'upper right', 'upper left', 'lower right', 'lower left'
    """
    if not data_series_list and not extra_frac_series:
        return 'upper right'

    xlo, xhi = ax.get_xlim()
    ylo, yhi = ax.get_ylim()
    x_range = xhi - xlo if abs(xhi - xlo) > 1e-30 else 1.0
    y_range = yhi - ylo if abs(yhi - ylo) > 1e-30 else 1.0

    # Collect all data points as (x_frac, y_frac)
    all_xf, all_yf = [], []
    for x_arr, y_arr in (data_series_list or []):
        x_arr = np.asarray(x_arr, dtype=float)
        y_arr = np.asarray(y_arr, dtype=float)
        mask = np.isfinite(x_arr) & np.isfinite(y_arr)
        if np.any(mask):
            all_xf.append((x_arr[mask] - xlo) / x_range)
            all_yf.append((y_arr[mask] - ylo) / y_range)

    # Merge pre-converted twin-axis fraction data
    for xf_arr, yf_arr in (extra_frac_series or []):
        xf_arr = np.asarray(xf_arr, dtype=float)
        yf_arr = np.asarray(yf_arr, dtype=float)
        mask = np.isfinite(xf_arr) & np.isfinite(yf_arr)
        if np.any(mask):
            all_xf.append(xf_arr[mask])
            all_yf.append(yf_arr[mask])

    if not all_xf:
        return 'upper right'

    xf = np.concatenate(all_xf)
    yf = np.concatenate(all_yf)

    pad = 0.02  # small inset from axes edge
    # Each entry: (x_lo, x_hi, y_lo, y_hi) in axes-fraction [0, 1]
    candidates = {
        'upper right': (1.0 - legend_w_frac - pad, 1.0 - pad,
                        1.0 - legend_h_frac - pad, 1.0 - pad),
        'upper left':  (pad, pad + legend_w_frac,
                        1.0 - legend_h_frac - pad, 1.0 - pad),
        'lower right': (1.0 - legend_w_frac - pad, 1.0 - pad,
                        pad, pad + legend_h_frac),
        'lower left':  (pad, pad + legend_w_frac,
                        pad, pad + legend_h_frac),
    }

    best_loc = 'upper right'
    best_score = -1e30  # higher = better (fewer points inside)

    for loc_name, (xl, xh, yl, yh) in candidates.items():
        # Normalise bounds
        xl, xh = min(xl, xh), max(xl, xh)
        yl, yh = min(yl, yh), max(yl, yh)
        # Count data points inside this rectangle
        inside = np.sum((xf >= xl) & (xf <= xh) & (yf >= yl) & (yf <= yh))
        # Also compute minimum distance from box centre to nearest data point
        cx, cy = (xl + xh) / 2, (yl + yh) / 2
        dists = np.sqrt((xf - cx)**2 + (yf - cy)**2)
        min_dist = float(np.min(dists)) if len(dists) > 0 else 1.0
        # Score: fewer points inside is better; break ties by distance
        score = -float(inside) + min_dist * 0.1
        if score > best_score:
            best_score = score
            best_loc = loc_name

    return best_loc


def _journal_legend(ax, loc='upper right', **kwargs):
    """
    Create legend with visible sample-line + framed box so legend text
    stands out from data curves. Uses subtle white box with thin border.

    Returns (legend_obj, (y_lo, y_hi)) where y_lo/y_hi are axes fractions
    of the legend bounding box — useful for exclusion zone registration.
    """
    leg = ax.legend(
        loc=loc,
        frameon=True,
        framealpha=0.92,
        edgecolor='0.7',
        fancybox=False,
        borderpad=0.4,
        handlelength=2.0,   # longer sample line segments
        handletextpad=0.5,
        labelspacing=0.3,
        **kwargs,
    )
    leg.get_frame().set_linewidth(0.3)
    leg.set_zorder(20)  # legend always on topmost layer

    # Estimate legend bbox in axes fraction for exclusion zones.
    # Renderer needed for exact bbox; use approximate fallback.
    try:
        fig = ax.get_figure()
        renderer = fig.canvas.get_renderer()
        bb = leg.get_window_extent(renderer)
        bb_ax = bb.transformed(ax.transAxes.inverted())
        y_lo_leg = float(bb_ax.y0)
        y_hi_leg = float(bb_ax.y1)
    except Exception:
        # Fallback: upper-right legend occupies roughly top 25%
        y_lo_leg = 0.72
        y_hi_leg = 1.0

    return leg, (y_lo_leg, y_hi_leg)

def build_ma_panel(ax, data):
    """Top panel: Ma_max — auto-scaled with 0.3 compressibility threshold."""
    ftt = data['FTT']
    ma  = data['Ma_max']

    ax.plot(ftt, ma, color=COLOR_MA, lw=0.8, label=r'$Ma_{\max}$')
    ax.axhline(0.3, color='0.5', ls='--', lw=0.5, alpha=0.6,
               label=r'$Ma = 0.3$')
    ax.set_ylabel(r"$Ma_{\max}$")
    ax.grid(False)

    # Auto y-axis
    finite = ma[np.isfinite(ma)]
    if len(finite) > 0:
        ma_min = float(finite.min())
        ma_max = float(finite.max())
        y_top = max(ma_max, 0.3)
        pad = max((y_top - ma_min) * 0.05, 0.005)
        ax.set_ylim(max(0.0, ma_min - pad), y_top + pad)

    _journal_legend(ax)  # returns (leg, band) but Ma panel has no labels to avoid
    _panel_label(ax, '(a)')


def build_ub_fstar_panel(ax, data, Re, ftt_stats_start):
    """
    Middle panel: Ub/Uref (blue, LEFT y-axis) + F* (green, RIGHT y-axis).

    Separate axes so each quantity has its own scale, but they share
    the same FTT x-axis and visual space — overlap is acceptable.

    Convergence criterion (§6 — regime-dependent):
      Laminar  (Re ≤ 150):  iterative residual ε on U* — measures if
                             the solution has stopped changing step-to-
                             step. This IS the laminar convergence check.
      Turbulent (Re > 150):  NO convergence label here. Turbulent
                             convergence is assessed via CV on RS/TKE
                             in the bottom panel (after accumulation).
    """
    ftt   = data['FTT']
    ub    = data['Ub_Uref']
    fstar = data['F_star']
    current_ftt = float(ftt[np.isfinite(ftt)][-1]) if np.any(np.isfinite(ftt)) else 0.0

    label_mgr = LabelManager(ax, min_gap_frac=0.08)

    # ── U* on LEFT y-axis (blue) ──
    ln1 = ax.plot(ftt, ub, color=COLOR_UB, lw=0.8,
                  label=r'$U_b / U_{ref}$')
    ax.axhline(1.0, color='0.5', ls='--', lw=0.5, alpha=0.5,
               label=r'$U_b/U_{ref}=1$')
    ax.set_ylabel(r"$U_b \,/\, U_{ref}$", color=COLOR_UB)
    ax.tick_params(axis='y', labelcolor=COLOR_UB)
    ax.grid(False)

    # Auto y-axis for Ub: tight data min-max, ensure 1.0 target line visible
    ub_finite = ub[np.isfinite(ub)]
    if len(ub_finite) > 0:
        ub_lo, ub_hi = float(ub_finite.min()), float(ub_finite.max())
        ub_top = max(ub_hi, 1.0)   # ensure target line visible
        ub_bot = min(ub_lo, 1.0)
        pad_ub = max((ub_top - ub_bot) * 0.05, 0.01)
        ax.set_ylim(ub_bot - pad_ub, ub_top + pad_ub)

    # Register Ub data for smart label avoidance
    label_mgr.register_data(ftt, ub)

    # ── F* on RIGHT y-axis (green) ──
    ax_fc = ax.twinx()
    # Ensure F* twin axes is BEHIND primary axes so green line
    # doesn't cover legend / labels on the primary axes
    ax_fc.set_zorder(ax.get_zorder() - 1)
    ax.patch.set_visible(False)  # make primary axes background transparent
    ln2 = ax_fc.plot(ftt, fstar, color=COLOR_FC, lw=0.6,
                     label=r'$F^{\!*}$')
    ax_fc.set_ylabel(r"$F^{\!*}$", color=COLOR_FC)
    ax_fc.tick_params(axis='y', labelcolor=COLOR_FC)

    # Auto y-axis for F*: tight data min-max
    fs_finite = fstar[np.isfinite(fstar)]
    if len(fs_finite) > 0:
        fs_lo, fs_hi = float(fs_finite.min()), float(fs_finite.max())
        pad_fs = max((fs_hi - fs_lo) * 0.05, abs(fs_hi) * 0.01 + 1e-6)
        ax_fc.set_ylim(fs_lo - pad_fs, fs_hi + pad_fs)

    # Register Ub data for smart label avoidance (primary axis)
    label_mgr.register_data(ftt, ub)

    # §3: accumulate label — only shown when FTT > ftt_stats_start
    # §4: positioned via blended transform to avoid axis/border overlap
    mark_ftt_start(ax, ftt_stats_start, current_ftt)

    # Place legend FIRST to get its bbox for exclusion zone
    lns = ln1 + ln2
    leg, leg_band = _journal_legend(ax, handles=lns, labels=[l.get_label() for l in lns])
    label_mgr.register_exclusion(*leg_band)

    # §6: laminar convergence uses ε (iterative residual);
    #      turbulent convergence is NOT assessed here — it uses CV
    #      on RS/TKE in the dedicated bottom panel.
    if is_laminar(Re):
        convergence_epsilon(label_mgr, ax, ftt, ub, text_y=0.55,
                            cpp_error=data.get('cpp_error'))

    label_mgr.resolve()
    _panel_label(ax, '(b)')


def build_rho_panel(ax, rho_data):
    """
    Density panel: mass conservation measured against ρ₀ = 1.0.

    Design philosophy (revised):
      The LBM theoretical reference density is ρ₀ = 1.0 (lattice unit).
      Mass conservation quality = |<ρ> − 1.0|.
      Perfect conservation ⟹ |<ρ> − 1.0| = 0.

    Strategy:
      1. Plot raw <ρ>(t) in purple.
      2. Draw ρ₀ = 1.0 dashed reference line (the STANDARD, not initial ρ).
      3. y-axis: data min-max, but force upper bound ≥ 1.000000 so the
         1.0 standard line is always visible for comparison.
      4. High-precision y-tick format (%.7f) to reveal micro-deviations.
      5. Convergence metric = |<ρ>_mean − 1.0| (NOT temporal change).
         Zero ⟹ exact conservation.
    """
    ftt = rho_data['FTT']
    rho = rho_data['rho_avg']

    ax.plot(ftt, rho, color=COLOR_RHO, lw=0.8,
            label=r'$\langle \rho \rangle$')

    # ── Reference line: theoretical standard ρ₀ = 1.0 ──
    ax.axhline(1.0, color='0.4', ls='--', lw=0.5, alpha=0.6,
               label=r'$\rho_0 = 1$')

    ax.set_ylabel(r"$\langle \rho \rangle$", color=COLOR_RHO)
    ax.tick_params(axis='y', labelcolor=COLOR_RHO)
    ax.grid(False)

    # ── Auto y-axis: data min-max, force upper ≥ 1.0 ──
    finite = rho[np.isfinite(rho)]
    if len(finite) > 0:
        rho_min = float(finite.min())
        rho_max = float(finite.max())
        rho_mean = float(finite.mean())
        data_range = rho_max - rho_min

        # Ensure 1.0 standard line is always visible
        y_top = max(rho_max, 1.0)
        y_bot = min(rho_min, 1.0)

        if data_range < 1e-12:
            # Perfectly flat: show symmetric window to reveal scale
            half_win = max(abs(rho_mean - 1.0) * 2, 1e-6)
            y_bot = min(rho_mean, 1.0) - half_win * 0.2
            y_top = max(rho_mean, 1.0) + half_win * 0.2
        else:
            pad = max((y_top - y_bot) * 0.10, 1e-8)
            y_bot -= pad
            y_top += pad

        ax.set_ylim(y_bot, y_top)

    # High-precision y-ticks to show micro-deviations from 1.0
    ax.ticklabel_format(axis='y', useOffset=False, style='plain')
    ax.yaxis.set_major_formatter(
        mpl.ticker.FormatStrFormatter('%.7f')
    )

    # Place legend FIRST to get bbox for exclusion zone
    leg, leg_band = _journal_legend(ax)

    # ── Mass conservation metric: |<ρ>_mean − 1.0| ──
    if len(finite) > 1:
        rho_mean = float(finite.mean())
        dev_from_1 = abs(rho_mean - 1.0)
        dev_max    = float(np.max(np.abs(finite - 1.0)))

        # Journal-quality: compact, precise notation — black text
        if dev_from_1 < 1e-10:
            status = r"$|\langle\rho\rangle - 1|$" + f" = {dev_from_1:.1e}"
        elif dev_from_1 < 1e-6:
            status = (r"$|\langle\rho\rangle - 1|$" + f" = {dev_from_1:.1e}"
                      + f"\n" + r"$\max|\Delta\rho|$" + f" = {dev_max:.1e}")
        elif dev_from_1 < 1e-3:
            status = (r"$|\langle\rho\rangle - 1|$" + f" = {dev_from_1:.1e}"
                      + f"\n" + r"$\max|\Delta\rho|$" + f" = {dev_max:.1e}")
        else:
            status = (r"$|\langle\rho\rangle - 1|$" + f" = {dev_from_1:.1e}"
                      + f"\n" + r"$\max|\Delta\rho|$" + f" = {dev_max:.1e}")

        # Smart placement: detect rho curve + reference line + legend zone
        rho_series = [(ftt, rho)]
        ones_line = np.ones_like(ftt)
        rho_series.append((ftt, ones_line))

        clear_y = find_clear_y(ax, rho_series,
                               x_frac_region=(0.0, 0.50),
                               label_height=0.20, margin=0.08,
                               n_candidates=40, va='top',
                               exclude_y_bands=[leg_band])

        ax.text(0.02, clear_y, status,
                transform=ax.transAxes, ha='left', va='top',
                color='black', fontsize=7,
                bbox=dict(facecolor='white', alpha=0.90,
                          edgecolor='0.3', pad=1.5,
                          boxstyle='round,pad=0.3',
                          linewidth=0.3),
                clip_on=True, zorder=20)

    _panel_label(ax, '(c)')


def build_perf_panel(ax, timing_data, panel_label='(d)'):
    """
    Performance panel: MLUPS_rec (left y-axis) + MPI_ms (right y-axis).

    Dual-axis design with contrasting colors:
      Left  (sky blue):  instantaneous throughput (MLUPS)
      Right (vermilion):  MPI halo exchange latency (ms)

    Stats box is placed at the top-left, with y-axis expanded upward
    so the box sits above the data curves (no occlusion).
    """
    ftt   = timing_data['FTT']
    mlups = timing_data['MLUPS_rec']
    mpi   = timing_data['MPI_ms']

    # ── Left axis: MLUPS ──
    ln1 = ax.plot(ftt, mlups, color=COLOR_MLUPS, lw=0.8,
                  label=r'$\mathrm{MLUPS}_{\mathrm{rec}}$')
    ax.set_ylabel(r'MLUPS', color=COLOR_MLUPS)
    ax.tick_params(axis='y', labelcolor=COLOR_MLUPS)
    ax.grid(False)

    # Auto y-range: expand TOP by ~35% to leave room for stats box
    finite_m = mlups[np.isfinite(mlups)]
    if len(finite_m) > 1:
        m_mean = float(finite_m.mean())
        m_min  = float(finite_m.min())
        m_max  = float(finite_m.max())
        data_span = m_max - m_min
        pad_bot = max(data_span * 0.10, 0.3)
        pad_top = max(data_span * 0.65, 1.2)   # extra headroom for 3-line stats box
        ax.set_ylim(max(0, m_min - pad_bot), m_max + pad_top)

    # MLUPS mean reference line
    if len(finite_m) > 1:
        ax.axhline(m_mean, color=COLOR_MLUPS, ls=':', lw=0.5, alpha=0.5)

    # ── Right axis: MPI_ms ──
    ax_mpi = ax.twinx()
    ln2 = ax_mpi.plot(ftt, mpi, color=COLOR_MPI, lw=0.6, alpha=0.8,
                      label=r'$\mathrm{MPI}_{\mathrm{ms}}$')
    ax_mpi.set_ylabel(r'MPI (ms)', color=COLOR_MPI)
    ax_mpi.tick_params(axis='y', labelcolor=COLOR_MPI)

    finite_p = mpi[np.isfinite(mpi)]
    if len(finite_p) > 1:
        p_min = float(finite_p.min())
        p_max = float(finite_p.max())
        p_span = p_max - p_min
        pad_p_bot = max(p_span * 0.10, 0.1)
        pad_p_top = max(p_span * 0.65, 0.6)    # match MLUPS headroom
        ax_mpi.set_ylim(max(0, p_min - pad_p_bot), p_max + pad_p_top)

    # ── Legend (top-right, consistent with other panels) ──
    lns = ln1 + ln2
    leg, leg_band = _journal_legend(ax, handles=lns,
                                    labels=[l.get_label() for l in lns])

    # ── Summary stats text (top-left, above data) ──
    if len(finite_m) > 1 and len(finite_p) > 1:
        # Total simulation GPU time (cumulative across ALL sessions)
        gpu_min_total = float(timing_data['GPU_min'][-1])
        if gpu_min_total >= 60:
            gpu_str = f"GPU total = {gpu_min_total/60:.1f} hr"
        else:
            gpu_str = f"GPU total = {gpu_min_total:.1f} min"
        stats_txt = (f"MLUPS avg = {m_mean:.1f}\n"
                     f"MPI avg = {float(finite_p.mean()):.1f} ms\n"
                     f"{gpu_str}")
        ax.text(0.02, 0.97, stats_txt,
                transform=ax.transAxes, ha='left', va='top',
                color='black', fontsize=5.5,
                bbox=dict(facecolor='white', alpha=0.90,
                          edgecolor='0.3', pad=1.2,
                          boxstyle='round,pad=0.25',
                          linewidth=0.3),
                clip_on=True, zorder=20)

    _panel_label(ax, panel_label)


def build_rs_tke_panel(ax, data, ftt_stats_start, panel_label='(d)'):
    """
    Turbulent-only panel: RS + TKE convergence statistics.

    This panel is ONLY created when ALL conditions hold (§2):
      1. Flow is turbulent (Re > 150)
      2. Data file has RS/TKE columns (7-col format)
      3. Current FTT > FTT_STATS_START (accumulation has started)
      4. Valid RS data exists in the accumulated range

    If ANY condition fails → no panel, no placeholder, no empty chart.

    Data range (§5):
      Only data with FTT ≥ ftt_stats_start is plotted. The x-axis is
      restricted to the accumulated interval [FTT_STATS_START, end],
      reflecting "statistics from accumulation onset" — NOT from t=0.

    Convergence criterion (§6 — turbulent):
      CV (coefficient of variation) on RS and TKE over the last 10 FTT.
      This measures statistical stationarity of turbulence quantities —
      the appropriate criterion for turbulent flow.
      Laminar ε is NOT used here.
    """
    ftt = data['FTT']
    uu  = data['uu_RS']
    k   = data['k_TKE']

    label_mgr = LabelManager(ax, min_gap_frac=0.08)

    # §5: strictly post-accumulation data only
    mask_valid = np.isfinite(uu) & np.isfinite(k)
    mask_stats = mask_valid & (ftt >= ftt_stats_start) & (uu > 0)

    if not np.any(mask_stats):
        return

    ftt_acc = ftt[mask_stats]
    uu_acc  = uu[mask_stats]
    k_acc   = k[mask_stats]

    # ── RS on LEFT y-axis ──
    ln1 = ax.plot(ftt_acc, uu_acc, color=COLOR_UU, lw=0.8,
                  label=r"$\langle u'u' \rangle / U_{ref}^2$")
    ax.set_ylabel(r"$\langle u'u' \rangle / U_{ref}^2$", color=COLOR_UU)
    ax.tick_params(axis='y', labelcolor=COLOR_UU)
    ax.grid(False)

    # Auto y-axis for RS: generous padding so data only occupies ~60% of
    # the vertical axes space — leaving clear room for labels/legend.
    # Old value was 5%, far too tight: curves filled the entire [0,1]
    # fraction range and find_clear_y had nowhere to place the CV box.
    uu_lo, uu_hi = float(uu_acc.min()), float(uu_acc.max())
    uu_span = max(uu_hi - uu_lo, abs(uu_hi) * 0.02 + 1e-8)
    pad_uu_bot = uu_span * 0.35   # 35% below data — room for CV box
    pad_uu_top = uu_span * 0.50   # 50% above data — room for legend
    ax.set_ylim(uu_lo - pad_uu_bot, uu_hi + pad_uu_top)

    # Register RS data for smart label avoidance
    label_mgr.register_data(ftt_acc, uu_acc)

    # ── TKE on RIGHT y-axis ──
    ax_k = ax.twinx()
    # Ensure k twin axes is BEHIND primary axes so orange line
    # doesn't cover legend / labels on the primary axes
    ax_k.set_zorder(ax.get_zorder() - 1)
    ax.patch.set_visible(False)  # make primary axes background transparent
    ln2 = ax_k.plot(ftt_acc, k_acc, color=COLOR_K, lw=0.6,
                    label=r"$k / U_{ref}^2$")
    ax_k.set_ylabel(r"$k / U_{ref}^2$", color=COLOR_K)
    ax_k.tick_params(axis='y', labelcolor=COLOR_K)

    # Auto y-axis for TKE: same generous padding as RS axis
    k_lo, k_hi = float(k_acc.min()), float(k_acc.max())
    k_span = max(k_hi - k_lo, abs(k_hi) * 0.02 + 1e-8)
    pad_k_bot = k_span * 0.35
    pad_k_top = k_span * 0.50
    ax_k.set_ylim(k_lo - pad_k_bot, k_hi + pad_k_top)

    # Note: k_acc lives on ax_k's y-scale (different from ax's y-scale).
    # We do NOT register k_acc with label_mgr (which uses ax's y-limits)
    # because _data_to_axes_frac(ax, k_acc) would produce wrong fractions.
    # Instead, we pre-convert k fractions via ax_k and pass them as
    # extra_occupied_y_fracs to find_clear_y later.

    # Place legend: smart location that avoids BOTH data curves.
    # uu is on primary axis → pass as data_series_list.
    # k is on twin axis → pre-convert to fractions and pass separately.
    lns = ln1 + ln2
    xlo_ax, xhi_ax = ax.get_xlim()
    x_range_leg = xhi_ax - xlo_ax if abs(xhi_ax - xlo_ax) > 1e-30 else 1.0
    k_xf = (ftt_acc - xlo_ax) / x_range_leg
    k_yf = _data_to_axes_frac(ax_k, k_acc)
    smart_loc = _best_legend_loc(ax,
                                 data_series_list=[(ftt_acc, uu_acc)],
                                 legend_w_frac=0.35, legend_h_frac=0.22,
                                 extra_frac_series=[(k_xf, k_yf)])
    leg, leg_band = _journal_legend(ax, loc=smart_loc,
                                    handles=lns,
                                    labels=[l.get_label() for l in lns])
    label_mgr.register_exclusion(*leg_band)

    # §6: turbulent convergence via CV on RS and TKE
    # convergence_cv now only draws σ band + returns (cv, state)
    cv_uu, state_uu = convergence_cv(ax, ftt_acc, uu_acc, 'uu_RS')
    cv_k, state_k   = convergence_cv(ax_k, ftt_acc, k_acc, 'k')

    # ── Single CV summary box — unified black, serif font ──
    # Style: matches density panel's conservation label (black, serif, fontsize 7)
    if state_uu == 'CONVERGED' and state_k == 'CONVERGED':
        overall = 'converged'
    elif state_uu in ('CONVERGED', 'NEAR') and state_k in ('CONVERGED', 'NEAR'):
        overall = 'near converged'
    else:
        overall = 'converging'

    cv_text = (f"k: CV = {cv_k:.2f}%  ({state_k})\n"
               f"\u27E8u\u2032u\u2032\u27E9: CV = {cv_uu:.2f}%  ({state_uu})\n"
               f"Overall: {overall}")

    # ── 2D smart placement: scan entire panel for the largest empty region ──
    # Pre-convert k data to axes-fraction (x, y) pairs for twin-axis
    xlo_cv, xhi_cv = ax.get_xlim()
    x_range_cv = xhi_cv - xlo_cv if abs(xhi_cv - xlo_cv) > 1e-30 else 1.0
    k_xf = (ftt_acc - xlo_cv) / x_range_cv
    k_yf = _data_to_axes_frac(ax_k, k_acc)

    cv_x, cv_y, cv_ha, cv_va = find_clear_2d(
        ax, label_mgr._data_series,
        label_w=0.32, label_h=0.18,
        margin=0.06, nx=25, ny=25,
        exclude_y_bands=[leg_band],
        extra_occupied_xy_fracs=[(k_xf, k_yf)])

    ax.text(cv_x, cv_y, cv_text,
            transform=ax.transAxes,
            fontsize=6, family='serif', color='black',
            ha=cv_ha, va=cv_va,
            bbox=dict(facecolor='white', alpha=0.92, edgecolor='0.5',
                      boxstyle='round,pad=0.3', linewidth=0.3),
            clip_on=True, zorder=20)

    label_mgr.resolve()
    _panel_label(ax, panel_label)

# ═══════════════════════════════════════════════════════════════
#  10. Title & Saving
# ═══════════════════════════════════════════════════════════════

def format_title_with_reynolds_number(Re):
    return f"Periodic Hill Flow Monitor — Re = {Re}"

def save_figure(fig, Re, script_dir):
    base = f"monitor_convergence_Re{Re}"
    # Journal priority: vector PDF first (300 DPI embedded), then high-res PNG
    for ext, dpi in [('.pdf', 300), ('.png', 600)]:
        out_path = os.path.join(script_dir, base + ext)
        try:
            fig.savefig(out_path, dpi=dpi, bbox_inches="tight",
                        pad_inches=0.02)
        except (OSError, FileNotFoundError) as e:
            fallback = os.path.join(os.getcwd(), base + ext)
            try:
                fig.savefig(fallback, dpi=dpi, bbox_inches="tight",
                            pad_inches=0.02)
                print(f"[INFO] Saved to fallback: {fallback}")
            except Exception:
                print(f"[WARN] Could not save {ext}: {e}")
    regime = "laminar" if is_laminar(Re) else "turbulent"
    print(f"[OK] Saved ({regime}): {base}.pdf / .png")

# ═══════════════════════════════════════════════════════════════
#  11. Validation
# ═══════════════════════════════════════════════════════════════

def validate_data(monitor_data, rho_data):
    """Run basic sanity checks before plotting."""
    errors = []

    ftt_m = monitor_data['FTT']
    if len(ftt_m) < 2:
        errors.append("Ustar_Force_record.dat has < 2 data points")

    if np.any(~np.isfinite(ftt_m)):
        n_bad = int(np.sum(~np.isfinite(ftt_m)))
        errors.append(f"Ustar_Force_record.dat has {n_bad} non-finite FTT values")

    if rho_data is not None:
        ftt_r = rho_data['FTT']
        rho   = rho_data['rho_avg']

        if len(ftt_r) < 2:
            errors.append("checkrho.dat has < 2 data points")

        if len(ftt_r) != len(rho):
            errors.append(f"checkrho.dat FTT/rho length mismatch: "
                          f"{len(ftt_r)} vs {len(rho)}")

        # Check FTT range overlap
        ftt_m_range = (float(ftt_m[0]), float(ftt_m[-1]))
        ftt_r_range = (float(ftt_r[0]), float(ftt_r[-1]))
        overlap = min(ftt_m_range[1], ftt_r_range[1]) - max(ftt_m_range[0], ftt_r_range[0])
        if overlap < 0:
            errors.append(f"FTT ranges do not overlap: "
                          f"monitor [{ftt_m_range[0]:.2f}, {ftt_m_range[1]:.2f}] vs "
                          f"checkrho [{ftt_r_range[0]:.2f}, {ftt_r_range[1]:.2f}]")
        else:
            print(f"[VALID] FTT overlap: {overlap:.2f} "
                  f"(monitor: {ftt_m_range[0]:.2f}–{ftt_m_range[1]:.2f}, "
                  f"checkrho: {ftt_r_range[0]:.2f}–{ftt_r_range[1]:.2f})")

    if errors:
        for e in errors:
            print(f"[ERROR] {e}")
        sys.exit(1)
    else:
        print("[VALID] All data checks passed")

# ═══════════════════════════════════════════════════════════════
#  12. Main
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Monitor convergence plot")
    parser.add_argument('--Re', type=int, default=None,
                        help='Reynolds number (default: auto from variables.h)')
    args, _ = parser.parse_known_args()

    vh   = parse_variables_h()
    _apply_parsed_convergence_params(vh)   # sync all thresholds from variables.h
    Re   = args.Re if args.Re is not None else (int(vh['Re']) if vh['Re'] else 700)
    Uref = vh['Uref'] if vh['Uref'] else 0.0583
    ftt_stats_start = vh['FTT_STATS_START']

    # ── §1: Flow regime classification ──
    regime_str = "LAMINAR" if is_laminar(Re) else "TURBULENT"
    print(f"[INFO] Re = {Re} → {regime_str} mode (threshold: {LAMINAR_RE_THRESHOLD})")
    print(f"[INFO] Uref = {Uref},  FTT_STATS_START = {ftt_stats_start}")
    print(f"[INFO] Convergence params from variables.h:")
    print(f"       Laminar:   EPS_CONVERGED={EPS_CONVERGED:.0e}, "
          f"EPS_NEAR={EPS_NEAR:.0e}")
    print(f"       Turbulent: CV_CONVERGED={CV_CONVERGED}%, "
          f"CV_NEAR={CV_NEAR}%, CV_WINDOW={CV_WINDOW_FTT} FTT")

    # ── Style — Journal Publication Quality ──
    # Target: JFM / JCP / PoF double-column (174 mm = 6.85 in)
    # Font sizes calibrated for 6.85 in width at 300+ DPI
    try:
        import scienceplots  # noqa: F401
        plt.style.use(['science', 'no-latex'])  # clean baseline
    except ImportError:
        pass
    mpl.rcParams.update({
        "font.family":       "serif",
        "font.serif":        ["STIXGeneral", "Times New Roman",
                              "DejaVu Serif"],
        "mathtext.fontset":  "stix",
        "font.size":         8,           # base font
        "axes.labelsize":    9,           # axis labels
        "axes.titlesize":    9,           # panel (a)(b)(c) labels
        "legend.fontsize":   7,           # compact legends
        "xtick.labelsize":   7.5,
        "ytick.labelsize":   7.5,
        "xtick.direction":   "in",
        "ytick.direction":   "in",
        "xtick.top":         True,        # ticks on all sides
        "ytick.right":       True,
        "xtick.major.size":  4,
        "ytick.major.size":  4,
        "xtick.minor.size":  2,
        "ytick.minor.size":  2,
        "xtick.major.width": 0.5,
        "ytick.major.width": 0.5,
        "xtick.minor.width": 0.4,
        "ytick.minor.width": 0.4,
        "xtick.minor.visible": True,
        "ytick.minor.visible": True,
        "axes.linewidth":    0.6,         # thinner spines
        "lines.linewidth":   0.8,         # thinner data lines
        "grid.linewidth":    0.3,
        "grid.alpha":        0.25,
        "legend.frameon":    True,         # legend has white background box
        "legend.handlelength": 1.5,
        "savefig.dpi":       600,          # print quality
        "savefig.transparent": False,
        "figure.dpi":        150,
    })

    # ── Load monitor data ──
    monitor_path = os.path.join(SCRIPT_DIR, "..", "Ustar_Force_record.dat")
    data = load_monitor_data(monitor_path)
    if data.get('has_error'):
        fmt = "10-col (RS/TKE + Error/Conv)"
    elif data['has_rs']:
        fmt = "7-col (with RS/TKE)"
    else:
        fmt = "4-col (legacy)"
    print(f"[INFO] Loaded {len(data['FTT'])} rows, {fmt}")

    # ── Load density data ──
    rho_path = os.path.join(SCRIPT_DIR, "..", "checkrho.dat")
    rho_data = load_checkrho(rho_path)

    # ── Load timing data ──
    timing_path = os.path.join(SCRIPT_DIR, "..", "timing_log.dat")
    # Also check result/ subfolder
    if not os.path.isfile(timing_path):
        timing_path = os.path.join(SCRIPT_DIR, "timing_log.dat")
    timing_data = load_timing_log(timing_path)

    # ── Validate ──
    validate_data(data, rho_data)

    # ── §2: Determine current simulation time & accumulation status ──
    ftt_all = data['FTT']
    current_ftt = (float(ftt_all[np.isfinite(ftt_all)][-1])
                   if np.any(np.isfinite(ftt_all)) else 0.0)
    has_accumulated = current_ftt > ftt_stats_start

    # ── §2/§6: RS/TKE panel visibility ──
    # The RS/TKE panel is ONLY shown when ALL of these hold:
    #   (a) Flow is turbulent (Re > 150)           — §1
    #   (b) Data file has 7 columns (RS/TKE cols)  — format check
    #   (c) FTT > FTT_STATS_START                  — §2 accumulation started
    #   (d) Valid RS data exists in accumulated range
    # If ANY condition fails: no panel, no placeholder, no empty chart (§2).
    show_rs = (should_show_turbulence_panel(Re)
               and data['has_rs']
               and has_accumulated)

    if show_rs:
        mask_acc = ((ftt_all >= ftt_stats_start)
                    & np.isfinite(data['uu_RS'])
                    & (data['uu_RS'] > 0))
        if not np.any(mask_acc):
            print(f"[INFO] No valid RS data after FTT={ftt_stats_start:.0f}"
                  f" — RS/TKE panel hidden")
            show_rs = False

    # Informative messages for turbulent cases where the panel is deferred
    if should_show_turbulence_panel(Re) and not show_rs:
        if not has_accumulated:
            print(f"[INFO] FTT={current_ftt:.2f} ≤ FTT_STATS_START="
                  f"{ftt_stats_start:.0f} — accumulation not started, "
                  f"RS/TKE panel deferred (§2)")
        elif not data['has_rs']:
            print(f"[INFO] 4-col data format — no RS/TKE columns available")

    has_rho = rho_data is not None
    has_timing = timing_data is not None

    # ── Figure layout ──
    # Panels top→bottom: Ma_max | Ub+F* | <ρ> | [perf] | [RS/TKE if turbulent+accumulated]
    panel_list = ['ma', 'ub_fstar']
    if has_rho:
        panel_list.append('rho')
    if has_timing:
        panel_list.append('perf')
    if show_rs:
        panel_list.append('rs_tke')

    n_rows = len(panel_list)

    # ── Journal-standard figure dimensions ──
    # Double-column width: 174 mm = 6.85 in (JFM/PoF/JCP)
    FIG_W = 6.85  # inches
    height_ratios = []
    for p in panel_list:
        if p in ('ma', 'rho', 'perf'):
            height_ratios.append(0.7)
        else:
            height_ratios.append(1.0)

    # When RS/TKE panel exists, add extra figure height for the visual gap
    # between the upper group (panels a-c) and the bottom panel (d)
    extra_gap = 0.6 if show_rs else 0.0
    fig_h = sum(height_ratios) * 1.5 + 0.4 + extra_gap

    fig, axes = plt.subplots(
        n_rows, 1, figsize=(FIG_W, fig_h), sharex=False,
        gridspec_kw={'height_ratios': height_ratios},
    )
    if n_rows == 1:
        axes = [axes]

    # ── Build panels ──
    panel_idx = 0

    # Panel: Ma_max (top)
    build_ma_panel(axes[panel_idx], data)
    panel_idx += 1

    # Panel: Ub/Uref + F* (middle)
    build_ub_fstar_panel(axes[panel_idx], data, Re, ftt_stats_start)
    panel_idx += 1

    # Panel: <ρ> density (if available)
    if has_rho:
        build_rho_panel(axes[panel_idx], rho_data)
        panel_idx += 1

    # Panel: Performance (MLUPS + MPI)
    if has_timing:
        perf_label = '(d)' if has_rho else '(c)'
        build_perf_panel(axes[panel_idx], timing_data,
                         panel_label=perf_label)
        panel_idx += 1

    # Panel: RS/TKE (turbulent only, FTT > FTT_STATS_START only — §2/§5)
    if show_rs:
        rs_label = '(e)' if has_timing else '(d)'
        build_rs_tke_panel(axes[panel_idx], data, ftt_stats_start,
                           panel_label=rs_label)
        panel_idx += 1

    # ── [需求1] Set x-axis ranges per panel ──
    # Upper panels (Ma, Ub/F*, ρ): full FTT range.
    # RS/TKE panel: independent x-axis starting at accu_start_ftt.
    #
    # [需求1] Detect actual accumulation start from accu_count
    accu_start_ftt = ftt_stats_start  # fallback
    if data['has_rs']:
        accu = data['accu_count']
        for ii in range(len(accu)):
            if np.isfinite(accu[ii]) and accu[ii] > 0:
                accu_start_ftt = float(ftt_all[ii])
                print(f"[INFO] [需求1] Detected accu_count > 0 at FTT = {accu_start_ftt:.4f}")
                break

    # Find the index of the last upper-group panel (before rs_tke)
    rs_tke_idx = panel_list.index('rs_tke') if 'rs_tke' in panel_list else -1
    last_upper_idx = rs_tke_idx - 1 if rs_tke_idx > 0 else n_rows - 1

    for i, pname in enumerate(panel_list):
        if pname == 'rs_tke':
            # [需求1] Independent x-axis: accu_start_ftt → end
            ftt_acc = ftt_all[ftt_all >= accu_start_ftt]
            tight_axis_range(axes[i], ftt_acc)
        else:
            tight_axis_range(axes[i], ftt_all)

        # [需求1] When RS/TKE exists, the upper group and bottom panel have
        # DIFFERENT time axes. Show x-ticks + xlabel on:
        #   (a) last upper-group panel — marks upper time range boundary
        #   (b) the bottom RS/TKE panel — marks accumulated time range
        if show_rs and i == last_upper_idx:
            axes[i].tick_params(labelbottom=True)
            axes[i].set_xlabel(r"FTT (Flow-Through Time)")
        elif i == n_rows - 1:
            axes[i].tick_params(labelbottom=True)
            if show_rs:
                axes[i].set_xlabel(r"FTT (Flow-Through Time, accumulated)")
            else:
                axes[i].set_xlabel(r"FTT (Flow-Through Time)")
        else:
            axes[i].tick_params(labelbottom=False)

    # ── Layout ──
    fig.tight_layout()
    if show_rs and n_rows >= 4:
        # Extra vertical gap between panel (c) and panel (d)
        # to visually separate the two different time-axis groups
        fig.subplots_adjust(hspace=0.35)
        # Move panel (d) down by adjusting its gridspec position
        gs = axes[-1].get_gridspec()
        # Manually shift bottom panel down
        pos_d = axes[-1].get_position()
        axes[-1].set_position([pos_d.x0, pos_d.y0 - 0.04,
                               pos_d.width, pos_d.height])
    else:
        fig.subplots_adjust(hspace=0.35)

    # ── Save ──
    save_figure(fig, Re, SCRIPT_DIR)
    plt.close(fig)


if __name__ == '__main__':
    main()