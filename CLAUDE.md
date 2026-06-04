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

## 跳轉機臨時鎖 / 還原回自由跳轉 partition&&jp（NCHC 政策用臨時開關）

正常情況下 dispatcher 會「自由跳轉」：依即時空閒自動切 `jp`（NCHC 政策自由切換集 `{16,32,64}`）
與對應 H200 partition（`16gpus@16` / `32gpus@32` / `64gpus@64`；計畫編號 `MST114348`）。當 NCHC 政策需要把規模**暫時固定**時，
用下方「單一旗標」臨時鎖死，政策結束再一鍵還原。**鎖只影響排程選擇，不碰流場/checkpoint。**

### 臨時鎖定（目前狀態：鎖定 `jp=16 | 16gpus`，嚴格鎖）

單一哨兵檔 **`restart/LOCK_JP_PARTITION`** 同時鎖 jp 與 partition：

| 機制 | 檔案 / 行為 | 效果 |
|------|------------|------|
| 凍 jp | `submit_dispatcher.sh` `pick_jp_and_partition`：旗標在 → `locked=1` → `KEEP` 現 jp | jp 停在當前值（=16），不自動升降 |
| 鎖 partition | `submit_dispatcher.sh` `pick_cluster`：旗標在 + `restart/h200_partition` pin 的分區**在可投清單中**（已過靜態 cap + 即時 headroom）→ 直接回 pin，跳過自由 ETA 選擇 | partition 釘在 pin（=16gpus） |
| 守門（**嚴格鎖** 2026-06-04） | pin 此刻不可投（超 cap/帳號占滿/非 up）→ **已試過 `sbatch --test-only` 確認後**，記警告 **跳過本輪、維持鎖定（不落回別分區、不強投）**，下輪再試 | 不亂跳別 partition、不造成永久 PENDING；連續無容量達 `NOCAPACITY_LIMIT`（≈4h）才觸發 `STOP_NOCAPACITY` backstop |

> 嚴格鎖即「限定 16gpus、遇上限跳過+警告、不連試都沒試、也不亂跳」。對映 `pick_cluster` 的
> `[LOCK_JP_PARTITION][strict]` 路徑。dev cap=4 < jp=16 永不可投，故 pin **不可**選 dev。
> 例外：未設 `h200_partition` pin（誤配置）→ 落回自由選擇（安全保底）。

**手動上鎖步驟**（已套用，供日後重做）：
```bash
echo 16gpus > restart/h200_partition     # pin 目標 partition（cap=16 剛好容 jp=16；claude_changepartition 亦可）
touch restart/LOCK_JP_PARTITION          # 上鎖（jp 凍在當前值 + partition 釘 pin）
rm -f restart/STOP_JPSWITCH              # LOCK_JP_PARTITION 已含凍 jp，STOP_JPSWITCH 冗餘 → 清掉避免混淆
./run dispatcher stop && sleep 38 && rm -f STOP_DISPATCHER && ./run dispatcher start  # 重啟載入鎖定碼
DISPATCHER_SELFTEST=1 bash chain_code/submit_dispatcher.sh   # 應印 ">>> 決策結果: KEEP <jp> H200@16gpus"
```
> 旗標是**執行期讀取**：碼一旦載入（重啟過一次），之後 `touch`/`rm` 旗標即時生效，**無需再重啟**。

**鎖定狀態靜態心跳（隨時檢驗組合是否生效）**：
- 腳本：`chain_code/tools/jp_lock_selfcheck.sh`（純靜態、READ-ONLY，不投遞）。靜態驗 A.鎖定哨兵
  B.候選機制限定 C.超上限/嚴格鎖路徑 D.live 可行性；輸出 `live/jp_lock_heartbeat.log`（每行一筆）、
  `live/jp_lock_status`（`OK`/`OK(warn)`/`DRIFT`）、`live/jp_lock_DRIFT.alert`（漂移時生成、恢復自清）。
  退出碼 0=PASS、1=DRIFT。
- 持久 loop 心跳：login-node crontab `*/10`（與其他專案 cron 並存）跑此 selfcheck；
  cron stdout → `$HOME/jp_lock_cron.log`。手動跑：`bash chain_code/tools/jp_lock_selfcheck.sh`。

### 還原回自由跳轉 partition&&jp（政策結束時執行）

```bash
rm -f restart/LOCK_JP_PARTITION          # 解鎖：jp 與 partition 都恢復自由跳轉
rm -f restart/STOP_JPSWITCH              # （保險）若另有單獨凍 jp 的旗標一併清掉
# （可選）放掉 partition pin，讓直投/jobscript 自投也完全自由：
#   rm -f restart/h200_partition         # 清 pin（pick_cluster 解鎖後本就忽略 pin，但直投路徑會用到）
# 驗證已還原（應印自由選擇而非鎖定）：
DISPATCHER_SELFTEST=1 bash chain_code/submit_dispatcher.sh   # 決策應依即時空閒自由選 jp+partition
```
解鎖**不需重啟** dispatcher（旗標執行期讀取）；下一輪界起即恢復「抓空閒、偏高 jp」的自由跳轉。
JP 候選與自由跳轉邏輯（score=`jp*1000−wait−sw`、即時 headroom 過濾、pick_cluster 最早 ETA）
皆原封保留，解鎖即生效。

## `a.out` 生命週期：watcher / dispatcher 的開啟與死亡機制（2026-06-04）

### 成因備忘（為何 `live/`+`restart/` 會「持續自我重生」）
`./run dispatcher start` 會自動安裝一條 **`*/5 * * * *` crontab → `chain_code/tools/daemon_keepalive.sh`**
（layer-3 watchdog，見 `dispatcher_start.sh:105-116`）。該引擎每 5 分鐘**救活** watcher（建 `live/`）
與 dispatcher（建 `restart/`）。**過去缺「死亡條件」** → 即使刪了 `live/`、殺了 watcher，5 分內又被
救回；跨節點 watcher 還得手動 `ssh` 去殺。這就是先前 `live/` 一直回來的根因。

### 解法：用 `a.out` 當「存活信物（liveness token）」
| 階段 | 條件 | 行為 |
|------|------|------|
| **開啟** | 第一次 `./run dispatcher start` | 裝 `*/5` keepalive cron + 起 dispatcher；keepalive 隨後維持 watcher |
| **維持** | keepalive `*/5` **且 `a.out` 存在** | 死掉的 watcher/dispatcher → 救活（原行為） |
| **死亡** | **`a.out` 不存在**（build artifact 被刪＝專案拆除） | 三路自動死亡（見下），且**不再被救活** |

### 三道死亡機制（`a.out` 一刪即全動，免 `ssh`、免手動清 cron）
| 機制 | 檔案 | 行為（`a.out` 不在時） |
|------|------|----------------------|
| keepalive 死亡閘 | `chain_code/tools/daemon_keepalive.sh`（STOP_CHAIN 閘之後） | 殺本機本專案 watcher/dispatcher（驗 cwd）＋**自我移除本專案 `*/5` cron**（`grep -vF "$(basename PROJECT_ROOT)/…/daemon_keepalive.sh"`，保留別專案）＋ exit |
| watcher 自死 | `watcher/hill_watcher.sh`（`while :` 迴圈頂） | `[ ! -e $PROJECT_DIR/a.out ]` 或 STOP_CHAIN → 清自身 heartbeat/pid → `exit 0`（**跨節點也能自己停**） |
| dispatcher 自死 | `chain_code/submit_dispatcher.sh`（主迴圈頂，Stop 條件 0） | `[ ! -e a.out ]` → `break` 收工 |

### ⚠️ 啟動順序（新機制的必然結果）
`a.out` 既是存活信物，**必須先存在才能啟動 daemon**，否則 keepalive 第一輪即判死：
```bash
./run --rebuild         # 1) 先產生 a.out（存活信物就位）
./run dispatcher start  # 2) 再啟動 → 裝 cron + keepalive 維持 watcher/dispatcher
```

### 徹底停機（取代「逐一 ssh 殺、手動刪 cron」）
```bash
rm -f a.out a.out.H200 a.out.GB200   # 刪存活信物 → 一個週期內 watcher/dispatcher 全自死、keepalive 自移除 cron
```
（`lbm-clean` / `periodichill-testing reset` 本來就會刪 `a.out*`，故照常清理即自動觸發全體死亡。）

### 守門/相容
- **跨專案安全**：死亡閘殺進程前驗 `/proc/PID/cwd == PROJECT_ROOT`；自移除 cron 以 `basename PROJECT_ROOT`
  scope，**只動本專案那行**，絕不碰別專案（含 Edit8）的 keepalive / cron。
- STOP_CHAIN 仍為「使用者刻意停」獨立守門（keepalive 與 watcher 皆尊重）；`a.out` 死亡閘是
  「專案拆除」的額外、更徹底的死亡訊號，兩者並存。
