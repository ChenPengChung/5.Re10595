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

- Branch: Edit3_Re5600newmesh
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
   - When `accu > 0` in the slurm log (= statistics started, FTT crossed
     `FTT_STATS_START` from `variables.h`), additionally runs
     `result/2.Benchmark.py --Re <Re> --no-ask-scales --no-ask-density` and
     copies `benchmark_Umean_Re*`, `benchmark_RS_Re*`, `benchmark_all_Re*`
     to `live/`.
   - Emits NaN/divergence alerts based on the slurm tail.
   Open `live/monitor_latest.png` to view the single rolling status image.
6. **Arm one Monitor watcher** (Monitor tool, not bare bash):
   - **Status snapshot** every 60s: queue state + latest `Step ... Re=...
     Ma_max=...` line + tail of `checkrho.dat`.
   Tail `live/watcher.log` separately if you want CONV/BENCH event signals.

**DO NOT** start the animation pipeline (`animation/pipeline.py`,
`animation/png_frames/`, `animation/flow_*.mp4`). The watcher in this project
is convergence + benchmark plots only — no per-VTK rendering, no MP4 encoding.
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
   ```
7. Then proceed with the standard sequence (steps 1–7 above).
