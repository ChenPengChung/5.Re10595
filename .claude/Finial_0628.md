# NCHC 6/28 復機「一鍵搶位暖續投三專案」定稿（Finial_0628）

> **定稿說明**：本檔為 `Edit11_NCHC_GRAB_PLAN.md` × `Edit12_NCHC_GRAB_PLAN.md` 經 **Codex 五輪複檢 + 四輪雙向交叉比對** 收斂後的單一乾淨版。**2026-06-25 三方勘驗修正版**（自檢 + Workflow 7 透鏡 + codex 對真碼取證後補 6 缺口，見文末修正紀錄）。
> **本檔是「DESIGN 設計稿」，`lbm-grab`/FIX-C/RESUME-ARM 尚未實作**（grep 真檔找不到，只在計畫/log）；實作時照本檔走 L0→L6。
> **範圍**：僅「復機後投遞搶位」(`lbm-grab`)；斷線前停機保檔 (park) 不在本檔（靠既有 chain 週期 checkpoint）。
> **產出**：relauncher 本體只動 `~/.bashrc`；另有兩組「復投前一次性 config 前置」由使用者執行（見 §3）。

---

## 1. 情境與目標

NCHC 維護 **6/27 09:00 停機 → 6/28 14:00 復機**，kill 所有 RUNNING job，復機後叢集高度競爭。
復機一登入打 **`lbm-grab`** 一個指令 → 三個 chain-job **同時暖續投搶位**、各自鎖 partition@jp、起 dispatcher(第一層)+ watcher + watchdog(第二層)，從最新且完整 checkpoint 續跑，**零資料損失、不雙頭**。

## 2. 三專案註冊表（單一真相，資料驅動）

```
# ROOT|CHAINDIR|WATCHERDIR|PREFIX|JP|PARTITION|ACCOUNT
/home/s8313697/5.Re10595/Edit11_Krank5600 |chain_code     |watcher     |edit11-|64|64gpus|MST115169
/home/s8313697/5.Re10595/Edit12_Krank56002|chain_code     |watcher     |edit12-|32|64gpus|MST114348
/home/s8313697/5.Re10595/Edit13_2800ITBLBM|chain_code_nchc|watcher_nchc|edit13-|32|16gpus|MST115169
```
- **Edit13 路徑分歧**：`chain_code_nchc` / `watcher_nchc`（不可硬編前兩個的 `chain_code`）。
- **GPU 帳**：MST115169 = Edit11(64,64gpus)+Edit13(32,16gpus)；MST114348 = Edit12(32)。per-user 上限 128，**三者剛好填滿、零裕度**（期間不可再投第 4 個 job）。
- **⚠️ Edit13 16gpus PENDING 風險**：`p_16gpus` per-account cap=32 **帳號共用**，live 已有他人 job（`u8035407` 110088, 32 GPU）佔用 → Edit13 復機後**很可能仍 PENDING(MaxGRESPerAccount)**。換 16gpus 的淨效果＝**walltime 48h、消除 dev 4h churn；但首投落地不保證**。PENDING=進度安全、**使用者勿手動 scancel**。

## 3. 復投前一次性 config 前置（不在 `lbm-grab` 內；使用者手動，relauncher 只 P2 唯讀驗證）

### 3a. Edit13 換鎖 `dev@MST114348@4h → 16gpus@MST115169@2d`（jp32 不變）
**時點硬性規定**：**在「復機後、Edit13 已被斷線殺掉(無活鏈)」時做**。
- 絕不可停機前改：否則 Edit13 下次 dev 4h 自投（斷線前）就用新 config 投出＝提前換鎖。
- 若非得停機前改：須同時 `./run job-guard stop-chain` 凍住 Edit13、`systemctl --user stop edit13-dispatcher.service edit13-watchdog.timer`（dispatcher 啟動只 source selector 一次，活著改不生效）。

**要改的檔（逐項 verify→change，冪等）**：
1. `chain_code_nchc/jobscript_chain.slurm.H200`：`--account=MST115169`、`--partition=16gpus`、**`--time=04:00:00 → 2-00:00:00`**。（**GB200 jobscript 不動**——它是 `--partition=gb200` 的另一 cluster。）
2. `restart/h200_partition = 16gpus`。
3. `chain_code_nchc/tools/select_combo_lib.sh`：`SC_ACCT→MST115169`、`SC_PARTITIONS→16gpus`。
4. `chain_code_nchc/submit_dispatcher.sh`：`ACCOUNT default→MST115169`（**★這才是實際 sbatch 送出的 account**，獨立於 SC_ACCT；SC_ACCT 只是 selector 探測可用性用）。
5. `chain_code_nchc/install_systemd.sh`：`:22` 每次 reinstall 把 `h200_partition` 覆寫回 `dev` → 改 16gpus（revert 風險在 watchdog 重裝時，非 arm 步）。
6. `chain_code_nchc/tools/partition_lib.sh` / `partition_ctl.sh`：allowlist/cap（`16gpus=32` 已合法）。
7. **`chain_code_nchc/tools/jp_lock_selfcheck.sh`（★FIX-1 補全：不只 EXP_PIN/EXP_PARTS）**：
   - `EXP_PIN`(:26)、`EXP_PARTS`(:28)（現 `dev`/`32gpus`，互相矛盾）**及註解**一致改 `16gpus@MST115169`。
   - **★該檔 check 6 還有 `binary_manifest.dat` jp32 md5 相等檢查(:62-65)、漂移即 exit≠0(:79)**。**Edit13 無 `restart/binary_manifest.dat`** → 此分支會 **false-drift hard-fail 擋續跑**。**必須讓它在 manifest 缺失時放行**（鏡射 Gate-B：`mman=$(...); [ -z "$mman" ] && ok+=("manifest 缺→跳過") || 比 md5`）。**漏這條 = Edit13 換鎖後 selfcheck 仍 FATAL**（三方勘驗最高 blocker）。
8. `edit13-dispatcher.service`：可選 `Environment=SC_ACCT=MST115169 SC_PARTITIONS=16gpus`（已裝 unit 無此行 → 須編輯 + `systemctl --user daemon-reload`）；**或**只改 #3/#4 script default（較簡單，免動 unit）。
9. **其他 hardcode `dev` 的操作檔（★FIX-5；★Edit11 session live 校正檔案歸屬）**：
   - **`switch_partition.sh:31` `LOCKED_PARTITION="dev"`**（live 確認；不改 → switch 非 dev 會 FATAL）。
   - **`tools/partition_lib.sh:40` `h200_known_partitions() { echo "dev"; }`**（live 確認；★allowlist **只回 dev** → 16gpus 不在 known set,partition 驗證會拒；§3a #6 的「16gpus=32 已合法」只講 cap、**沒講這個 allowlist**,必須一併改成 16gpus）。
   - `run.sh:323` `_RUNSH_CANDIDATES` default 含 `H200:dev`、`dispatcher_status.sh:85` 候選 default、`build_and_submit.sh.H200` fallback → 改 16gpus 或確認 `--h200`+`SC_PARTITIONS` pin 覆蓋。
   - **★校正**:`h200_known_partitions()`/`LOCKED_PARTITION` **在 `switch_partition.sh`+`partition_lib.sh`,不是 `run.sh`**(三方勘驗原寫 run.sh 有誤;.bak.lockdev 是換鎖前備份)。

**驗收**：`grep -rn 'MST114348\|\bdev\b\|32gpus' chain_code_nchc` 無殘留舊鎖（除歷史註解、GB200）。warm 安全（jp32 不變→不重編、不丟統計）。

### 3b. Edit12 dispatcher-default 修正（★FIX-4 釐清：真正漂的是 partition）
`Edit12_Krank56002/chain_code/tools/select_combo_lib.sh`：**`SC_PARTITIONS 32gpus→64gpus`（這是實際 partition 漂移，必改）**；`SC_ACCT MST115169→MST114348`（一致性也改，但 **SC_ACCT 只是 selector 探測用、實際 sbatch account 走 `submit_dispatcher.sh` 的 `ACCOUNT`(=MST114348 已對)**）。可改 default 或 unit `Environment=`。不改 partition → Edit12 dispatcher 重投漂到 32gpus。

### 3c.（獨立 fix，不綁進換鎖）selector `%b` parser
三專案 `select_combo_lib.sh` 的 `%b` parser `gpu:[0-9]+` 對 `gres/gpu:H200:8` 算 0 → 改「取**末段** `:` 數字」（awk：`m=split($2,p,":"); if(p[m]~/^[0-9]+$/) g=p[m]+0`，**只對 squeue `%b` 的 `:` 字串，非 sacctmgr/scontrol 的 `gres/gpu=N` cap 字串**）。Edit11:96/Edit12:99/Edit13:86。是既有 dispatcher 選擇邏輯的潛在 bug，抽獨立 fix + 自己單測。

---

## 4. `lbm-grab` 設計（`~/.bashrc` 函式；手動、冪等；登入只印提醒 banner，不自動執行）

### Pass-0 全域預檢（一次）
1. **叢集真回來**：`sinfo -p 64gpus,16gpus` up + `scontrol show reservation` 無覆蓋本帳號的 active maint 才續。**查不到 / `Operation not permitted` → fail-closed**（要求目視確認，不靜默放行）。
2. **128-GPU 預檢**：`squeue -h -u $USER -o '%i %b %D %Z %T'`；**總 GPU = Σ(per-node `%b` × 節點 `%D`)**（`%b` 是 per-node TRES；parse 取末段 `:` 數字，容忍 `gres/gpu:8` 與 `gres/gpu:H200:8`）。出現非三專案 WorkDir 的第 4 個 job → 警告會擠掉一個、請先清。

### Pass-1a 三專案「投遞並行」（背景 subshell + `wait`，三 sbatch ~1s 全射出＝公平搶位；失敗隔離、無 `set -e`、結果寫獨立 temp）
每專案 subshell：
- **Step-0 存活守衛**（排在任何 mutation/輸出前）：**判 head 活性要雙源（★FIX-2，鏡射 dispatcher 自己的 `chain_has_active_job()` submit_dispatcher.sh:252-273——它明說 `chain_jobid` 在 bad-node requeue 後會 lag、故也讀 `HEAD.lockdir/owner`）**：
  - `restart/dispatcher.heartbeat` 內嵌 **epoch**<300s **或** **（`restart/chain_jobid` **∪** `restart/HEAD.lockdir/owner` 的 jobid）經 `sacct`/`squeue` RUNNING/PENDING** → **ALIVE**。
  - ALIVE 且 daemon heartbeat 新鮮 → **verify-only**（`ALREADY-RUNNING`）。
  - **`RESUME-ARM` 分支**：若 **head 在佇列（任一來源 jobid 在）但 daemon 未武裝(heartbeat stale)** → **先清 `STOP_CHAIN`/`STOP_DISPATCHER`/`STOP_NOCAPACITY` 再進 Pass-1b 補武裝**（VERDICT=`RESUME-ARM`）。
- 否則(無活鏈)：
  1. **P1 只清「stop 哨兵」**：無條件清 `STOP_CHAIN`/`STOP_DISPATCHER`/`STOP_NOCAPACITY`；`DISPATCHER_ACTIVE`(root+restart/) **僅整串數字 PID 驗證(`=~ ^[0-9]+$`) + `kill -0` 確認已死才刪**（壞檔也刪）。**不刪 `HEAD.lockdir`/`dispatcher.heartbeat`/`.run.lock`/`nodelock`** → stale `HEAD.lockdir` 交 `head_lock_lib` acquire 自動 stale-clean（避 NCHC squeue 盲窗手動清造第二頭）。**註：warm 路徑呼叫的 `run.sh:496/514` 自己也會清 stale HEAD.lockdir，但它清前已查 squeue 活性(:513)＝liveness-checked、安全，與「不盲清」一致、非矛盾**（三方勘驗確認）。
  2. **Gate-A 資料完整性**（純唯讀 FS，不用 `./run status`）：**newest-first** 掃 `restart/checkpoint/step_*/`（非只 `readlink latest`；半寫 fallback 較舊 valid），跳 `.WRITING`，**鏡射 jobscript `validate_checkpoint()` 精確 glob**：`f[01][0-9]_*.bin` 計數 == `19×NP`(=f00..f18，jobscript:288-290)、`f00_*`==NP、`rho_*`==NP、`metadata.dat` 非空（`accu_count` 缺=0 不 abort；**`accu>0` 驗 36 個 stat 陣列(:303-310)、`cv>0` 驗 cv_uu/cv_k/cv_ftt_history(:313-318)**）。第一個 valid 即用，全不過才 `ABORT-NO-CKPT`、絕不 `./run`。
  3. **Gate-B binary 就緒**：`a.out`+`a.out.H200`+`a.out.jp<JP>` 在；`binary_manifest.dat` **存在才比 md5（`jpswitch_lib.sh:57 guard / :63 return 0`）、缺失＝放行**（Edit13 本無；**★Edit12 有 manifest → md5 路徑是 live，a.out.jp32 須與記錄 md5 相符否則 reject**）。缺/錯→`ABORT-NO-BIN`。
  4. **warm-start**：`( cd "$ROOT" && ./run --h200 --no-queue-check )` — **必帶 `--h200`**（裸 `--no-queue-check` 只設 MODE_NO_QCHECK、run.sh 仍自選 GB200/dev:323）；**絕不** `--force-cold`/`--rebuild`/pipe stdin。rc0=已投；rc4/5/6/7=「可能已處理」非保證；其他=FAIL。
  5. **FIX-B 確認 head 真進佇列**：poll≤30s（`python3 time.sleep`）`HEAD.lockdir/owner`==新 jobid **且** `squeue/sacct` ∈ {PENDING,RUNNING} 才算搶位成功（撞 stale HEAD.lockdir 須斷言真進佇列）。Edit13 PENDING 屬預期。結果寫 temp（標「待武裝」）。

### Pass-1b 序列武裝（`wait` 後逐專案序列；搶位已在 Pass-1a 完成，序列不損 latency）
6. **FIX-C 守鎖（block-arming，不只 warn；★FIX-3 非對稱有效值）**：武裝**前**核對 dispatcher 後續重投會用的**實際**值 == jobscript：
   - **★account：比 `ACCOUNT`（不是 `SC_ACCT`！）**——實際 sbatch account 走 `submit_dispatcher.sh:109` 的 `ACCOUNT`；`SC_ACCT` 只是 selector 探測可用性，**拿 SC_ACCT 比 jobscript 會 false-abort Edit12**（其 ACCOUNT=MST114348 對、SC_ACCT=MST115169 只是 probe）。有效 ACCOUNT = unit `Environment=`（若設）∪ `submit_dispatcher.sh` default，比 jobscript `--account`。
   - **★partition：比 `SC_PARTITIONS`**——有效 = unit `Environment=`（若設）∪ `select_combo_lib.sh` default，比 jobscript `--partition`。
   - **manager env（`systemctl --user show-environment`）只用來偵測污染**：有設 `SC_*/ACCOUNT` → **硬 `ABORT-MISCONFIG` 或強制 `unset-environment`**（它全 user 共用、會洩漏跨專案，**不可當取值層**）。
   - 不符 → 不武裝、`ABORT-MISCONFIG`（systemd user service 不繼承 shell env → `export` 無效）。
   - 武裝用 **`systemctl --user enable --now ${PREFIX}{dispatcher,watcher}.service ${PREFIX}watchdog.timer`**（units 已裝，**不用 `bash install_systemd.sh`** 以免觸發 dev-revert）。
7. **FIX-D 起跑確認**：poll≤30s `dispatcher.heartbeat`+`watcher.heartbeat` epoch<90s（不用 systemctl，node-local 假死）；穩態另用 HB_STALE~1200s。

### 彙整表（全部收齊後印）
`PROJECT | GATE(ckpt/bin) | JOBID | PART@ACCOUNT(scontrol讀) | STATE | WARM(3/3) | DISP_HB | WATCH_HB | WATCHDOG | ACCU | VERDICT`
- **WARM(3/3)**：grep slurm log `Restart from: …(step=…)`(jobscript:432，非冷啟) / **`[G6] Schema OK … grid=match`**(fileIO.h:630，抓 jp/grid 拓樸不符，Gate-A 檔數驗不到) / `[Phase5] dt_global consistent within 1e-10`(fileIO.h:686)。**PENDING job 無 log → `WARM=deferred`、非 FAIL**（Edit13 最可能；起跑後補驗）。
- **ACCU**：grep **無條件**行 **regex `\[CHECKPOINT\] Loaded: .*accu=`**(fileIO.h:899，**勿吃字面省略號**)；`Statistics loaded` 是 `accu>0` 條件式(fileIO.h:748)、僅補充。PENDING 同 deferred。
- **VERDICT** ∈ {`LAUNCHED`, `LAUNCHED-PENDING`(deferred), `RESUME-ARM`, `ALREADY-RUNNING`, `ABORT-NO-CKPT`, `ABORT-NO-BIN`, `ABORT-MISCONFIG`, `FAIL`}。
- **冪等**：再打全 `ALREADY-RUNNING`/`RESUME-ARM`、每專案僅一 head、無雙投+無累積狀態。

---

## 5. 安全骨架（風險 → 擋法）

**資料損失**
| 風險 | 擋法 |
|---|---|
| `--force-cold` 刪 restart/ | 絕不出現；只 `--h200 --no-queue-check` 暖續 |
| 餵 stdin 誤答 y/N | 絕不 pipe stdin |
| 從半寫 ckpt 續 | Gate-A newest-first + 鏡射 `f[01][0-9]` glob(計數 19×NP)，不過→`ABORT-NO-CKPT` |
| binary 缺 → 冷啟 | Gate-B；warm log 無 `Restart from:` → FAIL 不靜默冷啟；絕不 `--rebuild` |
| 統計 reset | V4 驗無條件 `\[CHECKPOINT\] Loaded: .*accu=` |
| 投到 GB200/dev | warm 帶 `--h200` 鎖 H200 |
| 拓樸不符(jp/grid) | WARM `[G6] grid=match` 抓 |
| Edit13 換鎖後 selfcheck FATAL | §3a #7 讓 jp_lock_selfcheck 容忍 manifest 缺失 |

**雙頭 / 雙投**
| 風險 | 擋法 |
|---|---|
| stale `DISPATCHER_ACTIVE` 擋手動投(exit 5) | P1 整串數字+`kill -0` guarded 清 |
| stale `HEAD.lockdir` → RC=42 | 不手動清，交 `head_lock_lib`/`run.sh:514`(liveness-checked) self-heal |
| dispatcher 先起搶第二頭 | 先投(Pass-1a) 後武裝(Pass-1b) |
| 重跑雙投 / head 在但 daemon 死 | Step-0 守衛(epoch + **chain_jobid ∪ HEAD.lockdir/owner**) → `RESUME-ARM` 補武裝(先清 stop 哨兵) |
| dispatcher 重投漂帳號/partition | FIX-C block-arming(**比 ACCOUNT 與 SC_PARTITIONS**、manager env 硬紅旗) |
| 跨 5 登入節點重複 | nodelock+heartbeat 單例 |

---

## 6. 測試（由小到大；標「現在可安全測 vs 須等斷線」）

| 級 | 目標 | 現在可測 |
|---|---|---|
| **L0 純函式單元** | `_ckpt_ok`(newest-first+鏡射 `f[01][0-9]`+19×NP+accu/cv)/`_bin_ok`(manifest 缺=PASS)/`_gpu_used`(末段`:` parse)/`_dispatcher_cfg_ok`(**account 比 ACCOUNT、partition 比 SC_PARTITIONS**、manager env 紅旗)/`_alive_guard`(**chain_jobid ∪ HEAD.lockdir/owner**)/`_hb_age`；臨時 fixture 不碰真專案 | ✅ |
| **L1 `--dry-run` 全流程** | 零 mutate、無 `chain_status` snapshot、Step-0 守衛排在任何 would-* 前、命令含 `--h200`、Edit13 pre-step dry(含先停 dispatcher + selfcheck manifest 容忍) | ✅ |
| **L2 單一 live(Edit13)** | Step-0 ALIVE→verify-only 無新 jobid；或 head 在(任一來源) daemon 死→`RESUME-ARM` | ✅ |
| **L3 負向 + 失敗隔離** | 無效 ckpt→`ABORT-NO-CKPT`、缺 binary→`ABORT-NO-BIN`、錯有效值→`ABORT-MISCONFIG`(尤其 SC_ACCT≠ACCOUNT 不誤判)、假 ROOT→FAIL 隔離（`LBM_TEST_BAD_ROOT` 注入鉤） | ✅ |
| **L4 三專案全場 live** | 序列武裝、各自鎖定 PART@ACCOUNT、WARM 3/3(PENDING deferred)、ACCU、冪等 | ❌ 須等 6/28 |
| **L5 邊界/持久** | daemon 別節點 heartbeat PASS；FIX-C 重投守鎖；Edit13 換鎖驗（selfcheck 一致+manifest 容忍、install_systemd 不打回 dev、無 dev 死碼/LOCKED_PARTITION 殘留）；跨專案隔離 grep | 部分須等斷線 |
| **L6 回歸** | 交 Codex + Workflow 確認落實 | — |

> **冷啟模擬（清哨兵→真暖投→真武裝→三專案冷啟）斷線前無法安全測**（活體上做會製造 lbm-grab 要修復的 hazard）。

---

## 7. 6/28 復機執行
```
復機登入 → 第一件事:
  # (若 Edit13 換鎖尚未做、且 Edit13 已死) → 先做 §3a 換鎖(含 jp_lock_selfcheck manifest 容忍) + §3b Edit12 partition default + §3c %b parser
  LBM_DRY=1 lbm-grab        # 先看命令流(含 --h200、Edit13=16gpus@MST115169)
  lbm-grab                  # 真跑:三 job 並行暖投搶位 + 序列武裝 daemon
  # 確認 GRABBED/LAUNCHED、WARM 3/3、單頭、o 綠;Edit13:16gpus 若 PENDING 屬正常(勿 scancel)
```

---

## 8. 三方勘驗修正紀錄（2026-06-25；自檢 + Workflow 7 透鏡 + codex；NEEDS-FIXES → 已補）
- **FIX-1（HIGH，最高 blocker）**：§3a #7 `jp_lock_selfcheck.sh` 補「manifest 缺失放行」（:62-65 md5 檢查在 Edit13 無 manifest 時 false-drift hard-fail :79）。
- **FIX-2（HIGH）**：Step-0/RESUME-ARM 判 head 活性用 **`chain_jobid` ∪ `HEAD.lockdir/owner`**（鏡射 submit_dispatcher.sh:252 chain_has_active_job；chain_jobid 會 lag）。
- **FIX-3（HIGH，最微妙）**：FIX-C 非對稱——**account 比 `ACCOUNT`(submit_dispatcher:109) 非 `SC_ACCT`(只 probe)、partition 比 `SC_PARTITIONS`**；否則 false-abort Edit12。
- **FIX-4**：§3b 釐清 Edit12 真正漂的是 `SC_PARTITIONS→64gpus`（account 經 ACCOUNT 已對；SC_ACCT 一致性也改）。
- **FIX-5**：§3a #9 補 `run.sh` 的 `h200_known_partitions()`/`LOCKED_PARTITION` 仍鎖 `dev`（workflow 抓到的額外殘留）。
- **FIX-6**：§3c `%b` parser 精確化（awk 末段 `:`，只對 squeue `%b` 非 cap 字串）+ 三專案行號(96/99/86)。
- 連帶小修：ACCU grep 改 regex `\[CHECKPOINT\] Loaded: .*accu=`(無條件行，非 `Statistics loaded` 條件式)；標明本檔為 DESIGN(未實作)；run.sh:514 自清 HEAD.lockdir 是 liveness-checked 安全(非矛盾)；citation 行號校正。
- **✅ 三方 CONFIRMED 強項**：Gate-A glob+accu/cv、Gate-B manifest 缺放行、locks rc4/5/6/7、STOP-sentinel(含 STOP_NOCAPACITY)、`--h200` 必要性、4 個 WARM/ACCU 字串逐字 verbatim、C12 時點、並行投/序列武裝、跨專案隔離。**無雙頭/資料損失路徑。**

*定稿來源：Codex 五輪複檢 + Edit12 四輪雙向交叉比對 + 2026-06-25 三方勘驗。完整可追溯紀錄見 `Edit11_NCHC_GRAB_PLAN.md` / `Edit12_NCHC_GRAB_PLAN.md`。*
