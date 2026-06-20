# 元件架構統整 — 全域 vs 本專案(daemon / watcher / solver)

> 本檔統整 Edit13_2800ITBLBM 在**本地 CFDLab 叢集**(非 NCHC/SLURM)實際運行時的所有
> 常駐元件:哪些是**全域共用**(跨所有專案)、哪些是**本專案專屬**;以及各自屬於
> **daemon(常駐服務)** / **watcher(週期巡檢產圖)** / **solver(計算)** 哪一類。
> 相關速記亦見 `CLAUDE.md`「本地 cfdq keepalive systemd 化 + 跨節點重複根因」一節。

---

## 0. 運行拓樸一覽

```
 登入節點 cfdlab (無 GPU, SLURM controller DOWN)
 ├─ [全域] cfdq daemon ......... ~/bin/cfdq daemon  (管所有專案 job 佇列)
 ├─ [本專案] hill_watcher.sh ... 每~30s 產 monitor 圖
 ├─ [本專案] keepalive ......... systemd user service(守 cfdq + watcher)
 └─ (Edit14 的 watcher/keepalive 亦在此, 別專案)

 計算節點 CFDLab-1 (8×Tesla V100)
 └─ [本專案] solver ............ mpirun -np 8 ./a.out --restart=…  (cfdq job 0003)

 計算節點 CFDLab-ib11 (8×V100)
 └─ [Edit14] solver ............ cfdq job 0007  (別專案, 絕不碰)

 NFS: home 由 192.168.170.1 匯出, 全節點共掛 → ~/.cfdq/ 、~/.config/systemd/user/ 、
      各專案目錄、live/ 皆為跨節點共享儲存。
```

---

## 1. 🌐 全域 GLOBAL(跨所有專案 / 節點共用,不屬任一專案)

### 1.1 cfdq daemon(= dispatcher)
| 屬性 | 內容 |
|------|------|
| 類型 | **daemon(常駐服務)** |
| 程式 | `~/bin/cfdq daemon` |
| 存活判定 | `~/.cfdq/daemon.lock/alive` 的 mtime age `< 90s` |
| 佇列/狀態 | `~/.cfdq/jobs/<id>/status` |
| 範圍 | **全域單例**,跑在 cfdlab。管理**所有專案**的 job 放置與續鏈 |
| 管轄 job | Edit13 = **job 0003**(@CFDLab-1)、Edit14 = job 0007(@CFDLab-ib11)… |
| 守護者 | **每個專案的 keepalive 都會「順手」守它**(誰先偵測 heartbeat>90s 就重啟;cfdq 自身 `daemon.lock` 保證單例)→ 這就是登入節點 reboot 後 cfdq 被某專案 keepalive 救回、job 0003 照跑的原因 |

### 1.2 NFS 共用 home
| 屬性 | 內容 |
|------|------|
| 類型 | 共享儲存(非程序) |
| 範圍 | `~/.cfdq/`、`~/.config/systemd/user/`、各專案目錄、`live/` 全在共用 NFS |
| ⚠ 重要 | **正是「watcher 暴增」亂象的根因**:user systemd unit 放在共用 NFS + `systemctl --user enable` → 每個有 user-systemd 的節點(登入即觸發)都會起同一個 keepalive → 跨節點重複、flock 跨 NFS 不可靠擋不住。修法見 §4 |

---

## 2. 📦 本專案 Edit13_2800ITBLBM(per-project)

### 2.1 solver(計算)
| 屬性 | 內容 |
|------|------|
| 類型 | **solver(計算)** |
| 程式 | `mpirun -np 8 ./a.out --restart=…` |
| 位置 | **CFDLab-1**(8×Tesla V100) |
| 放置者 | cfdq **job 0003** |
| 進度日誌 | `run_local_*.log`(`[CONV] Step=… FTT=…`),**非** `slurm_*.log` |
| 健康指標 | `--restart`(非 `--cold`)、`[G6] Schema OK`、無 `NaN/DIVERG/FATAL`、`checkrho.dat` 密度 ~1.0、checkpoint 每 0.5 FTT 推進 |

### 2.2 watcher
| 屬性 | 內容 |
|------|------|
| 類型 | **watcher(週期巡檢產圖)** |
| 程式 | `watcher_nchc/hill_watcher.sh` |
| pid | `live/watcher.pid` |
| 位置 | **cfdlab**(登入節點) |
| 行為 | 每 ~30s 跑 `result/4.Ma_U_Time.py` 產 `monitor_convergence_Re2800` / `monitor_latest.png`;FTT 達門檻後另跑 benchmark / tau_wall |
| 計數陷阱 | benchmark 期間會 spawn 短命子殼(`cd result/` 子殼),`ps` 會瞬時看到 **2 個** `hill_watcher.sh` → **1~2 個皆正常**;持久那個才是 `live/watcher.pid` |

### 2.3 keepalive(二次守衛)
| 屬性 | 內容 |
|------|------|
| 類型 | **daemon(systemd user service)** — per-project,但**跨界守護全域 cfdq** |
| 程式 | `chain_code_local/keepalive_watchdog.sh` |
| service | `edit13-local-keepalive.service`(原始檔 `chain_code_local/systemd/`,安裝器 `install_systemd_local.sh`) |
| pid | `live/watchdog.pid`(== service MainPID) |
| 守護對象 | (1) **全域 cfdq daemon**、(2) 本專案 **hill_watcher**、(3) flow_render(見 §2.4) |
| 關鍵設定 | `ConditionHost=cfdlab`(只在 cfdlab 啟動,根治跨節點重複)、`KillMode=process`(stop 只殺本體不波及子程序/全域 cfdq)、`Restart=on-failure`、`linger=yes`(撐過 reboot) |
| 操作 | 重啟用 `systemctl --user restart edit13-local-keepalive.service`,**不要再 nohup**(會搶 flock) |

### 2.4 flow_render(停用中)
| 屬性 | 內容 |
|------|------|
| 類型 | (本應是 watcher 型) |
| 程式 | `chain_code_local/flow_render_loop.sh` — **腳本不存在**,目前**忽略** |
| 影響 | keepalive 每 60s 試啟失敗(寫一行 log),**無害**;不重建、不告警 |

---

## 3. 🚫 Edit14_2800GILBM(別專案,絕不碰)

| 元件 | 內容 |
|------|------|
| solver | cfdq **job 0007** @ **CFDLab-ib11** |
| watcher / keepalive | Edit14 各自 per-project(其 `live/`、`chain_code_*`) |
| 共用點 | **與本專案共用同一個全域 cfdq daemon** —— 守 cfdq 時是共同受益,但 Edit14 的 solver/watcher/keepalive **絕不可碰** |

---

## 4. 一句話分類速查

| 元件 | 歸屬 | 類別 | 位置 |
|------|------|------|------|
| **cfdq daemon**(dispatcher) | 🌐 全域(唯一) | **daemon** | cfdlab |
| Edit13 **keepalive** | 📦 本專案 | **daemon**(systemd,守全域 cfdq + 本專案 watcher) | cfdlab |
| Edit13 **hill_watcher** | 📦 本專案 | **watcher** | cfdlab |
| Edit13 **solver** | 📦 本專案 | **solver**(計算) | CFDLab-1 |
| Edit14 solver/watcher/keepalive | 🚫 別專案 | 同上對應 | CFDLab-ib11 / cfdlab |

---

## 5. 根因 + 修法(watcher 暴增)

**根因:** `~/.config/systemd/user/` 在**共用 NFS home**,故 `systemctl --user enable` 讓
**每個有 user-systemd 的節點(登入即觸發)都啟動同一個 unit** → 跨節點重複起 keepalive、
各自再 spawn watcher;keepalive 的 flock(`live/.watchdog.lock`)**在 NFS 跨節點不可靠**擋不住,
加上壞節點 **CFDLAB-3**(唯讀 FS + 時鐘 +8h、log 出現 `2026-06-20 03:xx`)持鎖/寫錯誤 age →
cfdlab 端反覆把活著的 watcher 判死又重啟 → 15+ 個 watcher、login load 飆高。

**修法(commit `26e81ed`):** unit 內加
- **`ConditionHost=cfdlab`** — 只有 cfdlab 真正啟動,其餘節點 skip(根治跨節點重複)
- **`KillMode=process`** — stop/restart 不連帶 cgroup-kill 它 spawn 的 watcher / 全域 cfdq daemon

**一次性收斂工具:** `chain_code_local/keepalive_resync.sh`(**務必在全新 shell** 執行;
cwd 驗證、只動本專案、不碰全域 cfdq 與 Edit14)。

---

## 6. 稽核陷阱(grep 自我匹配)

用 `ps`+`grep keepalive_watchdog.sh` / `hill_watcher.sh` **數 daemon/watcher** 時,
**你自己的指令列含這些字串會被自己 grep 到** → 多算出幻影 keepalive/watcher,
進而追不存在的 stray。數的時候務必排除 `bash -c` / `snapshot` / 自己的 `$$`。
判專案歸屬一律用 `/proc/PID/cwd`(涵蓋絕對+相對路徑、跨專案安全),**絕不** `pkill -f`。
