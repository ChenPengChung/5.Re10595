# CFDLab 本地 Dispatcher(`cfdq`)實作說明

> NCHC 用 SLURM 當系統級排程器(`chain_code_nchc/` 那套 dispatcher 是它的)。
> **CFDLab 沒有任何系統排程器**,所以「dispatcher」這個角色由 user-space、純 SSH 的
> **`cfdq` daemon** 整包重新實現:自己 probe 節點、搶下、起跑、看顧、死了換節點續跑。
>
> 線上版本 = `~/bin/cfdq`(與 repo `watcher_local/cfdq` 位元組相同)。本檔依實際程式碼撰寫,
> 行號指向 `watcher_local/cfdq`、`watcher_local/cfdq-probe.sh`、`chain_code_local/hill_local_chain.sh`。

---

## 1. 角色對照:NCHC dispatcher → CFDLab 怎麼落地

| 機制 | NCHC(SLURM) | CFDLab(`cfdq`) |
|---|---|---|
| 排程器本體 | SLURM(系統級)+ `chain_code_nchc/submit_dispatcher.sh` 跨分區派工 | **無系統排程器** → `cfdq daemon` 自己當排程器,SSH 主動 probe+搶節點 |
| 提交單位 | `sbatch chain_code_nchc/jobscript_chain.slurm.{H200,GB200}` | `cfdq add … -- bash chain_code_local/hill_local_chain.sh` |
| 資源池 | partition `h200` / `gb200` | `/etc/hosts` 的 `CFDLab-*` 節點,**只認 V100** |
| 一段執行 | `jobscript_chain.slurm.*` | `chain_code_local/hill_local_chain.sh` |
| 續跑觸發 | walltime 到 → `--signal=USR1@120` → SLURM requeue | 被搶/崩潰 → exit-code 契約 → cfdq 重排佇列 |
| 監看 | `squeue` / `sacct` | `cfdq ls` / `cfdq nodes`(SSH probe) |
| 常駐方式 | systemd `edit6-dispatcher.service` | **母機 tmux 手動 `cfdq daemon`**(非 systemd) |

> 核心差異:NCHC 是「提交給排程器,排程器決定放哪」;CFDLab 是
> **cfdq 自己 SSH 進去看哪台空、搶下、起跑、看著它、死了重排** —— 排隊 / 放置 /
> 續跑全在 user-space 完成。

---

## 2. 元件

| 元件 | 位置 | 角色 |
|---|---|---|
| dispatcher daemon | `~/bin/cfdq`(= `watcher_local/cfdq`) | 單例 daemon,佇列 + probe + 搶節點 + 看顧 + 重排 |
| per-GPU probe | `~/.local/bin/cfdq-probe.sh`(= `watcher_local/cfdq-probe.sh`) | SSH 到單一節點執行,回報健康/型號/GPU 佔用/我方 job liveness |
| chain wrapper | `chain_code_local/hill_local_chain.sh` | 在搶到的節點上跑「一段」:冷啟/續跑 + 信號轉發 + exit-code |
| 佇列狀態 | `~/.cfdq/`(`jobs/<id>/`、`daemon.lock`、`daemon.log`、`seq`) | 純檔案系統佇列(NFS 母機可讀) |
| 本地編譯 | `chain_code_local/build_local.sh` | sm_70 V100 binary → 專案根 `./a.out` |
| 本地監看 | `chain_code_local/watch_local.sh` | 純讀 log/checkpoint/`cfdq ls`,無 python |

---

## 3. daemon 主迴圈(`cfdq:316-323`,預設每 `INTERVAL=20s` 一輪)

每輪 `touch daemon.lock/alive`(心跳)後做兩件事:**`reconcile` → `schedule`**。

預設參數(`cfdq:26-29`):`INTERVAL=20s`、`DEBOUNCE=2`、`LOST_TTL=180s`、`MEMFREE=1024MB`。

### 3.1 `schedule` — 搶節點(`cfdq:255-303`)
1. 候選節點 = job 的 `--nodes`,否則 `/etc/hosts` 的 `CFDLab-*`(`cfdq:64-69`)。
2. **平行 SSH** `cfdq-probe.sh` 到每台 → 回傳管線格式 `H|M|G|A|L|END`;
   **最後一行非 `END|` 就整包丟棄**(防 SICK / 逾時的半截輸出)(`cfdq-probe.sh:2-17`)。
3. `is_fully_free`:健康 + 有 GPU + **零外人 compute-app** + 每張卡 `mem_used<MEMFREE`(`cfdq:88-96`)。
4. **debounce**:連續 `DEBOUNCE` 輪全空才搶(`FREE_STREAK`,`cfdq:278-282`)→ 擋別人 MPI 重啟瞬間的假空檔。
5. **V100 硬過濾**:`node_ok_for` 比對 `model==V100`(`--model V100`)且 GPU 數 ≥ `np`;非 V100 直接跳過 → 「寧願等」(`cfdq:98-104`)。
6. 已被自己 job 佔用的節點 → 排除;一輪一節點。
7. `launch_job`(`cfdq:128-160`):搶前**再 probe 一次**確認仍空 → SSH
   `cd $cwd && nohup <job cmd> &`(本專案 = `bash chain_code_local/hill_local_chain.sh`),
   抓 `pid | /proc/pid/stat 第22欄 start-time`(防 PID 重用);
   **先寫 launchepoch 再抓 pid**(啟動視窗崩潰可回復)。

### 3.2 `reconcile` — 看顧執行中(`cfdq:213-251`),判定優先序:
1. wrapper 寫到 NFS 的 `exit` 檔 → **權威**。
2. launching 殘局 → `pgrep -f hill_local_chain.sh` adopt 回來(`recover_launching`,`cfdq:195-210`;
   注意 cfdq 用 **basename** 比對 cmdline,新路徑 `chain_code_local/hill_local_chain.sh` 仍命中)。
3. liveness probe(`L|pid|alive` + start-time 相符)→ 更新 `lastalive`;
   pid 不見且無 exit 檔 → `killed`;節點失聯 > `LOST_TTL` → `node-lost`。

---

## 4. 續跑契約(cfdq ↔ `chain_code_local/hill_local_chain.sh` ↔ `a.out`)

`finish_job`(`cfdq:177-192`)依 wrapper 回傳碼決定停 / 續:

| exit | 意義 | cfdq 動作 |
|---|---|---|
| `0` | 收斂 / `cfdq rm`(SIGUSR2)乾淨停 | **停鏈**(done) |
| `124` | 優雅被搶(SIGUSR1 → checkpoint)或崩潰 | **重排佇列** → 下一台空 V100 續 |
| `42` | 設定致命錯誤(bad argv 等) | 停(failed) |
| 其他 / killed / node-lost | 異常死亡 | 先 `cleanup_orphans`(USR1 → 等 18s → `kill -9` 清殘留 `a.out`,否則孤兒佔住節點會斷鍊,`cfdq:167-176`)→ 重排 |

**斷鍊續跑怎麼接上**:重排後,下一次 `launch_job` 起的
`chain_code_local/hill_local_chain.sh:52` 會自動挑最新 `restart/checkpoint/step_*` → `--restart=`
(沒有才 `--cold`)。所以 **daemon 不碰 checkpoint**,只負責「重排 + 換節點」,
resume 由 wrapper + solver 自理。

> 注意:`hill_local_chain.sh` 位於 `chain_code_local/`,腳本內 `cd "$(dirname …)/.."`
> 先回到專案根,`./a.out`、`restart/`、`run_local_*.log` 才會落在專案根(= cfdq 記的 cwd)。

手動觸發:
- `cfdq yield <id>` — 送 SIGUSR1 給 `a.out` → 存檔 exit124 → 換節點續(`cfdq:424-431`)。
- `cfdq rm <id>` — 送 SIGUSR2 → exit0 → 停鏈(`cfdq:408-422`)。

> GPU 排列(`chain_code_local/hill_local_chain.sh` 的 GPU 段):8×V100(DGX-1)用全 NVLink
> 流向鏈 `CUDA_VISIBLE_DEVICES=0,4,5,1,2,6,7,3`,避開 GPU3↔GPU4 那條不通的 P2P。

---

## 5. 本專案操作清單(Edit13 用 cfdq 起跑)

```bash
# 0) 先在本環境(有 numpy/scipy 的母機)備妥網格 + 編 V100 binary
cd ~/5.Re10595/Edit13_2800ITBLBM
python3.12 J_Frohlich/grid_zeta_tool.py --auto        # variables.h 改了參數才需重生
bash chain_code_local/build_local.sh                   # 產 ./a.out (sm_70)

# 1) 排 job(cwd 會被記成本專案根)
cfdq add --np 8 --model V100 --exclusive --chain --name edit13 -- bash chain_code_local/hill_local_chain.sh

# 2) 母機 tmux 常駐 daemon
tmux new -s cfdq
cfdq daemon            # 預設 interval=20s, debounce=2;Ctrl-b d 離開
# 更積極:cfdq daemon --interval 10 --debounce 2

# 3) 監看
cfdq ls                # 佇列 / 執行中(NODE/PID、elapsed)
cfdq nodes             # 即時節點快照(誰空/誰非 V100)
cfdq log daemon -f     # 搶佔 / 續鏈 / 失聯 事件
cfdq log <id> -f       # solver 進度
bash chain_code_local/watch_local.sh   # 純文字滾動監看(log+checkpoint+cfdq)

# 4) 停 / 續
cfdq yield <id>        # 優雅被搶 → checkpoint → 換節點續
cfdq rm <id>           # 乾淨停(不續鏈)
cfdq stop              # 停 daemon(執行中 job 不受影響)
```

---

## 6. 邊界與雷(MUST)

- **cfdq job 的歸屬看 `cwd`,不看 name**:`cfdq ls` 顯示的 job 可能屬於別的專案
  (例如 `name=edit11` 的 0001/0002 其 `~/.cfdq/jobs/<id>/spec` 的 `cwd=cfdtest/Edit11_local`)。
  動任何 job 前先 `cat ~/.cfdq/jobs/<id>/spec` 確認 `cwd=` 是本專案,**絕不**碰別專案的 job/資料。
- **CFDLab dispatcher 不走 systemd**:`chain_code_nchc/systemd/edit6-*` 是 NCHC 的,在此**休眠**;
  本地 daemon 是母機 tmux 手動常駐。
- **`chain_code_nchc/` 整套 = NCHC**:其硬編路徑 ROOT 已從舊的別人專案改成本專案
  (`/home/chenpengchung/5.Re10595/Edit13_2800ITBLBM`),但**內部彼此引用仍指舊資料夾名**
  (本機優先,未一併改名),故在本地是休眠/未完全接線狀態;本地流程不呼叫它,
  **絕不**把 cfdq 指到 `./run`(那是投 NCHC SLURM)。
- **只吃 V100**:`--model V100` + binary 只編 sm_70 → 雙保險,永不誤跑 P100;沒有空 V100 就一直等。
- VTK/checkpoint 綁每萬步(NDTBIN/NDTVTK),home 空間吃緊,測完清 `result/*.{vtk,bin}`
  (見 `run_in_CFDLAB.md` §5;**勿**誤刪 `phase2_generatecheckpoint/` 的累積統計種子)。

---

> 操作步驟(排 job / 起 daemon / 監看 / 停)見 `run_in_CFDLAB.md`;本檔專講
> 「dispatcher 機制如何實現」。兩者互補。
