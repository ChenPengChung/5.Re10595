# 修改計畫：GILBM departure RK2 → RK4 step-doubling（precompute2.h，含嵌入式誤差自我認證）

> 目標：把 Algorithm2 precompute 的 departure 積分從 **explicit RK2 midpoint** 升級為
> **RK4 + step-doubling（Richardson）嵌入式誤差估計**，並把驗證閘從「folded == Algorithm1 RK2
> bit-exact」翻轉為「嵌入式誤差自我認證 + bounded-gap 報告」。
> **只改 Algorithm2 generator + 驗證；Algorithm1（`gilbm_rk2_displacement` + in-kernel 路徑）完全不動，當便宜 fallback。**
> 先在 **Edit10_GILBMworktree** 做 + 雙閘驗證；過了再套 **Edit6 / Edit7**（候選）。

---

## 0. 已確立事實（來自 departure-accuracy 調查 workflow，實測）

| 項 | 值 | 影響 |
|---|---|---|
| 現行 RK2 最壞 departure 誤差（本網格 STRETCH_A=0.95, NZ=321, dt=5.073e-4）| **6.27e-06 cells**（k=3 壁面），中心 3.8e-9 | 量級 negligible，但純壁面 ζ 強拉伸現象 |
| 換算 solution bias | ~6e-7 of Uref（低於 7 點插值 & O(Ma²) 3-4 階）| solution-level **大概看不出差** |
| **RK4 單步**最壞誤差 | **8.15e-12 cells**（斜率 5.0）| 本網格已≈機器精度，不需 Newton |
| iterate midpoint 到 1e-12 | 1.18e-6 cells，仍 O(dt²) | ❌ 駁回（只好 5×、破閘）|
| runtime 成本 | **每步 GPU = 0**（precompute 一次攤提）| 只有 Algorithm2 能享，Algorithm1 不能 |
| ITBLBM 機制 | Newton 逆映射（`ITB_NewtonSolveHost`），1.7M/1.7M 收斂、avg 2.05 iter、residual 1e-11 | 對照基準；RK4 已同級、更簡單 |

**定位**：此升級是「免費把 precompute 做到最準 + 自帶誤差證書」的工程衛生 / 未來保險
（換更粗網格或更大 dt 時 RK2 偏差會長大，RK4+embedded 自動扛），**非修當前 bug**。

---

## 1. 開關設計（可回退、可 A/B）

`variables.h`（或 precompute2.h 頂部）：
```c
#ifndef GILBM2_DEPARTURE_RK4
#define GILBM2_DEPARTURE_RK4 1   // 0 = legacy RK2 midpoint(逐位元同 Algorithm1); 1 = RK4 step-doubling + 嵌入式誤差
#endif
#ifndef GILBM2_DEPARTURE_ERRTOL
#define GILBM2_DEPARTURE_ERRTOL 1e-10   // 嵌入式 step-doubling 局部誤差容差(cells); 超過則自適應細分
#endif
#ifndef GILBM2_DEPARTURE_MAXDEPTH
#define GILBM2_DEPARTURE_MAXDEPTH 6      // 自適應細分最大遞迴深度(本網格幾乎用不到, 防呆上限)
#endif
```
- `=0` → 一切如現狀（RK2、bit-exact-vs-Algorithm1 驗證），**零行為改變**，可瞬間回退。
- `=1` → RK4 step-doubling + 翻轉驗證。**預設 1**（使用者要 RK4）。

---

## 2. 核心改動：`gilbm2_gen_departure_coords`（precompute2.h:173-244）

**只改「位移 (d_xi, delta_zeta) 的計算」；t_xi/t_zeta 推導 + 壁面 clamp（L226-237）原封不動。**

### 2a. 抽出取樣 helper（RK 各 stage 共用，= 現行 L195-222 的 7×7 Lagrange 取 metric）
```c
// 在計算空間位置 (pj,pk) 用 7×7 Lagrange 取 contravariant velocity (Vξ, Vζ)。
// stencil-base clamp 保持 7 點讀在陣列內(必要); 不對「位置」做 [3,NZ6-4] 壓平。
__host__ __device__ inline void gilbm2_sample_contravariant(
    double pj, double pk, double ey, double ez,
    const double *xi_y, const double *xi_z, const double *zeta_y, const double *zeta_z,
    double *Vxi, double *Vzeta)
{
    int sj = (int)floor(pj) - 3; if (sj<0) sj=0; if (sj+6>(int)NYD6-1) sj=(int)NYD6-7;
    int sk = (int)floor(pk) - 3; if (sk<0) sk=0; if (sk+6>(int)NZ6 -1) sk=(int)NZ6 -7;
    double aj[7], ak[7];
    gilbm2_lagrange7(pj-(double)sj, aj);
    gilbm2_lagrange7(pk-(double)sk, ak);
    double vxi=0.0, vze=0.0;
    for (int mj=0;mj<7;mj++){ int jj=sj+mj; double axi=0,aze=0;
        for(int mk=0;mk<7;mk++){ int kk=sk+mk; int id=jj*(int)NZ6+kk; double w=ak[mk];
            axi += w*(ey*xi_y[id]+ez*xi_z[id]); aze += w*(ey*zeta_y[id]+ez*zeta_z[id]); }
        vxi += aj[mj]*axi; vze += aj[mj]*aze; }
    *Vxi=vxi; *Vzeta=vze;
}
```

### 2b. 單步 RK4 位移（backward characteristic，h 步長）
ODE：index-space 位置 p=(j,k)，dp/dτ = −V(p)，積 [0,h] backward；位移 D = p(0)−p(h)。
```c
// 回傳位移 (Dxi, Dze) over 步長 h, 起點 (j0,k0)
__host__ __device__ inline void gilbm2_rk4_step(
    double j0, double k0, double h, double ey, double ez, /*4 metrics*/, double *Dxi, double *Dze)
{
    double V1x,V1z; gilbm2_sample_contravariant(j0,            k0,            ...,&V1x,&V1z);
    double V2x,V2z; gilbm2_sample_contravariant(j0-0.5*h*V1x,  k0-0.5*h*V1z,  ...,&V2x,&V2z); // = 現行 RK2 中點
    double V3x,V3z; gilbm2_sample_contravariant(j0-0.5*h*V2x,  k0-0.5*h*V2z,  ...,&V3x,&V3z);
    double V4x,V4z; gilbm2_sample_contravariant(j0-    h*V3x,  k0-    h*V3z,  ...,&V4x,&V4z);
    *Dxi = (h/6.0)*(V1x+2*V2x+2*V3x+V4x);
    *Dze = (h/6.0)*(V1z+2*V2z+2*V3z+V4z);
}
```
> 注意 stage2 = 現行 RK2 中點 → RK4 是現行碼的嚴格超集，stage3/4 + 端點為新增。

### 2c. Step-doubling（Richardson）嵌入式誤差 + 自適應細分
```c
// big = 一步 h; small = 兩步 h/2; E=|small-big|/15; use = small + (small-big)/15
// E>tol → 對半遞迴(bounded depth)。E 即「每輪 RK 的迭代誤差」(cells) = 自我認證證書。
```
- 回傳收斂位移 (d_xi, delta_zeta) + **嵌入式誤差 E（可選 out-param `double *err_out`）**。
- 本網格 E≈8e-12 < tol → 幾乎不觸發細分；細分只在壁面/粗網格偶發，全在 precompute 免費。

### 2d. 介面（向後相容）
`gilbm2_gen_departure_coords(... , unsigned char *flag_out=nullptr, double *err_out=nullptr)`：
- production（folded weights）路徑不傳 err_out。
- 驗證路徑傳 err_out 取每點嵌入式誤差。
- `#if GILBM2_DEPARTURE_RK4` → 走 2b/2c；`#else` → 原 RK2（L186-224 不動）。
- **t_xi/t_zeta 推導 + 壁面 clamp（L226-237）兩分支共用、完全不動** → 壁面 BC / consumer-cell 語意保持。

---

## 3. 驗證翻轉：`Algorithm2_ValidateCoordsTable`（2.algorithm2.h:515-643）

`#if GILBM2_DEPARTURE_RK4`（新路徑）：
1. **自我認證**：對每點取嵌入式誤差 E_local（gen_departure_coords 的 err_out），
   `assert max(E_local) < GILBM2_DEPARTURE_ERRTOL` → 證明 RK4 已收斂。不過則 MPI_Abort。
2. **bounded-gap 報告**：保留 `Algorithm2_RefCoords_Algo1Path`（RK2 參考），但**不再要求 bit-exact**；
   改算 `gap = max|coords_RK4 − coords_RK2|`，**印**「Algorithm2 比 Algorithm1 RK2 準 <gap> cells（預期 ~6e-6）」，
   並 `assert gap < 1e-3 cells`（sanity：RK4 沒爆掉，只是預期的 O(dt²) 改善量級）。
3. **in-stencil 檢查**：departure 落點 |t_xi|,|t_zeta| 在合理 stencil 範圍（壁面帶特別查）。
4. class-map 檢查（`Algorithm2_ValidateClassMap_Device`）原樣保留。

`#else`（legacy RK2）：現行 bit-exact-vs-Algorithm1 路徑**原封不動**。

> 哲學：不是「弱化驗證」，是「把參考從 Algorithm1-RK2 換成積分器自我認證 + 高精度語意」。
> RK4 的 8e-12 嵌入式誤差是比舊 bit-exact 更強的正確性證據。

---

## 4. 改動檔案清單（最小集）

| 檔 | 改動 | 不動 |
|---|---|---|
| `gilbm/precompute2.h` | 2a helper + 2b RK4 + 2c step-doubling + gen_departure_coords 加 `#if RK4` 分支 + err_out out-param | t_xi/t_zeta 推導、壁面 clamp、folded 結構/fold、lagrange7、class map |
| `gilbm/evolution_gilbm/2.algorithm2.h` | 驗證 `#if RK4` 自我認證 + gap 報告 + in-stencil | consumer hot path（折疊 MAC，零改動）、build_device kernel 結構 |
| `variables.h` | 加 `GILBM2_DEPARTURE_RK4` / `ERRTOL` / `MAXDEPTH` 開關 | 其餘 |
| `gilbm/evolution_gilbm/1.algorithm1.h` | **完全不動**（RK2 fallback）| 全部 |
| GPU per-step kernel / dispatch / memory | **零改動**（表值變、格式不變）| 全部 |

---

## 5. §3.5 驗證（每專案、雙閘 + solution-level）

1. **嵌入式自我認證**（runtime）：啟動時 `max E_local < 1e-10`，否則 abort（內建 dev gate）。
2. **編譯**：RK4=1 與 RK4=0 兩種都 BUILD OK（/tmp，無 running job）。
3. **codex 交叉** + **workflow 對抗式**：驗 RK4 數學（4 stage 係數 / backward 號 / step-doubling /15）、
   helper stencil-clamp 不越界、驗證翻轉正確、Algorithm1 fallback 完整、開關 gating。
4. **benchmark**：per-step 計時**應與現狀相同**（precompute 改動不碰 kernel）→ 確認 fold≈3.37ms 不變。
5. **solution-level A/B**（關鍵、誠實）：RK4 表 vs RK2 表跑 cf/cp/Reynolds stress；
   **預期無可測差**（插值才是精度瓶頸）→ 確認 no regression（不是期待變好）。

---

## 6. 順序（Edit10 先，過閘才外推）

1. **Edit10_GILBMworktree**：實作 2+3 → 編譯（RK4=1/0）→ 自我認證 → codex + workflow → benchmark → solution A/B。
2. **任一閘不過即停**，於 Edit10 修正，不外推。
3. **Edit6 + Edit7（候選）**：Edit10 全過後，把同改動套過去
   （Edit6 = 同 repo；Edit7 = 需重加 write_guard 白名單或從 Edit7 開 session）。各自再跑 codex + workflow + 編譯。

---

## 7. 風險 / 回退

| 風險 | 緩解 |
|---|---|
| RK4 stage 位置越界讀 ghost metric | helper 只 clamp stencil-base（讀在陣列內）；in-stencil 驗證 + gap sanity 守住 |
| 壁面 clamp 與高階軌跡衝突 | **只改位移計算**；最終 t_xi/t_zeta + 壁面 clamp 不動 → 壁面 BC 語意不變 |
| 驗證翻轉誤放水 | 自我認證(1e-10) 比舊 bit-exact 更嚴；gap sanity(<1e-3) 抓 RK4 爆掉；class-map 保留 |
| host/device FMA 差致嵌入式誤差不同 | err_out 容差 1e-10 >> ~1 ULP FMA 差；production 一律走 device 建表（bit-exact 自身） |
| solution 無改善 → 白做 | 定位本就是「保險 + 自我認證」，no-regression 即達標；不期待 solution 變好 |
| 回退 | `-DGILBM2_DEPARTURE_RK4=0` 瞬間回 legacy RK2 + 舊 bit-exact 驗證 |

---

*2026-06-12 初稿。實作於 Edit10；每輪 codex + workflow 雙閘 + solution-level A/B；全程不碰 running job。
Algorithm1 永遠不動當 fallback；開關預設 RK4=1，可 -D 回退。*
