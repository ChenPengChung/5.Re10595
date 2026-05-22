# -*- coding: utf-8 -*-
"""
Verify bottom-wall grid orthogonality at k=0.

Reads the 2D Tecplot mesh file:
    2.j*_*.dat

Computes the wall covariant basis vectors:
    g_xi   = (y_xi,   z_xi)
    g_zeta = (y_zeta, z_zeta)

at k=0 using the same 6th-order finite-difference convention as the phase
scripts, then outputs angle(g_xi, g_zeta) in degrees versus j.

Outputs:
    7.wall_orthogonality_k0_j_angle.dat
    7.wall_orthogonality_k0_j_angle.png
"""

from __future__ import annotations

import os
import re
import sys
from typing import Tuple

import numpy as np

# ---- I/O directory bootstrap (this script lives in verify/, one level
#       below the project root containing Input/, Output/, Reference/) ----
_HERE         = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "Reference"))
OUTPUT_DIR = os.path.join(_PROJECT_ROOT, "Output")
VERIFY_DIR = _HERE
# --------------------------------------------------------------------------

from phase1_common import (
    FD6_COEFF,
    d_dj_periodic_row,
    parse_tecplot_2d_mesh,
    find_unique_matching,
)


_MESH_RE = re.compile(r"^2\.j.+\.dat$", re.IGNORECASE)


def find_unique_mesh(folder: str = ".") -> str:
    return find_unique_matching(folder, "*.dat", _MESH_RE)


def compute_bottom_wall_angle(y_2d: np.ndarray, z_2d: np.ndarray) -> dict:
    """Return wall metric and angle between g_xi and g_zeta at k=0."""
    y_wall = y_2d[0]
    z_wall = z_2d[0]
    period = float(y_wall[-1] - y_wall[0])

    y_xi = d_dj_periodic_row(y_wall, period_offset=period)
    z_xi = d_dj_periodic_row(z_wall, period_offset=0.0)

    coef = FD6_COEFF[0]
    y_zeta = coef @ y_2d[0:7, :]
    z_zeta = coef @ z_2d[0:7, :]

    h_xi = np.sqrt(y_xi**2 + z_xi**2)
    h_zeta = np.sqrt(y_zeta**2 + z_zeta**2)
    dot = y_xi * y_zeta + z_xi * z_zeta
    cos_theta = dot / (h_xi * h_zeta)
    cos_theta = np.clip(cos_theta, -1.0, 1.0)
    angle_deg = np.degrees(np.arccos(cos_theta))
    deviation_deg = angle_deg - 90.0

    return {
        "y_wall": y_wall,
        "z_wall": z_wall,
        "y_xi": y_xi,
        "z_xi": z_xi,
        "y_zeta": y_zeta,
        "z_zeta": z_zeta,
        "h_xi": h_xi,
        "h_zeta": h_zeta,
        "dot": dot,
        "cos_theta": cos_theta,
        "angle_deg": angle_deg,
        "deviation_deg": deviation_deg,
    }


def write_dat(path: str, result: dict) -> None:
    J = len(result["angle_deg"])
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Bottom wall grid orthogonality check at k=0\n")
        f.write("# angle_deg = angle between g_xi=(y_xi,z_xi) and "
                "g_zeta=(y_zeta,z_zeta)\n")
        f.write("# Perfect orthogonality: angle_deg = 90, deviation_deg = 0\n")
        f.write("# columns:\n")
        f.write("# j y_wall z_wall y_xi z_xi y_zeta z_zeta "
                "h_xi h_zeta dot cos_theta angle_deg deviation_deg\n")
        for j in range(J):
            f.write(
                f"{j:6d} "
                f"{result['y_wall'][j]: .12e} {result['z_wall'][j]: .12e} "
                f"{result['y_xi'][j]: .12e} {result['z_xi'][j]: .12e} "
                f"{result['y_zeta'][j]: .12e} {result['z_zeta'][j]: .12e} "
                f"{result['h_xi'][j]: .12e} {result['h_zeta'][j]: .12e} "
                f"{result['dot'][j]: .12e} {result['cos_theta'][j]: .12e} "
                f"{result['angle_deg'][j]: .12e} "
                f"{result['deviation_deg'][j]: .12e}\n"
            )


def write_plot(path: str, result: dict) -> None:
    import matplotlib as mpl
    import matplotlib.pyplot as plt

    mpl.rcParams.update({
        "font.family": "serif",
        "mathtext.fontset": "cm",
        "axes.unicode_minus": False,
    })

    j = np.arange(len(result["angle_deg"]))
    angle = result["angle_deg"]

    fig, ax = plt.subplots(figsize=(10.5, 4.8), dpi=180)
    ax.plot(j, angle, color="#1f77b4", linewidth=1.6,
            label=r"$k=0$")
    ax.axhline(90.0, color="#d62728", linestyle="--", linewidth=1.1,
               label=r"$90^\circ$")
    ax.set_xlabel(r"$j\;(\xi\ \mathrm{computational\ coordinate})$",
                  fontsize=13)
    ax.set_ylabel(r"$\theta_{\xi\zeta}\;(\mathrm{degree})$", fontsize=13)
    ax.tick_params(axis="both", which="major", labelsize=11)
    ax.grid(True, linewidth=0.35, alpha=0.45)
    ax.legend(loc="best", frameon=False, fontsize=11)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def main() -> int:
    # Mesh comes from the project's Output/ folder; results written here
    mesh_path = find_unique_mesh(OUTPUT_DIR)
    out_dat = os.path.join(VERIFY_DIR, "7.wall_orthogonality_k0_j_angle.dat")
    out_png = os.path.join(VERIFY_DIR, "7.wall_orthogonality_k0_j_angle.png")

    print(f"input mesh: {mesh_path}")
    y_2d, z_2d, J, K = parse_tecplot_2d_mesh(mesh_path)
    if K < 7:
        raise ValueError(f"need K >= 7 for 6th-order wall derivative, got K={K}")

    result = compute_bottom_wall_angle(y_2d, z_2d)
    write_dat(out_dat, result)
    write_plot(out_png, result)

    angle = result["angle_deg"]
    dev = result["deviation_deg"]
    cos_theta = result["cos_theta"]
    j_max = int(np.argmax(np.abs(dev)))

    print(f"output dat : {out_dat}")
    print(f"output plot: {out_png}")
    print("summary:")
    print(f"  angle range          = [{angle.min():.9f}, {angle.max():.9f}] deg")
    print(f"  max |angle - 90 deg| = {np.abs(dev).max():.9f} deg at j={j_max}")
    print(f"  mean |angle - 90|    = {np.abs(dev).mean():.9f} deg")
    print(f"  max |cos(theta)|     = {np.abs(cos_theta).max():.9e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
