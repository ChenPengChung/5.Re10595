# New Grid Pipeline — Variable Gamma(y) 網格生成系統

> Edit6_5600DNS 專案  
> 最後更新: 2026-05-03

---

## 1. 概述

本專案使用 **Mode 3 (variable gamma)** 網格生成流程,
根據前次 CFD 模擬的壁面摩擦速度 u_tau(y) 資料,
在每個流向站點計算不同的 Vinokur tanh 拉伸參數 gamma(y),
確保全域 z+ < 1.0 (DNS 解析度要求).

### 呼叫方式

```bash
python3 restart_tools/grid_zeta_tool.py --auto
```

一行指令. 從 `variables.h` 讀取所有參數, 全自動完成.

---

## 2. Pipeline 流程

```
variables.h
    |
    |  grid_zeta_tool.py --auto 讀取所有 #define
    |  偵測 UTAU_BOT_DAT / UTAU_TOP_DAT 是否定義
    |    有定義 --> Mode 3 (variable gamma)
    |    無定義 --> Mode 2 (Poisson + 均勻 GAMMA)
    v
restart_tools/grid_zeta_tool.py --auto
    |
    |--- [輸入 1] Base topology grid (skip Poisson)
    |    J_Frohlich/adaptive_3.fine grid_I257_J129_g2.0_a0.5.dat
    |
    |--- [輸入 2] 底壁 u_tau
    |    J_Frohlich/29.Re5600_j257_zplus_bottom_normal_spanavg_2nd.dat
    |
    |--- [輸入 3] 頂壁 u_tau
    |    J_Frohlich/28.Re5600_j257_zplus_top_spanavg_2nd.dat
    |
    v  計算 gamma(y) --> redistribute_vertical_adaptive
    |
    |--- [輸出] J_Frohlich/adaptive_3.fine grid_I257_J129_a0.5.dat  <-- 新格點
    |--- [診斷] gamma_field, sensitivity, grid_data, compare plot
    v
initialization.h : ReadExternalGrid_YZ()
    snprintf("%s/adaptive_%s_I%d_J%d_a%.1f.dat",
             "J_Frohlich", "3.fine grid", NY, NZ, ALPHA)
    --> 讀取 J_Frohlich/adaptive_3.fine grid_I257_J129_a0.5.dat
    |
    v
Jacobian 計算 --> kernel loop 開始模擬
```

---

## 3. 檔案清單

### 3.1 程式碼 (restart_tools/)

| 檔案 | 行數 | 角色 |
|------|------|------|
| `grid_zeta_tool.py` | ~2750 | 網格生成主程式 (Mode 1/2/3 + --auto + --verify) |
| `interp_checkpoint.py` | ~1028 | Checkpoint 插值工具 (舊網格 --> 新網格 restart 資料轉移) |

### 3.2 輸入資料 (J_Frohlich/)

| ���案 | 大小 | 角色 |
|------|------|------|
| `adaptive_3.fine grid_I257_J129_g2.0_a0.5.dat` | 1.1 MB | Base topology (前次 Mode 2 生成, 均勻 GAMMA=2.0) |
| `29.Re5600_j257_zplus_bottom_normal_spanavg_2nd.dat` | 59 KB | 底壁 u_tau(y), 257 站點, Re=5600 |
| `28.Re5600_j257_zplus_top_spanavg_2nd.dat` | 36 KB | 頂壁 u_tau(y), 257 站點, Re=5600 |

### 3.3 輸出 (J_Frohlich/)

| 檔案 | 角色 | C 碼讀取 |
|------|------|----------|
| `adaptive_3.fine grid_I257_J129_a0.5.dat` | 新格點 (Tecplot POINT, I=257 J=129) | initialization.h 直接讀取 |
| `gamma_field_I257_J129_a0.5.dat` | gamma(y) 場 (每站 gamma, z+_bot, z+_top, z+_max) | 診斷用 |
| `sensitivity_I257_J129_a0.5.dat` | 敏感度分析 (u_tau 增長裕度) | 診斷用 |
| `grid_data_I257_J129_a0.5.txt` | i=0 列 dz_min/dz_max/拉伸比 | 診斷用 |
| `compare_auto_I257_J129_a0.5.png` | 新舊格點對比圖 | 視覺化 |

### 3.4 設定檔

| 檔案 | 角色 |
|------|------|
| `variables.h` | 唯一設定來源, grid_zeta_tool.py 與 C 碼共用 |
| `initialization.h` | C 碼讀取格點 (ReadExternalGrid_YZ, snprintf 命名慣例) |

---

## 4. variables.h 相關 define

### 4.1 網格幾何 (Mode 2 / Mode 3 共用)

| define | 值 | 說明 |
|--------|-----|------|
| `NY` | 257 | 流向格點數 (node count) |
| `NZ` | 129 | 法向格點數 (node count) |
| `LY` | 9.0 | 流向 (streamwise) 長�� |
| `LZ` | 3.036 | 法向 (wall-normal) 長度 |
| `GAMMA` | 4.3217 | gamma(y) 最大值, 用於 minSize/dt 參考 |
| `ALPHA` | 0.5 | Vinokur 對稱參數 (0.5 = 上下壁等密) |
| `CFL` | 0.5 | CFL 數 |

### 4.2 網格路徑

| define | 值 | 說明 |
|--------|-----|------|
| `GRID_DAT_DIR` | `"J_Frohlich"` | 格點檔案目錄 (相對 variables.h) |
| `GRID_DAT_REF` | `"3.fine grid.dat"` | Frohlich 原始參考網格 (Mode 2 用) |

### 4.3 Mode 3 專用 (定義即啟用, 註解掉則退回 Mode 2)

| define | 值 | 說明 |
|--------|-----|------|
| `UTAU_BOT_DAT` | `"29.Re5600_...dat"` | 底壁 u_tau 檔案名 |
| `UTAU_TOP_DAT` | `"28.Re5600_...dat"` | 頂壁 u_tau 檔案名 |
| `UTAU_RE` | 5600 | u_tau 資料來源的 Re (非模擬 Re=10595) |
| `ZP_TARGET` | 0.9 | z+ 設計目標 (0.9 = 10% 安全裕度) |

---

## 5. Mode 3 內部演算法

```
1. 搜尋 J_Frohlich/ 中已存在的 adaptive_*_I257_J129_*.dat
   --> 找到: 直接作為 base topology (skip Poisson, 快速)
   --> 沒找到: 從 GRID_DAT_REF 跑 Poisson 求解 (gamma=0) 得到 base

2. 載入 u_tau 資料 (底壁 + 頂壁, 各 257 站點)
   L_column(y) = z_top(y) - z_bottom(y)

3. 計算 gamma(y) 場 (compute_gamma_field):
   - 底壁/頂壁分別計算所需 gamma
   - 取 max --> max-filter 擴展 --> Gaussian 平滑 --> clamp
   - 保證 z+(y) <= ZP_TARGET everywhere

4. 法向重分佈 (redistribute_vertical_adaptive):
   - 每個流向站點用不同 gamma(y) 做 Vinokur tanh 拉伸
   - 保持流向拓撲不變, 只改法向分佈

5. 驗證:
   - Cell area 正值檢查
   - GILBM 穩定性 (omega, max|c_tilde|)
   - 敏感度分析 (u_tau 增長裕度)
```

---

## 6. 命名慣例對齊

**C 碼** (`initialization.h:94-97`):
```c
snprintf(grid_dat_path, sizeof(grid_dat_path),
         "%s/adaptive_%s_I%d_J%d_a%.1f.dat",
         GRID_DAT_DIR, "3.fine grid", NY, NZ, (double)ALPHA);
```
生成路徑: `J_Frohlich/adaptive_3.fine grid_I257_J129_a0.5.dat`

**Python** (`grid_zeta_tool.py`):
```python
out_name = f"adaptive_{grid_key}_I{NI}_J{NJ}_a{alpha:.1f}.dat"
```
生成路徑: `J_Frohlich/adaptive_3.fine grid_I257_J129_a0.5.dat`

兩者完全一致. 舊格點 `_g2.0_a0.5.dat` 因名稱不同, C 碼不會誤讀.

---

## 7. 當前數值結果

| 參數 | 值 |
|------|-----|
| gamma(y) 範圍 | [2.8493, 4.3217] |
| gamma(y) 平均 | 3.3230 |
| z+ 最大值 | 0.9000 (全 257 站 < 1.0) |
| omega (GILBM) | 0.5322 (GOOD/OPTIMAL) |
| max\|c_tilde\| | 597.1 |
| 最大拉伸比 | 18.7:1 (at gamma_max) |
| u_tau 安全裕度 | >= 11.1% (grid refinement 僅 2-5% 變化) |

---

## 8. Mode 2 / Mode 3 自由切換

兩種模式共用同一支程式 (`grid_zeta_tool.py --auto`),
由 `variables.h` 中是否定義 `UTAU_BOT_DAT` / `UTAU_TOP_DAT` 決定.

### 切換到 Mode 2 (Poisson + 均勻 GAMMA)

在 `variables.h` 中註解掉 Mode 3 的 4 個 define:

```c
// #define UTAU_BOT_DAT  "..."
// #define UTAU_TOP_DAT  "..."
// #define UTAU_RE       5600
// #define ZP_TARGET     0.9
```

所需檔案:

| 檔案 | 狀態 |
|------|------|
| `J_Frohlich/3.fine grid.dat` | 已備妥 (Frohlich 原始 197x129 參考網格) |
| `GAMMA` in variables.h | 均勻拉伸參數 (如 2.0 或 3.0) |

### 切換到 Mode 3 (variable gamma from u_tau)

取消註解 Mode 3 的 4 個 define, 確保 u_tau .dat 檔案存在.

所需檔案:

| 檔案 | 狀態 |
|------|------|
| `J_Frohlich/adaptive_*_I257_J129_*.dat` | 任一已存在的 adaptive grid 作為 base topology |
| `J_Frohlich/{UTAU_BOT_DAT}` | 底壁 u_tau |
| `J_Frohlich/{UTAU_TOP_DAT}` | 頂壁 u_tau |

若無現成 base grid, 會自動從 `3.fine grid.dat` 跑 Poisson 生成.

### 切換對照表

| 項目 | Mode 2 | Mode 3 |
|------|--------|--------|
| UTAU_BOT/TOP_DAT | 註解掉 | 定義 |
| GAMMA 意義 | 均勻拉伸值 | gamma(y) 最大值 (minSize 參考) |
| 需要 3.fine grid.dat | 必要 | 僅在無 base grid 時需要 |
| 需要 u_tau .dat | 不需要 | 必要 |
| 輸出檔名 | 相同 | 相同 |
| C 碼讀取 | 不需改動 | 不需改動 |
