#!/usr/bin/env python3
"""Track local density perturbations across restart checkpoints.

This script is intentionally checkpoint-based rather than checkrho-based:
checkrho.dat only reports the domain-averaged density, whereas a restart can
inject local pressure/density structure while preserving the global mean.

Outputs
-------
  result/restart_density_audit.csv
      Persistent metric history. Keep this file for the duration of one run;
      use --reset before starting a new restart experiment.
  result/restart_density_audit.{pdf,png}
      Live monitor figure using Times New Roman and vector PDF output.
  result/restart_density_audit.tex
      Standalone PGFPlots source using newtxtext/newtxmath for final LaTeX use.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

import matplotlib as mpl
if not os.environ.get("DISPLAY"):
    mpl.use("Agg")
import matplotlib.pyplot as plt

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
PHASE2_DIR = PROJECT_ROOT / "phase2_generatecheckpoint"
if str(PHASE2_DIR) not in sys.path:
    sys.path.insert(0, str(PHASE2_DIR))

from interp_checkpoint import BFR, GridConfig, auto_detect_from_metadata, parse_metadata, read_rank_bin, stitch_y

DEFAULT_CHECKPOINT_ROOT = PROJECT_ROOT / "restart" / "checkpoint"
DEFAULT_CSV = SCRIPT_DIR / "restart_density_audit.csv"
DEFAULT_PDF = SCRIPT_DIR / "restart_density_audit.pdf"
DEFAULT_PNG = SCRIPT_DIR / "restart_density_audit.png"
DEFAULT_TEX = SCRIPT_DIR / "restart_density_audit.tex"
DEFAULT_LATEX_PDF = SCRIPT_DIR / "restart_density_audit_latex.pdf"

CSV_FIELDS = [
    "kind",
    "path",
    "step",
    "ftt",
    "rho_mean",
    "rho_rms",
    "rho_std",
    "rho_max_abs",
    "wall_rms",
    "wall_max_abs",
]


def configure_plot_style():
    """Publication-oriented Matplotlib fallback for nodes without TeX."""
    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Times New Roman", "Times", "DejaVu Serif"],
        "mathtext.fontset": "stix",
        "font.size": 8,
        "axes.labelsize": 8,
        "axes.titlesize": 8,
        "axes.linewidth": 0.6,
        "xtick.labelsize": 7,
        "ytick.labelsize": 7,
        "xtick.direction": "in",
        "ytick.direction": "in",
        "xtick.major.width": 0.6,
        "ytick.major.width": 0.6,
        "xtick.minor.width": 0.4,
        "ytick.minor.width": 0.4,
        "legend.fontsize": 7,
        "legend.frameon": False,
        "lines.linewidth": 0.9,
        "lines.markersize": 2.5,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "savefig.bbox": "tight",
    })


def checkpoint_step(path: Path) -> int:
    name = path.name
    if name.startswith("step_"):
        try:
            return int(name.split("_", 1)[1])
        except ValueError:
            pass
    meta_path = path / "metadata.dat"
    if meta_path.is_file():
        meta = parse_metadata(str(meta_path))
        return int(float(meta.get("step", 0)))
    return 0


def candidate_restart_dirs(checkpoint_root: Path) -> list[Path]:
    dirs = []
    for path in checkpoint_root.glob("step_*"):
        if not path.is_dir():
            continue
        meta_path = path / "metadata.dat"
        if not meta_path.is_file():
            continue
        detected = auto_detect_from_metadata(str(meta_path))
        if detected is None:
            continue
        jp = int(detected["jp"])
        complete = all(
            (path / f"f{q:02d}_{rank}.bin").is_file()
            for q in range(19)
            for rank in range(jp)
        )
        if not complete:
            continue
        dirs.append(path)
    return sorted(dirs, key=checkpoint_step)


def detect_source_checkpoint(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    candidates = sorted(PHASE2_DIR.glob("oldcheckpoint_*"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def cfg_from_metadata(meta_path: Path) -> GridConfig:
    detected = auto_detect_from_metadata(str(meta_path))
    if detected is None:
        raise ValueError(f"Cannot infer grid dimensions from {meta_path}")
    return GridConfig(
        detected["NX"],
        detected["NY"],
        detected["NZ"],
        detected["jp"],
        gamma=0.0,
        alpha=0.5,
        grid_dat="",
    )


def load_rho(checkpoint_dir: Path, cfg: GridConfig) -> np.ndarray:
    rho = np.zeros((cfg.NY6, cfg.NZ6, cfg.NX6), dtype=np.float64)
    for q in range(19):
        per_rank = [
            read_rank_bin(str(checkpoint_dir / f"f{q:02d}_{rank}.bin"), cfg)
            for rank in range(cfg.JP)
        ]
        rho += stitch_y(per_rank, cfg)
    return rho[BFR:BFR + cfg.NY, BFR:BFR + cfg.NZ, BFR:BFR + cfg.NX]


def metric_row(checkpoint_dir: Path, kind: str) -> dict[str, float | int | str]:
    meta_path = checkpoint_dir / "metadata.dat"
    meta = parse_metadata(str(meta_path))
    cfg = cfg_from_metadata(meta_path)
    rho = load_rho(checkpoint_dir, cfg)
    dev = rho - 1.0
    wall_dev = np.concatenate((dev[:, 0, :].ravel(), dev[:, -1, :].ravel()))
    return {
        "kind": kind,
        "path": str(checkpoint_dir.relative_to(PROJECT_ROOT)),
        "step": int(float(meta.get("step", checkpoint_step(checkpoint_dir)))),
        "ftt": float(meta.get("FTT", math.nan)),
        "rho_mean": float(np.mean(rho)),
        "rho_rms": float(np.sqrt(np.mean(dev * dev))),
        "rho_std": float(np.std(rho)),
        "rho_max_abs": float(np.max(np.abs(dev))),
        "wall_rms": float(np.sqrt(np.mean(wall_dev * wall_dev))),
        "wall_max_abs": float(np.max(np.abs(wall_dev))),
    }


def load_rows(csv_path: Path) -> list[dict[str, str]]:
    if not csv_path.is_file():
        return []
    with csv_path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_rows(csv_path: Path, rows: list[dict[str, float | int | str]]):
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8",
                                     dir=str(csv_path.parent), delete=False) as tmp:
        writer = csv.DictWriter(tmp, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, csv_path)


def normalise_rows(rows: list[dict[str, str]]) -> list[dict[str, float | int | str]]:
    normalised = []
    for row in rows:
        converted: dict[str, float | int | str] = {
            "kind": row["kind"],
            "path": row["path"],
            "step": int(float(row["step"])),
        }
        for key in CSV_FIELDS[3:]:
            converted[key] = float(row[key])
        normalised.append(converted)
    return normalised


def merge_metrics(existing_rows: list[dict[str, float | int | str]],
                  source_dir: Path | None,
                  restart_dirs: list[Path]) -> tuple[list[dict[str, float | int | str]], int]:
    by_key = {(str(row["kind"]), str(row["path"])): row for row in existing_rows}
    added = 0

    if source_dir is not None and (source_dir / "metadata.dat").is_file():
        key = ("source", str(source_dir.relative_to(PROJECT_ROOT)))
        if key not in by_key:
            by_key[key] = metric_row(source_dir, "source")
            added += 1

    for restart_dir in restart_dirs:
        rel = str(restart_dir.relative_to(PROJECT_ROOT))
        key = ("restart", rel)
        if key in by_key:
            continue
        try:
            by_key[key] = metric_row(restart_dir, "restart")
        except (FileNotFoundError, OSError, ValueError):
            # The solver may rotate checkpoint directories while we scan them.
            # Skip this pass; a later stable checkpoint will be picked up.
            continue
        added += 1

    rows = list(by_key.values())
    rows.sort(key=lambda row: (0 if row["kind"] == "source" else 1, float(row["ftt"]), int(row["step"])))
    return rows, added


def split_rows(rows: list[dict[str, float | int | str]]):
    source = [row for row in rows if row["kind"] == "source"]
    restart = [row for row in rows if row["kind"] == "restart"]
    return source, restart


def plot_figure(rows: list[dict[str, float | int | str]], pdf_path: Path, png_path: Path):
    configure_plot_style()
    source_rows, restart_rows = split_rows(rows)
    if not restart_rows:
        return

    x = np.array([float(row["ftt"]) for row in restart_rows])
    xmax = max(float(np.max(x)), 0.05)
    metrics = [
        ("rho_rms", r"$\mathrm{RMS}_{\Omega}(\rho-1)$", "(a) domain RMS"),
        ("rho_max_abs", r"$\max_{\Omega}|\rho-1|$", "(b) domain maximum"),
        ("wall_max_abs", r"$\max_{\Gamma_w}|\rho-1|$", "(c) wall maximum"),
    ]

    fig, axes = plt.subplots(3, 1, figsize=(3.35, 4.55), sharex=True)
    for ax, (key, ylabel, panel_title) in zip(axes, metrics):
        y = np.array([float(row[key]) for row in restart_rows])
        ax.plot(x, y, color="black", marker="o", markerfacecolor="white",
                markeredgecolor="black", markeredgewidth=0.7,
                label="post-restart")
        if source_rows:
            baseline = float(source_rows[-1][key])
            ax.hlines(baseline, 0.0, xmax, colors="0.45",
                      linestyles="--", linewidth=0.8, label="source checkpoint")
        ax.set_ylabel(ylabel)
        ax.set_title(panel_title, loc="left", fontweight="normal")
        ax.set_xlim(0.0, xmax)
        ax.margins(y=0.12)
        ax.ticklabel_format(axis="y", style="sci", scilimits=(-2, 2), useMathText=True)
        ax.grid(False)
        ax.minorticks_on()
    axes[0].legend(loc="best")
    axes[-1].set_xlabel("flow-through time after restart")
    fig.align_ylabels(axes)
    fig.subplots_adjust(left=0.23, right=0.98, bottom=0.11, top=0.97, hspace=0.32)

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(pdf_path)
    fig.savefig(png_path, dpi=300)
    plt.close(fig)


def fmt_coord(x: float, y: float) -> str:
    return f"({x:.12g},{y:.12g})"


def write_pgfplots_tex(rows: list[dict[str, float | int | str]], tex_path: Path):
    source_rows, restart_rows = split_rows(rows)
    if not restart_rows:
        return

    x = [float(row["ftt"]) for row in restart_rows]
    xmax = max(max(x), 0.05)
    metrics = [
        ("rho_rms", 1.0e5, r"$\mathrm{RMS}_{\Omega}(\rho\!-\!1)$", r"$\times 10^{5}$", r"(a) domain RMS"),
        ("rho_max_abs", 1.0e4, r"$\max_{\Omega}|\rho\!-\!1|$", r"$\times 10^{4}$", r"(b) domain maximum"),
        ("wall_max_abs", 1.0e4, r"$\max_{\Gamma_w}|\rho\!-\!1|$", r"$\times 10^{4}$", r"(c) wall maximum"),
    ]

    blocks = []
    for idx, (key, scale, ylabel, scale_label, title) in enumerate(metrics):
        coords = "\n".join(
            "        " + fmt_coord(float(row["ftt"]), float(row[key]) * scale)
            for row in restart_rows
        )
        baseline = ""
        if source_rows:
            y0 = float(source_rows[-1][key]) * scale
            baseline = (
                "\n    \\addplot+[red!60!black, densely dashed, no marks, line width=0.6pt] coordinates {\n"
                f"        {fmt_coord(0.0, y0)}\n"
                f"        {fmt_coord(xmax, y0)}\n"
                "    };"
            )
        legend = "\n    \\legend{after restart, origin (pre-restart)}" if idx == 0 and source_rows else ""
        blocks.append(
            "    \\nextgroupplot[\n"
            f"        ylabel={{{ylabel}}},\n"
            f"        title={{\\footnotesize\\bfseries {title}"
            f"\\normalfont\\scriptsize\\ {scale_label}}}\n"
            "    ]\n"
            "    \\addplot+[blue!70!black, mark=none, line width=0.7pt] coordinates {\n"
            f"{coords}\n"
            "    };"
            f"{baseline}"
            f"{legend}"
        )

    xmin_trim = max(0.0, min(x) - 0.05)
    tex = rf"""\documentclass[tikz,border=3pt]{{standalone}}
\usepackage{{pgfplots}}
\usepgfplotslibrary{{groupplots}}
\usepackage{{newtxtext,newtxmath}}
\pgfplotsset{{compat=1.18}}
\begin{{document}}
\begin{{tikzpicture}}
\begin{{groupplot}}[
    group style={{
        group size=1 by 3,
        vertical sep=6mm,
        xlabels at=edge bottom,
        xticklabels at=edge bottom
    }},
    width=130mm,
    height=38mm,
    xmin={xmin_trim:.4g},
    xmax={xmax:.12g},
    axis lines=left,
    tick align=inside,
    tick style={{line width=0.4pt}},
    axis line style={{line width=0.4pt}},
    scaled y ticks=false,
    every axis title/.style={{at={{(0.02,0.97)}}, anchor=north west}},
    every axis plot/.append style={{line join=round}},
    label style={{font=\small}},
    tick label style={{font=\footnotesize}},
    xlabel style={{at={{(0.5,-0.08)}}, font=\small}},
    xlabel={{Flow-through time after restart, $\mathrm{{FTT}}_{{\mathrm{{restart}}}}$}},
    legend style={{font=\footnotesize, draw=none, fill=none,
        legend columns=2, at={{(0.98,0.97)}}, anchor=north east}},
    ymajorgrids=true,
    grid style={{line width=0.2pt, gray!30}},
]
{os.linesep.join(blocks)}
\end{{groupplot}}
\end{{tikzpicture}}
\end{{document}}
"""
    tex_path.write_text(tex, encoding="utf-8")


def find_latexmk() -> str | None:
    """Return a usable latexmk path, including the repo user's local TeX Live."""
    found = shutil.which("latexmk")
    if found:
        return found
    local = Path.home() / "texlive" / "2025" / "bin" / "x86_64-linux" / "latexmk"
    return str(local) if local.is_file() else None


def compile_pgfplots_pdf(tex_path: Path, latex_pdf_path: Path) -> tuple[bool, str]:
    """Compile the PGFPlots source without overwriting the Matplotlib PDF."""
    latexmk = find_latexmk()
    if latexmk is None:
        return False, "latexmk not found"
    env = os.environ.copy()
    bin_dir = str(Path(latexmk).parent)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    jobname = latex_pdf_path.stem
    proc = subprocess.run(
        [
            latexmk,
            "-pdf",
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"-jobname={jobname}",
            tex_path.name,
        ],
        cwd=str(tex_path.parent),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.returncode != 0:
        tail = "\n".join(proc.stdout.splitlines()[-8:])
        return False, tail
    generated = tex_path.with_name(jobname + ".pdf")
    if generated != latex_pdf_path:
        generated.replace(latex_pdf_path)
    return True, str(latex_pdf_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint-root", type=Path, default=DEFAULT_CHECKPOINT_ROOT)
    parser.add_argument("--source-checkpoint", type=Path, default=None)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--png", type=Path, default=DEFAULT_PNG)
    parser.add_argument("--tex", type=Path, default=DEFAULT_TEX)
    parser.add_argument("--latex-pdf", type=Path, default=DEFAULT_LATEX_PDF)
    parser.add_argument("--reset", action="store_true",
                        help="Discard prior metric history before scanning current checkpoints")
    parser.add_argument("--skip-latex", action="store_true",
                        help="Do not compile the generated PGFPlots source into PDF")
    args = parser.parse_args()

    checkpoint_root = args.checkpoint_root.resolve()
    source_dir = detect_source_checkpoint(args.source_checkpoint.resolve() if args.source_checkpoint else None)
    restart_dirs = candidate_restart_dirs(checkpoint_root)

    existing = [] if args.reset else normalise_rows(load_rows(args.csv))
    rows, added = merge_metrics(existing, source_dir, restart_dirs)
    if not rows:
        print("No checkpoint metrics available yet")
        return 0

    write_rows(args.csv, rows)
    plot_figure(rows, args.pdf, args.png)
    write_pgfplots_tex(rows, args.tex)
    latex_ok, latex_msg = (False, "skipped")
    if not args.skip_latex:
        latex_ok, latex_msg = compile_pgfplots_pdf(args.tex, args.latex_pdf)

    source_rows, restart_rows = split_rows(rows)
    print(
        f"[OK] density audit rows={len(rows)} "
        f"(source={len(source_rows)}, restart={len(restart_rows)}, new={added})"
    )
    if restart_rows:
        latest = restart_rows[-1]
        print(
            "[LATEST] "
            f"step={latest['step']} FTT={latest['ftt']:.6f} "
            f"rho_rms={latest['rho_rms']:.6e} "
            f"rho_max={latest['rho_max_abs']:.6e} "
            f"wall_max={latest['wall_max_abs']:.6e}"
        )
    print(f"[OUT] {args.csv}")
    print(f"[OUT] {args.pdf}")
    print(f"[OUT] {args.png}")
    print(f"[OUT] {args.tex}")
    if latex_ok:
        print(f"[LATEX] compiled {latex_msg}")
    else:
        print(f"[LATEX] {latex_msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
