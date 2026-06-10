#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
checklist.py — 即時重新產生 checklist.txt
================================================================================
本檔為「重要紀錄/狀態檔總清單」的**單一事實來源 (single source of truth)**。
清單規格 (哪些檔、屬哪類、用途、由哪行程式碼生成) 內建於此；每次執行就用
os.path.lexists() 重新探測磁碟，重算 [Y]存在 / [N]缺失，輸出根目錄的 checklist.txt。

安全性: 純 stat 探測 (os.path.lexists / glob / os.readlink)，唯一寫入對象是
checklist.txt。**不碰** 任何 job / dispatcher / watcher / checkpoint / 原始碼，
模擬執行中隨時可跑。

用法:
    python3 checklist.py            # 重新探測並覆寫 checklist.txt (預設)
    python3 checklist.py --stdout   # 只印到螢幕, 不寫檔
    python3 checklist.py --check    # 唯讀檢視 (= --stdout)；另印 Y/N 統計到 stderr

清單來源: workflow 多 agent 掃描 (5 區 + 完整性 critic) → codex 檢視補遺 (13 筆) →
本規格表。要新增/修改紀錄檔, 改下方 DOC 清單即可, 不要手改 checklist.txt。
================================================================================
"""
import os
import sys
import glob
import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))

# ── 顯示寬度 (CJK 全形算 2 格), 用於欄位對齊 ────────────────────────────────────
def dwidth(s):
    w = 0
    for ch in s:
        o = ord(ch)
        # 常見 CJK / 全形範圍
        if (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0x303E or 0x3041 <= o <= 0x33FF or
                0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or 0xA000 <= o <= 0xA4CF or
                0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE4F or
                0xFF00 <= o <= 0xFF60 or 0xFFE0 <= o <= 0xFFE6 or 0x20000 <= o <= 0x3FFFD):
            w += 2
        else:
            w += 1
    return w

def pad(s, width):
    return s + ' ' * max(0, width - dwidth(s))

# ── 紀錄檔規格表 (DOC) ─────────────────────────────────────────────────────────
# 每筆型別:
#   ('H', 文字)                     區段標題 ([log] ...)
#   ('C', 文字)                     區段內小註解 (# locks ...)
#   ('E', dict)                     一筆紀錄檔
# E 的 test 欄 (探測規則):
#   ('f',  path)            概念上單一檔/目錄；mark = lexists(path)
#   ('g',  pat)             glob；任一命中即 [Y]
#   ('gc', pat)             glob + 計數；附註 "(現有 N 份)"
#   ('gm', [pat,...])       多 glob 群組；任一命中即 [Y]
#   ('link', path)          symlink；mark=lexists, 附註 "-> target"
#   ('ckstep',)             checkpoint step 目錄 (排除 .WRITING)；計數
DOC = [
    ('H', '[log] ── 各 daemon / job / solver 的執行日誌 ──'),
    ('E', dict(p='keepalive.log',            t=('f','keepalive.log'), ig=1, d='keepalive cron 引擎日誌', c='chain_code/tools/daemon_keepalive.sh:51')),
    ('E', dict(p='slot_handoff.log',         t=('f','slot_handoff.log'), ig=1, d='slot 交接哨兵日誌', c='chain_code/tools/slot_handoff_sentinel.sh:30')),
    ('E', dict(p='live/watcher.log',         t=('f','live/watcher.log'), ig=1, d='watcher daemon 日誌', c='watcher/hill_watcher.sh:12,34')),
    ('E', dict(p='nan_monitor_log.txt',      t=('f','nan_monitor_log.txt'), ig=1, d='NaN/發散監控日誌', c='chain_code/build_and_submit.sh.*:322')),
    ('E', dict(p='weno7_diag.log',           t=('f','weno7_diag.log'), ig=0, d='WENO7 診斷日誌 (啟用才寫)', c='main.cu:1514,1895')),
    ('E', dict(p='weno7_diag.log.part',      t=('f','weno7_diag.log.part'), ig=0, d='↑ 截斷 .part 原子暫存', c='log_truncate.h:214')),
    ('E', dict(p='restart/chain.log',        t=('f','restart/chain.log'), ig=0, d='chain 主迴圈完整日誌 (tee)', c='chain_code/jobscript_chain.slurm.H200:103')),
    ('E', dict(p='restart/blacklist.log',    t=('f','restart/blacklist.log'), ig=0, d='節點黑名單狀態日誌', c='chain_code/tools/blacklist_lib.sh:190')),
    ('E', dict(p='restart/dispatcher.log',   t=('f','restart/dispatcher.log'), ig=0, d='dispatcher daemon 日誌 (append)', c='chain_code/submit_dispatcher.sh:121')),
    ('E', dict(p='slurm_<jobid>.log',        t=('gc','slurm_*.log'), ig=1, d='Slurm stdout (每輪 job 一份)', c='slurm; jobscript -o')),
    ('E', dict(p='slurm_<jobid>.err',        t=('gc','slurm_*.err'), ig=1, d='Slurm stderr (每輪 job 一份)', c='slurm; jobscript -e')),

    ('H', '[pid] ── daemon 行程 PID 檔 ──'),
    ('E', dict(p='restart/dispatcher.pid',       t=('f','restart/dispatcher.pid'), ig=0, d='dispatcher daemon PID', c='chain_code/dispatcher_start.sh:125')),
    ('E', dict(p='restart/slot_sentinel_v1.pid', t=('f','restart/slot_sentinel_v1.pid'), ig=0, d='slot 交接哨兵 PID (現用名)', c='chain_code/tools/slot_handoff_sentinel.sh')),
    ('E', dict(p='live/watcher.pid',             t=('f','live/watcher.pid'), ig=1, d='watcher daemon PID', c='watcher/hill_watcher.sh:61')),
    ('E', dict(p='restart/slot_handoff.pid',     t=('f','restart/slot_handoff.pid'), ig=1, d='哨兵 PID 舊名 (現用 v1)', c='.gitignore; slot_handoff_sentinel.sh')),

    ('H', '[heartbeat] ── mtime 前進 = daemon 存活 ──'),
    ('E', dict(p='restart/dispatcher.heartbeat',   t=('f','restart/dispatcher.heartbeat'), ig=1, d='dispatcher 心跳 (jobscript 檢查)', c='chain_code/submit_dispatcher.sh:887')),
    ('E', dict(p='restart/slot_handoff.heartbeat', t=('f','restart/slot_handoff.heartbeat'), ig=1, d='哨兵心跳', c='chain_code/tools/slot_handoff_sentinel.sh:76')),
    ('E', dict(p='live/watcher.heartbeat',         t=('f','live/watcher.heartbeat'), ig=1, d='watcher 心跳', c='watcher/hill_watcher.sh:51')),

    ('H', '[owner] ── 鎖擁有者檔 (★全部列出, 記錄存否) ──'),
    ('E', dict(p='restart/HEAD.lockdir/owner',        t=('f','restart/HEAD.lockdir/owner'), ig=0, d='Single-Head 鎖擁有者', c='chain_code/tools/head_lock_lib.sh:53')),
    ('E', dict(p='restart/HEAD.lockdir/.owner.XXXXXX', t=('g','restart/HEAD.lockdir/.owner.*'), ig=1, d='HEAD owner mktemp 暫存 (transient)', c='chain_code/tools/head_lock_lib.sh:118')),
    ('E', dict(p='restart/.headstage.XXXXXX/owner',   t=('g','restart/.headstage.*/owner'), ig=1, d='staging 內 owner (transient)', c='chain_code/tools/head_lock_lib.sh:53')),
    ('E', dict(p='restart/RUNNING.lockdir/owner',     t=('f','restart/RUNNING.lockdir/owner'), ig=1, d='legacy 鎖擁有者 (已停用)', c='chain_code/submit_dispatcher.sh:464')),
    ('E', dict(p='live/watcher.lock.d/owner',         t=('f','live/watcher.lock.d/owner'), ig=1, d='watcher 鎖目錄無 owner (純 mkdir)', c='watcher/hill_watcher.sh:52')),
    ('E', dict(p='live/watcher.nodelock/owner',       t=('f','live/watcher.nodelock/owner'), ig=1, d='此路徑不存在 (範例對照)', c='—')),

    ('H', '[other] ── 鎖/哨兵/計數器/provenance/checkpoint/資料紀錄 ──'),
    ('C', '# locks / lock dirs'),
    ('E', dict(p='.run.lock',                  t=('f','.run.lock'), ig=1, d='run.sh 互斥鎖', c='chain_code/run.sh:498')),
    ('E', dict(p='.keepalive.lock',            t=('f','.keepalive.lock'), ig=1, d='keepalive flock 鎖', c='chain_code/tools/daemon_keepalive.sh:54')),
    ('E', dict(p='restart/HEAD.lockdir/',      t=('f','restart/HEAD.lockdir'), ig=1, d='Single-Head 互斥目錄', c='chain_code/tools/head_lock_lib.sh:70')),
    ('E', dict(p='restart/.headstage.XXXXXX/', t=('g','restart/.headstage.*'), ig=1, d='HEAD 鎖 staging 目錄 (transient)', c='chain_code/tools/head_lock_lib.sh:68')),
    ('E', dict(p='restart/RUNNING.lockdir/',   t=('f','restart/RUNNING.lockdir'), ig=1, d='legacy 鎖目錄 (已停用)', c='chain_code/submit_dispatcher.sh:462')),
    ('E', dict(p='live/watcher.lock.d/',       t=('f','live/watcher.lock.d'), ig=1, d='watcher 單實例互斥目錄', c='watcher/hill_watcher.sh:52')),
    ('E', dict(p='restart/LOCK_COMBO',         t=('f','restart/LOCK_COMBO'), ig=0, d='臨時鎖定 jp|partition 組合', c='chain_code/partition_ctl.sh / changejp.sh')),
    ('E', dict(p='restart/h200_partition',     t=('f','restart/h200_partition'), ig=0, d='H200 partition pin (非預設才寫)', c='chain_code/run.sh:460')),
    ('E', dict(p='restart/gb200_partition',    t=('f','restart/gb200_partition'), ig=0, d='GB200 partition pin (非預設才寫)', c='chain_code/run.sh:460')),
    ('C', '# sentinels (事件觸發, 平時 [N])'),
    ('E', dict(p='DISPATCHER_ACTIVE',          t=('f','DISPATCHER_ACTIVE'), ig=1, d='dispatcher 在跑哨兵 (含 PID)', c='chain_code/submit_dispatcher.sh:146')),
    ('E', dict(p='restart/DISPATCHER_INTENT',  t=('f','restart/DISPATCHER_INTENT'), ig=1, d='dispatcher 啟用意向 (stop 才移除)', c='chain_code/dispatcher_start.sh:103')),
    ('E', dict(p='restart/STOP_CHAIN',         t=('f','restart/STOP_CHAIN'), ig=0, d='軟暫停哨兵 (job-guard stop-chain)', c='chain_code/jobscript_chain.slurm.H200:592')),
    ('E', dict(p='restart/STOP_NOCAPACITY',    t=('f','restart/STOP_NOCAPACITY'), ig=0, d='無資源連續失敗停機哨兵', c='chain_code/submit_dispatcher.sh:1028')),
    ('E', dict(p='STOP_DISPATCHER',            t=('f','STOP_DISPATCHER'), ig=1, d='dispatcher 優雅停機哨兵', c='chain_code/submit_dispatcher.sh:140')),
    ('E', dict(p='restart/jp_switch.inprogress', t=('f','restart/jp_switch.inprogress'), ig=0, d='changejp 原子操作階段日誌', c='chain_code/changejp.sh:272')),
    ('E', dict(p='restart/.watchdog_triggered', t=('f','restart/.watchdog_triggered'), ig=0, d='watchdog 殺 stale 行程後觸發哨兵', c='chain_code/tools/watchdog.sh:91')),
    ('C', '# counters / state'),
    ('E', dict(p='restart/chain_count',        t=('f','restart/chain_count'), ig=0, d='目前 chain 輪次計數', c='chain_code/jobscript_chain.slurm.H200:176')),
    ('E', dict(p='restart/chain_jobid',        t=('f','restart/chain_jobid'), ig=0, d='目前 chain head job 的 jobid', c='chain_code/jobscript_chain.slurm.H200:175')),
    ('E', dict(p='restart/fast_fail_count',    t=('f','restart/fast_fail_count'), ig=0, d='連續快速失敗計數 (發散早停)', c='chain_code/jobscript_chain.slurm.H200:628')),
    ('E', dict(p='restart/jp_controller.state', t=('f','restart/jp_controller.state'), ig=0, d='jp 動態控制器狀態', c='chain_code/submit_dispatcher.sh:706')),
    ('C', '# provenance / manifest'),
    ('E', dict(p='restart/grid_provenance',    t=('f','restart/grid_provenance'), ig=0, d='網格插值 provenance (regrid 驗證)', c='phase2_generatecheckpoint/interp_checkpoint.py')),
    ('E', dict(p='restart/MANIFEST.txt',       t=('f','restart/MANIFEST.txt'), ig=0, d='build/resume 清單 (相容性驗證)', c='solver a.out + build_and_submit.sh')),
    ('E', dict(p='restart/SUMMARY.md',         t=('f','restart/SUMMARY.md'), ig=0, d='人類可讀執行摘要 (每輪 append)', c='chain_code/chain_status.sh:235')),
    ('E', dict(p='restart/bad_nodes',          t=('f','restart/bad_nodes'), ig=0, d='壞節點黑名單', c='chain_code/jobscript_chain.slurm.H200:616')),
    ('E', dict(p='restart/summary/latest.txt', t=('f','restart/summary/latest.txt'), ig=1, d='最新快照狀態', c='chain_code/chain_status.sh:222')),
    ('E', dict(p='restart/summary/checkpoint_index.txt', t=('f','restart/summary/checkpoint_index.txt'), ig=1, d='checkpoint 發現事件索引 (append)', c='chain_code/chain_status.sh:227')),
    ('E', dict(p='restart/summary/snapshots/', t=('f','restart/summary/snapshots'), ig=1, d='生命週期狀態快照目錄', c='chain_code/chain_status.sh:219')),
    ('E', dict(p='restart/HEAD.lockdir.healed.*', t=('g','restart/HEAD.lockdir.healed.*'), ig=1, d='self-heal 損毀 owner 備份', c='chain_code/tools/head_lock_lib.sh:157')),
    ('C', '# checkpoint (rolling retention; 保留最新數份)'),
    ('E', dict(p='restart/checkpoint/',        t=('f','restart/checkpoint'), ig=1, d='checkpoint 根目錄', c='fileIO.h:270')),
    ('E', dict(p='restart/checkpoint/latest',  t=('link','restart/checkpoint/latest'), ig=1, d='最新完成 checkpoint 之 symlink', c='fileIO.h:468')),
    ('E', dict(p='restart/checkpoint/step_<N>/', t=('ckstep',), ig=1, d='每份含 f00..f18/rho/metadata.dat', c='fileIO.h:270')),
    ('E', dict(p='restart/checkpoint/step_<N>.WRITING/', t=('g','restart/checkpoint/step_*.WRITING'), ig=1, d='checkpoint 原子寫入 staging (transient)', c='fileIO.h:266,276')),
    ('E', dict(p='restart/ckpt_bak/',          t=('f','restart/ckpt_bak'), ig=1, d='changejp 切 jp 舊 checkpoint 備份', c='chain_code/changejp.sh:338')),
    ('E', dict(p='restart/summary/',           t=('f','restart/summary'), ig=1, d='checkpoint 摘要目錄', c='chain_code/chain_status.sh')),
    ('C', '# 湍流統計累積 (statistics/; lbm-clean 後平時 [N])'),
    ('E', dict(p='statistics/accu.dat',        t=('f','statistics/accu.dat'), ig=0, d='統計累積計數 / 元資料', c='fileIO.h:956,1165')),
    ('E', dict(p='statistics/<field>/<field>_merged.bin', t=('g','statistics/*/*_merged.bin'), ig=0, d='各場 (u,v,w,uu..) 合併統計輸出', c='fileIO.h:1095')),
    ('C', '# solver 資料紀錄 / 網格診斷 (.dat)'),
    ('E', dict(p='checkrho.dat',               t=('f','checkrho.dat'), ig=1, d='質量守恆監控 (※三大紀錄檔)', c='main.cu:1554')),
    ('E', dict(p='Ustar_Force_record.dat',     t=('f','Ustar_Force_record.dat'), ig=1, d='壁面摩擦力/流場監控 (※三大)', c='monitor.h:256')),
    ('E', dict(p='timing_log.dat',             t=('f','timing_log.dat'), ig=1, d='計時/效能日誌 (※三大)', c='timing.h:263')),
    ('E', dict(p='gilbm_metrics_full.dat',     t=('f','gilbm_metrics_full.dat'), ig=1, d='曲線網格 metric 全場診斷', c='gilbm/metric_terms.h:296')),
    ('E', dict(p='gilbm_metrics.dat',          t=('f','gilbm_metrics.dat'), ig=0, d='metric 精簡輸出 (特定分支才寫)', c='gilbm/metric_terms.h:452')),
    ('E', dict(p='gilbm_contravariant_wall.dat', t=('f','gilbm_contravariant_wall.dat'), ig=0, d='壁面逆變方向分類診斷', c='gilbm/metric_terms.h:532')),
    ('E', dict(p='meshX.DAT',                  t=('f','meshX.DAT'), ig=1, d='X 向 (流向) 網格座標診斷', c='initialization.h:71')),
    ('E', dict(p='meshYZ.DAT',                 t=('f','meshYZ.DAT'), ig=1, d='Y-Z (橫向) 網格座標診斷', c='initialization.h:311')),
    ('E', dict(p='{checkrho,Ustar_Force_record,timing_log}.dat.part', t=('gm',['checkrho.dat.part','Ustar_Force_record.dat.part','timing_log.dat.part']), ig=0, d='日誌截斷 .part 原子暫存 (平時 [N])', c='log_truncate.h:89,139')),
    ('C', '# watcher 監控圖 (CV gate 後才出現的 benchmark 圖平時 [N])'),
    ('E', dict(p='live/monitor_latest.png',    t=('f','live/monitor_latest.png'), ig=1, d='最新收斂監控圖', c='watcher/hill_watcher.sh:166')),
    ('E', dict(p='live/monitor_latest.pdf',    t=('f','live/monitor_latest.pdf'), ig=1, d='最新收斂監控圖 (PDF)', c='watcher/hill_watcher.sh:169')),
    ('E', dict(p='live/fig_{mean_u,mean_v,uu,vv,uv,k}.png', t=('g','live/fig_*.png'), ig=1, d='benchmark 圖 (CV 視窗滿才生成)', c='watcher/hill_watcher.sh:201')),
    ('E', dict(p='live/tau_wall_signed_Re10595_{cf,cp}.png', t=('g','live/tau_wall_signed_*.png'), ig=1, d='壁面剪應力 benchmark 圖 (G2 gate)', c='watcher/hill_watcher.sh:231')),
]

# ── 探測 ──────────────────────────────────────────────────────────────────────
def probe(t):
    """回傳 (mark 'Y'/'N', annot 附註字串)"""
    kind = t[0]
    if kind == 'f':
        return ('Y' if os.path.lexists(t[1]) else 'N', '')
    if kind == 'g':
        return ('Y' if glob.glob(t[1]) else 'N', '')
    if kind == 'gc':
        n = len(glob.glob(t[1]))
        return ('Y' if n else 'N', ' (現有 %d 份)' % n)
    if kind == 'gm':
        hit = any(glob.glob(p) for p in t[1])
        return ('Y' if hit else 'N', '')
    if kind == 'link':
        p = t[1]
        if os.path.lexists(p):
            try:
                tgt = os.readlink(p)
            except OSError:
                tgt = '?'
            return ('Y', ' -> ' + tgt)
        return ('N', '')
    if kind == 'ckstep':
        dirs = [d for d in glob.glob('restart/checkpoint/step_*') if not d.endswith('.WRITING')]
        return ('Y' if dirs else 'N', ' (現有 %d 份)' % len(dirs))
    return ('N', '')

# ── 產生內容 ──────────────────────────────────────────────────────────────────
PATHW = 46   # 路徑欄顯示寬度
DESCW = 34   # 用途欄顯示寬度

def render():
    body = []
    ny = 0
    nn = 0
    for kind, val in DOC:
        if kind == 'H':
            body.append('')
            body.append(val)
            continue
        if kind == 'C':
            body.append(' ' + val)
            continue
        # entry
        mark, annot = probe(val['t'])
        if mark == 'Y':
            ny += 1
        else:
            nn += 1
        disp = val['p'] + annot
        igf = 'ig ' if val.get('ig') else '   '
        line = '  [%s] %s %s%s  (%s)' % (mark, pad(disp, PATHW), igf, pad(val['d'], DESCW), val['c'])
        body.append(line)
    return '\n'.join(body), ny, nn

def build():
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    body, ny, nn = render()
    bar = '=' * 80
    dash = '-' * 80
    head = '\n'.join([
        bar,
        ' Edit8_NewInterpolation — 重要紀錄 / 狀態檔總清單 (CHECKLIST)',
        ' 模擬執行期 (chain / dispatcher / watcher / sentinel / solver) 生成的紀錄檔',
        dash,
        ' [Y] = 此刻存在於磁碟    [N] = 此刻不存在 (缺失 / 尚未生成 / legacy / transient)',
        ' 本檔為 checklist.py 自動生成 (勿手改); 要即時刷新請跑: python3 checklist.py',
        ' 生成時間: ' + now + '   ig = 已被 .gitignore 忽略 (執行期產物, 不進 git)',
        dash,
        ' [N] 不一定代表壞掉: legacy(RUNNING.lockdir) 永久[N]; transient(.owner/.headstage/',
        '   .WRITING/.part) 多數時間[N]; 事件觸發(STOP_*/*_partition/fig_*/fast_fail) 平時[N]',
        ' owner 區段為使用者指定之重點: 列出所有 */owner 鎖擁有者檔, 逐一記錄存否',
        bar,
    ])
    foot = '\n'.join([
        '',
        bar,
        ' 統計 (此刻磁碟): %d 行 [Y] 存在 / %d 行 [N] 缺失  (含 pattern 行)' % (ny, nn),
        ' 驗證鏈: 1) workflow 多 agent 掃描 (5 區 + critic)  2) codex 檢視補遺 (13 筆)',
        '         3) checklist.py 規格表為單一事實來源  4) 每次執行重新 stat 即時刷新',
        bar,
        '',
    ])
    return head + '\n' + body + '\n' + foot, ny, nn

def main():
    os.chdir(ROOT)
    args = set(sys.argv[1:])
    to_stdout = bool(args & {'--stdout', '--check', '-s'})
    content, ny, nn = build()
    if to_stdout:
        sys.stdout.write(content)
        sys.stderr.write('\n[checklist.py] %d [Y] / %d [N]  (唯讀, 未寫檔)\n' % (ny, nn))
    else:
        with open(os.path.join(ROOT, 'checklist.txt'), 'w', encoding='utf-8') as f:
            f.write(content)
        sys.stderr.write('[checklist.py] checklist.txt 已刷新: %d [Y] / %d [N]\n' % (ny, nn))

if __name__ == '__main__':
    main()
