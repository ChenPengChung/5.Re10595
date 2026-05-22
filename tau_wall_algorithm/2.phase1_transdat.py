# -*- coding: utf-8 -*-
"""
2.phase1_transdat.py

Convert PH 2D corner-grid Tecplot dat from local (x_corner, y_corner) in
physical units to project NEW convention (y, z) in h-normalized units,
with Tecplot index labels matching the project (i, j, k) computational
space convention:

  input (file labels)         output (file labels, project convention)
  -----------------------     ------------------------------------------
  VARIABLES "x corner" "y corner"   ->   VARIABLES "y" "z"
  values in metres                  ->   values in h units
  I=129  (sweeps stream-wise)       ->   J=129  (project j = stream)
  J=257  (sweeps wall-normal)       ->   K=257  (project k = wall-normal)
  K=1                               ->   I=1    (project i = span, degenerate 2D)
"""
from __future__ import annotations
import argparse, os, re, sys
import numpy as np
from typing import List, Tuple, Optional

# ---- I/O directory bootstrap (matches the Input/Output/Reference layout) -
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "Reference"))
INPUT_DIR  = os.path.join(_HERE, "Input")
OUTPUT_DIR = os.path.join(_HERE, "Output")
os.makedirs(OUTPUT_DIR, exist_ok=True)
# --------------------------------------------------------------------------

from phase1_common import find_unique_matching
from phase1_common import (
    auto_detect_variables_h,
    find_const,
    parse_header_constants,
)


_RAW_MESH_RE = re.compile(
    r"^(?:\d+\.)?I\d+_J\d+.*g\d+(?:\.\d+)?.*a\d+(?:\.\d+)?.*\.dat$",
    re.IGNORECASE)


def auto_detect_dat(folder: str = ".") -> str:
    """Find the unique INPUT .dat file in *folder*; error otherwise.

    Uses the raw mesh filename contract rather than "any .dat", so metadata
    such as Re*_metadata.dat does not become an accidental candidate.
    """
    return find_unique_matching(folder, "*.dat", _RAW_MESH_RE,
                                label="raw mesh .dat file")


def parse_g_a(name: str) -> Tuple[str, str]:
    """Extract g and a tokens from a filename like ``...g2.0_a0.5...``."""
    g = re.search(r"g(\d+(?:\.\d+)?)", name)
    a = re.search(r"a(\d+(?:\.\d+)?)", name)
    if not g or not a:
        print(f"[error] cannot find g<value> / a<value> in input filename: "
              f"{name}", file=sys.stderr)
        sys.exit(1)
    return g.group(1), a.group(1)


def build_output_name(in_path: str, I_in: int, J_in: int) -> str:
    """`<dir>/1.I257_J129_g2.0_a0.5.dat` -> `<dir>/2.j257_k129_g2.0_a0.5.dat`.

    Project convention: j = stream (= input I), k = wall-normal (= input J)."""
    folder = os.path.dirname(in_path) or "."
    g, a = parse_g_a(os.path.basename(in_path))
    return os.path.join(folder, f"2.j{I_in}_k{J_in}_g{g}_a{a}.dat")


def parse_tecplot_dat(path):
    with open(path) as f:
        raw = f.readlines()
    header, data_start = [], 0
    for k, ln in enumerate(raw):
        toks = ln.split()
        if len(toks) == 2:
            try:
                float(toks[0]); float(toks[1])
                data_start = k; break
            except ValueError:
                pass
        header.append(ln.rstrip("\r\n"))
    H = " ".join(header)
    I = int(re.search(r"I\s*=\s*(\d+)", H).group(1))
    J = int(re.search(r"J\s*=\s*(\d+)", H).group(1))
    data = np.loadtxt(raw[data_start:])
    if data.shape != (I*J, 2):
        raise ValueError(f"data shape {data.shape} != expected ({I*J}, 2)")
    arr = data.reshape(J, I, 2)
    return header, I, J, arr


def infer_hill_height(arr, stream_len: float, normal_len: float):
    h_x = arr[..., 0].max() / stream_len
    h_y = arr[..., 1].max() / normal_len
    print(f"  h from x_max/{stream_len}     = {h_x:.8f}")
    print(f"  h from y_max/{normal_len}     = {h_y:.8f}")
    rel_err = abs(h_x - h_y) / max(abs(h_x), abs(h_y))
    if rel_err > 1e-4:
        print(f"  [WARN] h_x and h_y disagree by {rel_err*100:.4f}% "
              f"-- mesh geometry may not match stream_len={stream_len} / "
              f"normal_len={normal_len}; "
              f"using h_x (stream-derived).  If the mesh truly has different "
              f"proportions, pass --stream-len/--normal-len or update "
              f"variables.h.", file=sys.stderr)
    return h_x


def transform(arr, h):
    out = np.empty_like(arr)
    out[..., 0] = arr[..., 0] / h    # y stream, h-normalized
    out[..., 1] = arr[..., 1] / h    # z normal, h-normalized
    return out


def sanity(out, I_in, J_in, stream_len: float, normal_len: float):
    y = out[..., 0]; z = out[..., 1]
    print(f"  y(stream) shape={y.shape}  range [{y.min():.6f}, {y.max():.6f}]")
    print(f"  z(normal) shape={z.shape}  range [{z.min():.6f}, {z.max():.6f}]")
    print(f"  corner check (y, z):")
    print(f"    bottom-left (k=0,    j=0   ) = ({y[0,0]:.4f}, {z[0,0]:.4f})   expect (0, 1)")
    print(f"    bottom-right(k=0,    j={I_in-1:>3}) = ({y[0,-1]:.4f}, {z[0,-1]:.4f})   expect ({stream_len:g}, 1)")
    print(f"    top-left    (k={J_in-1:>3}, j=0   ) = ({y[-1,0]:.4f}, {z[-1,0]:.4f})   expect (0, {normal_len:g})")
    print(f"    top-right   (k={J_in-1:>3}, j={I_in-1:>3}) = ({y[-1,-1]:.4f}, {z[-1,-1]:.4f})   expect ({stream_len:g}, {normal_len:g})")
    assert np.allclose(z[-1], z[-1, 0], atol=1e-5), "top wall not flat"
    assert np.isclose(z[0, 0], 1.0, atol=1e-3), "first lower-wall point not at hill peak"
    assert np.isclose(y[0, -1], stream_len, atol=1e-3)
    assert np.isclose(z[-1, 0], normal_len, atol=2e-3)
    print("  [OK] sanity passed")


def resolve_geometry_constants(args):
    var_h = args.variables_h or auto_detect_variables_h(
        os.path.join(_HERE, "Input"))
    consts = parse_header_constants(var_h) if var_h else {}

    if args.stream_len is not None:
        stream_len, stream_src = args.stream_len, "CLI --stream-len"
    else:
        stream_len = find_const(consts, ["LY"], var_h or "variables.h")
        stream_src = f"file {var_h}"

    if args.normal_len is not None:
        normal_len, normal_src = args.normal_len, "CLI --normal-len"
    else:
        normal_len = find_const(consts, ["LZ"], var_h or "variables.h")
        normal_src = f"file {var_h}"

    return stream_len, normal_len, stream_src, normal_src


def write_tecplot(path, I_in, J_in, out, g, a):
    """
    File-level (i,j,k) labels are remapped to match project convention:
       project i (span)        -> Tecplot I = 1   (degenerate, 2D)
       project j (stream)      -> Tecplot J = old I  (= I_in)
       project k (wall-normal) -> Tecplot K = old J  (= J_in)
    Data ordering in POINT format is I-fast, J-mid, K-slow.  With I=1
    the inner loop is degenerate, so the flat sequence is
       for k in 0..K-1:
           for j in 0..J-1:
               write y[k,j] z[k,j]
    which equals the input's flat ordering (outer J=normal, inner
    I=stream) byte-for-byte.  Only the header changes.
    """
    J_out = I_in    # stream      (project j)
    K_out = J_in    # wall-normal (project k)
    with open(path, "w") as f:
        f.write('TITLE     = "Periodic hill (h-normalized, project (i,j,k) layout)"\n')
        f.write('VARIABLES = "y" "z"\n')
        f.write('ZONE T="I1_J%d_K%d_g%s_a%s"\n' % (J_out, K_out, g, a))
        f.write(' I=1, J=%d, K=%d, F=POINT\n' % (J_out, K_out))
        f.write('DT=(SINGLE SINGLE )\n')
        for k in range(K_out):
            for j in range(J_out):
                y = out[k, j, 0]
                z = out[k, j, 1]
                f.write(' % .9E % .9E\n' % (y, z))


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("input",  nargs="?", default=None,
                   help="input .dat file (default: auto-detect unique .dat in cwd)")
    p.add_argument("-o", "--output", default=None,
                   help="output .dat file (default: 2.j<I>_k<J>_g<g>_a<a>.dat)")
    p.add_argument("--variables-h", default=None,
                   help="Input/variables.h (default: auto-detect)")
    p.add_argument("--stream-len", type=float, default=None,
                   help="streamwise length in h units (default: LY from variables.h)")
    p.add_argument("--normal-len", type=float, default=None,
                   help="wall-normal length in h units (default: LZ from variables.h)")
    args = p.parse_args(argv)
    in_path = args.input or auto_detect_dat(INPUT_DIR)
    print(f"input : {in_path}")

    stream_len, normal_len, stream_src, normal_src = resolve_geometry_constants(args)
    print(f"LY stream length = {stream_len:g}  (source: {stream_src})")
    print(f"LZ normal length = {normal_len:g}  (source: {normal_src})")

    print("\n[1] parse ..."); header, I, J, arr = parse_tecplot_dat(in_path)
    print(f"  I={I}, J={J}  shape={arr.shape}")
    print(f"  x_corner range [{arr[...,0].min():.6e}, {arr[...,0].max():.6f}]  (m, stream)")
    print(f"  y_corner range [{arr[...,1].min():.6e}, {arr[...,1].max():.6f}]  (m, normal)")

    g, a = parse_g_a(os.path.basename(in_path))
    out_path = args.output or os.path.join(
        OUTPUT_DIR, os.path.basename(build_output_name(in_path, I, J)))
    print(f"output: {out_path}")
    print(f"  g={g}, a={a}  (parsed from input filename)")

    print("\n[2] infer h ..."); h = infer_hill_height(arr, stream_len, normal_len); print(f"  using h = {h:.8f}")
    print("\n[3] transform ..."); out = transform(arr, h)
    print("\n[4] sanity ..."); sanity(out, I, J, stream_len, normal_len)
    print(f"\n[5] write ..."); write_tecplot(out_path, I, J, out, g, a)
    print(f"  wrote {os.path.getsize(out_path):,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
