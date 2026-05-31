#!/usr/bin/env python3
"""
validate_vec_interp.py — Verify interpolate_lagrange7_3d_with_mapping_vec is
bitwise-identical to the scalar interpolate_lagrange7_3d_with_mapping.

Synthetic small grids that DELIBERATELY exercise:
  - interior points (no wall ghost)
  - bottom-wall ghost stencils (k_o in {0,1,2})
  - top-wall ghost stencils (k_o near NZ_old)
  - i-stencil clamping at the spanwise edges

PASS criterion: max|vec - scalar| == 0.0 (true bitwise).  A nonzero-but-tiny
diff (<=1e-13) would still pass the 1e-10 divergence gate but is reported as
NON-BITWISE so the reviewer can decide.
"""
import sys, os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import interp_checkpoint as ic


def build_case(seed, nx_o, ny_o, nz_o, nx_n, ny_n, nz_n):
    rng = np.random.default_rng(seed)
    cfg_old = ic.GridConfig(nx_o, ny_o, nz_o, jp=1, gamma=1.0, alpha=0.5, grid_dat='old')
    cfg_new = ic.GridConfig(nx_n, ny_n, nz_n, jp=1, gamma=1.0, alpha=0.5, grid_dat='new')

    # OLD field with ghost buffer, random but deterministic.
    field_old = rng.standard_normal((cfg_old.NY6, cfg_old.NZ6, cfg_old.NX6))

    # Mapping anchors. Cover the FULL k_o range so bottom+top wall ghost fire.
    jstar = rng.integers(0, cfg_old.NY, size=(ny_n, nz_n)).astype(np.int64)
    kcol = np.linspace(0, cfg_old.NZ - 1, nz_n).round().astype(np.int64)
    kcol = np.clip(kcol, 0, cfg_old.NZ - 1)
    kstar = np.broadcast_to(kcol[None, :], (ny_n, nz_n)).copy()
    # Force the first/last few k columns onto the exact wall cells.
    for kk in range(min(3, nz_n)):
        kstar[:, kk] = kk
    for kk in range(min(3, nz_n)):
        kstar[:, nz_n - 1 - kk] = cfg_old.NZ - 1 - kk

    xistar = rng.random((ny_n, nz_n))
    etastar = rng.random((ny_n, nz_n))
    i_o_arr = rng.integers(0, cfg_old.NX, size=nx_n).astype(np.int64)
    xi_i_arr = rng.random(nx_n)

    mapping = ic.PhysMapping2D(jstar, kstar, xistar, etastar,
                              i_o_arr, xi_i_arr, cfg_old, cfg_new)
    return field_old, mapping


def main():
    print("=" * 64)
    print("  VALIDATE: vec vs scalar Lagrange-7 3D interpolation")
    print("=" * 64)

    # --- Unit test A: batched weights bitwise == scalar lagrange7_weights ---
    rng = np.random.default_rng(0)
    t = rng.random(5000)
    w_vec = ic._lagrange7_weights_batched_exact(t)
    w_ref = np.array([ic.lagrange7_weights(float(ti)) for ti in t])
    dA = float(np.max(np.abs(w_vec - w_ref)))
    print(f"[A] batched weights vs scalar: max|diff| = {dA:.3e}  "
          f"{'BITWISE ✓' if dA == 0.0 else ('close' if dA < 1e-13 else 'FAIL')}")

    # --- Full-field tests over several seeds / shapes ---
    cases = [
        dict(seed=1, nx_o=9,  ny_o=13, nz_o=14, nx_n=8,  ny_n=11, nz_n=9),
        dict(seed=2, nx_o=12, ny_o=10, nz_o=18, nx_n=10, ny_n=16, nz_n=12),
        dict(seed=3, nx_o=7,  ny_o=21, nz_o=11, nx_n=6,  ny_n=9,  nz_n=15),
    ]
    all_bitwise = (dA == 0.0)
    worst = dA
    for c in cases:
        field_old, mapping = build_case(**c)
        ref = ic.interpolate_lagrange7_3d_with_mapping(field_old, mapping)
        # test multiple chunk sizes to prove chunking is invariant
        for ch in (1, 4, 1000):
            vec = ic.interpolate_lagrange7_3d_with_mapping_vec(
                field_old, mapping, chunk_rows=ch)
            d = float(np.max(np.abs(vec - ref)))
            worst = max(worst, d)
            tag = 'BITWISE ✓' if d == 0.0 else ('close' if d < 1e-13 else 'FAIL ✗')
            if d != 0.0:
                all_bitwise = False
            print(f"[seed={c['seed']} {c['nx_o']}x{c['ny_o']}x{c['nz_o']}"
                  f"->{c['nx_n']}x{c['ny_n']}x{c['nz_n']} chunk={ch:>4}] "
                  f"max|vec-scalar| = {d:.3e}  {tag}")

    print("-" * 64)
    print(f"  worst max|diff| over all tests = {worst:.3e}")
    if all_bitwise:
        print("  RESULT: BITWISE-IDENTICAL ✓  (safe to switch production path)")
        return 0
    elif worst < 1e-13:
        print("  RESULT: NON-bitwise but < 1e-13 — gate (1e-10) would still pass,")
        print("          but reviewer should confirm acceptability.")
        return 2
    else:
        print("  RESULT: FAIL — difference too large, do NOT switch.")
        return 1


if __name__ == '__main__':
    sys.exit(main())
