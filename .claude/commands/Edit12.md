---
description: Edit12 續跑日常監控一輪 — 鎖 partition@jp=64gpus@mst114348(jp32);job 狀態/warm 三閘門/daemon heartbeat/checkpoint/offline-push
argument-hint: (無參數; 連續監控請用 /loop /Edit12)
---

你正在 `/home/s8313697/5.Re10595/Edit12_Krank56002`(GILBM periodic-hill Re5600 DNS)。
執行**一輪續跑日常監控**並回報精簡表格。當前鎖定 **partition@jp = 64gpus @ mst114348, jp32**
(head jobid 動態讀 `restart/chain_jobid`,handoff 後自動跟;若之後切回別的 partition@jp,
本檔的「應為 64gpus/mst114348」期望值需同步更新)。要連續監控就用 `/loop /Edit12`。

> **⚠ 國網中心停機 2026-06-27 09:00 ~ 06-28 14:00**。自此 loop 起每輪須維持「Edit12x
> 第二道防線」checkpoint 鏡像(見第(7)步);**停機前 1~2 小時(06-27 07:00~08:00)
> 硬閘驗 Edit12x restart 與本專案 latest 同步**。

## 監控步驟

(1) **head job 狀態(權威用 sacct,非 squeue)**:
```
JID=$(cat restart/chain_jobid)
sacct -j "$JID" -o JobID,State,ExitCode,Start,Elapsed,NodeList
squeue -u $USER -o "%.10i %.12P %.30j %.8T %.10M %.6D %R"   # 看本專案 + 手足
```
手足 Edit6 / Edit11(64gpus)**絕不可碰**。

(2) **★若 RUNNING → 驗 warm 三閘門**(`tail`/`grep` slurm_<JID>.log):
- `Restart from: restart/checkpoint/step_<N>` 且 N≥37848001(**非冷啟 step_00000001**)
- `[G6] Schema OK ... rank_count=32 grid=match`
- `[Phase5] dt_global consistent within 1e-10`
- `accu_count=` ~2200萬以上**非歸零**且持續累加(統計保留)
再看最新 `[Step.. FTT.. Re.. Ma_max.. Error..]` + `[CONV]` 推進、`checkrho.dat` 末尾密度~1.0
(末欄旗標非錯誤)、無 `FATAL|MPI_Abort|NaN|DIVERG|mismatch|cannot load`。
注意:log 內 `[ALGO2] ... dev-vs-Algo1ref bitwise mismatch`(max~3.8e-7, tol_fail=0, MAP OK)
是**預期的 RK4-vs-Algo1RK2 權重差,非失敗**,勿誤判。

(3) **★若 PENDING** → `scontrol show job <JID>` 取 Partition(應 64gpus)/Account(應 mst114348)/
Reason/StartTime。`Reason=QOSMaxJobsPerUserLimit` = 等釋出 running slot(正常);
`Reason=Priority/Resources` = 排隊等節點。留意 partition/account 是否仍 64gpus@mst114348
(`SC_PENDING_TIMEOUT_MIN=1440` 故 dispatcher 24h 內不會 churn 走 PENDING)。

(4) **★daemon 存活用 heartbeat age(跨節點權威)**:
```
NOW=$(date +%s)
for f in restart/dispatcher.heartbeat live/watcher.heartbeat; do
  hb=$(cat "$f"); ep=$(printf '%s' "$hb" | grep -oE '[0-9]{10}' | tail -1); echo "$f age=$((NOW-ep))s"
done   # <~90s = 活
```
`monitor_latest.png` mtime <~10min = 新鮮。本節點 `systemctl --user is-active
edit12-dispatcher/watcher` 可作輔證;**別 login node 上 node-local 會誤判 inactive,勿信**。

(5) **checkpoint / 去重 / offline-push**:
`readlink restart/checkpoint/latest`;`cat live/.last_bench_step` 應與最新 BENCH step 一致
(去重驗證);`git log --oneline -3` 看最新「更新 benchmark 比對圖 FTT-NN」(離線自動推送)。

(6) **跨專案隔離(MUST)**:只動 Edit12;**NEVER** 碰 Edit6/7/8/9/11 的 daemon/job/檔案;
scancel 一律 `./run job-guard scancel <id>`(驗 WorkDir);殺/數 daemon 用 `/proc/PID/cwd` 判歸屬。

(7) **★停機備援 — Edit12x 第二道防線(國網中心停機 2026-06-27 09:00 ~ 06-28 14:00)**:
每輪都要把本專案**新增的 checkpoint 鏡像到測試專案 Edit12x**,使其 restart 成為獨立第二份
還原點(若停機損及本專案 restart,可從 Edit12x 復原)。每份 checkpoint ~129GB / 1796 檔。
- **比較**: `readlink restart/checkpoint/latest` vs
  `readlink /home/s8313697/5.Re10595/Edit12x_Krank56002/restart/checkpoint/latest`。
- **鏡像(本專案有 Edit12x 缺的較新 step 時)**: 原子複製 —
  `cp -a restart/checkpoint/step_<N>  <Edit12x>/restart/checkpoint/step_<N>.WRITING`
  → `mv` 成 `step_<N>` → `ln -sfn step_<N> <Edit12x>/restart/checkpoint/latest`。
  cp 進手足由 write_guard 放行(Bash cp 允許),為使用者授權備援、非跨專案違規。
  **只在 Edit12x 無 active job 時複製**(`squeue` 確認),避免撞它自己的 restart;大檔可背景跑。
- **停機前硬閘(2026-06-27 07:00~08:00,即停機前 1~2 小時)**: 必須確認
  `Edit12x/.../checkpoint/latest` == 本專案 `latest`、**且第(8)的三大 log 已做最新快照**;
  落後就立即補齊(must-pass)。
- 詳見記憶 `project_edit12_nchc_downtime_0627`。

(8) **★三大紀錄檔備份(append-only log,~152M→~26M gzip;git 禁推→走檔案備份)**:
三檔 `Ustar_Force_record.dat` / `timing_log.dat` / `checkrho.dat`。每輪檢查 marker
`live/.last_log_backup`(epoch);**距今 >12h 或處於停機前窗口(06-27 07:00~09:00)才執行**,
否則回報「跳過(距上次 Nh)」。執行步驟:
- 壓縮主份: `gzip -c <f> > ~/log_backups/edit12_Krank56002/<stem>_<TS>_step<N>.dat.gz`
  (TS=`date +%Y%m%d_%H%M%S`,N=latest checkpoint step);逐檔 `gzip -t` 驗。
- 次份: `cp -a` 到 `/work/s8313697/edit12_log_backups/`;`md5sum` 兩地核對一致。
- 輪替: 每個 stem **各只留最近 7 份**(`ls -t <stem>_*.gz | tail -n +8 | xargs -r rm -f`),兩地都做。
- 更新 marker: `date +%s > live/.last_log_backup`。
- 主份在 /home 專案樹外(抗 reset/clean/cold-start 清 log);/work 為次(注意 scratch 可能清)。
  warm 重啟會 append 延續、不清;cold/clean 才清→這正是備份要防的。
- **停機前硬閘(同第(7),06-27 07:00~08:00)**: 強制再做一次(不管 12h),確保停機前最後資料入袋。

## 回報格式(精簡表格)
job state(partition@account) / Reason 或 ETA、FTT 進度(Step/FTT/Re%/Ma_max/Error)、
daemon heartbeat(dispatcher/watcher 秒數)、checkpoint+marker、
**Edit12x 同步(latest=step_<N>,== 或 落後本專案)**、**log 備份(TS 或 跳過)**、有無異常。
