# cfdq 續鏈 Dispatcher — 契約 + 測試 + 交給 Codex 驗證

> 目的:確保本地 V100 的 job 在「**任何中斷**」下都能從 checkpoint 續跑不斷鍊,
> **只有**少數合法終止條件才停。本文給 Codex 做獨立驗證。

---

## 1. 契約 (STOP vs RESUME) — 來源:`stop_control.h` + `hill_local_chain.sh` + `cfdq`

solver(`stop_control.h::StopReasonExitCode`)的 exit code 語意,經 wrapper 透傳、cfdq 判斷:

### 【STOP 停鏈】只有這些 — chain 終止, 不再 resume
| 條件 | solver exit | cfdq 結果 |
|---|---|---|
| Converged (收斂) | 0 | done |
| **NaN / Diverged**(Force NaN/Inf) | 0 (STOP_DIVERGED) | done |
| FTT_STOP / loop 上限 | 0 | done |
| **pkill a.out = SIGTERM** / SIGUSR2 / `restart/STOP_CHAIN` 檔 | 0 | done |
| 不可避免錯誤(grid/restart 損毀) | 42 | failed |

### 【RESUME 續鏈】其他全部 — 從 checkpoint 換節點續, 絕不斷鍊
| 條件 | 偵測 | cfdq 結果 |
|---|---|---|
| SIGUSR1 優雅被搶 | a.out exit 124 | queued → 重搶續跑 |
| crash (segfault 等) | a.out exit 1-9 → wrapper 映射 124 | queued |
| **kill -9 a.out** | 無 exit 檔 + pid 消失 | queued **+ 清節點 orphan** |
| 節點失聯 > TTL(180s) | 無法 probe | queued **+ 清節點 orphan** |

> 關鍵不變式:**RESUME 前必須清掉節點上本 job 的殘留 process(orphan)**,否則自己的孤兒
> 佔住節點 → 永遠無法重搶 → 斷鍊(見 §3 Bug B)。

---

## 2. 單元測試(Codex 主要驗證項)

```bash
cd ~/5.Re10595/Edit13_2800ITBLBM
bash tests/test_dispatcher.sh      # 期望: PASS=15 FAIL=0, exit 0
```

作法:`CFDQ_LIB=1 source ~/bin/cfdq` 只載函數,mock 掉 `probe_one`/`now`/`cleanup_orphans`,
直接驅動 `reconcile`/`finish_job`,斷言 job 狀態轉移。**不需真 GPU/SSH**,確定性、可重跑。
涵蓋:A.停鏈4項 B.續鏈4項(含 Bug B 清 orphan 被呼叫) C.安全性2項(短暫失聯/活著的job不誤判) D.chain=0。

**Codex 應確認**:(a) 15/15 全綠;(b) 邏輯對應上表;(c) 沒有任何「非合法終止」被判成 STOP。

---

## 3. 我們用實測抓到並已修的 2 個 bug(Codex 請覆核修正)

實測(對 live job 送 SIGUSR1)時暴露:

### Bug A — wrapper 信號 trap 不可靠
送 SIGUSR1 給 **wrapper bash**,其 `trap ... USR1` 未觸發 → wrapper 被預設動作殺死 →
**a.out 變 orphan 但繼續跑**。
**修正**(`cfdq` `cmd_rm`/`cmd_yield`):graceful 信號**直接送 a.out**
(`pkill -USR1/-USR2 -u $USER -x a.out`),a.out 自己的 handler 可靠;wrapper 只 `wait` + 回傳 exit 碼。

### Bug B — 自己的 orphan 擋住自己重搶(斷鍊根因)
job 判死後,殘留 orphan a.out 佔住節點(FOREIGN>0)→ `is_fully_free`=false → cfdq 永遠不重搶 →
**job 卡 queued = 斷鍊**。
**修正**(`cfdq` `finish_job` + 新 `cleanup_orphans`):RESUME 前(reason=killed/node-lost)先
`pkill -USR1 a.out`(嘗試存檔保進度)→ 等 ≤18s → 仍在則強制 kill → 節點釋放 → 重搶。

---

## 4. 整合測試(Codex 可選:在 live job 上驗證真實行為)

> ⚠️ 會中斷/影響執行中的 job;每項做完用 `cfdq ls` 確認結果再做下一項。
> 取執行中的節點:`N=$(sed -n 's/^node=//p' ~/.cfdq/jobs/0001/run)`

| # | 模擬 | 指令 | 期望 |
|---|---|---|---|
| I1 | 優雅被搶 | `cfdq yield 0001` | a.out exit124 → **queued → 重搶續跑**(log 顯示 `--restart=`) |
| I2 | kill -9(硬殺+orphan) | `ssh $N 'pkill -9 -u $USER -x a.out'` | daemon 判 killed → **清 orphan → 重搶續跑** |
| I3 | 收斂/使用者停 | `cfdq rm 0001` | a.out exit0 → **done(停鏈)** |
| I4 | daemon 自己掛 | `tmux kill-session -t cfdq` 後重起 `cfdq daemon` | reconcile **re-adopt** 執行中 job, 不重啟不誤停 |

每項驗收:`cfdq ls` 狀態符合「期望」欄;RESUME 類要看 solver log 出現 `[chain] 續跑 from ...step_N`
+ `--restart=`(非 cold),且 `Step/accu` 接續遞增。

---

## 5. Codex 驗證清單
- [ ] `bash tests/test_dispatcher.sh` → PASS=15 FAIL=0
- [ ] 覆核 §1 契約表與 `stop_control.h::StopReasonExitCode` 一致
- [ ] 覆核 Bug A/B 修正(`cfdq` 的 `cmd_rm`/`cmd_yield`/`finish_job`/`cleanup_orphans`)
- [ ] (可選)跑 §4 整合測試 I1–I4,確認「非合法終止一律續跑、合法終止才停」
- [ ] 回報:有無任何路徑會「該續跑卻斷鍊」或「該停卻續跑」
