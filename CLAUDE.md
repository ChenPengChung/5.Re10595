# CLAUDE.md

## Auto-commit (triggered by user command)

When the user types **`claude commit`**, execute the following:

1. Run `git status` and `git diff` to check for uncommitted changes.
2. If there are any staged or unstaged changes (modified, added, or deleted files):
   a. Stage all changed files with `git add` (specific files, not `-A`).
   b. Generate a concise commit message in **Traditional Chinese** that summarizes what changed, following the existing commit style (short descriptive phrase).
   c. Create the commit.
   d. Push to the remote tracking branch with `git push`.
   e. Report the result to the user.
3. If there are no changes, report "沒有未提交的變更。"

**Do NOT auto-commit on session start.** Only commit when the user explicitly says `claude commit`.

## Project info

- Branch: Edit2_test119regular
- Remote: origin (GitHub)
- Language: commit messages should be in Traditional Chinese (繁體中文)
- This is a CFD (Computational Fluid Dynamics) LBM simulation project running on HPC clusters (H200/GB200).

---

## 專案狀態記錄 (Project Status Log)

**最後更新**: 2026-05-14

### 當前模擬配置

| 參數 | 值 | 說明 |
|------|-----|------|
| **Re** | 5600 | 基於 H_HILL 和 Uref |
| **Grid** | 129×257×129 (NX×NY×NZ) | 展向×流向×法向 |
| **Domain** | 4.5×9.0×3.036 (LX×LY×LZ) | H_HILL=1.0 |
| **GPUs** | 8 (jp=8) | 流向分割 |
| **GAMMA** | 3.5 | Vinokur tanh 壁面拉伸參數 |
| **ALPHA** | 0.5 | 拉伸偏移參數 |
| **CFL** | 0.5 | 時間步控制 |
| **Uref** | 0.015 | 參考速度 (bulk velocity) |
| **COLLISION_MODE** | 1 (MRT) | 多重鬆弛時間碰撞 |
| **USE_GUO_FORCING** | 1 | Guo 強迫項 |
| **USE_WENO7** | 0 | 純居中 Lagrange-7 插值 |
| **WALL_GRAD_ORDER** | 4 | 壁面速度梯度有限差分階數 |
| **GHOST_EXTRAP_ORDER** | 3 | Ghost zone 外推階數 (Lagrange) |
| **SKIP_ALL_MASSCORR** | 0 | 質量修正啟用 |
| **INIT** | 0 | 冷啟動 (from rest) |
| **FTT_STATS_START** | 100.0 | 統計量開始累積 FTT |
| **CV_WINDOW_FTT** | 10.0 | CV 計算視窗 |

### Fröhlich 網格特性

- 壁面拉伸比: ~56.5× (GAMMA=3.5)
- 最小格距 (壁面): minSize ≈ 0.000416 (code units)
- 最大格距 (中心): ~0.0235

### 程式碼功能升級 (已完成)

1. **壁面速度梯度 (dudk) 階數開關** (`WALL_GRAD_ORDER`):
   - `#define WALL_GRAD_ORDER 2` → 2nd order: `(4u₁-u₂)/2h`
   - `#define WALL_GRAD_ORDER 4` → 4th order: `(48u₁-36u₂+16u₃-3u₄)/12h`
   - `#define WALL_GRAD_ORDER 6` → 6th order: `(360u₁-450u₂+400u₃-225u₄+72u₅-10u₆)/60h`
   - 實作位置: `gilbm/evolution_gilbm/1.algorithm1.h` (algorithm1_step1_GTS 及 _smem 兩處)
   - 係數已由 Codex 驗證正確，底壁/頂壁對稱

2. **Ghost zone 外推階數開關** (`GHOST_EXTRAP_ORDER`):
   - `#define GHOST_EXTRAP_ORDER 2` → 2nd order: 二次 Lagrange (3-point)
   - `#define GHOST_EXTRAP_ORDER 3` → 3rd order: 三次 Lagrange (4-point), 係數: `(-1/6, 4/6, -6/6, 4/6 → f(-1)=-f0/6+4f1/6-f2+4f3/6... )`
   - 實作位置: `gilbm/evolution_gilbm/1.algorithm1.h` 函式 `gilbm_ghost_zone_extrapolate()`

3. **Watcher 自適應 Re**: `watcher/hill_watcher.sh` 從 `variables.h` 動態讀取 Re，不再硬編碼

### 測試紀錄與失敗分析

#### Run #1: `dudk6_ghost2_g3.5_CFL0.5` (原始配置)
- **參數**: WALL_GRAD_ORDER=6, GHOST_EXTRAP_ORDER=2, CFL=0.5, GAMMA=3.5
- **結果**: ❌ **發散** — Ub 震盪 0.5~2.0 × Uref，Re% 在 ±53% 間震盪
- **表現**: 超過 30 FTT 震盪不收斂，Force 劇烈震盪
- **分析**: 6 階壁面梯度在 GAMMA=3.5 拉伸網格下需要跨越大格距比的 stencil，數值不穩定

#### Run #2: `dudk4_ghost3_g3.5_CFL0.5` (降階 + 升級 ghost)
- **參數**: WALL_GRAD_ORDER=4, GHOST_EXTRAP_ORDER=3, CFL=0.5, GAMMA=3.5
- **結果**: ❌ **未測試** (直接跳到 CFL=0.2)

#### Run #3: `dudk4_ghost3_g3.5_CFL0.2` (降 CFL)
- **參數**: WALL_GRAD_ORDER=4, GHOST_EXTRAP_ORDER=3, CFL=0.2, GAMMA=3.5
- **結果**: ❌ **發散** — Ub 單調 overshoot 到 +97% (1.968×Uref)
- **表現**: 曲線平滑無震盪，但 overshoot 幅度比 CFL=0.5 更大
- **最終指標** (FTT=0.26): Ub=1.968, Force F*=1.22 (接近零), max|Δρ|=1.2e-08 (密度偏差加速擴散)
- **分析**: CFL 降低使控制器響應變慢 (dt ∝ CFL)，overshoot 反而加劇。問題根源不在 CFL
- **結論**: **降低 CFL 不是解決方案，反而使不穩定加劇**

### 總結與結論

**GAMMA=3.5 拉伸在 129×257×129 網格上不穩定**:
- 壁面拉伸比 ~56.5× 造成近壁和中心格距差異過大
- 無論 CFL=0.5 (震盪型發散) 或 CFL=0.2 (overshoot 型發散) 都無法穩定
- WALL_GRAD_ORDER 4 和 6 皆不穩定
- Force 控制器無法在如此大的拉伸比下有效控制 Ub

**可能的下一步方向** (待使用者決定):
- 降低 GAMMA (減小拉伸比)
- 增加網格解析度 (更多格點來支撐高拉伸比)
- 對照 Re2800 穩定運行的參數配置
- 使用 Re2800 的 restart 場做為初始條件 (INIT≠0) 而非冷啟動

### 參考專案

- `/home/s8313697/3.Re2800_129x257x129_2nd/` — Re2800 版本，GAMMA=3.5 穩定運行，WALL_GRAD_ORDER=4, GHOST_EXTRAP_ORDER=3
- Re2800 穩定但 Re5600 不穩定 → 高 Re 數使得拉伸網格的數值邊界更加敏感

---

## SLURM Job Safety (MANDATORY)

This user runs multiple simulation projects on the same HPC cluster.
**Every project has its own jobs. Never touch another project's jobs.**

### Absolute prohibitions

1. **NEVER run bare `scancel <jobid>`** — always use `./run job-guard scancel <jobid>` which verifies the job belongs to this project before cancelling.
2. **NEVER run `scontrol update/hold/release/requeue/suspend/resume`** on any job.
3. **NEVER infer a jobid from `squeue` output and cancel it** — a job visible in `squeue` may belong to a different project.
4. **NEVER run `scancel -u $USER`** or any batch-cancel command — this would kill ALL of the user's jobs across all projects.

### Allowed SLURM operations

- `squeue` / `sinfo` / `sacct` — read-only queries, always safe
- `scontrol show` / `scontrol listpids` — read-only, safe
- `./run job-guard scancel <jobid>` — project-verified cancel, safe
- `./run job-guard stop-chain` — creates STOP_CHAIN sentinel for this project only
- `sbatch` via `./run` or `./run build` — project submission workflow

### Enforcement

A PreToolUse hook (`chain_code/tools/claude_slurm_guard.sh`) automatically blocks bare `scancel` and modifying `scontrol` commands. If the hook blocks you, do NOT attempt to bypass it.
