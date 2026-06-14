# Edit11_Krank5600 — Warm-start Regrid 執行計畫

> 交接文件。由 Edit8_NewInterpolation 的 session 規劃(2026-06-14),交給 **Edit11_Krank5600 自己的 Claude Code** 在 Edit11 根目錄執行。
> **取得方式**(在 Edit11 根目錄):`cp /home/s8313697/5.Re10595/Edit8_NewInterpolation/EDIT11_KRANK5600_WARMSTART_PLAN.md .`
> 目標:把 **Edit6 已發展的 Re5600 流場**內插到一張**達 Krank Re5600 解析度**的新網格,做為 Edit11 的暖啟動種子,並在 64gpus slot 上開跑。

---

## 0. 背景與安全前提(務必先讀)

- **為什麼**:要 match Krank Re5600 DNS(512×256×256, 8 階)的解析度,2 階方法需 ~2×/方向 → 目標 ≈ **1024×512×512**。Edit11 現在 `variables.h` 是 641×321×321(=Edit6,只 1.25×Krank),**不夠**,要細化。
- **種子場**:用 Edit6_5600DNS 已發展到 FTT 72+ 的 Re5600 場(同物理、同 STRETCH_A),內插偏移遠小於用舊的 129×257×129。**避免冷啟初始震盪**。
- **slot 來源**:Edit8 已於 2026-06-14 14:41 **優雅暫停**(STOP_CHAIN、無損可續跑、64gpus slot 已釋出)。**不要碰 Edit8**(它隨時可能要 resume)。
- **Edit6 正在跑(job 97266 @16gpus)**:對 Edit6 **只能唯讀複製**已完成的 checkpoint,**絕不**動它的 `restart/`、job、daemon。**不要複製移動中的 `latest`**。
- **跨專案隔離**:本計畫只在 **Edit11** 根目錄操作。`scancel` 只用 `./run job-guard`。

---

## 1. 目標網格(先定案,寫進 variables.h)

軸對應:`NX`=展向(span)、`NY`=流向(stream)、`NZ`=壁法向(wall-normal)。
`(NY-1)` 必須能被 `jp` 整除。

| 參數 | 現值(=Edit6) | **目標(主建議)** | 說明 |
|---|---|---|---|
| `NX`(展向) | 321 | **512** | 2×/dir over Krank |
| `NY`(流向) | 641 | **1025** | 1024 cells;(1025−1)=1024=64×16 ✓ jp=64 |
| `NZ`(壁法向) | 321 | **512** | |
| `jp` | 32 | **64** | 接 Edit8 的 64gpus slot |
| 總格點 | 66M | **≈269M** | Krank 33.5M 的 ~8× |
| `STRETCH_A` | 0.95 | 0.95(不動) | 壁法向 clustering |

**可選升級(最大舒適,2.25×/dir,~382M)**:`NX=576, NY=1153, NZ=576, jp=64`(1152=64×18 ✓)。15 天仍可(~41 FTT)。
**不要再大**:1280×640×640(NY=1281)15 天只剩 ~20-FTT 窗口、零餘裕。

### 1a. 同時要改的其他 `variables.h` 參數(一次改完,之後別再動)
- `jp`:32 → **64**
- `FTT_STATS_START`:60 → **約 10**(warm-start 流場 ~5–8 FTT 重平衡後就開始累積統計;留 60 會等太久)
- 其餘不動:`Re=5600`、`Uref=0.015`、`STRETCH_A=0.95`、`COLLISION_MODE=1`(MRT)、`FORCE_HERMITE_ORDER=2`、`USE_GILBM_ALGORITHM2=1`。

> ⚠️ **mtime 地雷**:`interp_checkpoint.py` 會把 `variables.h` 的 mtime 記進 `restart/grid_provenance`,`run.sh` Preflight C 每次續跑都會比對。**所以 variables.h 的所有改動必須在「生成網格 + 跑 interp」之前一次完成,之後到投遞前都不要再碰 variables.h**(否則 mtime 漂移 → FATAL)。

---

## 2. 生成 new_grid(641×321 → 1025×512)

設好 variables.h 後,用網格生成器產生新網格(它讀 variables.h 的 NY/NZ/STRETCH_A):

```bash
cd /home/s8313697/5.Re10595/Edit11_Krank5600
python3 J_Frohlich/grid_zeta_tool.py --auto
# 產出:J_Frohlich/adaptive_3.fine grid_I1025_J512_s0.950000.dat (+ grid_data_*.txt + compare_auto_*.png)
```

把它複製到 phase1 並改 `newgrid_` 前綴(interp 只掃 `phase1_generategrid/`):

```bash
cp "J_Frohlich/adaptive_3.fine grid_I1025_J512_s0.950000.dat" \
   "phase1_generategrid/newgrid_3.fine grid_I1025_J512_s0.950000.dat"
```

---

## 3. 備好 old_grid(= Edit6 的 641×321 網格)

old_grid 是「內插源網格」,要對應種子場(Edit6 的網格)。唯讀複製、改 `oldgrid_` 前綴:

```bash
cp "/home/s8313697/5.Re10595/Edit6_5600DNS/J_Frohlich/adaptive_3.fine grid_I641_J321_s0.950000.dat" \
   "phase1_generategrid/oldgrid_3.fine grid_I641_J321_s0.950000.dat"
```

---

## 4. 備好 source checkpoint(= Edit6 已發展場,唯讀複製)

Edit6 job 97266 正在跑 → **挑一個已完成、穩定的 step(不是 `latest`)**:

```bash
E6=/home/s8313697/5.Re10595/Edit6_5600DNS/restart/checkpoint
# 取「次新」的已完成 step(避開正在寫的最新顆),並做 stat-stable 確認
ls -1dt "$E6"/step_* | sed -n '2p'        # ← 用這顆;記其 STEP
# 例:step_85100001。先確認 5 秒內大小不變(穩定)再複製。
```

只需 **f + rho + metadata**(56 個 `sum_*` 統計檔**不用**複製 —— interp 會把統計歸零):

```bash
SRC=$E6/step_85100001          # ←換成上面挑到的 step
DST=phase2_generatecheckpoint/oldcheckpoint_Re5600_step_85100001
mkdir -p "$DST"
cp "$SRC"/metadata.dat "$DST"/
cp "$SRC"/f??_*.bin    "$DST"/      # 19 dirs × 32 ranks = 608 檔
cp "$SRC"/rho_*.bin    "$DST"/      # 32 檔
# 驗:metadata 應為 mpi_rank_count=32, grid_dims=327,27,327(= jp32 的 641×321)
grep -E 'mpi_rank_count|grid_dims|step|FTT|accu_count' "$DST"/metadata.dat
```

> 注意:source 是 **jp=32**,target 是 **jp=64** → interp 會自動 stitch 32 片成全場再重切 64 片(無需同 jp)。

---

## 5. 清理 phase1(關鍵:各只能留一張)

`interp_checkpoint --auto` 要求 `phase1_generategrid/` 裡 **恰好一張 `oldgrid_` + 一張 `newgrid_`**,否則 FATAL(ambiguous)。移走 Edit6 帶來的舊的:

```bash
cd phase1_generategrid
mkdir -p _stale
mv oldgrid_I257_J129_g2.0_a0.5.dat _stale/ 2>/dev/null            # 舊 129×257 source 的
mv "newgrid_3.fine grid_I641_J321_s0.950000.dat" _stale/ 2>/dev/null  # 舊 641×321 的
ls -1                 # 應只剩 oldgrid_…I641_J321… 與 newgrid_…I1025_J512…
cd ..
```

---

## 6. 跑 interp_checkpoint(產生暖啟動 checkpoint)

```bash
cd /home/s8313697/5.Re10595/Edit11_Krank5600
python3 phase2_generatecheckpoint/interp_checkpoint.py --auto --step 1
```

預設(都正確,不用改):`--fneq-mode chapman-enskog --interp-mode phys --interp-order 6 --project-velocity div-exact --div-gate-tol 1e-12`。
它會:stitch Edit6 32 片 → 全場 → 內插 641×321→1025×512 → div-exact 投影 → **重切 64 片** → 寫 `restart/checkpoint/step_00000001/` → 寫 `restart/grid_provenance`。

---

## 7. 驗收 interp 輸出(任一不過就停,別投遞)

interp 自帶兩道 1e-12 閘門 + 守恆檢查,過不了會直接 FATAL 不寫檔。輸出後再人工確認:

```bash
M=restart/checkpoint/step_00000001/metadata.dat
grep -E 'mpi_rank_count|grid_dims|step|FTT|accu_count|dt_global' "$M"
# 期望:mpi_rank_count=64, grid_dims=518,23,518(NX6,NYD6,NZ6 of 512/1025/512@jp64),
#       step=1, FTT=0.0, accu_count=0(統計歸零 ✓), dt_global=新網格實值(非 -1)
cat restart/grid_provenance     # 應有 new I1025_J512 / old I641_J321 / old_jp=32 / new_jp=64 / variables_h_mtime
```

- `accu_count=0` 與 `FTT=0` 是**正確的**(全新統計窗口)。
- `div` 診斷應 ~1e-13(< 1e-12 gate)。

---

## 8. 編譯(jp=64 binary)

```bash
./run build H200 --build-only          # 只編不投
cp -f a.out a.out.H200                  # ⚠️ --build-only 不會自動同步 alias,手動補
md5sum a.out a.out.H200                  # 兩者應相同
```

> 之後到投遞前**不要再動 variables.h / 網格檔**(Preflight C mtime)。

---

## 9. 投遞 + 武裝 daemon(搶 64gpus slot)

```bash
rm -f .run.lock restart/STOP_CHAIN
./run --no-queue-check                   # warm 載入 step_00000001(非冷啟、非 --rebuild)
# 驗:squeue 應出現本專案 Edit11 job;scontrol show job <id> | grep WorkDir 指向 Edit11
./run dispatcher start
bash watcher/hill_watcher_start.sh
```

⚠️ **slot race**:64gpus 是跨帳號共用(Edit9_ITB5600 在 mst114348、其他使用者),Edit8 一釋出就是搶的。**第 1–8 步全部離線備妥後,第 9 步要一口氣投遞**,把空窗壓到最小;若有 slot-handoff sentinel 機制就武裝它。

---

## 10. 必知的坑(checklist)

- [ ] variables.h 一次改完(NX/NY/NZ/jp/FTT_STATS_START),之後到投遞前不再碰(Preflight C mtime)。
- [ ] phase1 恰好一張 `oldgrid_` + 一張 `newgrid_`(移走舊的 257×129 與 641×321)。
- [ ] new/old grid 檔名帶 `_s0.950000` tag(與 variables.h STRETCH_A 差 < 5e-7)。
- [ ] source checkpoint 用 Edit6 **已完成** step(非 live `latest`),stat-stable 後才複製;不碰 Edit6 job/restart。
- [ ] source metadata = mpi_rank_count=32 / grid_dims=327,27,327(與 oldgrid 641×321 一致)。
- [ ] interp 後 `accu_count=0/FTT=0`(正確);兩道 1e-12 gate 通過。
- [ ] build 後 `cp a.out a.out.H200` 並 md5 一致。
- [ ] 投遞用 warm(`./run --no-queue-check`),**不帶** `--rebuild` / `--force-cold`。
- [ ] 全程不碰 Edit8、Edit6、Edit9。

---

## 11. 時程預估(15 天預算)

- 吞吐(從 Edit8 897×449×449@64gpus 6.88 FTT/天,及 Edit6 641×321×321@32gpus 14.6 FTT/天 兩路推算):
  **1024×512×512 @ jp=64 ≈ 4.1–4.5 FTT/天**。
- warm-start 需 FTT:~6–8 重平衡 + ~30 累積 ≈ **~38 FTT** → **≈ 9–10 天**(15 天內舒適,留 ~5 天餘裕)。
- 若跑滿 65 FTT ≈ 16 天(略超預算)。
- 升級到 1152×576×576 → ~2.8 FTT/天 → ~40 FTT ≈ 14 天(剛好)。

> 結論:**主建議 `NX=512 NY=1025 NZ=512 jp=64`(1024×512×512)** —— 達 Krank 解析度(2×/dir)且 15 天內跑得出 ~30-FTT 收斂窗口。

---

## 12. Edit8 現況(備查,不要操作)

Edit8_NewInterpolation 已優雅暫停、無損可續跑:checkpoint `step_52300001` FTT=32.02 accu=11.47M,`restart/STOP_CHAIN` 在、dispatcher/keepalive 已停、a.out 保留。日後續跑:`rm restart/STOP_CHAIN && ./run dispatcher start`(在 Edit8 根目錄)。**本計畫不應碰 Edit8。**

---

## 13. 啟動後常駐監控 /loop(由 Edit11 自己的 Claude session 跑)

**觸發時機**:第 9 步投遞成功、job 開始跑之後(「一切行為就緒」)。建議用 `/loop`(自我節奏 ~60s),每輪依序做以下 5 檢查。**搶救動作(2/4/5)是本專案寫入,必須在 Edit11 session 內執行**;跨專案 session 只能唯讀告警、無法真正搶救。

**① Benchmark 對比(僅當進入統計階段 FTT ≥ FTT_STATS_START≈10)**
- 讀最新 convergence/benchmark 輸出(watcher 產的 `live/monitor_latest.png` + `result/` 的 benchmark 圖/數據)。
- 進統計階段後,**逐變數回報 vs Krank Re5600 benchmark 的誤差**:平均流向速度 ⟨U⟩、Reynolds 應力 ⟨u'u'⟩/⟨v'v'⟩/⟨w'w'⟩/⟨u'v'⟩、`Cf`、`Cp`。
- benchmark 資料:`result/benchmark/Benjamin Krank et al. 2018/Re5600/`;比對工具:`result/2.Benchmark.py --Re 5600`、`result/10.tau_wall_benchmark.py --Re 5600`。回報相對誤差 % 或 L2。
- 未進統計階段:只回報 FTT 進度 + CV 收斂狀態,不比對。

**② Dispatcher 存活 + 搶救(守住 64gpus slot,避免被搶)**
- **判活只看 `restart/dispatcher.heartbeat` 的 mtime**(唯一可靠信號)。⚠️ **不要用 `./run dispatcher status`** —— 跨 login node 會誤報 DEAD,照它重啟會產生第二個 dispatcher → 雙投。
- heartbeat mtime 停滯 > ~180s → 立即 `./run dispatcher start` 搶救(start 自帶 dup-guard)。

**③ Watcher 存活 + 出圖到 live/**
- 檢查 `live/watcher.pid` + 其 heartbeat/最新圖 mtime。
- 若活:確認它持續把最新圖更新到 `live/`(回報最新圖 mtime)。
- 若死:`bash watcher/hill_watcher_start.sh` 重啟(自帶 dup-guard + stale-PID 清理)。

**④ 守衛士兵(slot sentinel)存活 + 建 dispatcher 第二道防線**
- 檢查 Claude 端 sentinel:`live/claude_sentinel.pid`(pid@host + `kill -0`,**dedup 靠此檔不靠 TaskList** —— TaskList 看不到 Monitor 任務)。
- 若不存活:建立**第二道防線** —— 一個 alert-only 常駐 Monitor sentinel,獨立監看 dispatcher heartbeat + slot 狀態,在 dispatcher 死/slot 危險時**告警**(不自行雙投、不自行 scancel)。
- 用途:dispatcher 是第一道(自動續投),sentinel 是第二道(偵測 dispatcher 失效並告警/觸發搶救)。

**⑤ LOCK_COMBO 鎖檢查(partition@jp = 64gpus@64jp)**
- 確認 `restart/LOCK_COMBO` 存在且內容 = `64 H200@64gpus`(jp=64 / 64gpus partition)。
- 若漂移/不存在:回報;必要時 `echo "64 H200@64gpus" > restart/LOCK_COMBO` 重設並重啟 dispatcher 載入。
- 一致性驗證:`bash chain_code/tools/verify_combo.sh`(EXPECT_COMBO="64 H200@64gpus";比對 LOCK_COMBO / variables.h jp / jobscript header / mpirun -np)。

**守門(MUST)**:全程只操作 Edit11 本專案;`scancel` 只用 `./run job-guard`;sentinel 為 alert-only/read-only;不碰 Edit8/Edit6/Edit9。NaN/divergence 從 slurm tail 偵測即告警。
