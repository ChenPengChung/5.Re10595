---
description: Edit6 日常監控一輪 — 8 點 + cadence 巡檢 (流場/benchmark 誤差 + dispatcher/watcher/watchdog 存活 + 32gpus@MST115169@jp32 鎖定 + benchmark 圖時效/auto-push/dedup)。連續監控用 /loop /Edit6
argument-hint: (無參數; 連續監控請用 /loop /Edit6)
---

你正在 `/home/s8313697/5.Re10595/Edit6_5600DNS`（GILBM periodic-hill Re_H=5600 DNS）。
執行**一輪完整日常監控巡檢**（8 點 + cadence）。本指令可在**任何 session** 重現相同行為；
連續監控讓使用者下 `/loop /Edit6`（loop 會週期重跑本指令，建議 ~60 分鐘）。

## 當前錨點（2026-06-23 partition 遷移後）
- 目標鎖定：**partition=`32gpus` · account=`MST115169` · jp=32（4 nodes）· walltime=1-day**。
- 遷移原因：原 `64gpus`@`MST114348`；`p_64gpus` QOS MaxJobsPU=2 被 Edit6+Edit11 填滿 → 手足 Edit12
  PENDING(QOSMaxJobsPerUserLimit)。Edit6 移到 `32gpus`@`MST115169`（p_32gpus cap=32≥jp32, MaxJobsPU=4,
  帳號115 該 QOS 用量 0）釋出 64gpus slot 給 Edit12。warm 續跑（同 binary, 零資料遺失）。
- head jobid 會隨 chain hop 改變 → **一律 `cat restart/chain_jobid` 動態讀取**，不要硬編。
- **解鎖還原**：改回 `64gpus`+`MST114348`（`select_combo_lib.sh` SC_PARTITIONS/SC_ACCT、
  `submit_dispatcher.sh` ACCOUNT、`jobscript_chain.slurm.H200` `--account`、`./run partition 64gpus`）。

## 判定方法（MUST — 唯一正確法）
- **唯讀為主**。唯一允許的動作：救活**本專案自己**死掉的 dispatcher/watcher。其餘一律不改。
- **跨專案隔離**：只操作本專案；**WorkDir 驗 job 歸屬**（非 job 號 — sacct/squeue 會混進手足）；
  **絕不**碰 Edit11/Edit12 等手足的 job/daemon/檔案。
- **取消 job 只准** `./run job-guard scancel <id>`（hook 保護）；**never bare `scancel`** /
  `scancel -u`/`--name` / `scontrol update|hold|requeue|...`。本巡檢通常根本不需動 job。
- **daemon 存活 = 跨節點 heartbeat 權威**（非 node-local `systemctl is-active`，會在他節點假死）：
  `restart/dispatcher.heartbeat` / `live/watcher.heartbeat` 的 epoch 新鮮度；殺/數 daemon 用 `/proc/PID/cwd` 判歸屬。

## 巡檢 8 點

```bash
cd /home/s8313697/5.Re10595/Edit6_5600DNS
NOW=$(date +%s); JID=$(cat restart/chain_jobid)
# [1+5] head 狀態 + 鎖定驗證 (Partition=32gpus, Account=mst115169, NumNodes=4, WorkDir 本專案, 無 062, 單一 head)
scontrol show job "$JID" 2>/dev/null | grep -oE 'JobState=[^ ]+|Partition=[^ ]+|Account=[^ ]+|NumNodes=[^ ]+|TimeLimit=[^ ]+|RunTime=[^ ]+|WorkDir=[^ ]+'
scontrol show job "$JID" 2>/dev/null | grep -oE ' NodeList=25a-hgpn[^ ]+' | tail -1   # 須無 hgpn062
squeue -u "$USER" -o "%.10i %.20j %.10P %.10a %.6D %.8T %R" 2>/dev/null              # 確認單一 Edit6 head
# [2] 流場進度 + 健康
grep -E '\[Step ' "slurm_${JID}.log" 2>/dev/null | tail -1
grep -E 'accu_count' "slurm_${JID}.log" 2>/dev/null | tail -1
grep -E 'FATAL|MPI_Abort|cannot load|NaN|DIVERG|--cold' "slurm_${JID}.log" 2>/dev/null | grep -v ALGO2 | tail -3
tail -2 checkrho.dat 2>/dev/null                                                     # 密度 ~1.0
# [2-daemon] dispatcher 存活 (heartbeat <60s = 活; 跨節點)
DH=$(cat restart/dispatcher.heartbeat 2>/dev/null); echo "dispatcher hb $DH age=$((NOW-$(echo "$DH"|awk -F: '{print $NF}')))s"
# [3] watcher 存活 (heartbeat <90-180s 或 png <5min = 活/忙非死)
WHB=$(cat live/watcher.heartbeat 2>/dev/null); echo "watcher hb $WHB age=$((NOW-$(echo "$WHB"|awk -F: '{print $NF}')))s"
[[ -f live/monitor_latest.png ]] && echo "png age=$(( (NOW-$(stat -c %Y live/monitor_latest.png))/60 ))min"
# [4] watchdog
systemctl --user list-timers edit6-watchdog.timer --no-pager 2>/dev/null | sed -n 2p
# [6] benchmark 圖時效 (<120min); [7] auto-push; [8] dedup marker
for f in live/fig_uu.png live/fig_k.png live/tau_wall_signed_Re5600_cf.png; do [[ -f "$f" ]] && echo "$(basename $f) $(( (NOW-$(stat -c %Y "$f"))/60 ))min"; done
grep -E '\[push_bench\]' live/watcher.log 2>/dev/null | tail -2
echo "marker=$(cat live/.last_bench_step 2>/dev/null) newestVTK=$(ls -t result/velocity_merged_*.vtk 2>/dev/null|head -1) ckpt=$(readlink restart/checkpoint/latest 2>/dev/null)"
# [5b] git
git fetch origin 2>/dev/null; git rev-list --left-right --count origin/Edit6_5600DNS...HEAD 2>/dev/null | awk '{print "behind="$1" ahead="$2}'
```

判讀重點（逐點）：
1. **統計 vs benchmark 誤差**：各變數 L2（U/V/uu/vv/uv/k）對 Krank DNS，仍在 method-floor plateau（U1.5/uu3.5/vv3.3/uv4.2/k2.6%）。
2. **流場 + dispatcher**：Step/FTT/accu_count 推進、Re% 在 0 附近振盪、`Error` 小、checkrho≈1.0、無 NaN/FATAL（ALGO2 table-validation 行是預期 RK4-vs-Algo1 gap，非失敗）；dispatcher hb <60s。
3. **watcher**：hb <90–180s 或 png <5min（png 新鮮=忙非死）。
4. **watchdog**：`edit6-watchdog.timer` active（每 10 分）。
5. **鎖定**：head **Partition=32gpus · Account=mst115169 · NumNodes=4 · WorkDir=本專案**、**無 hgpn062**、**單一** Edit6 head；git behind/ahead。
6. **benchmark 圖時效**：`live/fig_*`<120min；若 live 比 `result/` 舊 → cp + push（逐檔，禁 `-A`）。
7. **auto-push**：`[push_bench]` LIVE（每 VTK 自動 commit+push）。
8. **dedup marker**：`live/.last_bench_step` = newest VTK = ckpt（隨每個新 VTK 前進，無重解析）。

**cadence**：每 VTK（≈1.179M steps/FTT）刷新 benchmark；chain hop ≈ 每 1 day（32gpus walltime）→
hop 後驗 warm 三閘門（`Restart from step_<latest>`（非 step_1）/ `[G6] Schema OK grid=match` /
`[Phase5] dt_global ✓` / `Statistics loaded accu_count=...`，無 `[FTT-GATE] discarding`）+ accu 連續 + 自投仍套 blacklist 排除 062。

## 異常處置（救活本專案自己的 daemon 才動手；其餘只回報）
- **dispatcher 死**（hb stale）：若 nodelock stale → 清 `restart/dispatcher.nodelock` + `dispatcher.heartbeat`，
  再 `./run dispatcher start`；驗新 owner=本節點:新PID + hb 更新。（watchdog 每 10 分也會自動救，常已自癒。）
- **watcher 死**（hb stale **且** png 也 >5min）：清殘留 pid/heartbeat 後 `bash watcher/hill_watcher_start.sh`；驗單一實例。
- **chain 斷頭**（無 head 在 queue 且非終態續投中）：`cat restart/chain_jobid` 對 sacct 終態 → 必要時
  `rm -f restart/STOP_CHAIN .run.lock` 後 `./run --no-queue-check` warm 續投（**never cold**）。
- 任何疑似要動 job/別專案 → **先回報、不擅自動手**。

## 輸出格式
回報一張精簡表：head jobid/State、FTT/Step、Re%/Error/Ma_max、accu_count、checkrho、
Partition/Account/NumNodes/無062、dispatcher/watcher/watchdog 存活、benchmark 圖時效/push_bench/dedup marker、git behind/ahead。
全綠則一句「八點全綠」；有異常則標出問題點 + 已處置/待處置。
