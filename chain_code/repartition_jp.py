#!/usr/bin/env python3
"""Same-grid checkpoint repartitioner (jp change only) — POINT-TO-POINT, NO interpolation.

CRITICAL axis order: the solver stores each per-rank field with linear index
    idx = j*NX6*NZ6 + k*NX6 + i      (fileIO.h:923,943; result_writebin writes this buffer verbatim)
i.e. C-order shape **(NYD6, NZ6, NX6)** — j (streamwise, the decomposed axis) OUTERMOST,
i (spanwise) innermost. This mirrors the AUTHORITATIVE generator
phase2_generatecheckpoint/interp_checkpoint.py (read_rank_bin / stitch_y / split_y /
enforce_periodic_physical_duplicates / fill_ghost) for the same-grid case.

A previous version used the WRONG shape (NX6,NYD6,NZ6) + whole-slab copy; because
NX6==NZ6 it "looked" right and round-trip self-consistency passed, but it SCRAMBLED the
field whenever jp actually changed (the real cause of the Edit6 post-switch oscillation).

Correctness is cross-checked bit-for-bit against interp_checkpoint.py by
chain_code/tools/repartition_xcheck.py.

Migrates f00..f18 + rho AND, when accu_count>0, the 36 turbulence accumulators (see
EXPECTED_STAT_BASES) the SAME point-to-point way (they share rho's per-rank (NYD6,NZ6,NX6)
layout). Global CV ring-buffers (cv_*_history.bin) are copied verbatim; accu_count is carried
in metadata. ⇒ statistics integrity is preserved bit-for-bit across a jp change (the
time-average sum/accu_count at every physical point is unchanged) — no interpolation, ever.

When accu_count>0 the FULL 36-accumulator set is REQUIRED: if any accumulator (or any of its
per-rank files) is missing, the run is REFUSED rather than migrating a partial set that would
silently corrupt the running averages. Use --drop-stats to intentionally discard ALL statistics
(resets accu_count=0).
"""
import argparse, os, sys, shutil
import numpy as np

BFR = 3  # j/i ghost-buffer rows each side (matches interp_checkpoint.py:127)

# The solver writes EXACTLY these 36 turbulence accumulators when (accu_count>0 && TBSWITCH)
# — fileIO.h:299-317. They share rho's per-rank (NYD6,NZ6,NX6) layout, so they migrate
# point-to-point identically. The list is hard-coded (not merely auto-detected) so that an
# incomplete checkpoint missing even ONE accumulator is REFUSED rather than silently migrated
# as a partial set (which would corrupt the running averages — Codex audit finding B).
# NOTE the pressure terms use a capital P in the filename (sum_P/sum_PP/sum_Pu/sum_Pv/sum_Pw).
EXPECTED_STAT_BASES = [
    "sum_u", "sum_v", "sum_w",                                              # 3 first moments
    "sum_uu", "sum_uv", "sum_uw", "sum_vv", "sum_vw", "sum_ww",             # 6 second moments
    "sum_uuu", "sum_uuv", "sum_uuw", "sum_uvv", "sum_uvw", "sum_uww",       # 10 third moments
    "sum_vvv", "sum_vvw", "sum_vww", "sum_www",
    "sum_P", "sum_PP", "sum_Pu", "sum_Pv", "sum_Pw",                        # 5 pressure
    "sum_dudx2", "sum_dudy2", "sum_dudz2",                                  # 9 gradient^2
    "sum_dvdx2", "sum_dvdy2", "sum_dvdz2",
    "sum_dwdx2", "sum_dwdy2", "sum_dwdz2",
    "sum_ox", "sum_oy", "sum_oz",                                           # 3 vorticity
]
assert len(EXPECTED_STAT_BASES) == 36, "EXPECTED_STAT_BASES must list exactly 36 accumulators"


def parse_metadata(path):
    meta = {}
    with open(path) as f:
        for line in f:
            if '=' in line:
                k, v = line.strip().split('=', 1)
                meta[k] = v
    return meta


def read_rank(src_dir, fname, r, NYD6, NZ6, NX6):
    path = os.path.join(src_dir, "{}_{}.bin".format(fname, r))
    expected = NYD6 * NZ6 * NX6 * 8
    sz = os.path.getsize(path)
    if sz != expected:
        sys.exit("ERROR: {}: size {} != expected {} (NYD6*NZ6*NX6*8 = {}*{}*{}*8)".format(
            path, sz, expected, NYD6, NZ6, NX6))
    return np.fromfile(path, dtype=np.float64).reshape(NYD6, NZ6, NX6)


def enforce_periodic_physical_duplicates(g, NX, NY):
    # last physical node == first physical node (periodic i=spanwise, j=streamwise)
    g[BFR + NY - 1, :, :] = g[BFR, :, :]
    g[:, :, BFR + NX - 1] = g[:, :, BFR]


def fill_ghost(g, NX, NY, NZ):
    NX6, NY6, NZ6 = NX + 6, NY + 6, NZ + 6
    # X (spanwise) periodic
    g[:, :, 2] = g[:, :, NX6 - 5]; g[:, :, 1] = g[:, :, NX6 - 6]; g[:, :, 0] = g[:, :, NX6 - 7]
    g[:, :, NX6 - 3] = g[:, :, 4]; g[:, :, NX6 - 2] = g[:, :, 5]; g[:, :, NX6 - 1] = g[:, :, 6]
    # Z (wall-normal) constant copy from nearest wall (BC kernel overwrites on first step)
    g[:, 2, :] = g[:, 3, :]; g[:, 1, :] = g[:, 3, :]; g[:, 0, :] = g[:, 3, :]
    g[:, NZ6 - 3, :] = g[:, NZ6 - 4, :]; g[:, NZ6 - 2, :] = g[:, NZ6 - 4, :]; g[:, NZ6 - 1, :] = g[:, NZ6 - 4, :]
    # Y (streamwise) periodic
    g[2, :, :] = g[NY6 - 5, :, :]; g[1, :, :] = g[NY6 - 6, :, :]; g[0, :, :] = g[NY6 - 7, :, :]
    g[NY6 - 3, :, :] = g[4, :, :]; g[NY6 - 2, :, :] = g[5, :, :]; g[NY6 - 1, :, :] = g[6, :, :]


def stitch(src_dir, fname, old_jp, NX, NY, NZ):
    """Read old_jp per-rank slabs into global (NY6, NZ6, NX6). Only each rank's unique
    interior rows local[BFR:BFR+CHUNK] are authoritative (ghost/overlap rows are NOT copied)."""
    NX6, NY6, NZ6 = NX + 6, NY + 6, NZ + 6
    NYD6_old = (NY - 1) // old_jp + 7
    CHUNK = NYD6_old - 7
    g = np.zeros((NY6, NZ6, NX6), dtype=np.float64)
    for r in range(old_jp):
        local = read_rank(src_dir, fname, r, NYD6_old, NZ6, NX6)
        j0 = r * CHUNK
        g[j0 + BFR:j0 + BFR + CHUNK, :, :] = local[BFR:BFR + CHUNK, :, :]
    enforce_periodic_physical_duplicates(g, NX, NY)
    fill_ghost(g, NX, NY, NZ)
    return g


def split_write(g, fname, new_jp, NX, NY, NZ, dst_dir):
    """Split global (NY6,NZ6,NX6) into new_jp per-rank (NYD6_new,NZ6,NX6) files (axis 0)."""
    CHUNK_new = (NY - 1) // new_jp
    NYD6_new = CHUNK_new + 7
    for r in range(new_jp):
        j0 = r * CHUNK_new
        local = g[j0:j0 + NYD6_new, :, :].copy()
        local.tofile(os.path.join(dst_dir, "{}_{}.bin".format(fname, r)))


def main():
    p = argparse.ArgumentParser(description='Same-grid checkpoint repartitioner (point-to-point, no interpolation)')
    p.add_argument('--src', required=True, help='Source checkpoint dir')
    p.add_argument('--dst', required=True, help='Destination checkpoint dir (must be fresh)')
    p.add_argument('--new-jp', type=int, required=True)
    p.add_argument('--nq', type=int, default=19, help='# distribution functions (D3Q19=19)')
    p.add_argument('--drop-stats', action='store_true',
                   help='intentionally DROP turbulence statistics (reset accu_count=0) instead of migrating them')
    args = p.parse_args()

    meta = parse_metadata(os.path.join(args.src, 'metadata.dat'))
    old_jp = int(meta['mpi_rank_count'])
    NX6, NYD6_old, NZ6 = (int(x) for x in meta['grid_dims'].split(','))
    NX, NZ = NX6 - 6, NZ6 - 6
    NY = (NYD6_old - 7) * old_jp + 1           # (NY-1) = CHUNK_old * old_jp
    NY6 = NY + 6
    CHUNK_new = (NY - 1) // args.new_jp
    NYD6_new = CHUNK_new + 7

    if (NY - 1) % args.new_jp != 0:
        sys.exit("ERROR: (NY-1)={} not divisible by new_jp={}".format(NY - 1, args.new_jp))
    if CHUNK_new < 7:
        sys.exit("ERROR: (NY-1)/new_jp = {} < 7 (kernel slab floor)".format(CHUNK_new))

    # Turbulence statistics: the 36 sum_* accumulators have the SAME per-rank (NYD6,NZ6,NX6)
    # layout as rho, so they migrate POINT-TO-POINT exactly like rho — the time-accumulated sum
    # at every physical grid point is moved (not interpolated) to its new rank, and accu_count
    # (global, sample count) is carried verbatim in metadata. ⇒ the time-average sum/accu_count
    # at every physical point is bit-for-bit preserved across the jp change (statistics integrity).
    # CV history (cv_*_history.bin) is global/rank-independent → copied verbatim.
    accu = int((meta.get('accu_count', '0') or '0'))
    present = set(os.listdir(args.src))
    detected = sorted({f[:-len("_0.bin")] for f in present
                       if f.startswith('sum_') and f.endswith('_0.bin')})
    cv_files = sorted(f for f in present if f.startswith('cv_') and f.endswith('.bin'))

    if args.drop_stats:
        if detected or cv_files or accu > 0:
            print("  [--drop-stats] NOT migrating {} sum_* / {} cv_*; resetting accu_count {}->0".format(
                len(detected), len(cv_files), accu))
        stat_bases = []; cv_files = []; meta['accu_count'] = '0'
    elif accu > 0:
        # Statistics MUST survive intact. Assert the FULL 36-accumulator set is present and that
        # every accumulator has all old_jp per-rank files — refuse a partial set (would corrupt the
        # running averages by silently migrating a subset). [Codex audit finding B]
        missing = [b for b in EXPECTED_STAT_BASES if b not in detected]
        if missing:
            sys.exit("ERROR: accu_count={} > 0 but {}/36 expected sum_* accumulator(s) MISSING from src:\n"
                     "  {}\n  Migrating a partial statistics set would silently corrupt the running "
                     "averages. Refuse. (use --drop-stats to intentionally discard ALL statistics.)".format(
                         accu, len(missing), ', '.join(missing)))
        extra = [b for b in detected if b not in EXPECTED_STAT_BASES]
        if extra:
            sys.exit("ERROR: unexpected sum_* file(s) not in the known 36-accumulator set: {}\n"
                     "  Refusing rather than guessing their layout.".format(', '.join(extra)))
        for b in EXPECTED_STAT_BASES:
            miss_r = [r for r in range(old_jp) if "{}_{}.bin".format(b, r) not in present]
            if miss_r:
                sys.exit("ERROR: accumulator {} is missing rank file(s) {} (expected ranks 0..{}). "
                         "Incomplete checkpoint — refuse.".format(b, miss_r, old_jp - 1))
        if not cv_files:
            print("  WARNING: accu_count>0 but no cv_*_history.bin in src — CV ring buffer will be empty "
                  "(convergence monitor re-fills over a fresh window; mean-field statistics unaffected).")
        stat_bases = EXPECTED_STAT_BASES
    else:
        # accu_count==0 (spin-up): solver wrote no accumulators. Migrate any strays for completeness.
        stat_bases = detected

    print("=== Same-grid repartition (point-to-point): jp {} -> {} ===".format(old_jp, args.new_jp))
    print("  Grid: NX={} NY={} NZ={}  (NX6={} NZ6={} NY6={})".format(NX, NY, NZ, NX6, NZ6, NY6))
    print("  Old: NYD6={} chunk={}   New: NYD6={} chunk={}".format(NYD6_old, NYD6_old - 7, NYD6_new, CHUNK_new))
    print("  Axis order: (NYD6, NZ6, NX6)  [matches solver idx=j*NX6*NZ6+k*NX6+i]")
    print("  Per-field global memory: {:.0f} MB".format(NY6 * NZ6 * NX6 * 8 / 1e6))

    os.makedirs(args.dst, exist_ok=True)

    for q in range(args.nq):
        fname = "f{:02d}".format(q)
        print("  [{}/{}] {}: stitch {} -> split {} ... ".format(q + 1, args.nq, fname, old_jp, args.new_jp), end='', flush=True)
        g = stitch(args.src, fname, old_jp, NX, NY, NZ)
        split_write(g, fname, args.new_jp, NX, NY, NZ, args.dst)
        del g
        print("done")

    print("  [rho] stitch {} -> split {} ... ".format(old_jp, args.new_jp), end='', flush=True)
    g = stitch(args.src, "rho", old_jp, NX, NY, NZ)
    split_write(g, "rho", args.new_jp, NX, NY, NZ, args.dst)
    del g
    print("done")

    # Turbulence statistics: identical point-to-point treatment as rho (same layout, no interpolation).
    for i, base in enumerate(stat_bases):
        print("  [stat {}/{}] {}: stitch {} -> split {} ... ".format(i + 1, len(stat_bases), base, old_jp, args.new_jp), end='', flush=True)
        g = stitch(args.src, base, old_jp, NX, NY, NZ)
        split_write(g, base, args.new_jp, NX, NY, NZ, args.dst)
        del g
        print("done")
    # CV history ring-buffers are global (rank-independent) → copy verbatim.
    for cv in cv_files:
        shutil.copy2(os.path.join(args.src, cv), os.path.join(args.dst, cv))
    if stat_bases or cv_files:
        print("  Statistics: migrated {} sum_* (point-to-point) + copied {} cv_*; accu_count={} preserved".format(
            len(stat_bases), len(cv_files), meta.get('accu_count', '0')))

    # New metadata: only mpi_rank_count + grid_dims change; everything else (step/FTT/dt/Force/accu) verbatim
    meta['mpi_rank_count'] = str(args.new_jp)
    meta['grid_dims'] = "{},{},{}".format(NX6, NYD6_new, NZ6)
    with open(os.path.join(args.dst, 'metadata.dat'), 'w') as f:
        for k, v in meta.items():
            f.write("{}={}\n".format(k, v))

    # Verify file count (ignore dotfiles / stray .part)
    actual = len([f for f in os.listdir(args.dst) if not f.startswith('.')])
    expected = (args.nq * args.new_jp + args.new_jp            # f + rho
                + len(stat_bases) * args.new_jp                # 36 sum_* per rank (if any)
                + len(cv_files) + 1)                           # global cv_* + metadata
    print("\n  Output: {}".format(args.dst))
    print("  Files: {} (expected {})  metadata: grid_dims={},{},{} mpi_rank_count={}".format(
        actual, expected, NX6, NYD6_new, NZ6, args.new_jp))
    if actual == expected:
        print("  OK Repartition complete (point-to-point, field preserved)")
    else:
        print("  ERROR file count mismatch")
        sys.exit(1)


if __name__ == '__main__':
    main()
