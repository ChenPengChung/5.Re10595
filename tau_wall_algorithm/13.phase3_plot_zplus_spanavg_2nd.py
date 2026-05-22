#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
13.phase3_plot_zplus_spanavg_2nd.py
====================================

Companion plot to 11.phase3_plot_zplus_spanavg.py, but for the
2nd-order FD pipeline (step 12 outputs 27/28/29.dat).

Same LaTeX-quality academic style as 9 / 11.py.

Output: 32.Re<X>_zplus_streamwise_2nd1A2D.{pdf,png}
"""

from __future__ import annotations
import argparse
import glob
import os
import re
import sys

import numpy as np
import matplotlib.pyplot as plt


def parse_tecplot_1d(path: str):
    Ny = None
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
                m = re.search(r"I\s*=\s*(\d+)", s)
                if m:
                    Ny = int(m.group(1))
                continue
            break
    if Ny is None or not col_names:
        raise ValueError(f"could not parse: {path}")
    raw = np.loadtxt(path, skiprows=header_lines)
    if raw.shape[0] != Ny:
        raise ValueError(f"{path}: expected {Ny} rows, got {raw.shape[0]}")
    cols = {name: raw[:, c] for c, name in enumerate(col_names)}
    return cols, Ny


def find_dat(folder: str, num: int, suffix: str) -> str:
    pattern = os.path.join(folder, f"{num}.Re*{suffix}")
    hits = sorted(glob.glob(pattern))
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise FileNotFoundError(f"no file matching {pattern}")
    raise FileNotFoundError(f"multiple matches for {pattern}: {hits}")


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Plot 2nd-order FD pipeline z+ vs stream-wise y/h.")
    ap.add_argument("--folder",  default="Output",
                    help="folder with 29/30/31.dat (default: Output)")
    ap.add_argument("--bot",     default=None, help="29.*.dat path")
    ap.add_argument("--top",     default=None, help="30.*.dat path")
    ap.add_argument("--normal",  default=None, help="31.*.dat path")
    ap.add_argument("--show",    action="store_true",
                    help="display interactive window")
    args = ap.parse_args(argv)

    folder = args.folder
    bot_path    = args.bot    or find_dat(folder, 29,
                                          "_zplus_bottom_spanavg_2nd.dat")
    top_path    = args.top    or find_dat(folder, 30,
                                          "_zplus_top_spanavg_2nd.dat")
    normal_path = args.normal or find_dat(folder, 31,
                                          "_zplus_bottom_normal_spanavg_2nd.dat")

    print(f"input bot    : {bot_path}")
    print(f"input top    : {top_path}")
    print(f"input normal : {normal_path}")

    bot, Ny_bot = parse_tecplot_1d(bot_path)
    top, _      = parse_tecplot_1d(top_path)
    nrm, _      = parse_tecplot_1d(normal_path)

    m = re.search(r"Re(\d+)", os.path.basename(bot_path))
    Re = int(m.group(1)) if m else 0

    y_bot = bot["y"]
    y_top = top["y"]
    y_nrm = nrm["y"]
    assert np.allclose(y_bot, y_nrm), "y mismatch between 27 and 29"

    zp_bot = bot["z_plus"]
    zp_top = top["z_plus"]
    zp_nrm = nrm["z_plus_proj"]

    print(f"\n2nd-order-pipeline z+  (Re = {Re}, Ny = {Ny_bot}):")
    for tag, arr in [("top    ", zp_top),
                     ("bot    ", zp_bot),
                     ("bot(n) ", zp_nrm)]:
        print(f"  {tag}: min = {arr.min():.4f}   max = {arr.max():.4f}   "
              f"mean = {arr.mean():.4f}")

    ut_bot = bot["u_tau_local"]
    ut_top = top["u_tau_local"]

    print(f"\n2nd-order u_tau:")
    print(f"  bot: min = {ut_bot.min():.6e}   max = {ut_bot.max():.6e}")
    print(f"  top: min = {ut_top.min():.6e}   max = {ut_top.max():.6e}")

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

    stem = (f"32.Re{Re}_zplus_streamwise_2nd1A2D"
            if Re else "32.zplus_streamwise_2nd1A2D")
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
