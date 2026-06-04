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

- Branch: Edit7_10595SNS
- Remote: origin (GitHub)
- Language: commit messages should be in Traditional Chinese (繁體中文)
- This is a CFD (Computational Fluid Dynamics) LBM simulation project running on HPC clusters (H200/GB200).

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

## 禁止跨專案操作 (MANDATORY — Cross-Project Isolation for this project)

**本專案 = `5.Re10595/Edit8_NewInterpolation`。所有操作只能針對「本專案」。**
本使用者在同一個 `5.Re10595/` 樹下有多個並存的子專案
（`Edit1_non-clamp` … `Edit6_5600DNS`、`Edit7_10595SNS`、`Edit8_ITBopt_bench`、`Edit8_NewInterpolation` …），
每個子專案有自己的 job / dispatcher / watcher / crontab / checkpoint。

### 絕對禁止 (FORBIDDEN — 對任何「非本專案」的子專案)

1. **絕不** 取消、修改、暫停、requeue 其他子專案的 SLURM job
   （沿用上節 SLURM 規則；`scancel` 只能用 `./run job-guard scancel`，且只動本專案記錄的 job）。
2. **絕不** kill / 重啟其他子專案的 dispatcher、watcher、`hill_watcher.sh`、
   `nan_monitor.py`、a.out/mpirun，或其 codex/claude session。
3. **絕不** 在 crontab 增加、修改指向「非本專案」的行；
   本專案安裝的 keepalive cron **只能** 指向 `Edit8_NewInterpolation/chain_code/tools/daemon_keepalive.sh`。
4. **絕不** `rm`/`mv`/`>`/`touch STOP_CHAIN` 到其他子專案的 `restart/`、`live/`、checkpoint、原始碼。
5. **唯讀允許**：`cat`/`head`/`tail`/`grep`/`diff`、`squeue`/`sacct`/`scontrol show`、`cp 其他專案/檔 ./` 是允許的（對照參考用）。

### Edit7 已退役 (RETIRED — 2026-06-04)

- `Edit7_10595SNS` 已退役，工作改在 **Edit8_NewInterpolation** 進行（warm-start 自 Edit7 checkpoint）。
- **不再對 Edit7 做任何操作**（讀取對照除外）。Edit7 仍可能有使用者自己的 IDE / claude / codex / tmux session 在跑 → 一律不碰。
- 一次性清理（已於 2026-06-04 執行）：移除了 crontab 中殘留、每 5 分鐘重生 Edit7 `live/`、`restart/` 的
  keepalive 行 `*/5 * * * * .../Edit7_10595SNS/chain_code/tools/daemon_keepalive.sh`
  （備份於 `~/.claude/crontab_backup_20260604_161745.txt`，僅保留 `2.Re1400` 監控行）。

### `live/` 與 `restart/` 為何會「持續重生」(成因備忘)

> **[2026-06-04 已根除自我重生引擎]** 依使用者「完全根除生成源頭」要求，已永久停用
> `chain_code/dispatcher_start.sh` 的 `*/5min` keepalive cron 自動安裝（改為主動清除本專案殘留的
> cron 行，只比對本專案 `daemon_keepalive.sh` 路徑、`grep -vF` 保留其他所有行含別專案）。
> 現況：**0 keepalive cron、0 daemon、0 job**，`live/`+`restart/` 不再自我重生。下方「成因備忘」
> 保留為歷史說明；`mkdir -p restart/` 仍會在**手動執行 chain 腳本時**按需重建（checkpoint 必需，
> 屬正常行為，非自我重生）。**代價（使用者已接受）：dispatcher 不再被 cron auto-heal，死了需
> 手動 `./run dispatcher start` 重啟。**

- 兩者皆為執行期產物，已被 `.gitignore` 忽略；幾乎每個 `chain_code/*.sh` 啟動時都會 `mkdir -p restart/`，watcher 會建 `live/`。
- **重生引擎 = keepalive cron**：`chain_code/dispatcher_start.sh` 在 `./run dispatcher start` 時會
  **自動裝一條 `*/5 * * * *` 的 keepalive cron**（指向本專案 `daemon_keepalive.sh`），
  之後每 5 分鐘把 watcher（→`live/`）與 dispatcher（INTENT 在時 →`restart/`）救活。
- **已知漏洞**：`chain_code/dispatcher_stop.sh` 只移除 `DISPATCHER_INTENT`/heartbeat，
  **不移除那條 cron**；且 keepalive 的 watcher 分支只看 `restart/STOP_CHAIN`、不看 `STOP_DISPATCHER`。
  → 因此單純 `dispatcher stop` 後，`live/` 仍會每 5 分鐘被 cron 重生。
- **要完全停止本專案的重生**：建 `restart/STOP_CHAIN`（`./run job-guard stop-chain`）讓 keepalive 整個退出，
  **並** 手動移除本專案那條 keepalive cron（`crontab -l | grep -vF '<本專案>/chain_code/tools/daemon_keepalive.sh' | crontab -`）。

## Periodic Hill testing shortcut (triggered by user command)

When the user types **`periodichill-testing`** (any spacing/case), execute the
full clean-cold-start + monitoring sequence below. The intent is to verify the
solver end-to-end at the parameters in `variables.h` with the fewest manual
steps. **Do NOT pre-run any grid generator** — main is responsible for calling
`J_Frohlich/grid_zeta_tool.py` itself when the parameter-matched grid is
missing.

### Sequence

1. **Pre-flight check** — `git status --short`, confirm working tree is in a
   sensible state. `ls a.out` to know if rebuild is needed.
2. **Rebuild + cold-start submit** (auto-confirm the `--force-cold` prompt):
   ```bash
   rm -f .run.lock
   echo y | ./run --rebuild --force-cold --no-queue-check
   ```
   If the build wrapper produces only `a.out` but no `a.out.H200` (e.g. the
   alias was wiped by a prior reset), copy it: `cp a.out a.out.H200`.
3. **Verify job** — `squeue -u $USER` confirms exactly one project job is in
   the queue; `scontrol show job <id> | grep WorkDir` confirms it points at
   this project root.
4. **Start dispatcher** (cross-partition auto-resubmit daemon):
   ```bash
   ./run dispatcher start
   ```
5. **Launch `watcher/hill_watcher.sh`** (the project's one and only watcher):
   ```bash
   nohup bash watcher/hill_watcher.sh > /dev/null 2>&1 &
   ```
   It writes a PID file at `live/watcher.pid`, polls every 30s, and:
   - Always runs `result/4.Ma_U_Time.py --Re <Re>` against the latest stable
     VTK; copies the produced `monitor_convergence_Re*.{png,pdf}` to
     `live/monitor_latest.{png,pdf}`.
   - When FTT >= FTT_STATS_START + CV_WINDOW_FTT (G2 gate, CV window full),
     additionally runs:
     - `result/2.Benchmark.py --Re <Re> --no-ask-scales --no-ask-density` →
       copies `fig_mean_u.png`, `fig_uu.png`, etc. to `live/`.
     - `result/10.tau_wall_benchmark.py --Re <Re> --auto` →
       copies `tau_wall_signed_Re<Re>_cf.png` and
       `tau_wall_signed_Re<Re>_cp.png` to `live/`.
   - Emits NaN/divergence alerts based on the slurm tail.
   Open `live/monitor_latest.png` to view the single rolling status image.
6. **Arm one Monitor watcher** (Monitor tool, not bare bash):
   - **Status snapshot** every 60s: queue state + latest `Step ... Re=...
     Ma_max=...` line + tail of `checkrho.dat`.
   Tail `live/watcher.log` separately if you want CONV/BENCH event signals.

**DO NOT** start the animation pipeline (`animation/pipeline.py`,
`animation/png_frames/`, `animation/flow_*.mp4`). The watcher in this project
is convergence + benchmark plots only — no per-VTK rendering, no MP4 encoding.

## Short-video snapshot shortcut (triggered by user command)

When the user types **`periodicHill-shortvedio`** (or `periodichill-shortvideo`,
the user's typo is canonical), build a **bounded** ~3-second animation from
the upcoming VTKs without disturbing the solver. The default budget is
**100 frames @ 33 fps ≈ 3.0 s**. Optional integer arg overrides the count
(e.g. `periodicHill-shortvedio 130` → ~3.9 s). Hard upper cap is 200 frames
to prevent runaway login-node load.

### Sequence

1. **Pre-flight**:
   - Confirm `result/velocity_merged_*.vtk` is being produced (solver running).
   - `mkdir -p animation/png_frames`.
   - Pick a tag: `T=$(date +%Y%m%d_%H%M%S)`. Final outputs go to
     `animation/short_${T}_cont.mp4` / `animation/short_${T}_RD.mp4`.
2. **Bounded snapshot loop** — implement as a `Monitor` task with a counter
   and an explicit `break` when N frames are rendered:
   - Track `RENDERED=0`. Newest-first scan of `result/velocity_merged_*.vtk`,
     skip steps already rendered. For each candidate: wait 5s for size
     stability, re-verify the file still exists (rolling retention may have
     purged it), then call:
     ```bash
     python3 animation/pipeline.py "$vtk" "$step" --width 1920 --fps 33 \
         --codec libx264 --pix-fmt yuv420p
     ```
     If `rc==0`, increment `RENDERED`. Append a one-line status update.
   - When `RENDERED >= N`, **stop the monitor** (`TaskStop`) — do not loop
     forever.
3. **Final rename**:
   ```bash
   mv animation/flow_cont.mp4 animation/short_${T}_cont.mp4
   mv animation/flow_RD.mp4   animation/short_${T}_RD.mp4
   ```
   Also surface the final MP4 path back to the user.

### Constraints (the reason for the bound)

- Each frame render takes ~7–15 s on login node. 100 frames ≈ 12–25 min wall
  time — tolerable as a one-shot task. >200 frames is rejected.
- Login-node rendering is decoupled from compute-node simulation, so this
  shortcut **does not** stretch FTT progress directly. The bound exists so
  one accidental long video doesn't tie up login CPU/IO during the entire
  simulation lifetime.
- VTK rolling retention keeps only the newest ~10 files. The shortcut
  therefore captures the **next** N VTKs as they appear, NOT past ones —
  and uses a 5 s stat-stable check so half-written VTKs are skipped.
- `live/monitor_latest.png` and `hill_watcher.sh` keep running normally.
  This shortcut is **additive**, not a replacement for the watcher.

### Pairs with `periodichill-testing`

Trigger `periodicHill-shortvedio` only after the cold-start has been running
long enough that VTKs are actively being produced (typically a few minutes
post-submit). Aborting mid-shortcut: `TaskStop` the monitor; the partial
MP4 in `animation/flow_*.mp4` will reflect whatever frames were rendered.
7. **Trust main's auto-grid path** — when the solver enters and finds no
   `J_Frohlich/adaptive_<stem>_I<NY>_J<NZ>_g<GAMMA>_a<ALPHA>.dat`, it will
   invoke `python3 J_Frohlich/grid_zeta_tool.py --auto` itself. Do not race
   ahead of it.

### What this shortcut does NOT do

- Does not modify `variables.h` (whatever `Re`, `Uref`, `NY`, `NZ`, `GAMMA`,
  `ALPHA` are set is what gets tested).
- Does not touch `phase1_generategrid/` or `phase2_generatecheckpoint/` — those
  are isolated by regulation; main never reads/writes them.
- Does not auto-cancel jobs, auto-stop the chain, or auto-clean any data on
  finish — leave the chain running so the dispatcher can keep it alive.

### Cleanup variant: `periodichill-testing reset`

When the user types **`periodichill-testing reset`**, perform a hard reset
before the sequence above:
1. Cancel any active project jobs via `./run job-guard scancel <id>`.
2. `./run dispatcher stop` if a daemon is running.
3. `TaskStop` any active Monitor watchers from the prior session.
4. Stop hill_watcher: `pkill -F live/watcher.pid 2>/dev/null; rm -f live/watcher.pid`.
5. Delete simulation outputs only — keep tracked code:
   - `restart/ statistics/ live/`
   - `slurm_*.log slurm_*.err nan_monitor_log.txt`
   - `checkrho.dat Ustar_Force_record.dat timing_log.dat gilbm_metrics_full.dat meshYZ.DAT`
   - `a.out a.out.H200 a.out.GB200`
   - `J_Frohlich/adaptive_*.dat J_Frohlich/grid_data_*.txt J_Frohlich/compare_auto_*.png`
6. **Do NOT** `rm -rf result/` blindly — `result/` contains tracked Python
   scripts and DNS benchmark data. Only clean its `*.vtk`, `*.bin`, and
   stale convergence/benchmark plots:
   ```bash
   rm -f result/*.vtk result/*.bin
   rm -f result/monitor_convergence_*.png result/monitor_convergence_*.pdf
   rm -f result/benchmark_*.png result/benchmark_*.pdf
   rm -f result/tau_wall_signed_*.png result/tau_wall_signed_*.pdf
   ```
7. Then proceed with the standard sequence (steps 1–7 above).

## 全場 GIF 動畫設定 (Full-field GIF animation spec)

當需要產生**全場 (full-field) 動畫**時(有別於上面 `periodicHill-shortvedio`
的 fast_slice X=mid 薄板短 MP4),一律套用以下四項設定。這是全場動畫的權威
規格,不覆寫、不取代既有的短影片/MP4 流程。

### 1. 每幀 0.05 秒 → GIF 20 fps

- 播放速率固定為 **20 fps**(每一幀顯示 **0.05 秒**),刻意比現行 MP4 的
  33 fps 放慢,讓全場演化看得清楚。
- 對應參數:`--fps 20`(現行 `animation/pipeline.py` / `video_encode_mp4.py`
  預設為 33,全場 GIF 必須顯式改成 20)。

### 2. 全場、每一幀都要完整

- 渲染**完整全場**,不走 fast_slice X=mid 薄板:`SLICE_ONLY = False`、
  **不**傳 `--slice-only`(`render_frame.py:26`)。完整讀整顆
  `velocity_merged_*.vtk`,跑完整渲染路徑(含需要完整 volume 的 Path D
  Q-criterion,`render_frame.py:866`)。
- **每一幀都要產生、都要完整**:不設幀數上限(無 `periodicHill-shortvedio`
  的 100/200 幀 budget),每一個產出的 VTK 都渲染成一幀,不抽樣、不跳幀,
  半寫入檔以 5s stat-stable 檢查跳過後**等下一輪補上**而非丟棄。

### 3. 統一 max/min 色階(固定硬編 vmin/vmax)— 消除閃爍

- **問題根源**:`render_frame.py:654-661` 目前用
  `infoA.GetComponentRange()` 逐幀從該幀資料的 min/max 重算 `u_streamwise`
  色階範圍 (`lo_A`,`hi_A`),每幀範圍不同 → 顏色逐幀漂移 → 影片「一閃一閃」。
- **規格**:全場 GIF 的所有純量場色階一律用**固定數值範圍(全片硬編一組
  vmin/vmax)**,不做逐幀自動重縮。做法比照現行已固定的
  `W_RANGE = [-0.02, 0.02]`(`render_frame.py:121`):為 `u_streamwise`
  (及其他著色場)挑一組涵蓋全程的固定 `[vmin, vmax]`,所有幀共用同一範圍與
  同一 LUT,確保色彩對應的物理量值跨幀一致。

### 4. 輸出 GIF

- 最終輸出為 **GIF**(現行流程輸出 MP4)。以固定色階、完整全場、20 fps
  的 PNG 序列組成 GIF。

### 其餘皆不改

- **不動**既有 `periodicHill-shortvedio` 短影片快捷、`animation/pipeline.py`
  與 `video_encode_mp4.py` 的 MP4 預設(width/codec/pix-fmt/33fps)、watcher、
  以及任何模擬程式碼或 `variables.h`。
- 本節僅新增「全場 GIF」這一組設定,屬**追加**性質,不修改上述任何既有行為。

## Quick clean shortcut (triggered by user command)

When the user types **`lbm-clean`**, delete the following simulation-generated
files from the **current project directory only**. This removes build artifacts,
logs, mesh files, and heavy output — but preserves all tracked source code,
scripts, and DNS benchmark data.

```bash
# Logs and lock
rm -f slurm_*.log slurm_*.err .run.lock nan_monitor_log.txt

# Build artifacts
rm -f a.out a.out.H200

# Mesh and metrics files
rm -f meshX.DAT meshYZ.DAT gilbm_metrics_full.dat

# Heavy result outputs (keep tracked .py scripts and DNS data)
rm -f result/*.bin result/*_Final.vtk

# Statistics directory
rm -rf statistics/
```

**Safety notes:**
- Does NOT touch `restart/`, `live/`, `checkrho.dat`, `Ustar_Force_record.dat`,
  `timing_log.dat`, or `gilbm_metrics_full.dat` — use `periodichill-testing reset`
  for a full reset.
- Does NOT touch `result/*.py`, `result/*.dat` (DNS benchmark), or
  `result/*.png`/`result/*.pdf` — only `*.bin` and `*_Final.vtk`.
- Does NOT cancel any running jobs or stop the dispatcher/watcher.
- Report what was deleted (file count / size freed) after execution.

## GILBM 效能優化架構 — MRT 預計算 + eta 權重共享 + Forcing 開關

本專案已完成三項 host-side 預計算優化，所有表格在 `main.cu` 初始化階段
計算一次，上傳至 `__constant__` memory，kernel 端只做 table-lookup + multiply-add。

### 1. MRT 非平衡投影預計算 (K 矩陣)

**原理：** 將 MRT 碰撞 `f* = f − M⁻¹·S·M·(f−f_eq)` 中的三重矩陣乘法
預合成為一張 `K[19][19] = M⁻¹·S·M`，kernel 只做 19×19 矩陣-向量乘法。

**檔案：**
| 位置 | 說明 |
|------|------|
| `mrt_projection_host.h` : `BuildMrtProjectionTablesHost()` | Host 端建 K 矩陣與 Forcing 投影表 |
| `mrt_projection_host.h` : `VerifyMrtProjectionHost()` | 36 組樣本驗證 Legacy vs Projection，1e-12 容差 |
| `main.cu:650-710` | 呼叫 Build → Verify → `cudaMemcpyToSymbol` |
| `0.shared_code.h:48` | `__constant__ double GILBM_MRT_K[19][19]` |
| `0.collision.h:61-66` | Kernel: `relax += GILBM_MRT_K[a][b] * fneq[b]` |

**驗證項目（啟動時自動檢查，任一 > 1e-12 即 MPI_Abort）：**
- `Mi*M` 恆等矩陣誤差
- `M*feq` vs 解析平衡矩
- 守恆矩不受 K 影響: `M_conserved * K ≈ 0`
- Legacy vs Projection 碰撞絕對誤差

### 2. Eta 方向 Lagrange 插值權重共享預計算

**原理：** GILBM eta 方向的 7 點 Lagrange 插值權重只依賴 `sign(e_x)`
（+1 或 −1），不依賴完整速度索引 q。預計算 2 組權重（正/負方向），
kernel 按 `e_x` 符號查表，避免每個 q 重複計算 Lagrange 係數。

**檔案：**
| 位置 | 說明 |
|------|------|
| `gilbm/precompute.h:165` : `PrecomputeGILBM_EtaSharedWeights()` | Host 端計算 2×7 權重表 |
| `gilbm/precompute.h:180` : `VerifyGILBM_EtaSharedWeights()` | 驗證係數差 + 插值差 < 1e-12 |
| `main.cu:617-641` | 呼叫 Precompute → Verify → `cudaMemcpyToSymbol` |
| `0.shared_code.h:55` | `__constant__ double GILBM_L_eta_shared[2][7]` |

### 3. 新增 Hermite 1st-order 在 MRT 預計算下的開關

**開關：** `variables.h` 中 `FORCE_HERMITE_ORDER`（編譯期，只允許 1 或 2）

**階數定義：**

| 值 | Guo forcing 公式 | 說明 |
|----|------------------|------|
| `1` | `F_i = w_i · 3 · c_y · Force` | 一階 Hermite — 無速度項，質量守恆 ✓ |
| `2` | `F_i = w_i · Force · [3(c_y−v) + 9(c·u)c_y]` | 二階 Hermite — 完整 Guo (2002) |

**修改範圍 (4 檔案，`#if FORCE_HERMITE_ORDER >= 2` 控制)：**

| 檔案 | 修改內容 |
|------|---------|
| `variables.h:262-266` | `#define FORCE_HERMITE_ORDER 1` + `#error` 範圍檢查 |
| `mrt_projection_host.h` | `BuildMrtProjectionTablesHost`: 一階時基底 `Fu=Fv=Fw=0`，只留 `F0=w_q·3·cy` |
| | `LegacyMrtCollisionHost`: 一階時 `Fq = w_q·force·3·cy` |
| | `VerifyMrtProjectionHost`: 直接力/分裂力/守恆矩驗證同步切換 |
| `0.collision.h:113` | BGK Guo 路徑: 一階時 `F_q = w_q·Force·3·cy` |
| `main.cu:713` | 啟動時印出 `GILBM: FORCE_HERMITE_ORDER = N`（MRT/BGK 共用） |

**MRT kernel 不需修改：** GPU kernel 始終讀取 4 張 `__constant__` 表
`(Fproj, Fproj_u, Fproj_v, Fproj_w)`。一階時後三張為零陣列，等效於
`f += dt · Force · Fproj[a]`。

### 與參考專案 Duct 的對比

| 項目 | 本專案 (D3Q19) | Duct 參考專案 (D3Q27) |
|------|---------------|----------------------|
| 路徑 | `Edit7_10595SNS` | `/home/s8313697/D3Q27_PeriodicHill/Edit2_PeriodicHillDuct` |
| K 矩陣 | `GILBM_MRT_K[19][19]` | `GILBM_MRT_K[27][27]` |
| Forcing 投影 | 4 表 (F0+Fu+Fv+Fw, 一階時後三為零) | 1 表 `GILBM_MRT_Fproj[27]` |
| Forcing 基底 | `F0[q] = w_q·3·cy` (兩專案相同) | `F_unit[q] = D3Q27_W[q]*3.0*D3Q27_ey[q]` |
| Hermite 開關 | `FORCE_HERMITE_ORDER` (1 或 2) | 固定一階 |
| Eta 共享權重 | `GILBM_L_eta_shared[2][7]` | 同架構 |
| 碰撞 kernel | `0.collision.h:61-73` | `0.collision.h:60-72` |
| BGK Guo | 有 `FORCE_HERMITE_ORDER` 條件 | 完整二階（BGK 路徑未簡化） |

### 常見錯誤提醒

一階 Hermite 是 `w_q · 3 · c_y`（純格子速度方向），**不是** `w_q · 3 · (c_y − v)`。
`−v` 項屬於二階展開的一部分（用於消除 Fv 質量矩以保證 Galilean invariance）。
混淆此兩者會導致 `Fv` 基底的零階矩 = −3，造成不可逆密度漂移。

## 臨時鎖定 / 還原自由跳轉 (Temporary lock toggle — partition && jp)

dispatcher 預設**自由跳轉**：每輪界即時掃描 `{128,64,32,16} × {normal,4nodes,dev}` 矩陣，
超 cap/inuse 的組合「試過 `--test-only` 再警告跳過」，在可行組合中選最佳（高 jp 優先 + 抓空閒、不看 walltime）。

### 臨時開關：`restart/LOCK_COMBO` sentinel

當 NCHC 政策 / 帳號飽和等情況需要**暫時固定**到某 `jp|partition` 時，用此 sentinel 鎖定（繞過矩陣評估）：

- **檔案**：`restart/LOCK_COMBO`，內容格式 `<jp> <ARCH@partition>`（例：`16 H200@dev`）。
- **作用**：dispatcher 的 `pick_jp_and_partition`（凍結 jp = 當前值）+ `pick_cluster`（鎖 partition）都檢查此檔，
  存在時直接回傳鎖定組合、**不做矩陣評估 / 不自動跳轉**。jp 凍結在「當前值」（`KEEP cur`，不重切、無 repartition）。
- **設定（鎖定）**：`echo "16 H200@dev" > restart/LOCK_COMBO`，再停舊 dispatcher（cwd 驗證後 SIGTERM 特定 PID）+ `./run dispatcher start` 讓 daemon 載入新狀態。
- **目前狀態（2026-06-03 設定）**：鎖定 `16 H200@dev`（因帳號 mst114348 被別用戶占滿 normal 16/16、4nodes 32/32，僅 dev 有空檔）。

### 快捷指令：`還原回自由跳轉`（或 `還原回自由跳轉 partition&&jp`）

When the user types **`還原回自由跳轉`**（或含 `partition&&jp`），還原 dispatcher 的**自由跳轉**機制：

1. `rm -f restart/LOCK_COMBO`（移除臨時鎖）。
2. 重啟 dispatcher 讓 daemon 回到矩陣評估：停當前 dispatcher（**cwd 驗證、SIGTERM 特定 PID**，絕不 `pkill -f`）→ `./run dispatcher start`。
3. 驗證：`DISPATCHER_SELFTEST=1 bash chain_code/submit_dispatcher.sh` 應回到矩陣評估（不再印 `[LOCK]`），在可行組合中選最佳。
4. 回報：自由跳轉已還原，dispatcher 重新依容量自動切換 `partition × jp`。

**守門**：只操作當前專案（cwd=Edit8）；遵守跨專案 Job 隔離；`scancel` 只用 `./run job-guard`；不在跑著的原目錄重編 a.out。
