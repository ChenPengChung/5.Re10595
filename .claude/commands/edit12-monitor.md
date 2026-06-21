---
description: Edit12 健康巡檢一輪 — watcher/dispatcher 存活 + partition×jp 切換機制稽核, 死亡則修碼+獨立推送, 沒問題則回報沒問題
argument-hint: (無參數; 連續監控請用 /loop /edit12-monitor)
---

你正在 `/home/s8313697/5.Re10595/Edit12_Krank56002`(GILBM periodic-hill Re5600 DNS)。
執行**一輪完整健康巡檢**,涵蓋下列 4 點。這條指令可在**任何 session** 重現相同行為;
要連續監控就讓使用者下 `/loop /edit12-monitor`(loop 會週期重跑本指令)。

## 判定方法(MUST — 唯一正確法)
- watcher / dispatcher 的**唯一真相來源 = systemd user service**:`edit12-watcher.service`、
  `edit12-dispatcher.service`。存活用 `systemctl --user is-active` + `show -p MainPID,NRestarts`。
- **殺/數 daemon 一律用 `/proc/PID/cwd` 判專案歸屬**(cwd 在 `Edit12_Krank56002` 才算本專案)—
  同時涵蓋絕對+相對路徑啟動,且**跨專案安全**。**絕不**用 `pkill -f` / cmdline 路徑字串。
- **絕不碰** Edit7/Edit8/2.Re1400 等別專案的 daemon、job、檔案。
- 取消 job **只准** `./run job-guard scancel <id>`(本巡檢通常根本不需取消 job)。

## 巡檢 4 點
1. **存活**:
   - dispatcher:`systemctl --user is-active edit12-dispatcher.service`=active 且 cwd 實例=1。
   - watcher:同上(`edit12-watcher.service`)且 cwd 實例=1 且 `live/monitor_latest.png` < ~10 分;
     另量 `grep -c PROCESS live/watcher.log` 的 ~35s 增量應 0~2(爆量=spin/多隻 → `bash chain_code/daemon_reset.sh` 清成單一)。
2. **死亡/crash-loop 則維修並獨立推送**:任一 inactive/failed、cwd 實例=0、或 NRestarts 比上次暴增:
   - `systemctl --user status` + `journalctl --user -u <service> -n 50` + tail 對應 log 找死因。
   - 若為 **code-level** bug → 修對應程式碼(`chain_code/submit_dispatcher.sh`、
     `chain_code/tools/select_combo_lib.sh`、`watcher/hill_watcher.sh`、`watcher/hill_watcher_start.sh` 等)
     → `bash -n` 驗證 → `systemctl --user restart` → 確認 active。
   - **獨立推送**:逐檔 `git add`(**禁** `-A`)+ `git commit`(繁中,訊息含**問題點 + 對應解法**)
     + `git push`。三大紀錄檔(Ustar_Force_record/timing_log/checkrho)先 `gzip -k -9 -f` 成 `.dat.gz` 再 add;
     排除執行期產物;**不** `--force`;非 fast-forward 被拒則先回報讓使用者決定。
3. **partition×jp 自由切換機制稽核**:
   - job:`sacct -j $(cat restart/chain_jobid) -o State,Elapsed,Partition` 看 RUNNING/PENDING;
     `chain_jobid` 與 `squeue` 一致;FTT 推進(朝 40)。
   - PENDING 過久 → 確認 dispatcher 有 net-best **re-select**(候選 EVAL/⚠QOS + 選擇 + 警告 + 切到可投,never-idle)。
   - 震盪:`bash live/osc_check.sh` → `*** OSCILLATION ***`(|ΔUb/Ub|>3%)/ `*** ALERT ***`(NaN/FATAL/G6)/
     Ma_max>0.3 → 警報。選中 jp 連續來回 32↔64↔32 = thrash → 警報。
   - `accu_count` > 0 後切 jp → 確認 repartition 統計連續(沒被重置)。
   - 機制壞掉(屬 code 問題)→ 同第 2 點:修碼 + 獨立推送。
4. **沒發現問題 → 回報「沒問題」**:輸出精簡表格(dispatcher / watcher / job / 切換 各一列 + 結論「沒問題」)。

## 背景
三層保命:dispatcher(net-best + PENDING re-select)、jobscript Layer 2(compute node 自我續投)、
systemd(`Restart=on-failure` + linger,15s 級)。另有 Route B:`chain_code/health_watchdog.sh`
由 `edit12-watchdog.timer`(每 10 分)在**無 session** 時做存活+稽核+自動 restart+推 alert 報告。
有效 jp ∈ {16,32,64};cap normal=16 / 4nodes=32 / dev=∞;帳號 MST114348;H200=8 GPU/node。
