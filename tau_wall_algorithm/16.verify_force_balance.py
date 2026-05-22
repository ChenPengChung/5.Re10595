#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
16.verify_force_balance.py
===========================

Verify the FULL force balance for periodic-hill flow:

    F_body_y + F_vis_y + F_pressure_y = 0

where:
    F_body_y    = +<Force> × V    (time-averaged Force from monitor file)
    F_vis_y     = ∫ tau * t_y dA  (global e_y wall force, from step 14)
                  tau = niu * du_t/dn (lattice stress); step 4 already
                  rescaled VTK velocity to physical lattice units (×Uref).
    F_pressure_y is computed from the pressure traction projected onto global
                 e_y before integration.

IMPORTANT: P_mean in VTK is the TIME-AVERAGED gauge pressure accumulated
over the statistics period.  By default this script detects that period from
the first monitor row with accu_cnt > 0.  For consistency, F_body must use the
TIME-AVERAGED Force over the same period, NOT the instantaneous (final) Force
from metadata.  For turbulent/unsteady flows the force controller oscillates,
making the final Force a poor estimate of <Force>.

P_mean in VTK = time-averaged gauge pressure: <p> = <ρ/3 − 1/3>  (code units)
"""

from __future__ import annotations
import argparse, glob, os, re, sys, time
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "Reference"))

from phase1_common import (
    parse_tecplot_2d_mesh,
    map_vtk_sections,
    read_scalar_full,
    parse_dimensions,
    auto_detect_variables_h,
    find_const,
    parse_header_constants,
    detect_ftt_start_from_monitor,
    parse_monitor_force_avg,
    verify_lattice_tau_dat,
)

INPUT_DIR  = os.path.join(_HERE, "Input")
OUTPUT_DIR = os.path.join(_HERE, "Output")


def parse_metadata(path: str) -> dict:
    out = {}
    with open(path) as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                out[k.strip()] = v.strip()
    return out


def parse_scalar_from_dat(path: str, key: str) -> float:
    with open(path) as f:
        for line in f:
            if line.startswith(key) and "=" in line:
                return float(line.split("=")[1].strip())
    raise KeyError(f"{key!r} not found in {path}")


def parse_optional_scalar_from_dat(path: str, key: str):
    try:
        return parse_scalar_from_dat(path, key)
    except KeyError:
        return None


def find_one(pattern: str, label: str) -> str:
    hits = sorted(glob.glob(pattern))
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise FileNotFoundError(f"no {label} matching {pattern}")
    raise FileNotFoundError(f"multiple {label} files matching {pattern}: {hits}")


def find_vtk(output_folder: str, input_folder: str) -> str:
    out_hits = sorted(glob.glob(os.path.join(output_folder, "1.*_v2.vtk")))
    if len(out_hits) == 1:
        return out_hits[0]
    if len(out_hits) > 1:
        raise FileNotFoundError(
            f"multiple transformed VTK files in {output_folder}: {out_hits}")
    return find_one(os.path.join(input_folder, "*.vtk"), "raw VTK")


def resolve_constants(args) -> dict:
    var_h = args.variables_h or auto_detect_variables_h(
        os.path.join(_HERE, "Input"))
    consts = parse_header_constants(var_h) if var_h else {}

    if args.Uref is not None:
        Uref, Uref_src = args.Uref, "CLI --Uref"
    else:
        Uref = find_const(consts, ["Uref", "U_ref"], var_h or "variables.h")
        Uref_src = f"file {var_h}"

    if args.LX is not None:
        LX, LX_src = args.LX, "CLI --lx"
    else:
        LX = find_const(consts, ["LX"], var_h or "variables.h")
        LX_src = f"file {var_h}"

    LY = find_const(consts, ["LY"], var_h or "variables.h")

    ftt_stats = args.ftt_stats_start

    return dict(Uref=Uref, Uref_src=Uref_src,
                LX=LX, LX_src=LX_src, LY=LY,
                ftt_stats_start=ftt_stats)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Verify full force balance including viscous and pressure drag.")
    ap.add_argument("--input-folder", default=INPUT_DIR,
                    help="folder containing Re*_metadata.dat and raw VTK fallback")
    ap.add_argument("--output-folder", default=OUTPUT_DIR,
                    help="folder containing numbered pipeline outputs")
    ap.add_argument("--variables-h", default=None,
                    help="Input/variables.h (default: auto-detect)")
    ap.add_argument("--Uref", type=float, default=None,
                    help="reference velocity override (default: Uref from variables.h)")
    ap.add_argument("--lx", dest="LX", type=float, default=None,
                    help="spanwise length override (default: LX from variables.h)")
    ap.add_argument("--metadata", default=None,
                    help="explicit Re*_metadata.dat path")
    ap.add_argument("--fvis", default=None,
                    help="explicit 32.Re*_total_Fvics.dat path")
    ap.add_argument("--fbody", default=None,
                    help="explicit 33.Re*_Fbody_volume.dat path")
    ap.add_argument("--mesh", default=None,
                    help="explicit 2.* mesh dat path")
    ap.add_argument("--vtk", default=None,
                    help="explicit transformed or raw VTK path")
    ap.add_argument("--monitor", default=None,
                    help="Ustar_Force_record.dat path for time-averaged Force")
    ap.add_argument("--ftt-stats-start", type=float, default=None,
                    help=("FTT when statistics started; default auto-detects "
                          "the first monitor row with accu_cnt > 0"))
    args = ap.parse_args(argv)

    input_folder = args.input_folder
    output_folder = args.output_folder

    rc = resolve_constants(args)
    Uref = rc["Uref"]; LX = rc["LX"]; LY = rc["LY"]
    ftt_stats_start = rc["ftt_stats_start"]
    print(f"Uref       = {Uref:.12e}  (source: {rc['Uref_src']})")
    print(f"LX         = {LX:.12e}  (source: {rc['LX_src']})")
    print(f"LY         = {LY:.12e}")

    # ── [1] Read metadata ──
    meta_path = args.metadata or find_one(
        os.path.join(input_folder, "Re*_metadata.dat"), "metadata")
    meta = parse_metadata(meta_path)
    Force_final = float(meta["Force"])
    dt_global = float(meta["dt_global"])
    print(f"metadata   = {meta_path}")
    print(f"Force(final) = {Force_final:.12e}")
    print(f"dt_global  = {dt_global:.12e}")

    # ── [1b] Time-averaged Force from monitor file ──
    monitor_path = args.monitor
    if monitor_path is None:
        hits = sorted(glob.glob(os.path.join(input_folder,
                                             "*_Ustar_Force_record.dat")))
        if not hits:
            parent = os.path.dirname(os.path.dirname(
                os.path.abspath(output_folder)))
            hits = sorted(glob.glob(os.path.join(parent,
                                                 "*_Ustar_Force_record.dat")))
        if hits:
            monitor_path = hits[-1]

    Force_avg = None
    if monitor_path and os.path.isfile(monitor_path):
        print(f"\nmonitor    = {monitor_path}")
        mon = parse_monitor_force_avg(monitor_path, ftt_stats_start, Uref, LY)
        Force_avg = mon["Force_avg"]
        ftt_start_used = mon["ftt_start_used"]
        if ftt_stats_start is None:
            print(f"FTT stats start = {ftt_start_used:.6f}  "
                  f"(auto-detected from accu_cnt)")
        else:
            print(f"FTT stats start = {ftt_start_used:.6f}  (CLI override)")
        print(f"  samples        = {mon['n_samples']}")
        print(f"  FTT range      = [{mon['ftt_min']:.2f}, {mon['ftt_max']:.2f}]")
        print(f"  Force* range   = [{mon['Force_star_min']:+.6f}, "
              f"{mon['Force_star_max']:+.6f}]")
        print(f"  <Force*>       = {mon['Force_star_avg']:+.12e}")
        print(f"  <Force>        = {Force_avg:+.12e}")
        print(f"  Force(final)   = {Force_final:+.12e}")
        ratio = Force_avg / Force_final if abs(Force_final) > 0 else float("inf")
        print(f"  ratio avg/final = {ratio:.4f}")
    else:
        print(f"\n[warn] no monitor file found — using final metadata Force")

    Force = Force_avg if Force_avg is not None else Force_final

    # ── [2] Read global-y viscous force from step 14 ──
    fvis_path = args.fvis or find_one(
        os.path.join(output_folder, "34.Re*_total_Fvics.dat"), "34 Fvics")
    verify_lattice_tau_dat(fvis_path, "viscous force input")
    # Step 14 integrates tau = niu * du_t/dn (lattice stress).  Step 4 already
    # rescaled V_mean (= V_lat/Uref in VTK) back to physical lattice velocity
    # before computing u_t, so du_t/dn is lattice/lattice = no further Uref
    # correction is needed when forming the force balance below.
    SUM_F_vis = parse_scalar_from_dat(fvis_path, "SUM_F_vis ")
    F_vis_y_lat_int = parse_optional_scalar_from_dat(fvis_path, "F_vis_y")
    F_vis_bot_y_lat_int = parse_optional_scalar_from_dat(
        fvis_path, "F_vis_bottom_y")
    F_vis_top_y_lat_int = parse_optional_scalar_from_dat(
        fvis_path, "F_vis_top_y")

    # Backward compatibility with older 32.dat files:
    # bottom legacy key is tau*t_y integral, so wall-on-fluid force is negative;
    # top legacy key already has the wall-on-fluid y sign.
    if F_vis_y_lat_int is None:
        F_bot_signed = parse_scalar_from_dat(fvis_path, "F_vis_bottom_signed")
        F_top_signed = parse_scalar_from_dat(fvis_path, "F_vis_top_signed")
        F_vis_bot_y_lat_int = -F_bot_signed
        F_vis_top_y_lat_int = F_top_signed
        F_vis_y_lat_int = F_vis_bot_y_lat_int + F_vis_top_y_lat_int

    print(f"SUM_F_vis  = {SUM_F_vis:.12e}  (drag key from step 14)")
    print(f"  F_vis_bottom_y_lat_int = {F_vis_bot_y_lat_int:+.12e}")
    print(f"  F_vis_top_y_lat_int    = {F_vis_top_y_lat_int:+.12e}")
    print(f"  F_vis_y_lat_int        = {F_vis_y_lat_int:+.12e}")

    # ── [3] Read volume from step 15 ──
    fbody_path = args.fbody or find_one(
        os.path.join(output_folder, "35.Re*_Fbody_volume.dat"), "35 Fbody")
    V_3D = parse_scalar_from_dat(fbody_path, "V_3D_shoelace")
    print(f"V_3D       = {V_3D:.12e}")

    # ── [4] Read 2D mesh ──
    mesh_path = args.mesh or find_one(
        os.path.join(output_folder, "2.j*_k*_g*_a*.dat"), "2D mesh")
    print(f"\nReading mesh: {mesh_path}")
    y2d, z2d, J_mesh, K_mesh = parse_tecplot_2d_mesh(mesh_path)
    print(f"  mesh shape (J, K) = ({J_mesh}, {K_mesh})")

    # parse_tecplot_2d_mesh returns shape (K, J): first axis = wall-normal
    y_bot = y2d[0, :]    # bottom wall y-coordinates (J_mesh,)
    z_bot = z2d[0, :]    # bottom wall z-coordinates (J_mesh,)
    y_top = y2d[-1, :]
    z_top = z2d[-1, :]

    print(f"  z_bot range: [{z_bot.min():.4f}, {z_bot.max():.4f}]  (hill: 0 to H_HILL)")
    print(f"  z_top range: [{z_top.min():.6f}, {z_top.max():.6f}]  (should be flat ~ LZ)")

    # ── [5] Read P_mean from VTK ──
    vtk_path = args.vtk or find_vtk(output_folder, input_folder)
    print(f"\nReading VTK: {vtk_path}")

    NX, NY, NZ = parse_dimensions(vtk_path)
    n_total = NX * NY * NZ
    print(f"  dimensions: NX={NX}, NY={NY}, NZ={NZ}  ({n_total:,} points)")

    t0 = time.time()
    print("  scanning sections ...")
    sections = map_vtk_sections(vtk_path)
    print(f"    found {len(sections)} sections ({time.time()-t0:.1f}s)")

    if "SCALARS:P_mean" not in sections:
        avail = [k for k in sections if k.startswith("SCALARS:")]
        print(f"[ERROR] P_mean not found.  Available SCALARS: {avail}")
        return 1

    t0 = time.time()
    print("  reading P_mean ...")
    P_flat = read_scalar_full(vtk_path, sections, "P_mean", n_total)
    print(f"    done ({time.time()-t0:.1f}s)")

    # VTK order: i-fast, j-mid, k-slow -> reshape to (NZ, NY, NX)
    P_3d = P_flat.reshape(NZ, NY, NX)

    # ── [6] P_mean near walls, spanwise average ──
    # Wall nodes (k=0, k=NZ-1) have P_mean=0 because the statistics
    # kernel skips boundary points.  Wall BC uses dp/dn=0 (Imamura),
    # so we extrapolate to the wall from interior layers.
    #
    # Method A: use k=1 directly (1st-order, dp/dn=0 justification)
    # Method B: quadratic extrapolation from k=1,2,3 to k=0
    #           P_wall = (15*P1 - 10*P2 + 3*P3) / 8  (dp/dn=0 + quadratic fit)

    P_bot_k1 = P_3d[1, :, :].mean(axis=1)   # (NY,)
    P_bot_k2 = P_3d[2, :, :].mean(axis=1)
    P_bot_k3 = P_3d[3, :, :].mean(axis=1)

    P_top_k1 = P_3d[-2, :, :].mean(axis=1)
    P_top_k2 = P_3d[-3, :, :].mean(axis=1)
    P_top_k3 = P_3d[-4, :, :].mean(axis=1)

    P_bot_avg_A = P_bot_k1
    P_bot_avg_B = (15*P_bot_k1 - 10*P_bot_k2 + 3*P_bot_k3) / 8.0

    P_top_avg_A = P_top_k1
    P_top_avg_B = (15*P_top_k1 - 10*P_top_k2 + 3*P_top_k3) / 8.0

    print(f"\n  P_mean near bottom wall (spanwise avg):")
    print(f"    k=1:  [{P_bot_k1.min():.6e}, {P_bot_k1.max():.6e}]")
    print(f"    k=2:  [{P_bot_k2.min():.6e}, {P_bot_k2.max():.6e}]")
    print(f"    k=3:  [{P_bot_k3.min():.6e}, {P_bot_k3.max():.6e}]")
    print(f"    extrap: [{P_bot_avg_B.min():.6e}, {P_bot_avg_B.max():.6e}]")

    p_abs_mean = np.abs(P_bot_k1).mean()
    if p_abs_mean > 0:
        print(f"    spanwise variation (k=1): std/mean = "
              f"{P_3d[1,:,:].std(axis=1).mean() / p_abs_mean:.4e}")

    # ── [7] Compute pressure force in global e_y ──
    # Bottom outward area vector has n_y dA = +dz*dx, so pressure force on
    # the fluid is -p*n_y*dA = -p*dz*dx.
    # Top outward area vector has n_y dA = -dz*dx, so pressure force on
    # the fluid is +p*dz*dx.  Top is flat here, so this should be ~0.

    dz_bot = np.diff(z_bot)                          # (J_mesh-1,)
    dz_top = np.diff(z_top)

    def pressure_integral_p_dz(P_avg, dz):
        P_cell = 0.5 * (P_avg[:-1] + P_avg[1:])
        return float(np.sum(P_cell * dz))

    Pdz2d_bot_A = pressure_integral_p_dz(P_bot_avg_A, dz_bot)
    Pdz2d_bot_B = pressure_integral_p_dz(P_bot_avg_B, dz_bot)
    Pdz2d_top_A = pressure_integral_p_dz(P_top_avg_A, dz_top)
    Pdz2d_top_B = pressure_integral_p_dz(P_top_avg_B, dz_top)

    print(f"\n  Pressure projected integral, per unit span (sum P*dz):")
    print(f"    bot (k=1 only) = {Pdz2d_bot_A:+.12e}")
    print(f"    bot (extrap)   = {Pdz2d_bot_B:+.12e}")
    print(f"    top (k=1 only) = {Pdz2d_top_A:+.12e}  (expect ~0)")
    print(f"    top (extrap)   = {Pdz2d_top_B:+.12e}")

    F_pressure_bot_y_A = -Pdz2d_bot_A * LX
    F_pressure_bot_y_B = -Pdz2d_bot_B * LX
    F_pressure_top_y_A = +Pdz2d_top_A * LX
    F_pressure_top_y_B = +Pdz2d_top_B * LX

    print(f"\n  Pressure force on fluid, global e_y (3D):")
    print(f"    bot (k=1)    = {F_pressure_bot_y_A:+.12e}")
    print(f"    bot (extrap) = {F_pressure_bot_y_B:+.12e}")
    print(f"    top (k=1)    = {F_pressure_top_y_A:+.12e}")
    print(f"    top (extrap) = {F_pressure_top_y_B:+.12e}")

    # ── [8] Viscous force in kinematic (code) units ──
    # Step 14 writes ∫ tau * t_y dA where tau = niu * du_t/dn (lattice stress).
    # Step 4 already rescaled VTK velocity to physical lattice units.
    # No extra correction needed here.
    F_vis_y = F_vis_y_lat_int
    F_vis_bottom_y = F_vis_bot_y_lat_int
    F_vis_top_y = F_vis_top_y_lat_int
    D_vis = -F_vis_y

    # ── [9] Force balance check ──
    F_body_y = Force * V_3D
    force_label = "<Force> (time-avg)" if Force_avg is not None else "Force (final)"

    print(f"\n{'='*60}")
    print(f"  FORCE BALANCE VERIFICATION")
    print(f"  using {force_label}")
    print(f"{'='*60}")
    print(f"  Force used               = {Force:+.12e}  ({force_label})")
    if Force_avg is not None:
        print(f"  Force (final, metadata)  = {Force_final:+.12e}")
    print(f"  F_body_y = Force × V     = {F_body_y:+.12e}")
    print(f"  F_vis_bottom_y           = {F_vis_bottom_y:+.12e}")
    print(f"  F_vis_top_y              = {F_vis_top_y:+.12e}")
    print(f"  F_vis_y                  = {F_vis_y:+.12e}")
    print(f"  D_vis (= -F_vis_y)       = {D_vis:+.12e}")
    print()

    methods = [
        ("Method A (k=1 only)", F_pressure_bot_y_A, F_pressure_top_y_A),
        ("Method B (extrap)",   F_pressure_bot_y_B, F_pressure_top_y_B),
    ]
    results = []
    for label, F_pb, F_pt in methods:
        F_pressure_y = F_pb + F_pt
        residual = F_body_y + F_vis_y + F_pressure_y
        drag_y = -(F_vis_y + F_pressure_y)
        err = residual / F_body_y * 100
        results.append(dict(
            label=label, F_pb=F_pb, F_pt=F_pt,
            F_pressure_y=F_pressure_y, residual=residual,
            drag_y=drag_y, err=err,
        ))

        print(f"  --- {label} ---")
        print(f"    F_pressure_bottom_y     = {F_pb:+.12e}")
        print(f"    F_pressure_top_y        = {F_pt:+.12e}")
        print(f"    F_pressure_y            = {F_pressure_y:+.12e}")
        print(f"    -(F_vis_y+F_pressure_y) = {drag_y:+.12e}")
        print(f"    residual F_body+Fvis+Fp = {residual:+.12e}  (err = {err:+.2f}%)")
        print(f"    D_vis / F_body          = {D_vis / F_body_y * 100:.2f}%")
        print(f"    (-F_pressure_y)/F_body  = {-F_pressure_y / F_body_y * 100:.2f}%")
        print()

    # ── [10] Write 34.dat ──
    m = re.search(r"Re(\d+)", os.path.basename(fvis_path))
    if not m:
        m = re.search(r"Re(\d+)", os.path.basename(
            args.metadata or meta_path))
    Re = int(m.group(1)) if m else 0

    best = min(results, key=lambda r: abs(r["err"]))

    out_path = os.path.join(output_folder, f"36.Re{Re}_force_balance.dat")
    with open(out_path, "w") as f:
        f.write("# Force balance verification: F_body_y + F_vis_y + F_pressure_y = 0\n")
        f.write("# All quantities in code (kinematic) units.\n")
        f.write(f"# Fvics source  : {os.path.basename(fvis_path)}\n")
        f.write(f"# Fbody source  : {os.path.basename(fbody_path)}\n")
        f.write(f"# mesh source   : {os.path.basename(mesh_path)}\n")
        f.write(f"# VTK source    : {os.path.basename(vtk_path)}\n")
        if monitor_path:
            f.write(f"# monitor source: {os.path.basename(monitor_path)}\n")
        f.write(f"# Uref          = {Uref:.12e}\n")
        f.write(f"# LX            = {LX:.12e}\n")
        f.write("#\n")
        f.write(f"Re                       = {Re}\n")
        f.write(f"Force_final              = {Force_final:+.12e}\n")
        if Force_avg is not None:
            f.write(f"Force_time_avg           = {Force_avg:+.12e}\n")
            f.write(f"Force_avg_final_ratio    = {Force_avg / Force_final if abs(Force_final) > 0 else float('inf'):+.4f}\n")
        f.write(f"Force_used               = {Force:+.12e}  # {'time-avg' if Force_avg else 'final'}\n")
        f.write(f"F_body_y                 = {F_body_y:+.12e}\n")
        f.write("\n")
        f.write(f"F_vis_bottom_y           = {F_vis_bottom_y:+.12e}\n")
        f.write(f"F_vis_top_y              = {F_vis_top_y:+.12e}\n")
        f.write(f"F_vis_y                  = {F_vis_y:+.12e}\n")
        f.write("\n")
        for r in results:
            tag = "k1" if "k=1" in r["label"] else "extrap"
            f.write(f"# --- {r['label']} ---\n")
            f.write(f"F_pressure_bottom_y_{tag} = {r['F_pb']:+.12e}\n")
            f.write(f"F_pressure_top_y_{tag}    = {r['F_pt']:+.12e}\n")
            f.write(f"F_pressure_y_{tag}        = {r['F_pressure_y']:+.12e}\n")
            f.write(f"residual_{tag}            = {r['residual']:+.12e}\n")
            f.write(f"residual_pct_{tag}        = {r['err']:+.4f}\n")
            f.write(f"D_vis_pct_{tag}           = {D_vis / F_body_y * 100:.4f}\n")
            f.write(f"D_pressure_pct_{tag}      = {-r['F_pressure_y'] / F_body_y * 100:.4f}\n")
            f.write("\n")

        f.write(f"best_method              = {best['label']}\n")
        f.write(f"best_residual_pct        = {best['err']:+.4f}\n")

    print(f"  saved -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
