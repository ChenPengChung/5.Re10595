# `chain_code_local/run.sh` — 本地 V100 一條龍 Fork 計畫

> **給 Codex 的執行計畫。** 目標：把 `chain_code_nchc/run.sh`(1313 行,SLURM/H200/GB200
> pipeline)**fork** 成 `chain_code_local/run.sh`(本地 CFDLab V100-8 + cfdq pipeline),
> 完整接通 **phase1(網格)→ phase2(種子內插)→ phase3(編譯 + 投遞 + 續鏈)**。
> 作法已定:**Fork**(保留 NCHC 原語意/結構,逐段抽換 SLURM 段),不是另寫精簡版。
> 寫完後由 Codex 做執行驗證,重點是 **pipeline 完整性**(見 §10 驗收清單)。

---

## 0. 決策定錨(已由使用者拍板)

1. **作法 = Fork**：`cp chain_code_nchc/run.sh chain_code_local/run.sh` 後逐段改;保留所有
   非 SLURM 的判斷骨架(flag 解析、helper、case 樹、provenance preflight),只抽換
   「叢集偵測 / 並行互斥 / 編譯 / 投遞」四塊 SLURM 專屬碼。
2. **無 checkpoint 時的預設 = 自動 regrid-from-seed**：
   > 「若 phase1 內部網格點齊全,phase2 的資料點存在且齊全,則發動自動成資料點機制,
   > 生成 restart 到根目錄,且從 restart warm start。」

   ⇒ 預設(不帶 `--force-cold`)走 **Case 2**：自動把 phase2 種子內插成現格 checkpoint
   `restart/checkpoint/step_00000001` → `hill_local_chain.sh` 挑到它 → `--restart=` 暖啟。
   只有 `--force-cold` 才真零場冷啟。
3. **本計畫只產出計畫 + 由 Codex 實作並驗證**(本檔即計畫)。

---

## 1. 環境前提(本機已驗證,事實)

| 項目 | 值 / 路徑 | 驗證 |
|---|---|---|
| 母機 python(備網格/內插) | `python3.12` → numpy 1.26.4 + scipy 1.11.1 | ✅ 已確認 |
| 系統 python(**不可用**) | `/usr/bin/python3` = 3.6.8,**無 numpy** | ✅ 已確認;節點上多半也是這支 |
| 本地編譯 | `chain_code_local/build_local.sh`(nvcc `-arch=sm_70` → `./a.out`) | ✅ |
| 一段執行 | `chain_code_local/hill_local_chain.sh`(冷啟/續跑 + 信號 + exit-code) | ✅ |
| 排隊/搶節點/續鏈 | `cfdq`(= `~/bin/cfdq` = `watcher_local/cfdq`),`cfdq add … -- bash chain_code_local/hill_local_chain.sh` | ✅ |
| 現格網格(solver 讀) | `J_Frohlich/adaptive_3.fine grid_I513_J257_s0.950000.dat`(6.2 MB) | ✅ 存在 |
| 種子 origin | `phase2_generatecheckpoint/step_58706001/`(452 檔,mpi_rank_count=8,grid_dims=135,39,135,accu_count=10060263) | ✅ 存在,SOURCE.sha256 + VERIFY_RESULT.txt=PASS |
| OLD 網格(內插來源) | `phase1_generategrid/oldgrid_I257_J129_g2.0_a0.5.dat`(I257=NY257,J129=NZ129,對上種子) | ✅ |
| NEW 網格(內插目標,= solver 現格) | `J_Frohlich/adaptive_3.fine grid_I513_J257_s0.950000.dat` | ✅ |
| 現格參數(`variables.h`) | NX=257 NY=513 NZ=257 jp=8 STRETCH_A=0.95;`GRID_DAT_DIR="J_Frohlich"` `GRID_DAT_REF="3.fine grid.dat"` | ✅ |
| 現格 padded dims | `NX6=NX+6=263`,`NYD6=(NY-1)/jp+7=71`,`NZ6=NZ+6=263` | ✅(variables.h:145-148) |

> **內插為強制**:種子格(NX129/NY257/NZ129)≠ 現格(NX257/NY513/NZ257),不能直接
> `--restart=` 吃種子;一定要 phase2 內插到現格。

---

## 2. 產物

- **新檔**：`chain_code_local/run.sh`(fork 自 `chain_code_nchc/run.sh`)。
- 不動 `chain_code_nchc/`(保持 NCHC 休眠原樣)。
- 不改根目錄 `./run`(它硬寫死 `chain_code_nchc`;本地直接 `bash chain_code_local/run.sh …` 呼叫,
  **絕不**把 cfdq 指到 `./run`)。可選:在 §9 加一個 `chain_code_local/run_local`(thin wrapper)
  讓人少打字,但**非必要**。

---

## 3. Fork 對照表(逐段:KEEP / MODIFY / DELETE / REPLACE)

> 行號指 `chain_code_nchc/run.sh`(fork 來源)。

| NCHC run.sh 區段 | 行號 | 處置 | 本地改法 |
|---|---|---|---|
| 專案根自定位 | 56-59 | **MODIFY** | `CHAIN_DIR` 仍 = 腳本所在;`PROJECT_ROOT=CHAIN_DIR/..` 不變(腳本在 `chain_code_local/`,`..` 一樣是專案根)。Sibling 腳本改指 `chain_code_local/`。 |
| `MODE_*` 變數 | 61-74 | **MODIFY** | 移除 `MODE_CLUSTER`(--h200/--gb200);其餘保留。新增 `MODE_NP`(預設 8)、`MODE_NAME`(預設 edit13)、`MODE_NO_SUBMIT`。 |
| flag 解析 while/case | 76-134 | **MODIFY** | 刪 `--h200/--gb200`;`--no-queue-check` 改為「跳過 cfdq 重投 guard」;新增 `--np N` `--name X` `--no-submit`。保留 `--force-cold/--regrid-from-origin/--force-regrid/--preflight-only/--origin-dir/--old-grid/--new-grid/--rebuild`。`--defer-gen` **刪除**(本地一律母機 python3.12 即時內插,不延後到節點)。 |
| flag 互斥 guard | 136-144 | **MODIFY** | **只保留** 136(`--force-cold`+`--regrid`)、140(`--force-regrid` 無 `--regrid`)。**刪除 144 的 `--force-cold`+`--preflight-only` FATAL 互斥**——本地此組合合法(= wipe + cold 備料 + 不投遞,V14 要用)。 |
| `_project_abs_path` | 150 | **KEEP** | — |
| `_read_define_value` | 157 | **KEEP** | 讀數值 #define(NX/NY/NZ/jp/STRETCH_A)。 |
| `_grid_dim_value` | 169 | **KEEP** | 讀 grid `.dat` 表頭 I=/J=。 |
| `_read_string_define_value` | 185 | **KEEP** | 讀字串 #define(GRID_DAT_DIR/REF)。 |
| **`_derive_solver_grid_path`** | **197-213** | **REPLACE** | ⚠️ 核心 bug。丟掉 `_g${gamma}_a${alpha}` 組名,改 `_s<STRETCH_A>`:見 §4.1。 |
| `_compare_grid_dat_coords_exact` | 215-267 | **MODIFY** | 內部 `python3` → `"$PY"`(python3.12)。 |
| `_discover_phase1_grid` | 269-286 | **KEEP** | 用於找唯一 phase1 網格。 |
| **叢集偵測(4-tier ETA)** | **299-422** | **DELETE** | 整段刪。本地無 SLURM/分區/`sbatch --test-only`。固定 `CLUSTER=V100`。 |
| jobscript/build 選擇 | 424-435 | **REPLACE** | `BUILD_SCRIPT=chain_code_local/build_local.sh`;「一段腳本」= `chain_code_local/hill_local_chain.sh`(由 cfdq 投,非此處)。刪 `JOBSCRIPT=…slurm.*`。 |
| `flock .run.lock` | 442-447 | **KEEP** | 本地仍要防同一專案重入。 |
| `DISPATCHER_ACTIVE` sentinel | 456-461 | **DELETE** | NCHC dispatcher 專屬;本地 cfdq 是唯一佇列。 |
| `HEAD.lockdir` 單頭互斥 | 479-526 | **DELETE** | SLURM 多頭防護;本地 cfdq 單例 daemon 已保證單頭。 |
| `RUNNING.lockdir`+`chain_jobid` | 529-564 | **REPLACE** | 改成 §6 的 cfdq guard(掃 `~/.cfdq/jobs/*/spec` 比對 `cwd==本專案`)。 |
| 狀態旗標 HAS_BIN/HAS_CKPT/HAS_STATE | 569-585 | **MODIFY** | HAS_BIN/HAS_CKPT 照舊;`HAS_STATE`(chain_count+chain_jobid)**刪**(SLURM 鏈狀態,本地由 cfdq + hill_local_chain.sh 處理)。⚠️ HAS_STATE 被 banner 652-668 消費,刪它必須連 banner 一起改(見下「狀態 banner」列),否則留懸空引用。 |
| JOB_NAME(awk $JOBSCRIPT) | 587-588 | **DELETE** | 無 `#SBATCH job-name`;名稱由 `cfdq --name` 帶。 |
| PARTITION 設定 + `query_partition_status` def+call + QUEUE_JOBS | 590-647 | **DELETE** | 整單元刪(def 614-642、裸呼叫 644、`QUEUE_JOBS=$(squeue …)` 646-647)。⚠️ `set -eo pipefail` 下只刪 def 留 644 裸呼叫 → command-not-found rc127 abort,故**整段一起刪**。 |
| **狀態 banner** | **649-696** | **REWRITE** | 不可逐行 cp(會貼出引用已刪變數 CLUSTER/CLUSTER_SRC/PARTITION/JOBSCRIPT/BUILD_SCRIPT/QUEUE_JOBS/CC_DISPLAY 的列)。改成本地版:只印 `pwd`、`a.out`(HAS_BIN)、`checkpoint`(HAS_CKPT)、`BUILD_SCRIPT=chain_code_local/build_local.sh`、§6 guard 掃到的 cfdq job 狀態;**刪** cluster/partition/jobscript/queue-jobs/chain-state/congestion 各列(含 652-653 `[ "$HAS_STATE" -eq 1 ] && CC_DISPLAY=…`、668 `echo " chain state : $CC_DISPLAY"`、670-671 讀 `$QUEUE_JOBS` 的列)。 |
| `MODE_STATUS` 早退 | 698-701 | **KEEP**(若留 `--status`)或 **DELETE** | 保留 `--status` 就留;否則刪。 |
| scenario 3A squeue 早退 | 706-712 | **REPLACE** | 改 §6.2 cfdq guard(它自掃 `~/.cfdq/jobs/*/spec` 算 active job,**不**引用已刪的 `$QUEUE_JOBS`)。 |
| `--force-cold` wipe | **717-731** | **KEEP** | 只保留使用者提示 + rm wipe(`restart/ checkpoint/ checkrho.dat Ustar_Force_record.dat timing_log.dat statistics/`)+ `mkdir -p restart/`。 |
| `gb200_partition` 還原 + 殘 `HAS_STATE=0` | **732-743** | **DELETE** | 732-739 依賴已刪的 `_BEST_P/_BEST_C`(僅 396-397 賦值,在已刪的 cluster block 內)且引用 `jobscript_chain.slurm.*`;741 的殘 `HAS_STATE=0` 也一併刪(HAS_STATE 已於 row 81 移除)。 |
| **`_regrid_inputs_complete`** | **762-784** | **MODIFY** | 自動 Case 2 觸發判斷 = 使用者的「齊全才發動」閘:見 §5。 |
| `_run_regrid_pipeline`(內插) | 789-871 | **MODIFY** | 內部 `python3 interp_checkpoint.py` → `"$PY"`;指令見 §4.3。刪 `--defer-gen` 分支(823-828)。**刪 838 的 `--preflight-only → 給 interp 加 --dry-run` 分支 + 841-843 的 `HAS_CKPT=0`**——本地 `--preflight-only` 要**真的**內插產出 `step_00000001`(不可 dry-run),退出點改到 build 之後(見 row「`--preflight-only` 退出」)。 |
| origin globs | 943-958(+773) | **MODIFY** | 新增 `phase2_generatecheckpoint/step_*/`(需 metadata.dat):見 §4.2。 |
| case 決策樹 | 874-890 | **MODIFY** | 見 §5(去掉 cluster,加齊全閘)。 |
| Case 1 resume | 906-914 | **MODIFY** | `python3` → `"$PY"`。 |
| Case 2 regrid | 924-1052 | **MODIFY** | `python3`→`"$PY"`;grid 名用 `_s`;step3 座標比對用 `"$PY"`。**刪除 1020-1024 的 `if [ "${MODE_DEFER_GEN:-0}" -eq 1 ]` Step-3 guard 與對應 1044 的 `fi`,只留無條件全座標比對分支(1025-1043),該分支 `python3` 改 `"$PY"`**(MODE_DEFER_GEN 的兩個消費點:此處 1020-1044 + 823-828,後者由 `_run_regrid_pipeline`(789-871)列處理)。 |
| Case 3 cold | 1057-1065 | **MODIFY** | `python3 grid_zeta_tool.py --auto` → `"$PY" …`。 |
| provenance preflight C-0 / C | 1070-1150 | **KEEP** | 與叢集無關;原樣保留(暖啟正確性靠它)。 |
| `--preflight-only` 退出 | 1152-1155 | **RELOCATE** | ⚠️ NCHC 在此(build 1160 **之前**)就早退 → `--preflight-only --rebuild`(V10)會在編譯前 exit、不產 a.out。本地把 `MODE_PREFLIGHT_ONLY`(及 `MODE_NO_SUBMIT`)的退出點**移到 build+ELF 檢查之後、§7 cfdq 投遞之前**,讓 `--preflight-only` 完成「網格→內插→編譯」全備料只略過投遞。 |
| 編譯 | 1160-1177 | **REPLACE** | `bash chain_code_local/build_local.sh`;驗 `./a.out`;`file a.out` 須含 ELF x86-64(sm_70 binary)。刪 `cp a.out a.out.<CLUSTER>`(無 ETA 快取需求;可選保留 `a.out.V100`)。 |
| 鏈狀態部署(chain_count/jobid) | 1182-1231 | **DELETE** | SLURM 鏈計數;本地 cfdq `--chain` + wrapper 自理。 |
| ARCH-GUARD(file(1) 比對) | 1248-1275 | **MODIFY** | 簡化成「`file a.out` 必須是 x86-64 ELF」一行檢查(防誤用 aarch64 binary);無多架快取切換。 |
| 投遞前 echo + `RUNSH_PART*` 匯出 + `chain_status.sh` hook | 1276-1298 | **DELETE** | 此段 live 讀已刪變數(1278 `$BUILD_SCRIPT`/`$CLUSTER`、1282 `RUNSH_PARTITION=$PARTITION`、1283-1286 `RUNSH_PART_*=$PART_*`、1288 `chain_status.sh --cluster=$CLUSTER`)。無 `set -u` → 空字串不 abort,但屬 SLURM 殘留,整段刪。 |
| 投遞 handoff(exec sbatch) | 1299-1312 | **REPLACE** | `cfdq add --np "$MODE_NP" --model V100 --exclusive --chain --name "$MODE_NAME" -- bash chain_code_local/hill_local_chain.sh`(§7);退出點前先做 §3「`--preflight-only` 退出」的判斷。 |

---

## 4. 關鍵替換細節(精確碼)

### 4.1 網格檔名:`_g_a` → `_s<STRETCH_A>`（取代 `_derive_solver_grid_path`)

solver 端真正讀的名字(`initialization.h:99`、`main.cu:271`、`grid_zeta_tool.py:2272`):

```
<GRID_DAT_DIR>/adaptive_<stem>_I<NY>_J<NZ>_s<STRETCH_A:.6f>.dat
```

`stem` = `GRID_DAT_REF` 去掉 `.dat`(`"3.fine grid.dat"` → `"3.fine grid"`)。新 helper:

```bash
_derive_solver_grid_path() {            # 取代 NCHC 197-213
  local dir stem ny nz sa
  dir=$(_read_string_define_value GRID_DAT_DIR)        # J_Frohlich
  stem=$(_read_string_define_value GRID_DAT_REF); stem=${stem%.dat}   # "3.fine grid"
  ny=$(_read_define_value NY)           # 513
  nz=$(_read_define_value NZ)           # 257
  sa=$(_read_define_value STRETCH_A)    # 0.95  (純數字; 不要讀 GAMMA, 它是 log() 運算式)
  printf '%s/adaptive_%s_I%d_J%d_s%.6f.dat' "$dir" "$stem" "$ny" "$nz" "$sa"
}
```

> 產出:`J_Frohlich/adaptive_3.fine grid_I513_J257_s0.950000.dat`(= 磁碟現檔)。路徑含空白,
> 全程務必雙引號。

### 4.2 origin 自動偵測:加收 `phase2_generatecheckpoint/step_*/`

NCHC 三個 glob(773 與 943):`restart/step_*_origin*/`、`phase2_generatecheckpoint/step_*_origin*/`、
`phase2_generatecheckpoint/oldcheckpoint_*/`。**新增第四個**:`phase2_generatecheckpoint/step_*/`
(需含 `metadata.dat`)。保留 NCHC 規則:**唯一匹配**(0 或 >1 → FATAL),`--origin-dir` 可覆蓋。

```bash
_discover_origin() {
  local hits=() d
  for d in restart/step_*_origin*/ \
           phase2_generatecheckpoint/step_*_origin*/ \
           phase2_generatecheckpoint/oldcheckpoint_*/ \
           phase2_generatecheckpoint/step_*/ ; do
    [ -d "$d" ] && [ -f "${d%/}/metadata.dat" ] && hits+=("${d%/}")
  done
  # 去重 + 唯一性檢查; >1 或 0 → FATAL(除非 --origin-dir)
}
```

> 本機目前唯一命中 = `phase2_generatecheckpoint/step_58706001`。

### 4.3 phase2 內插指令(母機 python3.12,即時、不 defer)

```bash
"$PY" phase2_generatecheckpoint/interp_checkpoint.py \
    --auto --step 1 \
    --old-dir "$ORIGIN_DIR" \
    --variables-h variables.h \
    --old-grid-dat "$OLD_GRID" \
    --new-grid-dat "$NEW_GRID" \
    --solver-grid-dat "$NEW_GRID" \    # ← 必帶: 才會寫 interp_solver_grid_match=1 + solver sha256
    --no-generate-solver-grid \        # ← 必帶: 禁 interp 自行 grid_zeta --auto(網格只在 §6.3 受控生成)
    --skip-drift-check                 # ← 必帶: 才會寫 dt_global=-1.0(否則寫實算 dt_real,見下)
# 預設 --output-root restart/checkpoint → 產 restart/checkpoint/step_00000001/
#   + restart/grid_provenance
# 內插器既有科學預設: --fneq-mode chapman-enskog, --interp-order 6, --rho-mass-target unit,
#   --project-velocity div-exact(★ 沿用預設;div-exact 才過 1e-12 散度寫入閘,poisson 只到 1e-6 會被擋,故不加旗標)
# metadata 實際寫入(已讀碼確認 interp_checkpoint.py:4102-4232):
#   mpi_rank_count=NEW.JP(=8), grid_dims=NEW.NX6,NEW.NYD6,NEW.NZ6(=263,71,263),
#   step=1, checkpoint_version=2,
#   dt_global=-1.0 ← 僅在帶了 --skip-drift-check 時才寫(line 3814);否則寫實算 dt_real(line 3818)
#   FTT=0 / accu_count=0(line 4107-4108; ⚠️ 見 §11 V9), controller state 保留,
#   interp_solver_grid_match / interp_*_grid_params_sha256(line 4221/4229-4232,僅 --solver-grid-dat 有傳才寫)
```

- `OLD_GRID` 預設 = 唯一 `phase1_generategrid/oldgrid_*.dat`(`--old-grid` 覆蓋)。
- `NEW_GRID` 預設 = `_derive_solver_grid_path`(= solver 現格;**必須**等於 solver 執行期讀的檔,
  否則 `PrecheckCheckpointGridConsistency` 的 sha256 對不上)。`--new-grid` 覆蓋。
- ⚠️ **`--solver-grid-dat` 不傳的後果**:interp **不寫** `interp_solver_grid_match`/solver sha256
  (line 4221/4267 以 `args.solver_grid_dat` 為條件),則 solver `PrecheckCheckpointGridConsistency`
  對缺欄位是 **WARN+skip(非 FATAL)** —— 仍能跑,但失去一道一致性保護。故 §4.3 一律補帶
  `--solver-grid-dat "$NEW_GRID"` 取得 match=1 的強保證。
- ⚠️ **`--no-generate-solver-grid` 必帶(codex 查到)**:interp 預設 `generate_solver_grid=True`
  (`interp_checkpoint.py:3169`),solver 網格缺檔時它會自行 `sys.executable J_Frohlich/grid_zeta_tool.py --auto`
  (`interp_checkpoint.py:345-373`)。雖然 `sys.executable`==python3.12(由 `"$PY"` 啟動)不會踩 python3.6,
  但會讓網格在「不受控的 interp 階段」被生成、繞過 §6.3 的 `git diff --quiet variables.h` 守則。故一律
  傳 `--no-generate-solver-grid`,網格只在 §6.3 受控生成;配合 §6.4 開頭的硬存在性 guard。
- 🔴 **`--skip-drift-check` 必帶(codex + empirical 雙確認)**:interp 只在 `args.skip_drift_check` 為真時才寫
  `dt_global=-1.0`(`interp_checkpoint.py:3813-3818`);否則寫**實算 `dt_real`(正值)**。solver 端只有
  `dt_saved<0.0` 才跳過 Phase5 drift 檢查(`fileIO.h:658-661`)。換格後 dt 必變(新格較細),舊 dt 無意義,
  本就該讓 solver 重算並跳 drift → 故 **必帶 `--skip-drift-check`** 使 `dt_global=-1.0` 成立。
  (不帶的話 drift gate 會 live 跑,只在 `|Δ/dt|<1e-6` 時才不 FATAL,等於把正確性押在 dt 公式 parity 上,較脆。)
- ✅ **投影決策已定:用預設 `div-exact`,不加 `--project-velocity`**(已查 `interp_checkpoint.md` 無相關建議,
  以 argparse 為準)。理由:interp 有**兩層散度寫入閘 `--div-gate-tol=1e-12`**——`max|div(u*)|` 未嚴格低於 1e-12
  就**不寫 checkpoint**(`interp_checkpoint.py:3134-3139`)。`div-exact` = 「exact minimum-norm 修正,直接歸零
  最終 CD2 散度診斷」(= 閘量的同一診斷),故能過 1e-12 閘;而 `poisson` 只 target **1e-6 RMS**(Richardson ≤80 迭代),
  可能**過不了 1e-12 閘**→ checkpoint 不寫 → FATAL。故 `div-exact`(預設)是正確且安全選擇。argparse help 把
  「(default)」標在 poisson 是**過時註解**,真實 `default='div-exact'`(line 3120)。

### 4.4 PY 變數 + 啟動自檢

```bash
PY="${LOCAL_PY:-python3.12}"
command -v "$PY" >/dev/null || { echo "[FATAL] 找不到 $PY"; exit 42; }
"$PY" - <<'EOF' || { echo "[FATAL] $PY 缺 numpy/scipy"; exit 42; }
import numpy, scipy
EOF
```

> **絕不**用 `python3`(系統 3.6.8 無 numpy)。所有 `grid_zeta_tool.py` / `interp_checkpoint.py` /
> 座標比對都走 `"$PY"`。

---

## 5. Case 決策邏輯(本地版)+ 「齊全才自動發動」閘

取代 NCHC 874-890。次序:

```
1) --force-cold                         → Case 3 (真零場冷啟; 先 wipe restart/ 等)
2) --regrid-from-origin                 → Case 2 (明確要求內插)
3) HAS_CKPT (restart/checkpoint/step_* 已存在, 非 .WRITING, 含 metadata.dat)
                                        → Case 1 (resume; 不重內插)
4) _regrid_inputs_complete == true      → Case 2 (★ 自動觸發, 預設路徑)
5) 否則                                 → Case 3 (cold)
```

`_regrid_inputs_complete`(取代 NCHC 762-784,= 使用者的齊全閘):**全部成立**才回 true:

- **A. phase1 網格齊全**：
  - `NEW_GRID`(`_derive_solver_grid_path`)存在;表頭 `_grid_dim_value` I==NY(513)、J==NZ(257)。
    (不存在則 §6 preflight 會先 `grid_zeta_tool.py --auto` 生成;生成後再判。)
  - `OLD_GRID`(唯一 `phase1_generategrid/oldgrid_*`)存在;表頭與種子 NY/NZ 相容(257/129)。
- **B. phase2 種子齊全**(`_seed_complete "$ORIGIN_DIR"`):
  - `metadata.dat` 存在且可讀 `mpi_rank_count`(R)、`grid_dims`、`step`、`accu_count`。
  - **檔數齊全**(依 R=8):`f00..f18`×R=152 + `rho`×R=8 + `sum_*`×36×R=288 + `cv_*history`×3
    = **451 `.bin` + 1 metadata.dat = 452**。Codex 實作以「依 R 動態算期望數」而非寫死。
  - **(強)** 若同層有 `<step>.SOURCE.sha256` 與 `<step>.VERIFY_RESULT.txt`,後者須含 `VERDICT.*PASS`;
    否則僅警告(因母機已驗過一次,sha256 全驗很慢)。

> A∧B 成立 ⇒ 自動 Case 2 ⇒ 內插 → `restart/checkpoint/step_00000001` → wrapper `--restart=` 暖啟。
> 任一不齊 ⇒ 不亂內插,落 Case 3 cold(並 log 缺什麼),符合 NCHC「內插絕不靜默亂配」原則。

---

## 6. Preflight(全在母機,順序固定)

延用 NCHC 的 `flock .run.lock`(442-447)單一專案重入鎖,然後:

1. **PY 自檢**(§4.4)。
2. **cfdq 重投 guard**(取代 529-564 / 706-712):
   ```bash
   # 掃所有 cfdq job, 只認 cwd==本專案 且 status∈{queued,launching,running} 者
   for s in "$HOME"/.cfdq/jobs/*/spec; do
     [ -f "$s" ] || continue
     cwd=$(sed -n 's/^cwd=//p' "$s"); st=$(cat "${s%/spec}/status" 2>/dev/null)
     if [ "$cwd" = "$PWD" ] && case "$st" in queued|launching|running) true;; *) false;; esac; then
        echo "[FATAL] 本專案已有 cfdq job($(basename "${s%/spec}"), $st), 不重投"; exit 1
     fi
   done
   ```
   ⚠️ **雷**:`~/.cfdq/jobs/0001|0002` 目前 `cwd=/home/chenpengchung/cfdtest/Edit11_local`(**絕對路徑**,
   別專案;cfdq:338 `printf 'cwd=%s\n' "$PWD"` 必為絕對),`[ "$cwd" = "$PWD" ]` 兩端皆絕對且不等 → 自動略過,
   **絕不**動它們(guard 唯讀,只 `echo`+`exit`,不 rm/cancel)。`--no-queue-check` 可跳過本 guard。
3. **phase1 網格**：`NEW_GRID` 不存在 → `"$PY" J_Frohlich/grid_zeta_tool.py --auto`;事後驗存在 + 維度。
   ⚠️ **variables.h 不可變守則**:`grid_zeta_tool.py --auto` 在「重生網格且 GAMMA 被穩定性夾擠」時
   (`grid_zeta_tool.py:2300` `abs(gamma_original-gamma)>1e-6` → 2305 `update_stretch_a_in_variables_h`)
   會**自動回寫 `#define STRETCH_A`**。現況 happy path(網格已存在參數相符 → 2110-2131 提早 return)不會發生,
   但為防靜默改物理參數:`--auto` 跑完立即 `git diff --quiet variables.h`,**有變更即 FATAL** 並要求人工複核
   (改了 STRETCH_A 等於改了網格檔名 s 值與物理,須重新確認)。
4. **phase2 內插(Case 2)**：先硬擋 `[ -s "$NEW_GRID" ] || { echo "[FATAL] solver 網格未生成,先跑 §6.3"; exit 1; }`
   (確保網格已在 §6.3 受控生成,interp 不會自行補生)。然後:`restart/checkpoint/step_00000001` 不存在(或 `--force-regrid`)→ §4.3 指令。
   ⚠️ **`--preflight-only` 也要走真內插**(已於 row「`_run_regrid_pipeline`」刪掉 interp 的 `--dry-run` 分支)——
   不可只 dry-run,否則 V7/V8 期望的 `step_00000001` 不會生。事後驗 `restart/checkpoint/step_00000001/metadata.dat`:
   `grid_dims==263,71,263`、`mpi_rank_count==8`、`step==1`、`accu_count==0`、`dt_global==-1.0`(因帶了 `--skip-drift-check`)、
   `interp_solver_grid_match==1`,且 `restart/grid_provenance` 已生。沿用 NCHC provenance preflight C-0/C(1070-1150)。
5. **編譯**：無 `./a.out` 或 `--rebuild` → `bash chain_code_local/build_local.sh`;驗 `./a.out` 為 x86-64 ELF。

**`--preflight-only` / `--no-submit` 的退出點 = 此處(步驟 5 之後、§7 投遞之前)**——即 NCHC 1152-1155 的早退被
**下移**到 build 完成後,故 `--preflight-only --rebuild`(V10)會真的編譯出 a.out 才停。備料完成,不投遞。

---

## 7. 投遞(取代 NCHC 1299-1312 的 exec sbatch)

```bash
[ "$MODE_NO_SUBMIT" = 1 ] && { echo "[OK] 備料完成, --no-submit 不投遞"; exit 0; }
echo "[submit] cfdq add --np $MODE_NP --model V100 --exclusive --chain --name $MODE_NAME"
cfdq add --np "$MODE_NP" --model V100 --exclusive --chain --name "$MODE_NAME" \
     -- bash chain_code_local/hill_local_chain.sh
# 提醒: daemon 沒活就去母機 tmux 開 `cfdq daemon`
# (對齊 cfdq:117/352 的權威存活窗 INTERVAL*3+30=90s, 非硬寫 -mmin -2=120s)
A="$HOME/.cfdq/daemon.lock/alive"; ITV="${CFDQ_INTERVAL:-20}"
if ! { [ -e "$A" ] && [ $(( $(date +%s) - $(stat -c %Y "$A") )) -le $(( ITV*3 + 30 )) ]; }; then
   echo "[note] 未偵測到活著的 cfdq daemon → 去母機 tmux 執行: cfdq daemon"
fi
```

> 投遞當下 cwd 必須 = 專案根(腳本開頭已 `cd PROJECT_ROOT`),cfdq 才把 job cwd 記成本專案。
>
> 註1 **`--exclusive` 是 cosmetic no-op**:cfdq 只把它寫進「從不被讀取」的 `mode` 欄位(cfdq:328/331/339,
> 無任何 `meta_get … mode`)。真正整台獨占由 `is_fully_free` 全節點空閘 + `node_ok_for`(V100+np)+ debounce 保證,
> 與此旗標無關;留著純文件意味,別依賴它改變排程。
>
> 註2 **無雙重佈署競態**:`cfdq add` 只原子入列(`cmd_add` → `mv` 發佈,cfdq:342),佈署(`launch_job`)只在
> 單例 daemon 的序列 `reconcile→schedule` 迴圈內;run.sh **絕不**直接呼叫 schedule/launch/daemon。
> 重跑 run.sh 最多入列重複 job,已被 §6.2 guard 擋下,不會並行佈署同一 job。

---

## 8. 刻意「不做」清單(fork 時刪掉的 SLURM 段)

- `sbatch` / `srun` / `squeue` / `sinfo` / `sacct` / `scontrol` —— 一個都不留。
- H200/GB200 arch 偵測、`PARTITION_CANDIDATES`、`sbatch --test-only` ETA。
- `JOBSCRIPT=…slurm.*`、`build_and_submit.sh.*`。
- `DISPATCHER_ACTIVE`、`HEAD.lockdir`、`RUNNING.lockdir`、`restart/chain_jobid`、`restart/chain_count`。
- `restart/gb200_partition` 還原(NCHC 732-739,依賴 `_BEST_P/_BEST_C` + `jobscript_chain.slurm.*`)。
- `query_partition_status` / `QUEUE_JOBS` / `CC_DISPLAY` / 狀態 banner 中所有 cluster/partition/jobscript 列。
- `--h200/--gb200/--defer-gen` flag(連 `MODE_CLUSTER` 變數;`MODE_DEFER_GEN=0` 宣告可留但兩個消費點都刪)。
- NCHC `submit_dispatcher.sh` / `partition_ctl.sh` / systemd 相關。

---

## 9.（可選）便利 wrapper

若要少打字,可加 `chain_code_local/run_local`(3 行):自定位 → `exec bash chain_code_local/run.sh "$@"`。
**非必要**,不影響 pipeline。

---

## 10. Codex 執行步驟(有序)

1. `cp chain_code_nchc/run.sh chain_code_local/run.sh`。
2. 依 §3 對照表逐段改;§4 抽換精確碼貼入;§5/§6/§7 重寫對應區塊。
3. `bash -n chain_code_local/run.sh`(語法)。
4. 跑 §11 驗收清單(pipeline 完整性),逐項記錄 PASS/FAIL。
5. 全綠後**才** commit(繁中訊息;遵守 CLAUDE.md:逐檔 `git add`、禁 `-A`、禁 `--force`)。

---

## 11. Pipeline 完整性驗收清單（Codex 必跑;這是本任務的驗收核心）

> 在乾淨狀態(無 `restart/`、無 `a.out`)下,於專案根執行。

| # | 檢查 | 指令 / 期望 |
|---|---|---|
| V1 | 語法 | `bash -n chain_code_local/run.sh` → rc 0 |
| V2 | 無殘留 SLURM | `grep -nE 'sbatch\|squeue\|sinfo\|sacct\|scontrol\|--h200\|--gb200\|--defer-gen\|HEAD.lockdir\|RUNNING.lockdir\|chain_jobid\|chain_count\|QUEUE_JOBS\|query_partition_status\|gb200_partition\|_BEST_[PC]\|CC_DISPLAY\|JOBSCRIPT\|MODE_CLUSTER' chain_code_local/run.sh` → **空**(全部 SLURM 殘留一次抓乾) |
| V3 | PY 強制 | `grep -nE '\bpython3\b' chain_code_local/run.sh` → 只在註解;實際呼叫全是 `"$PY"`(python3.12) |
| V4 | 網格名 `_s` | run.sh 推導出的 NEW_GRID == `J_Frohlich/adaptive_3.fine grid_I513_J257_s0.950000.dat`(磁碟存在) |
| V5 | origin 偵測 | run.sh 自動找到唯一 `phase2_generatecheckpoint/step_58706001`,不誤抓別的 |
| V6 | 齊全閘 | `_seed_complete` 對 step_58706001 回 true(452 檔齊全);故意刪 1 檔 → 回 false 落 cold |
| V7 | **預設走 Case 2** | 乾淨態執行 `bash chain_code_local/run.sh --preflight-only` → 自動判 Case 2(非 Case 3) |
| V8 | **內插產物** | V7 後:`restart/checkpoint/step_00000001/metadata.dat` 存在;`grid_dims=263,71,263`、`mpi_rank_count=8`、`step=1`、**`dt_global=-1.0`**(因 §4.3 帶 `--skip-drift-check`)、`accu_count=0`;`restart/grid_provenance` 存在;metadata 有 `interp_solver_grid_match=1`(因 §4.3 帶了 `--solver-grid-dat`) |
| V8b | **interp 不自生網格** | run.sh 的 interp 呼叫含 `--no-generate-solver-grid`;§6.4 開頭有 `[ -s "$NEW_GRID" ] \|\| exit 1` 硬擋;把網格暫時移走跑 Case 2 → 應 FATAL 退出(而非偷偷 `grid_zeta --auto` 自補) |
| V9 | **accu 歸零(interp 設計)** | step_00000001 metadata `accu_count == 0` **且** `FTT == 0`。interp 只帶流場(f00..f18 + rho)+ controller state(Force/Force_integral/error_prev/ctrl_initialized/gehrke_activated),**不帶**統計 binaries(interp_checkpoint.py:4070-4108 硬寫 accu=0/FTT=0,否則 fileIO.h:748 會去 load 不存在的 36 個 sum_*.bin 而 abort)。種子 accu_count=10060263 只當 provenance(`interp_origin_accu_count`)。**統計於暖啟後 ~6-8 FTT 重平衡、FTT≥FTT_STATS_START(=10) 才從零重新累積。** |
| V10 | 編譯 | `bash chain_code_local/run.sh --preflight-only --rebuild` → `./a.out` 生成;`file a.out` 含 `ELF 64-bit … x86-64` |
| V11 | wrapper 接上 | `hill_local_chain.sh` 的選檔邏輯挑到 `restart/checkpoint/step_00000001` → `--restart=…`(非 `--cold`) |
| V12 | cfdq guard 安全 | guard 掃描時略過 `cwd=/home/chenpengchung/cfdtest/Edit11_local`(絕對路徑)的 0001/0002;對本專案無 job 時放行;guard 唯讀(只 `echo`+`exit`),**絕不**列出/cancel/動到 Edit11 job |
| V13 | 不真投遞驗證 | 用 `--preflight-only` 或 `--no-submit`:**不**執行 `cfdq add`(驗證期不佔節點) |
| V14 | `--force-cold` | `bash chain_code_local/run.sh --force-cold --preflight-only` → wipe `restart/` 後判 Case 3,**不**產 step_00000001。前提:已刪 144 的 `--force-cold`+`--preflight-only` 互斥 guard(否則此命令會被 FATAL 擋下、V14 過不了)。 |
| V16 | `--skip-drift-check` 已帶 | `grep -- '--skip-drift-check' chain_code_local/run.sh` → 命中(interp 指令含此旗標),且 §6.4 事後檢查 metadata `dt_global == -1.0` |
| V15 | 暖啟一致性(可選,需節點) | 真投一段:solver `[G6] Schema OK`(過 mpi_rank_count==jp、grid_dims==263,71,263)、`Precheck` 不 FATAL、無 `mismatch/FATAL/NaN/--cold`,且 Step/FTT 前進。⚠️ **暖啟當下不會出現** `Statistics loaded … accu_count=`(fileIO.h:805 由 accu_count>0 gating;此時 accu=0)——要等 FTT 重達 FTT_STATS_START 後統計重新累積才會再現,**別把它列為暖啟成功條件**。 |

> V13:驗收期 **不要** 真的 `cfdq add`(會佔 V100、與 Edit11 競節點)。只驗到備料齊全 + guard 正確即可。
> V15 為端到端真跑,等計畫採用後另行排,不在語法/備料驗收必跑項。

---

## 12. 風險與雷區(MUST)

1. **節點 python3.6**:若網格 `.dat` 在投遞前不存在,solver 會在 V100 節點 `system("python3 …grid_zeta_tool.py --auto")`,
   用節點的 python3.6(無 numpy)→ 失敗。**所以 §6.3 必須在母機先把網格生好**。
2. **UTAU dat 非問題(修正使用者原判斷)**:`variables.h:203-211` 的 `UTAU_BOT_DAT/UTAU_TOP_DAT` 在 `/* */` 內
   (已註解),不是 active #define,`main.cu` 的 `#ifdef` 不觸發,缺檔無害。不需為它做任何事。
3. **NEW_GRID 必須 == solver 執行期讀的檔**,且 §4.3 要補 `--solver-grid-dat "$NEW_GRID"`:否則 interp 不寫
   `interp_solver_grid_match`/solver sha256(以 `args.solver_grid_dat` 為條件),solver `PrecheckCheckpointGridConsistency`
   對缺欄位是 **WARN+skip(非 FATAL)**——能跑但少一道保護。傳了才有 match=1 的強一致性檢查。
4. **STRETCH_A 格式**:`printf %.6f 0.95` → `0.950000`(對得上磁碟檔);別讀 GAMMA(它是 `log()` 運算式)。
5. **路徑含空白**:`adaptive_3.fine grid_…` 有空白,全程雙引號。
6. **Edit11 job**:`~/.cfdq/jobs/0001|0002` 屬 `/home/chenpengchung/cfdtest/Edit11_local`(絕對路徑),**絕不**碰
   (guard 以 `[ "$cwd" = "$PWD" ]` 兩端絕對字串比對過濾;guard 唯讀)。
7. **`grid_zeta_tool.py --auto` 可能回寫 `variables.h`**:僅在「重生網格且 GAMMA 被穩定性夾擠」
   (`grid_zeta_tool.py:2300` `abs(gamma_original-gamma)>1e-6` → 2305)時自動改 `#define STRETCH_A`。
   現況不觸發,但 §6.3 規定 `--auto` 後 `git diff --quiet variables.h`,有變更即 FATAL+人工複核(防靜默改物理)。
8. **`--exclusive` 無效用**:cfdq 寫進不被讀的 `mode` 欄位;真正獨占靠 `is_fully_free` 全空閘,別依賴此旗標。
9. **commit 規範**:遵守 CLAUDE.md(繁中、逐檔 add、禁 `-A`/`--force`);三大紀錄檔先 gzip 再 commit。
```
