#!/usr/bin/env python3
"""Lightweight same-grid checkpoint repartitioner (jp change only).

Processes one f-field at a time to keep peak memory low (~600 MB instead of ~6 GB).
No interpolation, no velocity projection — pure data reshuffling.
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
    """Read per-rank files and stitch into global array."""
    NY6_global = (NYD6_old - 7) * old_jp + 7
    global_arr = np.empty((NX6, NY6_global, NZ6), dtype=np.float64)
    chunk = NYD6_old - 7
    for r in range(old_jp):
        fpath = os.path.join(src_dir, f"{fname_base}_{r}.bin")
        local = np.fromfile(fpath, dtype=np.float64).reshape((NX6, NYD6_old, NZ6))
        j_start = r * chunk
        if r == 0:
            global_arr[:, :NYD6_old, :] = local
        elif r == old_jp - 1:
            global_arr[:, j_start:, :] = local
        else:
            global_arr[:, j_start:j_start + NYD6_old, :] = local
    return global_arr

def split_field(global_arr, new_jp, NX6, NZ6, dst_dir, fname_base):
    """Split global array into new_jp rank files."""
    NY6_global = global_arr.shape[1]
    chunk_new = (NY6_global - 7) // new_jp
    NYD6_new = chunk_new + 7
    for r in range(new_jp):
        j_start = r * chunk_new
        local = global_arr[:, j_start:j_start + NYD6_new, :].copy()
        fpath = os.path.join(dst_dir, f"{fname_base}_{r}.bin")
        local.tofile(fpath)

def main():
    p = argparse.ArgumentParser(description='Same-grid checkpoint repartitioner')
    p.add_argument('--src', required=True, help='Source checkpoint dir (e.g. restart/checkpoint/step_18001)')
    p.add_argument('--dst', required=True, help='Destination checkpoint dir')
    p.add_argument('--new-jp', type=int, required=True, help='New GPU count')
    p.add_argument('--nq', type=int, default=19, help='Number of distribution functions (D3Q19=19)')
    args = p.parse_args()

    meta = parse_metadata(os.path.join(args.src, 'metadata.dat'))
    old_jp = int(meta['mpi_rank_count'])
    dims = meta['grid_dims'].split(',')
    NX6, NYD6_old, NZ6 = int(dims[0]), int(dims[1]), int(dims[2])

    NY_global = (NYD6_old - 7) * old_jp + 1
    chunk_new = (NY_global - 1) // args.new_jp
    NYD6_new = chunk_new + 7

    if (NY_global - 1) % args.new_jp != 0:
        print(f"ERROR: (NY-1)={NY_global-1} not divisible by new_jp={args.new_jp}")
        sys.exit(1)

    print(f"=== Same-grid repartition: jp {old_jp} → {args.new_jp} ===")
    print(f"  Grid: NX6={NX6} NY={NY_global} NZ6={NZ6}")
    print(f"  Old: NYD6={NYD6_old}, chunk={NYD6_old-7}")
    print(f"  New: NYD6={NYD6_new}, chunk={chunk_new}")
    print(f"  Per-field memory: {NX6 * (chunk_new * args.new_jp + 7) * NZ6 * 8 / 1e6:.0f} MB")
    print()

    os.makedirs(args.dst, exist_ok=True)

    for q in range(args.nq):
        fname = f"f{q:02d}"
        print(f"  [{q+1}/{args.nq}] {fname}: stitch {old_jp} ranks → split {args.new_jp} ranks ... ", end='', flush=True)
        glob = stitch_field(args.src, fname, old_jp, NX6, NYD6_old, NZ6)
        split_field(glob, args.new_jp, NX6, NZ6, args.dst, fname)
        del glob
        print("done")

    # rho field
    print(f"  [rho] stitch {old_jp} → split {args.new_jp} ... ", end='', flush=True)
    glob = stitch_field(args.src, "rho", old_jp, NX6, NYD6_old, NZ6)
    split_field(glob, args.new_jp, NX6, NZ6, args.dst, "rho")
    del glob
    print("done")

    # Write new metadata
    meta['mpi_rank_count'] = str(args.new_jp)
    meta['grid_dims'] = f"{NX6},{NYD6_new},{NZ6}"
    meta_path = os.path.join(args.dst, 'metadata.dat')
    with open(meta_path, 'w') as f:
        for k, v in meta.items():
            f.write(f"{k}={v}\n")

    # Verify file count
    expected = args.nq * args.new_jp + args.new_jp + 1  # f-files + rho + metadata
    actual = len(os.listdir(args.dst))
    print(f"\n  Output: {args.dst}")
    print(f"  Files: {actual} (expected {expected})")
    print(f"  New metadata: grid_dims={NX6},{NYD6_new},{NZ6} mpi_rank_count={args.new_jp}")

    if actual == expected:
        print("  ✓ Repartition complete")
    else:
        print("  ✗ File count mismatch!")
        sys.exit(1)

if __name__ == '__main__':
    main()
