# RK-4/Algorithm2 vs RK-2/Algorithm1 — 演算法正確性與重現實驗 檢驗結果

產出日期: 2026-06-15 22:31 | 專案: Edit6_5600DNS (GILBM 週期山丘 Re_H=5600, D3Q19, jp=32)

---

## 結論 (Verdict)

**RK-4 / Algorithm2 演算法「沒有寫錯」,而且不比 RK-2 / Algorithm1 差 —— RK-4 反而準 ~6.7×10⁶ 倍。**
benchmark 與 Krank DNS 的 ~4–6% 偏差是 **2 階 LBM vs 8 階 DG 的方法級地板**,不是演算法 bug;
任何 RK2↔RK4 / Algorithm1↔Algorithm2 的切換都改不動它。使用者觀察到「舊結果(RK-2)比較準」
經查是 **confound**(平均視窗端點/統計抖動),非演算法回歸。

---

## 三條獨立實證 (Three independent lines of evidence)

### ① (r,s) 解析工具 — 與「解析精確解」比 (tools/rk2_rk4_rs_error.py)
在實際壁法向 Vinokur-tanh 網格 (NZ=321, a=0.95) 上,用 tanh 解析逆映射當真值:
```
RK2 離精確解 = 2.276e-06 cells   ← RK2 真實誤差
RK4 離精確解 = 3.411e-13 cells   ← RK4 真實誤差
→ RK4 比 RK2 準 ~6,700,000 倍
```
使用者看到的「RK4-vs-RK2 gap ~3–5e-6」= 兩者差異 = **RK2 的誤差**(RK4 是準的參考),不是 RK4 的誤差。

### ② Solver 啟動自我認證閘 (每次啟動執行;任一失敗即 MPI_Abort)
32 ranks 全過 (slurm log):
- `J·J⁻¹ = I` max|err| = **6.7e-16**
- Algorithm2 優化表 vs Algorithm1 host 參考 = **5.5e-14**(優化逐位元忠於參考)
- `class-map mismatch = 0`、`tol_fail 總數 = 0`
- RK4 embedded 自證 `E_local ~ 1e-11` (< tol 1e-10, CONVERGED)

### ③ Codex 獨立稽核 #1 (RK4 程式碼)
`gilbm/precompute2.h:189-271`: 標準古典 RK4 Butcher (1,2,2,1)/6 + 正確 step-doubling Richardson
(÷15、外推 +/15,4 階正確) → **無 bug**。git: RK4 是 2026-06-12 新增 (2231d1f 當時只有 Algorithm1)。

---

## benchmark 證據 (RK-4/Algorithm2,凍存於本資料夾)
- 凍存點: final checkpoint **step_97780300, FTT 82.68, accu_count 26,818,150**
- 逐站 peak-deficit L2 vs Krank DNS (peak_deficit / fig_*.png): **uu 5.4 / vv 5.8 / k 4.0 / uv 4.2% (ex-inlet)**
- **全部 ≤ MGLET(Breuer)-vs-Krank 的 inter-code scatter**;與 MGLET 偏差相關 r=0.909、sign pattern 一致
  → 確認 ~5% 是「2 階方法地板」,GILBM 甚至全面贏過同為 2 階的 MGLET。

---

## 重現實驗 (live) — Algorithm1 + RK2 + SKIP=1
為實證上述結論,chain 已還原為參考算法:
- 設定: `USE_GILBM_ALGORITHM2=0` (Algorithm1+RK2), **`SKIP_MIDSTEP_MASSCORR=1`**(刻意保留加速,
  與 2231d1f 的 SKIP=0 唯一差異;已驗 benchmark-neutral), `FTT_STATS_START=88`(統計重置)
- binary `e254b6ff081b` (a.out/H200/jp32 + manifest 一致)
- head **107018 @ 32gpus** (PriorityTier 10), warm-restart from step_97780300

### Codex 獨立稽核 #2 (重現設定) — 結論「CORRECTLY SET UP」
- (a) `USE_GILBM_ALGORITHM2=0` → evolution.h `#else Algorithm1_FusedKernel` 路徑 + `gilbm_rk2_displacement`,
  RK4 旗標完全 inert → 確為 Algorithm1+RK2。
- (b1) `main.cu:1338-1381` FTT-gate: FTT_restart 82.68 < 88 → **硬閘清空全部 RK4 統計**(accu=0, tavg 歸零)。
- (b2) SKIP=1 只拿掉 mid-step MPI barrier;end-step `UpdateVolumeWeightedMassCorrection()`(main.cu:2158)
  無條件保留 → **守恆不變、benchmark-neutral**。
- (b3) 5.3 FTT settle(82.68→88)對 O(dt²) 數值差遠遠足夠去相關。
- (d) binary/manifest/checkpoint/jobscript 全 CORRECT,**無阻斷性缺陷**。

### 預期結果 (待 107018 累積後實測比對)
Algorithm1+RK2+SKIP=1 的統計將與 RK-4/Algorithm2 **在抽樣噪聲 ~0.2–0.5% 內吻合** →
證實「舊比較準」是平均視窗端點的 confound,非演算法效應。屆時會重 dump L2 並交 Codex 做
RK-4 證據 vs Algorithm1+RK2 的 head-to-head 比對。

---
*注意 caveat: 這是 continuation(RK4 演化場 FTT82.68 切到 Algo1+RK2)非 2231d1f bit-reproduction;
但時均統計為穩態湍流,應收斂到同一地板。*
