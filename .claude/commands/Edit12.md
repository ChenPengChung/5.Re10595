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
>
> **★session-independent 備援(2026-06-26 加裝)**:`chain_code/preshutdown_backup.sh`
> 已被 `health_watchdog.sh`(systemd `edit12-watchdog.timer`, 每 10min)在**停機前閘窗
> [06-27 07:00, 06-28 14:00) 自動呼叫** → 即使本 loop / session 掛掉, 仍會把最新
> checkpoint 鏡像到 Edit12x + 三大 log gzip 備份(idempotent, flock 防重疊, in-sync 則
> no-op)。本 loop 的第(7)(8)步是**第一道**(較即時), watchdog backstop 是**保險**;
> 兩者同一套原子鏡像/備份邏輯, 互不衝突。手動驗證: `bash chain_code/preshutdown_backup.sh`。

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

(7)+(8) **★停機備援 — Edit12x checkpoint 鏡像 + 三大 log 備份(國網中心停機
2026-06-27 09:00 ~ 06-28 14:00)**:**統一委派給** `chain_code/preshutdown_backup.sh`
(idempotent + 自身 flock + 原子鏡像 + 自癒;**勿再手動 cp/mv/ln**——手動路徑會與 watchdog
backstop 撞同名 `.WRITING` 造成 content-corrupt latest,已統一成單一加鎖程式路徑)。每輪:
```
bash chain_code/preshutdown_backup.sh            # (A) Edit12x 落後且無 job 才原子鏡像最新 checkpoint
                                                 # (B) 三大 log 預設僅 >12h 才 gzip(節流), 否則自動跳過
```
- 它做的事 = 舊第(7)(8)步全部: 比較兩邊 `latest`、落後才原子鏡像(`.WRITING`→`mv -T`→`ln -sfn`,
  檔數+位元組大小一致才切, 絕不先刪既有好份, 自癒前次 ln 失敗遺留)、Edit12x 有 active job 則跳過;
  三大 log gzip 到 `~/log_backups/edit12_Krank56002/`(主, 樹外抗 reset)+ `/work/...`(次)、md5 核對、
  各 stem 留 7 份、marker `live/.last_log_backup` 只在成功時更新。每份 checkpoint ~129GB / 1796 檔。
- **回報**: 跑完後 `readlink restart/checkpoint/latest` vs Edit12x `latest`(== 或落後 N step)、
  `tail -5 live/preshutdown_backup.log`(看 (A)/(B) 結果)、marker age。rc≠0 代表鏡像/備份未完成需查。
- **停機前硬閘(2026-06-27 07:00~08:00,停機前 1~2h)**: `PSB_FORCE_LOGS=1 bash chain_code/preshutdown_backup.sh`
  (強制連 log 一起做最新快照, 不管 12h)→ **必須確認 Edit12x `latest` == 本專案 `latest`**;落後 must-pass 補齊。
- **session-independent**: 同一支腳本已由 `health_watchdog.sh`(systemd timer, 每10min)在停機前閘窗
  `[07:00, 06-28 14:00)` 自動以 `PSB_FORCE_LOGS=1` 呼叫 → 即使本 loop 掛了仍會備份(見頂部 banner)。
- 詳見記憶 `project_edit12_nchc_downtime_0627`。

## 回報格式(精簡表格)
job state(partition@account) / Reason 或 ETA、FTT 進度(Step/FTT/Re%/Ma_max/Error)、
daemon heartbeat(dispatcher/watcher 秒數)、checkpoint+marker、
**Edit12x 同步(latest=step_<N>,== 或 落後本專案)**、**log 備份(TS 或 跳過)**、有無異常。
