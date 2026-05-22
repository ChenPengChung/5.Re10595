#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
9.phase3_plot_zplus.py
======================

Plot span-wise averaged z+ along the stream-wise direction (y/h).

Reads step-8 outputs (15/16/17.dat), averages z+ over x (span-wise,
homogeneous), and draws three curves over the full input y/h range:

    top wall      -- simple |delta_z|            from 16.Re*.dat
    bottom wall   -- simple |delta_z|            from 15.Re*.dat
    bottom wall   -- n-hat projection            from 17.Re*.dat

Output: 18.Re<X>_zplus_streamwise.{pdf,png}
"""

from __future__ import annotations
import argparse
import glob
import os
import re
import sys

import numpy as np
import matplotlib.pyplot as plt


# ============================================================================
#  Tecplot POINT-format reader
# ============================================================================
def parse_tecplot_point(path: str) -> tuple[dict, int, int]:
    """Parse a Tecplot POINT-format file written by step 8.

    Returns
    -------
    cols : dict[str, ndarray]  each value has shape (Ny, Nx)
    Nx   : int  (I, span-wise)
    Ny   : int  (J, stream-wise)
    """
    Nx = Ny = None
    col_names: list[str] = []
    header_lines = 0
    with open(path) as f:
        for line in f:
            s = line.strip()
            if not s or s[0] == "#":
                header_lines += 1
                continue
            up = s.upper()
            if up.startswith("TITLE") or up.startswith("DT"):
                header_lines += 1
                continue
            if up.startswith("VARIABLES"):
                header_lines += 1
                col_names = re.findall(r'"([^"]+)"', s)
                continue
            if up.startswith("ZONE"):
                header_lines += 1
                mi = re.search(r"I\s*=\s*(\d+)", s)
                mj = re.search(r"J\s*=\s*(\d+)", s)
                if mi and mj:
                    Nx, Ny = int(mi.group(1)), int(mj.group(1))
                continue
            break
    if Nx is None or Ny is None:
        raise ValueError(f"ZONE I/J not found in {path}")
    if not col_names:
        raise ValueError(f"VARIABLES not found in {path}")

    raw = np.loadtxt(path, skiprows=header_lines)
    if raw.shape[0] != Nx * Ny:
        raise ValueError(
            f"{path}: expected {Nx * Ny} rows, got {raw.shape[0]}")

    cols = {}
    for c, name in enumerate(col_names):
        cols[name] = raw[:, c].reshape(Ny, Nx)
    return cols, Nx, Ny


# ============================================================================
#  Auto-detect helper
# ============================================================================
def find_dat(folder: str, num: int, suffix: str) -> str:
    pattern = os.path.join(folder, f"{num}.Re*{suffix}")
    hits = sorted(glob.glob(pattern))
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise FileNotFoundError(f"no file matching {pattern}")
    raise FileNotFoundError(f"multiple matches for {pattern}: {hits}")


# ============================================================================
#  Plot style  (LaTeX-quality via mathtext + Computer Modern)
# ============================================================================
def setup_academic_style() -> None:
    plt.rcParams.update({
        "font.family":          "serif",
        "font.serif":           ["Computer Modern Roman",
                                 "Times New Roman", "DejaVu Serif"],
        "mathtext.fontset":     "cm",
        "axes.labelsize":       14,
        "font.size":            12,
        "legend.fontsize":      10.5,
        "xtick.labelsize":      12,
        "ytick.labelsize":      12,
        "axes.linewidth":       0.8,
        "lines.linewidth":      1.5,
        "xtick.direction":      "in",
        "ytick.direction":      "in",
        "xtick.top":            True,
        "ytick.right":          True,
        "xtick.major.size":     5,
        "ytick.major.size":     5,
        "xtick.minor.size":     3,
        "ytick.minor.size":     3,
        "xtick.minor.visible":  True,
        "ytick.minor.visible":  True,
        "figure.dpi":           150,
        "savefig.dpi":          300,
        "savefig.bbox":         "tight",
        "savefig.pad_inches":   0.05,
    })


# ============================================================================
#  Main
# ============================================================================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Plot span-averaged z+ vs stream-wise y/h.")
    ap.add_argument("--folder",  default="Output",
                    help="folder with 15/16/17.dat  (default: Output)")
    ap.add_argument("--bot",     default=None, help="15.*.dat path")
    ap.add_argument("--top",     default=None, help="16.*.dat path")
    ap.add_argument("--normal",  default=None, help="17.*.dat path")
    ap.add_argument("--show",    action="store_true",
                    help="display interactive window")
    args = ap.parse_args(argv)

    folder = args.folder
    bot_path    = args.bot    or find_dat(folder, 15, "_zplus_bottom.dat")
    top_path    = args.top    or find_dat(folder, 16, "_zplus_top.dat")
    normal_path = args.normal or find_dat(folder, 17, "_zplus_bottom_normal.dat")

    print(f"input bot    : {bot_path}")
    print(f"input top    : {top_path}")
    print(f"input normal : {normal_path}")

    # ---- parse ----
    bot, Nx, Ny = parse_tecplot_point(bot_path)
    top, _,  _  = parse_tecplot_point(top_path)
    nrm, _,  _  = parse_tecplot_point(normal_path)

    m = re.search(r"Re(\d+)", os.path.basename(bot_path))
    Re = int(m.group(1)) if m else 0

    # ---- span-wise (x) arithmetic mean ----
    y_bot  = bot["y_wall"][:, 0]
    y_top  = top["y_wall"][:, 0]
    y_nrm  = nrm["y_wall"][:, 0]
    assert np.allclose(y_bot, y_nrm), "y_wall mismatch between file 15 and 17"

    zp_bot = bot["z_plus"].mean(axis=1)
    zp_top = top["z_plus"].mean(axis=1)
    zp_nrm = nrm["z_plus_proj"].mean(axis=1)

    ut_bot = bot["u_tau_local"].mean(axis=1)
    ut_top = top["u_tau_local"].mean(axis=1)

    print(f"\nSpan-averaged u_tau:")
    print(f"  bot: min = {ut_bot.min():.6e}   max = {ut_bot.max():.6e}")
    print(f"  top: min = {ut_top.min():.6e}   max = {ut_top.max():.6e}")

    print(f"\nSpan-averaged z+  (Re = {Re}, Nx = {Nx}, Ny = {Ny}):")
    for tag, arr in [("top    ", zp_top),
                     ("bot    ", zp_bot),
                     ("bot(n) ", zp_nrm)]:
        print(f"  {tag}: min = {arr.min():.4f}   max = {arr.max():.4f}   "
              f"mean = {arr.mean():.4f}")

    # ---- plot ----
    setup_academic_style()
    fig, ax = plt.subplots(figsize=(8, 4.5))

    ax.plot(y_top, zp_top, "-",
            color="#24347a", lw=1.6,
            label=r"Top wall, simple $|\Delta z|$")
    ax.plot(y_bot, zp_bot, "--",
            color="#d62728", lw=1.6,
            label=r"Bottom wall, simple $|\Delta z|$")
    ax.plot(y_bot, zp_nrm, "-.",
            color="#2ca02c", lw=1.6,
            label=r"Bottom wall, $\hat{n}$-projection")

    ax.set_xlabel(r"$y \,/\, h$")
    ax.set_ylabel(r"$\langle z^{+} \rangle_x$")
    ax.set_xlim(min(y_top.min(), y_bot.min(), y_nrm.min()),
                max(y_top.max(), y_bot.max(), y_nrm.max()))

    ax2 = ax.twinx()
    ax2.plot(y_bot, ut_bot, "-o",
             color="#ff7f0e", lw=1.4, markersize=3, markevery=8,
             label=r"$\langle u_\tau \rangle_x$ bottom")
    ax2.plot(y_top, ut_top, "-o",
             color="#9467bd", lw=1.4, markersize=3, markevery=8,
             label=r"$\langle u_\tau \rangle_x$ top")
    ax2.set_ylabel(r"$\langle u_\tau \rangle_x$")

    lines1, labels1 = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines1 + lines2, labels1 + labels2,
              frameon=True, fancybox=False, edgecolor="0.4",
              framealpha=1.0, loc="best")

    fig.tight_layout()

    stem = f"18.Re{Re}_zplus_streamwise_1D2A" if Re else "18.zplus_streamwise_1D2A"
    for ext in ("pdf", "png"):
        out = os.path.join(folder, f"{stem}.{ext}")
        fig.savefig(out)
        print(f"  saved -> {out}")

    if args.show:
        plt.show()

    plt.close(fig)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
