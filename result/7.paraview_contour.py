# -*- coding: utf-8 -*-
"""
pvpython 離線渲染：YZ 中間剖面 Contour + 流線，直接輸出 PNG。
用法：
  pvpython SL_python.py
  pvpython SL_python.py "C:\path\to\vtk_folder"
"""
import os, sys, glob, math

# 指定檔名或 None：None 時自動偵測資料夾內「最新」的 .vtk（依修改時間）
VTK_FILE = None
FOLDER = os.path.dirname(os.path.abspath(__file__))
if len(sys.argv) > 1:
    FOLDER = os.path.abspath(sys.argv[1])
OUTPUT_PNG = os.path.join(FOLDER, "yz_contour_streamlines.png")
IMAGE_W, IMAGE_H = 2800, 800

# 入口流線（主通道）：單一垂直線種子，少數幾條粗線
NUM_MAIN_SEEDS = 15
# 渦流區：較密種子、均勻間隔取樣（分段間段密呈現渦流）
VORTEX_LOW_PERCENTILE = 15
MAX_VORTEX_SEEDS = 90
MAX_STREAMLINE_LEN = 20.0
SEED_STEP = 0.015
MAX_STEPS = 6000

def log(msg):
    print(msg, flush=True)

from paraview.simple import *

# ============================================================
# 1) 讀取（自動偵測最新 .vtk）
# ============================================================
candidates = sorted(glob.glob(os.path.join(FOLDER, "*.vtk")), key=os.path.getmtime)
if not candidates:
    log("ERROR: no .vtk in " + FOLDER)
    sys.exit(1)
if VTK_FILE:
    specified = os.path.join(FOLDER, VTK_FILE)
    vtk_path = specified if os.path.isfile(specified) else candidates[-1]
else:
    vtk_path = candidates[-1]
log("Loading (latest): " + vtk_path)

reader = LegacyVTKReader(FileNames=[vtk_path])
reader.UpdatePipeline()

bounds = reader.GetDataInformation().GetBounds()
xmin, xmax = bounds[0], bounds[1]
ymin, ymax = bounds[2], bounds[3]
zmin, zmax = bounds[4], bounds[5]
xmid = (xmin + xmax) * 0.5
log("Bounds  X:[%.3f,%.3f]  Y:[%.3f,%.3f]  Z:[%.3f,%.3f]" % (xmin, xmax, ymin, ymax, zmin, zmax))
log("Slice at X = %.3f" % xmid)

# ============================================================
# 2) 中間 YZ 剖面
# ============================================================
sliceF = Slice(Input=reader)
sliceF.SliceType.Normal = [1, 0, 0]
sliceF.SliceType.Origin = [xmid, (ymin+ymax)/2, (zmin+zmax)/2]
sliceF.UpdatePipeline()

calcU = Calculator(Input=sliceF)
calcU.ResultArrayName = "u_streamwise"
calcU.Function = "velocity_Y"
calcU.UpdatePipeline()

calcMag = Calculator(Input=calcU)
calcMag.ResultArrayName = "VelMag"
calcMag.Function = "mag(velocity)"
calcMag.UpdatePipeline()

# ============================================================
# 3) 流線（入口處垂直線種子：單條粗線風格）
# ============================================================
lineSeed = Line()
lineSeed.Point1 = [xmid, ymin + 0.01, zmin]
lineSeed.Point2 = [xmid, ymin + 0.01, zmax]
lineSeed.Resolution = NUM_MAIN_SEEDS
log("Seed line: %d points at Y=%.3f (單條粗線)" % (NUM_MAIN_SEEDS, ymin + 0.01))

st1 = StreamTracerWithCustomSource(Input=reader, SeedSource=lineSeed)
st1.Vectors = ["POINTS", "velocity"]
st1.MaximumStreamlineLength = MAX_STREAMLINE_LEN
st1.InitialStepLength = SEED_STEP
st1.MaximumSteps = MAX_STEPS
st1.IntegrationDirection = "FORWARD"
st1.UpdatePipeline()
log("Uniform streamlines done")

# ============================================================
# 4) 渦流/回流區加密流線（低速區）
# ============================================================
st2 = None
try:
    from paraview.servermanager import Fetch
    data = Fetch(calcMag)
    arr = data.GetPointData().GetArray("VelMag") if data else None
    if arr:
        npts = arr.GetNumberOfTuples()
        vals = sorted([arr.GetValue(i) for i in range(npts)])
        k = int((npts - 1) * VORTEX_LOW_PERCENTILE / 100.0)
        thresh_lo = vals[k]
        vmin = vals[0]
        if thresh_lo > vmin:
            threshF = Threshold(Input=calcMag)
            threshF.Scalars = ["POINTS", "VelMag"]
            threshF.LowerThreshold = vmin
            threshF.UpperThreshold = thresh_lo
            threshF.ThresholdMethod = "Between"
            threshF.UpdatePipeline()
            nv = threshF.GetDataInformation().GetNumberOfPoints()
            if nv > 0:
                seedSource = threshF
                if nv > MAX_VORTEX_SEEDS:
                    mask = MaskPoints(Input=threshF)
                    mask.OnRatio = max(2, nv // MAX_VORTEX_SEEDS)
                    mask.RandomSampling = 0
                    mask.UpdatePipeline()
                    seedSource = mask
                    nv = seedSource.GetDataInformation().GetNumberOfPoints()
                st2 = StreamTracerWithCustomSource(Input=reader, SeedSource=seedSource)
                st2.Vectors = ["POINTS", "velocity"]
                st2.MaximumStreamlineLength = MAX_STREAMLINE_LEN * 0.8
                st2.InitialStepLength = SEED_STEP * 0.3
                st2.MaximumSteps = MAX_STEPS
                st2.IntegrationDirection = "BOTH"
                st2.UpdatePipeline()
                log("Vortex (low-speed) streamlines: %d seeds" % nv)
            else:
                log("Low-speed points=%d, skip vortex seeds" % nv)
        else:
            log("No clear recirculation region detected")
except Exception as e:
    log("Vortex seed fallback: " + str(e))

# ============================================================
# 5) 離線渲染
# ============================================================
ren = CreateView("RenderView")
ren.ViewSize = [IMAGE_W, IMAGE_H]
ren.Background = [1, 1, 1]

disp = Show(calcMag, ren)
disp.Representation = "Surface"
disp.ColorArrayName = ["POINTS", "u_streamwise"]

lut = GetColorTransferFunction("u_streamwise")
info = calcMag.GetDataInformation().GetPointDataInformation().GetArrayInformation("u_streamwise")
if info:
    lo = info.GetComponentRange(0)[0]
    hi = info.GetComponentRange(0)[1]
else:
    lo, hi = 0.0, 1.0
log("u_streamwise range: [%.4f, %.4f]" % (lo, hi))

# Step：在控制點之間為常數色（階梯狀），控制點愈多階梯愈密集
lut.ColorSpace = "Step"
# 33 點 → 32 階梯
key_colors = [
    (0.0, 0.0, 0.0, 0.5), (0.125, 0.0, 0.0, 1.0), (0.25, 0.0, 0.5, 1.0),
    (0.375, 0.0, 1.0, 1.0), (0.5, 0.5, 1.0, 0.5), (0.625, 1.0, 1.0, 0.0),
    (0.75, 1.0, 0.5, 0.0), (0.875, 1.0, 0.0, 0.0), (1.0, 0.5, 0.0, 0.0),
]
rgb_pts = []
for i in range(33):
    t = i / 32.0
    for j in range(len(key_colors) - 1):
        if key_colors[j][0] <= t <= key_colors[j+1][0]:
            t0, r0, g0, b0 = key_colors[j]
            t1, r1, g1, b1 = key_colors[j+1]
            s = (t - t0) / (t1 - t0) if t1 > t0 else 0
            r = r0 + (r1 - r0) * s
            g = g0 + (g1 - g0) * s
            b = b0 + (b1 - b0) * s
            rgb_pts.extend([lo + (hi - lo) * t, r, g, b])
            break
    else:
        r, g, b = key_colors[-1][1], key_colors[-1][2], key_colors[-1][3]
        rgb_pts.extend([hi, r, g, b])
lut.RGBPoints = rgb_pts
disp.LookupTable = lut
disp.SetScalarBarVisibility(ren, True)

bar = GetScalarBar(lut, ren)
bar.Title = "u_streamwise"
bar.ComponentTitle = ""
bar.TitleFontSize = 18
bar.LabelFontSize = 14
bar.Orientation = "Vertical"
bar.WindowLocation = "Any Location"
bar.Position = [0.90, 0.05]
bar.ScalarBarLength = 0.85

# 流線粗度：非渦流區單條粗線；渦流區較密但線稍細以呈現結構
STREAMLINE_WIDTH_MAIN = 2.8
STREAMLINE_WIDTH_VORTEX = 1.5

# 主通道流線（非渦流區）
sd1 = Show(st1, ren)
sd1.Representation = "Wireframe"
sd1.LineWidth = STREAMLINE_WIDTH_MAIN
sd1.AmbientColor = [0.12, 0.12, 0.12]
sd1.DiffuseColor = [0.12, 0.12, 0.12]
ColorBy(sd1, None)
sd1.SetScalarBarVisibility(ren, False)

# 渦流區流線：較粗、較深色，凸顯結構
if st2:
    sd2 = Show(st2, ren)
    sd2.Representation = "Wireframe"
    sd2.LineWidth = STREAMLINE_WIDTH_VORTEX
    sd2.AmbientColor = [0.05, 0.05, 0.05]
    sd2.DiffuseColor = [0.05, 0.05, 0.05]
    ColorBy(sd2, None)
    sd2.SetScalarBarVisibility(ren, False)

cam = ren.GetActiveCamera()
cam.SetPosition(xmid + 20, (ymin+ymax)/2, (zmin+zmax)/2)
cam.SetFocalPoint(xmid, (ymin+ymax)/2, (zmin+zmax)/2)
cam.SetViewUp(0, 0, 1)
cam.SetParallelProjection(True)
ResetCamera()
cam.SetParallelScale((zmax - zmin) * 0.55)

Render(ren)
SaveScreenshot(OUTPUT_PNG, ren, ImageResolution=[IMAGE_W, IMAGE_H])
log("Image saved: " + OUTPUT_PNG)
log("Done.")
