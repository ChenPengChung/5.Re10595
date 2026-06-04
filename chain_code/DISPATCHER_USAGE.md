# Dispatcher 使用說明 (方案 B: 跨 partition 自動派工)

## 1. 這是什麼

原本的 chain restart (方案 A) 只能綁在**單一 partition** 上：
- `./run.sh` 在 GB200 login → 所有 round 都投 `gb200-dev`
- `./run.sh` 在 H200  login → 所有 round 都投 `16gpus` (NCHC 政策自由切換集 {16gpus,32gpus,64gpus})

**方案 B 新增一個背景 daemon** (`submit_dispatcher.sh`)，在每輪結束後，
自動查 `sinfo` 決定下一輪要丟 `gb200-dev` 還是 `16gpus/32gpus/64gpus`，哪邊閒丟哪邊。

適用情境：
- 24h/48h 級長跑，想盡量塞滿兩個 partition 的空檔
- 不想手動盯著 queue 切換 cluster
- 單一 case 要持續續跑直到自然收斂 / STOP_CHAIN

**不適用**：多 case 同時跑 → 應該每個 case 目錄各自啟一份 dispatcher。

---

## 2. 三個新指令 (方案 B 新增)

| 指令                        | 功能                                                     |
|----------------------------|--------------------------------------------------------|
| `./dispatcher_start.sh`    | 背景啟動 daemon，從這一刻開始接手續投                         |
| `./dispatcher_stop.sh`     | 優雅停止 (等下次輪詢後 clean-exit)                          |
| `./dispatcher_status.sh`   | 查目前狀態 (daemon/chain/partition/binary/log)              |

現有指令**完全不變**：
- `./run.sh` 仍然是首輪投遞用 (cold start / round 1)
- `./chain_status.sh` 仍然查 chain 狀態
- 若 dispatcher 沒啟動 → 一切行為與方案 A 完全一致

---

## 3. 先決條件

### 3.1 產生兩個 arch 的 binary

跨 partition 需要兩個 arch 的 `a.out`（因為 GB200=aarch64/sm_100、H200=x86_64/sm_90，不能互換）：

```bash
# 1) 先產生 GB200 aarch64 binary（透過 salloc cross-compile）
bash build_and_submit.sh.GB200 --build-only
cp a.out a.out.GB200

# 2) 再產生 H200 x86_64 binary（login 節點直接編）
bash build_and_submit.sh.H200 --build-only
cp a.out a.out.H200

# 3) 確認兩個都有
ls -l a.out.GB200 a.out.H200
```

`./dispatcher_status.sh` 會列出兩個 binary 的存在狀態。

> **若只有單一 arch binary**：dispatcher 可以啟動，但只會投有對應 binary 的 partition（等於退化成單 partition 模式）。

### 3.2 首輪要不要先 `./run.sh`

**兩種啟動方式都 OK**：

**方式 A — 先 cold start，再接手：**
```bash
./run.sh                    # 投 round 1（自動偵測 cluster）
./dispatcher_start.sh       # daemon 接手 round 2 以後
```

**方式 B — 全程交給 dispatcher：**
```bash
# 先手動清理 + 產 binary
rm -rf restart/
bash build_and_submit.sh.GB200 --build-only && cp a.out a.out.GB200
bash build_and_submit.sh.H200  --build-only && cp a.out a.out.H200
# 建最小 chain state 讓 dispatcher 覺得 "該投 round 1 了"
mkdir -p restart/ && echo 1 > restart/chain_count
./dispatcher_start.sh
```

推薦方式 A，流程較直覺。

---

## 4. 典型使用流程

```bash
cd /path/to/<case>

# 一次性準備（只需做一次，之後 binary 沿用）
bash build_and_submit.sh.GB200 --build-only && cp a.out a.out.GB200
bash build_and_submit.sh.H200  --build-only && cp a.out a.out.H200

# 啟 chain round 1
./run.sh                        # cold start, 自動投到某個 partition

# 啟動 dispatcher (背景)
./dispatcher_start.sh

# 看狀態（隨時可查）
./dispatcher_status.sh

# 追 log
tail -f restart/dispatcher.log
tail -f restart/chain.log       # chain 本身的 log

# 當想停下
./dispatcher_stop.sh            # 優雅停 (下次輪詢後 exit)
./dispatcher_stop.sh --kill-now # 立刻 kill
```

---

## 5. Dispatcher 停止條件

daemon 在以下任一條件滿足時 clean-exit：

| 條件                                     | 說明                                      |
|------------------------------------------|------------------------------------------|
| `restart/STOP_CHAIN` 存在                 | solver 自然收斂 / 使用者手動觸發停鏈           |
| 最新 job 以 `exit 42` 結束                 | POLICY-C1 unavoidable error, 永遠不重投      |
| `STOP_DISPATCHER` 存在                    | `./dispatcher_stop.sh` 觸發 (優雅停)         |
| 找不到任何可用的 partition 且無 active job  | Fail-safe（理論上應極少發生）                  |

> **沒有** MAX_ROUNDS 限制。只要一直續跑成功，就一直續跑。

---

## 6. 運作原理

```
┌─────────────────────┐
│  submit_dispatcher  │  background daemon
│        .sh          │
└──────────┬──────────┘
           │ (每 30s 輪詢一次)
           ▼
   ┌──────────────┐      no active job?      ┌────────────┐
   │ squeue chain │ ─── yes ──────────────▶  │ sleep 30s  │
   │   jobid ?    │                           └────────────┘
   └──────┬───────┘
          │ no active job
          ▼
   ┌────────────────────┐
   │ sacct last exit    │
   │  == 42 ?           │──── yes ─▶  exit (POLICY-C1 unavoidable)
   └──────┬─────────────┘
          │ no (RC=0/124/other)
          ▼
   ┌────────────────────┐
   │ sinfo: 誰閒?        │
   │ GB200? H200?       │
   └──────┬─────────────┘
          │
          ▼
   ┌────────────────────┐
   │ cp a.out.<C>       │    並 sbatch
   │     → a.out        │    對應 jobscript
   │ sbatch jobscript_  │──▶
   │   chain.slurm.<C>  │
   └────────────────────┘
```

關鍵協調：`DISPATCHER_ACTIVE` sentinel
- Dispatcher 啟動 → 建立此檔 (內含 daemon PID)
- Jobscript 在續投判斷時 (L523 ish) 讀到此檔 → 跳過內建 self-resubmit
- Dispatcher 停止 → 刪除此檔 → jobscript 回復 self-resubmit 行為

---

## 7. 常見疑問

**Q: Dispatcher 會不會跟 jobscript 同時 sbatch，導致雙投？**
- 不會。Jobscript 裡有 sentinel check：
  ```bash
  if [ -f DISPATCHER_ACTIVE ]; then
      log "dispatcher 接手續投"
      exit 0
  fi
  ```
  只要 sentinel 存在，jobscript 就不會自己續投。

**Q: 如果我直接 `./run.sh` 而 dispatcher 正在跑？**
- `run.sh` 頂端有防呆：偵測到 `DISPATCHER_ACTIVE` 就 `exit 5`，印出提示要先 stop dispatcher。
- Dispatcher 自己呼叫 jobscript 時不經過 run.sh，不會踩到此防呆。

**Q: Dispatcher crash 了怎辦？**
- `DISPATCHER_ACTIVE` 會殘留但裡面 PID 已失效。
- `./dispatcher_start.sh` 會偵測並自動清理殘留 sentinel。
- `./dispatcher_status.sh` 會提示需要手動 `rm DISPATCHER_ACTIVE`。

**Q: 兩個 binary 大小對嗎？**
- GB200 binary (aarch64) 通常略大於 H200 binary (x86_64)，但相差不多（數 MB 級）。
- 差太多可能是編譯 flag 不一致，`./dispatcher_status.sh` 會顯示大小與時間戳幫助比對。

**Q: Dispatcher 和 chain_count 的關係？**
- `restart/chain_count` 是 jobscript 的 RC=0 前增 1 並寫入 (L520)
- Sentinel check 在 L523 之後才 exit → `chain_count` 已經是下一輪號碼
- Dispatcher 下次 sbatch 時，jobscript 讀到的就是正確的 round 號

---

## 8. 檔案清單 (方案 B 新增)

```
chain_code/
├─ submit_dispatcher.sh       # 核心 daemon (被 start 呼叫)
├─ dispatcher_start.sh        # 啟動
├─ dispatcher_stop.sh         # 停止
├─ dispatcher_status.sh       # 查狀態
└─ DISPATCHER_USAGE.md        # 本文件
```

根目錄 symlink (4 個)：
```
submit_dispatcher.sh   → chain_code/submit_dispatcher.sh
dispatcher_start.sh    → chain_code/dispatcher_start.sh
dispatcher_stop.sh     → chain_code/dispatcher_stop.sh
dispatcher_status.sh   → chain_code/dispatcher_status.sh
```

修改過的既有檔案 (插入 sentinel check, 零破壞)：
- `chain_code/run.sh`                   : L114 加防呆 (無 sentinel 時行為不變)
- `chain_code/jobscript_chain.slurm.GB200` : L523 加 dispatcher 接手檢查
- `chain_code/jobscript_chain.slurm.H200`  : L523 加 dispatcher 接手檢查

---

## 9. 上傳到 HPC 時要帶的檔案

**最小集合 (方案 B 新增的)**：
```
chain_code/submit_dispatcher.sh
chain_code/dispatcher_start.sh
chain_code/dispatcher_stop.sh
chain_code/dispatcher_status.sh
chain_code/run.sh                       # 已加 sentinel 防呆
chain_code/jobscript_chain.slurm.GB200  # 已加 sentinel 檢查
chain_code/jobscript_chain.slurm.H200   # 已加 sentinel 檢查
```

加上根目錄 symlink (用 `tar --hard-dereference` 或個別建立)：
```
submit_dispatcher.sh   → chain_code/submit_dispatcher.sh
dispatcher_start.sh    → chain_code/dispatcher_start.sh
dispatcher_stop.sh     → chain_code/dispatcher_stop.sh
dispatcher_status.sh   → chain_code/dispatcher_status.sh
```

上傳後第一件事：
```bash
chmod +x chain_code/*.sh
chmod +x dispatcher_*.sh submit_dispatcher.sh  # symlink 本身不需要, 但原始檔要
```
