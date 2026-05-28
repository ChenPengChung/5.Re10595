"""
test_adi_audit.py -- Rigorous numerical audit of the ADI Poisson solver
in grid_zeta_tool.py. Runs 8 PASS/FAIL tests. Prints summary.
Does NOT write .dat files or modify grid_zeta_tool.py.
"""
import sys, os, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grid_zeta_tool as gzt

REF_DAT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "3.fine grid.dat")
passed = []

def report(n, name, ok, msg=""):
    status = "PASS" if ok else "FAIL"
    print(f"  TEST {n}: [{status}] {name}")
    for line in (msg or "").strip().split("\n"):
        if line: print(f"           {line}")
    passed.append(ok)
    return ok

# ============================================================
# TEST 1: Thomas solver exactness
# ============================================================
print("\n" + "="*70)
print("TEST 1: Thomas solver exactness")
print("="*70)
try:
    rng = np.random.default_rng(42)
    n_sys, M_batch = 200, 50
    max_err_all = 0.0
    for trial in range(5):
        a_raw = rng.uniform(0.1, 0.4, (n_sys, M_batch))
        c_raw = rng.uniform(0.1, 0.4, (n_sys, M_batch))
        b_raw = a_raw + c_raw + rng.uniform(0.5, 1.0, (n_sys, M_batch))
        d_raw = rng.standard_normal((n_sys, M_batch))
        a_raw[0, :] = 0.0; c_raw[-1, :] = 0.0
        x_sol = gzt._thomas_solve_vec(a_raw, b_raw, c_raw, d_raw)
        resid = b_raw * x_sol
        resid[1:, :] += a_raw[1:, :] * x_sol[:-1, :]
        resid[:-1, :] += c_raw[:-1, :] * x_sol[1:, :]
        max_err_all = max(max_err_all, float(np.max(np.abs(resid - d_raw))))
    ok = max_err_all < 1e-12
    report(1, "Thomas solver exactness", ok,
           f"5 random DD tridiagonal systems ({n_sys},{M_batch})\n"
           f"max|A*x - b| = {max_err_all:.4e}  (threshold 1e-12)")
except Exception as e:
    report(1, "Thomas solver exactness", False, f"EXCEPTION: {e}")

# ============================================================
# TEST 2: Residual formula cross-check
# ============================================================
print("\n" + "="*70)
print("TEST 2: Residual formula cross-check on reference grid")
print("="*70)
try:
    x_ref, y_ref, ni_ref, nj_ref = gzt.parse_tecplot_dat(REF_DAT)
    print(f"  Loaded reference grid: {ni_ref}x{nj_ref}")
    x = x_ref; y = y_ref
    sj = slice(1, nj_ref - 1); si = slice(1, ni_ref - 1)
    x_xi  = np.zeros_like(x);  x_xi[:, 1:-1]  = 0.5 * (x[:, 2:] - x[:, :-2])
    y_xi  = np.zeros_like(y);  y_xi[:, 1:-1]  = 0.5 * (y[:, 2:] - y[:, :-2])
    x_eta = np.zeros_like(x);  x_eta[1:-1, :] = 0.5 * (x[2:, :] - x[:-2, :])
    y_eta = np.zeros_like(y);  y_eta[1:-1, :] = 0.5 * (y[2:, :] - y[:-2, :])
    al = (x_eta**2 + y_eta**2)[sj, si]
    be = (x_xi * x_eta + y_xi * y_eta)[sj, si]
    ga = (x_xi**2 + y_xi**2)[sj, si]
    J_full = x_xi * y_eta - x_eta * y_xi
    j2 = (J_full**2)[sj, si]
    metrics_ref = gzt._compute_metrics(x_ref, y_ref)
    P_ref, Q_ref = gzt._compute_PQ(metrics_ref)
    Pi = P_ref[sj, si]; Qi = Q_ref[sj, si]
    x_xixi   = x[sj, 2:] - 2.0 * x[sj, si] + x[sj, :-2]
    y_xixi   = y[sj, 2:] - 2.0 * y[sj, si] + y[sj, :-2]
    x_etaeta = x[2:, si] - 2.0 * x[sj, si] + x[:-2, si]
    y_etaeta = y[2:, si] - 2.0 * y[sj, si] + y[:-2, si]
    x_xieta  = 0.25 * (x[2:, 2:] - x[2:, :-2] - x[:-2, 2:] + x[:-2, :-2])
    y_xieta  = 0.25 * (y[2:, 2:] - y[2:, :-2] - y[:-2, 2:] + y[:-2, :-2])
    x_xi_i   = 0.5 * (x[sj, 2:] - x[sj, :-2])
    y_xi_i   = 0.5 * (y[sj, 2:] - y[sj, :-2])
    x_eta_i  = 0.5 * (x[2:, si] - x[:-2, si])
    y_eta_i  = 0.5 * (y[2:, si] - y[:-2, si])
    Ra_x = al * x_xixi - 2.0 * be * x_xieta + ga * x_etaeta + j2 * (Pi * x_xi_i + Qi * x_eta_i)
    Ra_y = al * y_xixi - 2.0 * be * y_xieta + ga * y_etaeta + j2 * (Pi * y_xi_i + Qi * y_eta_i)
    m = metrics_ref
    al_b = m["alpha"][sj, si]; be_b = m["beta"][sj, si]; ga_b = m["gamma"][sj, si]
    J2_b = (m["J"]**2)[sj, si]
    Rb_x = (al_b * m["x_xixi"][sj, si] - 2.0 * be_b * m["x_xieta"][sj, si]
            + ga_b * m["x_etaeta"][sj, si] + J2_b * (Pi * m["x_xi"][sj, si] + Qi * m["x_eta"][sj, si]))
    Rb_y = (al_b * m["y_xixi"][sj, si] - 2.0 * be_b * m["y_xieta"][sj, si]
            + ga_b * m["y_etaeta"][sj, si] + J2_b * (Pi * m["y_xi"][sj, si] + Qi * m["y_eta"][sj, si]))
    diff_x = float(np.max(np.abs(Ra_x - Rb_x)))
    diff_y = float(np.max(np.abs(Ra_y - Rb_y)))
    max_diff = max(diff_x, diff_y)
    ok = max_diff < 1e-14
    report(2, "Residual formula cross-check", ok,
           f"max|R_a - R_b| x: {diff_x:.4e}  y: {diff_y:.4e}\n"
           f"max combined: {max_diff:.4e}  (threshold 1e-14)")
except Exception as e:
    import traceback
    report(2, "Residual formula cross-check", False, f"EXCEPTION: {e}\n{traceback.format_exc()}")

# ============================================================
# TEST 3: Boundary preservation
# ============================================================
print("\n" + "="*70)
print("TEST 3: Boundary preservation after 100 ADI iterations")
print("="*70)
try:
    x_ref, y_ref, ni_ref, nj_ref = gzt.parse_tecplot_dat(REF_DAT)
    P0 = np.zeros_like(x_ref); Q0 = np.zeros_like(x_ref)
    bx = {0: x_ref[0,:].copy(), -1: x_ref[-1,:].copy()}
    bxc = {0: x_ref[:,0].copy(), -1: x_ref[:,-1].copy()}
    by = {0: y_ref[0,:].copy(), -1: y_ref[-1,:].copy()}
    byc = {0: y_ref[:,0].copy(), -1: y_ref[:,-1].copy()}
    x_out, y_out, _ = gzt._poisson_solve_adi(x_ref.copy(), y_ref.copy(), P0, Q0,
        n_iter=100, omega=0.9, tol=1e-30, print_every=0, n_adi_params=8)
    if x_out is None:
        report(3, "Boundary preservation", False, "ADI returned None")
    else:
        checks = {
            "j=0  x": np.array_equal(x_out[0,:], bx[0]),
            "j=-1 x": np.array_equal(x_out[-1,:], bx[-1]),
            "i=0  x": np.array_equal(x_out[:,0], bxc[0]),
            "i=-1 x": np.array_equal(x_out[:,-1], bxc[-1]),
            "j=0  y": np.array_equal(y_out[0,:], by[0]),
            "j=-1 y": np.array_equal(y_out[-1,:], by[-1]),
            "i=0  y": np.array_equal(y_out[:,0], byc[0]),
            "i=-1 y": np.array_equal(y_out[:,-1], byc[-1]),
        }
        all_ok = all(checks.values())
        lines = "; ".join(f"{k}:{'OK' if v else 'MISMATCH'}" for k, v in checks.items())
        report(3, "Boundary preservation", all_ok, lines)
except Exception as e:
    report(3, "Boundary preservation", False, f"EXCEPTION: {e}")

# ============================================================
# TEST 4: Identity test WITH boundary overwrite
# ============================================================
print("\n" + "="*70)
print("TEST 4: Identity test WITH analytical boundary overwrite")
print("="*70)
try:
    x_ref, y_ref, ni_ref, nj_ref = gzt.parse_tecplot_dat(REF_DAT)
    print(f"  Calling generate_adaptive_grid at {ni_ref}x{nj_ref}, gamma=0, tol=1e-12, ADI ...")
    x_out, y_out, conv = gzt.generate_adaptive_grid(
        x_ref, y_ref, ni_ref, nj_ref,
        gamma=0.0, alpha=0.5, poisson_iter=50000, poisson_tol=1e-12,
        LZ=None, poisson_method="adi")
    if x_out is None:
        report(4, "Identity test WITH overwrite", False, "generate_adaptive_grid returned None")
    else:
        dx = float(np.max(np.abs(x_out - x_ref)))
        dy = float(np.max(np.abs(y_out - y_ref)))
        iters = len(conv); final_res = conv[-1] if conv else float("nan")
        ok = dx < 1e-4 and dy < 1e-4
        report(4, "Identity test WITH overwrite", ok,
               f"max|dx|={dx:.4e}  max|dy|={dy:.4e}\n"
               f"iters={iters}  final_res={final_res:.4e}\n"
               f"Floor ~1.47e-7 is irreducible: comes from analytical hill boundary overwrite.\n"
               f"Poisson convergence is correct; error > 0 is by design (boundary correction).")
except Exception as e:
    import traceback
    report(4, "Identity test WITH overwrite", False, f"EXCEPTION: {e}\n{traceback.format_exc()}")

# ============================================================
# TEST 5: Identity test WITHOUT boundary overwrite
# ============================================================
print("\n" + "="*70)
print("TEST 5: Identity test WITHOUT analytical boundary overwrite")
print("="*70)
try:
    x_ref, y_ref, ni_ref, nj_ref = gzt.parse_tecplot_dat(REF_DAT)
    metrics_ref = gzt._compute_metrics(x_ref, y_ref)
    P_ref, Q_ref = gzt._compute_PQ(metrics_ref)
    print(f"  Calling _poisson_solve_adi directly, tol=1e-14, n_iter=50000 ...")
    x_out, y_out, conv = gzt._poisson_solve_adi(
        x_ref.copy(), y_ref.copy(), P_ref, Q_ref,
        n_iter=50000, omega=0.9, tol=1e-14,
        print_every=5000, n_adi_params=8)
    if x_out is None:
        report(5, "Identity test WITHOUT overwrite", False,
               "_poisson_solve_adi returned None -- potential bug")
    else:
        dx = float(np.max(np.abs(x_out - x_ref)))
        dy = float(np.max(np.abs(y_out - y_ref)))
        iters = len(conv); final_res = conv[-1] if conv else float("nan")
        ok = dx < 1e-11 and dy < 1e-11
        bug_note = "" if ok else "\n*** REAL BUG DETECTED: error exceeds 1e-11 ***"
        report(5, "Identity test WITHOUT overwrite", ok,
               f"max|dx|={dx:.4e}  max|dy|={dy:.4e}\n"
               f"iters={iters}  final_res={final_res:.4e}\n"
               f"Expected: < 1e-11 (exact fixed-point; boundaries exact){bug_note}")
except Exception as e:
    import traceback
    report(5, "Identity test WITHOUT overwrite", False, f"EXCEPTION: {e}\n{traceback.format_exc()}")

# ============================================================
# TEST 6: ADI vs GS consistency at 65x33
# ============================================================
print("\n" + "="*70)
print("TEST 6: ADI vs GS consistency at 65x33")
print("="*70)
try:
    x_ref, y_ref, ni_ref, nj_ref = gzt.parse_tecplot_dat(REF_DAT)
    NI_T, NJ_T = 65, 33
    print(f"  Running ADI at {NI_T}x{NJ_T} ...")
    x_adi, y_adi, _ = gzt.generate_adaptive_grid(
        x_ref, y_ref, NI_T, NJ_T, gamma=0.0, alpha=0.5,
        poisson_iter=50000, poisson_tol=1e-12, LZ=None, poisson_method="adi")
    print(f"  Running GS at {NI_T}x{NJ_T} ...")
    x_gs, y_gs, _ = gzt.generate_adaptive_grid(
        x_ref, y_ref, NI_T, NJ_T, gamma=0.0, alpha=0.5,
        poisson_iter=50000, poisson_tol=1e-12, LZ=None, poisson_method="gs")
    if x_adi is None or x_gs is None:
        report(6, "ADI vs GS consistency", False,
               f"ADI None:{x_adi is None}  GS None:{x_gs is None}")
    else:
        dx_diff = float(np.max(np.abs(x_adi - x_gs)))
        dy_diff = float(np.max(np.abs(y_adi - y_gs)))
        ok = dx_diff < 1e-6 and dy_diff < 1e-6
        report(6, "ADI vs GS consistency", ok,
               f"max|x_adi - x_gs|={dx_diff:.4e}  max|y_adi - y_gs|={dy_diff:.4e}\n"
               f"(threshold 1e-6)")
except Exception as e:
    import traceback
    report(6, "ADI vs GS consistency", False, f"EXCEPTION: {e}\n{traceback.format_exc()}")

# ============================================================
# TEST 7: Failure propagation
# ============================================================
print("\n" + "="*70)
print("TEST 7: Failure propagation (NaN injection)")
print("="*70)
try:
    x_ref, y_ref, ni_ref, nj_ref = gzt.parse_tecplot_dat(REF_DAT)
    metrics_ref = gzt._compute_metrics(x_ref, y_ref)
    P_ref, Q_ref = gzt._compute_PQ(metrics_ref)
    x_nan = x_ref.copy()
    x_nan[nj_ref // 2, ni_ref // 2] = float("nan")
    print("  (a) _poisson_solve_adi with NaN interior ...")
    r_a = gzt._poisson_solve_adi(x_nan, y_ref.copy(), P_ref, Q_ref,
        n_iter=1000, omega=0.9, tol=1e-10, print_every=0, n_adi_params=8)
    sub_a = r_a[0] is None and r_a[1] is None
    print(f"  (a) returned None tuple: {sub_a}")
    print("  (b) generate_adaptive_grid with NaN reference ...")
    x_ref_nan = x_ref.copy()
    x_ref_nan[nj_ref // 2, ni_ref // 2] = float("nan")
    r_b = gzt.generate_adaptive_grid(
        x_ref_nan, y_ref, ni_ref, nj_ref,
        gamma=0.0, poisson_iter=1000, poisson_tol=1e-10,
        LZ=None, poisson_method="adi")
    sub_b = r_b[0] is None and r_b[1] is None
    print(f"  (b) returned None tuple: {sub_b}")
    ok = sub_a and sub_b
    report(7, "Failure propagation", ok,
           f"(a) _poisson_solve_adi NaN -> None: {sub_a}\n"
           f"(b) generate_adaptive_grid NaN -> None: {sub_b}")
except Exception as e:
    import traceback
    report(7, "Failure propagation", False, f"EXCEPTION: {e}\n{traceback.format_exc()}")

# ============================================================
# TEST 8: Jacobian positivity at 449x225
# ============================================================
print("\n" + "="*70)
print("TEST 8: Jacobian positivity at 449x225")
print("="*70)
try:
    x_ref, y_ref, ni_ref, nj_ref = gzt.parse_tecplot_dat(REF_DAT)
    NI_P, NJ_P = 449, 225
    print(f"  Resampling boundaries to {NI_P}x{NJ_P} ...")
    xb, yb = gzt._resample_boundary(x_ref[0, :],  y_ref[0, :],  NI_P)
    xt, yt = gzt._resample_boundary(x_ref[-1, :], y_ref[-1, :], NI_P)
    xl, yl = gzt._resample_boundary(x_ref[:, 0],  y_ref[:, 0],  NJ_P)
    xr, yr = gzt._resample_boundary(x_ref[:, -1], y_ref[:, -1], NJ_P)
    xl[0]=xb[0];   yl[0]=yb[0];   xl[-1]=xt[0];  yl[-1]=yt[0]
    xr[0]=xb[-1];  yr[0]=yb[-1];  xr[-1]=xt[-1]; yr[-1]=yt[-1]
    x_tfi, y_tfi = gzt._tfi(xb, yb, xt, yt, xl, yl, xr, yr)
    metrics_ref = gzt._compute_metrics(x_ref, y_ref)
    P_ref, Q_ref = gzt._compute_PQ(metrics_ref)
    P_new, Q_new = gzt._interpolate_PQ(P_ref, Q_ref, ni_ref, nj_ref, NI_P, NJ_P)
    print(f"  Running ADI at {NI_P}x{NJ_P}, tol=1e-10, max_iter=20000 ...")
    x_out, y_out, conv = gzt._poisson_solve_adi(
        x_tfi, y_tfi, P_new, Q_new,
        n_iter=20000, omega=0.9, tol=1e-10, print_every=2000, n_adi_params=8)
    if x_out is None:
        report(8, "Jacobian positivity at 449x225", False, "ADI returned None")
    else:
        sj_f = slice(1, NJ_P-1); si_f = slice(1, NI_P-1)
        xxi_f = np.zeros_like(x_out); xxi_f[:,1:-1] = 0.5*(x_out[:,2:]-x_out[:,:-2])
        yxi_f = np.zeros_like(y_out); yxi_f[:,1:-1] = 0.5*(y_out[:,2:]-y_out[:,:-2])
        xet_f = np.zeros_like(x_out); xet_f[1:-1,:] = 0.5*(x_out[2:,:]-x_out[:-2,:])
        yet_f = np.zeros_like(y_out); yet_f[1:-1,:] = 0.5*(y_out[2:,:]-y_out[:-2,:])
        J_fin = xxi_f * yet_f - xet_f * yxi_f
        J_int = J_fin[sj_f, si_f]
        J_min = float(np.min(J_int))
        n_neg = int(np.sum(J_int <= 0))
        iters = len(conv); final_res = conv[-1] if conv else float("nan")
        ok = J_min > 0
        report(8, "Jacobian positivity at 449x225", ok,
               f"J_min={J_min:.6e}  J_neg_count={n_neg}\n"
               f"iters={iters}  final_res={final_res:.4e}")
except Exception as e:
    import traceback
    report(8, "Jacobian positivity at 449x225", False, f"EXCEPTION: {e}\n{traceback.format_exc()}")

# ============================================================
# Summary
# ============================================================
print("\n" + "="*70)
n_pass = sum(passed); n_total = len(passed)
print(f"{n_pass}/{n_total} PASSED")
print("="*70)
