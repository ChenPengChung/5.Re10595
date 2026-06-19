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

## Resume-status check shortcut (triggered by user command)

When the user types **`claude_check`** (= 「幫我檢驗目前續跑狀態」), run a **read-only**
verification of the current chain / resume status and report a compact table.
Do NOT submit, cancel, rebuild, or mutate any job / chain / checkpoint state.

### Steps

1. **Chain head + job state** — `JID=$(cat restart/chain_jobid)`; get state with
   **`sacct -j $JID -o JobID,State,ExitCode,Start,Elapsed,NodeList`** (authoritative).
   ⚠️ `squeue -u $USER` may NOT list h200/gb200 jobs (NCHC cross-cluster
   federation display quirk) — **trust `sacct` for job state**, not squeue.
   Also: `scontrol show job $JID | grep -oE 'Partition=[^ ]+|Account=[^ ]+|TimeLimit=[^ ]+|WorkDir=[^ ]+'`
   (verify WorkDir == this project; expect partition h200/gb200, walltime 4-00:00:00,
   account mst114348).
2. **Solver progress** — `slurm_$JID.log`: tail the latest
   `[Step N | FTT=.. Re=.. Ma_max=.. Error=..]` line + latest `[CONV] ...` line
   + the per-2000-step MLUPS block if present. Confirm Step / FTT are advancing.
3. **Clean-restart sanity** — confirm `--restart=` (not `--cold`), `[G6] Schema OK`,
   `Statistics loaded ... accu_count=`. Alert on any
   `FATAL|MPI_Abort|mismatch|cannot load|NaN|DIVERG|--cold`.
4. **Stats accumulation** — confirm `accu_count` is advancing vs the checkpoint
   metadata → statistics preserved and still growing (not reset).
5. **Daemons** — dispatcher (`restart/dispatcher.pid` + `kill -0`) and watcher
   (`live/watcher.pid`) alive; tail `restart/dispatcher.log` + `live/watcher.log`.
6. **Health + checkpoint** — `checkrho.dat` tail (density ~1.0, last col flag 0),
   `readlink restart/checkpoint/latest` + its `accu_count`.
7. **GB200 switch readiness** — `ls a.out.GB200` (cross-cluster free switching is
   active only when this binary exists; see [GB200 Switch Pending] memory).

Report concisely. This shortcut is **read-only** — it never changes job/chain state.

## Daemon health monitoring — two routes (Route A = Claude / Route B = systemd timer)

監控 watcher + dispatcher 存活 + partition×jp 切換機制是否正確,死亡則維修+推送,沒問題則回報。
**這套行為可被任何 session 重現**(不必重貼長 prompt):

### Route A — `/edit6-monitor`(Claude 在 session 內;能修程式碼)
- 任何 session 打 **`/edit6-monitor`** 即執行一輪完整巡檢(定義於
  `.claude/commands/edit6-monitor.md`,隨 git 散佈)。連續監控:**`/loop /edit6-monitor`**。
- 等價觸發詞 **`claude_monitor`**(= 同一行為;本檔已載入每個 session,故任何 session 都認得)。
- 行為:(1) 存活檢查(systemd is-active + `/proc/PID/cwd` 判本專案實例);(2) 死亡/crash-loop →
  journalctl 診斷 → 修對應程式碼 → `bash -n` → `systemctl --user restart` → **逐檔 commit + 獨立 push**
  (繁中、含問題點+解法、三大紀錄檔先 gzip、禁 `-A`、禁 `--force`);(3) 切換機制稽核
  (sacct job / chain_jobid↔squeue / `live/osc_check.sh` 震盪 / PENDING re-select / accu 連續);
  (4) 沒問題 → 回報「沒問題」表格。

### Route B — systemd watchdog timer(無 session 也運作;不修碼)
- `chain_code/health_watchdog.sh` 由 **`edit6-watchdog.timer`(每 10 分)** 執行,
  做存活+切換稽核,**自動 `systemctl --user restart`** 死掉的 daemon、watcher 多實例則
  `daemon_reset.sh` 清成單一,並把問題 best-effort 推到 tracked `chain_code/health_watchdog_alerts.log`。
- **不修程式碼**(那需 Claude/Route A);code-level 問題只推「診斷 + 需 Claude 修」的報告。
- 安裝/重裝(冪等):`bash chain_code/install_systemd.sh`;查下次巡檢:
  `systemctl --user list-timers edit6-watchdog.timer`。

### 跨 session / 別專案邊界(MUST)
- **唯一真相 = systemd user service**(`edit6-dispatcher` / `edit6-watcher`);存活看
  `systemctl --user is-active` + `NRestarts`。
- **殺/數 daemon 一律用 `/proc/PID/cwd` 判專案歸屬**(涵蓋絕對+相對路徑、跨專案安全);
  **絕不** `pkill -f` / cmdline 路徑字串;**絕不**碰 Edit7/Edit8/2.Re1400 等別專案 daemon。
- Route B 是 systemd timer,**不靠 user crontab**(避免歷史的 cross-project crontab clobber)。

## Project info

- Branch: Edit6_5600DNS
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

## Benchmark 比對圖雙軌分流 (triggered by `lbm-plot-benchmark`)

平均場 VTK 已達數十 GB(全場 double)。login node 的 `user.slice` cgroup 記憶體硬上限
**~20GB(跨使用者共用)**,float64 讀整顆 VTK 峰值會 OOM,且 cgroup OOM-killer 可能誤殺
同 slice 的別專案程序。故 benchmark 比對圖採**雙軌分流**:

| 軌 | 觸發 | dtype | 在哪跑 | 記憶體 |
|----|------|-------|--------|--------|
| **watcher inline** | 自動(VTK-gated, FTT≥G2=20) | float32(`--lowmem`) | login node, 0 SLURM | ~11GB < 20GB ✓ |
| **手動 canonical** | `lbm-plot-benchmark` | float64(無 `--lowmem`) | dev 計算節點(48G) | ~20GB(隔離) |

- `result/2.Benchmark.py` 的 `--lowmem`: 湍流跳過 `_BENCH_SKIP_FIELDS`(velocity/inst/omega/P_mean,
  湍流路徑硬用 U_mean/V_mean 不碰它們)+ float32 讀入。**不帶 `--lowmem` ⇒ float64**(零誤差,
  benchmark 輸出不變)。float32 捨入 ~6e-8 << 5% 比對精度,監控足夠。
- watcher(`watcher/hill_watcher.sh:run_benchmark`)永遠 inline + `--lowmem`,**不投任何 SLURM**。

### `lbm-plot-benchmark` 流程(手動 float64 最精準)
1. 唯讀挑最新 stat-stable VTK;讀 FTT(< G2=20 → 結果標 **preliminary**)。
2. `sbatch --test-only result/bench_computenode.slurm` 估開跑(dev 是 scavenger 可能 PENDING);
   再真 `sbatch`(`--gres=gpu:1`:dev QOS 有 gpu MinTRES → 0-GPU 被 QOSMinGRES 永久擋;用 dev cap
   1/32、與主 job 的 p_32gpus QOS 分離 → 不撞上限、不影響主模擬;GPU 純滿足 QOS, 計算在 CPU)。
   可 `--export=ALL,BENCH_RE=<N>` 改 Re;`--export=ALL,BENCH_LOWMEM=1` → float32(A/B 用)。
3. 背景輪詢到離隊 → sacct 終態 + log(rc + getrusage 峰值 RSS,不依賴 `/usr/bin/time`)+ ls fig_*.png。
4. 回報逐變數 L2(uu/vv/ww/uv/k vs Krank/Breuer)+ copy fig 到 `live/`。
- 守門:唯讀分析(只讀 VTK + 寫 fig);不重編/不碰 restart/不覆寫 checkpoint;dev 1-GPU job 用 dev QOS
  獨立 cap(與主 job 的 32gpus QOS 分離)→ 不影響主模擬、不撞 per-account 上限。

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
| 路徑 | `Edit6_5600DNS` | `/home/s8313697/D3Q27_PeriodicHill/Edit2_PeriodicHillDuct` |
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
