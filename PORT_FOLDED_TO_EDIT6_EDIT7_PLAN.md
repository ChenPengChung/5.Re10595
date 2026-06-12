# 移植計畫：GILBM Algorithm2 折疊最快版 (STORE=2) → Edit6 + Edit7

> 目標：把 Edit10 的最優配置 `USE_GILBM_ALGORITHM2=1 + GILBM_ALGO2_STORE=2`
> (folded 單遍 flat MAC, 勝 ITBLBM) 移植到 Edit6 + Edit7。
> **限制：只移植最快版 (STORE=2)；STORE 0/1 為「裝飾」(完整版留 Edit10, 不移植避免錯誤)。**
> 多遍 (STORE=3) 已三方驗證不可行, 不存在。

---

## 0. 勘查事實 (決定策略)
| 事實 | 影響 |
|------|------|
| Edit6 + Edit10 = 同 repo (worktree); Edit7 同 GitHub remote, 獨立 working copy | Edit6=branch merge; Edit7=獨立移植 |
| **Edit6 已有 Algorithm2** (precompute2.h + 2.algorithm2.h, 開關 5 處) 但可能舊版 | Edit6 = merge Edit10 最新 folded |
| **Edit7 無 Algorithm2** (Algorithm1-only) | Edit7 = 完整 folded-only 移植 + 適配 |
| Edit6/Edit7 無 running job (88688/88931 COMPLETED) | 移植安全 |
| Edit7 用 GILBM RK2 departure (1.algorithm1.h) | folded 移植可行 (同底層) |

---

## 1. 開關設計 (「裝飾」語意)

每個目標專案 variables.h:
```c
#ifndef USE_GILBM_ALGORITHM2
#define USE_GILBM_ALGORITHM2 0      // 0=Algorithm1(原版) 1=Algorithm2 折疊查表
#endif
```
precompute2.h (或 variables.h):
```c
#define GILBM2_STORE_COORDS         0   // 裝飾: 完整版在 Edit10, 本專案未移植
#define GILBM2_STORE_WEIGHTS        1   // 裝飾: 同上
#define GILBM2_STORE_WEIGHTS_FOLDED 2   // ★唯一生效: 折疊最快版★
#ifndef GILBM_ALGO2_STORE
#define GILBM_ALGO2_STORE GILBM2_STORE_WEIGHTS_FOLDED   // 預設直接 = 2 (本專案只用最快版)
#endif
#if USE_GILBM_ALGORITHM2 && (GILBM_ALGO2_STORE == 0 || GILBM_ALGO2_STORE == 1)
#error "本專案只移植 folded(STORE=2); COORDS/WEIGHTS 完整版在 Edit10。請用 GILBM_ALGO2_STORE=2"
#endif
```
→ **STORE 0/1 = 裝飾 (註解 + #error 擋), 編譯只認 STORE=2。**

**GILBM_ALGO2_VALIDATE (需移植, 非裝飾, 預設 1)**:
```c
#ifndef GILBM_ALGO2_VALIDATE
#define GILBM_ALGO2_VALIDATE 1   // 啟動表驗證: 1=init 驗折疊表(bitwise+1e-12+classmap)不過則 abort; 0=跳過
#endif
```
- **不影響算法/結果**: 0 與 1 的 solver(折疊 consumer streaming/collision/插值)完全相同;
  此開關只控制「啟動時要不要驗表」, 不碰任何計算路徑。
- 1 = 多花 ~幾秒 init 驗表(抓建表錯誤, 安全網); 0 = 跳過(init 略快, 無安全網)。
- **移植時設 1 (安全)。** 與 STORE 不同: STORE 0/1 是裝飾(#error 擋), VALIDATE 0/1 都是真實可用值。

---

## 2. folded-only 最小檔案集 (移植清單)

| 檔案 | 移植內容 (僅 folded 路徑) | COORDS/WEIGHTS 處理 |
|------|--------------------------|---------------------|
| `gilbm/precompute2.h` | folded struct `GILBM2_DepartWeightsFolded` + folded 生成器 `gilbm2_gen_departure_weights_folded` + class map + lagrange7 helper + fold_zeta_ghost | COORDS/WEIGHTS struct/生成器可省略或留但 typedef 只 resolve folded |
| `gilbm/evolution_gilbm/2.algorithm2.h` | folded consumer (flat MAC 2D/3D) + 表 build/驗證 | legacy #else 分支省略 (STORE 0/1 已 #error) |
| `variables.h` | 上節開關 | — |
| `main.cu` | 表 build (`BuildGILBM2DepartureTableHost` + device build) + §B5 驗證呼叫 | — |
| `evolution.h` | dispatch: `Algorithm2_FusedKernel_GTS_Buffer` (folded) 的 launch | — |
| `memory.h` | 表 buffer `gilbm2_coords_d` alloc | — |
| `0.shared_code.h` | `__constant__ GILBM_L_eta_shared` 等 (Edit7 可能已有) | — |

> wall-race 雙緩衝 fix: Edit6/Edit7 已有(前已驗證), 移植 folded 須**保留不破壞**。

---

## 3. 移植策略 (逐專案, 風險不同)

### A. Edit6_5600DNS (同 repo, 已有 Algorithm2 舊版) — 低風險先做
1. **驗證 diff**: `git log Edit6_5600DNS..Edit10_GILBMworktree -- gilbm/ 2.algorithm2.h precompute2.h` 看 Edit10 領先哪些 folded 改進(連續 k_idx / 3D 重排 / wall-race)。
2. **merge/cherry-pick** Edit10 的 folded 改進 commit 進 Edit6_5600DNS branch (同 repo, 乾淨)。
   - 衝突則逐檔解 (Edit6 的 Algorithm1 路徑不動, 只更新 folded consumer + 表)。
3. variables.h 設 `USE_GILBM_ALGORITHM2=1` + `GILBM_ALGO2_STORE=2` + STORE 0/1 裝飾 #error。
4. **驗證**: dev A/B (folded vs Algorithm1, 1e-12) + 編譯 + benchmark (gilbm_a≈4.48 閘 / fold≈3.37)。

### B. Edit7_10595SNS (無 Algorithm2) — 完整移植, 後做
1. **複製 folded-only 檔案** (上節清單) 進 Edit7, **適配 Edit7 SNS 結構**(kernel body / macro / 已有的 wall-race)。
2. STORE 0/1 裝飾 #error; STORE 預設 2。
3. **適配點** (SNS 與 GILBM 差異): 確認 Edit7 的 1.algorithm1.h departure (gilbm_rk2) 與 folded 表生成器相容; dispatch 接 Edit7 的 fused kernel; memory/init 對齊。
4. **驗證**: 因 Edit7 無現成 A/B harness → 移植 A/B harness 或寫最小 determinism test; dev 驗 1e-12 vs Edit7 Algorithm1; 編譯 + benchmark。

---

## 4. §3.5 驗證 (每專案、每步雙閘)
1. **dev A/B 1e-12**: folded vs 該專案 Algorithm1 (全域含壁面)。
2. **編譯**: BUILD OK (到 /tmp 或該專案, 無 running job 故安全)。
3. **codex 交叉** + **workflow 對抗式**: 驗 folded consumer + 表生成 + wall-race 保留 + STORE 裝飾 gating。
4. **benchmark**: 健康節點 (gilbm_a≈4.48 品質閘) + 同 seed, 確認 fold≈3.37/856 勝 ITBLBM。

---

## 5. 安全守門 (MUST)
- **無 running job** 才動 (88688/88631 COMPLETED 已確認; 動前再查一次)。
- **跨專案 write guard**: Edit6 = 本 session 專案(放行); **Edit7 需先加白名單** (改 ~/.claude/hooks/write_guard.sh 加 Edit7 例外, 同 Edit10 做法) 或從 Edit7 開 session。
- **絕不碰 Edit8/Edit9** (running job)。
- **保留 wall-race fix** (移植 folded 不得破壞已驗證的雙緩衝)。
- 非 ff 不 --force; 逐檔 commit 繁中。

---

## 6. 順序 (低風險先)
1. **Edit6** (同 repo merge, 已有 Algorithm2) → 驗證過閘。
2. **Edit7** (完整移植 + SNS 適配) → 驗證過閘。
3. 每步 1e-12 + codex + workflow; 任一不過即停, 不前進。

---

## 7. 風險 / 回退
| 風險 | 緩解 |
|------|------|
| Edit6 既有 Algorithm2 與 Edit10 merge 衝突 | 逐檔解, Algorithm1 路徑不動 |
| Edit7 SNS 結構差異 | 先勘 Edit7 kernel/dispatch, 適配後再驗 |
| wall-race fix 被破壞 | 移植後重跑 wall-race A/B (壁面 0 mismatch) |
| Edit7 無 A/B harness | 移植 harness 或寫最小 determinism test |
| 開關裝飾誤設 STORE 0/1 | #error 硬擋, 編不過即明確提示 |
| 回退 | 開關預設可切回 USE_GILBM_ALGORITHM2=0 (原 Algorithm1), 不影響原生產 |

---

*2026-06-12 初稿。實作前需使用者確認; 每實作輪 codex + workflow 雙閘; 全程不碰 running job。*
