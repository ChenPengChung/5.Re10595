"""
y+ profile along the bottom wall of a Periodic-Hill simulation.

Auto-detection (no manual user input required)
----------------------------------------------
* Reynolds number ........ parsed from the script's parent-path
* Grid (NX, NY, NZ) ...... read from the VTK DIMENSIONS header
* Input VTK file ......... newest .vtk in the script's directory;
                           if none, fall back to <script_dir>/result/
* Output file numbering .. max integer prefix in the directory + 1
"""

from pathlib import Path
import itertools
import re
import time
import numpy as np
import matplotlib.pyplot as plt
from matplotlib import rcParams


# ============================================================ USER-TUNABLE
H,         U_b       = 1.0, 1.0
LINELABEL            = "GILBM"
SCRIPT_DIR           = Path(__file__).parent.resolve()


# ------------------------------------------------ GLOBAL FONT (Times, BOLD)
rcParams["font.family"]      = "serif"
rcParams["font.serif"]       = ["Times New Roman", "Times", "DejaVu Serif"]
rcParams["font.weight"]      = "bold"
rcParams["axes.labelweight"] = "bold"
rcParams["axes.titleweight"] = "bold"
rcParams["mathtext.fontset"] = "stix"
rcParams["mathtext.default"] = "bf"


# ============================ helpers =================================
def parse_re_from_path(path: Path) -> float:
    pat = re.compile(r"^Re(\d+(?:\.\d+)?)$", re.IGNORECASE)
    for part in [path.name, *map(lambda p: p.name, path.parents)]:
        m = pat.match(part)
        if m:
            return float(m.group(1))
    raise RuntimeError(f"Cannot find 'Re<num>' token in path:\n  {path}")


def find_latest_vtk(folder: Path) -> Path:
    cs = sorted(folder.glob("*.vtk"), key=lambda p: p.stat().st_mtime)
    if cs:
        return cs[-1]
    fb = folder / "result"
    if fb.is_dir():
        cs = sorted(fb.glob("*.vtk"), key=lambda p: p.stat().st_mtime)
        if cs:
            return cs[-1]
    raise FileNotFoundError(f"No .vtk in {folder} or {fb}")


_PREFIX_RE = re.compile(r"^(\d+)\.")
def next_index(folder: Path) -> int:
    nums = []
    for child in folder.iterdir():
        m = _PREFIX_RE.match(child.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def read_vtk_ascii(path, want):
    want = set(want); out = {}
    def slurp(f, n):
        return np.array("".join(itertools.islice(f, n)).split(), dtype=np.float64)
    def skip(f, n):
        for _ in itertools.islice(f, n):
            pass
    with open(path, "r") as f:
        dims = None; Npts = None
        for line in f:
            if line.startswith("DIMENSIONS"):
                dims = tuple(map(int, line.split()[1:4]))
            elif line.startswith("POINTS"):
                Npts = int(line.split()[1]); break
        pts = slurp(f, Npts).reshape(Npts, 3)
        while True:
            line = f.readline()
            if not line: break
            s = line.strip()
            if s.startswith("VECTORS"):
                name = s.split()[1]
                if name in want: out[name] = slurp(f, Npts).reshape(Npts, 3)
                else: skip(f, Npts)
            elif s.startswith("SCALARS"):
                name = s.split()[1]
                f.readline()
                if name in want: out[name] = slurp(f, Npts)
                else: skip(f, Npts)
    return pts, out, dims


# ===================== MAIN PIPELINE ===================================
RE       = parse_re_from_path(SCRIPT_DIR)
NU       = U_b * H / RE
VTK_PATH = find_latest_vtk(SCRIPT_DIR)

print(f"[INFO] Re = {RE:g}")
print(f"[INFO] VTK : {VTK_PATH.name}")

t0 = time.time()
pts, fields, dims = read_vtk_ascii(VTK_PATH, want=["U_mean", "V_mean"])
NX, NY, NZ = dims
print(f"[INFO] parsed in {time.time()-t0:.1f}s, dims={dims}, Npts={len(pts)}")

xyz      = pts.reshape(NZ, NY, NX, 3)
u_stream = fields["U_mean"].reshape(NZ, NY, NX)
w_normal = fields["V_mean"].reshape(NZ, NY, NX)

P_w = xyz[0].mean(axis=1)
P_1 = xyz[1].mean(axis=1)
u_1 = u_stream[1].mean(axis=1)
w_1 = w_normal[1].mean(axis=1)

dn_vec = P_1 - P_w
dn     = np.linalg.norm(dn_vec, axis=1)

ty = np.gradient(P_w[:, 1])
tz = np.gradient(P_w[:, 2])
nrm = np.hypot(ty, tz)
ty, tz = ty / nrm, tz / nrm

u_tan     = u_1 * ty + w_1 * tz
tau_w_rho = NU * np.abs(u_tan) / dn
u_tau     = np.sqrt(tau_w_rho)
y_plus    = u_tau * dn / NU


# ============================================================ OUTPUTS
x_H = P_w[:, 1] / H

re_str   = f"{int(RE)}" if float(RE).is_integer() else f"{RE:g}"
basename = f"Re{re_str}_{NX}x{NY}x{NZ}_yplus"

idx_dat  = next_index(SCRIPT_DIR)
dat_path = SCRIPT_DIR / f"{idx_dat}.{basename}.dat"

np.savetxt(dat_path,
           np.column_stack([x_H, y_plus, dn, u_tan, u_tau]),
           header="x/H            y+             dn             u_tan          u_tau",
           fmt="%14.6e")

idx_png  = idx_dat + 1
png_path = SCRIPT_DIR / f"{idx_png}.{basename}.png"

plot_label = rf"$Re = {re_str},\ {NX}\times{NY}\times{NZ},\ \mathrm{{{LINELABEL}}}$"

fig, ax = plt.subplots(figsize=(7.5, 4.5))
ax.plot(x_H, y_plus, color="magenta", lw=1.8, label=plot_label)
ax.axhline(0.0, color="k", lw=0.4, ls=":")
ax.axvline(0.0, color="k", lw=0.4, ls=":")
ax.set_xlim(-0.2, 9.2)
# y-axis: keep lower bound at -0.2, upper bound auto-fits data max
y_top = float(np.nanmax(y_plus))
ax.set_ylim(-0.2, y_top)
ax.set_xlabel(r"$\mathbf{x/H}$", fontweight="bold")
ax.set_ylabel(r"$\mathbf{y^{+}}$", fontweight="bold")

for lbl in ax.get_xticklabels() + ax.get_yticklabels():
    lbl.set_fontweight("bold")

leg = ax.legend(loc="upper center", frameon=False)
for txt in leg.get_texts():
    txt.set_fontweight("bold")
    txt.set_fontfamily("serif")

fig.tight_layout()
fig.savefig(png_path, dpi=220)

print(f"[OK] wrote {dat_path.name}")
print(f"[OK] wrote {png_path.name}")
