# -*- coding: utf-8 -*-
"""
1.phase1_transvtk.py
========================

Convert variable names in a legacy ASCII VTK file from the ERCOFTAC
directional convention to the project convention used in this PeriodicHill /
GILBM project.

ERCOFTAC convention (raw VTK variable layout):
    u, U = stream-wise
    v, V = wall-normal
    w, W = span-wise

NEW convention (project specification):
    x | u | U = span-wise
    y | v | V = stream-wise
    z | w | W = wall-normal

The renaming applies a cyclic permutation: u→v, v→w, w→u.
Coordinate axes (POINTS) and the VECTORS data are already in the NEW
convention -- only dataset *names* are rewritten.

Two operating modes:
  * inplace  (default): copy-then-patch, requires same-length renames.
              Fast; only writes a few header bytes after the copy.
  * stream  : read/write line-by-line in binary mode. Allows length-changing
              renames.

Usage:
    python rename_vtk_convention.py
    python rename_vtk_convention.py input.vtk -o output.vtk
    python rename_vtk_convention.py --dry-run
    python rename_vtk_convention.py --mode stream
"""

from __future__ import annotations
import argparse, os, re, shutil, sys
from typing import Dict, List, Tuple, Optional

# ---- I/O directory bootstrap (matches the Input/Output/Reference layout) -
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "Reference"))
INPUT_DIR  = os.path.join(_HERE, "Input")
OUTPUT_DIR = os.path.join(_HERE, "Output")
os.makedirs(OUTPUT_DIR, exist_ok=True)
# --------------------------------------------------------------------------

from phase1_common import find_unique_excluding


_OUTPUT_RE = re.compile(r"^1\..+_v2\.vtk$", re.IGNORECASE)


def auto_detect_vtk(folder: str = ".") -> str:
    """Find the unique INPUT .vtk file in *folder*; error otherwise.

    Files matching the output pattern ``1.*_v2.vtk`` (produced by this
    script) are excluded so that re-running in the same folder still works.
    """
    return find_unique_excluding(folder, "*.vtk", _OUTPUT_RE,
                                 ext_label=".vtk file")


def build_output_name(in_path: str) -> str:
    """`<dir>/<raw>.vtk` -> `<dir>/1.<raw>_v2.vtk`.

    Rule: strip leading `\\d+\\.` from the stem, force `1.` prefix, then add
    `_v2` before the extension."""
    folder = os.path.dirname(in_path) or "."
    stem, ext = os.path.splitext(os.path.basename(in_path))
    stem = re.sub(r"^\d+\.", "", stem)        # remove any leading "N."
    return os.path.join(folder, f"1.{stem}_v2{ext}")


# ---- Renaming table: ERCOFTAC -> project (cyclic: u→v, v→w, w→u) ---------
RENAME: Dict[str, str] = {
    "u_inst":  "v_inst",
    "v_inst":  "w_inst",
    "w_inst":  "u_inst",
    "omega_u": "omega_v",
    "omega_v": "omega_w",
    "omega_w": "omega_u",
    "U_mean":  "V_mean",
    "V_mean":  "W_mean",
    "W_mean":  "U_mean",
    "uu_RS":   "vv_RS",
    "vv_RS":   "ww_RS",
    "ww_RS":   "uu_RS",
    "uv_RS":   "vw_RS",
    "vu_RS":   "wv_RS",
    "uw_RS":   "vu_RS",
    "wu_RS":   "uv_RS",
    "vw_RS":   "wu_RS",
    "wv_RS":   "uw_RS",
}

_KEYWORDS = (
    b"SCALARS", b"VECTORS", b"TENSORS", b"TENSORS6",
    b"NORMALS", b"COLOR_SCALARS", b"TEXTURE_COORDINATES",
)
_HEADER_RE = re.compile(
    rb"(?m)^(" + b"|".join(_KEYWORDS) + rb")\s+(\S+)"
)


def _scan_headers(path: str):
    """Yield (kind:str, name:str, name_byte_offset:int) for every dataset
    declaration in the file."""
    CHUNK = 64 * 1024 * 1024
    OVERLAP = 256
    seen = set()
    with open(path, "rb") as f:
        leftover = b""
        buf_start = 0
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            buf = leftover + chunk
            for m in _HEADER_RE.finditer(buf):
                name_off = buf_start + m.start(2)
                if name_off in seen:
                    continue
                # Skip matches anchored inside the previous overlap region
                # (already reported in the previous iteration).
                if buf_start != 0 and m.start() < OVERLAP:
                    continue
                seen.add(name_off)
                yield (
                    m.group(1).decode("ascii"),
                    m.group(2).decode("ascii", errors="replace"),
                    name_off,
                )
            if len(buf) > OVERLAP:
                leftover = buf[-OVERLAP:]
                buf_start += len(buf) - OVERLAP
            else:
                leftover = buf


def rename_inplace(input_path: str, output_path: str,
                   rename_map: Dict[str, str]) -> List[Tuple[str, str, str, int]]:
    """Fast path: copy file then patch the name bytes in place."""
    for old, new in rename_map.items():
        if old != new and len(old) != len(new):
            raise ValueError(
                f"In-place mode requires equal byte length: "
                f"'{old}' ({len(old)}) -> '{new}' ({len(new)})"
            )
    print("  [1/3] copying input -> output ...")
    shutil.copyfile(input_path, output_path)
    print(f"        copied {os.path.getsize(output_path):,} bytes")

    print("  [2/3] scanning for dataset declarations ...")
    plan: List[Tuple[str, str, str, int]] = []
    for kind, name, off in _scan_headers(output_path):
        if name in rename_map and rename_map[name] != name:
            plan.append((kind, name, rename_map[name], off))
    print(f"        found {len(plan)} renames to apply")

    print("  [3/3] patching name bytes in place ...")
    with open(output_path, "r+b") as f:
        for kind, old, new, off in plan:
            f.seek(off)
            f.write(new.encode("ascii"))
    return plan


def rename_streaming(input_path: str, output_path: str,
                     rename_map: Dict[str, str]) -> List[Tuple[str, str, str, int]]:
    """Generic path: stream input -> output line by line, in binary."""
    log: List[Tuple[str, str, str, int]] = []
    line_no = 0
    with open(input_path, "rb") as fin, open(output_path, "wb") as fout:
        for raw in fin:
            line_no += 1
            stripped = raw.rstrip(b"\r\n")
            is_decl = False
            for kw in _KEYWORDS:
                if stripped.startswith(kw + b" "):
                    is_decl = True
                    break
            if is_decl:
                parts = stripped.split(None, 2)
                if len(parts) >= 2:
                    name = parts[1].decode("ascii", errors="replace")
                    if name in rename_map and rename_map[name] != name:
                        new_name = rename_map[name].encode("ascii")
                        kind = parts[0].decode("ascii")
                        rest = b" " + parts[2] if len(parts) >= 3 else b""
                        out_line = parts[0] + b" " + new_name + rest
                        if raw.endswith(b"\r\n"):
                            out_line += b"\r\n"
                        elif raw.endswith(b"\n"):
                            out_line += b"\n"
                        fout.write(out_line)
                        log.append((kind, name, rename_map[name], line_no))
                        continue
            fout.write(raw)
    return log


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description=("Rename VTK scalar/vector dataset names from the OLD "
                     "(u=streamwise) to the NEW (u=spanwise, v=streamwise, "
                     "w=wall-normal) directional convention.")
    )
    p.add_argument("input", nargs="?", default=None,
                   help="input .vtk file (default: auto-detect unique .vtk in cwd)")
    p.add_argument("-o", "--output", default=None,
                   help="output .vtk file (default: 1.<stem>_v2.vtk)")
    p.add_argument("--mode", choices=("auto", "inplace", "stream"), default="auto")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    in_path = args.input or auto_detect_vtk(INPUT_DIR)
    if not os.path.isfile(in_path):
        print(f"[error] input file not found: {in_path}", file=sys.stderr)
        return 1

    # Default output goes to OUTPUT_DIR (regardless of where input came from)
    out_path = args.output or os.path.join(
        OUTPUT_DIR, os.path.basename(build_output_name(in_path)))

    same_length = all(
        old == new or len(old) == len(new) for old, new in RENAME.items()
    )
    mode = args.mode
    if mode == "auto":
        mode = "inplace" if same_length else "stream"

    in_size = os.path.getsize(in_path)
    print(f"Input  : {in_path}   ({in_size:,} bytes)")
    print(f"Output : {out_path}")
    print(f"Mode   : {mode}")
    n_active = sum(1 for k, v in RENAME.items() if k != v)
    print(f"Active rename rules: {n_active}")
    for old, new in RENAME.items():
        if old != new:
            print(f"    {old:10s} -> {new}")
    print()

    if args.dry_run:
        print("(dry-run) scanning input to list datasets that will be renamed:")
        targets = {k for k, v in RENAME.items() if k != v}
        found = []
        for kind, name, off in _scan_headers(in_path):
            if name in targets and name not in [n for _, n, _, _ in found]:
                found.append((kind, name, off, RENAME[name]))
        for kind, name, off, new in found:
            print(f"    [{kind:8s}] {name:12s} -> {new:12s}  @byte {off:>13,}")
        return 0

    print("Processing...")
    if mode == "inplace":
        log = rename_inplace(in_path, out_path, RENAME)
    else:
        log = rename_streaming(in_path, out_path, RENAME)

    print(f"\nRenamed {len(log)} dataset declarations:")
    for kind, old, new, locator in log:
        loc = f"@byte {locator:>13,}" if mode == "inplace" else f"@line {locator}"
        print(f"    [{kind:8s}] {old:12s} -> {new:12s}  ({loc})")

    out_size = os.path.getsize(out_path)
    print(f"\nInput  size : {in_size:>14,} bytes")
    print(f"Output size : {out_size:>14,} bytes")
    print(f"Delta       : {out_size - in_size:>+14,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
