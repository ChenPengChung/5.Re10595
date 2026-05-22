# -*- coding: utf-8 -*-
"""
phase1_common.py
================
Shared utilities for the Phase 1 friction-velocity pipeline.

Contents:
    * 6th-order Fornberg finite-difference coefficients + operators
        - FD6_COEFF, FD6_FWD, FD6_BWD
        - d_dj_periodic_2d, d_dj_periodic_row, d_dxi_periodic_2d_axis0
        - d_dk_fornberg
    * Tecplot 2D mesh parser   (parse_tecplot_2d_mesh)
    * ASCII VTK section scan + scalar/coord readers
        - scan_vtk_sections, map_vtk_sections, parse_dimensions
        - read_scalar_full, read_x_array
    * File auto-detection helpers
        - find_unique_matching   (positive filter — used by 3/4/5/verify)
        - find_unique_excluding  (negative filter — used by 1/2)
    * parse_re_token  (extract Re<num> from filenames)
    * Wall-slab dat column schema  (WALL_DAT_COLUMNS, WALL_DAT_NCOLS)

All numerics extracted verbatim from the original 3/4/5/verify scripts so
that pipeline outputs remain byte-for-byte identical after the refactor.
"""

from __future__ import annotations
import glob
import os
import re
import sys
from typing import Dict, Iterator, Optional, Sequence, Tuple

import numpy as np


# ============================================================================
#  6th-order Fornberg finite-difference coefficients
# ============================================================================
#
#  7-point stencil, 1st derivative, unit spacing.
#  FD6_COEFF[p, m]: stencil offset p (0=forward, 3=central, 6=backward),
#                   m = stencil index 0..6.  derivative = sum(coeff * f) / 60.
#
FD6_COEFF = np.array([
    [-147.0,  360.0, -450.0,  400.0, -225.0,   72.0,  -10.0],   # p=0 forward
    [ -10.0,  -77.0,  150.0, -100.0,   50.0,  -15.0,    2.0],   # p=1
    [   2.0,  -24.0,  -35.0,   80.0,  -30.0,    8.0,   -1.0],   # p=2
    [  -1.0,    9.0,  -45.0,    0.0,   45.0,   -9.0,    1.0],   # p=3 central
    [   1.0,   -8.0,   30.0,  -80.0,   35.0,   24.0,   -2.0],   # p=4
    [  -2.0,   15.0,  -50.0,  100.0, -150.0,   77.0,   10.0],   # p=5
    [  10.0,  -72.0,  225.0, -400.0,  450.0, -360.0,  147.0],   # p=6 backward
]) / 60.0

FD6_FWD = FD6_COEFF[0]   # forward 6th-order, used at k=0    (in 5.py)
FD6_BWD = FD6_COEFF[6]   # backward 6th-order, used at k=K-1 (in 5.py)


def d_dj_periodic_2d(f: np.ndarray, period_offset: float = 0.0) -> np.ndarray:
    """6th-order central FD on axis 1 (j, stream), periodic with optional
    period offset for monotonic coordinates.

    Convention: J = N+1 inclusive grid where j=0 and j=N=J-1 represent the
    SAME physical point (periodic hill identifies y=0 with y=L).  Period
    in j-index is N = J-1.

    For the y stream coordinate that ramps 0 -> L over one period (NOT
    truly periodic in value, only in topology), pass period_offset = L so
    the wrap-around values get the correct +/-L jump.  For genuinely
    periodic fields (e.g. z, or any flow variable) leave period_offset=0.
    """
    K, J = f.shape
    pad_lo = f[:, J - 4:J - 1] - period_offset      # j = N-3, N-2, N-1
    pad_hi = f[:, 1:4]         + period_offset      # j = 1, 2, 3 (next period)
    f_ext  = np.concatenate([pad_lo, f, pad_hi], axis=1)   # shape (K, J+6)
    return (-f_ext[:, 0:J]
            + 9 * f_ext[:, 1:J + 1]
           - 45 * f_ext[:, 2:J + 2]
           + 45 * f_ext[:, 4:J + 4]
            - 9 * f_ext[:, 5:J + 5]
            +     f_ext[:, 6:J + 6]) / 60.0


def d_dj_periodic_row(f_row: np.ndarray, period_offset: float = 0.0) -> np.ndarray:
    """6th-order central FD along j on a single row (1D array), periodic.

    f_row shape (J,).  Returns shape (J,).  period_offset = period length
    for monotonic coordinates (e.g. y in periodic hill); 0 for periodic
    fields (e.g. z).
    """
    J = f_row.shape[0]
    pad_lo = f_row[J - 4:J - 1] - period_offset      # j = N-3, N-2, N-1
    pad_hi = f_row[1:4]         + period_offset      # j = 1, 2, 3
    f_ext  = np.concatenate([pad_lo, f_row, pad_hi]) # length J+6
    return (-f_ext[0:J]
            + 9 * f_ext[1:J + 1]
           - 45 * f_ext[2:J + 2]
           + 45 * f_ext[4:J + 4]
            - 9 * f_ext[5:J + 5]
            +     f_ext[6:J + 6]) / 60.0


def d_dxi_periodic_2d_axis0(f_2d: np.ndarray) -> np.ndarray:
    """6th-order central FD on axis 0 (j) with periodic wrap, no offset.

    f_2d shape (J, I).  Returns (J, I).  Used by 5.py for du_t/dxi at the
    wall row, where u_t is a periodic FIELD (not a coordinate) so no
    offset.
    """
    Jdim = f_2d.shape[0]
    pad_lo = f_2d[Jdim - 4:Jdim - 1]            # j = N-3, N-2, N-1
    pad_hi = f_2d[1:4]                           # j = 1, 2, 3
    f_ext  = np.concatenate([pad_lo, f_2d, pad_hi], axis=0)   # length J+6
    return (-f_ext[0:Jdim]
            + 9 * f_ext[1:Jdim + 1]
           - 45 * f_ext[2:Jdim + 2]
           + 45 * f_ext[4:Jdim + 4]
            - 9 * f_ext[5:Jdim + 5]
            +     f_ext[6:Jdim + 6]) / 60.0


def d_dk_fornberg(f: np.ndarray) -> np.ndarray:
    """6th-order Fornberg adaptive FD along axis 0 (k, normal), non-periodic.

    For each k, pick stencil start s = clamp(k-3, 0, K-7) and offset p = k-s,
    then apply FD6_COEFF[p].  Recovers central diff for k in [3, K-4],
    forward-biased near k=0..2, backward-biased near k=K-3..K-1.  Uniform
    6th order; no 5th-order buffer.
    """
    K = f.shape[0]
    if K < 7:
        raise ValueError(f"need K >= 7 for 6th-order Fornberg, got K={K}")
    out = np.empty_like(f)
    for k in range(K):
        s = max(0, min(K - 7, k - 3))
        p = k - s
        out[k] = FD6_COEFF[p] @ f[s:s + 7]
    return out


# ============================================================================
#  File auto-detection
# ============================================================================
def find_unique_matching(folder: str, glob_pattern: str,
                         name_re: re.Pattern, label: str = "") -> str:
    """Find the unique file in *folder* matching *glob_pattern* AND *name_re*.

    Used when the script knows the EXACT name pattern of its input
    (e.g. step 3/4 looks for `1.*_v2.vtk` produced by step 1).
    Errors out (sys.exit) if 0 or >=2 matches.
    """
    paths = sorted(glob.glob(os.path.join(folder, glob_pattern)))
    matches = [p for p in paths if name_re.match(os.path.basename(p))]
    if len(matches) == 0:
        print(f"[error] no input matching '{name_re.pattern}' "
              f"in {os.path.abspath(folder)}", file=sys.stderr)
        sys.exit(1)
    if len(matches) >= 2:
        print(f"[error] {len(matches)} files match '{name_re.pattern}' "
              f"in {os.path.abspath(folder)}:", file=sys.stderr)
        for m in matches:
            print(f"    {os.path.basename(m)}", file=sys.stderr)
        sys.exit(1)
    return matches[0]


def find_unique_excluding(folder: str, glob_pattern: str,
                          exclude_re: re.Pattern,
                          ext_label: str = "") -> str:
    """Find the unique file in *folder* matching *glob_pattern* but NOT
    *exclude_re*.

    Used when the script wants to grab any input of a given extension but
    must skip files it has previously produced (e.g. step 1 picks any
    `*.vtk` except `1.*_v2.vtk` so that re-running in the same folder
    still works).
    """
    all_paths = sorted(glob.glob(os.path.join(folder, glob_pattern)))
    matches = [p for p in all_paths
               if not exclude_re.match(os.path.basename(p))]
    label = ext_label or glob_pattern
    if len(matches) == 0:
        print(f"[error] no input {label} found in "
              f"{os.path.abspath(folder)}", file=sys.stderr)
        sys.exit(1)
    if len(matches) >= 2:
        print(f"[error] {len(matches)} input {label} files found in "
              f"{os.path.abspath(folder)}; cannot decide which to use:",
              file=sys.stderr)
        for m in matches:
            print(f"    {os.path.basename(m)}", file=sys.stderr)
        sys.exit(1)
    return matches[0]


def parse_re_token(name: str, default: Optional[str] = None) -> str:
    """Extract `Re<num>` token from filename.

    If not found:
        * return *default* if given (used by 5.py with default='ReXXX')
        * otherwise sys.exit(1)
    """
    m = re.search(r"Re(\d+)", name)
    if m:
        return f"Re{m.group(1)}"
    if default is not None:
        return default
    print(f"[error] cannot find Re<num> in: {name}", file=sys.stderr)
    sys.exit(1)


# ============================================================================
#  variables.h numeric #define parser
# ============================================================================
_DEFINE_RE = re.compile(
    r"^\s*#define\s+(\w+)\s+(.+?)\s*(?://|/\*|$)", re.MULTILINE)


def _safe_eval_numeric_expr(expr: str) -> Optional[float]:
    """Evaluate a pure arithmetic expression; return None for anything else."""
    if not re.fullmatch(r"[\d.+\-*/()eE\s]+", expr):
        return None
    try:
        return float(eval(expr, {"__builtins__": {}}, {}))
    except Exception:
        return None


def _resolve_define_expr(expr: str, defs: Dict[str, str],
                         depth: int = 0) -> Optional[float]:
    """Resolve a #define expression by recursively substituting other names."""
    if depth > 16:
        return None
    text = expr.strip()
    val = _safe_eval_numeric_expr(text)
    if val is not None:
        return val

    substituted = text
    for name in sorted(defs.keys(), key=len, reverse=True):
        substituted = re.sub(r"\b" + re.escape(name) + r"\b",
                             f"({defs[name]})", substituted)
    if substituted == text:
        return None
    return _resolve_define_expr(substituted, defs, depth + 1)


def auto_detect_variables_h(folder: str = ".") -> Optional[str]:
    """Return the unique variables.h path in *folder*, or None if absent."""
    candidates = sorted(glob.glob(os.path.join(folder, "variables.[hH]")))
    if len(candidates) > 1:
        raise FileNotFoundError(
            f"multiple variables.h candidates in {folder}: {candidates}")
    return candidates[0] if candidates else None


def parse_header_constants(path: str) -> Dict[str, float]:
    """Parse numeric #define constants from variables.h.

    Handles simple arithmetic and references such as ``niu (Uref/Re)``.
    Non-numeric macros and string macros are ignored.
    """
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("latin-1", errors="replace")

    defs_raw = {m.group(1): m.group(2).strip()
                for m in _DEFINE_RE.finditer(text)}
    out: Dict[str, float] = {}
    for name, expr in defs_raw.items():
        val = _resolve_define_expr(expr, defs_raw)
        if val is not None:
            out[name] = val
    return out


def find_const(consts: Dict[str, float], names: Sequence[str],
               path: str = "variables.h") -> float:
    """Find a numeric constant by any case-insensitive name in *names*."""
    for wanted in names:
        for actual, value in consts.items():
            if actual.lower() == wanted.lower():
                return value
    raise KeyError(
        f"missing numeric #define {names!r} in {path}; "
        f"available numeric names: {sorted(consts)}")


# ============================================================================
#  Tau-wall convention (single, textbook lattice form)
# ============================================================================
#  The pipeline uses one convention for tau_wall throughout:
#
#      tau_wall = niu * du_t/dn               rho = 1
#
#  Step 4 reads VTK (V_mean = V_lattice/Uref) and multiplies by Uref
#  to restore physical lattice velocity.  u_t in 5/6.dat is therefore
#  already in lattice units, so the textbook formula applies directly:
#      tau_wall = nu * dV_lat/dn
#  which is the textbook lattice wall shear stress.
#
#      u_tau    = sqrt(tau_wall/rho)
#      z+       = u_tau * d_n / niu      (textbook y+ = u_tau*y/nu)
#
TAU_CONVENTION_LABEL = "tau = niu * du_t/dn (lattice stress, rho=1)"


#  Sentinel substrings for refusing legacy outer-nondim dat files
#  (kept only so the verify function can recognise them and emit a
#  helpful re-run message; they are NOT used in any computation).
_LEGACY_OUTER_NONDIM_SUBSTRINGS = (
    "tau/(rho*Uref^2)",
    "tau/(rho*Uref)",
    "tau = niu*Uref * du_t/dn",
)


def verify_lattice_tau_dat(path: str, context: str = "tau input",
                           n_bytes: int = 8192) -> None:
    """Sanity-check that *path* was produced by the lattice tau convention.

    The check is a substring match against the header of the file: any dat
    written under the legacy outer-nondim scheme is rejected so it cannot
    silently feed a downstream stage that now assumes the lattice form.
    """
    with open(path, "rb") as f:
        head = f.read(n_bytes).decode("utf-8", errors="replace")
    if TAU_CONVENTION_LABEL in head:
        return
    if any(s in head for s in _LEGACY_OUTER_NONDIM_SUBSTRINGS):
        raise ValueError(
            f"{context}: {path} was produced by the LEGACY outer-nondim "
            f"scheme (tau pre-divided by Uref^2 or Uref). Re-run step 5 "
            f"(and 10/12 for span-avg pipelines) with the current lattice "
            f"convention: {TAU_CONVENTION_LABEL}")
    raise ValueError(
        f"{context}: {path} has no recognizable tau-convention header. "
        f"Expected substring: {TAU_CONVENTION_LABEL!r}")


# ============================================================================
#  Tecplot 2D mesh parser
# ============================================================================
def parse_tecplot_2d_mesh(path: str) -> Tuple[np.ndarray, np.ndarray, int, int]:
    """Read a 2D Tecplot dat (I=1, J, K, F=POINT) and return y[k,j], z[k,j], J, K.

    Reshape uses (K, J, 2) -- which matches the I-fast/J-mid/K-slow POINT
    layout when I=1 collapses (effective K-slow / J-fast).
    """
    with open(path) as f:
        raw = f.readlines()
    head = " ".join(raw[:10])
    I = int(re.search(r"I\s*=\s*(\d+)", head).group(1))
    J = int(re.search(r"J\s*=\s*(\d+)", head).group(1))
    K = int(re.search(r"K\s*=\s*(\d+)", head).group(1))
    if I != 1:
        print(f"[error] expected I=1 (degenerate span axis), got I={I}",
              file=sys.stderr)
        sys.exit(1)

    data_start = None
    for n, ln in enumerate(raw):
        toks = ln.split()
        if len(toks) == 2:
            try:
                float(toks[0]); float(toks[1])
                data_start = n; break
            except ValueError:
                pass
    if data_start is None:
        raise ValueError("no 2-column numeric data found in dat")

    data = np.loadtxt(raw[data_start:])
    if data.shape != (J * K, 2):
        raise ValueError(
            f"data shape {data.shape} != expected ({J*K}, 2) for J={J} K={K}")
    arr = data.reshape(K, J, 2)
    return arr[..., 0], arr[..., 1], J, K


# ============================================================================
#  ASCII VTK section scanning
# ============================================================================
#  Broad match: POINTS + POINT_DATA + SCALARS + VECTORS.
#  3.py originally only matched POINT_DATA|VECTORS|SCALARS but adding POINTS
#  is harmless: 3.py only ever queries SCALARS:V_mean / SCALARS:W_mean
#  and uses POINT_DATA boundary, so an extra "POINTS" key in the dict does
#  not change any byte of the output.
_HDR_RE = re.compile(
    rb"^(POINT_DATA|VECTORS|SCALARS|POINTS)[ \t]+(\S+)[^\n]*\n", re.MULTILINE)


def scan_vtk_sections(path: str) -> Iterator[Tuple[str, str, int, int]]:
    """Yield (kind, name, line_start_byte, line_end_byte) for every section
    declaration in the VTK file.  Chunked binary scan for speed.
    """
    CHUNK = 64 * 1024 * 1024
    OVERLAP = 1024
    seen = set()
    with open(path, 'rb') as f:
        leftover = b''
        while True:
            chunk = f.read(CHUNK)
            if not chunk and not leftover:
                break
            buf = leftover + chunk
            buf_offset = f.tell() - len(buf)
            for m in _HDR_RE.finditer(buf):
                ls = buf_offset + m.start()
                if ls in seen:
                    continue
                seen.add(ls)
                yield (m.group(1).decode("ascii"),
                       m.group(2).decode("ascii"),
                       ls,
                       buf_offset + m.end())
            if not chunk:
                break
            leftover = buf[-OVERLAP:] if len(buf) > OVERLAP else buf


def map_vtk_sections(path: str) -> Dict[str, Tuple[int, int]]:
    """Return dict mapping section keys to (line_start_byte, line_end_byte).

    Keys:
        'POINTS'         (only one POINTS section per file)
        'POINT_DATA'     (declaration line for the data block)
        '<KIND>:<name>'  for SCALARS / VECTORS / TENSORS / etc.
    """
    out: Dict[str, Tuple[int, int]] = {}
    for kind, name, ls, le in scan_vtk_sections(path):
        if kind == "POINTS":
            out["POINTS"] = (ls, le)
        elif kind == "POINT_DATA":
            out["POINT_DATA"] = (ls, le)
        else:
            out[f"{kind}:{name}"] = (ls, le)
    return out


def parse_dimensions(path: str) -> Tuple[int, int, int]:
    """Parse `DIMENSIONS Nx Ny Nz` from the VTK header (first 2 KB)."""
    with open(path, 'rb') as f:
        head = f.read(2048).decode('ascii', errors='replace')
    m = re.search(r"DIMENSIONS\s+(\d+)\s+(\d+)\s+(\d+)", head)
    if not m:
        raise ValueError("DIMENSIONS line not found in first 2KB of VTK")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _detect_vtk_format(path: str) -> str:
    """Return 'ASCII' or 'BINARY' from VTK file header (line 3)."""
    with open(path, 'rb') as f:
        f.readline()  # # vtk DataFile Version ...
        f.readline()  # title
        fmt_line = f.readline().decode('ascii').strip().upper()
    if fmt_line == "BINARY":
        return "BINARY"
    return "ASCII"


_VTK_DTYPE_MAP = {
    "float": (np.dtype('>f4'), 4),
    "double": (np.dtype('>f8'), 8),
    "int": (np.dtype('>i4'), 4),
    "unsigned_int": (np.dtype('>u4'), 4),
    "long": (np.dtype('>i8'), 8),
    "short": (np.dtype('>i2'), 2),
}


def read_scalar_full(path: str, sections: Dict[str, Tuple[int, int]],
                     name: str, n_values: int) -> np.ndarray:
    """Read a SCALARS <name> data block as a flat float64 array.

    Supports both ASCII and BINARY VTK formats.
    """
    key = f"SCALARS:{name}"
    if key not in sections:
        raise ValueError(f"section {key!r} not found in VTK")
    line_start, line_end = sections[key]

    fmt = _detect_vtk_format(path)

    if fmt == "BINARY":
        with open(path, 'rb') as f:
            f.seek(line_start)
            header_line = f.readline().decode('ascii')
        parts = header_line.split()
        dtype_str = parts[2] if len(parts) >= 3 else "float"
        if dtype_str not in _VTK_DTYPE_MAP:
            raise ValueError(f"unsupported VTK dtype '{dtype_str}' in {name}")
        be_dtype, itemsize = _VTK_DTYPE_MAP[dtype_str]
        nbytes = n_values * itemsize

        with open(path, 'rb') as f:
            f.seek(line_end)
            f.readline()  # skip LOOKUP_TABLE default
            raw = f.read(nbytes)

        if len(raw) < nbytes:
            raise ValueError(
                f"expected {nbytes} bytes for {name}, got {len(raw)}")
        arr = np.frombuffer(raw, dtype=be_dtype, count=n_values).astype(
            np.float64)
    else:
        sorted_keys = sorted(sections.keys(), key=lambda k: sections[k][0])
        idx = sorted_keys.index(key)
        data_byte_end = (sections[sorted_keys[idx + 1]][0]
                         if idx + 1 < len(sorted_keys)
                         else os.path.getsize(path))

        with open(path, 'rb') as f:
            f.seek(line_end)
            f.readline()  # skip LOOKUP_TABLE default
            data_byte_start = f.tell()
            text_bytes = f.read(data_byte_end - data_byte_start)

        arr = np.array(text_bytes.split(), dtype=np.float64)

    if arr.size != n_values:
        raise ValueError(
            f"read {arr.size} values from {name}, expected {n_values}")
    return arr


def read_x_array(path: str, sections: Dict[str, Tuple[int, int]],
                 Nx: int) -> np.ndarray:
    """Read first Nx points (x-coordinates) from POINTS section."""
    if "POINTS" not in sections:
        raise ValueError("POINTS section not found in VTK")
    line_start, line_end = sections["POINTS"]

    fmt = _detect_vtk_format(path)

    if fmt == "BINARY":
        with open(path, 'rb') as f:
            f.seek(line_start)
            header_line = f.readline().decode('ascii')
        parts = header_line.split()
        dtype_str = parts[2] if len(parts) >= 3 else "float"
        if dtype_str not in _VTK_DTYPE_MAP:
            raise ValueError(f"unsupported VTK dtype '{dtype_str}' in POINTS")
        be_dtype, itemsize = _VTK_DTYPE_MAP[dtype_str]
        nbytes = Nx * 3 * itemsize
        with open(path, 'rb') as f:
            f.seek(line_end)
            raw = f.read(nbytes)
        if len(raw) < nbytes:
            raise ValueError(
                f"expected {nbytes} bytes for POINTS, got {len(raw)}")
        pts = np.frombuffer(raw, dtype=be_dtype, count=Nx * 3).astype(
            np.float64).reshape(Nx, 3)
        return pts[:, 0].copy()
    else:
        x_arr = np.empty(Nx)
        with open(path, 'rb') as f:
            f.seek(line_end)
            for i in range(Nx):
                line = f.readline().decode("ascii")
                x_arr[i] = float(line.split()[0])
        return x_arr


# ============================================================================
#  Ustar_Force_record.dat (monitor) parsing
# ============================================================================
#  Two helpers shared by 15.phase4_compute_Fbody.py and
#  16.verify_force_balance.py.  The two scripts had near-identical copies
#  that drifted (16.py tracked ftt_min/max, 15.py did not); this single
#  source uses the richer signature so both callers get the same fields.
def detect_ftt_start_from_monitor(monitor_path: str) -> float:
    """Return the FTT where accu_cnt first becomes > 0 in the monitor file."""
    with open(monitor_path) as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            accu_cnt = int(parts[4])
            if accu_cnt > 0:
                return float(parts[0])
    raise ValueError(f"no rows with accu_cnt > 0 in {monitor_path}")


def parse_monitor_force_avg(monitor_path: str, ftt_start: Optional[float],
                            Uref: float, LY: float) -> Dict[str, float]:
    """Compute time-averaged Force from Ustar_Force_record.dat.

    If *ftt_start* is None, auto-detect from the first row with accu_cnt > 0.
    Monitor convention: Force* = Force * LY / Uref**2, so the recovered
    physical Force is Force* * Uref**2 / LY.

    Returns dict with keys:
        Force_avg, Force_star_avg, Force_star_min, Force_star_max,
        n_samples, ftt_min, ftt_max, ftt_start_used
    """
    if ftt_start is None:
        ftt_start = detect_ftt_start_from_monitor(monitor_path)

    sum_fstar = 0.0
    count = 0
    fstar_min = float("inf")
    fstar_max = float("-inf")
    ftt_min = float("inf")
    ftt_max = float("-inf")

    with open(monitor_path) as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            ftt = float(parts[0])
            if ftt < ftt_start:
                continue
            fstar = float(parts[2])
            sum_fstar += fstar
            count += 1
            if fstar < fstar_min:
                fstar_min = fstar
            if fstar > fstar_max:
                fstar_max = fstar
            if ftt < ftt_min:
                ftt_min = ftt
            if ftt > ftt_max:
                ftt_max = ftt

    if count == 0:
        raise ValueError(
            f"no monitor data with FTT >= {ftt_start} in {monitor_path}")

    fstar_avg = sum_fstar / count
    force_avg = fstar_avg * Uref ** 2 / LY

    return dict(
        Force_avg=force_avg,
        Force_star_avg=fstar_avg,
        Force_star_min=fstar_min,
        Force_star_max=fstar_max,
        n_samples=count,
        ftt_min=ftt_min,
        ftt_max=ftt_max,
        ftt_start_used=ftt_start,
    )


# ============================================================================
#  Wall-slab dat column schema  (set by 4.phase1_computeutangent.py)
# ============================================================================
#  Dat layout written by 4.py and consumed by 5.py.  Centralizing here
#  prevents a column drift if either script ever adds/reorders fields.
WALL_DAT_COLUMNS = {
    "i":            0,
    "j":            1,
    "k":            2,
    "x":            3,
    "y":            4,
    "z":            5,
    "V_mean":       6,
    "W_mean":       7,
    "u_tangent":    8,
    "u_normal":     9,
    "h_xi":         10,
    "J":            11,
    "e_xi.e_zeta":  12,
    "y_kn":         13,
    "z_kn":         14,
}
WALL_DAT_NCOLS = len(WALL_DAT_COLUMNS)        # = 15


# ============================================================================
#  Tau-wall dat column schema (set by 5.phase1_compute_tauwall.py)
# ============================================================================
#  Dat layout written by 5.py and consumed by 6.py.  K=1 single-layer file.
TAUWALL_DAT_COLUMNS = {
    "i":                0,
    "j":                1,
    "x":                2,
    "y":                3,
    "z":                4,
    "du_t_dxi":         5,
    "du_t_dzeta":       6,
    "h_xi":             7,
    "J":                8,
    "e_xi.e_zeta":      9,
    "du_t_dn":          10,
    "tau_wall_signed":  11,
    "tau_wall_abs":     12,
}
TAUWALL_DAT_NCOLS = len(TAUWALL_DAT_COLUMNS)  # = 13


def load_tauwall_dat(path: str) -> Dict[str, np.ndarray]:
    """Load 7.*.dat or 8.*.dat (bottom/top tau_wall, K=1 wall layer).

    Returns dict with:
        Nx, Ny  : grid shape (int)
        x  (Nx,)         : span coordinates (varies only with i)
        y  (Ny,)         : stream coordinates at wall (varies only with j)
        z  (Ny,)         : normal coordinates at wall (varies only with j)
        tau_signed (Ny, Nx)
        tau_abs    (Ny, Nx)
        du_t_dn    (Ny, Nx)
    """
    data = np.loadtxt(path, skiprows=4)
    if data.shape[1] != TAUWALL_DAT_NCOLS:
        raise ValueError(
            f"expected {TAUWALL_DAT_NCOLS} columns in {path}, "
            f"got {data.shape[1]}")
    i_col = data[:, TAUWALL_DAT_COLUMNS["i"]].astype(int)
    j_col = data[:, TAUWALL_DAT_COLUMNS["j"]].astype(int)
    Nx = int(i_col.max()) + 1
    Ny = int(j_col.max()) + 1
    if data.shape[0] != Nx * Ny:
        raise ValueError(
            f"row count {data.shape[0]} != Nx*Ny ({Nx}*{Ny} = {Nx*Ny})")

    # Tecplot POINT format: i-fast, j-slow → reshape (Ny, Nx)
    tau_signed = data[:, TAUWALL_DAT_COLUMNS["tau_wall_signed"]].reshape(Ny, Nx)
    tau_abs    = data[:, TAUWALL_DAT_COLUMNS["tau_wall_abs"]].reshape(Ny, Nx)
    du_t_dn    = data[:, TAUWALL_DAT_COLUMNS["du_t_dn"]].reshape(Ny, Nx)

    # x varies only with i: first Nx rows have j=0, span over i=0..Nx-1
    x_arr = data[0:Nx, TAUWALL_DAT_COLUMNS["x"]].copy()
    # y, z vary only with j: sample at i=0 across all j
    y_arr = data[0:Ny * Nx:Nx, TAUWALL_DAT_COLUMNS["y"]].copy()
    z_arr = data[0:Ny * Nx:Nx, TAUWALL_DAT_COLUMNS["z"]].copy()

    return dict(Nx=Nx, Ny=Ny, x=x_arr, y=y_arr, z=z_arr,
                tau_signed=tau_signed, tau_abs=tau_abs, du_t_dn=du_t_dn)


def cell_average_2d(field: np.ndarray) -> np.ndarray:
    """4-point cell average.  Input shape (Ny, Nx) → output (Ny-1, Nx-1).

    cell[j, i] = mean of field at corners
                 (j, i), (j+1, i), (j, i+1), (j+1, i+1)
    """
    return 0.25 * (field[:-1, :-1] + field[1:, :-1] +
                   field[:-1, 1:]  + field[1:, 1:])


def cell_areas_2d(x_arr: np.ndarray,
                  y_arr: np.ndarray,
                  z_arr: np.ndarray) -> np.ndarray:
    """Parallelogram cell areas for a wall layer where x varies only with i
    and (y, z) vary only with j.

    Cell edges:
        a = (Δx_i, 0,      0     )
        b = (0,     Δy_j,  Δz_j  )
        ΔA = |a × b| = Δx_i · sqrt(Δy_j² + Δz_j²)  (parallelogram area)

    The √(Δy² + Δz²) factor is the arc-length step along the wall in the
    y-z plane, so curved (hill) walls correctly accumulate more area than
    a flat reference.

    Returns shape (Ny-1, Nx-1).
    """
    dx = np.diff(x_arr)                   # (Nx-1,)
    dy = np.diff(y_arr)                   # (Ny-1,)
    dz = np.diff(z_arr)                   # (Ny-1,)
    ds = np.sqrt(dy ** 2 + dz ** 2)       # (Ny-1,)  arc-length along wall
    return ds[:, None] * dx[None, :]      # (Ny-1, Nx-1)
