#!/usr/bin/env python3
"""
validate_mapping_cache.py — Verify the precompute_phys_mapping_2d disk cache:
  (1) cache key is deterministic + geometry-sensitive (no false hits)
  (2) MISS build then HIT load returns a BITWISE-identical mapping
  (3) npz round-trip preserves all 6 arrays exactly

Uses a small UNIFORM rectangular OLD/NEW grid pair (the cell search works on
any curvilinear grid; uniform is the simplest valid case).  Fast (<1s).
"""
import sys, os, shutil, tempfile
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import interp_checkpoint as ic
BFR = ic.BFR


def make_uniform_grid(cfg, y0, y1, z0, z1):
    """Build (y2d, z2d) of shape (NY6, NZ6) with a uniform interior grid."""
    y2d = np.zeros((cfg.NY6, cfg.NZ6), dtype=np.float64)
    z2d = np.zeros((cfg.NY6, cfg.NZ6), dtype=np.float64)
    ys = np.linspace(y0, y1, cfg.NY)
    zs = np.linspace(z0, z1, cfg.NZ)
    YY, ZZ = np.meshgrid(ys, zs, indexing='ij')          # (NY, NZ)
    y2d[BFR:BFR+cfg.NY, BFR:BFR+cfg.NZ] = YY
    z2d[BFR:BFR+cfg.NY, BFR:BFR+cfg.NZ] = ZZ
    return y2d, z2d


def main():
    print("=" * 64)
    print("  VALIDATE: precompute_phys_mapping_2d disk cache")
    print("=" * 64)
    ok = True

    # Isolate the cache dir to a temp location so we don't pollute the repo.
    tmp_cache = tempfile.mkdtemp(prefix='mapcache_test_')
    real_dirname = os.path.dirname
    # Monkeypatch: point the cache dir at our temp by overriding os.path.dirname
    # is fragile; instead set the module's file-based cache via env + patch.
    # Simpler: the cache dir is derived from __file__; we redirect by chdir-free
    # patching of os.makedirs target through a wrapper. Easiest: temporarily
    # replace the function's cache dir by patching np.savez target is overkill —
    # instead we patch the module attribute used to locate the cache.
    # The code computes _cache_dir = dirname(abspath(__file__)) + '/mapping_cache'.
    # We override by setting the env to disable, build once WITHOUT cache to get
    # the reference, then enable and build twice to exercise MISS->HIT.

    cfg_old = ic.GridConfig(nx=6, ny=9, nz=8, jp=1, gamma=1.0, alpha=0.5, grid_dat='old')
    cfg_new = ic.GridConfig(nx=5, ny=7, nz=6, jp=1, gamma=1.0, alpha=0.5, grid_dat='new')

    # OLD domain [0,8] x [1,3]; NEW strictly inside to avoid clamp/fallback noise.
    y2d_o, z2d_o = make_uniform_grid(cfg_old, 0.0, 8.0, 1.0, 3.0)
    y2d_n, z2d_n = make_uniform_grid(cfg_new, 0.5, 7.5, 1.2, 2.8)

    # --- Test 1: cache key determinism + geometry sensitivity ---
    k1 = ic._mapping_cache_key(y2d_o, z2d_o, y2d_n, z2d_n, cfg_old, cfg_new)
    k2 = ic._mapping_cache_key(y2d_o, z2d_o, y2d_n, z2d_n, cfg_old, cfg_new)
    y2d_n_pert = y2d_n.copy(); y2d_n_pert[BFR, BFR] += 1e-9
    k3 = ic._mapping_cache_key(y2d_o, z2d_o, y2d_n_pert, z2d_n, cfg_old, cfg_new)
    det = (k1 == k2)
    sens = (k1 != k3)
    print(f"[1] key determinism: {'PASS' if det else 'FAIL'} (k1==k2: {det})")
    print(f"    key geometry-sensitivity (1e-9 perturb -> different key): "
          f"{'PASS' if sens else 'FAIL'}")
    ok = ok and det and sens

    # --- Test 2: MISS build -> HIT load, bitwise-identical mapping ---
    # Redirect the cache dir to our temp by patching the module's view of
    # __file__-derived dir: precompute uses dirname(abspath(__file__)).
    # We instead clear any real cache for this key and let it write to the
    # real mapping_cache, then read it back — but to stay clean we use env to
    # build a reference WITHOUT cache first.
    os.environ['INTERP_NO_MAP_CACHE'] = '1'
    ref = ic.precompute_phys_mapping_2d(y2d_o, z2d_o, y2d_n, z2d_n, cfg_old, cfg_new)
    os.environ.pop('INTERP_NO_MAP_CACHE', None)

    real_cache_dir = os.path.join(os.path.dirname(os.path.abspath(ic.__file__)),
                                  'mapping_cache')
    cache_file = os.path.join(real_cache_dir, k1 + '.npz')
    # ensure clean slate for this key
    if os.path.isfile(cache_file):
        os.remove(cache_file)

    miss = ic.precompute_phys_mapping_2d(y2d_o, z2d_o, y2d_n, z2d_n, cfg_old, cfg_new)  # builds+saves
    built_file = os.path.isfile(cache_file)
    hit = ic.precompute_phys_mapping_2d(y2d_o, z2d_o, y2d_n, z2d_n, cfg_old, cfg_new)   # loads

    fields = ['jstar', 'kstar', 'xistar', 'etastar', 'i_o_arr', 'xi_i_arr']
    miss_vs_ref = all(np.array_equal(getattr(miss, f), getattr(ref, f)) for f in fields)
    hit_vs_miss = all(np.array_equal(getattr(hit, f), getattr(miss, f)) for f in fields)
    print(f"[2] cache file written: {'PASS' if built_file else 'FAIL'}")
    print(f"    MISS build == no-cache reference: {'PASS' if miss_vs_ref else 'FAIL'}")
    print(f"    HIT load == MISS build (bitwise): {'PASS' if hit_vs_miss else 'FAIL'}")
    ok = ok and built_file and miss_vs_ref and hit_vs_miss

    # --- Test 3: npz round-trip exactness on each field ---
    d = np.load(cache_file)
    rt = all(np.array_equal(d[f], getattr(miss, f)) for f in fields)
    print(f"[3] npz round-trip exact (all 6 arrays): {'PASS' if rt else 'FAIL'}")
    ok = ok and rt


    # --- Test 4: dtype preservation for all 6 arrays after npz round-trip ---
    expected_dtypes = {
        'jstar':    np.dtype('int32'),
        'kstar':    np.dtype('int32'),
        'xistar':   np.dtype('float64'),
        'etastar':  np.dtype('float64'),
        'i_o_arr':  np.dtype('int64'),
        'xi_i_arr': np.dtype('float64'),
    }
    dtype_ok = True
    for name, exp in expected_dtypes.items():
        loaded_dt = d[name].dtype
        orig_dt   = getattr(miss, name).dtype
        if loaded_dt != exp or orig_dt != exp:
            print(f"[4] dtype FAIL: {name} orig={orig_dt} loaded={loaded_dt} expected={exp}")
            dtype_ok = False
    print(f"[4] dtype preservation (all 6 arrays): {'PASS' if dtype_ok else 'FAIL'}")
    ok = ok and dtype_ok

    # --- Test 5: cfg-dim sensitivity (NX_old change -> different key) ---
    cfg_old_alt = ic.GridConfig(nx=cfg_old.NX + 1, ny=cfg_old.NY, nz=cfg_old.NZ,
                                jp=1, gamma=1.0, alpha=0.5, grid_dat='old_alt')
    k_alt = ic._mapping_cache_key(y2d_o, z2d_o, y2d_n, z2d_n, cfg_old_alt, cfg_new)
    cfg_new_alt = ic.GridConfig(nx=cfg_new.NX, ny=cfg_new.NY + 1, nz=cfg_new.NZ,
                                jp=1, gamma=1.0, alpha=0.5, grid_dat='new_alt')
    k_alt2 = ic._mapping_cache_key(y2d_o, z2d_o, y2d_n, z2d_n, cfg_old, cfg_new_alt)
    cfg_sens = (k1 != k_alt) and (k1 != k_alt2)
    print(f"[5] cfg-dim sensitivity (NX_old+1->diff key, NY_new+1->diff key): "
          f"{'PASS' if cfg_sens else 'FAIL'}")
    ok = ok and cfg_sens

    # --- Test 6: cfg objects on HIT path come from live args, not cache ---
    # Rebuild the cache file (it was loaded by Test 3 but file may still exist)
    if not os.path.isfile(cache_file):
        os.environ['INTERP_NO_MAP_CACHE'] = '1'
        _tmp_ref = ic.precompute_phys_mapping_2d(y2d_o, z2d_o, y2d_n, z2d_n, cfg_old, cfg_new)
        os.environ.pop('INTERP_NO_MAP_CACHE', None)
        _t = cache_file + '.tmp.npz'
        import numpy as _np2
        _np2.savez(_t, jstar=_tmp_ref.jstar, kstar=_tmp_ref.kstar,
                   xistar=_tmp_ref.xistar, etastar=_tmp_ref.etastar,
                   i_o_arr=_tmp_ref.i_o_arr, xi_i_arr=_tmp_ref.xi_i_arr)
        os.replace(_t, cache_file)
    cfg_old2 = ic.GridConfig(nx=cfg_old.NX, ny=cfg_old.NY, nz=cfg_old.NZ,
                             jp=1, gamma=1.0, alpha=0.5, grid_dat='old2')
    cfg_new2 = ic.GridConfig(nx=cfg_new.NX, ny=cfg_new.NY, nz=cfg_new.NZ,
                             jp=1, gamma=1.0, alpha=0.5, grid_dat='new2')
    hit2 = ic.precompute_phys_mapping_2d(y2d_o, z2d_o, y2d_n, z2d_n, cfg_old2, cfg_new2)
    cfg_reattach = (hit2.cfg_old is cfg_old2) and (hit2.cfg_new is cfg_new2)
    print(f"[6] HIT path cfg re-attached from live args (not cache): "
          f"{'PASS' if cfg_reattach else 'FAIL'}")
    ok = ok and cfg_reattach

        # cleanup our test cache file (leave the dir for real runs)
    try:
        os.remove(cache_file)
    except OSError:
        pass
    shutil.rmtree(tmp_cache, ignore_errors=True)

    print("-" * 64)
    print(f"  RESULT: {'ALL PASS ✓ — caching is correct' if ok else 'FAIL ✗'}")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
