# Pipeline-Prompt:systemd 自主監控 watchdog 跨專案安全移植(可重用)

> 用途:當某 GILBM 子專案的 systemd 自主監控(dispatcher / watcher daemon + health watchdog
> timer)腳本參照了「別專案的 editX-」服務名 / ROOT 路徑(從 Edit6 等抄來未改完 → 跑下去會
> 操作別專案 = 跨專案污染),照此流程把它正確移植成「本專案 editM-」+ 完全不碰別專案,驗證後
> 再啟用。本檔隨 git 散佈,任何 session 可貼上重跑。
> ★只操作當前專案,絕不碰別專案的 job / daemon / systemd service / 檔案。

## 名詞(每專案替換)
- `THIS` = 本專案目錄名(如 `Edit12_Krank56002`);`PREFIX` = 本專案 systemd 前綴(如 `edit12-`)。
- 一律從 `variables.h` 所在的專案根判定;絕不假設、絕不沿用抄來的別專案名。

## 觸發時機
- `bash chain_code/install_systemd.sh` 或 `systemctl --user list-timers` 顯示服務名是別專案 `editX-`,
- 或 `systemctl --user is-active editM-watcher` 回 inactive 但 daemon 其實在跑(plain process,未被 systemd 託管),
- 或全專案 grep 到 `ROOT=.../EditX_*` / `cd .../EditX_*` / `systemctl ... editX-` 等跨專案功能性殘留。

## Step 0 — 診斷(唯讀,先分類再動)
1. 全專案盤點:
   ```
   grep -rnE 'edit[0-9]+-|Edit[0-9]+_[0-9A-Za-z]+' . --include='*.sh' --include='*.py' \
     --include='*.md' --include='*.slurm*' --include='*.service' --include='*.timer' --exclude-dir='.git'
   ```
2. 逐一分類(關鍵:別 blanket 全換,會破壞合法跨專案語意):
   - 🔴 **功能性地雷(必改 → 本專案)**:`ROOT=` / `cd ` 帶別專案路徑;`systemctl`/`cp`/`enable`/`disable`
     操作 `editX-` 服務;unit 檔的 `WorkingDirectory`/`ExecStart`;install / health_watchdog / daemon_reset /
     clean_this_node 的服務操作。
   - **自指註解(改 → 本專案)**:描述「本專案」卻寫成別名的註解 / docstring / log 字串 / argparse description。
   - ✅ **合法跨專案引用(保留,不可改)**:`絕不碰 EditX/EditY 別專案`、`production-proven EditX/EditY 佈局
     一致`、歷史 lineage(`種子來自 EditX step_N`、`EditX job 97266 佔滿 cap`)、修復史(`原本誤抄 EditX 的
     editX- 服務`)。改了會讓語意錯誤或破壞跨專案安全說明。
3. `systemctl --user is-active editX-*`(別專案現役服務)→ 移植全程**不可碰**。

## Step 1 — 建立本專案 `PREFIX`* unit 範本(chain_code/systemd/)
4 個檔,`WorkingDirectory` + `ExecStart` 全用**本專案絕對路徑**:
- `editM-dispatcher.service` → `ExecStart .../chain_code/submit_dispatcher.sh`(Type=simple、Restart=on-failure、RestartSec=15)
- `editM-watcher.service` → `.../watcher/hill_watcher.sh`(同上)
- `editM-watchdog.service` → `.../chain_code/health_watchdog.sh`(Type=oneshot)
- `editM-watchdog.timer` → `OnCalendar=*:0/10`、`Persistent=true`、`OnBootSec=2min`
> ★`submit_dispatcher.sh` / `hill_watcher.sh` 用 self-relative `PROJECT_ROOT="$(cd "$DIR/.." && pwd)"`
> 推導專案根,**本身不需改**;只要 unit 的 ExecStart 用本專案絕對路徑 + WorkingDirectory 指本專案即可。

## Step 2 — 移植 3 核心腳本 + 所有功能性自指檔
- `install_systemd.sh`:`ROOT=本專案`、`cp editM-*` 範本、`enable --now editM-*` + `editM-watchdog.timer`;
  ★加註解「只 enable/管理 editM-*,絕不碰別專案 editX-」。
- `health_watchdog.sh`:`ROOT=本專案`、self-heal 迴圈的 unit 清單 + 全部 `systemctl` 都 `editM-`、ALERTS 標題本專案名。
- `daemon_reset.sh`:`ROOT=本專案`、stop/start 服務名 `editM-`。
- 其餘功能性自指檔(`clean_this_node.sh` 的 systemctl stop、`switch_partition.sh` / `dispatcher_watchdog.sh` /
  `tools/osc_check.sh` / `tools/jp_lock_selfcheck.sh` / `animation/gif_watchdog.sh` / `animation/gif_render_loop.py`
  的 ROOT/cd、`submit_dispatcher.sh` / `dispatcher_start.sh` / `handoff_scout.sh` 的服務名註解、`checklist.py` 等):
  對**純自指檔**可 `sed -e 's|EditX_old|THIS|g' -e 's|editX-|editM-|g' -e 's|EditX|ThisShort|g'`;
  對**含合法跨專案引用的檔(如 CLAUDE.md)只換完整名 + 前綴,不換裸 `EditX`**(保護 `EditX/EditY` 引用)。
- 命令檔 `.claude/commands/editX-monitor.md` → `git mv editM-monitor.md` + sweep 內容 + CLAUDE.md 的
  `/editX-monitor`、`editX-monitor.md` 引用同步改名。
- 刪除本專案內無用的 `editX-*` unit 範本(`git rm`;**別專案自己 repo 的範本不受影響**)。

## Step 3 — 驗證(全過才啟用)
- 殘留掃描:`grep -rnE 'editX-|EditX_old'`,只剩刻意的合法跨專案引用(EditX/EditY 別專案、歷史 lineage、install 註解)。
- 語法:`bash -n` 所有 .sh、`python3 -m py_compile` 所有 .py。
- `ExecStart` 目標檔都存在;`WorkingDirectory` 全指本專案。
- ★防呆:`grep -rE 'systemctl.*editX-|cp .*editX-'` 確認**無任何操作別專案 editX- 服務**。
- **codex 單發驗證**(`</dev/null` 防 stdin hang;單發 `codex exec` 安全,勿用 rescue agent 重試風暴弄壞 ~/.codex):
  ```
  timeout 300 codex exec --skip-git-repo-check "<review prompt>" </dev/null > /tmp/codex.out 2>&1
  ```
  prompt 要它驗:unit 路徑/WorkingDirectory/ExecStart 全指本專案、install/watchdog/daemon_reset 無跨專案污染、
  功能性殘留=0、合法跨專案引用保留、bash -n/py_compile 過、命令改名一致。NEEDS-FIX 就修到 PASS。

## Step 4 — 啟用
- `bash chain_code/install_systemd.sh` → enable `editM-dispatcher`/`editM-watcher` + `editM-watchdog.timer`。
- 驗:`systemctl --user is-active` 三者 active;`list-timers editM-watchdog.timer` 有下次巡檢;
  daemon 收編單一(`/proc/PID/cwd == 本專案`);**別專案 editX- 服務狀態不變**(未被碰)。
- ★若本專案已有 plain-process daemon 在跑:install 後 systemd 會接管;watcher 的 cross-node 鎖 +
  self-eviction 會自動收斂成單一,**免 SSH**(跨節點殭屍 ~60s 自滅)。

## Step 5 — gitpush + git fetch origin
- 逐檔 `git add`(**禁 `-A`**);commit 繁中(真因 + 移植範圍 + 驗證);`git push`;`git fetch origin` 回報 ahead/behind。
- 非 fast-forward 被拒 → 先回報讓使用者決定,**不可 `--force`**;三大紀錄檔(Ustar_Force_record/timing_log/checkrho)不碰。

## 守門(MUST)
- 全程**只操作當前專案**;絕不 `systemctl`/`cp`/`rm` 別專案 `editX-*` 服務或 unit 檔;絕不 stop/disable 別專案服務。
- 殺/數 daemon 用 `pgrep` + `/proc/PID/cwd` 判歸屬,**絕不 `pkill -f`** / cmdline 路徑字串。
- **合法跨專案引用一律保留**(EditX/EditY 別專案、歷史 lineage、修復史、production-proven 佈局),不可 blanket sed 破壞。
- 沙箱擋裸 `sleep`(exit 144)→ 用 `python3 -c "import time;time.sleep(N)"`;`cd` 子目錄會讓 write_guard 把子目錄當專案根 → 編輯前 `cd` 回專案根。

## 背景知識
- 為何要做:Edit11 實測發現 `install_systemd.sh` / `health_watchdog.sh` 是 Edit6 逐字拷貝(`ROOT=Edit6`、
  `enable edit6-*`),從 Edit11 跑會**啟動/接管 Edit6 服務 = 跨專案污染**(違反全域第一條 Job 隔離)。
- 為何安全網仍在:chain 續跑靠 **jobscript 計算節點自投**(不靠任何 login daemon);systemd watchdog 是
  「daemon 死了自動重啟」的加值層(login node 每 10 分自查,免 Claude / 免 API / 免疫 rate-limit + session 關閉)。
- 參考實作:Edit11 commit(本次)— 13 自指檔 sweep + CLAUDE.md 保護式換 + 命令 `/edit6-monitor`→`/edit11-monitor`
  + 刪 edit6-* 範本 + 建 edit11-* unit;保留 hill_watcher_start.sh 修復史 / jobscript Edit6/Edit9 佈局 / phase2 lineage。
