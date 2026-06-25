#!/usr/bin/env python3
"""Same-grid checkpoint repartitioner (jp change only) — STATS-PRESERVING, layout-correct.

Reconstructs the GLOBAL periodic streamwise field (NY-1 unique points) from per-rank
slabs and re-splits it for a new jp. Pure data reshuffle — no interpolation — so the
OWNED flow field is bit-exact; ghost/overlap layers are refilled from periodic neighbours.

MEMORY LAYOUT (authoritative, from the solver index macro used everywhere —
evolution.h/statistics.h/fileIO.h/initialization.h):
        index = j*NX6*NZ6 + k*NX6 + i
    ⇒ per-rank array is [j][k][i]  →  numpy shape (NYD6, NZ6, NX6), STREAMWISE j = axis 0.
        i (axis 2) = spanwise NX6,  k (axis 1) = wall-normal NZ6,  j (axis 0) = streamwise NYD6.

SLAB LAYOUT on the partitioned streamwise (eta/j) axis (variables.h:87 "[3 ghost|N|3 ghost]"
+ fileIO.h:1066 pack convention):
        j_local: 0 1 2 | 3 .. 3+N-1 | 3+N | 3+N+1 3+N+2 3+N+3
                 bot gh | N owned    | ovlp|      top ghost
        NYD6 = N + 7,  N = (NY-1)/jp,  unique owned = local[3:3+N, :, :]  (axis 0)

BUG HISTORY (fixed 2026-06-02): the original version reshaped (NX6,NYD6,NZ6) and sliced
axis 1, and treated owned = local[0:N]. Both were wrong: it operated in an [i,j,k] frame
with no bottom-ghost offset, producing a field self-consistent in that wrong frame but
perturbed by ~3e-4 at every rank boundary in the solver's true [j,k,i] frame → post-switch
flow oscillation. Verified via empirical compare to the pristine jp=64 backup + the index
macro. Always run chain_code/tools/roundtrip_verify.sh before deploying.
"""
import argparse, os, sys, shutil
import numpy as np


def parse_metadata(path):
    meta = {}
    with open(path) as f:
        for line in f:
            if '=' in line:
                k, v = line.strip().split('=', 1)
                meta[k] = v
    return meta


def stitch_field(src_dir, fname_base, old_jp, NX6, NYD6_old, NZ6):
    """Per-rank slabs -> global unique periodic field (shape (NY-1, NZ6, NX6), [j,k,i]).
    Takes only the N owned rows (axis-0 j_local 3..3+N-1) from each rank."""
    N = NYD6_old - 7
    NYm1 = N * old_jp                       # NY-1 unique periodic streamwise points
    g = np.empty((NYm1, NZ6, NX6), dtype=np.float64)
    for r in range(old_jp):
        local = np.fromfile(os.path.join(src_dir, f"{fname_base}_{r}.bin"),
                            dtype=np.float64).reshape((NYD6_old, NZ6, NX6))
        g[r * N:(r + 1) * N, :, :] = local[3:3 + N, :, :]
        del local
    return g


def split_field(g, new_jp, NX6, NZ6, dst_dir, fname_base):
    """Global unique periodic field -> per-rank slabs, refilling bottom-ghost / overlap /
    top-ghost from the periodic neighbours (consistent checkpoint)."""
    NYm1 = g.shape[0]
    N = NYm1 // new_jp
    NYD6_new = N + 7
    for r in range(new_jp):
        base = r * N
        local = np.empty((NYD6_new, NZ6, NX6), dtype=np.float64)
        local[3:3 + N, :, :] = g[base:base + N, :, :]                  # owned j_local 3..N+2
        for m in range(3):                                            # bottom ghost j_local 0,1,2
            local[m, :, :] = g[(base - 3 + m) % NYm1, :, :]
        for m in range(4):                                            # overlap + top ghost j_local N+3..N+6
            local[3 + N + m, :, :] = g[(base + N + m) % NYm1, :, :]
        local.tofile(os.path.join(dst_dir, f"{fname_base}_{r}.bin"))
        del local


def main():
    p = argparse.ArgumentParser(description='Same-grid checkpoint repartitioner (stats-preserving, layout-correct)')
    p.add_argument('--src', required=True)
    p.add_argument('--dst', required=True)
    p.add_argument('--new-jp', type=int, required=True)
    p.add_argument('--nq', type=int, default=19)
    args = p.parse_args()

    meta = parse_metadata(os.path.join(args.src, 'metadata.dat'))
    old_jp = int(meta['mpi_rank_count'])
    dims = meta['grid_dims'].split(',')
    NX6, NYD6_old, NZ6 = int(dims[0]), int(dims[1]), int(dims[2])

    N_old = NYD6_old - 7
    NYm1 = N_old * old_jp                   # NY-1
    if NYm1 % args.new_jp != 0:
        print(f"ERROR: (NY-1)={NYm1} not divisible by new_jp={args.new_jp}")
        sys.exit(1)
    N_new = NYm1 // args.new_jp
    NYD6_new = N_new + 7

    print(f"=== Same-grid repartition (stats-preserving, [j,k,i]): jp {old_jp} → {args.new_jp} ===")
    print(f"  Grid: NX6={NX6} NY-1={NYm1} NZ6={NZ6}  (periodic streamwise; j=axis0)")
    print(f"  Old: NYD6={NYD6_old} owned/rank={N_old}    New: NYD6={NYD6_new} owned/rank={N_new}")
    print(f"  Per-field global: {NYm1 * NZ6 * NX6 * 8 / 1e6:.0f} MB")
    print()

    os.makedirs(args.dst, exist_ok=True)

    for q in range(args.nq):
        fname = f"f{q:02d}"
        print(f"  [{q+1}/{args.nq}] {fname}: stitch {old_jp} → split {args.new_jp} ... ", end='', flush=True)
        g = stitch_field(args.src, fname, old_jp, NX6, NYD6_old, NZ6)
        split_field(g, args.new_jp, NX6, NZ6, args.dst, fname)
        del g
        print("done")

    print(f"  [rho] stitch {old_jp} → split {args.new_jp} ... ", end='', flush=True)
    g = stitch_field(args.src, "rho", old_jp, NX6, NYD6_old, NZ6)
    split_field(g, args.new_jp, NX6, NZ6, args.dst, "rho")
    del g
    print("done")

    # --- STATS-PRESERVING: 36 sum_* accumulators share the SAME [j,k,i] layout. ---
    accu = int((meta.get('accu_count', '0') or '0'))
    cv_count = int((meta.get('cv_count', '0') or '0'))
    # Canonical statistics field set (fileIO.h SaveBinaryCheckpoint: 33 RS + 3 vorticity = 36)
    CANONICAL_STATS = {
        'sum_u','sum_v','sum_w', 'sum_uu','sum_uv','sum_uw','sum_vv','sum_vw','sum_ww',
        'sum_uuu','sum_uuv','sum_uuw','sum_uvv','sum_uvw','sum_uww','sum_vvv','sum_vvw','sum_vww','sum_www',
        'sum_P','sum_PP','sum_Pu','sum_Pv','sum_Pw',
        'sum_dudx2','sum_dudy2','sum_dudz2','sum_dvdx2','sum_dvdy2','sum_dvdz2','sum_dwdx2','sum_dwdy2','sum_dwdz2',
        'sum_ox','sum_oy','sum_oz'}
    stat_bases = sorted({f[:-6] for f in os.listdir(args.src)
                         if f.startswith('sum_') and f.endswith('_0.bin')})
    # [Codex P2 fix, hardened] if accu>0 the source MUST have EXACTLY the 36 canonical stat fields
    # (exact NAME match, not just count) — refuse stray/missing names so stats can't be silently corrupted.
    if accu > 0 and set(stat_bases) != CANONICAL_STATS:
        miss = sorted(CANONICAL_STATS - set(stat_bases)); extra = sorted(set(stat_bases) - CANONICAL_STATS)
        print(f"  ✗ ERROR: accu_count={accu} stat-field set mismatch — missing={miss} extra={extra}; "
              f"refusing (incomplete/wrong checkpoint, stats would be corrupted)")
        sys.exit(1)
    if stat_bases and accu == 0:
        print(f"  WARN: {len(stat_bases)} sum_* present but accu_count=0 (moving anyway)")
    for i, base in enumerate(stat_bases):
        print(f"  [stat {i+1}/{len(stat_bases)}] {base}: stitch → split ... ", end='', flush=True)
        g = stitch_field(args.src, base, old_jp, NX6, NYD6_old, NZ6)
        split_field(g, args.new_jp, NX6, NZ6, args.dst, base)
        del g
        print("done")

    # --- global CV ring-buffer history (rank-independent; copy verbatim) ---
    CANONICAL_CV = {'cv_uu_history.bin', 'cv_k_history.bin', 'cv_ftt_history.bin'}
    cv_files = sorted(f for f in os.listdir(args.src)
                      if f.startswith('cv_') and f.endswith('_history.bin'))
    # [Codex P2 fix, hardened] if cv_count>0 the source MUST have EXACTLY the 3 canonical cv files
    # (exact NAME match) + each non-empty (a truncated/empty cv file would be dropped on load).
    if cv_count > 0:
        if set(cv_files) != CANONICAL_CV:
            print(f"  ✗ ERROR: cv_count={cv_count} cv-file set mismatch — have={cv_files} "
                  f"expected={sorted(CANONICAL_CV)}; refusing"); sys.exit(1)
        for cv in cv_files:
            if os.path.getsize(os.path.join(args.src, cv)) == 0:
                print(f"  ✗ ERROR: cv file {cv} is empty/truncated; refusing"); sys.exit(1)
    for cv in cv_files:
        shutil.copy2(os.path.join(args.src, cv), os.path.join(args.dst, cv))
    if cv_files:
        print(f"  [cv] copied {len(cv_files)} global history file(s)")

    # metadata (accu_count / cv_count carried verbatim)
    meta['mpi_rank_count'] = str(args.new_jp)
    meta['grid_dims'] = f"{NX6},{NYD6_new},{NZ6}"
    with open(os.path.join(args.dst, 'metadata.dat'), 'w') as f:
        for k, v in meta.items():
            f.write(f"{k}={v}\n")

    expected = (args.nq * args.new_jp + args.new_jp
                + len(stat_bases) * args.new_jp + len(cv_files) + 1)
    actual = len(os.listdir(args.dst))
    print(f"\n  Output: {args.dst}")
    print(f"  Files: {actual} (expected {expected})")
    print(f"  metadata: grid_dims={NX6},{NYD6_new},{NZ6} mpi_rank_count={args.new_jp} "
          f"accu_count={accu} (stats {'PRESERVED' if stat_bases else 'none'})")
    if actual == expected:
        print("  ✓ Repartition complete (stats-preserving, ghost-consistent)")
    else:
        print("  ✗ File count mismatch!"); sys.exit(1)


if __name__ == '__main__':
    main()
