# -*- coding: utf-8 -*-
"""
pvpython 離線渲染：YZ 中間剖面 Contour (+ optional streamlines for U_mean/V_mean)
===========================================================================
用法：
  pvpython render_frame.py [vtk_file] [--outdir png_frames] [--step 1000]

  若不指定 vtk_file，自動搜尋 result/ 中最新的 velocity_merged_*.vtk

功能：
  Path A (GIF frame):  u_streamwise 瞬時等值面, 無流線, 白底, 帶尺規 → PNG
  Path B (Mean field):  若 VTK 含 U_mean+V_mean, 用平均速度向量畫流線 → 另一 PNG

色標、色階、字體、尺規完全沿用 7.paraview_contour.py 設定。
"""
import os, sys, glob, math

# ═══════════════════════════════════════════════════════════════════
# §0  引數解析
# ═══════════════════════════════════════════════════════════════════
VTK_FILE = None
OUTDIR = "png_frames"
STEP_NUM = None
VIDEO_MODE = False      # True = 影片模式 (只吐 frame_cont + frame_RD)
                        # False = 人工模式 (8 張 PNG 全輸出)

args = sys.argv[1:]
i = 0
while i < len(args):
    if args[i] == "--outdir" and i+1 < len(args):
        OUTDIR = args[i+1]; i += 2
    elif args[i] == "--step" and i+1 < len(args):
        STEP_NUM = int(args[i+1].rstrip('.')); i += 2
    elif args[i] == "--video-mode":
        VIDEO_MODE = True; i += 1
    elif not args[i].startswith("--"):
        VTK_FILE = os.path.abspath(args[i]); i += 1
    else:
        i += 1

if VTK_FILE is None:
    # 自動搜尋最新「非空」VTK 檔案（跳過 0 bytes 的截斷檔）
    MIN_VALID_BYTES = 1024  # 小於 1 KiB 視為無效（正常 VTK > 1 GB）
    search_dirs = ["result", "../result", "."]
    for d in search_dirs:
        pattern = os.path.join(d, "velocity_merged_*.vtk")
        matches = sorted(glob.glob(pattern))
        # 從新到舊挑第一個有效的檔
        for cand in reversed(matches):
            try:
                sz = os.path.getsize(cand)
            except OSError:
                continue
            if sz < MIN_VALID_BYTES:
                print("Skip empty/truncated VTK (%d bytes): %s" % (sz, cand))
                continue
            VTK_FILE = os.path.abspath(cand)
            print("Auto-detected latest VTK: " + VTK_FILE)
            break
        if VTK_FILE is not None:
            break
    if VTK_FILE is None:
        print("ERROR: No valid VTK file specified and none found in result/.")
        print("Usage: pvpython render_frame.py [vtk_file]")
        sys.exit(1)

if not os.path.isfile(VTK_FILE):
    print("ERROR: VTK file not found: " + VTK_FILE)
    sys.exit(1)

if not os.path.isdir(OUTDIR):
    os.makedirs(OUTDIR)

if STEP_NUM is not None:
    OUT_INST         = os.path.join(OUTDIR, "frame_%06d.png" % STEP_NUM)
    OUT_INST_CONT    = os.path.join(OUTDIR, "frame_%06d_cont.png" % STEP_NUM)
    OUT_INST_RD      = os.path.join(OUTDIR, "frame_%06d_RD.png" % STEP_NUM)
    OUT_INST_RD_CONT = os.path.join(OUTDIR, "frame_%06d_RD_cont.png" % STEP_NUM)
    OUT_MEAN         = os.path.join(OUTDIR, "Umean_%06d.png" % STEP_NUM)
    OUT_MEAN_CONT    = os.path.join(OUTDIR, "Umean_%06d_cont.png" % STEP_NUM)
    OUT_TKE_CONT     = os.path.join(OUTDIR, "TKE_%06d_cont.png" % STEP_NUM)
    OUT_QCRIT        = os.path.join(OUTDIR, "Qcrit_%06d.png" % STEP_NUM)
else:
    base = os.path.splitext(os.path.basename(VTK_FILE))[0]
    OUT_INST         = os.path.join(OUTDIR, base + "_inst.png")
    OUT_INST_CONT    = os.path.join(OUTDIR, base + "_inst_cont.png")
    OUT_INST_RD      = os.path.join(OUTDIR, base + "_inst_RD.png")
    OUT_INST_RD_CONT = os.path.join(OUTDIR, base + "_inst_RD_cont.png")
    OUT_MEAN         = os.path.join(OUTDIR, base + "_Umean.png")
    OUT_MEAN_CONT    = os.path.join(OUTDIR, base + "_Umean_cont.png")
    OUT_TKE_CONT     = os.path.join(OUTDIR, base + "_TKE_cont.png")
    OUT_QCRIT        = os.path.join(OUTDIR, base + "_Qcrit.png")

# ═══════════════════════════════════════════════════════════════════
# §1  渲染參數（完全沿用 7.paraview_contour.py）
# ═══════════════════════════════════════════════════════════════════
IMAGE_W, IMAGE_H = 5600, 1600     # 直接用原生解析，避免 ViewSize→ImageResolution 的 GPU upscale 造成霧化
SAVE_SCALE = 1
SAVE_W, SAVE_H = IMAGE_W * SAVE_SCALE, IMAGE_H * SAVE_SCALE

NUM_MAIN_SEEDS = 10
VORTEX_LOW_PERCENTILE = 12
MAX_VORTEX_SEEDS = 50
MAX_STREAMLINE_LEN = 20.0
SEED_STEP = 0.015
MAX_STEPS = 6000
STREAMLINE_WIDTH_MAIN = 2.8
STREAMLINE_WIDTH_VORTEX = 1.5

# ── Q-criterion 參數 ──
Q_AUTO_FRACTION = 0.05            # fallback: 初始 Q_threshold = Qmax * 5%
Q_CELLS_TARGET_LO = 20000
Q_CELLS_TARGET_HI = 80000
Q_COVERAGE = 0.02                 # 渦流結構覆蓋 = 1% 流場點數 (Q > threshold) → 只留強渦管，對齊 Frohlich/Breuer 可視化風格
Q_IMG_W, Q_IMG_H = 1920, 1080     # 與參考範本 IMG_SIZE 一致 (16:9)
Q_OPACITY = 0.8                   # 與參考範本 OPACITY 一致
W_RANGE = [-0.02, 0.02]           # 與參考範本 W_RANGE 一致（固定範圍，不做對稱重縮）

# ── FTT / Ma 文字標註用常數（對齊參考範本）──
Q_U_REF       = 0.0583
Q_LY          = 9.0
Q_CS          = 1.0 / 1.732050807568877
Q_DT_GLOBAL   = 2.397914e-03

KEY_COLORS = [
    (0.0, 0.0, 0.0, 0.5), (0.125, 0.0, 0.0, 1.0), (0.25, 0.0, 0.5, 1.0),
    (0.375, 0.0, 1.0, 1.0), (0.5, 0.5, 1.0, 0.5), (0.625, 1.0, 1.0, 0.0),
    (0.75, 1.0, 0.5, 0.0), (0.875, 1.0, 0.0, 0.0), (1.0, 0.5, 0.0, 0.0),
]

def log(msg):
    print(msg, flush=True)


# ═══════════════════════════════════════════════════════════════════
# §2  共用函數
# ═══════════════════════════════════════════════════════════════════
from paraview.simple import *

def build_rgb_points(lo, hi, key_colors):
    """33-step colormap interpolation → RGBPoints list"""
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
    return rgb_pts


def setup_view(ren):
    """白底 + 隱藏 XYZ widget + 關閉 FXAA/MSAA 避免色塊霧化"""
    ren.Background = [1.0, 1.0, 1.0]
    ren.Background2 = [1.0, 1.0, 1.0]
    try: ren.UseColorPaletteForBackground = 0
    except: pass
    try: ren.BackgroundColorMode = "Single Color"
    except: pass
    try: LoadPalette(paletteName='WhiteBackground')
    except: pass
    ren.OrientationAxesVisibility = 0
    # 關掉抗鋸齒 → 避免色塊邊緣被模糊掉（造成整體「霧」感）
    try: ren.UseFXAA = 0
    except: pass
    try: ren.MultiSamples = 0
    except: pass
    try: ren.StillRenderImageReductionFactor = 1
    except: pass


def flatten_display(disp):
    """2D 剖面 flat unlit shading — Ambient=1, Diffuse=0 避免光照稀釋顏色"""
    try: disp.Ambient = 1.0
    except: pass
    try: disp.Diffuse = 0.0
    except: pass
    try: disp.Specular = 0.0
    except: pass
    try: disp.Interpolation = "Flat"
    except: pass
    try: disp.Opacity = 1.0
    except: pass
    # 開啟 → 先頂點插值純量再查 LUT，配合 Discretize=1 產生 surface 離散色塊
    try: disp.InterpolateScalarsBeforeMapping = 1
    except: pass


def resample_lut_to_n_keys(lut, n, lo, hi):
    """ApplyPreset 之後把 RGBPoints 重採樣成 n 個控制點（[lo, hi] 範圍），
    讓 preset 色階的可見色帶數與 KEY_COLORS (build_rgb_points 33 點) 一致。"""
    src = list(lut.RGBPoints)
    if len(src) < 4:
        return
    tuples = []
    for i in range(0, len(src), 4):
        tuples.append((src[i], src[i+1], src[i+2], src[i+3]))
    tuples.sort(key=lambda t: t[0])
    x_min, x_max = tuples[0][0], tuples[-1][0]
    if x_max <= x_min:
        return
    new_pts = []
    for i in range(n):
        t = i / (n - 1.0)
        x_src = x_min + t * (x_max - x_min)
        r, g, b = tuples[-1][1], tuples[-1][2], tuples[-1][3]
        for j in range(len(tuples) - 1):
            x0, r0, g0, b0 = tuples[j]
            x1, r1, g1, b1 = tuples[j+1]
            if x0 <= x_src <= x1:
                s = (x_src - x0) / (x1 - x0) if x1 > x0 else 0.0
                r = r0 + (r1 - r0) * s
                g = g0 + (g1 - g0) * s
                b = b0 + (b1 - b0) * s
                break
        x_new = lo + t * (hi - lo)
        new_pts.extend([x_new, r, g, b])
    lut.RGBPoints = new_pts


def harden_lut(lut, n_bands=256):
    """關透明度映射 + 離散色表 → 消除霧感 / 避免 alpha blending 帶來的半透明"""
    try: lut.EnableOpacityMapping = 0
    except: pass
    try: lut.Discretize = 1
    except: pass
    try: lut.NumberOfTableValues = n_bands
    except: pass
    try: lut.NanOpacity = 1.0
    except: pass


def setup_axes_grid(ren, bounds):
    """座標尺規 — 如參考圖：x/H [-] (streamwise) + y/H [-] (wall-normal)"""
    ag = ren.AxesGrid
    ag.Visibility = 1

    # 軸標題：對應參考圖格式（MathText 排版）
    ag.XTitle = ""
    ag.YTitle = r"$x/H$"
    ag.ZTitle = r"$y/H$"

    black = [0.0, 0.0, 0.0]
    ag.XTitleColor = black
    ag.YTitleColor = black
    ag.ZTitleColor = black
    ag.XLabelColor = black
    ag.YLabelColor = black
    ag.ZLabelColor = black
    ag.GridColor = black

    # Times New Roman Bold
    ag.XTitleFontFamily = "Times"
    ag.YTitleFontFamily = "Times"
    ag.ZTitleFontFamily = "Times"
    ag.XLabelFontFamily = "Times"
    ag.YLabelFontFamily = "Times"
    ag.ZLabelFontFamily = "Times"
    ag.XTitleBold = 1
    ag.YTitleBold = 1
    ag.ZTitleBold = 1
    ag.XLabelBold = 1
    ag.YLabelBold = 1
    ag.ZLabelBold = 1

    # 標題（x/H, y/H）放大；刻度數字保持適中（已 OK）
    ag.XTitleFontSize = 48
    ag.YTitleFontSize = 48
    ag.ZTitleFontSize = 48
    ag.XLabelFontSize = 36
    ag.YLabelFontSize = 36
    ag.ZLabelFontSize = 36

    # 整數刻度
    ag.XAxisNotation = "Fixed"
    ag.YAxisNotation = "Fixed"
    ag.ZAxisNotation = "Fixed"
    ag.XAxisPrecision = 0
    ag.YAxisPrecision = 0
    ag.ZAxisPrecision = 0

    # 自訂 tick 標籤：只顯示整數 0, 1, 2, ...
    ymin_b, ymax_b = bounds[2], bounds[3]
    zmin_b, zmax_b = bounds[4], bounds[5]
    y_labels = [float(v) for v in range(int(math.ceil(ymin_b)), int(math.floor(ymax_b)) + 1)]
    z_labels = [float(v) for v in range(int(math.ceil(zmin_b)), int(math.floor(zmax_b)) + 1)]

    # ParaView 5.12 custom labels API（嘗試多種寫法）
    try:
        ag.YAxisUseCustomLabels = 1
        ag.ZAxisUseCustomLabels = 1
        ag.YAxisLabels = y_labels
        ag.ZAxisLabels = z_labels
        log("Custom labels (per-axis API): Y=%s, Z=%s" % (y_labels, z_labels))
    except Exception:
        try:
            ag.UseCustomLabels = [0, 1, 1]
            ag.YAxisLabels = y_labels
            ag.ZAxisLabels = z_labels
            log("Custom labels (vector API): Y=%s, Z=%s" % (y_labels, z_labels))
        except Exception as e:
            log("Custom labels not supported: %s" % str(e))


def setup_camera(ren, bounds):
    """正交投影相機 — 加 padding 避免尺規標籤被裁切"""
    xmin, xmax = bounds[0], bounds[1]
    ymin, ymax = bounds[2], bounds[3]
    zmin, zmax = bounds[4], bounds[5]
    xmid = (xmin + xmax) * 0.5

    cam = ren.GetActiveCamera()
    cam.SetPosition(xmid + 20, (ymin+ymax)/2, (zmin+zmax)/2)
    cam.SetFocalPoint(xmid, (ymin+ymax)/2, (zmin+zmax)/2)
    cam.SetViewUp(0, 0, 1)
    cam.SetParallelProjection(True)
    ResetCamera()
    # 0.58: 稍微縮小讓 x/H 標題留在邊界內
    cam.SetParallelScale((zmax - zmin) * 0.58)


def setup_scalar_bar(lut, ren, title="u_streamwise", value_range=None, n_ticks=8):
    """色標列 — Times New Roman Bold, fixed-point labels
    （只給 Path A/B/C 的 7 張圖用；Path D Q-criterion 有獨立 barD 不受影響）
    value_range=(lo,hi) 指定後會強制顯示 n_ticks 個 tick（避開 ParaView 的自動密集 tick）"""
    bar = GetScalarBar(lut, ren)
    bar.Title = title
    bar.ComponentTitle = ""
    # 字體放大：Title 80pt, Label 40pt
    bar.TitleFontSize = 80
    bar.LabelFontSize = 40
    bar.Orientation = "Vertical"
    bar.WindowLocation = "Any Location"
    # 色條延伸至幾乎全高（y 起點 0.02，長度 0.96）
    bar.Position = [0.92, 0.02]
    bar.ScalarBarLength = 0.96
    # 色條本體加粗 1.5 倍（ParaView 預設 ScalarBarThickness ≈ 16 → 24）
    try: bar.ScalarBarThickness = 24
    except: pass
    # ── 強制 n_ticks 個 tick（用 CustomLabels，比 NumberOfLabels 可靠）──
    if value_range is not None:
        lo, hi = value_range
        ticks = [lo + (hi - lo) * i / (n_ticks - 1) for i in range(n_ticks)]
        try: bar.UseCustomLabels = 1
        except: pass
        try: bar.CustomLabels = ticks
        except: pass
        try: bar.AddRangeLabels = 0    # 避免兩端被重複加 tick
        except: pass
    else:
        try: bar.NumberOfLabels = n_ticks
        except: pass
    bar.TitleColor = [0, 0, 0]
    bar.LabelColor = [0, 0, 0]
    # Times New Roman Bold
    bar.TitleFontFamily = "Times"
    bar.LabelFontFamily = "Times"
    bar.TitleBold = 1
    bar.LabelBold = 1
    # 根據數值範圍選擇 tick 精度（避免 0.1 重複）
    if value_range is not None:
        span = abs(value_range[1] - value_range[0])
        if span < 0.1:    fmt = "%-#6.3f"
        elif span < 1.0:  fmt = "%-#6.2f"
        else:             fmt = "%-#6.1f"
    else:
        fmt = "%-#6.1f"
    try: bar.AutomaticLabelFormat = 0
    except: pass
    try: bar.RangeLabelFormat = fmt
    except: pass
    try: bar.LabelFormat = fmt
    except: pass
    return bar


def add_mean_streamlines(reader, ren, bounds):
    """
    Path B 專用：從 U_mean + V_mean 標量場建構平均速度向量，再畫流線
    ─────────────────────────────────────────────────────────────
    VTK 座標映射：
      X = spanwise (code u) → 平均展向速度 ≈ 0
      Y = streamwise (code v) → U_mean
      Z = wall-normal (code w) → V_mean
    平均速度向量 = (0, U_mean, V_mean)
    ─────────────────────────────────────────────────────────────
    """
    xmin, xmax = bounds[0], bounds[1]
    ymin, ymax = bounds[2], bounds[3]
    zmin, zmax = bounds[4], bounds[5]
    xmid = (xmin + xmax) * 0.5

    # ── 建構平均速度向量場 ──
    calcMeanVel = Calculator(Input=reader)
    calcMeanVel.ResultArrayName = "MeanVelocity"
    calcMeanVel.Function = "iHat*0 + jHat*U_mean + kHat*V_mean"
    calcMeanVel.UpdatePipeline()
    log("Mean velocity vector constructed: (0, U_mean, V_mean)")

    # ── 主通道流線：入口垂直線種子 ──
    lineSeed = Line()
    lineSeed.Point1 = [xmid, ymin + 0.01, zmin]
    lineSeed.Point2 = [xmid, ymin + 0.01, zmax]
    lineSeed.Resolution = NUM_MAIN_SEEDS
    log("Seed line: %d points at Y=%.3f" % (NUM_MAIN_SEEDS, ymin + 0.01))

    st1 = StreamTracerWithCustomSource(Input=calcMeanVel, SeedSource=lineSeed)
    st1.Vectors = ["POINTS", "MeanVelocity"]
    st1.MaximumStreamlineLength = MAX_STREAMLINE_LEN
    st1.InitialStepLength = SEED_STEP
    st1.MaximumSteps = MAX_STEPS
    st1.IntegrationDirection = "FORWARD"
    st1.UpdatePipeline()
    log("Main mean streamlines done")

    sd1 = Show(st1, ren)
    sd1.Representation = "Wireframe"
    sd1.LineWidth = STREAMLINE_WIDTH_MAIN
    sd1.AmbientColor = [0.0, 0.0, 0.0]
    sd1.DiffuseColor = [0.0, 0.0, 0.0]
    try: sd1.Ambient = 1.0
    except: pass
    try: sd1.Diffuse = 0.0
    except: pass
    ColorBy(sd1, None)
    sd1.SetScalarBarVisibility(ren, False)

    # ── 渦流/回流區加密流線（低速區）──
    # 先 Slice + VelMag on mean field
    sliceF_vel = Slice(Input=calcMeanVel)
    sliceF_vel.SliceType.Normal = [1, 0, 0]
    sliceF_vel.SliceType.Origin = [xmid, (ymin+ymax)/2, (zmin+zmax)/2]
    sliceF_vel.UpdatePipeline()

    calcMag_vel = Calculator(Input=sliceF_vel)
    calcMag_vel.ResultArrayName = "MeanVelMag"
    calcMag_vel.Function = "mag(MeanVelocity)"
    calcMag_vel.UpdatePipeline()

    st2 = None
    try:
        from paraview.servermanager import Fetch
        data = Fetch(calcMag_vel)
        arr = data.GetPointData().GetArray("MeanVelMag") if data else None
        if arr:
            npts = arr.GetNumberOfTuples()
            vals = sorted([arr.GetValue(i) for i in range(npts)])
            k_idx = int((npts - 1) * VORTEX_LOW_PERCENTILE / 100.0)
            thresh_lo = vals[k_idx]
            vmin = vals[0]
            log("Mean VelMag: min=%.4f, P%d=%.4f" % (vmin, VORTEX_LOW_PERCENTILE, thresh_lo))
            if thresh_lo > vmin:
                threshF = Threshold(Input=calcMag_vel)
                threshF.Scalars = ["POINTS", "MeanVelMag"]
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
                    st2 = StreamTracerWithCustomSource(Input=calcMeanVel, SeedSource=seedSource)
                    st2.Vectors = ["POINTS", "MeanVelocity"]
                    st2.MaximumStreamlineLength = MAX_STREAMLINE_LEN * 0.8
                    st2.InitialStepLength = SEED_STEP * 0.3
                    st2.MaximumSteps = MAX_STEPS
                    st2.IntegrationDirection = "BOTH"
                    st2.UpdatePipeline()
                    log("Vortex mean streamlines: %d seeds" % nv)

                    sd2 = Show(st2, ren)
                    sd2.Representation = "Wireframe"
                    sd2.LineWidth = STREAMLINE_WIDTH_VORTEX
                    sd2.AmbientColor = [0.0, 0.0, 0.0]
                    sd2.DiffuseColor = [0.0, 0.0, 0.0]
                    try: sd2.Ambient = 1.0
                    except: pass
                    try: sd2.Diffuse = 0.0
                    except: pass
                    ColorBy(sd2, None)
                    sd2.SetScalarBarVisibility(ren, False)
                else:
                    log("Low-speed mean points=%d, skip vortex seeds" % nv)
            else:
                log("No clear mean recirculation region")
    except Exception as e:
        log("Mean vortex seed fallback: " + str(e))


# ═══════════════════════════════════════════════════════════════════
# §3  讀取 VTK (ASCII → Binary 自動轉換加速)
# ═══════════════════════════════════════════════════════════════════
import time as _time

def _convert_ascii_to_binary(ascii_path):
    """ASCII VTK → Binary VTK, 回傳 binary 路徑。已存在且較新則跳過。"""
    base, ext = os.path.splitext(ascii_path)
    bin_path = base + "_bin" + ext  # e.g. velocity_merged_062001_bin.vtk
    if os.path.isfile(bin_path) and os.path.getmtime(bin_path) >= os.path.getmtime(ascii_path):
        log("Binary cache exists: %s" % bin_path)
        return bin_path
    log("Converting ASCII → Binary: %s" % os.path.basename(bin_path))
    t0 = _time.time()
    _r = LegacyVTKReader(FileNames=[ascii_path])
    _r.UpdatePipeline()
    _w = SaveData(bin_path, proxy=_r, FileType='Binary')
    Delete(_r)
    elapsed = _time.time() - t0
    sz_mb = os.path.getsize(bin_path) / 1048576.0
    log("Converted in %.1fs → %s (%.0f MB)" % (elapsed, os.path.basename(bin_path), sz_mb))
    return bin_path

# ── 判斷是否為 ASCII VTK, 若是則轉 binary 加速後續讀取 ──
_use_file = VTK_FILE
with open(VTK_FILE, 'rb') as _fh:
    for _ln in range(6):
        _raw = _fh.readline()
        if not _raw:
            break
        _s = _raw.strip().upper()
        if _s == b"ASCII":
            _use_file = _convert_ascii_to_binary(VTK_FILE)
            break
        if _s == b"BINARY":
            break

log("Loading: " + _use_file)
t_load = _time.time()
reader = LegacyVTKReader(FileNames=[_use_file])
reader.UpdatePipeline()
log("Loaded in %.1fs" % (_time.time() - t_load))

bounds = reader.GetDataInformation().GetBounds()
xmin, xmax = bounds[0], bounds[1]
ymin, ymax = bounds[2], bounds[3]
zmin, zmax = bounds[4], bounds[5]
xmid = (xmin + xmax) * 0.5
log("Bounds  X:[%.3f,%.3f]  Y:[%.3f,%.3f]  Z:[%.3f,%.3f]" % (xmin, xmax, ymin, ymax, zmin, zmax))
log("Slice at X = %.3f" % xmid)

pointData = reader.GetDataInformation().GetPointDataInformation()
has_Umean = False
has_Vmean = False
has_TKE = False
has_uu = False
has_vv = False
has_ww = False
has_velocity = False
TKE_name = None
uu_name = None
vv_name = None
ww_name = None
for idx in range(pointData.GetNumberOfArrays()):
    name = pointData.GetArrayInformation(idx).GetName()
    if name == "velocity": has_velocity = True
    if name == "U_mean": has_Umean = True
    if name == "V_mean": has_Vmean = True
    if name in ("TKE", "tke", "k_turb", "k_TKE"):
        has_TKE = True; TKE_name = name
    if name in ("uu_mean", "u_u_mean", "uprime_uprime", "uu_RS"):
        has_uu = True; uu_name = name
    if name in ("vv_mean", "v_v_mean", "vprime_vprime", "vv_RS"):
        has_vv = True; vv_name = name
    if name in ("ww_mean", "w_w_mean", "wprime_wprime", "ww_RS"):
        has_ww = True; ww_name = name
has_TKE_computable = has_TKE or (has_uu and has_vv and has_ww)
log("U_mean: %s, V_mean: %s" % (
    "FOUND" if has_Umean else "not found",
    "FOUND" if has_Vmean else "not found"))
log("TKE: %s (direct=%s, uu=%s, vv=%s, ww=%s)" % (
    "AVAILABLE" if has_TKE_computable else "not available",
    "FOUND" if has_TKE else "-",
    "FOUND" if has_uu else "-",
    "FOUND" if has_vv else "-",
    "FOUND" if has_ww else "-"))


# ═══════════════════════════════════════════════════════════════════
# §4  Path A: 瞬時 u_streamwise — 無流線, 白底, 帶尺規
# ═══════════════════════════════════════════════════════════════════
log("=== Path A: Instantaneous u_streamwise ===")

sliceA = Slice(Input=reader)
sliceA.SliceType.Normal = [1, 0, 0]
sliceA.SliceType.Origin = [xmid, (ymin+ymax)/2, (zmin+zmax)/2]
sliceA.UpdatePipeline()

calcA = Calculator(Input=sliceA)
calcA.ResultArrayName = "u_streamwise"
calcA.Function = "velocity_Y"
calcA.UpdatePipeline()

renA = CreateView("RenderView")
renA.ViewSize = [IMAGE_W, IMAGE_H]
try: renA.UseOffscreenRendering = 1     # 強制 offscreen，避免 X server 依賴
except: pass
setup_view(renA)

dispA = Show(calcA, renA)
dispA.Representation = "Surface"
dispA.ColorArrayName = ["POINTS", "u_streamwise"]
flatten_display(dispA)

lutA = GetColorTransferFunction("u_streamwise")
infoA = calcA.GetDataInformation().GetPointDataInformation().GetArrayInformation("u_streamwise")
if infoA:
    lo_A = infoA.GetComponentRange(0)[0]
    hi_A = infoA.GetComponentRange(0)[1]
else:
    lo_A, hi_A = 0.0, 1.0
log("u_streamwise range: [%.4f, %.4f]" % (lo_A, hi_A))

lutA.ColorSpace = "Step"
lutA.RGBPoints = build_rgb_points(lo_A, hi_A, KEY_COLORS)
harden_lut(lutA)
dispA.LookupTable = lutA
dispA.SetScalarBarVisibility(renA, True)
setup_scalar_bar(lutA, renA, r"$u/U_{ref}$", value_range=(lo_A, hi_A))

setup_camera(renA, bounds)
setup_axes_grid(renA, bounds)

if not VIDEO_MODE:
    Render(renA)
    SaveScreenshot(OUT_INST, renA, ImageResolution=[SAVE_W, SAVE_H],
                   OverrideColorPalette='WhiteBackground')
    log("Path A saved (step):       " + OUT_INST)
else:
    log("Path A step skipped (video-mode)")

# ── 連續色標版本：切到 RGB 空間後重新渲染輸出（影片模式也需要）──
lutA.ColorSpace = "RGB"
Render(renA)
SaveScreenshot(OUT_INST_CONT, renA, ImageResolution=[SAVE_W, SAVE_H],
               OverrideColorPalette='WhiteBackground')
log("Path A saved (continuous): " + OUT_INST_CONT)

# ── Rainbow Desaturated 比較版本（step + continuous）──
# 色階數統一：ApplyPreset 後重採樣到 33 個控制點，與 KEY_COLORS (build_rgb_points) 一致
try: lutA.ApplyPreset('Rainbow Desaturated', True)
except: pass
try: lutA.RescaleTransferFunction(lo_A, hi_A)
except: pass
resample_lut_to_n_keys(lutA, 33, lo_A, hi_A)

lutA.ColorSpace = "Step"
Render(renA)
SaveScreenshot(OUT_INST_RD, renA, ImageResolution=[SAVE_W, SAVE_H],
               OverrideColorPalette='WhiteBackground')
log("Path A saved (Rainbow Desaturated, step, 33 bands):       " + OUT_INST_RD)

if not VIDEO_MODE:
    lutA.ColorSpace = "RGB"
    Render(renA)
    SaveScreenshot(OUT_INST_RD_CONT, renA, ImageResolution=[SAVE_W, SAVE_H],
                   OverrideColorPalette='WhiteBackground')
    log("Path A saved (Rainbow Desaturated, continuous, 33 keys):  " + OUT_INST_RD_CONT)
else:
    log("Path A RD_cont skipped (video-mode)")

Delete(renA)
del renA


# ═══════════════════════════════════════════════════════════════════
# §5  Path B: U_mean + 平均流線（需要 U_mean AND V_mean）
# ═══════════════════════════════════════════════════════════════════
if VIDEO_MODE:
    log("Path B/C/D skipped (video-mode)")
elif has_Umean and has_Vmean:
    log("=== Path B: U_mean contour + mean velocity streamlines ===")

    sliceB = Slice(Input=reader)
    sliceB.SliceType.Normal = [1, 0, 0]
    sliceB.SliceType.Origin = [xmid, (ymin+ymax)/2, (zmin+zmax)/2]
    sliceB.UpdatePipeline()

    calcB = Calculator(Input=sliceB)
    calcB.ResultArrayName = "U_mean_display"
    calcB.Function = "U_mean"
    calcB.UpdatePipeline()

    renB = CreateView("RenderView")
    renB.ViewSize = [IMAGE_W, IMAGE_H]
    try: renB.UseOffscreenRendering = 1     # 強制 offscreen，避免 X server 依賴
    except: pass
    setup_view(renB)

    dispB = Show(calcB, renB)
    dispB.Representation = "Surface"
    dispB.ColorArrayName = ["POINTS", "U_mean_display"]
    flatten_display(dispB)

    lutB = GetColorTransferFunction("U_mean_display")
    infoB = calcB.GetDataInformation().GetPointDataInformation().GetArrayInformation("U_mean_display")
    if infoB:
        lo_B = infoB.GetComponentRange(0)[0]
        hi_B = infoB.GetComponentRange(0)[1]
    else:
        lo_B, hi_B = 0.0, 1.0
    log("U_mean range: [%.4f, %.4f]" % (lo_B, hi_B))

    lutB.ColorSpace = "Step"
    lutB.RGBPoints = build_rgb_points(lo_B, hi_B, KEY_COLORS)
    harden_lut(lutB)
    dispB.LookupTable = lutB
    dispB.SetScalarBarVisibility(renB, True)
    setup_scalar_bar(lutB, renB, r"$\langle u \rangle / U_{ref}$", value_range=(lo_B, hi_B))

    # ── 用平均速度向量 (0, U_mean, V_mean) 畫流線 ──
    add_mean_streamlines(reader, renB, bounds)

    setup_camera(renB, bounds)
    setup_axes_grid(renB, bounds)

    Render(renB)
    SaveScreenshot(OUT_MEAN, renB, ImageResolution=[SAVE_W, SAVE_H],
                   OverrideColorPalette='WhiteBackground')
    log("Path B saved (step):       " + OUT_MEAN)

    # ── 連續色標版本 ──
    lutB.ColorSpace = "RGB"
    Render(renB)
    SaveScreenshot(OUT_MEAN_CONT, renB, ImageResolution=[SAVE_W, SAVE_H],
                   OverrideColorPalette='WhiteBackground')
    log("Path B saved (continuous): " + OUT_MEAN_CONT)

    Delete(renB)
    del renB
elif has_Umean:
    log("Path B skipped: has U_mean but missing V_mean (need both for mean streamlines)")
else:
    log("Path B skipped (no U_mean in VTK)")


# ═══════════════════════════════════════════════════════════════════
# §6  Path C: TKE（連續色標；若無統計資料則跳過）
# ═══════════════════════════════════════════════════════════════════
if VIDEO_MODE:
    pass  # 略過 Path C (video-mode)
elif has_TKE_computable:
    log("=== Path C: TKE (continuous colormap) ===")

    sliceC = Slice(Input=reader)
    sliceC.SliceType.Normal = [1, 0, 0]
    sliceC.SliceType.Origin = [xmid, (ymin+ymax)/2, (zmin+zmax)/2]
    sliceC.UpdatePipeline()

    calcC = Calculator(Input=sliceC)
    calcC.ResultArrayName = "TKE"
    if has_TKE:
        calcC.Function = TKE_name
        log("TKE source: direct field '%s'" % TKE_name)
    else:
        calcC.Function = "0.5*(%s + %s + %s)" % (uu_name, vv_name, ww_name)
        log("TKE source: 0.5*(%s + %s + %s)" % (uu_name, vv_name, ww_name))
    calcC.UpdatePipeline()

    renC = CreateView("RenderView")
    renC.ViewSize = [IMAGE_W, IMAGE_H]
    try: renC.UseOffscreenRendering = 1
    except: pass
    setup_view(renC)

    dispC = Show(calcC, renC)
    dispC.Representation = "Surface"
    dispC.ColorArrayName = ["POINTS", "TKE"]
    flatten_display(dispC)

    lutC = GetColorTransferFunction("TKE")
    infoC = calcC.GetDataInformation().GetPointDataInformation().GetArrayInformation("TKE")
    if infoC:
        lo_C = infoC.GetComponentRange(0)[0]
        hi_C = infoC.GetComponentRange(0)[1]
    else:
        lo_C, hi_C = 0.0, 1.0
    # 確保 TKE ≥ 0
    if lo_C < 0.0: lo_C = 0.0
    if hi_C <= lo_C: hi_C = lo_C + 1e-6
    log("TKE range: [%.6f, %.6f]" % (lo_C, hi_C))

    # TKE 預設連續色標
    lutC.ColorSpace = "RGB"
    lutC.RGBPoints = build_rgb_points(lo_C, hi_C, KEY_COLORS)
    harden_lut(lutC)
    dispC.LookupTable = lutC
    dispC.SetScalarBarVisibility(renC, True)
    setup_scalar_bar(lutC, renC, r"$k/U_{ref}^{2}$", value_range=(lo_C, hi_C))

    setup_camera(renC, bounds)
    setup_axes_grid(renC, bounds)

    Render(renC)
    SaveScreenshot(OUT_TKE_CONT, renC, ImageResolution=[SAVE_W, SAVE_H],
                   OverrideColorPalette='WhiteBackground')
    log("Path C saved (continuous): " + OUT_TKE_CONT)

    Delete(renC)
    del renC
else:
    log("Path C skipped (no TKE or Reynolds stress components in VTK)")


# ═══════════════════════════════════════════════════════════════════
# §7  Path D: Q-criterion 3D 等值面（對齊 4.Q-criterion_Animation.py 參考範本）
# ═══════════════════════════════════════════════════════════════════
#  ─ 版本：重寫版，完全對齊 4.Q-criterion_Animation.py 的輸出風格
#  ─ 色階：Rainbow Desaturated（ParaView preset，與參考範本一致）
#  ─ 著色變數：w (velocity_Z 分量)，範圍固定 [-0.02, 0.02]
#  ─ 畫布：1920×1080，相機斜俯視 (18,-8,6.3) → focal (2.25,4.5,1.5)
#  ─ Threshold：Qmax * 5%，再依 20K-80K cells 自適應
#  ─ 標註：上方置中 "Step=... | FTT=... | Ma_max=..." 文字
#
if VIDEO_MODE:
    pass  # 略過 Path D (video-mode)
elif has_velocity:
    log("=== Path D: Q-criterion isosurface (Rainbow Desaturated, ref-aligned) ===")

    # ── Step 1: 計算 Q-criterion ──
    gradD = Gradient(Input=reader)
    props = gradD.ListProperties()
    log("Gradient filter properties: " + str(props))

    input_set = False
    for prop_name in ['SelectInputScalars', 'InputArray', 'ScalarArray',
                      'SelectInputArray', 'SelectInputVectors']:
        if prop_name in props:
            try:
                setattr(gradD, prop_name, ['POINTS', 'velocity'])
                input_set = True
                log("Gradient using property: " + prop_name)
                break
            except:
                continue
    if not input_set:
        gradD.SetInputArrayToProcess(0, 0, 0, 0, 'velocity')
        log("Gradient using SetInputArrayToProcess fallback")

    gradD.ResultArrayName = 'VelocityGradient'
    gradD.ComputeQCriterion = 1
    gradD.QCriterionArrayName = 'Q'
    gradD.ComputeVorticity = 0
    gradD.ComputeDivergence = 0
    gradD.UpdatePipeline()

    q_info = gradD.GetDataInformation().GetPointDataInformation().GetArrayInformation('Q')
    q_range = q_info.GetComponentRange(0) if q_info else (0.0, 1.0)
    log("Q-criterion range: [%.8f, %.8f]" % (q_range[0], q_range[1]))

    if q_range[1] <= 0.0:
        log("Path D skipped: Qmax <= 0, no vortex structure")
    else:
        # ── Step 2: Q 等值面 — 以「Q > threshold 覆蓋 50% 點數」決定 threshold ──
        Q_THRESHOLD = q_range[1] * Q_AUTO_FRACTION  # fallback
        try:
            from paraview.servermanager import Fetch
            q_fetched = Fetch(gradD)
            q_arr = q_fetched.GetPointData().GetArray('Q') if q_fetched else None
            if q_arr is not None:
                npts = q_arr.GetNumberOfTuples()
                # 升序排列，取第 (1 - coverage)*N 位 → 上方 coverage*N 個點滿足 Q > threshold
                q_vals = sorted(q_arr.GetValue(i) for i in range(npts))
                idx = int(npts * (1.0 - Q_COVERAGE))
                idx = max(0, min(npts - 1, idx))
                Q_THRESHOLD = q_vals[idx]
                log("Q threshold @ %.0f%% coverage: %.8f  (npts=%d, rank=%d, Qmin=%.4g, Qmedian=%.4g, Qmax=%.4g)"
                    % (Q_COVERAGE*100, Q_THRESHOLD, npts, idx,
                       q_vals[0], q_vals[npts//2], q_vals[-1]))
            else:
                log("Q array not fetchable, fallback to Qmax*%.2f = %.8f"
                    % (Q_AUTO_FRACTION, Q_THRESHOLD))
        except Exception as _qe:
            log("Q percentile fallback: %s -> Qmax*%.2f = %.8f"
                % (str(_qe), Q_AUTO_FRACTION, Q_THRESHOLD))

        contourD = Contour(Input=gradD)
        contourD.ContourBy = ['POINTS', 'Q']
        contourD.Isosurfaces = [Q_THRESHOLD]
        contourD.ComputeNormals = 1
        contourD.ComputeScalars = 1
        contourD.UpdatePipeline()

        n_cells = contourD.GetDataInformation().GetNumberOfCells()
        log("Final Q threshold: %.8f  (%d iso cells)" % (Q_THRESHOLD, n_cells))

        # ── Step 3: 直接取 VTK 變數 v_inst (normal wall velocity) ──
        # 座標慣例：Z = wall-normal，code w = ERCOFTAC v
        # 注意 v_inst 在 fileIO.h L1678 已經 ÷Uref (= w_code/Uref = ERCOFTAC v/Ub),
        # 此處 Calculator 為 pass-through，僅改名以對齊 ColorBy 標籤；
        # 切勿再 / Q_U_REF (歷史 bug: 雙重 normalize 會讓色階全部 saturate)。
        calcD = Calculator(Input=contourD)
        calcD.Function = 'v_inst'
        calcD.ResultArrayName = 'v_over_Uref'
        calcD.UpdatePipeline()

        # ── Step 4: 1920×1080 渲染視圖（對齊參考範本 IMG_SIZE）──
        renD = CreateView("RenderView")
        renD.ViewSize = [Q_IMG_W, Q_IMG_H]
        try: renD.Background = [1.0, 1.0, 1.0]
        except: pass
        try: renD.UseOffscreenRendering = 1
        except: pass

        dispD = Show(calcD, renD)
        dispD.Representation = "Surface"
        dispD.Opacity = Q_OPACITY

        # normal wall velocity 著色 + Rainbow Desaturated 色階
        ColorBy(dispD, ('POINTS', 'v_over_Uref'))
        lutD = GetColorTransferFunction("v_over_Uref")
        # v_over_Uref ≡ v_inst = w_code/Uref (ERCOFTAC v/Ub), 已是無因次.
        # W_RANGE = [-0.02, 0.02] 是 lattice w_code 的範圍 (典型 LBM 數量級),
        # ÷ Q_U_REF 得到 v/Ub 範圍 ≈ [-0.343, 0.343], 用作 LUT 顯示界線.
        try: lutD.RescaleTransferFunction(W_RANGE[0] / Q_U_REF, W_RANGE[1] / Q_U_REF)
        except: pass
        try: lutD.ApplyPreset('Rainbow Desaturated', True)
        except: pass

        # Scalar bar — Title='v/U_ref' (MathText), normal wall velocity
        barD = GetScalarBar(lutD, renD)
        barD.Title = r'$v/U_{ref}$'
        barD.ComponentTitle = ''
        barD.Visibility = 1
        # 只放大 label 標題（Title 'w'），其餘維持原本比例
        barD.TitleFontSize = 32
        barD.LabelFontSize = 16
        barD.TitleColor = [0.0, 0.0, 0.0]
        barD.LabelColor = [0.0, 0.0, 0.0]
        try: barD.TitleFontFamily = 'Times'
        except: pass
        try: barD.LabelFontFamily = 'Times'
        except: pass
        try: barD.TitleBold = 1
        except: pass
        try: barD.LabelBold = 1
        except: pass
        try: barD.ScalarBarLength = 0.4
        except: pass
        try: dispD.SetScalarBarVisibility(renD, True)
        except: pass

        # 域 outline 作為參考（與參考範本相同樣式）
        try:
            outlineD = Show(reader, renD)
            outlineD.Representation = "Outline"
            outlineD.AmbientColor = [0.3, 0.3, 0.3]
            outlineD.DiffuseColor = [0.3, 0.3, 0.3]
            outlineD.LineWidth = 1.5
            outlineD.Opacity = 0.3
        except:
            pass

        # ── Step 4a: 底部曲面山丘壁面 (curvilinear bottom wall) ──
        # 薄殼：只取 NormalZ < -0.3 的朝下面（底壁），排除側壁與頂壁
        WALL_OPACITY = 0.4
        try:
            surfD = ExtractSurface(Input=reader)
            surfD.UpdatePipeline()

            normD = GenerateSurfaceNormals(Input=surfD)
            normD.ComputeCellNormals = 1
            normD.UpdatePipeline()

            calcNz = Calculator(Input=normD)
            calcNz.ResultArrayName = "NormalZ"
            calcNz.Function = "Normals_Z"
            calcNz.UpdatePipeline()

            wallBot = Threshold(Input=calcNz)
            wallBot.Scalars = ['POINTS', 'NormalZ']
            wallBot.LowerThreshold = -1.0
            wallBot.UpperThreshold = -0.3
            wallBot.ThresholdMethod = 'Between'
            wallBot.UpdatePipeline()

            nWall = wallBot.GetDataInformation().GetNumberOfPoints()
            log("Bottom wall (NormalZ < -0.3): %d points" % nWall)

            if nWall > 0:
                dispWall = Show(wallBot, renD)
                dispWall.Representation = "Surface"
                dispWall.Opacity = WALL_OPACITY
                dispWall.AmbientColor = [0.7, 0.7, 0.7]
                dispWall.DiffuseColor = [0.7, 0.7, 0.7]
                ColorBy(dispWall, None)
                dispWall.SetScalarBarVisibility(renD, False)
                log("Bottom wall displayed at opacity %.2f" % WALL_OPACITY)
        except Exception as _we:
            log("Bottom wall skipped: " + str(_we))

        # 補缺角邊線 (4.5, 0, 0) → (4.5, 0, 1)
        try:
            edgeLine = Line()
            edgeLine.Point1 = [4.5, 0.0, 0.0]
            edgeLine.Point2 = [4.5, 0.0, 1.0]
            edgeLine.Resolution = 1
            edgeLine.UpdatePipeline()
            dispEdgeLine = Show(edgeLine, renD)
            dispEdgeLine.Representation = "Wireframe"
            dispEdgeLine.AmbientColor = [0.3, 0.3, 0.3]
            dispEdgeLine.DiffuseColor = [0.3, 0.3, 0.3]
            dispEdgeLine.LineWidth = 1.5
            dispEdgeLine.Opacity = 0.3
            dispEdgeLine.SetScalarBarVisibility(renD, False)
            log("Edge line (4.5,0,0)→(4.5,0,1) added")
        except Exception as _el:
            log("Edge line skipped: " + str(_el))

        # ── 座標箭頭 — Krank 風格，服貼 (4.5, 0, 0) 角落 ──
        renD.OrientationAxesVisibility = 0
        _AX_LEN = 0.7
        _AX_R = 0.012
        _AX_O = [0.0, 0.0, 0.0]
        _arrows = [
            ([1, 0, 0], "x"),
            ([0, 1, 0],  "y"),
            ([0, 0, 1],  "z"),
        ]
        _col = [0.0, 0.0, 0.0]
        for _dir, _lbl in _arrows:
            try:
                _ep = [_AX_O[i] + _dir[i] * _AX_LEN for i in range(3)]
                _ln = Line()
                _ln.Point1 = _AX_O
                _ln.Point2 = _ep
                _ln.Resolution = 1
                _tb = Tube(Input=_ln)
                _tb.Radius = _AX_R
                _tb.NumberofSides = 8
                _tb.UpdatePipeline()
                _dp = Show(_tb, renD)
                _dp.Representation = "Surface"
                _dp.AmbientColor = _col
                _dp.DiffuseColor = _col
                _dp.MapScalars = 0
                _dp.SetScalarBarVisibility(renD, False)

                _cn = Cone()
                _tip = [_ep[i] + _dir[i] * 0.05 for i in range(3)]
                _cn.Center = _tip
                _cn.Direction = _dir
                _cn.Height = 0.12
                _cn.Radius = 0.035
                _cn.Resolution = 12
                _cn.Capping = 1
                _cn.UpdatePipeline()
                _dpc = Show(_cn, renD)
                _dpc.Representation = "Surface"
                _dpc.AmbientColor = _col
                _dpc.DiffuseColor = _col
                _dpc.MapScalars = 0
                _dpc.SetScalarBarVisibility(renD, False)

                # (x/y/z 標籤改用 PIL 後製疊加，避免 Text3D 方向問題)
                log("Arrow '%s' at (4.5,0,0) placed" % _lbl)
            except Exception as _ae:
                log("Arrow '%s' skipped: %s" % (_lbl, str(_ae)))

        # ── Step 4b: 在 Q-criterion 3D 視圖上疊加 2D slice contour 平面 ──
        # Slice 1: Y=9.0 (streamwise 末端, XZ 平面) → 用 Y=8.95 避開邊界
        # Slice 2: X=0.0 (spanwise 邊緣, YZ 平面) → 用 X=0.05 避開邊界
        # 著色: streamwise velocity (velocity_Y), Rainbow Desaturated, 半透明
        SLICE_OPACITY = 0.7
        SLICE_Y_POS = ymax - 0.05
        SLICE_X_POS = xmin + 0.05
        try:
            # ── Slice @ Y ≈ 9.0 ──
            sliceY9 = Slice(Input=reader)
            sliceY9.SliceType.Normal = [0, 1, 0]
            sliceY9.SliceType.Origin = [(xmin+xmax)/2, SLICE_Y_POS, (zmin+zmax)/2]
            sliceY9.UpdatePipeline()

            nY9 = sliceY9.GetDataInformation().GetNumberOfPoints()
            log("Slice Y=%.2f: %d points" % (SLICE_Y_POS, nY9))

            calcY9 = Calculator(Input=sliceY9)
            calcY9.ResultArrayName = "u_slice_Y"
            calcY9.Function = "velocity_Y"
            calcY9.UpdatePipeline()

            dispY9 = Show(calcY9, renD)
            dispY9.Representation = "Surface"
            dispY9.Opacity = SLICE_OPACITY

            ColorBy(dispY9, ('POINTS', 'u_slice_Y'))
            lutSliceY = GetColorTransferFunction("u_slice_Y")
            infoSliceY = calcY9.GetDataInformation().GetPointDataInformation().GetArrayInformation("u_slice_Y")
            if infoSliceY:
                slo_y = infoSliceY.GetComponentRange(0)[0]
                shi_y = infoSliceY.GetComponentRange(0)[1]
            else:
                slo_y, shi_y = -0.5, 1.4
            if abs(shi_y - slo_y) < 1e-10:
                slo_y, shi_y = -0.5, 1.4
            try: lutSliceY.RescaleTransferFunction(slo_y, shi_y)
            except: pass
            try: lutSliceY.ApplyPreset('Rainbow Desaturated', True)
            except: pass
            harden_lut(lutSliceY)
            dispY9.SetScalarBarVisibility(renD, False)
            log("Slice Y=%.2f: u_slice range [%.4f, %.4f]" % (SLICE_Y_POS, slo_y, shi_y))

            # ── Slice @ X ≈ 0.0 ──
            sliceX0 = Slice(Input=reader)
            sliceX0.SliceType.Normal = [1, 0, 0]
            sliceX0.SliceType.Origin = [SLICE_X_POS, (ymin+ymax)/2, (zmin+zmax)/2]
            sliceX0.UpdatePipeline()

            nX0 = sliceX0.GetDataInformation().GetNumberOfPoints()
            log("Slice X=%.2f: %d points" % (SLICE_X_POS, nX0))

            calcX0 = Calculator(Input=sliceX0)
            calcX0.ResultArrayName = "u_slice_X"
            calcX0.Function = "velocity_Y"
            calcX0.UpdatePipeline()

            dispX0 = Show(calcX0, renD)
            dispX0.Representation = "Surface"
            dispX0.Opacity = SLICE_OPACITY

            ColorBy(dispX0, ('POINTS', 'u_slice_X'))
            lutSliceX = GetColorTransferFunction("u_slice_X")
            infoSliceX = calcX0.GetDataInformation().GetPointDataInformation().GetArrayInformation("u_slice_X")
            if infoSliceX:
                slo_x = infoSliceX.GetComponentRange(0)[0]
                shi_x = infoSliceX.GetComponentRange(0)[1]
            else:
                slo_x, shi_x = -0.5, 1.4
            if abs(shi_x - slo_x) < 1e-10:
                slo_x, shi_x = -0.5, 1.4
            try: lutSliceX.RescaleTransferFunction(slo_x, shi_x)
            except: pass
            try: lutSliceX.ApplyPreset('Rainbow Desaturated', True)
            except: pass
            harden_lut(lutSliceX)
            dispX0.SetScalarBarVisibility(renD, False)
            log("Slice X=%.2f: u_slice range [%.4f, %.4f]" % (SLICE_X_POS, slo_x, shi_x))
        except Exception as _se:
            log("Slice contours skipped: " + str(_se))

        # ── Step 5: (已移除) 上方文字標註 FTT / Ma_max → 保留純粹瞬間快照 ──

        # ── Step 6: 固定斜俯視相機（完全對齊參考範本）──
        # focal=(2.25, 4.5, 1.5) 域中心, position=(18, -8, 6.3), ViewAngle=21.08
        camD = renD.GetActiveCamera() if hasattr(renD, 'GetActiveCamera') else GetActiveCamera()
        camD.SetFocalPoint(2.25, 4.5, 1.5)
        camD.SetPosition(18.0, -8.0, 6.3)
        camD.SetViewUp(0, 0, 1)
        camD.SetViewAngle(21.080246913580243)
        # Dolly 維持原比例（避免主體超出邊界），頂端空白改用後製剪切處理
        camD.Dolly(1.1)

        # ── Step 7: AxesGrid (對齊 Path A/B/C 樣式：MathText $x/H$ Times Bold 36pt) ──
        try:
            agD = renD.AxesGrid
            agD.Visibility = 1
            agD.XTitle = ""
            agD.YTitle = ""
            agD.ZTitle = ""
            try:
                agD.XTitleFontFamily = "Times"
                agD.YTitleFontFamily = "Times"
                agD.ZTitleFontFamily = "Times"
                agD.XLabelFontFamily = "Times"
                agD.YLabelFontFamily = "Times"
                agD.ZLabelFontFamily = "Times"
            except: pass
            try:
                agD.XTitleBold = 1; agD.YTitleBold = 1; agD.ZTitleBold = 1
                agD.XLabelBold = 1; agD.YLabelBold = 1; agD.ZLabelBold = 1
            except: pass
            try:
                agD.XTitleColor = [0,0,0]; agD.YTitleColor = [0,0,0]; agD.ZTitleColor = [0,0,0]
                agD.XLabelColor = [0,0,0]; agD.YLabelColor = [0,0,0]; agD.ZLabelColor = [0,0,0]
                agD.GridColor = [0,0,0]
            except: pass
            agD.XTitleFontSize = 36
            agD.YTitleFontSize = 36
            agD.ZTitleFontSize = 36
            try:
                agD.XLabelFontSize = 20
                agD.YLabelFontSize = 20
                agD.ZLabelFontSize = 20
            except: pass
            try:
                agD.XAxisLabelOffset = 40
                agD.YAxisLabelOffset = 40
                agD.ZAxisLabelOffset = 60
            except: pass
            try:
                agD.XAxisTitleOffset = 65
                agD.YAxisTitleOffset = 65
                agD.ZAxisTitleOffset = 95
            except: pass
            try:
                agD.XTitleOffset = 65
                agD.YTitleOffset = 65
                agD.ZTitleOffset = 95
            except: pass
            try:
                agD.XLabelOffset = 40
                agD.YLabelOffset = 40
                agD.ZLabelOffset = 60
            except: pass
            # 整數刻度: X=[0,1,2,3,4], Y=[0,1,...,9], Z=[0,1,2,3]
            try:
                for _ax in ("X", "Y", "Z"):
                    try: setattr(agD, _ax+"AxisNotation", "Printf")
                    except: pass
                    try: setattr(agD, _ax+"AxisPrintfFormat", "%g")
                    except: pass
            except: pass
            try:
                agD.XAxisUseCustomLabels = 1
                agD.XAxisLabels = [0, 1, 2, 3, 4, 4.5]
                agD.YAxisUseCustomLabels = 1
                agD.YAxisLabels = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
                agD.ZAxisUseCustomLabels = 1
                agD.ZAxisLabels = [0, 1, 2, 3.036]
            except: pass
        except Exception as e:
            log("Q-crit axes grid skipped: " + str(e))

        # ── Step 8: Render + save ──
        Render(renD)
        SaveScreenshot(OUT_QCRIT, renD,
                       ImageResolution=[Q_IMG_W, Q_IMG_H],
                       OverrideColorPalette='WhiteBackground',
                       TransparentBackground=0)
        log("Path D saved: " + OUT_QCRIT)

        # ── Step 9: 後製裁切 + PIL 疊加 x/y/z 箭頭標籤 ──
        try:
            from PIL import Image as _PILImage, ImageDraw as _PILDraw, ImageFont as _PILFont
            _im = _PILImage.open(OUT_QCRIT)
            _w, _h = _im.size
            _top_cut = int(_h * 0.15)
            _im_crop = _im.crop((0, _top_cut, _w, _h))

            # 在裁切後的圖上標箭頭名稱 (Krank convention)
            # x=streamwise(+Y), y=wall-normal(+Z), z=spanwise(-X)
            _arrow_labels = [
                ([0.0 + _AX_LEN + 0.20, 0.0, 0.0],  "z", "spanwise"),
                ([0.0, _AX_LEN + 0.20, 0.0],          "x", "streamwise"),
                ([0.0 - 0.20, 0.12, _AX_LEN + 0.12], "y", "wall-normal"),
            ]
            _arrow_px = []
            try:
                _ren = renD.GetRenderWindow().GetRenderers().GetFirstRenderer()
                from vtkmodules.vtkRenderingCore import vtkCoordinate as _vtkC
                for _apos, _altr, _adir in _arrow_labels:
                    _vc = _vtkC()
                    _vc.SetCoordinateSystemToWorld()
                    _vc.SetValue(*_apos)
                    _dp = _vc.GetComputedDisplayValue(_ren)
                    _px = int(_dp[0])
                    _py = int(Q_IMG_H - _dp[1]) - _top_cut
                    _arrow_px.append((_px, _py, _altr, _adir))
                log("Arrow label pixels: %s" % str(_arrow_px))
            except Exception as _vce:
                log("WorldToDisplay fallback: %s" % str(_vce))
                _cw, _ch = _w, _h - _top_cut
                _arrow_px = [
                    (int(_cw*0.09), int(_ch*0.88), "z", "spanwise"),
                    (int(_cw*0.15), int(_ch*0.90), "x", "streamwise"),
                    (int(_cw*0.11), int(_ch*0.74), "y", "wall-normal"),
                ]

            _draw = _PILDraw.Draw(_im_crop)
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as _plt
            from io import BytesIO as _BytesIO
            matplotlib.rcParams["mathtext.fontset"] = "cm"

            def _latex_label(tex, fontsize=22, dpi=120):
                fig = _plt.figure(figsize=(0.4, 0.4))
                fig.text(0.5, 0.5, tex, fontsize=fontsize,
                         ha="center", va="center", color="black")
                buf = _BytesIO()
                fig.savefig(buf, format="png", dpi=dpi, transparent=True,
                            bbox_inches="tight", pad_inches=0.01)
                _plt.close(fig)
                buf.seek(0)
                return _PILImage.open(buf).convert("RGBA")

            for _apx, _apy, _altr, _adir in _arrow_px:
                _lbl_img = _latex_label("$%s$" % _altr)
                _lw, _lh = _lbl_img.size
                _paste_x = _apx - _lw // 2
                _paste_y = _apy - _lh // 2
                _im_crop.paste(_lbl_img, (_paste_x, _paste_y), _lbl_img)

            _im_crop.save(OUT_QCRIT)
            log("Path D top-crop + arrow labels: new size %dx%d"
                % (_w, _h - _top_cut))
        except Exception as _e:
            log("Path D post-process skipped: " + str(_e))

        Delete(renD)
        del renD
else:
    log("Path D skipped (no 'velocity' vector field in VTK)")

