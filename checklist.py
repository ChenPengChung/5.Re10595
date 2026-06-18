#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checklist.py — Edit14_2800GILBM 重要紀錄/狀態檔 checklist 即時生成器.

掃描本專案根目錄下 daemon / chain 關鍵狀態檔的「存在性」, 重新標記 [Y]/[N],
重新蓋上生成時間 + chain head 狀態, 並動態列舉 owner 單例鎖 / slurm 當前 log /
gridgen 歷史 log, 輸出對應的 checklist.txt(預設)。

設計原則:
  * 結構(四區 log / pid / heartbeat / other)、註解、★絕不可刪 / 預期缺 標記
    都寫死在本檔(metadata), 不會因掃描而漂移。
  * 只「讀」檔案系統與 sacct(唯讀), 絕不動 job / chain / checkpoint / 任何狀態檔。
  * 路徑一律相對本專案根目錄(= 本檔所在目錄), 不論從哪個 cwd 執行都正確。

用法:
    python3 checklist.py            # 重新生成 checklist.txt(覆寫)
    python3 checklist.py --stdout   # 印到 stdout, 不寫檔
    python3 checklist.py --check    # 只印摘要; 有「非預期缺漏」則 exit 1
    python3 checklist.py -o foo.txt # 寫到指定路徑

退出碼: 0 = 全部符合預期; 1 = 有非預期缺漏(--check / 一般模式皆適用)。
"""
import argparse
import os
import subprocess
import sys
import time
import unicodedata
from glob import glob

ROOT = os.path.dirname(os.path.abspath(__file__))

# 條目種類:
#   "normal"        預期存在; 缺 = 問題
#   "critical"      預期存在 + 加 ★絕不可刪 標記; 缺 = 嚴重問題(斷鏈/FATAL)
#   "expect_missing" 預期缺席; 存在 = 值得注意(如 STOP_CHAIN 出現)
#   "info"          僅顯示 [Y]/[N], 不納入問題判定(如 .run.lock)
NORMAL, CRITICAL, EXPECT_MISSING, INFO = "normal", "critical", "expect_missing", "info"

MARK_STAR = "★絕不可刪"
DESC_PAD = 47           # critical 條目 desc 補到此顯示寬度後接 ★(超過則僅留 2 空格)
PATH_COL = 37           # 路徑欄左對齊顯示寬度
LINE_W = 78             # 區段分隔線/邊框總寬


# ---------------------------------------------------------------- 顯示寬度工具
def dwidth(s):
    """字串顯示寬度(東亞全形字算 2)。"""
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def dpad(s, width):
    """以空白把 s 補到顯示寬度 width(已超過則不補)。"""
    pad = width - dwidth(s)
    return s + (" " * pad if pad > 0 else "")


# ---------------------------------------------------------------- 唯讀狀態擷取
def read1(rel):
    """讀單行狀態檔內容(strip); 不存在回 None。"""
    p = os.path.join(ROOT, rel)
    try:
        with open(p, "r") as f:
            return f.read().strip()
    except OSError:
        return None


def present(rel):
    """檔案/symlink/目錄是否存在(symlink 即使 dangling 也算存在=指針在)。"""
    return os.path.lexists(os.path.join(ROOT, rel))


def job_state(jid):
    """以 sacct(權威; squeue 在 NCHC 跨叢集聯邦下可能漏列)查 job 狀態。"""
    if not jid:
        return "NONE"
    try:
        r = subprocess.run(
            ["sacct", "-j", str(jid), "-X", "-n", "-P", "-o", "State"],
            capture_output=True, text=True, timeout=15,
        )
        for ln in r.stdout.splitlines():
            ln = ln.strip()
            if ln:
                return ln  # 例: RUNNING / COMPLETED / CANCELLED+ / NODE_FAIL
        return "UNKNOWN"
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"


def symlink_target(rel):
    p = os.path.join(ROOT, rel)
    try:
        return os.readlink(p)
    except OSError:
        return "?"


def binary_manifest_tag():
    """從 binary_manifest.dat 取 'jpNN=md5前8碼' 供描述顯示。"""
    txt = read1("restart/binary_manifest.dat")
    if not txt:
        return "?"
    line = txt.splitlines()[0]
    if "=" in line:
        key, _, val = line.partition("=")
        return f"{key.strip()}={val.strip()[:8]}"
    return line[:16]


# ---------------------------------------------------------------- 渲染
def divider(name):
    label = f" [{name}] "
    left = LINE_W - 2 - len(label) - 7
    return "# " + "-" * max(left, 0) + label + "-" * 7


def fmt(rel, desc, kind, stats):
    ok = present(rel)
    flag = "Y" if ok else "N"
    prefix = f"{dpad(rel, PATH_COL)}  [{flag}]  "          # 固定到 PATH_COL+7 顯示寬
    if kind == CRITICAL:
        body = dpad(desc, DESC_PAD)
        line = prefix + body + "  " + MARK_STAR
    else:
        line = prefix + desc
    if kind in (NORMAL, CRITICAL) and not ok:
        stats["missing"].append((rel, kind))
    elif kind == EXPECT_MISSING and ok:
        stats["appeared"].append(rel)
    return line


# ---------------------------------------------------------------- 主體建構
def build(stats):
    jid = read1("restart/chain_jobid") or ""
    state = job_state(jid)
    ccount = read1("restart/chain_count") or "?"
    part = read1("restart/h200_partition") or "?"
    ckpt = symlink_target("restart/checkpoint/latest")
    bmtag = binary_manifest_tag()
    now = time.strftime("%Y-%m-%d %H:%M %Z")

    L = []  # 輸出行
    a = L.append

    # ---- 檔頭 ----
    a("# " + "=" * (LINE_W - 2))
    a("# Edit14_2800GILBM 重要紀錄/狀態檔 checklist")
    a("# " + "-" * (LINE_W - 2))
    a(f"# 生成時間: {now} | chain head: {jid or '(無)'} ({state})")
    a("# [Y]=存在  [N]=缺失")
    a("# 格式: 四區 [log] / [pid] / [heartbeat] / [other]; owner 單例鎖檔列於 [other]。")
    a("# 用途: 模擬生成期間快速檢查 daemon/chain 狀態檔是否齊全(有無空缺)。")
    a("#       [N] 且非「預期缺」者 = 需查 (daemon 死 / 誤刪 / I/O 異常)。")
    a("# 重新生成: python3 checklist.py        (本檔即此 txt 的產生器)")
    a("# 只看摘要: python3 checklist.py --check (有非預期缺漏 exit 1)")
    a("# " + "=" * (LINE_W - 2))
    a("")

    # ---- [log] ----
    a(divider("log"))
    a(fmt("restart/dispatcher.log", "dispatcher 輪詢/partition×jp 切換日誌 (持續寫)", NORMAL, stats))
    a(fmt("restart/dispatcher_watchdog.log", "dispatcher watchdog 看守日誌 (持續寫)", NORMAL, stats))
    a(fmt("restart/chain.log", "chain 續投歷史 (每輪 sbatch 紀錄)", NORMAL, stats))
    a(fmt("restart/blacklist.log", "partition 壞節點黑名單 (空=無黑名單, 正常)", NORMAL, stats))
    a(fmt("live/watcher.log", "watcher 收斂/benchmark 事件 (持續寫)", NORMAL, stats))
    a(fmt("live/health_watchdog.log", "Route B systemd watchdog 每10分巡檢", NORMAL, stats))
    a(fmt("live/handoff_scout.log", "交棒哨兵稽核 (僅事件時寫, 可能偏舊)", NORMAL, stats))
    a(fmt("chain_code/health_watchdog_alerts.log", "watchdog 警報彙整 (本地; 已 gitignore 不追蹤)", NORMAL, stats))
    # 當前 job 的 slurm log(動態: 由 chain_jobid 推導)
    if jid:
        a(fmt(f"slurm_{jid}.log", "★當前 job solver stdout (正在寫=job 健康)", NORMAL, stats))
        a(fmt(f"slurm_{jid}.err", "當前 job solver stderr (小=無錯誤輸出, 正常)", NORMAL, stats))
    # gridgen 歷史 log(動態 glob; 一次性, 非執行期)
    for g in sorted(glob(os.path.join(ROOT, "J_Frohlich", "gridgen_*.log"))):
        rel = os.path.relpath(g, ROOT)
        a(fmt(rel, "歷史一次性 gridgen log (靜止, 非執行期)", INFO, stats))
    a(fmt("nan_monitor_log.txt", "預期缺: 僅偵測到 NaN 時才生成 (正常)", EXPECT_MISSING, stats))
    a(fmt("animation/gif_render_loop.log", "預期缺: 未開 GIF 渲染迴圈 (正常)", EXPECT_MISSING, stats))
    a("# 註: 缺 .log 多為歷史遺失/自動重建, 非致命; slurm_<jid>.log 必須在且 mtime 新。")
    a("")

    # ---- [pid] ----
    a(divider("pid"))
    a(fmt("restart/dispatcher.pid", "dispatcher PID 檔 (daemon 自寫)", NORMAL, stats))
    a(fmt("live/watcher.pid", "watcher PID 檔 (daemon 自寫)", NORMAL, stats))
    a("# 註: 跨登入節點單例下, pid 檔可能記本節點已死 PID — 屬正常。")
    a("#     判 daemon 是否真活, 以 *.heartbeat 新鮮度(<120s)為準, 非 kill -0 此 pid。")
    a("")

    # ---- [heartbeat] ----
    a(divider("heartbeat"))
    a(fmt("restart/dispatcher.heartbeat", "dispatcher 跨節點單例心跳 host:pid:epoch (應<120s 新鮮)", NORMAL, stats))
    a(fmt("live/watcher.heartbeat", "watcher 跨節點單例心跳 host:pid:epoch (應<120s 新鮮)", NORMAL, stats))
    a("# 註: heartbeat 是「daemon 是否活」的唯一可靠判據(跨節點)。")
    a("#     存在但 epoch 過期(>120s) = owner daemon 可能死 → 查; 缺 = daemon 重啟自建。")
    a("")

    # ---- [other] ----
    a(divider("other"))
    a("# --- chain / partition / binary / checkpoint 關鍵狀態 (★絕不可刪) ---")
    a(fmt("restart/chain_jobid", f"當前 head job ID (={jid or '?'})", CRITICAL, stats))
    a(fmt("restart/chain_count", f"chain 迭代次數 (={ccount})", CRITICAL, stats))
    a(fmt("restart/grid_provenance", "grid+variables.h mtime 一致性; Preflight C 閘門", CRITICAL, stats))
    a(fmt("restart/h200_partition", f"pinned partition (={part}); partition@jps 鎖", CRITICAL, stats))
    a(fmt("restart/throughput_by_jp.dat", "partition×jp 吞吐量表 (dispatcher 切換用)", CRITICAL, stats))
    a(fmt("restart/binary_manifest.dat", f"a.out md5 manifest ({bmtag}; 防舊 binary 蓋回)", CRITICAL, stats))
    a(fmt("restart/checkpoint/latest", f"最新 checkpoint 指針 symlink (->{ckpt})", CRITICAL, stats))
    a(fmt("restart/gb200_partition", "預期缺: 本專案跑 H200, 尚無 GB200 binary", EXPECT_MISSING, stats))
    a("# --- STOP / active 哨兵 (鏈正常續跑時應缺) ---")
    a(fmt("restart/STOP_CHAIN", "預期缺: 存在=已要求停鏈", EXPECT_MISSING, stats))
    a(fmt("restart/STOP_DISPATCHER", "預期缺: 存在=已停 dispatcher", EXPECT_MISSING, stats))
    a(fmt("restart/STOP_NOCAPACITY", "預期缺: GPU 容量不足回退 (曾因誤判出現過)", EXPECT_MISSING, stats))
    a(fmt("restart/DISPATCHER_ACTIVE", "預期缺: 新架構用 nodelock 取代此舊哨兵", EXPECT_MISSING, stats))
    a(fmt("DISPATCHER_ACTIVE", "預期缺: 同上 (root 路徑); 缺席=安全", EXPECT_MISSING, stats))
    a(fmt(".run.lock", "sbatch 互斥鎖 (job 運行中存在屬正常; 見下方註記)", INFO, stats))
    a("# --- owner 單例鎖 (find . -name owner 窮舉; 動態列舉, 一個不漏) ---")
    owner_desc = {
        "restart/dispatcher.nodelock/owner": "dispatcher 跨節點互斥鎖 owner (內容 host:pid; 防多節點重複 daemon)",
        "live/watcher.nodelock/owner": "watcher 跨節點互斥鎖 owner (內容 host:pid)",
        "restart/HEAD.lockdir/owner": "HEAD 投遞鎖 owner (防重複投遞; 內容 state/jobid/cluster/host)",
    }
    # owner 鎖只住在 restart/ live/ 下的 *.nodelock / *.lockdir; 為了讓本檔可被
    # watcher 每輪(~30s)呼叫, 剪掉重子樹(checkpoint 約 9000 entries 等)避免無謂 I/O。
    prune = {".git", "checkpoint", "result", "statistics", "animation",
             "J_Frohlich", "phase1_generategrid", "phase2_generatecheckpoint",
             "__pycache__"}
    owners = []
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in prune]
        if "owner" in files:
            owners.append(os.path.relpath(os.path.join(dirpath, "owner"), ROOT))
    for rel in sorted(owners):
        a(fmt(rel, owner_desc.get(rel, "owner 單例鎖檔 (host:pid)"), NORMAL, stats))
    a("")

    # ---- footer ----
    a("# " + "=" * (LINE_W - 2))
    a("# ⚠️ 註記 (生成時快照):")
    a("#  1. 標 ★絕不可刪 者: 刪除會導致 warm-resume FATAL / Preflight C FATAL / 斷鏈。")
    a("#  2. .run.lock 存在: job RUNNING 中屬正常(防重投)。若 job 已離隊且無活躍")
    a("#     sbatch 仍殘留此鎖 → 下輪自投會被擋, 需手動 rm .run.lock。")
    a("#  3. *.pid 記本節點已死 PID(跨節點單例正常); daemon 真實存活以 heartbeat 判。")
    a("#  4. owner 缺失 = 對應單例鎖被清 → daemon 重啟會以原子 mkdir 重建; 通常自癒。")
    a("#  5. 三大紀錄檔(Ustar_Force_record.dat / timing_log.dat / checkrho.dat)未列上")
    a("#     (非 daemon 狀態檔, 且已 gitignore), 但 solver 運行中持續 append, 同樣絕不可刪。")
    a("# " + "=" * (LINE_W - 2))
    return "\n".join(L) + "\n"


def summary(stats):
    miss = stats["missing"]
    app = stats["appeared"]
    out = []
    crit = [r for r, k in miss if k == CRITICAL]
    norm = [r for r, k in miss if k == NORMAL]
    if crit:
        out.append(f"  ✗ 嚴重缺漏(★絕不可刪) {len(crit)}: " + ", ".join(crit))
    if norm:
        out.append(f"  ✗ 非預期缺漏 {len(norm)}: " + ", ".join(norm))
    if app:
        out.append(f"  ! 預期缺卻出現 {len(app)}: " + ", ".join(app))
    if not out:
        return "  ✓ 全部符合預期(無非預期缺漏)。", 0
    return "\n".join(out), 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Edit14_2800GILBM checklist 即時生成器")
    ap.add_argument("--stdout", action="store_true", help="印到 stdout, 不寫檔")
    ap.add_argument("--check", action="store_true", help="只印摘要; 非預期缺漏則 exit 1")
    ap.add_argument("-o", "--output", default=os.path.join(ROOT, "checklist.txt"),
                    help="輸出路徑 (預設: 專案根 checklist.txt)")
    args = ap.parse_args(argv)

    stats = {"missing": [], "appeared": []}
    text = build(stats)
    msg, code = summary(stats)

    if args.check:
        print(msg)
        return code

    if args.stdout:
        sys.stdout.write(text)
    else:
        with open(args.output, "w") as f:
            f.write(text)
        print(f"已寫入 {os.path.relpath(args.output, ROOT)} ({text.count(chr(10))} 行)")
    print(msg, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
