---
description: Edit11 日常巡檢一輪 — solver/FTT/MLUPS + benchmark去重 + daemon跨節點存活 + cluster + 壞節點復原(配 /loop /Edit11 連續監控)
---

Edit11 日常巡檢一輪(**只操作 Edit11_Krank5600,絕不碰 Edit6/Edit12/其他專案的 job/daemon/檔案**)。
讀 `restart/chain_jobid` 取 JID。背景:GILBM/ITB-ISLBM D3Q19、jp=64、partition 64gpus、account MST115169、
SKIP_MIDSTEP_MASSCORR=1;★daemon 跨節點 systemd 單例(可能在別 login node);★benchmark 圖 watcher 自主推
+ 已去重(commit 6a77936,共享檔 `live/.last_bench_step`+atomic 預搶占);★持久壞節點黑名單
`restart/bad_nodes_global_local`(純節點名、勿加註解);★Re% 看滾動平均非瞬時(瞬時 ±1-2.4% 極限環正常);
★MLUPS instant 基準 ~750/GPU(Iter_ms ~7.8ms,= 健康滿速)。

**[1] chain job** — `sacct -j $JID -o State,ExitCode,Elapsed,NodeList`:
- ★換新 head(chain_jobid 變)→ 驗 warm-load 三閘門(`slurm_<JID>.log`:`Restart from step_<latest>` /
  `[G6] Schema OK ... grid=match` / `[Phase5] dt_global consistent`)+ FTT 接續(非從 0)+ accu 保留 +
  舊 head COMPLETED(RC 0)+ **單一 Edit11 head**(squeue 以 WorkDir 驗歸屬)。
- walltime elapsed ≥ 22h → 收緊 ~900s 盯 jobscript 自投續鏈(graceful 寫 final ckpt → exit → 自投)。
- ★**撞壞節點復原**(任一:`mpirun exit: RC=1` 在 ~13s / `.err` 有 `7 GPUs but 8`(GPU 共享)/
  metrics+slurm log >5 分凍(hang)/ chain_jobid 狂跳):`touch restart/STOP_CHAIN` 止血 +
  `systemctl --user stop edit11-dispatcher` → 找壞節點(崩看 `.err` rank→節點;hang 看 slurm
  `watchdog forced <node> into resubmit exclude`)→ 加進 `restart/bad_nodes_global_local`(純節點名)→
  確認舊 head 離隊 → `rm -f restart/STOP_CHAIN .run.lock` → `echo y | ./run --no-queue-check`(warm,
  不 rebuild/不 cold)→ 重啟 dispatcher(`reset-failed`+清死 `dispatcher.nodelock`+`systemctl --user start
  edit11-dispatcher`)。湊不到 8 個真健康 idle → 維持 PENDING 等(進度安全、零損失)。

**[2] solver** — `slurm_<JID>.log` 最新 `[Step N | FTT=.. Re%=.. Ma_max=.. Error=..]` + `[CONV]`:
FTT 前進(maxstep 增、非卡 restart 點)、Error<1e-5、Ma_max<0.1、無 NaN/FATAL/MPI_Abort;
★MLUPS instant `grep 'MLUPS (instant)'` ~750/GPU 沒掉(掉=壞節點拖累)。

**[3] benchmark 圖 + 去重** — `live/watcher.log` 的 `BENCH step=N` vs 最新 `result/velocity_merged_*.vtk` step
(都==跟上);`tail live/push_benchmark_figs.log` 近期 `✅ pushed`;dedup:新 step 只 count=1
(`grep -aoE 'BENCH step=[0-9]+' live/watcher.log|sort|uniq -c`)、同 FTT 單 commit、rc=137 不因併跑增。
圖沒跟上 backup:`bash chain_code/push_benchmark_figs.sh <step>`(flock),>2 輪 deferred 才回報。

**[★3b] L2 趨勢監控** — `tail -5 live/l2_history.dat`(watcher 每次成功 benchmark 記一行:step/FTT +
MGLET/Krank 各 U,V,uu,vv,uv,k 的 `<avg>` 相對 L2%)。**判讀**:
- **正常**:L2 plateau 在 ~5%(Reynolds 應力 uu/vv/uv/k)= **2 階 LBM vs 高階 DNS 的方法地板**(系統性、
  不可再降,跑再久也一樣);`<u>` U 應 ~1–2%(極低);統計尾巴 ~0.1–0.3pp 微降也正常。**plateau 不是 bug,不報警。**
- **只在異常時回報**:(a) L2 隨 FTT **明顯上升** → 統計可能失效/被 reset → 查 `accu_count` 是否還在成長
  (該檔 §[7] 旁)+ 有無 cold-start;(b) **U(`<u>`)L2 突跳** → 流場問題,連同 NaN/drift 一起查。
- 檔案 `live/l2_history.dat`(gitignore,隨 benchmark 累積);若尚未生成 = watcher 在 L2-logging 新碼下還沒跑過
  benchmark(下次 VTK 自然生成,需先 RESTART_WATCHER 載入新碼)。

**[★4] daemon 跨節點存活** — ★用 **heartbeat / lock-owner(跨節點權威)** 判,**非** node-local `systemctl is-active`
(本 session 在別 login node 時 local 顯示 inactive 是正常單例行為):
- watcher:`live/watcher.heartbeat`(epoch 第三欄)距今 <90s + `live/watcher.nodelock/owner`;SE(SELF-EVICT)穩定不增。
- dispatcher:`restart/dispatcher.heartbeat` mtime <~120s + `restart/dispatcher.nodelock/owner`。
- heartbeat 新鮮 = 活(別管 local systemctl)。★只有 heartbeat **>180s 凍** 才是真死 → 在 owner 那個節點、或本節點
  清污染(stale nodelock/pid/heartbeat)後重啟,**絕不在別節點盲目重啟製造重複**。

**[6]** `jp` (variables.h) =64 / `squeue %P` =64gpus。**[7]** `checkrho.dat` 末密度 ~1.0、drift ~e-12、末欄 flag=1
是 SKIP_MC 旗標非錯誤;無 NaN/DIVERG。**[8]** `git fetch origin` ahead/behind(自主推送圖應 a=0 b=0)。
**[★9] cluster** — 真健康 idle:`sinfo -h -p 64gpus -t idle -N -o '%N %E'` 數 **reason=none**(扣黑名單)的節點
(★`sinfo -t idle` 看到的「idle」很多其實 drained DRAM error/保留,要看 reason 欄);+ 本帳號 RUNNING GPU。

**[★9b] 黑名單自動瘦身**(防過肥 → PENDING 飢餓)— 對 `restart/bad_nodes_global_local` 每個節點稽核:
`sinfo -h -n <node> -o '%T %E'` 看 state/drain + `squeue -h -w <node> -t RUNNING -o '%u %M'` 看其他 user 的 job。
**移除條件(全部滿足)**:(a) 非 drained(reason=none)、(b) **正被其他使用者的 job 穩定運行 >3h**(別人測過健康)、
(c) 非「我近 ~2-3h 內才 hang/崩潰加入」的節點(剛失敗的留著觀察)。移除 = 從檔案刪該行(**純節點名、絕不留
# 註解**;先 `cp` 備份);只影響下次 submit、不碰 running job。drained / idle 無 job / 剛失敗 → 保留。回報移除/保留清單。

**[★10] Edit11x checkpoint vault 同步(NCHC 停機 2026-06-27 09:00~06-28 14:00 第二道防線)** — 把 production
最新 checkpoint 鏡像到隔離複製檔 `/home/s8313697/5.Re10595/Edit11x_Krank5600`(jp=64 相同 → 純整目錄複製,
無 repartition);Edit11x 有 STOP_CHAIN+無 job,安全當 vault。詳見記憶 `project_edit11_nchc_shutdown_0627`。
- **每輪呼叫一次**:`bash chain_code/sync_checkpoint_to_testcopy.sh`(背景跑佳,~157GB/3588 檔)。dedup:vault
  latest==prod latest 則秒退;新 checkpoint(~每 FTT)出現才真複製。腳本:rsync→.partial→驗 bytes+files→
  `mv -T`→原子切 latest→prune 留最近 2,**絕不在驗證好的新份就位前刪舊份**;flock 防併發;STABLE_SEC=90 防 mid-write。
- **回報**:`bash chain_code/sync_checkpoint_to_testcopy.sh --verify`(exit 0=同步/2=不同步)→ 報 vault latest step
  與 prod latest 是否一致;>1 FTT 落後才示警。
- **自足性**:checkpoint `step_N/metadata.dat` 自帶 accu_count/grid_dims/dt_global → 只複製 `checkpoint/step_N`+
  `latest` 即可 warm-restart;**專案根 `statistics/`(58G live 輸出)非 restart 依賴 → 不同步**。
- **時間閘(每輪比對 currentDate)**:(a) 06/27 ~07:00–08:00(停機前 1–2h):`--verify` 確認同步,不同步立刻補
  sync;(b) 06/27 09:00 前:再 sync 一次確保 vault 是停機瞬間最新;(c) 06/27 09:00~06/28 14:00 停機中:job 被
  NCHC 砍、squeue 無 job 屬正常,勿驚慌勿亂投;(d) 06/28 14:00 復機:`lbm-grab` 搶位 warm 重投,production restart
  損壞才動用 Edit11x vault(直接在 Edit11x warm restart,或複製 vault checkpoint 回 production 再 warm)。
- **守門**:只讀 Edit11、只 rsync/cp 寫 Edit11x(write_guard 放行 Bash cp);**絕不**碰 Edit6/Edit12/Edit13。

**[★11] 三大紀錄檔分層備份**(過大不進 git;`Ustar_Force_record.dat`/`checkrho.dat`/`timing_log.dat`)—
gzip(~7x)時間戳快照,**主 `~/log_backups/Edit11_Krank5600`(/home 持久、專案樹外 → 抗 rm/clean/reset/cold)
+ 次 `/work/s8313697/edit11_log_backups`(便利份,scratch 可能被清)**;檔名 `<base>_<時間戳>_step<N>.dat.gz`,
各 dest/各檔輪替留最近 **10 份**,gzip -t + 兩份 **md5 雙向核對**。⚠ /home 與 /work 同一 weka 後端、**非真異地**
(要真異地需給 remote 走 rclone/scp)。
- **每輪呼叫**:`bash chain_code/backup_record_files.sh --throttle 180`(180 分內已備則 dedup 跳過);`--status` 回報份數。
- **主力 session-independent**:`health_watchdog.sh`(systemd timer 每 10 分)內建 `--throttle 180` → 即使無
  Claude session(含停機跨夜)也 ~3h 自動一份(留 10 ≈ 30h 史)。
- **停機前硬閘**:watchdog 於 06/27 07:00~09:00 自動 `--force` 一份「停機前最新」(sentinel `.preshutdown_done`
  只做一次,session-independent);loop 在該窗亦可手動 `--force` + 與 Edit11x checkpoint `--verify` 同一時點。
- **append-only 守門**:來源比上次小(疑 cold-reset/truncate)→ 跳過本次快照(保住既有好快照不被輪替沖出)+ ⚠SHRINK。
- **守門**:只讀三檔、只寫上述兩 dest(路徑硬閘);不碰流場/checkpoint/job/別專案。

**守門(MUST)**:絕不 `systemctl/cp/rm` 任何 `edit6-*`;不碰別專案 job/daemon/checkpoint;`scancel` 只用
`./run job-guard scancel <明確 jobid>`(帶變數會被 hook 擋);blacklist 數據檔一律純節點名;vault 同步只寫 Edit11x。
**節奏**:正常 ~3600s;walltime 近(elapsed≥22h)/復原中/**接近停機(06/27 06:00 後)** 收緊 ~900s;穩定 RUNNING +
FTT 前進 + MLUPS 正常 → 回 ~3600s。
**全綠回報格式**:「沒問題」+ FTT / Re% / CV / drift / MLUPS + daemon(heartbeat)+ cluster + dedup + 距目標 FTT 天數 一句。
