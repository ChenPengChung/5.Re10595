#!/usr/bin/env python3
"""
3D 物理域體積驗證 — Shoelace vs Jacobian 3×3 GL vs Analytical
=============================================================
Three ground-truth references:
  1. Analytical:  V = LX × (LY×LZ − ∫₀^LY h(y) dy)
  2. External grid Shoelace (polygon area × dx)
  3. External grid Jacobian 3×3 GL (6th-order FD for J, 6-pt Lagrange interp)

Key: j-direction (streamwise) is PERIODIC → need ghost extension with LY offset.
     k-direction (wall-normal) uses adaptive Fornberg FD (matching metric_terms.h).
"""
import numpy as np

# ============================================================
#  §0. Grid parameters (from variables.h)
# ============================================================
LX, LY, LZ, H_HILL = 4.5, 9.0, 3.036, 1.0
NX, NY, NZ, jp = 129, 257, 129, 8
NI, NJ = NY, NZ  # Frohlich grid dimensions: I=streamwise, J=wall-normal
bfr = 3

# ============================================================
#  §1. Hill function (Mellen-Fröhlich-Rodi, exact from model.h)
# ============================================================
def hill_function(Y):
    Yb = Y % LY if Y >= 0 else (Y % LY + LY) % LY
    s = 54.0 / 28.0
    t = Yb * 28.0
    r_right = (LY - Yb) * 28.0

    # left hill
    if Yb <= s * (9/54):
        return (1/28) * min(28, 28 + 0.006775070969851*t*t - 0.0021245277758*t*t*t)
    if Yb <= s * (14/54):
        return (1/28) * (25.07355893131 + 0.9754803562315*t - 0.1016116352781*t*t + 0.001889794677828*t*t*t)
    if Yb <= s * (20/54):
        return (1/28) * (25.79601052357 + 0.8206693007457*t - 0.09055370274339*t*t + 0.001626510569859*t*t*t)
    if Yb <= s * (30/54):
        return (1/28) * (40.46435022819 - 1.379581654948*t + 0.019458845041284*t*t - 0.000207031893219*t*t*t)
    if Yb <= s * (40/54):
        return (1/28) * (17.92461334664 + 0.8743920332081*t - 0.05567361123058*t*t + 0.0006277731764683*t*t*t)
    if Yb <= s:
        return (1/28) * max(0, 56.39011190988 - 2.010520359035*t + 0.01644919857549*t*t + 0.00002674976141766*t*t*t)
    # right hill
    if Yb >= LY - s:
        Yr = LY - Yb
        tr = Yr * 28.0
        if Yr <= s*(9/54):
            return (1/28)*min(28, 28 + 0.006775070969851*tr*tr - 0.0021245277758*tr*tr*tr)
        if Yr <= s*(14/54):
            return (1/28)*(25.07355893131 + 0.9754803562315*tr - 0.1016116352781*tr*tr + 0.001889794677828*tr*tr*tr)
        if Yr <= s*(20/54):
            return (1/28)*(25.79601052357 + 0.8206693007457*tr - 0.09055370274339*tr*tr + 0.001626510569859*tr*tr*tr)
        if Yr <= s*(30/54):
            return (1/28)*(40.46435022819 - 1.379581654948*tr + 0.019458845041284*tr*tr - 0.000207031893219*tr*tr*tr)
        if Yr <= s*(40/54):
            return (1/28)*(17.92461334664 + 0.8743920332081*tr - 0.05567361123058*tr*tr + 0.0006277731764683*tr*tr*tr)
        return (1/28)*max(0, 56.39011190988 - 2.010520359035*tr + 0.01644919857549*tr*tr + 0.00002674976141766*tr*tr*tr)
    return 0.0

# ============================================================
#  §2. Read external grid & non-dimensionalize
# ============================================================
grid_path = "J_Frohlich/adaptive_3.fine grid_I257_J129_g3.60_a0.5.dat"
print(f"Reading grid: {grid_path}")

with open(grid_path, 'r') as f:
    for line in f:
        if 'DT=' in line:
            break
    coords = []
    for line in f:
        coords.extend(line.split())

coords = np.array(coords, dtype=np.float64)
x_fro = coords[0::2].reshape(NJ, NI)  # Frohlich x → code y (streamwise)
y_fro = coords[1::2].reshape(NJ, NI)  # Frohlich y → code z (wall-normal)

h_phys = x_fro[0, -1] / LY
scale = H_HILL / h_phys
y_grid = x_fro * scale   # code y-coords [NJ, NI]  (NJ=129 wall-normal, NI=257 streamwise)
z_grid = y_fro * scale   # code z-coords [NJ, NI]

print(f"h_physical = {h_phys:.6e},  scale = {scale:.6f}")
print(f"y range: [{y_grid[0,0]:.6f}, {y_grid[0,-1]:.6f}]  (expect LY={LY})")
print(f"z range: [{z_grid[0,0]:.6f}, {z_grid[-1,0]:.6f}]  (expect LZ={LZ})")

# ============================================================
#  §3. Build EXTENDED arrays with periodic ghost in I-direction
#      Period = NI-1 = 256 unique nodes (I=0 and I=256 are same point)
#      Need ghost for: 7-pt FD stencil (±3) + 6-pt Lagrange stencil (−2..+3)
#      → extend by ±6 in I-direction
# ============================================================
PERIOD = NI - 1   # = 256
GHOST_I = 6
NI_ext = NI + 2 * GHOST_I   # extended streamwise size

# Allocate extended arrays [NJ, NI_ext]
y_ext = np.zeros((NJ, NI_ext))
z_ext = np.zeros((NJ, NI_ext))

for I_ext in range(NI_ext):
    I_orig = I_ext - GHOST_I   # maps to I = -6 .. NI+5 = -6..262
    # Periodic mapping: I_phys = I_orig % PERIOD, in [0, 255]
    I_phys = I_orig % PERIOD
    if I_phys < 0:
        I_phys += PERIOD
    # y-offset for periodic wrap: how many full periods away
    n_periods = (I_orig - I_phys) // PERIOD
    y_ext[:, I_ext] = y_grid[:, I_phys] + n_periods * LY
    z_ext[:, I_ext] = z_grid[:, I_phys]

# Verify: I_ext=GHOST_I (=6) maps to I_orig=0, which should equal y_grid[:,0]
assert np.allclose(y_ext[:, GHOST_I], y_grid[:, 0])
assert np.allclose(z_ext[:, GHOST_I], z_grid[:, 0])

print(f"\nExtended grid: NI_ext={NI_ext}, I_ext range [0,{NI_ext-1}]")
print(f"  I_ext={GHOST_I} ↔ I_orig=0 (y={y_ext[0,GHOST_I]:.4f})")
print(f"  I_ext={GHOST_I+PERIOD} ↔ I_orig={PERIOD} (y={y_ext[0,GHOST_I+PERIOD]:.4f}, should={LY})")

# ============================================================
#  §4. Fornberg FD6 coefficients (from metric_terms.h)
# ============================================================
FD6_COEFF = np.array([
    [-147,  360, -450,  400, -225,   72,  -10],  # p=0 forward
    [ -10,  -77,  150, -100,   50,  -15,    2],  # p=1
    [   2,  -24,  -35,   80,  -30,    8,   -1],  # p=2
    [  -1,    9,  -45,    0,   45,   -9,    1],  # p=3 central
    [   1,   -8,   30,  -80,   35,   24,   -2],  # p=4
    [  -2,   15,  -50,  100, -150,   77,   10],  # p=5
    [  10,  -72,  225, -400,  450, -360,  147],  # p=6 backward
], dtype=np.float64)

FD5_FWD = np.array([-137, 300, -300, 200, -75, 12], dtype=np.float64)
FD5_BWD = np.array([-12, 75, -200, 300, -300, 137], dtype=np.float64)

def fd6_k_adaptive(arr_1d, k, k_lo, k_hi):
    """Adaptive 6th-order FD in k (wall-normal), matching metric_terms.h.
       arr_1d: 1D array indexed by k (= arr[J, :] for a fixed I)."""
    NJ_local = len(arr_1d)
    if k == 0:
        # bottom buffer: 5th-order forward
        return np.dot(FD5_FWD, arr_1d[0:6]) / 60.0
    elif k == NJ_local - 1:
        # top buffer: 5th-order backward
        return np.dot(FD5_BWD, arr_1d[NJ_local-6:NJ_local]) / 60.0
    elif k >= k_lo and k <= k_hi:
        # adaptive Fornberg
        s = k - 3
        s = max(s, k_lo)
        s = min(s, k_hi - 6)
        p = k - s
        return np.dot(FD6_COEFF[p], arr_1d[s:s+7]) / 60.0
    else:
        # 2nd-order central fallback
        return (arr_1d[k+1] - arr_1d[k-1]) / 2.0

def fd6_i_central(arr_1d, i):
    """6th-order central FD in I-direction (extended array, always enough room)."""
    return (-arr_1d[i-3] + 9*arr_1d[i-2] - 45*arr_1d[i-1]
            + 45*arr_1d[i+1] - 9*arr_1d[i+2] + arr_1d[i+3]) / 60.0

# ============================================================
#  §5. Compute J_2D on the extended grid
#      I_ext = GHOST_I-3 .. GHOST_I+NI-1+3  (full FD range for all physical+stencil cells)
#      J     = 1 .. NJ-2  (adaptive FD covers all interior; J=0,NJ-1 use forward/backward)
# ============================================================
# For Lagrange stencil: cells from I_cell=0..PERIOD-1 (256 cells in I)
# Each cell's GL stencil: I_cell-2 .. I_cell+3 → need J at I_ext from
#   GHOST_I + (0-2) = 4  to  GHOST_I + (PERIOD-1+3) = 264
# FD at those points needs I_ext ± 3: 1 to 267 — all within [0, NI_ext-1=268]. ✓

# J ranges for stencil validity in k-direction:
k_lo_fd = 1   # adaptive FD computable from k=1 (Fornberg auto-shift)
k_hi_fd = NJ - 2  # = 127

# For the Lagrange stencil in k, the C code uses k_lo_J=3 (bfr), k_hi_J=NZ6-4=131.
# In Frohlich coords: k_lo_J maps to J = k_lo_J - bfr = 0, k_hi_J maps to J = 131-3 = 128 = NJ-1.
# But C code explicitly sets k_lo_J=3 (= J=0 in Frohlich) and k_hi_J=NZ6-4=131 (= J=128=NJ-1).
# The Lagrange stencil select is clamped to this range.
# We need J_2D valid at J = 0..NJ-1 for the stencil to work.

# Actually, let's compute J_2D everywhere we can:
J_2D = np.full((NI_ext, NJ), np.nan)

# I_ext range for physical cells + stencils: need I_ext from GHOST_I-5 to GHOST_I+PERIOD+4
# (for the outermost Lagrange stencil + FD). But let's just compute all valid I_ext.
i_lo_compute = 3   # need i-3 ≥ 0 → i ≥ 3
i_hi_compute = NI_ext - 4  # need i+3 ≤ NI_ext-1 → i ≤ NI_ext-4

# k_lo, k_hi for adaptive Fornberg (matching metric_terms.h)
k_lo_fornberg = 1   # J=1 can use adaptive (p shifted)
k_hi_fornberg = NJ - 2  # J=127

for i_ext in range(i_lo_compute, i_hi_compute + 1):
    for J in range(NJ):
        # ∂y/∂ξ (i-direction, central FD6)
        y_xi = fd6_i_central(y_ext[J, :], i_ext)
        # ∂z/∂ξ
        z_xi = fd6_i_central(z_ext[J, :], i_ext)
        # ∂y/∂ζ (k-direction, adaptive)
        y_zeta = fd6_k_adaptive(y_ext[:, i_ext], J, k_lo_fornberg, k_hi_fornberg)
        # ∂z/∂ζ
        z_zeta = fd6_k_adaptive(z_ext[:, i_ext], J, k_lo_fornberg, k_hi_fornberg)

        J_2D[i_ext, J] = y_xi * z_zeta - y_zeta * z_xi

n_valid = np.sum(np.isfinite(J_2D))
n_negative = np.sum(J_2D[np.isfinite(J_2D)] <= 0)
print(f"\n  J_2D computed: {n_valid} points, {n_negative} non-positive")
print(f"  J_2D range: [{np.nanmin(J_2D):.6e}, {np.nanmax(J_2D):.6e}]")

# ============================================================
#  §6. Analytical volume (Simpson N=100000)
# ============================================================
N_QUAD = 100000
y_q = np.linspace(0, LY, N_QUAD + 1)
h_q = np.array([hill_function(y) for y in y_q])
w_s = np.ones(N_QUAD + 1)
w_s[1:-1:2] = 4.0
w_s[2:-2:2] = 2.0
hill_integral = np.sum(w_s * h_q) * (LY / N_QUAD) / 3.0
A_yz_analytical = LY * LZ - hill_integral
V_analytical = LX * A_yz_analytical

print(f"\n{'='*70}")
print(f"  §6. ANALYTICAL (Simpson N={N_QUAD})")
print(f"{'='*70}")
print(f"  ∫₀^LY h(y) dy    = {hill_integral:.15e}")
print(f"  A_yz_analytical   = {A_yz_analytical:.15e}")
print(f"  V_analytical      = {V_analytical:.15e}")

# ============================================================
#  §7. External grid Shoelace (polygon area sum)
#      Use Frohlich coords directly: I_cell = 0..NI-2, J_cell = 0..NJ-2
# ============================================================
def shoelace_area_fro(I_cell, J_cell):
    """Shoelace area for cell (I_cell, J_cell) in Frohlich grid."""
    y0, z0 = y_grid[J_cell,   I_cell],   z_grid[J_cell,   I_cell]
    y1, z1 = y_grid[J_cell,   I_cell+1], z_grid[J_cell,   I_cell+1]
    y2, z2 = y_grid[J_cell+1, I_cell+1], z_grid[J_cell+1, I_cell+1]
    y3, z3 = y_grid[J_cell+1, I_cell],   z_grid[J_cell+1, I_cell]
    return 0.5 * abs(y0*z1 - z0*y1 + y1*z2 - z1*y2 + y2*z3 - z2*y3 + y3*z0 - z3*y0)

# For periodic: I_cell = 0..PERIOD-1 (256 cells), J_cell = 0..NJ-2 (128 cells)
# I_cell=255 uses I=255 and I=256(=0 periodic) — y_grid[:,256]=LY ≈ y_grid[:,0]+LY
# The Shoelace formula handles this correctly since it uses vertex coords directly.
area_shoe_total = 0.0
for I_cell in range(PERIOD):
    for J_cell in range(NJ - 1):
        area_shoe_total += shoelace_area_fro(I_cell, J_cell)

dx_uniform = LX / (NX - 1)
V_shoelace = (NX - 1) * dx_uniform * area_shoe_total

print(f"\n{'='*70}")
print(f"  §7. EXTERNAL GRID SHOELACE")
print(f"{'='*70}")
print(f"  Unique y-z cells  = {PERIOD} × {NJ-1} = {PERIOD*(NJ-1)}")
print(f"  Σ area_shoe       = {area_shoe_total:.15e}")
print(f"  V_shoelace        = {V_shoelace:.15e}")

# ============================================================
#  §8. External grid Jacobian 3×3 GL
#      Cell range: I_cell = 0..PERIOD-1, J_cell = 0..NJ-2
#      In extended array: I_ext = GHOST_I + I_cell
#      J_2D stencil validity:
#        I_ext: j_lo = i_lo_compute(=3), j_hi = i_hi_compute
#        J:     k_lo = 0, k_hi = NJ-1
# ============================================================
GL_NODES   = np.array([0.5*(1 - np.sqrt(3/5)), 0.5, 0.5*(1 + np.sqrt(3/5))])
GL_WEIGHTS = np.array([5/18, 8/18, 5/18])

def lagrange6_weights(x, start):
    w = np.zeros(6)
    for m in range(6):
        L = 1.0
        xm = float(start + m)
        for r in range(6):
            if r != m:
                xr = float(start + r)
                L *= (x - xr) / (xm - xr)
        w[m] = L
    return w

def select_stencil(cell_idx, lo, hi):
    """6-point stencil start, clamped to [lo, hi-5]."""
    ideal = cell_idx - 2
    max_start = hi - 5
    if max_start < lo:
        return None
    return max(lo, min(ideal, max_start))

# J_2D stencil bounds
# I_ext valid range: [i_lo_compute, i_hi_compute]
j_lo_stencil = i_lo_compute
j_hi_stencil = i_hi_compute
# J valid range: [0, NJ-1] — but C code uses k_lo_J=bfr=3 → in Frohlich J=0,
# k_hi_J = NZ6-4 = 131 → Frohlich J = 128 = NJ-1.
# So k_lo_stencil=0, k_hi_stencil=NJ-1
k_lo_stencil = 0
k_hi_stencil = NJ - 1

area_jac_total = 0.0
n_gl = 0
n_fallback = 0
per_cell_diff = []

for I_cell in range(PERIOD):
    I_ext_cell = GHOST_I + I_cell   # I_ext for this cell's lower-left corner
    for J_cell in range(NJ - 1):
        # Select 6-point stencils
        si = select_stencil(I_ext_cell, j_lo_stencil, j_hi_stencil)
        sk = select_stencil(J_cell, k_lo_stencil, k_hi_stencil)

        area_gl = None
        if si is not None and sk is not None:
            area_val = 0.0
            ok = True
            for a in range(3):
                for b in range(3):
                    xi_pos   = float(I_ext_cell) + GL_NODES[a]
                    zeta_pos = float(J_cell) + GL_NODES[b]
                    wi = lagrange6_weights(xi_pos, si)
                    wk = lagrange6_weights(zeta_pos, sk)
                    J_val = 0.0
                    for m in range(6):
                        for n in range(6):
                            v = J_2D[si + m, sk + n]
                            if not np.isfinite(v):
                                ok = False
                                break
                            J_val += wi[m] * wk[n] * v
                        if not ok:
                            break
                    if not ok or not np.isfinite(J_val) or J_val <= 0:
                        ok = False
                        break
                    area_val += GL_WEIGHTS[a] * GL_WEIGHTS[b] * J_val
                if not ok:
                    break
            if ok:
                area_gl = area_val

        area_shoe = shoelace_area_fro(I_cell, J_cell)

        if area_gl is not None:
            area_jac_total += area_gl
            n_gl += 1
            per_cell_diff.append(abs(area_gl - area_shoe))
        else:
            area_jac_total += area_shoe
            n_fallback += 1

V_jac_gl = (NX - 1) * dx_uniform * area_jac_total
per_cell_diff = np.array(per_cell_diff) if per_cell_diff else np.array([0])

print(f"\n{'='*70}")
print(f"  §8. JACOBIAN 3×3 GAUSS-LEGENDRE (6th-order FD + 6-pt Lagrange)")
print(f"{'='*70}")
print(f"  GL cells       = {n_gl}")
print(f"  Fallback cells = {n_fallback}")
print(f"  Σ area_Jac_GL  = {area_jac_total:.15e}")
print(f"  V_Jac_GL       = {V_jac_gl:.15e}")
if len(per_cell_diff) > 1:
    print(f"  Per-cell |ΔA| (GL−Shoe):")
    print(f"    max  = {per_cell_diff.max():.6e}")
    print(f"    mean = {per_cell_diff.mean():.6e}")

# ============================================================
#  §9. Full comparison
# ============================================================
print(f"\n{'='*70}")
print(f"  §9. COMPARISON TABLE")
print(f"{'='*70}")

print(f"\n  A_yz (y-z cross-section area):")
print(f"    Analytical    = {A_yz_analytical:.15e}")
print(f"    Shoelace      = {area_shoe_total:.15e}")
print(f"    Jacobian GL   = {area_jac_total:.15e}")

err_shoe = abs(area_shoe_total - A_yz_analytical) / A_yz_analytical
err_jac  = abs(area_jac_total  - A_yz_analytical) / A_yz_analytical

print(f"\n  V_3D = LX × A_yz:")
print(f"    Analytical    = {V_analytical:.15e}")
print(f"    Shoelace      = {V_shoelace:.15e}")
print(f"    Jacobian GL   = {V_jac_gl:.15e}")

err_v_shoe = abs(V_shoelace - V_analytical) / V_analytical
err_v_jac  = abs(V_jac_gl   - V_analytical) / V_analytical

print(f"\n  Relative errors (vs analytical):")
print(f"    Shoelace:    {err_v_shoe:.6e}")
print(f"    Jacobian GL: {err_v_jac:.6e}")
if err_v_jac > 0:
    ratio = err_v_shoe / err_v_jac
    if ratio > 1:
        print(f"    → Jacobian GL is {ratio:.1f}× more accurate than Shoelace")
    else:
        print(f"    → Shoelace is {1/ratio:.1f}× more accurate than Jacobian GL")

print(f"\n  Cross-method difference:")
print(f"    |V_Jac − V_Shoe| / V_Shoe = {abs(V_jac_gl - V_shoelace)/V_shoelace:.6e}")
print(f"    V_Jac − V_Shoe            = {V_jac_gl - V_shoelace:+.6e}")

print(f"\n  Grid: NY={NY}, NZ={NZ}, NX={NX}")
print(f"  Physical cells: {PERIOD}×{NJ-1}×{NX-1} = {PERIOD*(NJ-1)*(NX-1):,}")
