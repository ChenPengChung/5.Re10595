# -*- coding: utf-8 -*-
"""
0.run_pipeline.py
=================

Single entry point for the full post-processing pipeline.

Inputs:
    exactly one raw .vtk file and exactly one raw .dat mesh file

Default input location:
    Input/

Outputs:
    the existing numbered artifacts in Output/ (1..42)

Usage:
    python 0.run_pipeline.py
    python 0.run_pipeline.py --raw-dir raw_data
    python 0.run_pipeline.py --vtk raw_data/case.vtk --dat raw_data/mesh.dat

The controller passes explicit file paths between stages, so stale files in
Output/ are not used as implicit inputs.  Use --clean-output only when you
want to remove previous numbered pipeline outputs before running.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Sequence


ROOT = Path(__file__).resolve().parent
INPUT_DIR = ROOT / "Input"
OUTPUT_DIR = ROOT / "Output"
REFERENCE_DIR = ROOT / "Reference"


PIPELINE = [
    ("1", "1.phase1_transvtk.py"),
    ("2", "2.phase1_transdat.py"),
    ("3", "3.phase2_compute_uxi.py"),
    ("4", "4.phase2_computeutangent.py"),
    ("5", "5.phase2_compute_tauwall.py"),
    ("6", "6.phase2_compute_tauglobal.py"),
    ("7", "7.phase2_grid_delta.py"),
    ("8", "8.phase2_compute_zplus_1D2A.py"),
    ("9", "9.phase3_plot_zplus.py"),
    ("10", "10.phase3_compute_zplus_1A2D.py"),
    ("11", "11.phase3_plot_zplus_spanavg.py"),
    ("12", "12.phase3_compute_zplus_2nd1A2D.py"),
    ("13", "13.phase3_plot_zplus_spanavg_2nd.py"),
    ("14", "14.phase4_compute_total_drag.py"),
    ("15", "15.phase4_compute_Fbody.py"),
    ("16", "16.verify_force_balance.py"),
]


def fail(message: str) -> None:
    print(f"[error] {message}", file=sys.stderr)
    raise SystemExit(1)


def resolve_existing_file(path: str | Path, label: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    else:
        p = p.resolve()
    if not p.is_file():
        fail(f"{label} not found: {p}")
    return p


def resolve_existing_dir(path: str | Path, label: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    else:
        p = p.resolve()
    if not p.is_dir():
        fail(f"{label} not found: {p}")
    return p


def looks_like_raw_mesh_dat(path: Path) -> bool:
    """Positive filter for the raw 2D mesh DAT, excluding metadata DAT files."""
    name = path.name
    if re.search(r"_metadata\.dat$", name, re.IGNORECASE):
        return False
    if not re.search(r"(?:^|\.)I\d+_J\d+", name, re.IGNORECASE):
        return False
    if not re.search(r"g\d+(?:\.\d+)?", name, re.IGNORECASE):
        return False
    if not re.search(r"a\d+(?:\.\d+)?", name, re.IGNORECASE):
        return False

    header: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                toks = line.split()
                if len(toks) == 2:
                    try:
                        float(toks[0])
                        float(toks[1])
                        break
                    except ValueError:
                        pass
                header.append(line)
                if len(header) >= 50:
                    break
    except OSError:
        return False
    text = " ".join(header)
    return (re.search(r"\bI\s*=\s*\d+", text, re.IGNORECASE) is not None and
            re.search(r"\bJ\s*=\s*\d+", text, re.IGNORECASE) is not None)


def discover_raw_inputs(raw_dir: Path) -> tuple[Path, Path]:
    vtk_files = sorted(p for p in raw_dir.iterdir()
                       if p.is_file() and p.suffix.lower() == ".vtk")
    dat_files = sorted(p for p in raw_dir.iterdir()
                       if p.is_file()
                       and p.suffix.lower() == ".dat"
                       and looks_like_raw_mesh_dat(p))

    if len(vtk_files) != 1:
        found = "\n    ".join(p.name for p in vtk_files) or "(none)"
        fail(f"expected exactly one raw .vtk in {raw_dir}, "
             f"found {len(vtk_files)}:\n    {found}")
    if len(dat_files) != 1:
        found = "\n    ".join(p.name for p in dat_files) or "(none)"
        fail(f"expected exactly one raw .dat in {raw_dir}, "
             f"found {len(dat_files)}:\n    {found}")
    return vtk_files[0].resolve(), dat_files[0].resolve()


def strip_leading_number(stem: str) -> str:
    return re.sub(r"^\d+\.", "", stem)


def parse_re_token(name: str) -> str:
    m = re.search(r"Re\d+", name)
    if not m:
        fail(f"cannot find Re<num> token in filename: {name}")
    return m.group(0)


def parse_re_number(re_token: str) -> int:
    m = re.fullmatch(r"Re(\d+)", re_token)
    if not m:
        fail(f"bad Re token: {re_token}")
    return int(m.group(1))


def parse_vtk_dimensions(path: Path) -> tuple[int, int, int]:
    with path.open("rb") as f:
        for raw in f:
            line = raw.decode("ascii", errors="ignore")
            m = re.search(r"\bDIMENSIONS\s+(\d+)\s+(\d+)\s+(\d+)", line)
            if m:
                return tuple(int(x) for x in m.groups())
    fail(f"cannot find VTK DIMENSIONS in {path}")


def parse_raw_mesh_dat(path: Path) -> tuple[int, int, str, str]:
    header: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            toks = line.split()
            if len(toks) == 2:
                try:
                    float(toks[0])
                    float(toks[1])
                    break
                except ValueError:
                    pass
            header.append(line)
    text = " ".join(header)
    mi = re.search(r"\bI\s*=\s*(\d+)", text, re.IGNORECASE)
    mj = re.search(r"\bJ\s*=\s*(\d+)", text, re.IGNORECASE)
    if not mi or not mj:
        fail(f"cannot parse I/J dimensions from raw mesh dat: {path}")
    g = re.search(r"g(\d+(?:\.\d+)?)", path.name)
    a = re.search(r"a(\d+(?:\.\d+)?)", path.name)
    if not g or not a:
        fail(f"raw mesh dat filename must contain g<value> and a<value>: "
             f"{path.name}")
    return int(mi.group(1)), int(mj.group(1)), g.group(1), a.group(1)


def mesh_stem(mesh_path: Path) -> str:
    m = re.match(r"^2\.(.+)\.dat$", mesh_path.name, re.IGNORECASE)
    if not m:
        fail(f"internal mesh path does not match 2.<stem>.dat: {mesh_path}")
    return m.group(1)


def run_command(label: str, script: str, args: Sequence[Path | str],
                dry_run: bool) -> None:
    script_path = ROOT / script
    if not script_path.is_file():
        fail(f"pipeline script missing: {script_path}")

    cmd = [sys.executable, str(script_path), *[str(a) for a in args]]
    print("\n" + "=" * 78, flush=True)
    print(f"[{label}] {script}", flush=True)
    print(" ".join(f'"{c}"' if " " in c else c for c in cmd), flush=True)
    print("=" * 78, flush=True)
    if dry_run:
        return

    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        fail(f"{script} failed with exit code {proc.returncode}")


def assert_outputs(paths: Iterable[Path], dry_run: bool) -> None:
    if dry_run:
        return
    missing = [p for p in paths if not p.is_file()]
    if missing:
        fail("expected output(s) were not produced:\n    " +
             "\n    ".join(str(p) for p in missing))


def clean_output_dir() -> None:
    out = OUTPUT_DIR.resolve()
    root = ROOT.resolve()
    try:
        out.relative_to(root)
    except ValueError:
        fail(f"refusing to clean output outside workspace: {out}")

    if not out.is_dir():
        return
    generated = [p for p in out.iterdir()
                 if p.is_file() and re.match(r"^(?:[1-9]|[1-3]\d|4[0-2])\.", p.name)]
    for p in generated:
        p.unlink()
    print(f"[clean] removed {len(generated)} numbered pipeline output file(s)",
          flush=True)


def discover_metadata(raw_dir: Path, re_tok: str) -> Path:
    """Find Re<num>_metadata.dat in *raw_dir*."""
    expected = raw_dir / f"{re_tok}_metadata.dat"
    if expected.is_file():
        return expected.resolve()
    hits = sorted(raw_dir.glob("Re*_metadata.dat"))
    if len(hits) == 1:
        return hits[0].resolve()
    if not hits:
        fail(f"no Re*_metadata.dat found in {raw_dir} "
             f"(needed for phase 4 force balance)")
    fail(f"multiple Re*_metadata.dat in {raw_dir}: "
         + ", ".join(h.name for h in hits))


def build_expected_paths(raw_vtk: Path, raw_dat: Path) -> dict[str, Path]:
    re_tok = parse_re_token(raw_vtk.name)
    re_num = parse_re_number(re_tok)
    nx, ny, nz = parse_vtk_dimensions(raw_vtk)
    j_raw, k_raw, g, a = parse_raw_mesh_dat(raw_dat)
    if (j_raw, k_raw) != (ny, nz):
        fail(f"raw mesh dimensions I/J=({j_raw},{k_raw}) do not match "
             f"VTK stream/normal dimensions Ny/Nz=({ny},{nz})")

    vtk_stem = strip_leading_number(raw_vtk.stem)
    mesh_name = f"2.j{j_raw}_k{k_raw}_g{g}_a{a}.dat"
    mesh2 = OUTPUT_DIR / mesh_name
    mstem = mesh_stem(mesh2)

    return {
        "vtk_v2": OUTPUT_DIR / f"1.{vtk_stem}_v2{raw_vtk.suffix}",
        "mesh2": mesh2,
        "uxi_vtk": OUTPUT_DIR / f"3.{re_tok}_uxi_{nx}x{ny}x{nz}.vtk",
        "metric4": OUTPUT_DIR / f"4.{re_tok}_inverseJacobian_j{ny}_k{nz}.dat",
        "utan_bot5": OUTPUT_DIR / f"5.{re_tok}_utan_i{nx}_j{ny}_k0-6.dat",
        "utan_top6": OUTPUT_DIR / f"6.{re_tok}_utan_i{nx}_j{ny}_k{nz-7}-{nz-1}.dat",
        "tau_bot7": OUTPUT_DIR / f"7.{re_tok}_i{nx}j{ny}_bottomtauwall.dat",
        "tau_top8": OUTPUT_DIR / f"8.{re_tok}_i{nx}j{ny}_toptauwall.dat",
        "tau_global9": OUTPUT_DIR / f"9.{re_tok}_tauwall_global.dat",
        "delta10": OUTPUT_DIR / f"10.{mstem}_delta.dat",
        "extrema11": OUTPUT_DIR / f"11.{mstem}_delta_extrema.txt",
        "vtk13": OUTPUT_DIR / f"13.Re{re_num}_Deltay_Deltaz.vtk",
        "zsum14": OUTPUT_DIR / f"14.Re{re_num}_zplus_summary.txt",
        "zbot15": OUTPUT_DIR / f"15.Re{re_num}_zplus_bottom.dat",
        "ztop16": OUTPUT_DIR / f"16.Re{re_num}_zplus_top.dat",
        "znorm17": OUTPUT_DIR / f"17.Re{re_num}_zplus_bottom_normal.dat",
        "plot18_pdf": OUTPUT_DIR / f"18.Re{re_num}_zplus_streamwise_1D2A.pdf",
        "plot18_png": OUTPUT_DIR / f"18.Re{re_num}_zplus_streamwise_1D2A.png",
        "span19": OUTPUT_DIR / f"19.Re{re_num}_utan_spanavg_j{ny}_k0-6.dat",
        "span20": OUTPUT_DIR / f"20.Re{re_num}_utan_spanavg_j{ny}_k{nz-7}-{nz-1}.dat",
        "tau1d21": OUTPUT_DIR / f"21.Re{re_num}_j{ny}_bottomtauwall_spanavg.dat",
        "tau1d22": OUTPUT_DIR / f"22.Re{re_num}_j{ny}_toptauwall_spanavg.dat",
        "z1d23": OUTPUT_DIR / f"23.Re{re_num}_j{ny}_zplus_bottom_spanavg.dat",
        "z1d24": OUTPUT_DIR / f"24.Re{re_num}_j{ny}_zplus_top_spanavg.dat",
        "z1d25": OUTPUT_DIR / f"25.Re{re_num}_j{ny}_zplus_bottom_normal_spanavg.dat",
        "plot26_pdf": OUTPUT_DIR / f"26.Re{re_num}_zplus_streamwise_1A2D.pdf",
        "plot26_png": OUTPUT_DIR / f"26.Re{re_num}_zplus_streamwise_1A2D.png",
        "tau2d27": OUTPUT_DIR / f"27.Re{re_num}_j{ny}_bottomtauwall_spanavg_2nd.dat",
        "tau2d28": OUTPUT_DIR / f"28.Re{re_num}_j{ny}_toptauwall_spanavg_2nd.dat",
        "z2d29": OUTPUT_DIR / f"29.Re{re_num}_j{ny}_zplus_bottom_spanavg_2nd.dat",
        "z2d30": OUTPUT_DIR / f"30.Re{re_num}_j{ny}_zplus_top_spanavg_2nd.dat",
        "z2d31": OUTPUT_DIR / f"31.Re{re_num}_j{ny}_zplus_bottom_normal_spanavg_2nd.dat",
        "plot32_pdf": OUTPUT_DIR / f"32.Re{re_num}_zplus_streamwise_2nd1A2D.pdf",
        "plot32_png": OUTPUT_DIR / f"32.Re{re_num}_zplus_streamwise_2nd1A2D.png",
        "utau37": OUTPUT_DIR / f"37.Re{re_num}_utau_spanavg_bottom_1D2A.txt",
        "utau38": OUTPUT_DIR / f"38.Re{re_num}_utau_spanavg_top_1D2A.txt",
        "utau39": OUTPUT_DIR / f"39.Re{re_num}_utau_1d_bottom_1A2D.txt",
        "utau40": OUTPUT_DIR / f"40.Re{re_num}_utau_1d_top_1A2D.txt",
        "utau41": OUTPUT_DIR / f"41.Re{re_num}_utau_1d_bottom_2nd1A2D.txt",
        "utau42": OUTPUT_DIR / f"42.Re{re_num}_utau_1d_top_2nd1A2D.txt",
        "fvis34": OUTPUT_DIR / f"34.Re{re_num}_total_Fvics.dat",
        "fbody35": OUTPUT_DIR / f"35.Re{re_num}_Fbody_volume.dat",
        "balance36": OUTPUT_DIR / f"36.Re{re_num}_force_balance.dat",
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run the full 16-stage Utau pipeline from one raw VTK and "
                    "one raw mesh DAT.")
    p.add_argument("--raw-dir", default=str(INPUT_DIR),
                   help="directory containing exactly one .vtk and one .dat "
                        "(default: Input)")
    p.add_argument("--vtk", default=None,
                   help="explicit raw .vtk input; use together with --dat")
    p.add_argument("--dat", default=None,
                   help="explicit raw mesh .dat input; use together with --vtk")
    p.add_argument("--clean-output", action="store_true",
                   help="remove previous numbered Output/ artifacts before run")
    p.add_argument("--skip-plots", action="store_true",
                   help="run compute stages only; skip stages 9, 11, and 13")
    p.add_argument("--metadata", default=None,
                   help="explicit Re*_metadata.dat path for phase 4 "
                        "(default: auto-detect from --raw-dir)")
    p.add_argument("--monitor", default=None,
                   help="Ustar_Force_record.dat for time-averaged Force "
                        "(default: auto-detect in parent directory)")
    p.add_argument("--dry-run", action="store_true",
                   help="print commands and expected outputs without running")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    OUTPUT_DIR.mkdir(exist_ok=True)

    if (args.vtk is None) != (args.dat is None):
        fail("use both --vtk and --dat, or use neither and rely on --raw-dir")

    if args.vtk and args.dat:
        raw_vtk = resolve_existing_file(args.vtk, "raw VTK")
        raw_dat = resolve_existing_file(args.dat, "raw DAT")
    else:
        raw_dir = resolve_existing_dir(args.raw_dir, "raw input directory")
        raw_vtk, raw_dat = discover_raw_inputs(raw_dir)

    for _, script in PIPELINE:
        if not (ROOT / script).is_file():
            fail(f"pipeline script missing: {ROOT / script}")

    if args.clean_output and not args.dry_run:
        clean_output_dir()

    paths = build_expected_paths(raw_vtk, raw_dat)
    print("Raw VTK:", raw_vtk, flush=True)
    print("Raw DAT:", raw_dat, flush=True)
    print("Output :", OUTPUT_DIR, flush=True)
    print("Tau convention: tau = niu * du_t/dn (lattice stress, rho=1) "
          "for stages 5, 10, and 12.", flush=True)

    run_command("1/16", "1.phase1_transvtk.py",
                [raw_vtk, "-o", paths["vtk_v2"]], args.dry_run)
    assert_outputs([paths["vtk_v2"]], args.dry_run)

    run_command("2/16", "2.phase1_transdat.py",
                [raw_dat, "-o", paths["mesh2"]], args.dry_run)
    assert_outputs([paths["mesh2"]], args.dry_run)

    run_command("3/16", "3.phase2_compute_uxi.py",
                ["--vtk", paths["vtk_v2"], "--dat", paths["mesh2"]],
                args.dry_run)
    assert_outputs([paths["uxi_vtk"], paths["metric4"]], args.dry_run)

    run_command("4/16", "4.phase2_computeutangent.py",
                ["--vtk", paths["vtk_v2"], "--dat", paths["mesh2"]],
                args.dry_run)
    assert_outputs([paths["utan_bot5"], paths["utan_top6"]], args.dry_run)

    run_command("5/16", "5.phase2_compute_tauwall.py",
                ["--bot", paths["utan_bot5"], "--top", paths["utan_top6"]],
                args.dry_run)
    assert_outputs([paths["tau_bot7"], paths["tau_top8"]], args.dry_run)

    run_command("6/16", "6.phase2_compute_tauglobal.py",
                ["--bot", paths["tau_bot7"], "--top", paths["tau_top8"]],
                args.dry_run)
    assert_outputs([paths["tau_global9"]], args.dry_run)

    run_command("7/16", "7.phase2_grid_delta.py",
                ["--mesh", paths["mesh2"]], args.dry_run)
    assert_outputs([paths["delta10"], paths["extrema11"]], args.dry_run)

    run_command("8/16", "8.phase2_compute_zplus_1D2A.py",
                ["--mesh", paths["mesh2"],
                 "--bot", paths["tau_bot7"],
                 "--top", paths["tau_top8"],
                 "--global", paths["tau_global9"],
                 "--extrema", paths["extrema11"]],
                args.dry_run)
    assert_outputs([paths["zsum14"], paths["zbot15"], paths["ztop16"],
                    paths["znorm17"],
                    paths["utau37"], paths["utau38"]], args.dry_run)

    if not args.skip_plots:
        run_command("9/16", "9.phase3_plot_zplus.py",
                    ["--folder", OUTPUT_DIR,
                     "--bot", paths["zbot15"],
                     "--top", paths["ztop16"],
                     "--normal", paths["znorm17"]],
                    args.dry_run)
        assert_outputs([paths["plot18_pdf"], paths["plot18_png"]],
                       args.dry_run)

    run_command("10/16", "10.phase3_compute_zplus_1A2D.py",
                ["--bot", paths["utan_bot5"],
                 "--top", paths["utan_top6"],
                 "--mesh", paths["mesh2"]],
                args.dry_run)
    assert_outputs([paths["span19"], paths["span20"], paths["tau1d21"],
                    paths["tau1d22"], paths["z1d23"], paths["z1d24"],
                    paths["z1d25"],
                    paths["utau39"], paths["utau40"]], args.dry_run)

    if not args.skip_plots:
        run_command("11/16", "11.phase3_plot_zplus_spanavg.py",
                    ["--folder", OUTPUT_DIR,
                     "--bot", paths["z1d23"],
                     "--top", paths["z1d24"],
                     "--normal", paths["z1d25"]],
                    args.dry_run)
        assert_outputs([paths["plot26_pdf"], paths["plot26_png"]],
                       args.dry_run)

    run_command("12/16", "12.phase3_compute_zplus_2nd1A2D.py",
                ["--bot", paths["utan_bot5"],
                 "--top", paths["utan_top6"],
                 "--mesh", paths["mesh2"]],
                args.dry_run)
    assert_outputs([paths["tau2d27"], paths["tau2d28"], paths["z2d29"],
                    paths["z2d30"], paths["z2d31"],
                    paths["utau41"], paths["utau42"]], args.dry_run)

    if not args.skip_plots:
        run_command("13/16", "13.phase3_plot_zplus_spanavg_2nd.py",
                    ["--folder", OUTPUT_DIR,
                     "--bot", paths["z2d29"],
                     "--top", paths["z2d30"],
                     "--normal", paths["z2d31"]],
                    args.dry_run)
        assert_outputs([paths["plot32_pdf"], paths["plot32_png"]],
                       args.dry_run)

    # ── Phase 4: force balance (steps 14-16) ──
    re_tok = parse_re_token(raw_vtk.name)
    if args.metadata:
        meta_path = resolve_existing_file(args.metadata, "metadata")
    else:
        meta_search = resolve_existing_dir(args.raw_dir,
                                           "metadata directory")
        meta_path = discover_metadata(meta_search, re_tok)
    print(f"\nMetadata : {meta_path}", flush=True)

    run_command("14/16", "14.phase4_compute_total_drag.py",
                ["--folder", OUTPUT_DIR,
                 "--bot", paths["tau_bot7"],
                 "--top", paths["tau_top8"]],
                args.dry_run)
    assert_outputs([paths["fvis34"]], args.dry_run)

    step15_args = ["--output-folder", OUTPUT_DIR,
                   "--metadata", meta_path,
                   "--mesh", paths["mesh2"]]
    if args.monitor:
        step15_args += ["--monitor", resolve_existing_file(
            args.monitor, "monitor file")]
    run_command("15/16", "15.phase4_compute_Fbody.py",
                step15_args, args.dry_run)
    assert_outputs([paths["fbody35"]], args.dry_run)

    step16_args = ["--output-folder", OUTPUT_DIR,
                   "--metadata", meta_path,
                   "--fvis", paths["fvis34"],
                   "--fbody", paths["fbody35"],
                   "--mesh", paths["mesh2"],
                   "--vtk", paths["vtk_v2"]]
    if args.monitor:
        step16_args += ["--monitor", resolve_existing_file(
            args.monitor, "monitor file")]
    run_command("16/16", "16.verify_force_balance.py",
                step16_args, args.dry_run)
    assert_outputs([paths["balance36"]], args.dry_run)

    expected = [
        p for key, p in paths.items()
        if not args.skip_plots or not key.startswith(("plot18", "plot26", "plot32"))
    ]
    assert_outputs(expected, args.dry_run)

    print("\nPipeline complete.", flush=True)
    print(f"Produced/verified {len(expected)} output file(s) in {OUTPUT_DIR}.",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
