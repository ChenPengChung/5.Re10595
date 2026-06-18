# cfdq 本地 V100 搶佔測試 — 完整 Runbook

> 目標:把 Edit14_2800GILBM (Re2800 / 513×257×257 / s0.95 / jp=8) 排進佇列,守望 daemon 一旦
> 偵測到「整台全空的 V100 節點」立刻搶下開跑;節點崩潰/被殺則從 checkpoint 換節點
> 續跑(避免斷鍊);收斂則停。**只吃 V100,絕不跑 P100,寧願等。**

---

## 0. 已就緒清單(全部裝好且實機驗證)

| 元件 | 路徑 | 狀態 |
|---|---|---|
| 搶佔佇列 | `~/bin/cfdq` | ✅ V100-only,`cfdq nodes`/`daemon --once` 實機驗證 |
| per-GPU probe | `~/.local/bin/cfdq-probe.sh` | ✅ 含型號/外人佔用/PID-reuse 身分 |
| 本地 binary | `~/5.Re10595/Edit14_2800GILBM/a.out` | ✅ sm_70,0 warn/err,driver 550 支援 |
| chain wrapper | `~/5.Re10595/Edit14_2800GILBM/chain_code_local/hill_local_chain.sh` | ✅ 冷/續 + 信號轉發 + exit碼契約 |
| 編譯腳本 | `~/5.Re10595/Edit14_2800GILBM/chain_code_local/build_local.sh` | ✅ |
| 預生網格 | `J_Frohlich/adaptive_..._I513_J257_s0.950000.dat` | ✅ 穩定性 GOOD;節點免 python |

驗過的風險:CUDA13→12.4 降版、sm_90→sm_70、driver、grid 生成、數值穩定性、V100 過濾。

---

## 1. 現況(`cfdq nodes`)

只有 **CFDLab-1 (8×V100)** 活著,正被 **albert** 佔滿 → 這就是要搶的目標。
CFDLab-2/4/18/argosy/ib* 多數 DOWN;CFDLab-3 SICK;ib4/9/10 是 P100 → 自動跳過。
> ⚠️ V100 池目前很薄(只 CFDLab-1)。崩潰續跑時若沒有第二台空 V100,就會「等」。

---

## 2. 啟動測試(3 步)

**Step A — 排 job**(在測試目錄):
```bash
cd ~/5.Re10595/Edit14_2800GILBM
cfdq add --np 8 --model V100 --exclusive --chain --name edit14 -- bash chain_code_local/hill_local_chain.sh
```

**Step B — 啟動 daemon**(母機 cfdlab 的 tmux,長駐):
```bash
tmux new -s cfdq          # 或 ~/bin/tmux
cfdq daemon               # 預設 interval=20s, debounce=2
#   想更積極搶:cfdq daemon --interval 10 --debounce 2
# Ctrl-b d 離開 tmux(daemon 繼續跑)
```

**Step C — 等**。albert 一停,CFDLab-1 連續 2 輪偵測為「整台全空 V100」→ cfdq 搶前再
複查一次仍空 → 啟動 `chain_code_local/hill_local_chain.sh` → 冷啟動讀預生網格 → 跑。

---

## 3. 監看

```bash
cfdq ls                 # 佇列/執行中(含 NODE/PID、elapsed)
cfdq nodes              # 即時節點快照(誰空/誰佔/誰非V100)
cfdq log daemon -f      # 搶佔/續鏈/失聯 事件
cfdq log edit14... ↓
cfdq log 0001 -f        # solver 進度([VTK]/[CONV] Step=.. FTT=.. Ma_max=.. accu=..)
```
> **第一次搶到 CFDLab-1 要盯緊**(這也是 a.out 第一次真正在 8×V100 上跑,之前無空卡可
> smoke test):確認 log 出現載入既有網格(不重生)、MPI init OK、`[Info] Rank x/7 ...
> GPUs_on_node: 8`(8 卡綁定)、前幾百步 `Ma_max` 正常無 `NaN/DIVERG`。不對就 `cfdq rm 0001`。

---

## 4. 驗證「斷鍊續跑」(避免斷鍊的核心)

先 `cfdq ls` 取得執行中的 **NODE/PID**(例如 `CFDLab-1/12345`)。

**(a) 優雅被搶**(SIGUSR1 → 即時 checkpoint → exit124 → 換節點續):
```bash
ssh CFDLab-1 'kill -USR1 12345'      # 12345 = cfdq ls 顯示的 wrapper pid
```
預期:wrapper log 出現 `→SIGUSR1 (優雅被搶, 觸發 checkpoint)` → a.out 寫最終 checkpoint →
exit 124 → daemon log `續鏈` → 下一台空 V100(或 CFDLab-1 自己再空)啟動,且 solver log
顯示 `[chain] 續跑 from restart/checkpoint/step_N` + `--restart=`(不是 cold),accu/step 接續。

**(b) 硬殺/模擬節點崩潰**(無優雅 checkpoint → 從上個週期 checkpoint 續):
```bash
ssh CFDLab-1 'pkill -9 -u $USER -f a.out'
```
預期:daemon `pid 消失且無 exit 檔 → 視為被 kill/崩潰` → 重排 → 從**上一個** `step_*` 續
(損失 ≤ NDTBIN=10000 步)。

**驗收**:整條鏈 `Step` / `FTT` / `accu_count` 單調遞增,中途沒有再出現 `--cold`,無 NaN。

---

## 5. 停止 / 清理

```bash
cfdq rm 0001        # 執行中 → 送 SIGUSR2 乾淨停 a.out(exit0,不續鏈);佇列中 → 直接移除
cfdq stop           # 停 daemon(執行中的 job 不受影響)
# 清重資料(測試後):
rm -f ~/5.Re10595/Edit14_2800GILBM/result/*.vtk ~/5.Re10595/Edit14_2800GILBM/result/*.bin
```

---

## 6. 重要前提與雷

- **網格已預生**,節點冷啟不需 python。若改 `Re/NX/NY/NZ/jp/s` → 先重生網格再重編:
  ```bash
  cd ~/5.Re10595/Edit14_2800GILBM
  python3.12 J_Frohlich/grid_zeta_tool.py --auto    # 母機/此環境有 numpy+scipy
  bash chain_code_local/build_local.sh                         # variables.h 改了就要重編
  ```
- **只吃 V100**:`--model V100` 讓 cfdq 用 probe 的型號硬過濾;binary 又只編 sm_70 → 雙保險,永不誤跑 P100。沒有空 V100 就一直等。
- **jp=8 = 整台獨佔**:`cudaSetDevice(local_rank)` + GPU-sharing FATAL,所以只搶「無外人、8 卡全空」的節點;`--debounce 2` 擋別人 MPI 重啟瞬間的假空檔。
- **別碰原專案**:`~/5.Re10595/Edit11_Krank5600/` 有正在跑的 NCHC dispatcher/watcher;測試一律在 `~/5.Re10595/Edit14_2800GILBM/`。**絕不**把 cfdq 指到 `./run`(那是投 NCHC SLURM)。
- **NDTBIN/NDTVTK=10000**:checkpoint 與 VTK 綁在一起(每萬步)。VTK 在這 mesh 不小,home 空間吃緊,測完記得清 `result/*.vtk`。

---

## 7. cfdq 速查

```
cfdq add [--np N] [--model V100] [--chain] [--exclusive] [--name X] [--nodes "n1 n2"] -- <cmd>
cfdq daemon [--once] [--interval S] [--debounce K]
cfdq ls | status        cfdq nodes        cfdq log <id|daemon> [-f]
cfdq rm <id>            cfdq stop          cfdq help
環境變數: CFDQ_HOME CFDQ_INTERVAL CFDQ_DEBOUNCE CFDQ_LOST_TTL CFDQ_MEMFREE CFDQ_NODES
```

### cfdq 如何保證在共享/不穩叢集上安全(設計重點)
- 單一 daemon(singleton 鎖,NFS mtime 判活,空鎖有界回收);序列處理 → 不會重複搶。
- 原子發佈 job(staging→mv)、搶前再 probe 複查、debounce 連續全空才搶。
- 崩潰/失聯判定優先序:wrapper 寫的 exit 檔 > PID+start-time liveness(防 PID 重用) >
  連續失聯 TTL(180s,非 job 年齡)才判 node-lost。
- 啟動視窗崩潰可回復(launching 殘局 adopt 或回滾)。
