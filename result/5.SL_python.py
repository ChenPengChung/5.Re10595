# -*- coding: utf-8 -*-
"""
VTK + Matplotlib 離線渲染：YZ 中間剖面 Contour + 流線，直接輸出 PNG。
用 pvpython 執行（pvpython 自帶 vtk, matplotlib, numpy）：
  pvpython 6.SL_python.py [vtk_folder]
"""
import os, sys, glob, time

import vtk
from vtk.util.numpy_support import vtk_to_numpy
import numpy as np

import matplotlib
matplotlib.use("Agg")          # 無 X display 也能用
import matplotlib.pyplot as plt
from matplotlib.ticker import AutoMinorLocator

# ── 設定 ──
VTK_FILE = None
FOLDER = os.path.dirname(os.path.abspath(__file__))
if len(sys.argv) > 1:
    FOLDER = os.path.abspath(sys.argv[1])
OUTPUT_PNG = os.path.join(FOLDER, "yz_contour_streamlines.png")
FIG_W, FIG_H = 24, 8          # inches
DPI = 150
NY, NZ = 600, 200             # 插值網格解析度 (grid points; 非 variables.h 的 NX/NY/NZ)
STREAM_DENSITY = 2.5           # streamplot 密度
NUM_CONTOUR_LEVELS = 64

def log(msg):
    print(msg, flush=True)

# ============================================================
# 1) 讀取（自動偵測最新 .vtk）
# ============================================================
candidates = sorted(glob.glob(os.path.join(FOLDER, "*.vtk")), key=os.path.getmtime)
if not candidates:
    log("ERROR: no .vtk in " + FOLDER); sys.exit(1)
if VTK_FILE:
    specified = os.path.join(FOLDER, VTK_FILE)
    vtk_path = specified if os.path.isfile(specified) else candidates[-1]
else:
    vtk_path = candidates[-1]
log("Loading: " + vtk_path)

t0 = time.time()
reader = vtk.vtkGenericDataObjectReader()
reader.SetFileName(vtk_path)
reader.Update()
data = reader.GetOutput()
log("Read done (%.1f s)" % (time.time() - t0))

bounds = data.GetBounds()
xmin, xmax = bounds[0], bounds[1]
ymin, ymax = bounds[2], bounds[3]
zmin, zmax = bounds[4], bounds[5]
xmid = (xmin + xmax) * 0.5
log("Bounds  X:[%.3f,%.3f]  Y:[%.3f,%.3f]  Z:[%.3f,%.3f]" % (xmin,xmax,ymin,ymax,zmin,zmax))
log("Slice at X = %.3f" % xmid)

# ============================================================
# 2) YZ 中間剖面
# ============================================================
plane = vtk.vtkPlane()
plane.SetOrigin(xmid, 0, 0)
plane.SetNormal(1, 0, 0)

cutter = vtk.vtkCutter()
cutter.SetCutFunction(plane)
cutter.SetInputData(data)
cutter.Update()
slice_data = cutter.GetOutput()
npts = slice_data.GetNumberOfPoints()
log("Slice points: %d" % npts)

# ============================================================
# 3) 插值到規則網格 (vtkProbeFilter)
# ============================================================
yi = np.linspace(ymin, ymax, NY)
zi = np.linspace(zmin, zmax, NZ)

probe_pts = vtk.vtkPoints()
for iz in range(NZ):
    for iy in range(NY):
        probe_pts.InsertNextPoint(xmid, yi[iy], zi[iz])

probe_pd = vtk.vtkPolyData()
probe_pd.SetPoints(probe_pts)

probe = vtk.vtkProbeFilter()
probe.SetInputData(probe_pd)
probe.SetSourceData(slice_data)
probe.Update()
probed = probe.GetOutput()

vel_probed = vtk_to_numpy(probed.GetPointData().GetArray("velocity"))
u_grid  = vel_probed[:, 1].reshape(NZ, NY)   # Y 分量 = stream-wise
vy_grid = vel_probed[:, 1].reshape(NZ, NY)
vz_grid = vel_probed[:, 2].reshape(NZ, NY)

lo, hi = u_grid.min(), u_grid.max()
log("u_streamwise range: [%.6f, %.6f]" % (lo, hi))
log("Interpolation done (%.1f s)" % (time.time() - t0))

# ============================================================
# 4) Matplotlib 繪圖
# ============================================================
YI, ZI = np.meshgrid(yi, zi)

fig, ax = plt.subplots(1, 1, figsize=(FIG_W, FIG_H), dpi=DPI)

# Contour 填色
levels = np.linspace(lo, hi, NUM_CONTOUR_LEVELS)
cf = ax.contourf(YI, ZI, u_grid, levels=levels, cmap="jet", extend="both")

# Colorbar
cb = fig.colorbar(cf, ax=ax, shrink=0.9, pad=0.02, aspect=30)
cb.set_label(r"$\bar{u}$  (stream-wise velocity)", fontsize=16)
cb.ax.tick_params(labelsize=12)

# 流線（黑色）
speed = np.sqrt(vy_grid**2 + vz_grid**2)
lw = 0.8 + 1.2 * speed / (speed.max() + 1e-15)   # 線寬隨速度變化
ax.streamplot(yi, zi, vy_grid, vz_grid, color="k",
              linewidth=lw, density=STREAM_DENSITY, arrowsize=0.8)

# 座標軸
ax.set_xlabel("Y (stream-wise)", fontsize=15)
ax.set_ylabel("Z (wall-normal)", fontsize=15)
ax.set_title("YZ Mid-Plane Contour + Streamlines  (X = %.3f)" % xmid, fontsize=17)
ax.tick_params(axis="both", which="major", labelsize=12, direction="in",
               top=True, right=True, length=6)
ax.tick_params(axis="both", which="minor", direction="in",
               top=True, right=True, length=3)
ax.xaxis.set_minor_locator(AutoMinorLocator(5))
ax.yaxis.set_minor_locator(AutoMinorLocator(5))
ax.set_aspect("equal")

plt.tight_layout()
plt.savefig(OUTPUT_PNG, dpi=DPI, bbox_inches="tight")
log("Image saved: " + OUTPUT_PNG)
log("Total: %.1f s" % (time.time() - t0))
log("Done.")
