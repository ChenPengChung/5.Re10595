#!/usr/bin/env python3
"""
ERCOFTAC UFR 3-30 Periodic Hill Benchmark Comparison
GILBM vs multiple benchmark sources (DNS/LES/Experiment)
=====================================================
用法:
  python3 2.Benchmark.py               # 互動詢問 Re
  python3 2.Benchmark.py --Re 700      # 指定 Re=700
  python3 2.Benchmark.py --Re 5600     # 指定 Re=5600

輸出:
  benchmark_Umean_Re{N}.png           — <U>/Ub offset profile
  benchmark_RS_Re{N}.png              — RS + k + <V>/Ub 5-panel offset
  benchmark_all_Re{N}.pdf/png         — 6x10 per-station 全比較圖

Benchmark 資料來源:
  Breuer et al. (2009), Computers & Fluids, 38, 433-457.
  Rapp & Manhart (2011), Experiments in Fluids.
  ERCOFTAC UFR 3-30 database.
  請將 benchmark 資料放在 result/benchmark/ 目錄下.

座標映射 (Code <-> VTK/ERCOFTAC 統一公約):
  Code x (i) = spanwise     -> VTK/ERCOFTAC w (W)
  Code y (j) = streamwise   -> VTK/ERCOFTAC u (U)
  Code z (k) = wall-normal  -> VTK/ERCOFTAC v (V)

  ERCOFTAC col 2: <U>/Ub     = VTK U_mean   (streamwise = code v)
  ERCOFTAC col 3: <V>/Ub     = VTK V_mean   (wall-normal = code w)
  ERCOFTAC col 4: <u'u'>/Ub² = VTK uu_RS    (stream x stream)
  ERCOFTAC col 5: <v'v'>/Ub² = VTK vv_RS    (wallnorm x wallnorm)
  ERCOFTAC col 6: <u'v'>/Ub² = VTK uv_RS    (stream x wallnorm)
  ERCOFTAC col 7: k/Ub²      = VTK k_TKE

層流 wall-normal 瞬時分量 w* = w / Uref（可選後處理）:
  啟動時自上一層目錄鏈尋找 variables.h 或 variable.h，解析 #define Uref（印出 U_REF）。
  本檔位於 Edit3_Re5600newmesh result/：LAMINAR_W_DIVIDE_BY_UREF=False（與 fileIO 已 ÷Uref 一致）。
  若須於 Python 再 ÷Uref，改 True；或僅用 Desktop 根目錄 2.Benchmark.py（該檔預設 True）。
"""

import os, sys, glob, argparse, re
import numpy as np

# Windows console UTF-8 support
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ── auto-detect backend ─────────────────────────────────────────
try:
    import matplotlib as mpl
    if not os.environ.get('DISPLAY') and sys.platform != 'win32':
        mpl.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    HAS_MPL = True
except ImportError:
    HAS_MPL = False
    print("[WARN] matplotlib not found; will export CSV only.")

# ================================================================
# Configuration
# ================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in dir() else os.getcwd()
VTK_DIR = SCRIPT_DIR
VTK_PATTERN = "velocity_merged_*.vtk"
BENCH_DIR = os.path.join(SCRIPT_DIR, "benchmark")

XH_STATIONS = [0.05, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
_STN_TO_XH = {1: 0.05, 2: 0.5, 3: 1.0, 4: 2.0, 5: 3.0,
              6: 4.0, 7: 5.0, 8: 6.0, 9: 7.0, 10: 8.0}

H_HILL = 1.0
LY     = 9.0
LZ     = 3.036

# ── 使用者自訂 offset-plot 可視化倍率 ──────────────────────────
# 所有數據來源（VTK、Tecplot、ERCOFTAC）的 dict 均儲存真實物理值，
# 不含任何可視化倍率。倍率只在繪圖時施加。
#
# FIELD_PLOT_SCALE: 每個輸出欄位的可視化放大倍率
#   key = 欄位名 (與 profile dict key 對應)
#   value = 倍率 (1.0 = 原始比例)
#
# 此 dict 在啟動時由互動式詢問填入，或由命令列引數 --scales 指定。
# 例: --scales "U:0.8,W:3.2,uu:30,ww:30,uw:60,k:20"
FIELD_PLOT_SCALE = {}  # 稍後由 _init_field_scales() 填入

# ── 預設倍率 (依 Re 區間) ─────────────────────────────────────
_DEFAULT_SCALES_LAMINAR = {
    "U": 0.8, "V": 3.2,    # V = wall-normal; 3.2 = 0.8 * 4.0
}
_DEFAULT_SCALES_TURBULENT = {
    "U": 1.0, "V": 8.0,
    "uu": 30, "vv": 30, "uv": 60, "k": 30,
}

# 層流：VTK 法向速度（v_inst / velocity_z / V_mean）是否再 ÷ variables.h 的 Uref
# True：假設 VTK 內為格子速度；False：與 fileIO 一致（輸出已 ÷Uref）
LAMINAR_W_DIVIDE_BY_UREF = False


def find_variables_header(start_dir, max_up=12):
    """Walk upward from start_dir; return path to variables.h or variable.h."""
    cur = os.path.abspath(start_dir)
    for _ in range(max_up + 1):
        for name in ("variables.h", "variable.h"):
            p = os.path.join(cur, name)
            if os.path.isfile(p):
                return p
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def parse_uref_from_header(filepath):
    """Read first #define Uref or U_ref numeric literal from a C header."""
    uref_pat = re.compile(r"#\s*define\s+Uref\s+([0-9.]+(?:[eE][+-]?\d+)?)")
    uref_alt = re.compile(r"#\s*define\s+U_ref\s+([0-9.]+(?:[eE][+-]?\d+)?)")
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.split("//")[0]
                m = uref_pat.search(s) or uref_alt.search(s)
                if m:
                    return float(m.group(1))
    except OSError:
        pass
    return None


def resolve_uref_for_postprocess(script_dir, fallback=0.0503):
    """Uref for non-dimensional wall-normal velocity; fallback matches typical variables.h."""
    hdr = find_variables_header(os.path.dirname(script_dir))
    if hdr is None:
        hdr = find_variables_header(script_dir)
    u = parse_uref_from_header(hdr) if hdr else None
    if u is None or u <= 0:
        print(f"[WARN] Uref not found in variables.h; using fallback U_REF = {fallback}")
        return float(fallback), hdr
    print(f"[INFO] U_REF = {u} (from {hdr})")
    return float(u), hdr

# ════════════════════════════════════════════════════════════════════
#  Benchmark Sources Definition — Journal Visual Identity System
# ════════════════════════════════════════════════════════════════════
#
#  DESIGN RATIONALE — Publication-quality (JFM / PoF / JCP style)
#  ────────────────────────────────────────────────────────────────
#
#  Observed convention in top-journal Periodic Hill figures:
#
#  ┌─────────────────────────────────────────────────────────────────┐
#  │ P1. Simulation (present work) = SOLID LINE, no markers.        │
#  │     This is always the most prominent visual element.           │
#  │                                                                 │
#  │ P2. ALL benchmarks = SCATTER ONLY (unfilled markers, no line). │
#  │     Sparse: every 3rd–5th data point via markevery.            │
#  │     This keeps profiles readable, avoids thick overlap clutter. │
#  │                                                                 │
#  │ P3. COLOUR + MARKER SHAPE dual encoding                        │
#  │                                                                 │
#  │   Source       Color      Marker  markevery                     │
#  │   ──────────   ─────────  ──────  ─────────                     │
#  │   GILBM(sim)   black      (none)  — (solid line)               │
#  │   LESOCC       blue       □ s     4                             │
#  │   MGLET        green      ◇ D     4                             │
#  │   Experiment   magenta    ○ o     3                             │
#  │   LBM          red        △ ^     4                             │
#  │   ISLBM        cyan       ▽ v     4                             │
#  │                                                                 │
#  │ P4. SIZE HIERARCHY                                              │
#  │   Unfilled markers, 3–4 pt, edge 0.5–0.7 pt.                  │
#  │   Experiment uses smallest (2.5 pt) + densest (every 3rd).     │
#  └─────────────────────────────────────────────────────────────────┘
#
# Default sparse factor: show every Nth benchmark data point.
BENCH_MARKEVERY = 4

BENCHMARK_SOURCES = {
    'LESOCC': {
        'dir_name':  'LESOCC (Breuer et al. 2009)',
        'label':     r'$\mathit{LESOCC}$-Breuer $\mathit{et\,al.}$',
        'delimiter': None,
        'color':     '#2255CC',   # blue
        'marker':    'o',         # circle  (body-fitted FVM family)
        'markersize': 3.5,
        'markevery':  4,
    },
    'MGLET': {
        'dir_name':  'MGLET (Breuer et al. 2009)',
        'label':     r'$\mathit{MGLET}$-Breuer $\mathit{et\,al.}$',
        'delimiter': None,
        'color':     '#DAA520',   # yellow (goldenrod)
        'marker':    'D',         # diamond (body-fitted FVM family)
        'markersize': 3.5,
        'markevery':  4,
    },
    'MGLET_Manhart': {
        'dir_name':  'MGLET (Manhart et al. 2011)',
        'label':     r'$\mathit{MGLET}$-Manhart $\mathit{et\,al.}$',
        'delimiter': None,
        'color':     '#DAA520',   # yellow (goldenrod) — same as MGLET family
        'marker':    'D',         # diamond — same as MGLET family
        'markersize': 3.5,
        'markevery':  4,
    },
    'Experiment': {
        'dir_name':  'Exp. (Rapp et al. 2011)',
        'label':     r'Exp.-Rapp $\mathit{et\,al.}$',
        'delimiter': ',',
        'color':     '#228B22',   # green (forest green)
        'marker':    '^',         # up-triangle  (experimental)
        'markersize': 3.5,
        'markevery':  3,          # densest data → every 3rd pt
        'filter_above_hill': True, # only draw points above hill surface
    },
    'LBM': {
        'dir_name':  'LBM',
        'label':     r'$\mathit{LBM}$',
        'delimiter': None,
        'color':     '#1A1A1A',   # near-black (contrasts red sim)
        'marker':    '^',         # up-triangle  (Boltzmann family)
        'markersize': 3.5,
        'markevery':  4,
        'format':    'tecplot',
        'tecplot_variant': 'legacy',
        'w_prescale': 4.0,
    },
    'ISLBM': {
        'dir_name':  'ISLBM',
        'label':     r'$\mathit{ISLBM}$',
        'delimiter': None,
        'color':     '#228B22',   # green (forest green)
        'marker':    'v',         # down-triangle  (Boltzmann family)
        'markersize': 3.5,
        'markevery':  4,
        'format':    'tecplot',
        'tecplot_variant': 'legacy',
        'w_prescale': 4.0,
    },
}

# ================================================================
# Re Argument
# ================================================================
parser = argparse.ArgumentParser(description="ERCOFTAC benchmark comparison")
parser.add_argument('--Re', type=int, default=None, help='Reynolds number')
parser.add_argument(
    '--no-laminar-w-divide-uref',
    action='store_true',
    help='層流：略過法向場 ÷Uref（VTK 已由 fileIO 無因次化時使用）',
)
parser.add_argument(
    '--scales',
    type=str, default=None,
    help='自訂 offset-plot 可視化倍率 (例: "U:0.8,W:3.2,uu:30")',
)
parser.add_argument(
    '--no-ask-scales',
    action='store_true',
    help='使用預設倍率，不進入互動詢問',
)
parser.add_argument(
    '--no-ask-density',
    action='store_true',
    help='Turbulent: 使用預設 benchmark 數據密度，不互動詢問',
)
args, _ = parser.parse_known_args()

U_REF, _VARIABLES_HEADER_PATH = resolve_uref_for_postprocess(SCRIPT_DIR)

if args.Re is not None:
    Re = args.Re
else:
    try:
        Re = int(input("Reynolds number (default 700): ") or "700")
    except (ValueError, EOFError):
        Re = 700
LAMINAR = (Re <= 150)
if Re <= 150:
    XH_STATIONS = [0, 1, 2, 3, 4, 5, 6, 7, 8]  # No 0.05, 0.5 — 對應 LBM 分布
print(f"[INFO] Re = {Re}  {'(laminar mode)' if LAMINAR else '(turbulent mode)'}")

# ── Benchmark 數據點密度控制 ──────────────────────────────────
# 層流: 100% (全部顯示)
# 紊流: 使用者選擇每個 benchmark 的顯示密度 (%)
_DEFAULT_DENSITY = {
    'LESOCC':        10,
    'MGLET':         10,
    'MGLET_Manhart': 10,
    'Experiment':    10,
    'LBM':           10,
    'ISLBM':         10,
}

def subsample_uniform(y_arr, data_arr, density_pct):
    """均勻子取樣: 從 N 個點中按 density_pct% 均勻選出 n_keep 個點。
    回傳 (y_sub, data_sub)。density_pct >= 100 時回傳原始資料。"""
    if density_pct >= 100 or len(y_arr) <= 2:
        return y_arr, data_arr
    n = len(y_arr)
    n_keep = max(2, int(round(n * density_pct / 100.0)))
    indices = np.round(np.linspace(0, n - 1, n_keep)).astype(int)
    return y_arr[indices], data_arr[indices]

def _init_benchmark_density(bench_sources_list, is_laminar):
    """設定每個 benchmark 來源的數據點顯示密度 (%)。
    層流: 全部 100% (不詢問)。
    紊流: 依使用者互動或 --no-ask-density 旗標決定。
    回傳 dict {source_id: density_pct}。"""
    density = {}

    if is_laminar:
        for src_id, info, bdata in bench_sources_list:
            density[src_id] = 100
        return density

    if not bench_sources_list:
        return density

    # 統計每個 source 的平均數據點數
    src_stats = {}
    for src_id, info, bdata in bench_sources_list:
        npts = [len(bdata[xh]['y']) for xh in bdata if 'y' in bdata[xh]]
        src_stats[src_id] = int(np.mean(npts)) if npts else 0

    # --no-ask-density: 使用預設值
    if args.no_ask_density:
        for src_id, info, bdata in bench_sources_list:
            d = _DEFAULT_DENSITY.get(src_id, 80)
            density[src_id] = d
        print("\n" + "="*60)
        print("  Benchmark scatter density (--no-ask-density)")
        print("="*60)
        for src_id, info, bdata in bench_sources_list:
            avg = src_stats.get(src_id, 0)
            d = density[src_id]
            n_show = max(2, int(round(avg * d / 100))) if avg > 0 else 0
            print(f"  {info['label']:30s}  {d:3d}%  ({avg} -> ~{n_show} pts/station)")
        print("="*60 + "\n")
        return density

    # 互動式詢問
    print("\n" + "="*60)
    print("  Benchmark scatter density (turbulent mode)")
    print("  設定每組 benchmark 顯示的數據點比例 (%)")
    print("  100% = 全部顯示, 50% = 均勻取半, 0% = 不顯示")
    print("="*60)
    for src_id, info, bdata in bench_sources_list:
        avg = src_stats.get(src_id, 0)
        default = _DEFAULT_DENSITY.get(src_id, 80)
        prompt = f"  {info['label']:30s}  ({avg} pts/station, default {default}%): "
        try:
            raw = input(prompt).strip()
            if raw == '':
                d = default
            else:
                d = int(raw)
                d = max(0, min(100, d))
        except (ValueError, EOFError):
            d = default
        density[src_id] = d
        n_show = max(2, int(round(avg * d / 100))) if avg > 0 and d > 0 else 0
        print(f"    -> {d}%  ({avg} -> ~{n_show} pts/station)")
    print("="*60 + "\n")
    return density

# (BENCH_DENSITY 在 benchmark 載入後初始化 — 見 Section 3 之後)

# ── 初始化使用者自訂 offset-plot 倍率 ─────────────────────────
def _init_field_scales():
    """設定每欄位可視化倍率。優先順序: --scales > interactive > defaults."""
    defaults = _DEFAULT_SCALES_LAMINAR.copy() if LAMINAR else _DEFAULT_SCALES_TURBULENT.copy()

    # 1. 命令列 --scales 覆蓋
    if args.scales:
        for pair in args.scales.split(","):
            pair = pair.strip()
            if ":" in pair:
                k, v = pair.split(":", 1)
                defaults[k.strip()] = float(v.strip())
        print(f"[INFO] 使用命令列倍率: {defaults}")
        return defaults

    # 2. --no-ask-scales → 使用預設
    if args.no_ask_scales:
        print(f"[INFO] 使用預設倍率: {defaults}")
        return defaults

    # 3. 互動式詢問
    if LAMINAR:
        fields_desc = [
            ("U",  "U (streamwise velocity)", defaults.get("U", 0.8)),
            ("V",  "V (wall-normal velocity)", defaults.get("V", 3.2)),
        ]
    else:
        fields_desc = [
            ("U",  "U (streamwise velocity)", defaults.get("U", 0.8)),
            ("V",  "V (wall-normal velocity)", defaults.get("V", 0.8)),
            ("uu", "uu (Reynolds stress)", defaults.get("uu", 30)),
            ("vv", "vv (Reynolds stress)", defaults.get("vv", 30)),
            ("uv", "uv (shear stress)", defaults.get("uv", 60)),
            ("k",  "k (TKE)", defaults.get("k", 20)),
        ]

    print(f"\n{'='*60}")
    print(f"Offset-plot 可視化倍率設定 (Re={Re})")
    print(f"{'='*60}")
    print(f"本次輸出共 {len(fields_desc)} 項數據。")
    print(f"★ 所有來源的原始數據 dict 儲存真實物理值 (無倍率)。")
    print(f"★ 以下倍率僅在繪圖時施加，不影響數據本身。")
    print(f"直接按 Enter 使用預設值。\n")

    result = {}
    for key, desc, default_val in fields_desc:
        try:
            user_input = input(f"  {desc} [default={default_val}]: ").strip()
            if user_input:
                result[key] = float(user_input)
            else:
                result[key] = default_val
        except (ValueError, EOFError):
            result[key] = default_val

    print(f"\n[INFO] 最終倍率: {result}")
    print(f"[INFO] ★ 所有 dict 儲存純物理值。倍率僅影響 offset-plot 可視化，標示於圖標題及註腳。")
    return result

FIELD_PLOT_SCALE = _init_field_scales()

# ================================================================
# Hill Function
# ================================================================
def hill_function(Y):
    """Standard periodic hill geometry, h=1, period=9h."""
    Y = np.asarray(Y, dtype=float).copy()
    Y = np.where(Y < 0, Y + LY, Y)
    Y = np.where(Y > LY, Y - LY, Y)
    model = np.zeros_like(Y)
    t = Y * 28.0
    seg1 = Y <= (54.0/28.0)*(9.0/54.0)
    model = np.where(seg1, (1.0/28.0)*np.minimum(28.0, 28.0 + 0.006775070969851*t*t - 0.0021245277758000*t*t*t), model)
    seg2 = (Y > (54.0/28.0)*(9.0/54.0)) & (Y <= (54.0/28.0)*(14.0/54.0))
    model = np.where(seg2, 1.0/28.0*(25.07355893131 + 0.9754803562315*t - 0.1016116352781*t*t + 0.001889794677828*t*t*t), model)
    seg3 = (Y > (54.0/28.0)*(14.0/54.0)) & (Y <= (54.0/28.0)*(20.0/54.0))
    model = np.where(seg3, 1.0/28.0*(25.79601052357 + 0.8206693007457*t - 0.09055370274339*t*t + 0.001626510569859*t*t*t), model)
    seg4 = (Y > (54.0/28.0)*(20.0/54.0)) & (Y <= (54.0/28.0)*(30.0/54.0))
    model = np.where(seg4, 1.0/28.0*(40.46435022819 - 1.379581654948*t + 0.019458845041284*t*t - 0.0002070318932190*t*t*t), model)
    seg5 = (Y > (54.0/28.0)*(30.0/54.0)) & (Y <= (54.0/28.0)*(40.0/54.0))
    model = np.where(seg5, 1.0/28.0*(17.92461334664 + 0.8743920332081*t - 0.05567361123058*t*t + 0.0006277731764683*t*t*t), model)
    seg6 = (Y > (54.0/28.0)*(40.0/54.0)) & (Y <= (54.0/28.0)*(54.0/54.0))
    model = np.where(seg6, 1.0/28.0*np.maximum(0.0, 56.39011190988 - 2.010520359035*t + 0.01644919857549*t*t + 0.00002674976141766*t*t*t), model)
    Yr = LY - Y; tr = Yr * 28.0; rseg = (Y >= LY - (54.0/28.0))
    model = np.where(rseg & (Yr <= (54.0/28.0)*(9.0/54.0)), (1.0/28.0)*np.minimum(28.0, 28.0 + 0.006775070969851*tr*tr - 0.0021245277758000*tr*tr*tr), model)
    model = np.where(rseg & (Yr > (54.0/28.0)*(9.0/54.0)) & (Yr <= (54.0/28.0)*(14.0/54.0)), 1.0/28.0*(25.07355893131 + 0.9754803562315*tr - 0.1016116352781*tr*tr + 0.001889794677828*tr*tr*tr), model)
    model = np.where(rseg & (Yr > (54.0/28.0)*(14.0/54.0)) & (Yr <= (54.0/28.0)*(20.0/54.0)), 1.0/28.0*(25.79601052357 + 0.8206693007457*tr - 0.09055370274339*tr*tr + 0.001626510569859*tr*tr*tr), model)
    model = np.where(rseg & (Yr > (54.0/28.0)*(20.0/54.0)) & (Yr <= (54.0/28.0)*(30.0/54.0)), 1.0/28.0*(40.46435022819 - 1.379581654948*tr + 0.019458845041284*tr*tr - 0.0002070318932190*tr*tr*tr), model)
    model = np.where(rseg & (Yr > (54.0/28.0)*(30.0/54.0)) & (Yr <= (54.0/28.0)*(40.0/54.0)), 1.0/28.0*(17.92461334664 + 0.8743920332081*tr - 0.05567361123058*tr*tr + 0.0006277731764683*tr*tr*tr), model)
    model = np.where(rseg & (Yr > (54.0/28.0)*(40.0/54.0)) & (Yr <= (54.0/28.0)*(54.0/54.0)), 1.0/28.0*np.maximum(0.0, 56.39011190988 - 2.010520359035*tr + 0.01644919857549*tr*tr + 0.00002674976141766*tr*tr*tr), model)
    return model

# ================================================================
# VTK Parsing
# ================================================================
def parse_vtk(filepath):
    """Read points, velocity, and scalar fields from legacy STRUCTURED_GRID VTK.

    Supports both BINARY (big-endian) and ASCII format. Header is parsed via
    readline(); BINARY data blocks are read as raw bytes and decoded with
    numpy (>f8 for double, >f4 for float).
    """
    dims = None
    npts = 0
    npts_from_dims = 0
    points = np.empty((0, 3))
    scalars = {}
    is_binary = False

    def _np_dtype(token):
        t = token.lower()
        if t == "double":
            return ">f8", 8
        if t == "float":
            return ">f4", 4
        if t == "int":
            return ">i4", 4
        return ">f8", 8

    def _read_ascii_block(f, n, stop_tokens):
        arr = np.empty(n, dtype=np.float64)
        idx = 0
        while idx < n:
            dline = f.readline()
            if not dline:
                break
            sline = dline.decode("latin-1", errors="ignore").strip()
            if not sline:
                continue
            first = sline.split()[0]
            if first.startswith(stop_tokens):
                # rewind not supported; caller handles by re-checking next line
                return arr[:idx], dline
            for v in sline.split():
                if idx < n:
                    arr[idx] = float(v)
                    idx += 1
        return arr[:idx], None

    with open(filepath, "rb") as f:
        pushback = None
        while True:
            if pushback is not None:
                raw = pushback
                pushback = None
            else:
                raw = f.readline()
            if not raw:
                break
            sline = raw.decode("latin-1", errors="ignore").strip()
            if not sline:
                continue

            if sline == "BINARY":
                is_binary = True
                continue
            if sline == "ASCII":
                is_binary = False
                continue

            if sline.startswith("DIMENSIONS"):
                dims = tuple(int(v) for v in sline.split()[1:4])
                npts_from_dims = dims[0] * dims[1] * dims[2]

            elif sline.startswith("POINT_DATA"):
                npts = int(sline.split()[1])

            elif sline.startswith("POINTS"):
                parts = sline.split()
                n = int(parts[1])
                dt, esize = _np_dtype(parts[2] if len(parts) > 2 else "double")
                if npts == 0:
                    npts = n
                if is_binary:
                    buf = f.read(n * 3 * esize)
                    pts = np.frombuffer(buf, dtype=dt).astype(np.float64).copy()
                    points = pts.reshape(-1, 3)
                    f.readline()  # consume trailing newline after binary block
                else:
                    pts, leftover = _read_ascii_block(
                        f, n * 3, ("SCALARS", "VECTORS", "POINT_DATA"))
                    points = pts.reshape(-1, 3)
                    pushback = leftover

            elif sline.startswith("VECTORS"):
                if npts == 0 and npts_from_dims > 0:
                    npts = npts_from_dims
                parts = sline.split()
                vec_name = parts[1] if len(parts) > 1 else "velocity"
                dt, esize = _np_dtype(parts[2] if len(parts) > 2 else "double")
                if is_binary:
                    buf = f.read(npts * 3 * esize)
                    vec = np.frombuffer(buf, dtype=dt).astype(np.float64).copy()
                    f.readline()
                else:
                    vec, leftover = _read_ascii_block(
                        f, npts * 3, ("SCALARS", "VECTORS", "POINT_DATA"))
                    pushback = leftover
                if vec.size == npts * 3:
                    scalars[f"{vec_name}_x"] = vec[0::3].copy()
                    scalars[f"{vec_name}_y"] = vec[1::3].copy()
                    scalars[f"{vec_name}_z"] = vec[2::3].copy()

            elif sline.startswith("SCALARS"):
                if npts == 0 and npts_from_dims > 0:
                    npts = npts_from_dims
                parts = sline.split()
                name = parts[1]
                dt, esize = _np_dtype(parts[2] if len(parts) > 2 else "double")
                f.readline()           # skip LOOKUP_TABLE line
                if is_binary:
                    buf = f.read(npts * esize)
                    arr = np.frombuffer(buf, dtype=dt).astype(np.float64).copy()
                    f.readline()
                else:
                    arr, leftover = _read_ascii_block(
                        f, npts, ("SCALARS", "VECTORS"))
                    pushback = leftover
                scalars[name] = arr

    return dims, points, scalars


def check_vtk_completeness(filepath):
    """Pre-parse scan: verify VTK file has all required sections."""
    markers = {"DIMENSIONS": False, "POINTS": False, "POINT_DATA": False,
               "U_mean": False, "V_mean": False}
    total_lines = 0
    grid_npts = 0

    with open(filepath, "rb") as f:
        for raw in f:
            total_lines += 1
            stripped = raw.decode("latin-1", errors="ignore").strip()
            if stripped.startswith("DIMENSIONS"):
                markers["DIMENSIONS"] = True
                parts = stripped.split()
                if len(parts) >= 4:
                    grid_npts = int(parts[1]) * int(parts[2]) * int(parts[3])
            elif stripped.startswith("POINTS"):
                markers["POINTS"] = True
            elif stripped.startswith("POINT_DATA"):
                markers["POINT_DATA"] = True
            elif stripped.startswith("SCALARS U_mean"):
                markers["U_mean"] = True
            elif stripped.startswith("SCALARS V_mean"):
                markers["V_mean"] = True

    expected_min = 5 + grid_npts + 3 * (grid_npts + 2)
    missing = [k for k, v in markers.items() if not v]
    diag = [f"  檔案: {os.path.basename(filepath)}", f"  總行數: {total_lines:,}"]
    if grid_npts > 0:
        diag.append(f"  網格點數: {grid_npts:,}")
        diag.append(f"  完成度: ~{total_lines / expected_min * 100:.1f}%")
    ok = len(missing) == 0
    if not ok:
        diag.append(f"  缺少區段: {', '.join(missing)}")
        if grid_npts > 0 and total_lines < expected_min:
            diag.append(f"  -> 檔案傳輸未完成 (行數不足)")
        elif not markers["U_mean"]:
            diag.append(f"  -> 此 VTK 不含時間平均資料 (U_mean)")
    return ok, "\n".join(diag)


# ================================================================
# Benchmark Scanning & Loading
# ================================================================
def find_re_directory(source_dir, target_re):
    """Find Re subdirectory matching target_re (exact or within 5%)."""
    if not os.path.isdir(source_dir):
        return None, None
    re_dirs = {}
    for d in os.listdir(source_dir):
        if d.startswith("Re") and os.path.isdir(os.path.join(source_dir, d)):
            try:
                # Support both 'Re100' and 'Re=100' naming
                re_str = d[2:].lstrip('=')
                re_dirs[int(re_str)] = d
            except ValueError:
                pass
    if target_re in re_dirs:
        return os.path.join(source_dir, re_dirs[target_re]), target_re
    for re_val, dirname in sorted(re_dirs.items(), key=lambda x: abs(x[0] - target_re)):
        if abs(re_val - target_re) / max(target_re, 1) < 0.05:
            return os.path.join(source_dir, dirname), re_val
    return None, None


def find_station_files(re_dir):
    """Find station files by new naming convention.

    Naming: {Source}_Re{Re}_{xh}.dat  (e.g. LESOCC_Re700_0.05.dat)
    Also:   {Source}_Re{Re}_{xh}.DAT  (e.g. LBM_Re100_0.DAT)

    Returns {xh_float: filepath}.
    """
    files = sorted(glob.glob(os.path.join(re_dir, "*.dat")) +
                   glob.glob(os.path.join(re_dir, "*.DAT")))
    xh_map = {}
    for f in files:
        base = os.path.splitext(os.path.basename(f))[0]
        # Support 'velocity_y=N' naming (ISLBM)
        if base.startswith('velocity_y='):
            xh_str = base[len('velocity_y='):]
        else:
            parts = base.rsplit('_', 1)
            if len(parts) == 2:
                xh_str = parts[1]
            else:
                continue
        if xh_str == 'wall':
            continue  # skip wall data files (MGLET)
        try:
            xh = float(xh_str)
            xh_map[xh] = f
        except ValueError:
            pass
    return xh_map


def load_station_file(filepath, delimiter=None, fmt=None, xh_station=0.0,
                      tecplot_variant='new', w_prescale=1.0):
    """Load one station .dat file and return pure physical values.

    fmt='tecplot': Tecplot format (LBM/ISLBM/GILBM combinepltv2 output)
        tecplot_variant='legacy':
            舊版 combinepltv2 格式: col2 = station + U/Uref, col3 = station + w_prescale × W/Uref
            → 減去 station offset，除以 w_prescale，回傳物理值。
        tecplot_variant='new':
            新版 combinepltv2 格式: col2 = U/Uref, col3 = W/Uref (純物理值)
            → 直接使用。

    fmt=None: ERCOFTAC format, columns: y/h, U, V, uu, vv, uv [, k]

    所有格式一律回傳純物理值 dict，不含任何 offset 或倍率。

    Returns dict {y, U, V, uu, vv, uv, k} or None.
    """
    try:
        if fmt == 'tecplot':
            data = np.loadtxt(filepath, skiprows=3)
            if data.ndim < 2 or data.shape[1] < 4:
                return None

            if tecplot_variant == 'legacy':
                # 舊格式: col2 = station + U/Uref, col3 = station + w_prescale × W/Uref
                U_phys = data[:, 2] - xh_station
                W_raw  = data[:, 3] - xh_station
                W_phys = W_raw / w_prescale
                if w_prescale != 1.0:
                    print(f"    [LEGACY] {os.path.basename(filepath)}: "
                          f"offset={xh_station}, W÷{w_prescale:.0f} → physical values")
                return {
                    "y": data[:, 0], "U": U_phys, "V": W_phys,
                    "uu": None, "vv": None, "uv": None, "k": None,
                }
            else:
                # 新格式: 純物理值，不含 offset，不含倍率
                return {
                    "y": data[:, 0],        # z (abs wall-normal)
                    "U": data[:, 2],        # v/Uref (streamwise, code v → VTK U)
                    "V": data[:, 3],        # w/Uref (wall-normal, code w → VTK V)
                    "uu": None, "vv": None, "uv": None, "k": None,
                }
        else:
            data = np.loadtxt(filepath, comments="#", delimiter=delimiter)
    except Exception:
        return None
    if data.ndim < 2 or data.shape[1] < 6:
        return None  # skip non-profile files (e.g. MGLET file-11 with 3 cols)
    result = {
        "y":  data[:, 0],
        "U":  data[:, 1],
        "V":  data[:, 2],
        "uu": data[:, 3],
        "vv": data[:, 4],
        "uv": data[:, 5],
    }
    result["k"] = data[:, 6] if data.shape[1] >= 7 else None
    return result


def scan_and_load_benchmarks(bench_dir, target_re):
    """Scan benchmark dir for all available sources at target Re.
    Returns list of (source_id, info, data_dict) where data_dict = {xh: {y,U,V,uu,...}}
    """
    results = []
    print(f"\n{'='*60}")
    print(f"掃描 Re={target_re} 的 benchmark 數據源...")
    print(f"{'='*60}")

    for src_id, info in BENCHMARK_SOURCES.items():
        src_dir = os.path.join(bench_dir, info['dir_name'])
        re_dir, matched_re = find_re_directory(src_dir, target_re)

        if re_dir is None:
            # List what Re values ARE available
            if os.path.isdir(src_dir):
                avail = sorted([d for d in os.listdir(src_dir)
                               if d.startswith("Re") and os.path.isdir(os.path.join(src_dir, d))])
                print(f"  \u274c {info['label']} \u2014 Re{target_re} 無數據 (有: {', '.join(avail)})")
            else:
                print(f"  \u274c {info['label']} \u2014 目錄不存在")
            continue

        xh_files = find_station_files(re_dir)
        if not xh_files:
            print(f"  \u26a0\ufe0f  {info['label']} \u2014 Re{matched_re} 目錄存在但無 .dat 檔案")
            continue

        data = {}
        src_fmt = info.get('format')  # None or 'tecplot'
        t_variant = info.get('tecplot_variant', 'new')
        t_wprescale = info.get('w_prescale', 1.0)
        for xh, fpath in sorted(xh_files.items()):
            stn_data = load_station_file(fpath, info.get('delimiter'),
                                         fmt=src_fmt, xh_station=xh,
                                         tecplot_variant=t_variant,
                                         w_prescale=t_wprescale)
            if stn_data is not None:
                data[xh] = stn_data

        if data:
            re_suffix = f" (matched Re{matched_re})" if matched_re != target_re else ""
            print(f"  \u2705 {info['label']} \u2014 Re{matched_re}{re_suffix}: "
                  f"{len(data)} 站位")
            results.append((src_id, info, data))
        else:
            print(f"  \u26a0\ufe0f  {info['label']} \u2014 Re{matched_re} 檔案載入失敗")

    print(f"\n可用數據源: {len(results)} 個")
    return results


# ================================================================
# 1. Load VTK (optional — benchmark-only mode if U_mean absent)
# ================================================================
HAS_VTK = False
vtk_files_all = sorted(glob.glob(os.path.join(VTK_DIR, VTK_PATTERN)),
                    key=lambda f: int(''.join(c for c in os.path.basename(f) if c.isdigit()) or '0'))

# ─────────────────────────────────────────────────────────────────────
# 自動從最新往舊找，第一個「完整」VTK 即停（跳過 0-byte / 截斷 / 缺區段）
# 判定：檔案 >= 1 KB AND check_vtk_completeness() 通過
# ─────────────────────────────────────────────────────────────────────
MIN_VTK_BYTES = 1024   # < 1 KB 視為截斷
latest_complete = None
skipped_vtks = []
for cand in reversed(vtk_files_all):
    try:
        sz = os.path.getsize(cand)
    except OSError:
        skipped_vtks.append((os.path.basename(cand), "getsize failed"))
        continue
    if sz < MIN_VTK_BYTES:
        skipped_vtks.append((os.path.basename(cand), f"size={sz}B, 太小"))
        continue
    try:
        ok, diag = check_vtk_completeness(cand)
    except Exception as e:
        skipped_vtks.append((os.path.basename(cand), f"讀檔錯誤: {e}"))
        continue
    if ok:
        latest_complete = cand
        break
    # 抽 diag 最後一行「-> ...」當原因
    reason_lines = [ln.strip() for ln in diag.split('\n')
                    if ln.strip().startswith('->') or '缺少' in ln]
    reason = reason_lines[0].lstrip('-> ').strip() if reason_lines else "不完整"
    skipped_vtks.append((os.path.basename(cand), reason))

if skipped_vtks:
    print(f"[INFO] 跳過 {len(skipped_vtks)} 個不完整/截斷 VTK (從最新往舊找)：")
    for name, reason in skipped_vtks[:5]:
        print(f"       skip: {name}  ({reason})")
    if len(skipped_vtks) > 5:
        print(f"       ... 另 {len(skipped_vtks)-5} 個")

vtk_files = [latest_complete] if latest_complete else []

if not vtk_files:
    if not vtk_files_all:
        print(f"[WARN] No VTK files matching '{VTK_PATTERN}' found — benchmark-only mode.")
    else:
        print(f"[WARN] 全部 {len(vtk_files_all)} 個 VTK 都不完整 — benchmark-only mode.")
else:
    vtk_path = vtk_files[-1]
    print(f"[INFO] Loading VTK: {os.path.basename(vtk_path)}  (latest COMPLETE)")
    dims, points, scalars = parse_vtk(vtk_path)
    nx, ny, nz = dims
    print(f"[INFO] Grid: {nx} x {ny} x {nz} = {nx*ny*nz} points")
    print(f"[INFO] Available scalars: {list(scalars.keys())}")

    # Determine which velocity fields to use
    vel_u_key, vel_v_key = None, None
    if LAMINAR:
        for u_candidate, v_candidate in [
            ("u_inst", "v_inst"),
            ("velocity_y", "velocity_z"),   # VECTORS velocity → y=streamwise, z=wall-normal
            ("U_mean", "V_mean"),
        ]:
            if u_candidate in scalars:
                vel_u_key, vel_v_key = u_candidate, v_candidate
                break
    else:
        vel_u_key, vel_v_key = "U_mean", "V_mean"

    if vel_u_key is not None and vel_u_key in scalars:
        HAS_VTK = True
        # Reshape to 3D: VTK order is (i fastest, j, k slowest)
        pts_3d    = points.reshape(nz, ny, nx, 3)
        U_mean_3d = scalars[vel_u_key].reshape(nz, ny, nx)
        V_mean_3d = scalars.get(vel_v_key)

        if LAMINAR:
            # Laminar: no RS/TKE fields
            uu_RS_3d = uv_RS_3d = vv_RS_3d = k_TKE_3d = None
        else:
            # Support both naming conventions:
            #   ERCOFTAC 公約:  "uu_RS", "uv_RS", "vv_RS"
            #   舊 fileIO.h:    "uu_RS", "uw_RS", "ww_RS" (或 "uu", "uw", "ww")
            uu_RS_3d  = scalars.get("uu_RS", scalars.get("uu"))
            uv_RS_3d  = scalars.get("uv_RS", scalars.get("uw_RS", scalars.get("uw")))
            vv_RS_3d  = scalars.get("vv_RS", scalars.get("ww_RS", scalars.get("ww")))
            k_TKE_3d  = scalars.get("k_TKE")

        if V_mean_3d is not None: V_mean_3d = V_mean_3d.reshape(nz, ny, nx)
        # ★ V_mean_3d 維持原始物理值 (w/Uref)，不施加任何倍率。
        #   可視化倍率統一在繪圖時透過 FIELD_PLOT_SCALE["V"] 施加。
        if uu_RS_3d  is not None: uu_RS_3d  = uu_RS_3d.reshape(nz, ny, nx)
        if uv_RS_3d  is not None: uv_RS_3d  = uv_RS_3d.reshape(nz, ny, nx)
        if vv_RS_3d  is not None: vv_RS_3d  = vv_RS_3d.reshape(nz, ny, nx)
        if k_TKE_3d  is not None: k_TKE_3d  = k_TKE_3d.reshape(nz, ny, nx)

        HAS_RS = (uu_RS_3d is not None)
        print(f"[INFO] Using VTK fields: {vel_u_key}, {vel_v_key}")
        if not LAMINAR:
            # Diagnostic: show which RS field names were matched
            _rs_matched = {}
            for _vname, _candidates in [("uu", ["uu_RS", "uu"]),
                                         ("uv", ["uv_RS", "uw_RS", "uw"]),
                                         ("vv", ["vv_RS", "ww_RS", "ww"]),
                                         ("k",  ["k_TKE"])]:
                for _c in _candidates:
                    if _c in scalars:
                        _rs_matched[_vname] = _c
                        break
            if HAS_RS:
                print(f"[INFO] RS fields found: {_rs_matched}")
            else:
                print(f"[WARN] No RS fields — looked for uu_RS/uu, uv_RS/uw_RS/uw, vv_RS/ww_RS/ww, k_TKE")
                print(f"[WARN] Available scalars: {list(scalars.keys())}")

        y_3d = pts_3d[:, :, :, 1]  # streamwise
        z_3d = pts_3d[:, :, :, 2]  # wall-normal
        y_stations = y_3d[0, :, 0]
    else:
        avail = list(scalars.keys())
        print(f"[WARN] No usable velocity field found in VTK — benchmark-only mode.")
        if not avail:
            print(f"[WARN] VTK file contains 0 scalar/vector fields — file may be incomplete.")

if not HAS_VTK:
    HAS_RS = False

# ================================================================
# 2. Extract spanwise-averaged profiles (only if VTK available)
#    Supports both uniform (rectilinear) and curvilinear grids.
#    Curvilinear: 6th-order Lagrange interpolation per k-level.
# ================================================================

# ── Curvilinear grid detection ──────────────────────────────────
def detect_curvilinear_grid(y_3d_arr, tol_frac=1e-4):
    """Detect whether xi-lines are straight (uniform) or curved.

    For a uniform/rectilinear grid, y_3d[k, j, 0] is constant across k
    for each j.  For a curvilinear grid, it varies.

    Returns
    -------
    is_curvilinear : bool
    max_deviation  : float  (max |y(k,j) - y(0,j)| across all k,j)
    """
    y_base = y_3d_arr[0, :, 0]                         # (ny,) baseline at k=0
    y_all  = y_3d_arr[:, :, 0]                          # (nz, ny)
    deviation = np.abs(y_all - y_base[np.newaxis, :])
    max_dev = float(np.max(deviation))
    threshold = tol_frac * LY                           # 1e-4 * 9.0 ~ 9e-4
    return (max_dev > threshold), max_dev


# ── 6th-order Lagrange interpolation engine ─────────────────────
# ★ 與程式碼內部 WENO5 (5+1=6 point stencil) 一致的 6 階精度
LAGRANGE_ORDER = 6

def _lagrange_weights(x_stencil, x_target):
    """Compute Lagrange basis weights for interpolation at x_target
    given stencil nodes x_stencil (array of length n).

    Returns weight array of same length.
    """
    n = len(x_stencil)
    w = np.ones(n, dtype=float)
    for i in range(n):
        for j in range(n):
            if i != j:
                denom = x_stencil[i] - x_stencil[j]
                if abs(denom) < 1e-30:
                    denom = 1e-30          # degenerate guard
                w[i] *= (x_target - x_stencil[j]) / denom
    return w


def _pick_stencil_indices(j_left, nj, order=LAGRANGE_ORDER):
    """Pick stencil indices for Lagrange interpolation.

    j_left : index such that y[j_left] <= y_target < y[j_left+1]
    nj     : total number of j-nodes
    order  : number of stencil points (default 6 for 6th-order)

    Central stencil (order=6): [j_left-2, ..., j_left+3]
    One-sided near boundaries: clamped to [0, nj-order].
    """
    j_start = j_left - (order // 2 - 1)               # j_left - 2 for order=6
    j_start = max(0, min(j_start, nj - order))        # clamp to [0, nj-order]
    return np.arange(j_start, j_start + order)


def interp_at_target_y(y_line, field_line, y_target, order=LAGRANGE_ORDER):
    """Interpolate field_line(y_line) at y_target using Lagrange polynomial.

    Parameters
    ----------
    y_line     : 1-D array of streamwise coordinates at this k-level (length nj)
    field_line : 1-D array of field values at this k-level (length nj)
    y_target   : scalar target streamwise position
    order      : interpolation order = number of stencil points (default 6)

    Returns
    -------
    Interpolated field value (scalar float).
    """
    nj = len(y_line)
    if nj < order:
        order = nj                                     # fallback to available pts

    # Locate j_left: y_line[j_left] <= y_target < y_line[j_left+1]
    # y_line is assumed monotonically increasing (streamwise direction)
    j_left = int(np.searchsorted(y_line, y_target, side='right')) - 1
    j_left = max(0, min(j_left, nj - 2))

    idx = _pick_stencil_indices(j_left, nj, order)
    weights = _lagrange_weights(y_line[idx], y_target)
    return float(np.dot(weights, field_line[idx]))


def interp_1d_lagrange(z_sim, f_sim, z_target, order=LAGRANGE_ORDER):
    """Interpolate simulation profile f_sim(z_sim) at arbitrary z_target points.

    Used for aligning simulation wall-normal profiles onto benchmark z-points.

    Parameters
    ----------
    z_sim    : 1-D array (nz,), monotonically increasing wall-normal coords
    f_sim    : 1-D array (nz,), field values at simulation z-points
    z_target : 1-D array (m,), target z-positions (benchmark z-points)
    order    : interpolation order (default 6)

    Returns
    -------
    f_interp : 1-D array (m,), interpolated field values
    mask_valid: 1-D bool array (m,), True where z_target is within z_sim range
    """
    m = len(z_target)
    f_interp = np.full(m, np.nan)
    z_lo, z_hi = z_sim[0], z_sim[-1]
    mask_valid = (z_target >= z_lo) & (z_target <= z_hi)

    nz = len(z_sim)
    for p in range(m):
        if not mask_valid[p]:
            continue
        zt = z_target[p]
        j_left = int(np.searchsorted(z_sim, zt, side='right')) - 1
        j_left = max(0, min(j_left, nz - 2))
        idx = _pick_stencil_indices(j_left, nz, min(order, nz))
        weights = _lagrange_weights(z_sim[idx], zt)
        f_interp[p] = float(np.dot(weights, f_sim[idx]))
    return f_interp, mask_valid


# ── Vectorised helper: interpolate entire k-column at once ──────
def _interp_profile_curvilinear(y_3d_arr, z_3d_arr, field_3d, y_target,
                                 order=4):
    """Interpolate a scalar field at y_target for every k-level.

    For each k and each spanwise index i, uses 6th-order Lagrange
    interpolation along the j-direction (streamwise), then averages
    across i (spanwise).

    Returns (field_profile, z_profile, y_actual_k0)  each shape (nz,).
    """
    nz_pts, ny_pts, nx_pts = field_3d.shape
    f_out = np.zeros(nz_pts)
    z_out = np.zeros(nz_pts)

    for k in range(nz_pts):
        f_sum = 0.0
        z_sum = 0.0
        for i in range(nx_pts):
            y_line = y_3d_arr[k, :, i]
            f_sum += interp_at_target_y(y_line, field_3d[k, :, i], y_target, order)
            z_sum += interp_at_target_y(y_line, z_3d_arr[k, :, i], y_target, order)
        f_out[k] = f_sum / nx_pts
        z_out[k] = z_sum / nx_pts

    # y_actual at k=0 (for diagnostics)
    y_actual = interp_at_target_y(y_3d_arr[0, :, 0],
                                   y_3d_arr[0, :, 0], y_target, order)
    return f_out, z_out, y_actual


# ── Profile extraction: always 6th-order Lagrange ───────────────
def extract_profile(xh):
    """Extract spanwise-averaged U profile at station x/h.

    ★ 無論網格是否曲線，一律使用 6th-order Lagrange 插值
      精確內插到指定 y_target，不接受 nearest-j 近似。
    ★ 對每個 k-level，沿 j 方向做 Lagrange 插值，再跨 i 取 spanwise 平均。
    ★ z 座標也做同樣插值 (body-fitted grid, z 隨 j 變化)。
    """
    y_target = xh * H_HILL

    U_prof, z_prof, y_actual = _interp_profile_curvilinear(
        y_3d, z_3d, U_mean_3d, y_target)
    z_wall = z_prof[0]
    z_norm = (z_prof - z_wall) / H_HILL
    return z_norm, U_prof, y_actual, z_wall


def extract_scalar_profile(xh, field_3d):
    """Extract spanwise-averaged vertical profile of any scalar field.

    ★ 一律使用 6th-order Lagrange 插值，精確內插到指定 y_target。
    """
    if field_3d is None:
        return None

    y_target = xh * H_HILL

    nz_pts, ny_pts, nx_pts = field_3d.shape
    result = np.zeros(nz_pts)
    for k in range(nz_pts):
        val_sum = 0.0
        for i in range(nx_pts):
            y_line = y_3d[k, :, i]
            val_sum += interp_at_target_y(y_line, field_3d[k, :, i],
                                           y_target)
        result[k] = val_sum / nx_pts
    return result


# ── Detect grid type (diagnostic only — always use Lagrange) ────
IS_CURVILINEAR = False
if HAS_VTK:
    IS_CURVILINEAR, _grid_max_dev = detect_curvilinear_grid(y_3d)
    if IS_CURVILINEAR:
        print(f"[INFO] Curvilinear grid detected: "
              f"max xi-line deviation = {_grid_max_dev:.6f}")
    else:
        print(f"[INFO] Rectilinear grid detected: "
              f"max deviation = {_grid_max_dev:.2e}")
    print(f"[INFO] ★ 一律使用 6th-order Lagrange 插值精確內插到目標位置")

profiles = {}
if HAS_VTK:
    mode_str = "6th-Lagrange"
    print(f"\n[DIAG] Profile extraction mode: {mode_str}")
    print(f"[DIAG] FIELD_PLOT_SCALE = {FIELD_PLOT_SCALE}")
    print(f"[DIAG] Data dict stores PHYSICAL values: V = w/Uref = <V>/Ub")
    print(f"\n{'x/h':>6s}  {'method':>12s}  {'y_actual':>8s}  "
          f"{'z_wall':>6s}  {'U_max':>8s}  {'V_range':>16s}")
    print("-" * 72)
    for xh in XH_STATIONS:
        z_n, U_p, y_a, z_w = extract_profile(xh)
        z_abs = z_n + z_w / H_HILL
        V_prof = extract_scalar_profile(xh, V_mean_3d)
        V_info = f"[{V_prof.min():.5f}, {V_prof.max():.5f}]" if V_prof is not None else "N/A"
        profiles[xh] = {
            "z_abs": z_abs, "U": U_p, "z_w": z_w,
            "V":  V_prof,
            "uu": extract_scalar_profile(xh, uu_RS_3d),
            "vv": extract_scalar_profile(xh, vv_RS_3d),
            "uv": extract_scalar_profile(xh, uv_RS_3d),
            "k":  extract_scalar_profile(xh, k_TKE_3d),
        }
        print(f"{xh:6.2f}  {mode_str:>12s}  {y_a:8.4f}  "
              f"{z_w:6.4f}  {U_p.max():8.5f}  {V_info:>16s}")
else:
    print("[INFO] No VTK time-averaged data — skipping profile extraction.")

# ================================================================
# 3. Load benchmark data (multi-source)
# ================================================================
bench_sources = scan_and_load_benchmarks(BENCH_DIR, Re)

# ── Scaling validation ────────────────────────────────────────────
# 驗證所有來源的 V (wall-normal) 量級一致
if HAS_VTK and bench_sources:
    vtk_xh_ref = XH_STATIONS[len(XH_STATIONS)//2]  # 取中間站位驗證
    if vtk_xh_ref in profiles and profiles[vtk_xh_ref]["V"] is not None:
        vtk_V_max = np.max(np.abs(profiles[vtk_xh_ref]["V"]))
        for src_id, info, bdata in bench_sources:
            if vtk_xh_ref in bdata and bdata[vtk_xh_ref].get("V") is not None:
                bench_V_max = np.max(np.abs(bdata[vtk_xh_ref]["V"]))
                ratio = bench_V_max / max(vtk_V_max, 1e-30)
                status = "OK" if 0.1 < ratio < 10 else "WARN: possible ×4 mismatch!"
                print(f"[VALID] V-scale check ({info['label'][:20]:20s}): "
                      f"|V|_max={bench_V_max:.4e}  vs VTK |V|_max={vtk_V_max:.4e}  "
                      f"ratio={ratio:.2f}  {status}")

if not bench_sources and not HAS_VTK:
    sys.exit("[ERROR] No benchmark data found and no VTK data. Nothing to plot.")

# ── 初始化 benchmark 數據點密度 (須在 bench_sources 載入後) ──
BENCH_DENSITY = _init_benchmark_density(bench_sources, LAMINAR)

# Collect ALL unique x/H stations from benchmarks + VTK
ALL_XH_STATIONS = sorted(set(XH_STATIONS))
for _, _, bdata in bench_sources:
    ALL_XH_STATIONS = sorted(set(ALL_XH_STATIONS) | set(bdata.keys()))

# ================================================================
# 3b. Quantitative L2-error: sim vs benchmark (6th-order z-interp)
# ================================================================
# For each benchmark source and each station, interpolate the
# simulation profile onto the benchmark's z-points using 6th-order
# Lagrange, then compute the relative L2 error norm:
#
#   L2_rel = || f_sim(z_bench) - f_bench || / || f_bench ||
#
# Field mapping:  sim_key -> bench_key
_FIELD_MAP = {
    "U":  "U",    # streamwise mean velocity
    "V":  "V",    # wall-normal mean velocity
    "uu": "uu",   # <u'u'>
    "vv": "vv",   # wall-normal RS
    "uv": "uv",   # shear stress
    "k":  "k",    # TKE
}

# L2_errors[src_id][xh][field] = L2_rel  (float or None)
L2_errors = {}

if HAS_VTK and bench_sources:
    print(f"\n{'='*72}")
    print(f"  Quantitative L2-error (6th-order Lagrange z-interpolation)")
    print(f"{'='*72}")
    print(f"  L2_rel = || f_sim(z_bench) - f_bench ||₂ / || f_bench ||₂")
    print()

    for src_id, info, bdata in bench_sources:
        L2_errors[src_id] = {}
        src_label = info['label']

        for xh in sorted(set(ALL_XH_STATIONS) & set(bdata.keys())):
            if xh not in profiles:
                continue
            p = profiles[xh]
            z_sim = p["z_abs"]          # simulation z/h (monotonic)
            bd = bdata[xh]
            z_bench = bd["y"]           # benchmark z/h

            L2_errors[src_id][xh] = {}

            for sim_key, bench_key in _FIELD_MAP.items():
                f_sim_full = p.get(sim_key)
                f_bench    = bd.get(bench_key)
                if f_sim_full is None or f_bench is None:
                    continue
                if len(f_bench) < 2:
                    continue

                # 6th-order Lagrange interpolation: sim → benchmark z-points
                f_sim_at_bench, mask = interp_1d_lagrange(
                    z_sim, f_sim_full, z_bench, order=LAGRANGE_ORDER)

                if np.sum(mask) < 2:
                    continue

                diff = f_sim_at_bench[mask] - f_bench[mask]
                norm_bench = np.linalg.norm(f_bench[mask])
                if norm_bench > 1e-30:
                    L2_rel = np.linalg.norm(diff) / norm_bench
                else:
                    L2_rel = np.linalg.norm(diff)
                L2_errors[src_id][xh][sim_key] = L2_rel

        # ── Terminal output: per-station table ──
        # Determine available fields for this source
        all_fields_avail = set()
        for xh_data in L2_errors[src_id].values():
            all_fields_avail |= set(xh_data.keys())
        fields_ordered = [f for f in ["U", "V", "uu", "vv", "uv", "k"]
                          if f in all_fields_avail]

        if not fields_ordered:
            continue

        # Header
        hdr_fields = "  ".join(f"{f:>8s}" for f in fields_ordered)
        print(f"  ── {src_label} ──")
        print(f"  {'x/h':>6s}  {hdr_fields}")
        print(f"  {'-'*8}  {'  '.join(['-'*8]*len(fields_ordered))}")

        # Per-station rows
        avg_L2 = {f: [] for f in fields_ordered}
        for xh in sorted(L2_errors[src_id].keys()):
            row_vals = []
            for f in fields_ordered:
                val = L2_errors[src_id][xh].get(f)
                if val is not None:
                    row_vals.append(f"{val*100:8.2f}%")
                    avg_L2[f].append(val)
                else:
                    row_vals.append(f"{'---':>8s}")
            print(f"  {xh:6.2f}  {'  '.join(row_vals)}")

        # Average row
        avg_vals = []
        for f in fields_ordered:
            if avg_L2[f]:
                avg_vals.append(f"{np.mean(avg_L2[f])*100:8.2f}%")
            else:
                avg_vals.append(f"{'---':>8s}")
        print(f"  {'<avg>':>6s}  {'  '.join(avg_vals)}")
        print()

# ── Build compact L2 summary string for plot titles ──
# Format: "L2(U): LESOCC 5.2%, MGLET 4.8%"
def _l2_title_summary(field_sim):
    """Return a compact L2 summary string for a given field, across sources."""
    parts = []
    for src_id, info, _ in bench_sources:
        if src_id not in L2_errors:
            continue
        vals = [L2_errors[src_id][xh].get(field_sim)
                for xh in L2_errors[src_id] if field_sim in L2_errors[src_id].get(xh, {})]
        if vals:
            avg = np.mean(vals) * 100
            short_name = src_id  # e.g. 'LESOCC', 'MGLET'
            parts.append(f"{short_name} {avg:.1f}%")
    if parts:
        return f"L\u2082(avg): {', '.join(parts)}"
    return ""


# ================================================================
# 4. Plotting
# ================================================================
if not HAS_MPL:
    if HAS_VTK:
        for xh in XH_STATIONS:
            p = profiles[xh]
            out = os.path.join(SCRIPT_DIR, f"profile_xh{xh:.1f}_Re{Re}.csv")
            np.savetxt(out, np.column_stack([p["z_abs"], p["U"]]),
                       header="z/h  U/Uref", fmt="%.8f")
            print(f"[INFO] Exported {out}")
    sys.exit(0)

# ── Matplotlib style ───────────────────────────────────────────
mpl.rcParams.update({
    "font.family":       "serif",
    "font.serif":        ["Times New Roman", "DejaVu Serif"],
    "mathtext.fontset":  "stix",
    "font.size":         8,          # base (tick labels)
    "axes.labelsize":    9,          # axis labels
    "axes.titlesize":    10,         # (unused — no titles)
    "legend.fontsize":   7,          # legend text
    "xtick.labelsize":   8,          # tick labels
    "ytick.labelsize":   8,
    "xtick.direction":   "in",
    "ytick.direction":   "in",
    "xtick.major.size":  5,
    "ytick.major.size":  5,
    "xtick.minor.size":  3,
    "ytick.minor.size":  3,
    "xtick.minor.visible": True,
    "ytick.minor.visible": True,
    "axes.linewidth":    0.8,
    "lines.linewidth":   1.2,
    "savefig.dpi":       300,
})

# ── Simulation colour ──
# Red solid line for GILBM (present work) — high contrast vs benchmark blues/greens.
c_sim = "#CC2222"  # red


def _scale_tag(scale):
    """Format a scale factor for display: ×0.8, ×3.2, ×1, etc.  (plain Unicode)"""
    if scale == 1.0:
        return u"\u00d71"
    if scale == int(scale):
        return u"\u00d7%d" % int(scale)
    return u"\u00d7%.4g" % scale


# ════════════════════════════════════════════════════════════════════
#  Hill-interior legend box + offset profile plotter
# ════════════════════════════════════════════════════════════════════
def _place_hill_legend(ax, field_label, scale, Re_val, legend_pos='bottom-left',
                       field_bench=None):
    """Place a journal-quality legend box inside the hill region.

    Strategy — "font first, box adapts":
      1. Determine the largest fontsize that fits the axes (based on axes height).
      2. Place all text invisibly, measure bounding boxes.
      3. Build a tight-fit box around the measured text content.
      4. If the box exceeds the hill slope, shrink fontsize and retry.

    Layout:
      Row 1:  <variable>  |  Re = <value>      — italic, CENTRED in box
      Row 2+: [sample] Label                    — normal, left-aligned
              ALL label first-letters vertically aligned.

    legend_pos : 'bottom-left' (default, inside hill) or 'top-right' (upper-right corner)
    """
    from matplotlib.patches import FancyBboxPatch

    fig = ax.get_figure()

    # ── Row content ──
    # Row 1: single unified $...$ so variable, Re, and value share the
    #         SAME math-mode font size — no mixed plain/math shrinkage.
    #   field_label is a LaTeX fragment WITHOUT outer $...$
    #   e.g. r"\langle u \rangle / u_B"
    # (Header row removed — space redistributed to entry rows)

    legend_entries = []   # (type, label, color, marker_char)
    if HAS_VTK:
        # GILBM = our own code, keep plain (non-italic) text
        legend_entries.append(('line', 'GILBM', c_sim, None))
    for _, info, bdata in bench_sources:
        # Only include benchmark sources that have data for the current field
        if field_bench is not None:
            has_field = any(xh_data.get(field_bench) is not None
                           for xh_data in bdata.values())
            if not has_field:
                continue
        # Benchmark labels already contain LaTeX $\mathit{...}$ from BENCHMARK_SOURCES
        legend_entries.append(('marker', info['label'], info['color'],
                               info['marker']))
    n_rows = len(legend_entries)
    if n_rows < 1:
        return  # nothing to draw

    # ── Maximum allowed region ──
    hill_at_right = float(hill_function(np.array([0.5]))[0])  # ≈ 0.857
    TOP_RIGHT = (legend_pos == 'top-right')
    if TOP_RIGHT:
        # Place box flush against top-right corner of axes frame
        ylim_ax = ax.get_ylim()
        MAX_Y1 = ylim_ax[1]          # flush with top edge
        MAX_Y0 = ylim_ax[1] - (hill_at_right - 0.02)  # same height as hill-based box
        MAX_X0 = None                 # computed later (flush right)
    else:
        MAX_Y1 = hill_at_right - 0.02   # tight gap below hill slope
        MAX_X0 = -1.0
        MAX_Y0 = 0.0

    # ── Step 1: determine max fontsize from axes height ──
    fig_h_inch = fig.get_size_inches()[1]
    ax_bbox = ax.get_position()
    axes_h_inch = fig_h_inch * ax_bbox.height
    ylim = ax.get_ylim()
    yr = ylim[1] - ylim[0] if (ylim[1] - ylim[0]) > 1e-10 else 1.0
    # Target: each row occupies ~(MAX_Y1 - MAX_Y0) / n_rows in data coords
    row_h_data = (MAX_Y1 - MAX_Y0) / n_rows
    row_h_inch = (row_h_data / yr) * axes_h_inch
    fontsize = max(8.0, min(row_h_inch * 72 * 0.80, 18.0))

    # ── Layout constants (data-coord offsets relative to box left) ──
    PAD = 0.03            # internal padding around text content
    SAMPLE_W = 0.18       # width reserved for line/marker sample column
    SAMPLE_GAP = 0.05     # gap between sample and label text

    # ── Helper: place all rows, measure, return objects + bounds ──
    def _try_layout(fs):
        """Place text+artists at fontsize fs. Return (texts, artists, bbox_dict)."""
        texts, artists = [], []

        # Vertical spacing: equal row height
        row_h = fs / 72.0              # font height in inches (approx)
        row_h_d = row_h / axes_h_inch * yr * 1.35  # in data coords (1.35× line spacing)

        # Total content height
        content_h = n_rows * row_h_d
        # Box vertical bounds
        by0 = MAX_Y0
        by1 = by0 + content_h + 2 * PAD
        if by1 > MAX_Y1:
            by1 = MAX_Y1
            content_h = by1 - by0 - 2 * PAD
            row_h_d = content_h / n_rows

        # Row y positions (top to bottom)
        def ry(i):
            return by1 - PAD - row_h_d * (i + 0.5)

        # Place entry labels at x=0 for measurement
        label_texts = []
        for j, (etype, label, color, mkr) in enumerate(legend_entries):
            t = ax.text(
                0, ry(j), label,
                fontsize=fs, fontfamily='serif',
                fontstyle='normal', fontweight='normal',
                verticalalignment='center', horizontalalignment='left',
                zorder=11, transform=ax.transData,
            )
            texts.append(t)
            label_texts.append(t)

        # Render to measure
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()

        # Measure widths in data coords
        def text_w(t):
            bb = t.get_window_extent(renderer=renderer)
            bb_d = bb.transformed(ax.transData.inverted())
            return bb_d.x1 - bb_d.x0

        max_label_w = max((text_w(t) for t in label_texts), default=0)

        # Content width = sample_col + gap + longest_label
        content_w = SAMPLE_W + SAMPLE_GAP + max_label_w

        # Box bounds
        box_w = content_w + 2 * PAD
        if TOP_RIGHT:
            xlim_cur = ax.get_xlim()
            bx1 = xlim_cur[1]         # flush with right edge
            bx0 = bx1 - box_w
        else:
            bx0 = MAX_X0
            bx1 = bx0 + box_w

        # Remove temporary texts
        for t in texts:
            t.remove()

        return bx0, bx1, by0, by1, ry, fs, content_w

    # ── Step 2: iterate — shrink font if box exceeds hill ──
    bx0 = bx1 = by0 = by1 = 0
    ry_func = None
    try:
        fig.canvas.draw()
        for _attempt in range(20):
            bx0, bx1, by0, by1, ry_func, fs, cw = _try_layout(fontsize)
            # Check if box top exceeds hill slope anywhere along box width
            # (skip for top-right — no hill constraint there)
            if not TOP_RIGHT:
                check_xs = np.linspace(max(bx0, 0), max(bx1, 0), 10)
                check_xs = check_xs[check_xs >= 0]  # hill only defined for x >= 0
                if len(check_xs) > 0:
                    hill_min = float(np.min(hill_function(check_xs)))
                    if by1 > hill_min - 0.02:
                        by1 = hill_min - 0.02

            if by1 - by0 < 0.15 or fontsize <= 7:
                break
            # Recheck if content fits vertically
            content_h_needed = (fs / 72.0 / axes_h_inch * yr * 1.35) * n_rows + 2 * PAD
            if content_h_needed <= (by1 - by0):
                break
            fontsize -= 0.5
    except Exception:
        # Fallback
        fontsize = 10.0
        bx0, bx1, by0, by1 = -1.0, 0.5, 0.0, 0.8
        ry_func = lambda i: by1 - 0.04 - i * 0.25

    # ── Step 3: draw the final box ──
    box = FancyBboxPatch(
        (bx0, by0), bx1 - bx0, by1 - by0,
        boxstyle='square,pad=0',
        facecolor='white', edgecolor='black', linewidth=0.6,
        alpha=0.95, zorder=10,
        transform=ax.transData,
    )
    ax.add_patch(box)

    # ── Recompute row y positions for final box ──
    row_h_inch = fontsize / 72.0
    row_h_d = row_h_inch / axes_h_inch * yr * 1.35
    content_h = by1 - by0 - 2 * PAD
    row_h_d = content_h / n_rows
    def row_y_final(i):
        return by1 - PAD - row_h_d * (i + 0.5)

    # ── Column x positions ──
    x_sample_left   = bx0 + PAD
    x_sample_right  = x_sample_left + SAMPLE_W
    x_sample_center = (x_sample_left + x_sample_right) / 2.0
    x_label         = x_sample_right + SAMPLE_GAP
    # ── Step 4: place final content (no header row) ──

    # Entry rows: sample + label, ALL labels at x_label (aligned)
    for j, (etype, label, color, mkr) in enumerate(legend_entries):
        ry = row_y_final(j)

        if etype == 'line':
            ax.plot(
                [x_sample_left, x_sample_right], [ry, ry],
                color=color, linewidth=1.5, linestyle='-',
                solid_capstyle='butt',
                zorder=11, clip_on=False,
            )
        elif etype == 'marker':
            ax.plot(
                [x_sample_center], [ry],
                marker=mkr, color=color, markersize=5,
                markerfacecolor='none', markeredgewidth=0.9,
                linestyle='None',
                zorder=11, clip_on=False,
            )

        # Label — ALL start at x_label (first letter aligned vertically)
        ax.text(
            x_label, ry, label,
            fontsize=fontsize, fontfamily='serif',
            fontstyle='normal', fontweight='normal',
            verticalalignment='center', horizontalalignment='left',
            zorder=11, transform=ax.transData,
        )


def plot_offset_panel(ax, field_sim, field_bench, scale,
                      field_label, Re_val,
                      xlabel=r"$x/H$ [-]",
                      xlim_range=None,
                      legend_pos='bottom-left'):
    """Offset-profile plotter — publication-quality (NO title).

    field_label : str  — variable name for the legend box header
    Re_val      : int  — Reynolds number (shown in legend box)

    Simulation = solid dark-blue line, lw=1.2.
    Benchmarks = sparse unfilled scatter (every Nth point).
    Legend box tucked inside hill geometry (lower-left).
    """
    yh_fine = np.linspace(0, LY, 3000)
    zh_fine = hill_function(yh_fine)
    ax.fill_between(yh_fine / H_HILL, 0, zh_fine / H_HILL, color="0.92", zorder=0)
    ax.plot(yh_fine / H_HILL, zh_fine / H_HILL, color="0.40", lw=0.8, zorder=1)
    ax.axhline(y=LZ / H_HILL, color="0.40", lw=0.8, zorder=1)

    for xh in ALL_XH_STATIONS:
        # ── Station vertical dashed reference line (full height) ──
        ax.axvline(x=xh, color='0.75', linestyle=':', linewidth=0.4, zorder=0)

        # ── Benchmark sparse scatter (density-controlled) ──
        for src_id, info, bdata in bench_sources:
            if xh in bdata and field_bench in bdata[xh]:
                d_b = bdata[xh][field_bench]
                if d_b is None:
                    continue
                z_b = bdata[xh]["y"]
                ms = info.get('markersize', 3.5)
                # 使用密度控制子取樣 (取代舊的 markevery)
                density_pct = BENCH_DENSITY.get(src_id, 100)
                if density_pct <= 0:
                    continue  # 0% = 不顯示此來源
                z_sub, d_sub = subsample_uniform(z_b, d_b, density_pct)

                # ── Hill filter: only keep points above hill surface ──
                if info.get('filter_above_hill', False):
                    xh_physical = xh * H_HILL          # x in physical coords
                    z_hill = float(hill_function(np.array([xh_physical]))[0])
                    z_hill_norm = z_hill / H_HILL       # normalised y/H
                    mask = z_sub > z_hill_norm + 0.01   # small margin above hill
                    z_sub = z_sub[mask]
                    d_sub = d_sub[mask]
                    if len(z_sub) == 0:
                        continue

                ax.plot(d_sub * scale + xh, z_sub,
                        linestyle='none',
                        marker=info['marker'],
                        markersize=ms,
                        markerfacecolor='none',
                        markeredgecolor=info['color'],
                        markeredgewidth=0.5,
                        zorder=4)

        # ── Simulation line (on top) ──
        if HAS_VTK and xh in profiles:
            p = profiles[xh]
            data_sim = p.get(field_sim)
            if data_sim is not None:
                ax.plot(data_sim * scale + xh, p["z_abs"],
                        ls='-', color=c_sim, lw=1.2, zorder=5)

    if xlim_range is not None:
        ax.set_xlim(xlim_range)
        ax.set_xticks(range(int(np.floor(xlim_range[0])), int(np.ceil(xlim_range[1])) + 1))
    else:
        ax.set_xticks(range(10))
        ax.set_xlim(0, 9)

    ax.set_ylim(0, LZ / H_HILL)
    ax.set_yticks([0, 1, 2, 3])
    ax.set_aspect("equal", adjustable="box")

    # NO title — self-explanatory through legend box
    ax.set_xlabel(xlabel, fontsize=9)
    ax.set_ylabel(r"$y/H$ [-]", fontsize=9)
    ax.tick_params(labelsize=8)

    # ── Legend box ──
    _place_hill_legend(ax, field_label, scale, Re_val, legend_pos=legend_pos,
                       field_bench=field_bench)


def compute_offset_extent(field_sim, field_bench, scale, padding=0.3):
    """Compute the x-axis data extent for an offset profile panel."""
    x_min = min(ALL_XH_STATIONS)
    x_max = max(ALL_XH_STATIONS)
    for xh in ALL_XH_STATIONS:
        if HAS_VTK and xh in profiles:
            p = profiles[xh]
            data_sim = p.get(field_sim)
            if data_sim is not None:
                vals = data_sim * scale + xh
                x_min = min(x_min, float(np.min(vals)))
                x_max = max(x_max, float(np.max(vals)))
        for _, info, bdata in bench_sources:
            if xh in bdata and field_bench in bdata[xh]:
                d_b = bdata[xh][field_bench]
                if d_b is not None:
                    vals = d_b * scale + xh
                    x_min = min(x_min, float(np.min(vals)))
                    x_max = max(x_max, float(np.max(vals)))
    return x_min - padding, x_max + padding


# ================================================================
# Print L2 summary to terminal
# ================================================================
if HAS_VTK and bench_sources and L2_errors:
    print(f"\n{'='*60}")
    print(f"  L2(avg) Summary")
    print(f"{'='*60}")
    for field in ["U", "V", "uu", "vv", "uv", "k"]:
        parts = []
        for src_id, info, _ in bench_sources:
            if src_id not in L2_errors:
                continue
            vals = [L2_errors[src_id][xh].get(field)
                    for xh in L2_errors[src_id] if field in L2_errors[src_id].get(xh, {})]
            if vals:
                parts.append(f"{src_id} {np.mean(vals)*100:.2f}%")
        if parts:
            print(f"  L2(avg) {field:>3s}: {', '.join(parts)}")

# ================================================================
# 6 Separate Figures — Publication-quality (10×4 in, 300 dpi)
# ================================================================
# Each figure: one variable, no title, legend in hill base.
#
# Fig 1: ū/u_B   (streamwise mean velocity)
# Fig 2: v̄/u_B   (wall-normal mean velocity)
# Fig 3: u'u'/u_B²  (streamwise normal stress)
# Fig 4: v'v'/u_B²  (wall-normal normal stress)
# Fig 5: u'v'/u_B²  (Reynolds shear stress)
# Fig 6: k/u_B²     (turbulent kinetic energy)
#
xlim_plot = (-1, 10)
FIG_SIZE = (10, 4)
FIG_DPI = 300

# ── Figure definition table ──
# (field_sim, field_bench, scale_key, default_scale, field_label, filename,
#  turbulent_only, xlim_override, legend_pos)
# field_label_inner: LaTeX fragment WITHOUT outer $...$, used to build
#   a single unified math string for the legend header row.
#   ⟨ ⟩ notation for mean quantities; prime for fluctuations.
_FIGURE_DEFS = [
    ("U",  "U",  "U",  0.8,
     r"\langle u \rangle / u_B" if not LAMINAR else r"U / U_{ref}",
     "fig_mean_u.png", False, None, 'bottom-left'),
    ("V",  "V",  "V",  0.8,
     r"\langle v \rangle / u_B" if not LAMINAR else r"V / U_{ref}",
     "fig_mean_v.png", False, None, 'bottom-left'),
    ("uu", "uu", "uu", 30,
     r"\langle u'u' \rangle / u_B^2",
     "fig_uu.png", True, None, 'bottom-left'),
    ("vv", "vv", "vv", 30,
     r"\langle v'v' \rangle / u_B^2",
     "fig_vv.png", True, None, 'bottom-left'),
    ("uv", "uv", "uv", 60,
     r"\langle u'v' \rangle / u_B^2",
     "fig_uv.png", True, (-1.5, 10), 'top-right'),
    ("k",  "k",  "k",  20,
     r"k / u_B^2",
     "fig_k.png", True, None, 'bottom-left'),
]

# ── Helper: check if ANY benchmark source has data for a given bench field ──
def _any_bench_has_field(field_bench):
    """Return True if at least one benchmark source has non-None data for field_bench."""
    for _, _, bdata in bench_sources:
        for xh_data in bdata.values():
            if xh_data.get(field_bench) is not None:
                return True
    return False


def _bench_sources_missing_field(field_bench):
    """Return list of source labels that do NOT have data for field_bench."""
    missing = []
    for src_id, info, bdata in bench_sources:
        has_it = False
        for xh_data in bdata.values():
            if xh_data.get(field_bench) is not None:
                has_it = True
                break
        if not has_it:
            missing.append(info['label'])
    return missing


def _bench_sources_present_field(field_bench):
    """Return list of source labels that DO have data for field_bench."""
    present = []
    for src_id, info, bdata in bench_sources:
        for xh_data in bdata.values():
            if xh_data.get(field_bench) is not None:
                present.append(info['label'])
                break
    return present


print(f"\n{'='*60}")
print(f"  Generating publication figures (10×4 in, {FIG_DPI} dpi)")
print(f"{'='*60}")

# ── Pre-scan: report benchmark variable availability ──
print(f"\n  Variable availability for Re={Re}:")
for fs, fb, sk, default_sc, fl, fname, turb_only, xlim_ov, leg_pos in _FIGURE_DEFS:
    if turb_only and LAMINAR:
        continue
    sim_has = HAS_VTK and (not turb_only or HAS_RS)
    bench_present = _bench_sources_present_field(fb)
    bench_missing = _bench_sources_missing_field(fb)
    sim_tag = "VTK:YES" if sim_has else "VTK:NO"
    bench_tag = f"Bench:[{', '.join(bench_present)}]" if bench_present else "Bench:NONE"
    miss_tag = f"  Missing:[{', '.join(bench_missing)}]" if bench_missing else ""
    print(f"    {sk:>3s} ({fb:>3s}): {sim_tag}, {bench_tag}{miss_tag}")
print()

n_generated = 0
n_skipped = 0

for fs, fb, sk, default_sc, fl, fname, turb_only, xlim_ov, leg_pos in _FIGURE_DEFS:
    # Skip turbulent-only figures in laminar mode
    if turb_only and LAMINAR:
        continue

    # ── Skip if no benchmark source has data for this variable ──
    sim_has_field = HAS_VTK and (not turb_only or HAS_RS)
    bench_has_field = _any_bench_has_field(fb)

    if not bench_has_field:
        print(f"  [SKIP] {fname} — no benchmark data for '{fb}' at Re={Re}")
        n_skipped += 1
        continue

    sc = FIELD_PLOT_SCALE.get(sk, default_sc)
    fig, ax = plt.subplots(figsize=FIG_SIZE)
    # Use per-figure xlim override if defined, else global
    xlim_use = xlim_ov if xlim_ov is not None else xlim_plot
    plot_offset_panel(ax, fs, fb, scale=sc,
                      field_label=fl, Re_val=Re,
                      xlabel=r"$x/H$ [-]", xlim_range=xlim_use,
                      legend_pos=leg_pos)

    # ── Top-left info label: variable | Re | ×scale ──
    info_str = r'$' + fl + r'$' + f'  |  Re = {Re}  |  ' + r'$\times\,$' + f'{sc:g}'
    ax.text(
        0.01, 0.98, info_str,
        fontsize=8, fontfamily='serif',
        verticalalignment='top', horizontalalignment='left',
        transform=ax.transAxes,
        zorder=50,
        bbox=dict(facecolor='white', edgecolor='none', alpha=0.85, pad=1.5),
    )

    fig.tight_layout()
    _fig_outdir = os.environ.get('BENCHMARK_OUTDIR', SCRIPT_DIR)
    outpath = os.path.join(_fig_outdir, fname)
    os.makedirs(_fig_outdir, exist_ok=True)
    fig.savefig(outpath, dpi=FIG_DPI, bbox_inches="tight")
    n_generated += 1

    # ── Console output with status ──
    status_parts = []
    if sim_has_field:
        status_parts.append("VTK")
    present_benches = _bench_sources_present_field(fb)
    missing_benches = _bench_sources_missing_field(fb)
    if present_benches:
        status_parts.append(f"Bench:[{','.join(present_benches)}]")
    print(f"  [OK] {fname}  ({' + '.join(status_parts)})")
    if missing_benches:
        for mb_label in missing_benches:
            print(f"        [INFO] {mb_label}: no benchmark for '{fb}' at Re={Re}")
    plt.close(fig)

print(f"\n  Generated: {n_generated} figures, Skipped: {n_skipped}")

if LAMINAR:
    print("[INFO] Laminar mode — RS figures skipped.")