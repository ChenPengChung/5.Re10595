"""
plot_style.py — Project-wide matplotlib style: Times New Roman + STIX math
==========================================================================
Usage (in any plot script):
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'result'))
    from plot_style import apply_style
    apply_style()          # call BEFORE any fig/ax creation

Or simply:
    from plot_style import apply_style; apply_style()
"""

def apply_style():
    import matplotlib
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        # Font: Times New Roman (serif) for all text
        "font.family":       "serif",
        "font.serif":        ["Times New Roman", "Times", "DejaVu Serif"],
        "font.size":         11,

        # Math text: STIX (Times-compatible) — no LaTeX needed
        "mathtext.fontset":  "stix",

        # Axes
        "axes.labelsize":    12,
        "axes.titlesize":    13,
        "axes.titleweight":  "bold",
        "axes.linewidth":    0.8,

        # Ticks
        "xtick.labelsize":   10,
        "ytick.labelsize":   10,
        "xtick.direction":   "in",
        "ytick.direction":   "in",
        "xtick.major.width": 0.8,
        "ytick.major.width": 0.8,
        "xtick.minor.width": 0.5,
        "ytick.minor.width": 0.5,

        # Legend
        "legend.fontsize":   9,
        "legend.framealpha": 0.85,
        "legend.edgecolor":  "0.7",

        # Figure
        "figure.dpi":        150,
        "savefig.dpi":       150,
        "savefig.bbox":      "tight",

        # Lines
        "lines.linewidth":   1.2,
    })
