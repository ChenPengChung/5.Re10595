# PLAN — 把 Edit8 的「s0.X→s0.Y regrid 整套流程」套用到另一專案(以 Edit7 為例)

> 用法:把**本檔全文**貼進**目標專案自己的 Claude Code session**(例如 Edit7_10595SNS 的 session),
> 當成 `/goal` 或 `/plan` 的輸入。**不要**在別專案 session 操作本專案;跨專案隔離。
> 先把下方〔填空〕全部填好(或叫該 session 先自己探勘填),再執行。

---

## 0. 〔填空:目標專案參數〕(執行前先確認)

| 變數 | Edit8 範例值 | 你的目標專案填 |
|---|---|---|
| 專案根 `$ROOT` | `/home/s8313697/5.Re10595/Edit8_NewInterpolation` | 〔Edit7 根〕 |
| 目前網格 STRETCH_A `s_old` | 0.80 | 〔讀 variables.h〕 |
| 目標網格 STRETCH_A `s_new` | 0.95 | 〔你要的細網格〕 |
| 目標 dz_min | <8.87e-4(得 8.6e-4) | 〔你的壁面解析需求〕 |
| 分割 jp / 網格維度 | jp=64, NX×NY×NZ=449×897×449 | 〔讀 variables.h〕 |
| 帳號/分區鎖 | MST115169 / 64gpus@64(LOCK_COMBO) | 〔讀 restart/LOCK_COMBO〕 |
| 源 checkpoint(插值來源) | `phase2_generatecheckpoint/oldcheckpoint_*`(257×129) | 〔該專案的 origin〕 |

**前置確認**:① 該專案是否「已退役」?要 regrid = 重新啟用,先確認。② 是否有 active chain job 卡著要保的 slot?
③ 該專案是否有同款 `chain_code/`(run.sh / build_and_submit / dispatcher / jobscript)、`phase2_generatecheckpoint/interp_checkpoint.py`、
`J_Frohlich/grid_zeta_tool.py`?**缺任一 → 本流程不適用,停。**

---

## 1. 目標 / 不可違反的守門

**目標**:把 live chain 從 `s_old` 網格換成更細的 `s_new`,**完全保流場語意**(warm from 新 step_00000001)、
**舊模擬輸出全刪不汙染新跑**、**全程守住帳號的 GPU slot**、**partition@jp 鎖(LOCK_COMBO)絕不動**。

**守門(MUST)**:
- `scancel` 只用 `./run job-guard`;停鏈只用 `./run job-guard stop-chain`;不在跑著的原目錄重編 a.out。
- `restart/LOCK_COMBO` **硬保護**:校驗 == 既有值,異常即中止,全程不刪不改。
- **僅 warm**,禁 `--force-cold`。只操作當前專案。

---

## 2. Phase A — 隔離準備(原 job 不動、續卡位)

在**隔離 git worktree** `$STG = git worktree add --detach <同檔案系統路徑> HEAD` 內做全部重活,主目錄一個位元不碰。

1. **生細網格**:`grid_zeta_tool.py` 用 `gamma_to_minSize` 反推達標的 `s_new`(`GAMMA=log((1+s)/(1-s))`,
   `dz_min=(LZ−H_hill)·min(Δζ_Vinokur)`);在沙盒 variables.h 設 `STRETCH_A=s_new` + 寬 `RATIO_HI`(若 ratio>限),dev job 生成
   `adaptive_..._s{s_new}.dat`。驗 dz_min < 目標。
2. **worktree 帶出已追蹤檔的坑**:worktree 會 checkout 追蹤檔(舊 newgrid、metadata-only 的 origin 殘骸)。
   **務必**:phase1 只留「一個 oldgrid + 一個 s_new newgrid」(刪 worktree 內的 s_old newgrid);origin 用 **symlink 指主目錄完整源**
   (worktree 的 origin 只有 metadata、缺 .bin);`$STG/restart/checkpoint/` 開跑前**無任何 step_***(否則 interp --auto 跳過)。
3. **$STG/variables.h** = 主目錄**現行** variables.h(含所有 live 參數)**只改 STRETCH_A=s_new**。
4. **dev 生成 step_00000001**:`interp_checkpoint.py --auto --step 1`(`POISSON_SERIAL=1`)。
   **★ DIV-GATE 浮點地板坑**:細網格的 div-exact 投影浮點地板可能 `max|div(u*)|≈1.2e-12 > 1e-12` gate(粗網格本來 9e-13 過)。
   **tighten `--projection-div-tol` 沒用**(已過度收斂)。修法:**兩道 gate 都放寬到 2e-12**(文件化理由:√N·ε 浮點地板) —
   CLI `--div-gate-tol 2e-12`(gate1, interp:~3841)+ **改 $STG 副本** `fneq_div_gate_tol = 2.0e-12`(gate2 硬編, interp:~3958)。
5. **隔離重編 binary**:`cd $STG && ./run build H200 --build-only` → **手動 `cp a.out a.out.H200`**(build-only 不自動同步)。
6. **驗種子**(切換前,不合格不換):完整性(f/rho 數 + 每檔 byte size + finite);**F\* 逐字元相同**(force 是純量、interp 複製);
   **U\* 體速度 Ub** interp 強制 = 源場(residual≈0;div 投影會微調 ~0.03%,可忽略);場為重採樣(不同網格本就點對點不同)。
7. **agent 死盯**:整個 Phase A monitor 確認原 job 只寫主 restart、$STG 乾淨、主 variables.h/a.out.H200 mtime 不變。

---

## 3. Phase B — 秒級原子切換(只在 Phase A 全備妥後;此刻才有 slot 空窗)

寫成**一支 DRY-RUN 預設的腳本**,內含逐閘 `die` + 備份 + 回滾 trap。順序與**已驗證的坑修**:

- **B0 前置驗證**(全綠才動):LOCK_COMBO 校驗;**genseed 用 `sacct State==COMPLETED` 判定**(別用 `squeue -j` 退碼 fail-closed
  — 已 COMPLETED+離隊的 job squeue 回非零會誤擋);種子 f/rho/metadata 完整 + `$STG/grid_provenance` 非空;binary/grid 就緒;
  主目錄無 step_00000001;**head jobid 動態讀 `restart/chain_jobid`**(chain 輪界會變,別硬編)。
- **B1 徹底停 dispatcher**:`dispatcher_stop.sh --kill-now`,等 PID 死,**再用 `dispatcher.heartbeat` 變 stale(>2×POLL_INTERVAL 不動)
  證死**(跨登入節點安全,kill -0 打不到別節點);清**根目錄** `DISPATCHER_ACTIVE`(run.sh 的 guard 在根、非 restart/)。
- **B2 優雅停 head**:`./run job-guard stop-chain`(solver 寫 final + RC=0 不自我續投)。
  **★ set -e 坑**:等待迴圈 `st="$(squeue -j $head ...)"` 在 head 完成時(squeue 退非零)**會在 set -e 下提早 abort**(在你的 fail-closed 之前)。
  **必須** `st="$(squeue ... || true)"` + 用 `sacct State` 判終態。等 squeue 空 + 無 `.WRITING`。
- **B2.5 清舊模擬輸出(你的需求:舊全刪、新不汙染)**:`result/*.vtk *.bin *Final.vtk` +
  **★ glob 漏網坑**:還要清 `*.vtk.tmp`(半寫檔)、**子目錄** `result/_slice_cache/`(maxdepth 1 掃不到)、
  `result/_slice_cache/geom_*.npy`(**mesh 座標 cache,key 不含 STRETCH_A → 細網格 false cache-hit 用錯座標**)。
  另清 statistics/ gilbm_metrics_full.dat mesh*.DAT 舊 slurm_*.log live/*.png。
  **三大紀錄檔(checkrho/Ustar/timing)** 依需求**重置**(move 到 backup 再讓 solver 新建)或保留。
  **保留**:result/*.py(tracked)、DNS *.dat、原始碼、LOCK_COMBO、grid_provenance。
- **B3 原子換**(全 mv/cp,同檔案系統瞬間):先**備份**(variables.h/a.out.H200/grid_provenance/chain 狀態 → BK,失敗即 fatal);
  s_old checkpoint **移開**到 `checkpoint_s08_waste_*`(可逆)→ `mv $STG/.../step_00000001` 進主;
  binary cp + md5 斷言;grid cp + NEW==SOLVER 雜湊一致;variables.h `STRETCH_A=s_new`;
  **`grid_provenance` 全欄位重指主目錄路徑 + 重 stat 所有 mtime**(用 awk 逐欄、別 sed 全域路徑;run.sh Preflight C 驗
  origin/variables_h/new_grid/old_grid mtime,只改一欄會 FATAL);重置 chain 狀態(rm chain_count/jobid/STOP_*/HEAD.lockdir,**不碰 LOCK_COMBO**)。
  **★ .run.lock 坑**:舊 nan_monitor 常殘留持有 `.run.lock` → 下一步 `./run` 會被 "lock 被佔用" 擋。
  **先 kill 持有者(`lsof -t .run.lock`)+ `rm -f .run.lock`** 才 `./run`。
- **B4 warm 投遞**:`./run --no-queue-check`(內含 Preflight C;`--no-build` 用已備 binary;**禁 `--force-cold`**)。
  用 `comm`(投前/投後 squeue 差集)抓**確有新 jobid**,無則立即人工查(slot 風險)。
- **B5 重啟 daemon**:`./run dispatcher start` + watcher 啟動器。

**★ slot 空窗 / 卡位**:帳號 cap 下停 head→投新 job 之間有**無法消除的秒級~數分空窗**。實測(Edit8):即使因故空窗 ~2-3 分,
**快速重投仍搶回原節點**(NCHC 把剛釋出的 GPU 給回同帳號)。緩解:重活全 Phase A 備妥,B 只剩 mv+sbatch;備份+waste 可回滾。

---

## 4. 驗證(切換後)+ agent 審查

1. **warm-load 三閘門**(`slurm_<新jid>.log`):`Restart from step_00000001`(非 cold)、`[G6] Schema OK grid=match`、
   `[Phase5] dt_global consistent`(新 dt 隨細網格自洽)。**無 Preflight C FATAL**(provenance mtime 對齊)。
2. **流場**:U\*≈1.0、Re 對、Force 保留、Ma_max 穩、無 NaN、checkrho~1.0、accu=0(未到統計門檻)。
3. **單一 job + 守 slot**:squeue 僅一個本專案 job、正確 partition/account/nodes、沒被別帳號搶。
4. **交 agent 獨立審查清理乾淨度**:遞迴掃 result/ 等,確認**零 s_old sim-output 殘留**(含 .vtk.tmp / _slice_cache / geom cache);
   tracked .py/DNS 保留;紀錄檔重置+備份;checkpoint 只剩 step_00000001。**反覆確認 + monitor 持續盯**。

---

## 5. 一鍵化建議 + 失敗回滾

- 把 Phase B 寫成 `phaseB_full_swap.sh`(DRY-RUN 預設,`--apply` 才動),**先 DRY-RUN 給人看全綠**再 `--apply`,並交 codex/agent
  複檢(本案 codex 跑了 3-4 輪才補齊:dispatcher 跨節點證死、set -e、種子強驗、awk provenance、回滾)。
- **--apply 中途死**:B0-B3 配置換好、只差 B4/B5 → 清 .run.lock → `./run --no-queue-check` → daemon。**別重頭跑**(B0 會擋撞名)。
- 失敗料留在 `restart/phaseB_backup_*`(config)+ `restart/checkpoint_s08_waste_*`(舊 checkpoint),可手動還原。

> 本流程實證來源:Edit8 於 2026-06-06 完成 s0.8→s0.95 切換(job 81499 warm@64gpus,雙 agent 確認零污染)。
> 對應 Edit8 腳本可對照借鑑(唯讀):`Edit8_NewInterpolation/grid_experiment_s095/{phaseB_full_swap.sh,verify_seed.sh,genseed_s095.slurm,phaseB_checkpoint_swap.sh}`。
