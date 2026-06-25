#!/usr/bin/env python3
"""
validate_poisson_parallel.py — Verify the parallel div-exact projection
(_DivExactFftProjector.correction with a fork Pool) is BITWISE-identical to the
serial reference path (POISSON_SERIAL=1).

Each FFT mode is factored+solved independently, so parallel must equal serial
to the last bit.  Small synthetic curvilinear grid; fast (<2s).
"""
import sys, os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import interp_checkpoint as ic
import poisson_projection as pp
BFR = ic.BFR


def make_curvilinear_grid(cfg):
    """A mildly non-uniform (curvilinear) y/z grid so the metrics are nontrivial
    but the operators stay well-conditioned."""
    NY6, NZ6 = cfg.NY6, cfg.NZ6
    y2d = np.zeros((NY6, NZ6)); z2d = np.zeros((NY6, NZ6))
    ys = np.linspace(0.0, ic.LX, cfg.NY)              # streamwise extent ~ LX
    zs = np.linspace(1.0, 3.0, cfg.NZ)                # wall-normal 1..3
    YY, ZZ = np.meshgrid(ys, zs, indexing='ij')
    # gentle shear so dj_dz / dk_dy are nonzero (genuine curvilinear coupling)
    ZZ = ZZ + 0.05 * np.sin(2 * np.pi * YY / max(ic.LX, 1e-9))
    y2d[BFR:BFR+cfg.NY, BFR:BFR+cfg.NZ] = YY
    z2d[BFR:BFR+cfg.NY, BFR:BFR+cfg.NZ] = ZZ
    return y2d, z2d


def main():
    print("=" * 64)
    print("  VALIDATE: parallel vs serial div-exact Poisson projection")
    print("=" * 64)

    cfg = ic.GridConfig(nx=12, ny=13, nz=9, jp=1, gamma=1.0, alpha=0.5, grid_dat='x')
    y2d, z2d = make_curvilinear_grid(cfg)
    dj_dy, dj_dz, dk_dy, dk_dz = ic.compute_inverse_metric_2d(y2d, z2d)

    dx = ic.LX / (cfg.NX - 1)
    nj, nk, ni = cfg.NY - 1, cfg.NZ, cfg.NX - 1
    n_modes = ni // 2 + 1
    print(f"  grid: nj={nj} nk={nk} ni={ni}  -> {n_modes} FFT modes, n={nj*nk} DOF/mode")

    # deterministic RHS shaped (nj, nk, ni)
    rng = np.random.default_rng(7)
    rhs = rng.standard_normal((nj, nk, ni))

    # Build the projector once (operators only; factor deferred to correction).
    proj = pp._DivExactFftProjector(dx, dj_dy, dj_dz, dk_dy, dk_dz, nj, nk, ni,
                                    verbose=True)

    # --- serial reference ---
    os.environ['POISSON_SERIAL'] = '1'
    qx_s, qy_s, qz_s = proj.correction(rhs)
    os.environ.pop('POISSON_SERIAL', None)

    # --- parallel ---
    nw = pp._divex_num_workers(n_modes)
    print(f"  parallel workers available: {nw}")
    qx_p, qy_p, qz_p = proj.correction(rhs)

    dqx = float(np.max(np.abs(qx_p - qx_s)))
    dqy = float(np.max(np.abs(qy_p - qy_s)))
    dqz = float(np.max(np.abs(qz_p - qz_s)))
    worst = max(dqx, dqy, dqz)

    print("-" * 64)
    print(f"  max|qx_par - qx_ser| = {dqx:.3e}")
    print(f"  max|qy_par - qy_ser| = {dqy:.3e}")
    print(f"  max|qz_par - qz_ser| = {dqz:.3e}")
    bit = (worst == 0.0)
    print(f"  worst = {worst:.3e}  -> {'BITWISE-IDENTICAL ✓' if bit else ('close (<1e-13)' if worst < 1e-13 else 'FAIL ✗')}")

    # Also confirm the correction actually reduces divergence equivalently:
    # apply q and check the residual D(rhs - Dq-image) — here just confirm
    # parallel path produced finite, non-trivial output.
    finite = all(np.all(np.isfinite(a)) for a in (qx_p, qy_p, qz_p))
    nonzero = worst >= 0.0 and float(np.max(np.abs(qx_s))) > 0.0
    print(f"  parallel output finite: {finite};  serial output non-trivial: {nonzero}")

    ok = bit and finite and nonzero
    print("=" * 64)
    print(f"  RESULT: {'PASS — parallel == serial bitwise' if ok else 'CHECK'}")
    return 0 if ok else (2 if (worst < 1e-13 and finite) else 1)


if __name__ == '__main__':
    sys.exit(main())
