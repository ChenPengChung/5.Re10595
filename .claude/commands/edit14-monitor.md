---
description: Edit14 GILBM 本地 cfdq 生產巡檢 (dispatcher/watcher/keepalive 存活 + git fetch + 剩餘時間 + benchmark L2)
---

# /edit14-monitor — Edit14_2800GILBM 一輪生產巡檢（唯讀，git fetch 除外）

本專案 `Edit14_2800GILBM` 走**本地 cfdq**(非 NCHC/SLURM):一台 8×V100 整台獨佔,
solver log 是 `run_local_*.log`(不是 slurm_*.log)。job 由 cfdq daemon 放置/續鏈。
**連續監控:`/loop /edit14-monitor`**(自訂間隔:`/loop 5m /edit14-monitor`)。

執行下列檢查並回報**一張精簡表格**。除 `git fetch origin` 外全程唯讀;
**絕不** submit/cancel/rebuild/改 checkpoint;**絕不**用 `cfdq rm`/`pkill`;
跨專案安全 = 一律 `/proc/PID/cwd` 判歸屬,絕不碰 Edit11/Edit13 等別專案 daemon。

專案根:`/home/chenpengchung/5.Re10595/Edit14_2800GILBM`(下稱 `$PROJ`)。常數:
`FTT_STATS_START=25.0`、`CV_WINDOW_FTT=10.0` → **benchmark 出圖門檻 FTT≥35**;`FTT_STOP=200.0`。

## a. dispatcher (cfdq daemon — 全域單例,由 keepalive 以 nohup 保活)
- `cfdq ls` → 確認 `daemon: running`;`age=$(( $(date +%s) - $(stat -c %Y ~/.cfdq/daemon.lock/alive) ))`
  應 < 90s。回報 daemon pid + age。(daemon **不是** systemd unit;Edit13/Edit14 共用,兩專案 keepalive 各以鎖+nohup 保活)
- 找本專案 job:掃 `~/.cfdq/jobs/*/spec`,挑 `cwd=$PROJ` 的(目前是 **0007 edit14**),
  讀其 `status`(running/done/...)與 `run`(node/pid/lastalive)。確認 `node=CFDLab-ib11`、
  `status=running`、`lastalive` 新鮮。

## b. watcher (`hill_watcher.sh`,由 keepalive 保活;/30s 收斂+benchmark 出圖)
- pid 交叉驗: `p=$(cat $PROJ/live/watcher.pid)`;`kill -0 $p` 且 `readlink /proc/$p/cwd == $PROJ` → 存活。
  (watcher **不是** systemd unit;死亡由 keepalive 60s 輪詢 nohup 拉回)
- 新鮮度:`live/watcher.heartbeat` 或 `live/watcher.log` mtime 應在數分鐘內。tail `live/watcher.log`
  最後 1-2 行(CONV/BENCH 事件)。產圖在 `live/monitor_latest.png`。

## c. 第二層守衛 (keepalive — systemd `edit14-local-keepalive.service`,守 daemon+watcher)
- **存活看 systemd (唯一真相)**: `systemctl --user is-active edit14-local-keepalive.service` 應 = active
  (+ enable-linger → 開機/登出/斷線自起;此層活著 → watcher+daemon 必被它保活)。
- pid 交叉驗: `w=$(cat $PROJ/live/watchdog.pid)`;`kill -0 $w` 且 `readlink /proc/$w/cwd == $PROJ`。
  tail `live/keepalive_watchdog.log` 最後 2 行(heartbeat / ALARM 重啟紀錄)。
- linger 檢查: `loginctl show-user $USER | grep Linger` 應 = `Linger=yes`(否則 reboot 不自起)。
- **死了才重啟(僅限本專案)**: `systemctl --user restart edit14-local-keepalive.service`
  (它再 nohup 拉回 watcher+daemon)。整套未安裝/損壞 → 冪等重裝:
  `bash chain_code_local/install_systemd_local.sh`。**絕不**碰 Edit13 的 `edit13-local-keepalive.service`。

## d. git fetch origin
- `cd $PROJ && git fetch origin` → 回報本地 `Edit14_2800GILBM` 與 `origin/Edit14_2800GILBM`
  的 ahead/behind(`git rev-list --left-right --count HEAD...origin/Edit14_2800GILBM`)。
  有未 commit 變更則提醒(`git status --short`),但**不自動 commit**(等使用者 `claude commit`)。

## e. 模擬剩餘時間 (~days)
- `LG=$(ls -t $PROJ/run_local_*.log | head -1)`;抓最新 `[Step N | FTT=..]` 的 FTT 與當下時間。
- 算速率:取 log 內**相隔較遠的兩個** `FTT=` 點(或本輪與上輪巡檢的 FTT),
  `rate = ΔFTT / Δwall(hr)`。回報 **目前 FTT、FTT/hr、距 FTT_STOP=200 的剩餘 ≈ (200−FTT)/rate 小時 ≈ ? 天**。
  注意暖啟動後 FTT 從低點重算(本次自 step_1429327 起 FTT≈1.5);續鏈換節點時 log 會換檔,跨檔取點。

## f. benchmark 出圖門檻 → L2 誤差比對
- 若 **目前 FTT < 35** → 回報「未達 benchmark 門檻 (FTT<35),統計累積中」,跳過。
- 若 **FTT ≥ 35**:
  1. watcher 此時應已自動產出 `live/fig_mean_u.png`、`live/fig_uu.png`、`tau_wall_signed_Re2800_c{f,p}.png`。
  2. 跑(唯讀)`cd $PROJ && python3 result/2.Benchmark.py --Re 2800 --no-ask-scales --no-ask-density`,
     對 Breuer DNS 比對平均速度/雷諾應力剖面,**算各站位的相對 L2 誤差**
     `L2 = ||u_LBM − u_DNS||_2 / ||u_DNS||_2`(腳本若已輸出 L2 就引用,否則就產出的剖面資料現算)。
  3. 回報每個比對量(⟨u⟩、⟨u'u'⟩、Cf、Cp)的 L2,並標示是否落在合理範圍(經驗 < ~5–10%)。
     **這是 Edit14(GILBM)對 Edit13(ITBLBM)演算法對照的最終科學指標** — 兩專案同門檻、同 DNS,
     可直接比 L2 收斂程度。

## solver 健康(每輪都看)
- `LG` 最新 `[Step .. FTT=.. Re=.. Ma_max=.. ]`:Re 應≈2800、Ma_max 應 < ~0.1 且不發散。
- `grep -niE 'FATAL|MPI_Abort|NaN|DIVERG' $LG`(忽略含 `mismatch`/`weight gap` 的 ALGO2 預期行)。
- `tail -2 $PROJ/checkrho.dat`:密度≈1.0、最後一欄 flag=0。

## 回報格式
一張表:`項目 | 狀態 | 重點數值`,涵蓋 a–f + solver 健康。沒問題就明講「沒問題」;
有死亡/異常才動手修(僅限本專案、僅重啟 daemon,不碰 job/checkpoint),修完回報做了什麼。
