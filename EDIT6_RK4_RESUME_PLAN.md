# Edit6 續跑計畫：RK4 + FTT_STATS_START=60 + 放棄統計 + H200 jp=32 暖啟動

> 2026-06-12。chain 88688 已 COMPLETED（非 running）。從最新 checkpoint **暖啟動**接續流場，
> 帶入已 commit 的 **RK4 departure**（重編），延後統計起點到 FTT=60 並**放棄既有統計**
> （accu_count 30.48M → 0，使用者已明確同意），jp=32 不變、H200。

## 0. 現況（評估）
| 項 | 值 |
|----|----|
| chain | 88688 COMPLETED（無 running job）|
| 最新 checkpoint | `restart/checkpoint/latest → step_60047800`（FTT=50.77, accu_count=30,480,237）|
| variables.h | USE_GILBM_ALGORITHM2=1, jp=32, FTT_STATS_START=**25.0**, FTT_STOP=200, CV_WINDOW=10 |
| precompute2.h | STORE=WEIGHTS_FOLDED, **GILBM2_DEPARTURE_RK4=1**（已 commit）|
| binary | a.out 1.605MB / a.out.H200 1.605MB = **舊 RK2 版 → 必須重編（RK4 ~2.01MB）** |
| provenance | variables_h_mtime=1780694139（改 variables.h 須同步）|

## 1. 放棄統計的機制
warm-resume 載入 checkpoint 時，因 **FTT_restart=50.77 < 新 FTT_STATS_START=60** →
solver 自動**重置 tavg/accu_count（=放棄 30.48M 統計）**，待 FTT≥60 才重新累積。
CV 收斂視窗檢查在 60+10=70；到 FTT_STOP=200 有充裕取樣。

## 2. 執行步驟（H200, warm, 禁 --cold）
```bash
cd /home/s8313697/5.Re10595/Edit6_5600DNS

# (1) 改統計起點 (RK4=1 + jp=32 已是預設, 不動)
#     variables.h:227  FTT_STATS_START 25.0 → 60.0

# (2) 同步 provenance mtime (否則 run.sh Preflight C FATAL)
#     restart/grid_provenance: variables_h_mtime = $(stat -c %Y variables.h)

# (3) 重編 H200 RK4 binary (--build-only 不投遞)
./run build H200 --build-only
cp -f a.out a.out.H200                       # --build-only 不自動同步, 必手動
#     驗證: a.out ~2.01MB (RK4, 非 1.6MB RK2); md5sum a.out a.out.H200 相同

# (4) 清 sentinel + 確認無 running job
rm -f restart/STOP_CHAIN .run.lock
squeue -u $USER -o "%.10i %.30j %.8T %R" | grep -i 5600 || echo "無 Edit6 job"

# (5) 停殘留 daemon (chain 已完成, dispatcher/watcher 多半已死)
./run dispatcher stop                        # 清殘留 DISPATCHER_ACTIVE
pkill -F live/watcher.pid 2>/dev/null; rm -f live/watcher.pid

# (6) 暖投 (不帶 --rebuild 用步驟3 binary; 不帶 --force-cold = warm; run.sh 自動 --restart=step_60047800)
./run --no-queue-check

# (7) 重啟 daemon
./run dispatcher start
bash watcher/hill_watcher_start.sh
```

## 3. 驗證（slurm_<新jid>.log）
- **warm-load 三閘**：`Restart from .../step_60047800`（非 step_1）、`[G6] Schema OK ... grid=match`、`[Phase5] dt_global consistent`
- **放棄統計確認**：`Statistics reset` / `accu_count=0`（因 50.77<60）、`Waiting for FTT>=60`
- **RK4 active**：validator `embedded max E_local < 1e-10 [CONVERGED OK]`、`int-field=0 [INT OK]`、`folded ... [FOLDED OK]`、`[= RK4-vs-Algo1RK2 weight gap (預期)]`
- **流場連續**：step/FTT 接續 50.77、Re/Ub/Ma_max 與停機前一致、無 NaN、`checkrho` ~1.0
- **單一 job**：以 WorkDir 驗歸屬、partition h200、jp=32、4 nodes（32/8）

## 4. 守門（MUST）
- **暖啟動禁 `--cold`**；放棄統計使用者已同意（資料安全閘 override）；流場一位元載入、RK4 從第一步接管 departure（~6e-7 of Uref 微擾, negligible）。
- **只操作 Edit6**；scancel 只用 `./run job-guard`；不碰 Edit8/Edit9（running）。
- 非 ff 不 --force；RK4 可隨時 `-DGILBM2_DEPARTURE_RK4=0` 回退 bit-exact RK2。

## 5. 回退
若 warm-load 或 RK4 validator FATAL → 不續投；查 log；必要時 `-DGILBM2_DEPARTURE_RK4=0` 重編回 RK2 暖啟動（流場/checkpoint 不變）。

*等使用者確認後執行；可改用 `claude_changestatsstart 60`（同引擎, 但其假設 running chain → 本案 chain 已 COMPLETED 故用上方 tailored 步驟）。*


