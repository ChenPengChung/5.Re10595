"""
ParaView Python Script: 渦流結構可視化 (Q-criterion isosurface)
================================================================
用法:
  1. 開啟 ParaView
  2. Tools → Python Shell → Run Script → 選此檔案
  或：
  pvpython paraview_vortex.py

輸出:
  - vortex_structure.png (斜俯視 Q-criterion 等值面, w 著色)

參考: Periodic Hill (LX=4.5, LY=9.0, LZ=3.036, Re=700)
"""

from paraview.simple import *
import os, glob, re

# ============================================================
# 設定區 (可自行調整)
# ============================================================
Q_THRESHOLD = 0.001     # 渦度量值等值面閾值 (自動微調, 此值為 fallback)
OPACITY     = 0.8        # 等值面透明度 (0~1)
BG_COLOR    = [1, 1, 1]  # 背景色 (白色)
IMG_SIZE    = [1920, 1080]  # 輸出圖片解析度

# 色標範圍 (w 速度, 用於著色)
W_RANGE     = [-0.02, 0.02]  # 配合參考圖的 w 範圍

# ============================================================
# 自動偵測最新 VTK 檔案
# ============================================================
script_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in dir() else os.getcwd()

# 搜尋 velocity_merged_*.vtk, 取步數最大的
candidates = glob.glob(os.path.join(script_dir, "velocity_merged_*.vtk"))
if not candidates:
    candidates = glob.glob(os.path.join(script_dir, "..", "result", "velocity_merged_*.vtk"))

if not candidates:
    raise FileNotFoundError("No velocity_merged_*.vtk found in result/ directory")

def extract_step(path):
    m = re.search(r'velocity_merged_(\d+)\.vtk', os.path.basename(path))
    return int(m.group(1)) if m else 0

vtk_path = max(candidates, key=extract_step)
print(f"Loading (latest): {vtk_path}")

# ============================================================
# Step 1: 載入 VTK 檔案
# ============================================================
reader = LegacyVTKReader(FileNames=[vtk_path])
reader.UpdatePipeline()

# 印出可用的陣列
info = reader.GetDataInformation()
pd = info.GetPointDataInformation()
print(f"\n=== VTK Data Info ===")
print(f"Points: {info.GetNumberOfPoints()}")
print(f"Available arrays:")
for i in range(pd.GetNumberOfArrays()):
    arr = pd.GetArrayInformation(i)
    print(f"  - {arr.GetName()} ({arr.GetNumberOfComponents()} components)")

# ============================================================
# Step 2: 計算 Q-criterion (從速度梯度張量)
# ============================================================
grad = Gradient(Input=reader)

# 探測 ParaView 版本的正確屬性名稱
props = grad.ListProperties()
print(f"  Gradient filter properties: {props}")

# 嘗試設定輸入陣列 (不同 PV 版本屬性名不同)
input_set = False
for prop_name in ['SelectInputScalars', 'InputArray', 'ScalarArray', 'SelectInputArray', 'SelectInputVectors']:
    if prop_name in props:
        try:
            setattr(grad, prop_name, ['POINTS', 'velocity'])
            input_set = True
            print(f"  Using property: {prop_name}")
            break
        except:
            continue

if not input_set:
    # 最後手段: 透過 SetInputArrayToProcess API
    grad.SetInputArrayToProcess(0, 0, 0, 0, 'velocity')  # (idx, port, connection, field_assoc, name)
    print(f"  Using SetInputArrayToProcess fallback")

grad.ResultArrayName = 'VelocityGradient'
grad.ComputeQCriterion = 1       # 直接輸出 Q-criterion
grad.QCriterionArrayName = 'Q'
grad.ComputeVorticity = 0
grad.ComputeDivergence = 0
grad.UpdatePipeline()

# Q-criterion 範圍
q_info = grad.GetDataInformation().GetPointDataInformation().GetArrayInformation('Q')
q_range = q_info.GetComponentRange(0)
print(f"\n=== Q-criterion Range ===")
print(f"  Min: {q_range[0]:.8f}")
print(f"  Max: {q_range[1]:.8f}")

# Q > 0 代表旋轉主導 (渦核), 取正值範圍的一個百分比
Q_THRESHOLD = q_range[1] * 0.05   # 5% of max Q (渦核比較清晰)
print(f"  Auto threshold (5% of Qmax): {Q_THRESHOLD:.8f}")

# ============================================================
# Step 3: Q-criterion 等值面 (Contour)
# ============================================================
contour = Contour(Input=grad)
contour.ContourBy = ['POINTS', 'Q']
contour.Isosurfaces = [Q_THRESHOLD]
contour.ComputeNormals = 1
contour.ComputeScalars = 1
contour.UpdatePipeline()

n_cells = contour.GetDataInformation().GetNumberOfCells()
print(f"\n=== Contour Info ===")
print(f"  Isosurface cells: {n_cells}")

# 自適應閾值: 若結構太多則提高, 太少則降低
if n_cells > 200000:
    # 結構太多, 逐步提高
    for fraction in [0.10, 0.15, 0.20, 0.30, 0.40]:
        Q_THRESHOLD = q_range[1] * fraction
        contour.Isosurfaces = [Q_THRESHOLD]
        contour.UpdatePipeline()
        n_cells = contour.GetDataInformation().GetNumberOfCells()
        print(f"  ↑ Trying {fraction*100:.0f}% = {Q_THRESHOLD:.8f} -> {n_cells} cells")
        if n_cells < 100000:
            break
elif n_cells < 2000:
    # 結構太少, 逐步降低
    for fraction in [0.02, 0.01, 0.005, 0.002, 0.001]:
        Q_THRESHOLD = q_range[1] * fraction
        contour.Isosurfaces = [Q_THRESHOLD]
        contour.UpdatePipeline()
        n_cells = contour.GetDataInformation().GetNumberOfCells()
        print(f"  ↓ Trying {fraction*100:.1f}% = {Q_THRESHOLD:.8f} -> {n_cells} cells")
        if n_cells >= 5000:
            break

print(f"\n  FINAL Q threshold: {Q_THRESHOLD:.8f}  ({n_cells} cells)")

# ============================================================
# Step 4: 提取 w 分量用於著色
# ============================================================
# 用 Calculator 從 velocity 向量提取 w (第 3 分量)
calc_w = Calculator(Input=contour)
calc_w.Function = 'velocity_Z'       # ParaView 語法: velocity 的 Z 分量
calc_w.ResultArrayName = 'w_velocity'
calc_w.UpdatePipeline()

# ============================================================
# Step 5: 設定渲染視圖
# ============================================================
renderView = GetActiveViewOrCreate('RenderView')
renderView.ViewSize = IMG_SIZE
renderView.Background = BG_COLOR

# 顯示等值面
display = Show(calc_w, renderView)
display.Representation = 'Surface'
display.Opacity = OPACITY

# 以 w 速度著色
ColorBy(display, ('POINTS', 'w_velocity'))
w_lut = GetColorTransferFunction('w_velocity')
w_lut.RescaleTransferFunction(W_RANGE[0], W_RANGE[1])

# 使用 Rainbow Uniform 色標
w_lut.ApplyPreset('Rainbow Uniform', True)

# 顯示色標列
w_bar = GetScalarBar(w_lut, renderView)
w_bar.Title = 'w'
w_bar.ComponentTitle = ''
w_bar.Visibility = 1
w_bar.TitleFontSize = 16
w_bar.LabelFontSize = 14

# ============================================================
# Step 6: 設定相機角度 (CFD 標準 Q-criterion 斜俯視角)
# ============================================================
renderView.ResetCamera()
camera = renderView.GetActiveCamera()

# 物理域: x=[0,4.5], y=[0,9], z=[0,3.036]
domain_center = [2.25, 4.5, 1.5]  # 域中心

camera.SetFocalPoint(*domain_center)
# 低仰角斜俯視 (~15°), 從右前上方淺角度俯瞰
camera.SetPosition(18, -8, 6.3)
camera.SetViewUp(0, 0, 1)          # z 軸朝上
camera.SetViewAngle(30)            # 適中視野角

renderView.ResetCamera()
camera.Dolly(0.9)

# ============================================================
# Step 7: 加入座標軸標註
# ============================================================
renderView.AxesGrid.Visibility = 1
renderView.AxesGrid.XTitle = 'X Axis'
renderView.AxesGrid.YTitle = 'Y Axis'
renderView.AxesGrid.ZTitle = 'Z Axis'
renderView.AxesGrid.XTitleFontSize = 14
renderView.AxesGrid.YTitleFontSize = 14
renderView.AxesGrid.ZTitleFontSize = 14

# ============================================================
# Step 8: 加入半透明底部山丘輪廓 (用壁面 slice)
# ============================================================
# 底壁 = 計算域最底面, 用 Clip 取出 z < hill_height 的區域
# 或直接顯示原始網格的外表面作為參考
try:
    outline = Show(reader, renderView)
    outline.Representation = 'Outline'
    outline.AmbientColor = [0.3, 0.3, 0.3]
    outline.DiffuseColor = [0.3, 0.3, 0.3]
    outline.LineWidth = 1.5
    outline.Opacity = 0.3
except:
    pass

# ============================================================
# Step 9: 渲染 + 截圖
# ============================================================
Render()

# 儲存截圖
output_dir = script_dir
output_png = os.path.join(output_dir, "vortex_structure.png")
SaveScreenshot(output_png, renderView,
               ImageResolution=IMG_SIZE,
               TransparentBackground=0)
print(f"\n=== Screenshot saved: {output_png} ===")

print(f"""
======================================================
  渦流結構可視化完成！(Q-criterion)
  
  Q-criterion 範圍: [{q_range[0]:.8f}, {q_range[1]:.8f}]
  使用閾值: {Q_THRESHOLD:.8f}
  等值面 cells: {n_cells}
  色標: w velocity [{W_RANGE[0]}, {W_RANGE[1]}]
  
  調整建議:
  - 結構太少/看不到 → 降低 Q_THRESHOLD (減小百分比)
  - 結構太多/糊成一團 → 提高 Q_THRESHOLD (增大百分比)
  - 色標範圍 → 修改 W_RANGE
  
  輸出檔案:
  - {output_png}
======================================================
""")

# 完成 — 在 ParaView GUI 中可直接互動操作視圖
