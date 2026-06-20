# Pipeline-Prompt:watcher benchmark 比對圖 OOM/卡圖修復(可重用)

> 用途:當 watcher 自動產的 benchmark 比對圖長時間沒更新 / OOM(rc=137)/ timeout 時,
> 照此流程「診斷 → lowmem+串流分塊讀修法 → codex 驗證 → gitpush → git fetch origin →
> 重啟 watcher 生效」。**只操作當前專案,絕不碰其他專案的 job/daemon/檔案。**
> 本檔隨 git 散佈,任何 session 可貼上重跑。

---

## 觸發時機
- `live/fig_uu.png` 等 Reynolds 應力圖 /  `tau_wall_signed_*_cf.png`/`_cp.png` **長時間沒更新**,
- 或日常 loop 的「benchmark 圖時效檢查」標 ⚠️(圖 mtime 落後最新 VTK),
- 或 `live/watcher.log` 出現 `BENCH step=... FAILED rc=137`(OOM)/ `TIMEOUT after ...s`。

## Step 0 — 診斷(唯讀,先確定真因再動碼)
1. **圖時效**:每張 `live/fig_*`、`tau_wall_signed_*_{cf,cp}.png` 的 mtime 跟「最新
   `result/velocity_merged_*.vtk`」mtime 比 → VTK 比圖新 = 沒刷上。
2. **失敗模式**:`grep -E 'BENCH step=|TAUWALL step=' live/watcher.log | tail`
   - `FAILED rc=137` = OOM(login node 共用 20GB user.slice cgroup);`TIMEOUT` = I/O 慢。
3. **記憶體 baseline**:`cat /sys/fs/cgroup/user.slice/user-$(id -u).slice/memory.current`
   (別 user 佔用越高、benchmark 越容易 OOM;~35GB 全場 VTK 用 float64 全讀峰值 >20GB 必爆)。
4. **保留 vs 用到的欄位**:`grep -noE 'scalars\[[^]]+\]' result/<bench>.py` 找真正讀取的場,
   對照 `_BENCH_SKIP_FIELDS`/keep-set,找「保留但從未讀」的可再 skip 的場(如 benchmark 的 W_mean)。

## Step 1 — 修法(三層;改 `result/2.Benchmark.py` + `result/10.tau_wall_benchmark.py`)
> 預設(無 `--lowmem`)維持 **float64 零誤差、位元不變**(canonical 走 dev 計算節點 `--mem=48G`);
> watcher inline 在 login node 才帶 `--lowmem`。

1. **float32 + 跳未用欄(`--lowmem` 旗標,argparse `action='store_true'`)**:降 persistent floor。
   keep-set 只留各腳本真正用到的場:
   - `2.Benchmark.py`:U_mean/V_mean + uu/uv/vv/k_TKE;`_BENCH_SKIP_FIELDS` 加未用場(含 **W_mean**)。
   - `10.tau_wall_benchmark.py`:U_mean/V_mean/**P_mean**(★cp 需 P_mean,絕不可跳)。
   - 其餘 SCALARS/VECTORS 在 `_LOWMEM and is_binary` 時 skip:`f.seek(size,1); f.readline(); continue`。
2. **串流分塊讀**(消暫態翻倍 spike,是 rc=137 真正元凶):
   把 `buf=f.read(n*esize); np.frombuffer(buf,dtype=dt).astype(out_dtype)` 改成預配置
   `out=np.empty(n,out_dtype)`,迴圈每次 `fh.read(4M 元素=32MB)` 邊讀邊
   `out[i:i+got]=np.frombuffer(cbuf,dtype=dt,count=got)`(`got=len(cbuf)//esize`)。
   套 POINTS/VECTORS/SCALARS 三處 binary 讀(兩檔各 3 處)。
   **★末尾加截斷守門 `if i<n: raise ValueError("truncated ...")`** —— 否則截斷/半寫入 VTK 時
   新版回傳滿長度帶未初始化垃圾尾 → reshape 誤成功 → **靜默產錯圖**;守門還原原版 loud-fail
   (短讀→ValueError→上層跳過重試)。
3. **watcher 呼叫帶 `--lowmem`**(`watcher/hill_watcher.sh` 的 run_benchmark / run_tauwall;
   ★tau_wall **無** `--no-ask-scales`,別誤加)。

## Step 2 — 驗證(四重,全過才 push)
1. **py_compile** 兩檔 + `bash -n watcher/hill_watcher.sh`。
2. **守門單元測試**:餵完整 bytes → `np.array_equal` 驗 bit-identical;餵截斷 bytes → 驗 raise ValueError。
3. **codex 單發驗證**(★`</dev/null` 防 stdin hang;單發 `codex exec` 安全,**勿**用 rescue agent
   重試風暴弄壞 ~/.codex sqlite):
   ```bash
   timeout 300 codex exec --skip-git-repo-check "<review prompt>" </dev/null > /tmp/codex.out 2>&1
   ```
   prompt 要它驗:完整檔 bit-identical、skip 欄安全(真沒用到)、float32 峰值塞進 20GB cgroup、
   預設 float64 不變、截斷守門在。回 NEEDS-FIX 就修到 PASS(codex doctor 確認 sqlite 沒壞)。
   （可選加碼:對抗 workflow 多 lens 並行審 bit-identical / 記憶體 / 截斷,適用 ultracode。）
4. **live 35GB 實測**(決定性):背景跑 `(cd result && python3 <bench>.py ... --lowmem)` 讀最新 VTK,
   迴圈監測 cgroup `memory.current` 峰值,確認 `rc=0`(非 137/124)+ 圖全產出 + 目視一張
   (GILBM 紅線 vs MGLET/Krank DNS 形狀對)。

## Step 3 — gitpush(逐檔,禁 `git add -A`)
- 只 `git add` 有意義 code 檔(`result/2.Benchmark.py`、`result/10.tau_wall_benchmark.py`,
  必要時 `watcher/hill_watcher.sh`);**不**加 runtime 產物(`fig_*.png`/`*_cf.png`/`*_cp.png`/
  收斂圖/`*.vtk`),**不**加三大紀錄檔(`Ustar_Force_record.dat*`/`timing_log.dat*`/`checkrho.dat*`)。
- commit 訊息**繁體中文**,簡述真因+修法+驗證;尾加
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。
- `git push`;非 fast-forward 被拒 → **先回報讓使用者決定,不可 `--force`**。

## Step 4 — git fetch origin
- `git fetch origin`;回報 `ahead/behind` 與 `HEAD` short hash(預期 ahead=0 behind=0)。

## Step 5 — 重啟 watcher 生效
- `touch live/RESTART_WATCHER` → 合法 watcher 原地 `exec` re-exec(同 PID 重奪鎖、免 SSH)。
  > benchmark 是 watcher 用 subprocess 呼叫的 python,下次 run_benchmark 本就讀新檔;
  > re-exec 額外讓 watcher 重處理「當前 VTK」立刻產新圖(last_processed 重置)。
- **驗 re-exec**:`grep 'RESTART_WATCHER → re-exec\|watcher started' live/watcher.log | tail`;
  確認 `FATAL=0`、`owner=<本節點>:<同PID>`、`SELF-EVICT=0`、heartbeat 新鮮(<90s)。
- **驗 production**:re-exec 重處理後(或下一顆 VTK)`watcher.log` 出現 `BENCH step=... outputs`
  (非 FAILED rc=137)+ `live/fig_*` mtime ≥ 最新 VTK。

## 守門(MUST)
- 全程**只操作當前專案**;遵守跨專案 Job 隔離;不碰其他專案的 daemon/job/檔案。
- 殺/數 watcher 用 `pgrep -f '[h]ill_watcher\.sh'` + `/proc/PID/cwd` 驗歸屬,**絕不** `pkill -f`;
  跨節點殭屍靠 self-eviction 自滅或 `bash watcher/kill_zombie_watcher.sh`(SSH 有 2FA 殺不到跨節點)。
- 不在跑著的目錄做破壞性操作;取消 job 只 `./run job-guard scancel`。
- 沙箱擋裸 `sleep`(exit 144)→ 用 `python3 -c "import time;time.sleep(N)"`。
- `cd` 進子目錄會讓 write_guard 把子目錄當專案根 → 編輯前 `cd` 回專案根。

## 背景知識
- 真因類別:benchmark 圖「卡住不更新」**先查 `rc=137` OOM / TIMEOUT,非缺碼**
  (run_benchmark/run_tauwall 本就會 copy 圖到 `live/`)。
- 記憶體模型:login node `user.slice` cgroup **= 20GB 硬上限(跨 user 共用)**;float64 全讀 ~35GB
  VTK 峰值 >20GB;**float32 降 persistent floor、串流分塊讀降暫態 spike**,兩者疊加 inline 才穩。
- 雙軌:watcher float32 `--lowmem` inline(login,~6e-8~1e-4 捨入,監控足夠)/ 手動 canonical
  float64 走 dev 計算節點(`result/bench_computenode.slurm`,`--mem=48G`,零誤差)。
