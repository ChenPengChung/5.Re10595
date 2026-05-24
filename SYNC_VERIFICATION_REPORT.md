# 跨專案同步驗證報告

> 驗證範圍: commit 7185dab..e3ec016 (Edit6_5600DNS)
> 驗證日期: 2026-05-24
> 驗證者: Claude Opus 4.6

---

## 一、涉及的 Commits

| Commit | 說明 | 涉及檔案 |
|--------|------|---------|
| 7185dab | #if ALPHA 浮點修正、STRETCH_A_X 自動同步 | `grid_zeta_tool.py` |
| 1e62ee6 | 增加密度檢驗 | `grid_zeta_tool.py` |
| 0fcc6bf | 靜默避開壞掉 GPU (node-level blacklist) | `main.cu` |
| 0daa9ba | GPU 增強設定 (interp 參數配置) | `interp_checkpoint.py` |
| 4e706e3 | solver-Jacobian 質量修正 (473 行核心) | `interp_checkpoint.py` |
| 133807b | H200 配置更新 2 nodes × 16 GPU | `variables.h`, `chain_code/` |
| b841c89 | interp_checkpoint 網格推斷 + legacy 自動查找 | `interp_checkpoint.py` |
| e3ec016 | NP fallback 回歸修正 8→16 | `jobscript_chain.slurm.H200/.GB200` |

---

## 二、D3Q19 三專案同步狀態 (Edit4 / Edit5 / Edit6)

### 核心求解器 (.h / .cu)

| 檔案 | Edit4↔Edit6 | Edit5↔Edit6 | 差異類型 |
|------|:-----------:|:-----------:|---------|
| mrt_projection_host.h | ✅ 相同 | ✅ 相同 | — |
| 0.collision.h | 註解差 | ✅ 相同 | INFRA |
| 0.shared_code.h | 註解差 | ✅ 相同 | INFRA |
| 1.algorithm1.h | 清理差 | 清理差 | WALL_GRAD_ORDER dead code 移除 |
| boundary_conditions.h | 清理差 | 清理差 | 同上 |
| diagnostic_gilbm.h | 清理差 | 清理差 | 同上 |
| communication.h | ✅ 相同 | ✅ 相同 | — |
| initialization.h | ✅ 相同 | ✅ 相同 | — |
| initializationTool.h | ✅ 相同 | ✅ 相同 | — |
| convergence.h | ✅ 相同 | ✅ 相同 | — |
| statistics.h | ✅ 相同 | ✅ 相同 | — |
| model.h | ✅ 相同 | ✅ 相同 | — |
| MRT_Matrix.h | ✅ 相同 | ✅ 相同 | — |
| MRT_Process.h | ✅ 相同 | ✅ 相同 | — |
| common.h | ✅ 相同 | ✅ 相同 | — |
| memory.h | 排序差 | ✅ 相同 | INFRA |
| evolution.h | const 差 | 空行差 | INFRA |
| fileIO.h | 註解差 | 註解差 | INFRA |
| monitor.h | ✅ 相同 | ✅ 相同 | — |
| timing.h | ✅ 相同 | ✅ 相同 | — |
| stop_control.h | ✅ 相同 | ✅ 相同 | — |
| runtime_args.h | ✅ 相同 | ✅ 相同 | — |
| log_truncate.h | ✅ 相同 | ✅ 相同 | — |
| interpolation_gilbm.h | ✅ 相同 | ✅ 相同 | — |
| metric_terms.h | ✅ 相同 | ✅ 相同 | — |
| precompute.h | ✅ 相同 | 註解差 | INFRA |
| weno7_core.h | ✅ 相同 | ✅ 相同 | — |

**結論: 演算法完全同步。** Edit6 清理了 WALL_GRAD_ORDER dead code 和 eta_sign 寫法，
在 Edit4/5 設定 WALL_GRAD_ORDER=6 時編譯結果完全一致。

### 共享工具檔案

| 檔案 | Edit4↔Edit6 | Edit5↔Edit6 |
|------|:-----------:|:-----------:|
| grid_zeta_tool.py | ✅ 0 diff | ✅ 0 diff |
| interp_checkpoint.py | ✅ 0 diff | ✅ 0 diff |

---

## 三、D3Q27 (Edit1/Edit2) vs D3Q19 (Edit6) 同步狀態

### chain_code 基礎設施

| 檔案 | Edit1↔Edit6 | Edit2↔Edit6 | 狀態 |
|------|:-----------:|:-----------:|------|
| run.sh | ✅ SYNC | ✅ SYNC (+duct preflight) | SYNC |
| build_and_submit.sh.H200 | ✅ SYNC | ✅ SYNC | SYNC |
| build_and_submit.sh.GB200 | ✅ SYNC | ✅ SYNC | SYNC |
| jobscript_chain.slurm.H200 | CONFIG 差 | CONFIG 差 | EXPECTED |
| jobscript_chain.slurm.GB200 | CONFIG 差 | CONFIG 差 | EXPECTED |
| dispatcher_*.sh (3 檔) | ✅ SYNC | ✅ SYNC | SYNC |
| chain_status.sh | ✅ SYNC | ✅ SYNC | SYNC |
| partition_ctl.sh | ✅ SYNC | ✅ SYNC | SYNC |
| submit_dispatcher.sh | ✅ SYNC | ✅ SYNC | SYNC |
| tools/ (8 檔) | ✅ SYNC | ✅ SYNC | SYNC |
| main.cu (GPU guard) | ✅ SYNC | ✅ SYNC | SYNC |

### 需要回移的問題

#### HIGH — 功能正確性

| # | 問題 | 影響 | 位置 |
|---|------|------|------|
| H1 | interp_checkpoint.py: 非有限數 divergence gate 被 Edit6 移除 | **Edit6 回歸** — NaN 會靜默通過 | Edit1 保留，需回移至 Edit6 |
| H2 | interp_checkpoint.py: `compute_jacobian_gl_cell_areas()` 缺失 | Edit1, Edit2 | Jacobian-GL 質量修正所需 |
| H3 | interp_checkpoint.py: `CLAMP_FRACTION_FATAL` 缺失 | Edit2 | 過度 clamp 無法偵測 |
| H4 | interp_checkpoint.py: `compute_grid_coord_sha256` 缺失 | Edit2 | 網格一致性驗證缺失 |

#### MEDIUM — 健壯性

| # | 問題 | 影響 | 位置 |
|---|------|------|------|
| M1 | grid_zeta_tool.py: 簡化 idempotency check 未回移 | Edit1, Edit2 | mtime 檢查易誤觸重生 |
| M2 | grid_zeta_tool.py: STRETCH_A 未加入 required list | Edit1, Edit2 | 缺少時 runtime 錯誤不明確 |
| M3 | watcher: Re auto-detect + --Re explicit 應合併 | 全部 | Edit6 硬編碼 Re=5600 |
| M4 | repartition_jp.py 缺失 | Edit1, Edit2 | 改 jp 時需重新插值 |

#### LOW — 品質改善

| # | 問題 | 影響 |
|---|------|------|
| L1 | grid_zeta_tool.py: 中/英文 log 不一致 | Edit1/2 英文, Edit6 中文 |
| L2 | interp_checkpoint.py: --project-velocity docstring 寫 poisson, default 是 div-exact | Edit6 |
| L3 | interp_checkpoint.py: cv_weights 函式簽名 (cfg,x,y,z) vs (y,z,cfg) | Edit1/2 vs Edit6 |

---

## 四、已修正的問題

| 問題 | Commit | 說明 |
|------|--------|------|
| NP fallback 8→16 回歸 | e3ec016 | H200/GB200 jobscript 已修正 |

---

## 五、數學驗證摘要 (interp_checkpoint.py Jacobian-GL)

| 驗證項目 | 結果 |
|---------|------|
| J_2D = y_ξ·z_ζ − y_ζ·z_ξ | ✅ 正確 |
| 3×3 Gauss-Legendre 節點/權重 | ✅ 正確 |
| Rank-local stencil 匹配 solver | ✅ 正確 |
| sum(f) = rho 守恆 | ✅ 7.8e-16 (機器精度) |
| div(u*) gate | ✅ 5.1e-12 < 1e-10 |
| CE f_neq 重建 | ✅ D3Q19 對稱保證 |
| rho 質量修正 | ✅ -6.3e-10 (幾乎為零) |
| 所有 f_q > 0 | ✅ 確認 |
