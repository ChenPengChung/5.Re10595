---
description: Edit12 續跑日常監控一輪 — 鎖 partition@jp=16gpus@mst114348(jp32, 2天walltime);job 狀態/warm 三閘門/fast-fail 稽查/daemon heartbeat/checkpoint/offline-push
argument-hint: (無參數; 連續監控請用 /loop /Edit12)
---

你正在 `/home/s8313697/5.Re10595/Edit12_Krank56002`(GILBM periodic-hill Re5600 DNS)。
執行**一輪續跑日常監控**並回報精簡表格。當前鎖定 **partition@jp = 16gpus @ mst114348, jp32**
(2-day walltime;2026-06-29 從 64gpus 切到 16gpus 以搶 2 天 walltime、減少續投次數)。
**三層 partition 鎖須一致 16gpus**:① jobscript `#SBATCH --partition`/`--time`、② `restart/h200_partition` pin、
③ `chain_code/tools/select_combo_lib.sh` 的 `SC_PARTITIONS`(dispatcher backstop)。
(head jobid 動態讀 `restart/chain_jobid`,handoff 後自動跟;若之後再切別的 partition@jp,
本檔「應為 16gpus/mst114348」期望值 + 上述三層鎖需同步更新)。要連續監控就用 `/loop /Edit12`。

> **✅ 國網中心停機已結束**(2026-06-27 09:00 ~ 06-28 14:00,已於 06-28 14:00 復機)。
> 停機備援(Edit12x 鏡像)現降為 **optional backstop**,不再每輪必做(見第(7)步)。
>
> **🔴 復機後 fast-fail 風險(2026-06-29 事故,每輪須稽查)**:NCHC 維護後 compute-node 環境回歸
> 曾致 **mpirun 秒崩 storm**(RC=1:`/tmp` 不可寫→OMPI session dir 失敗 + `module load cuda/13.0` 失效→CUDA
> runtime 未載入;後期變 RC=126:`--export=ALL` 跨輪累積 PATH→E2BIG `Argument list too long`)。已由 jobscript
> **`[HOTFIX 2026-06-29]`**(TMPDIR→`/dev/shm` + 直接補 CUDA `LD_LIBRARY_PATH`)修復。稽查重點見第(2)步;
> **HOTFIX 區塊若被 reset 抹掉 → 秒崩復發**,須確認它仍在 `chain_code/jobscript_chain.slurm.H200`。

## 監控步驟

(1) **head job 狀態(權威用 sacct,非 squeue)**:
```
JID=$(cat restart/chain_jobid)
sacct -j "$JID" -o JobID,State,ExitCode,Start,Elapsed,NodeList
squeue -u $USER -o "%.10i %.12P %.30j %.8T %.10M %.6D %R"   # 看本專案 + 手足
```
**★鏈死偵測**:若 sacct 的 chain_jobid 為**終態(FAILED/COMPLETED/TIMEOUT/NODE_FAIL)且 squeue 本專案無 job**
= 鏈斷,須搶救:先查 fast-fail 根因(第(2)步),`rm -f restart/STOP_CHAIN .run.lock` 後
`echo y | ./run --no-queue-check` warm 重投(沿用 jobscript header=16gpus,zero-loss)。
手足 Edit6 / Edit11(64gpus)、**Edit13(16gpus@mst115169,與本專案同 partition 不同帳號)**:全部**絕不可碰**。

(2) **★若 RUNNING → 驗 warm 三閘門**(`tail`/`grep` slurm_<JID>.log):
- `Restart from: restart/checkpoint/step_<N>` 且 N≥37848001(**非冷啟 step_00000001**)
- `[G6] Schema OK ... rank_count=32 grid=match`
- `[Phase5] dt_global consistent within 1e-10`
- `accu_count=` ~2200萬以上(現約 3700萬+)**非歸零**且持續累加(統計保留)
再看最新 `[Step.. FTT.. Re.. Ma_max.. Error..]` + `[CONV]` 推進、`checkrho.dat` 末尾密度~1.0
(末欄旗標非錯誤)、無 `FATAL|MPI_Abort|NaN|DIVERG|mismatch|cannot load`。
**★fast-fail 稽查(2026-06-29 事故類;務必查,但「只看本輪」——累積 log/舊 .err 永久保留 09:28 那批已復原的事故字串,grep 全檔會誤報)**:
- `cat restart/fast_fail_count 2>/dev/null || echo 0` — **非 0 且增長 = crash-loop**(健康時此檔不存在=0,別因檔缺報錯)。
- **只 grep 本輪 head job 的 `slurm_<JID>.err`**(非全 .err、非累積 log):不該有
  `RC=1|Argument list too long|module: command not found|TMPDIR.*not writeable`。
  ⚠️ **`The following module(s) are unknown: "cuda/13.0"` 是良性**(cuda/13.0 module 本就被移除、HOTFIX 已直接補 CUDA 路徑),
  **每個健康 job 的 .err 都有它 → 絕不可當 fast-fail 訊號**(故上面 grep 不含 `module(s) are unknown`)。
- 真正陽性 = `restart/chain.log` 在**本輪 sacct Start 之後**出現 `FAST-FAIL detected: <nodes> died in <N>s (RC=<rc>)`(RC=1=/tmp+module、RC=126=E2BIG)。
- `restart/dispatcher.log` 的 `RC=42 [POLICY-C1] unavoidable stop. dispatcher 收工` **只在它是最新一行 + sacct 顯示 chain_jobid 終態**時才=鏈死;否則是已復原的歷史殘留(勿誤判)。
注意:log 內 `[ALGO2] ... dev-vs-Algo1ref bitwise mismatch`(max~3.8e-7, tol_fail=0, MAP OK)
是**預期的 RK4-vs-Algo1RK2 權重差,非失敗**,勿誤判。

(3) **★若 PENDING** → `scontrol show job <JID>` 取 Partition(應 **16gpus**)/Account(應 mst114348)/
Reason/StartTime。`Reason=QOSMaxJobsPerUserLimit` = 等釋出 running slot(正常);
`Reason=Priority/Resources` = 排隊等節點。留意 partition/account 是否仍 **16gpus@mst114348**、walltime 2-00:00:00
(16gpus per-account cap=32 GPU,jp32 剛好;Edit13 同在 16gpus 但走 mst115169 不佔本帳號額度)。

(4) **★daemon 存活用 heartbeat age(跨節點權威)**:
```
NOW=$(date +%s)
for f in restart/dispatcher.heartbeat live/watcher.heartbeat; do
  hb=$(cat "$f"); ep=$(printf '%s' "$hb" | grep -oE '[0-9]{10}' | tail -1); echo "$f age=$((NOW-ep))s"
done   # <~90s = 活
```
`live/monitor_latest.png` mtime <~10min = 新鮮。本節點 `systemctl --user is-active edit12-dispatcher/watcher`
可作輔證;**別 login node 上 node-local 會誤判 inactive,勿信**。**認/數 daemon 擁有者**以 `restart/dispatcher.nodelock/owner`
(跨節點 singleton、真正會投的那個)為**權威**,**非** heartbeat owner —— heartbeat 是單一共享檔,deferring 的非 owner 實例會瞬寫一次
造成 owner 在節點間跳動(lgn02→lgn01→lgn02);heartbeat 只用來看**存活/age**。`pgrep -f submit_dispatcher` 會把 dispatcher
fork 的子 subshell 一起算→「2 實例」常是父+子(看 `ppid`),非真重複。

(5) **checkpoint / 去重 / offline-push**:
`readlink restart/checkpoint/latest`;`cat live/.last_bench_step` 應與最新 BENCH step 一致
(去重驗證);`git log --oneline -3` 看最新「更新 benchmark 比對圖 FTT-NN」(離線自動推送)。

(6) **跨專案隔離(MUST)**:只動 Edit12;**NEVER** 碰 Edit6/7/8/9/11/**13** 的 daemon/job/檔案;
scancel 一律 `./run job-guard scancel <id>`(驗 WorkDir);殺/數 daemon 用 `/proc/PID/cwd` 判歸屬。

(7)+(8) **停機備援 — Edit12x checkpoint 鏡像 + 三大 log 備份**(◇ 06-28 14:00 已復機,**此步現為
optional backstop、非每輪必做**;保留供下次停機或當第二道防線):**統一委派給** `chain_code/preshutdown_backup.sh`
(idempotent + 自身 flock + 原子鏡像 + 自癒;**勿再手動 cp/mv/ln**——手動路徑會與 watchdog
backstop 撞同名 `.WRITING` 造成 content-corrupt latest,已統一成單一加鎖程式路徑)。需要時:
```
bash chain_code/preshutdown_backup.sh            # (A) Edit12x 落後且無 job 才原子鏡像最新 checkpoint
                                                 # (B) 三大 log 預設僅 >12h 才 gzip(節流), 否則自動跳過
```
- 它做的事: 比較兩邊 `latest`、落後才原子鏡像(`.WRITING`→`mv -T`→`ln -sfn`,檔數+位元組大小一致才切,
  絕不先刪既有好份, 自癒前次 ln 失敗遺留)、Edit12x 有 active job 則跳過;三大 log gzip 到
  `~/log_backups/edit12_Krank56002/`(主)+ `/work/...`(次)、md5 核對、各 stem 留 7 份。每份 checkpoint ~129GB。
- **回報**: `readlink restart/checkpoint/latest` vs Edit12x `latest`、`tail -5 live/preshutdown_backup.log`、marker age。
- 詳見記憶 `project_edit12_nchc_downtime_0627`。

## 回報格式(精簡表格)
job state(partition@account,**應 16gpus@mst114348**)/ Reason 或 ETA、FTT 進度(Step/FTT/Re%/Ma_max/Error)、
**fast_fail_count(本輪)+ 本輪 .err 的 fast-fail 訊號有無(RC=1/RC=126/E2BIG/TMPDIR;不含良性 cuda/13.0 Lmod 警告)**、
**HOTFIX 區塊在否**(grep `[HOTFIX 2026-06-29]` jobscript)、**三層 partition 鎖一致(16gpus)**、
daemon heartbeat(dispatcher/watcher 秒數;owner 認 nodelock)、checkpoint+marker、Edit12x 同步(optional)、有無異常。
