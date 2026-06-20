# 本地 cfdq 運行架構：全域 vs 本專案 完整羅列

> 適用：`Edit14_2800GILBM`(本地 cfdq 生產,母機 `cfdlab` + V100 compute nodes,**非** NCHC/SLURM)。
> 姊妹專案 `Edit13_2800ITBLBM` 架構完全相同(命名換 `edit13-`)。
> 最後更新:2026-06-20。對應 commit:Edit14 `2a11523`+`1fa5e3a`、Edit13 `26e81ed`。

---

## 0. 一句話總結

- **daemon(dispatcher)= 全域**:1 個 cfdq daemon,Edit11/Edit13/Edit14 **共用**,非 systemd unit。
- **watcher = 每專案各一隻**(Edit14 一隻、Edit13 一隻),非 systemd unit,由該專案 keepalive 保活。
- **keepalive = 每專案各一個 systemd 服務**(`edit14-local-keepalive.service` / `edit13-local-keepalive.service`);
  **這是唯一上 systemd 的元件**,它同時保活「自己專案的 watcher」+「那顆全域 daemon」。
- **linger = 全域(per-user)**:開一次,所有專案的 systemd user 服務都受惠。

```
systemd (user, linger, ConditionHost=cfdlab)
  └─ edit14-local-keepalive.service        ← 每專案各一 (systemd 守它, Restart=on-failure)
       ├─ hill_watcher.sh   (watcher)      ← 每專案各一 (keepalive nohup 保活, 8>&-)
       └─ cfdq daemon       (dispatcher)   ← 全域單一 (Edit11/13/14 共用; keepalive nohup + singleton 鎖仲裁)
```

---

## 1. 🌐 全域元件(GLOBAL,跨 Edit11 / Edit13 / Edit14 **共用一份**)

| 元件 | 實體 / 路徑 | 說明 |
|------|------------|------|
| **cfdq daemon(dispatcher)** | **1 個** process(目前 pid 2232),`/home/chenpengchung/bin/cfdq daemon` | **全域單例**,排程/續鏈**所有**專案的 job。**不是** systemd unit;目前跑在母機 tmux session `cfdq` |
| daemon 單例鎖 | `~/.cfdq/daemon.lock/`(`alive`、`owner`) | NFS mtime 判活;`alive` age>90s 視為死 |
| daemon log / stop 哨符 | `~/.cfdq/daemon.log`、`~/.cfdq/daemon.stop` | 全域 |
| job 佇列 | `~/.cfdq/jobs/*/`(每筆有 `spec`/`status`/`run`) | 全域(0007=edit14, 0003=edit13, 0001/2/6/8=edit11) |
| cfdq 工具本體 | `~/bin/cfdq` | 全域 script(**本次 systemd 轉換維持原狀未改**) |
| **linger** | `loginctl enable-linger chenpengchung`(per-**user**) | 全域:開一次,所有專案 systemd user 服務 reboot/登出後都自起 |
| systemd user 單元目錄 | `~/.config/systemd/user/`(**共享 NFS home**) | 全域可見 → 故每個 unit 必須 `ConditionHost=cfdlab` 防跨節點重複啟動(見 §3) |

---

## 2. 📦 本專案專屬元件(Edit14_2800GILBM,**每專案各一份**)

| 元件 | 實體 / 路徑 | 說明 |
|------|------------|------|
| **keepalive(二次守衛)** | process(目前 pid 296270);`chain_code_local/keepalive_watchdog.sh` | **每專案各一隻**。守「本專案 watcher」+「全域 daemon」。**唯一上 systemd 者** |
| **keepalive 的 systemd unit** | **`edit14-local-keepalive.service`** | 原始檔 `chain_code_local/systemd/`,裝到 `~/.config/systemd/user/`。Edit13 對應 `edit13-local-keepalive.service` |
| keepalive 自我鎖(同節點單例) | `live/.watchdog.lock`(flock,fd 8) | 子程序皆 `8>&-` 關閉此 fd,避免繼承鎖卡住下一個 keepalive |
| keepalive pid / log | `live/watchdog.pid`、`live/keepalive_watchdog.log` | 每專案 |
| **watcher** | process(目前 pid 296279);`watcher_nchc/hill_watcher.sh` | **每專案各一隻**,/30s 產收斂/benchmark 圖。**不是** systemd unit |
| watcher 跨節點單例鎖 | `live/watcher.nodelock/`(原子 mkdir)、`live/watcher.heartbeat` | 每專案;全叢集同時只有一隻 watcher 真正工作 |
| watcher pid / 產圖 | `live/watcher.pid`、`live/monitor_latest.png` 等 | 每專案 |
| **job(本專案那筆)** | **0007**(在全域 `~/.cfdq/jobs/` 內) | job 歸屬 edit14,但放在全域佇列 |
| **solver 程序** | compute node `CFDLab-ib11` pid 409385(a.out) | 每專案(job 0007 的求解器) |
| 專案輸出 | `run_local_*.log`、`checkrho.dat`、`restart/`、`statistics/` 等 | 每專案 |

---

## 3. systemd 模型細節(為什麼這樣設計)

本專案三常駐原本都是 tmux/nohup 子程序 → session/tmux 一掉就全死
(2026-06-19 16:10 watcher+keepalive 同死、監控斷線 2h20m)。**改為 keepalive-only systemd 模型**:

| 設定 | 值 | 原因 |
|------|----|------|
| `Restart=` | `on-failure` | keepalive flock-defer / watcher 跨節點退讓時主動 `exit 0`,**不可** `always`(否則 tight-loop 狂起);crash(非零)才復活 |
| `KillMode=` | `process` | `stop/restart` 只終止 keepalive 本體,**不波及** nohup spawn 的 watcher 與(萬一由它復活的)全域 daemon。預設 control-group 會連 cgroup 子程序一起殺 → 會誤殺全域 daemon(跨專案災難) |
| `ConditionHost=` | `cfdlab` | `~/.config/systemd/user` 在共享 NFS → enable 等於**每個有 user systemd 實例的節點(登入即觸發)都會啟動** → 跨節點重複起 watcher。限定只有母機 cfdlab 真正啟動,其餘節點 skip(不算失敗) |
| `8>&-`(腳本內) | 各 nohup | 子程序不繼承 keepalive 的 flock fd8,否則 keepalive 死後子程序仍持鎖 → 下一個 keepalive 搶不到鎖,保活鏈斷裂 |

**為何 cfdq daemon 不做成 systemd unit**:它是 Edit11/13/14 共用的全域單例。若某專案獨佔一個
`cfdq-daemon.service`,會與其他專案還活著的 keepalive(也 `nohup cfdq daemon`)搶同一顆 daemon →
singleton 鎖互打。故兩專案一律以 keepalive(nohup + 鎖)保活全域 daemon,**不搶 systemd 擁有權**。

---

## 4. 跨專案安全(MUST)

- 殺/數程序**一律用 `/proc/PID/cwd` 判專案歸屬**(涵蓋絕對+相對路徑);**絕不** `pkill -f`/cmdline 字串。
- **絕不**碰別專案的服務/job/daemon:修 Edit14 只碰 Edit14、修 Edit13 只碰 Edit13;Edit11(cfdtest/Edit11_local)完全不碰。
- 全域 cfdq daemon 是唯一「合法被本專案 keepalive 管理」的共用元件(它本就由各專案 keepalive 以鎖+nohup 保活)。
- SLURM/job 操作禁令見 `CLAUDE.md`(本地 cfdq 不用 scancel,但同樣禁碰別專案 job)。

---

## 5. 操作速查

```bash
# 安裝 / 冪等重裝(本專案 keepalive systemd 服務)
bash chain_code_local/install_systemd_local.sh

# 存活看 systemd(唯一真相)
systemctl --user is-active edit14-local-keepalive.service        # 應 active
loginctl show-user $USER | grep Linger                           # 應 Linger=yes

# 死/卡了重拉(它再 nohup 拉回 watcher + daemon)
systemctl --user restart edit14-local-keepalive.service

# 全域 daemon 狀態(非 systemd)
cfdq ls                                                          # daemon: running + job 列表

# 巡檢(唯讀)
/edit14-monitor          # 或 /loop /edit14-monitor 連續巡檢
```

---

## 6. 已知環境問題(非軟體,需管理員)

- **CFDLAB-3 壞節點**:檔案系統 read-only、時鐘 +8h、`/usr/bin/sort: Input/output error`。
  其上的 watcher 曾因 future-dated 心跳干擾母機;已由 `ConditionHost=cfdlab` 隔離(其上不再起本專案服務)。
  **此為硬體/admin 層級,軟體已對它免疫**。

---

## 7. 與 CLAUDE.md 的關係

`CLAUDE.md`「本地 cfdq systemd 開機自起」章節是規範摘要;本檔是**完整 全域/專案 元件清單**。
兩者一致:keepalive-only + ConditionHost=cfdlab + KillMode=process + 8>&-,daemon 維持全域 nohup。
