---
description: Edit13 日常監控一輪 — 8 點 + cadence 巡檢 (Re2800 ITB-LBM; 流場/benchmark 誤差 + dispatcher/watcher/watchdog 存活 + partition×account×jp 鎖定 + benchmark 圖時效/dedup)。連續監控用 /loop /Edit13
argument-hint: (無參數; 連續監控請用 /loop /Edit13)
---

你正在 `/home/s8313697/5.Re10595/Edit13_2800ITBLBM`（GILBM/ITB-LBM periodic-hill **Re_H=2800**）。
執行**一輪完整日常監控巡檢**（8 點 + cadence）。本指令可在**任何 session** 重現相同行為；
連續監控讓使用者下 `/loop /Edit13`（建議 ~60 分鐘）。本檔由 Edit6 的 `/Edit6` 改編，路徑已對 Edit13 實際結構校正。

## 雙 fork 結構（重要 — 本監控針對 NCHC fork）
- **NCHC H200 叢集**：`chain_code_nchc/`（含 `./run`、`tools/project_job_guard.sh`、`jobscript_chain.slurm.H200`、
  `install_systemd.sh`）+ watcher `watcher_nchc/hill_watcher.sh`（+ `hill_watcher_start.sh`）。**← 本監控用這套。**
- **本機 8×V100 CFDLAB**：`chain_code_local/` + watcher `watcher_local/cfdq`（不同慣例，本監控不涵蓋）。
- systemd unit **已正名 `edit13-*`**（`chain_code_nchc/systemd/` 有 edit13-dispatcher/watcher/watchdog；
  `install_systemd.sh` 會 enable edit13-* 並清掉舊 edit6-* 殘留）→ 不需再改名。

## 套用前仍需完成的設定（變數仍為 [LOCAL-TEST]）
1. **partition / account / walltime / jp**：`variables.h` 目前 `[LOCAL-TEST]`（jp=32 / Re=2800 / NY=513 / NZ=257）。
   設好 NCHC 要用的 partition/account 並填下方錨點，重編 binary。
2. **冷啟動 + daemon**：`./run --rebuild --force-cold`（首次）→ `bash chain_code_nchc/install_systemd.sh`
   啟動 edit13-* daemon + watchdog → `bash watcher_nchc/hill_watcher_start.sh`。
3. 之後本監控才有 runtime 檔（chain_jobid / slurm log / live/*.png / checkrho 等）可讀。

## 當前錨點（設定完成後填真值）
- 目標鎖定：**partition=`<填>` · account=`<填>` · jp=`32` · walltime=`<填>`**。
- head jobid 隨 chain hop 改變 → **一律 `cat restart/chain_jobid` 動態讀取**，不硬編。
- Re=2800 → benchmark 對 **Re2800** DNS（輸出檔名用 Re2800）。注意 `variables.h` 仍保留 legacy
  `UTAU_RE=5600`/`UTAU_*` u_tau 資料來源常數與註解（已知殘留、非 bug，勿誤判為設定錯誤）。

## 判定方法（MUST）
- **唯讀為主**。唯一允許動作：救活**本專案(Edit13)自己**死掉的 dispatcher/watcher。其餘不改。
- **跨專案隔離**：只操作 Edit13；**WorkDir 驗 job 歸屬**；**絕不**碰 Edit6/Edit11/Edit12 等手足。
- **取消 job 只准** `./run job-guard scancel <id>`；never bare `scancel`/`-u`/`--name`/`scontrol update|hold|...`。
- **daemon 存活 = 跨節點 heartbeat 權威**（非 node-local `systemctl is-active`）；殺/數 daemon 用 `/proc/PID/cwd` 判歸屬。

## 巡檢 8 點

```bash
cd /home/s8313697/5.Re10595/Edit13_2800ITBLBM
NOW=$(date +%s); JID=$(cat restart/chain_jobid 2>/dev/null)
# [1+5] head 狀態 + 鎖定驗證 (Partition/Account/NumNodes/WorkDir 本專案, 單一 Edit13 head)
scontrol show job "$JID" 2>/dev/null | grep -oE 'JobState=[^ ]+|Partition=[^ ]+|Account=[^ ]+|NumNodes=[^ ]+|TimeLimit=[^ ]+|RunTime=[^ ]+|WorkDir=[^ ]+'
scontrol show job "$JID" 2>/dev/null | grep -oE 'NodeList=25a[^ ]+' | tail -1
squeue -u "$USER" -o "%.10i %.22j %.10P %.10a %.6D %.8T %R" 2>/dev/null    # 確認單一 Edit13 head (WorkDir 驗歸屬)
# [2] 流場進度 + 健康
LATEST=$(ls -t slurm_*.log 2>/dev/null | head -1)
grep -E '\[Step ' "$LATEST" 2>/dev/null | tail -1
grep -E 'accu_count' "$LATEST" 2>/dev/null | tail -1
grep -E 'FATAL|MPI_Abort|cannot load|NaN|DIVERG|--cold|7 GPUs|GPU sharing|FAST-FAIL' "$LATEST" 2>/dev/null | grep -v ALGO2 | tail -3
tail -2 checkrho.dat 2>/dev/null                                            # 密度 ~1.0
# [2-daemon] dispatcher 存活 (restart/dispatcher.heartbeat, <60s = 活; 跨節點)
DH=$(cat restart/dispatcher.heartbeat 2>/dev/null); [[ -n "$DH" ]] && echo "dispatcher hb $DH age=$((NOW-$(echo "$DH"|awk -F: '{print $NF}')))s" || echo "dispatcher: 無 heartbeat (看 restart/dispatcher.pid / restart/dispatcher.log)"
# [3] watcher 存活 (live/watcher.heartbeat <90-180s 或 monitor_latest.png <5min = 活/忙非死)
WHB=$(cat live/watcher.heartbeat 2>/dev/null); [[ -n "$WHB" ]] && echo "watcher hb $WHB age=$((NOW-$(echo "$WHB"|awk -F: '{print $NF}')))s" || echo "watcher: 無 heartbeat (看 live/watcher.pid)"
[[ -f live/monitor_latest.png ]] && echo "png age=$(( (NOW-$(stat -c %Y live/monitor_latest.png))/60 ))min"
# [4] watchdog (已正名 edit13-*)
systemctl --user list-timers edit13-watchdog.timer --no-pager 2>/dev/null | sed -n 2p
# [6] benchmark 圖時效 (<120min; Re2800) — hill_watcher 產 fig_*/tau_wall 進 live/
for f in live/fig_mean_u.png live/fig_mean_v.png live/fig_uu.png live/fig_vv.png live/fig_uv.png live/fig_k.png \
         live/tau_wall_signed_Re2800_cf.png live/tau_wall_signed_Re2800_cp.png; do
  [[ -f "$f" ]] && echo "$(basename $f) $(( (NOW-$(stat -c %Y "$f"))/60 ))min"
done
# [7] watcher 事件 (本分支 watcher 無 auto-push; CONV/BENCH/TAUWALL 為事件記號)
grep -E 'CONV|BENCH|TAUWALL' live/watcher.log 2>/dev/null | tail -3
# [8] benchmark 新鮮度 (本分支用記憶體 last_bench_step 去重, 無 marker 檔 → 比 VTK vs fig mtime)
echo "newestVTK=$(ls -t result/velocity_merged_*.vtk 2>/dev/null|head -1)"
echo "ckpt=$(readlink restart/checkpoint/latest 2>/dev/null)"
echo "newest fig=$(ls -t live/fig_*.png 2>/dev/null|head -1)"   # 若 newest VTK 比 fig 新且差很多 → 尚未刷上
# [5b] git (Edit13 分支)
git fetch origin 2>/dev/null; git rev-list --left-right --count origin/Edit13_2800ITBLBM...HEAD 2>/dev/null | awk '{print "behind="$1" ahead="$2}'
```

判讀重點（逐點）：
1. **統計 vs benchmark 誤差**：各變數 L2（U/V/uu/vv/uv/k）對 **Re2800** DNS。
2. **流場 + dispatcher**：Step/FTT/accu_count 推進、Re% 在 0 附近振盪、`Error` 小、checkrho≈1.0、無 NaN/FATAL；dispatcher hb <60s。
3. **watcher**：hb <90–180s 或 png <5min（png 新鮮=忙非死）。
4. **watchdog**：`edit13-watchdog.timer` active（每 10 分）。
5. **鎖定**：head Partition/Account/NumNodes/WorkDir=本專案、**單一** Edit13 head；git behind/ahead。
6. **benchmark 圖時效**：`live/fig_*`、`live/tau_wall_signed_Re2800_*` < 120min。
7. **watcher 事件**：`CONV`/`BENCH`/`TAUWALL` 持續出現＝watcher 在出圖。**注意：本分支 watcher 無 auto-commit/push**
   → benchmark 圖只在 `live/` 本地更新；要進 git 需**手動** commit（逐檔，禁 `-A`）。
8. **benchmark 新鮮度**：watcher 用記憶體 `last_bench_step` 去重（**無 `.last_bench_step` 檔**）；
   以 newest VTK vs newest `fig_*.png` mtime 判斷是否已刷上。

**cadence**：每 VTK 刷新 benchmark；chain hop 後驗 warm 三閘門（slurm log 頂部由 **jobscript** 印的
`Restart from: <path> (step=... FTT=...)`（step 非 1）/ solver 印的 `[G6] Schema OK ... grid=match` /
`[Phase5] dt_global ✓` / `Statistics loaded ... accu_count=`）+ accu 連續 + 自投仍套 blacklist。

## 異常處置（救活本專案(Edit13)自己的 daemon 才動手；其餘只回報）
- **dispatcher 死**（hb stale）：清 stale `restart/dispatcher.nodelock` + `restart/dispatcher.heartbeat` →
  `./run dispatcher start`（或 `bash chain_code_nchc/dispatcher_start.sh`）；驗新 owner=本節點:新PID + hb 更新。
- **watcher 死**（hb stale **且** png >5min）：清殘留 `live/watcher.pid`/`live/watcher.heartbeat` 後
  `bash watcher_nchc/hill_watcher_start.sh`；驗單一實例。
- **chain 斷頭**（無 head 在 queue 且非終態續投中）：對 sacct 終態 → 必要時
  `rm -f restart/STOP_CHAIN .run.lock` 後 `./run --no-queue-check` warm 續投（**never cold**）。
- 任何疑似要動 job/別專案 → **先回報、不擅自動手**。

## 輸出格式
回報一張精簡表：head jobid/State、FTT/Step、Re%/Error/Ma_max、accu_count、checkrho、
Partition/Account/NumNodes、dispatcher/watcher/watchdog 存活、benchmark 圖時效、git behind/ahead。
全綠則一句「八點全綠」；有異常則標出問題點 + 已處置/待處置。
