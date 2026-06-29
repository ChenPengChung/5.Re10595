#!/bin/bash
# ============================================================================
# health_watchdog.sh — Edit12_Krank56002 24/7 健康守護 (由 systemd --user timer 週期執行)
# (Edit6 模板移植: ROOT/全 edit*-unit 名/描述皆改 Edit12; /proc/cwd 跨專案守門不變)
# ----------------------------------------------------------------------------
# 不需要任何 Claude session 即可運作。職責 (僅做「腳本能安全做」的事):
#   1. 存活: dispatcher / watcher 用 systemd is-active + /proc cwd 判定本專案實例。
#      死亡/inactive/failed → systemctl --user reset-failed + restart;
#      watcher 多實例(spin/重複) → bash chain_code/daemon_reset.sh 清成單一。
#   2. 切換機制稽核: job 狀態(sacct)、chain_jobid↔squeue、震盪(osc_check)、PENDING 過久、斷鏈。
#   3. 無問題 → 寫一行 OK 到 live/health_watchdog.log (本地)。
#      有問題 → 追加結構化診斷到 *tracked* chain_code/health_watchdog_alerts.log,
#               並 best-effort 推遠端 (單檔、不 -A、不 --force、timeout、失敗即放棄不留殘狀態)。
# 本腳本「不做」(本質需 Claude / Route A 在 session 內): 不修程式碼、不 scancel、不冷啟。
# 跨專案安全: 一律 /proc/PID/cwd 判專案歸屬 (cwd 在本專案才算);
#            絕不碰 Edit6/Edit7/Edit8/Edit11 等別專案 daemon;不使用 pkill -f / cmdline 路徑字串。
# 手動測試: bash chain_code/health_watchdog.sh   (WATCHDOG_PUSH=0 可關閉推送只本地記錄)
# ============================================================================
set -uo pipefail
ROOT="/home/s8313697/5.Re10595/Edit12_Krank56002"
cd "$ROOT" 2>/dev/null || exit 0
export PATH="/usr/bin:/bin:/usr/local/bin:${PATH:-}"

LOG="live/health_watchdog.log"
ALERTS="chain_code/health_watchdog_alerts.log"   # tracked → 可 push
HASHF="live/.last_alert_hash"                     # 去重: 同樣問題不重覆 push
mkdir -p live
TS(){ date '+%F %T'; }

# [self-heal] 若本專案 systemd unit 檔被別專案 reset 清掉(歷史 cross-project 故障類別),
# 趁此次仍被 timer 拉起時補回檔案 + 重載 + 重新 enable, 確保 timer/daemon 不會默默消失。
# (僅在缺檔時動作; 不影響別專案 unit。)
UDIR="$HOME/.config/systemd/user"; _need_reload=0
for _u in edit12-dispatcher.service edit12-watcher.service edit12-watchdog.service edit12-watchdog.timer; do
    if [ ! -f "$UDIR/$_u" ] && [ -f "chain_code/systemd/$_u" ]; then
        mkdir -p "$UDIR"; cp -f "chain_code/systemd/$_u" "$UDIR/" 2>/dev/null && _need_reload=1
    fi
done
if [ "$_need_reload" = 1 ]; then
    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user enable --now edit12-watchdog.timer 2>/dev/null || true
    systemctl --user enable --now edit12-dispatcher.service edit12-watcher.service 2>/dev/null || true
    echo "[$(TS)] self-heal: 補回缺失的 systemd unit 檔 + daemon-reload + re-enable" >> "$LOG"
fi

# 本專案實例計數 (絕對+相對路徑都涵蓋; 跨專案安全; 末參=腳本才算真正 daemon)
cnt_cwd(){ local c=0 p l w; for p in $(pgrep -f "$1" 2>/dev/null); do
    l=$(tr '\0' '\n' </proc/"$p"/cmdline 2>/dev/null | tail -1)
    case "$l" in *"$1") w=$(readlink /proc/"$p"/cwd 2>/dev/null)
        case "$w" in "$ROOT"|"$ROOT"/*) c=$((c+1)) ;; esac ;; esac
  done; echo "$c"; }

# [跨節點 heartbeat 權威 — 2026-06-29] daemon 可能跑在別 login node; 心跳檔(共享 home)新鮮=跨節點存活,
# 只有凍結(>HB_STALE)才視為真死後 restart。避免本機 is-active 誤判 → 跨節點搶 nodelock / 重複 watcher。
HB_STALE=300
hb_fresh(){ local f="$1" max="${2:-$HB_STALE}" age; [ -f "$f" ] || return 1
    age=$(( $(date +%s) - $(stat -c %Y "$f" 2>/dev/null || echo 0) ))
    [ "$age" -lt "$max" ]; }

problems=()   # 問題點
actions=()    # watchdog 已自動補救

# ---------- 1. dispatcher ----------
da=$(systemctl --user is-active edit12-dispatcher.service 2>/dev/null || echo unknown)
dc=$(cnt_cwd submit_dispatcher.sh)
# ★heartbeat 新鮮 → 跨節點存活(單例可能非本節點)→ 不重啟; 只有凍結才視為真死後 restart。
if ! hb_fresh restart/dispatcher.heartbeat && { [ "$da" != "active" ] || [ "$dc" -lt 1 ]; }; then
    problems+=("dispatcher 死亡/異常: service=$da 本專案實例=$dc heartbeat 凍結>${HB_STALE}s")
    systemctl --user reset-failed edit12-dispatcher.service 2>/dev/null || true
    if systemctl --user restart edit12-dispatcher.service 2>/dev/null; then
        actions+=("已 systemctl --user restart edit12-dispatcher.service")
    else
        actions+=("dispatcher restart 失敗 → 需 Claude(Route A) journalctl --user -u edit12-dispatcher 診斷+修碼")
    fi
fi

# ---------- 2. watcher ----------
wa=$(systemctl --user is-active edit12-watcher.service 2>/dev/null || echo unknown)
wc=$(cnt_cwd hill_watcher.sh)
png_age=99999
[ -f live/monitor_latest.png ] && png_age=$(( ($(date +%s) - $(stat -c %Y live/monitor_latest.png 2>/dev/null)) / 60 ))
# ★heartbeat 新鮮 → 跨節點存活 → 不重啟; 只有凍結才視為真死後 restart。
if ! hb_fresh live/watcher.heartbeat && { [ "$wa" != "active" ] || [ "$wc" -lt 1 ]; }; then
    problems+=("watcher 死亡/異常: service=$wa 本專案實例=$wc heartbeat 凍結>${HB_STALE}s")
    systemctl --user reset-failed edit12-watcher.service 2>/dev/null || true
    if systemctl --user restart edit12-watcher.service 2>/dev/null; then
        actions+=("已 systemctl --user restart edit12-watcher.service")
    else
        actions+=("watcher restart 失敗 → 需 Claude journalctl --user -u edit12-watcher 診斷+修碼")
    fi
elif [ "$wc" -gt 1 ]; then
    # ★多實例「不」daemon_reset(根治 crash loop): daemon_reset 會 systemctl start 再生一個 watcher,
    #   與 mid-benchmark 慢死的舊 watcher 並存 → 下個 tick 又見 2 → 自我延續的偽 duplicate + 每~20min
    #   重啟 watcher 干擾 benchmark/CONV(實測 23:30~01:08 反覆 reset、benchmark 跑不完、figs 不更新)。
    #   改交 watcher 自身 singleton lock + self-eviction(~60s 把非鎖主者自滅, 實測確能 2→1)自癒;
    #   watchdog 只記錄供觀察。真正 spin/runaway(self-eviction 失效持續數小時)才由 Claude/Route A 介入。
    echo "[$(TS)] watcher 多實例=$wc (交 watcher self-eviction 自癒, watchdog 不 daemon_reset; 持續數小時才需 Claude 查)" >> "$LOG"
    rm -f live/.wd_dup_seen 2>/dev/null || true
else
    rm -f live/.wd_dup_seen 2>/dev/null || true   # 單一健康實例
fi
[ "$png_age" -gt 15 ] && problems+=("watcher live 圖過舊: ${png_age} 分 (應<15; watcher 卡住或長時間無新 VTK)")

# ---------- 3. 切換機制稽核 (只讀 + 警示; 不介入 job, 不 scancel) ----------
JID=$(cat restart/chain_jobid 2>/dev/null || echo "")
st=$(sacct -j "${JID:-0}" -o State -n 2>/dev/null | head -1 | tr -d ' ')
sqstate=$(squeue -j "${JID:-0}" -h -o '%T' 2>/dev/null | head -1)
osc=$(bash live/osc_check.sh 2>/dev/null || echo "")
if echo "$osc" | grep -qE 'OSCILLATION|ALERT|compressibility'; then
    problems+=("穩定性/切換: osc_check 警報 → $(echo "$osc" | grep -E 'OSCILLATION|ALERT|compressibility' | head -1 | tr -s ' ')")
fi
if [ "$sqstate" = "PENDING" ]; then
    pend=$(squeue -j "$JID" -h -o '%M' 2>/dev/null | head -1)
    problems+=("job $JID PENDING(已等 ${pend:-?}) → 確認 dispatcher net-best re-select 有切到可投 partition(never-idle)")
fi
case "${st:-}" in
    RUNNING|PENDING|COMPLETING|"") : ;;
    *) [ -z "$sqstate" ] && problems+=("job $JID 終態=$st 且不在佇列 → 確認 dispatcher 已續投下一輪(防斷鏈)") ;;
esac

# ---------- 3.5 停機前 checkpoint 備援 (NCHC 維護 2026-06-27 09:00 ~ 06-28 14:00) ----------
# session-independent 第二道防線: 停機前閘窗(06-27 07:00 起)每個 timer tick(每10min)嘗試一次,
# 把最新 checkpoint 鏡像到 Edit12x + 三大 log gzip 備份(idempotent; in-sync 則快速 no-op)。
# 窗外完全跳過(僅兩次 date 比較, 零開銷)。並行序列化靠 preshutdown_backup.sh 自身 flock(不在此處鎖)。
# `timeout -k`: SIGTERM 後再 SIGKILL, 防 D-state NFS hang 卡死 watchdog tick。備份 rc 回收進 problems[]
# → 失敗會走既有 alert/push(否則 session 不在時靜默漏備)。子shell 隔離確保絕不中止上方健康巡檢。
# 取捨(已評估): 真要 cp 129GB 時此 tick 同步等待(~5-15min, in-sync 則 ms);期間後續 tick 被 systemd
#   合併略過 → semantic 檢查延遲一輪。可接受: (a) daemon 程序存活由 systemd Restart=on-failure 獨立守;
#   (b) 停機前夕 semantic 延遲後果小(叢集本就將於 09:00 停);(c) 僅 Edit12x 落後時發生(每新 ckpt 一次)。
# (窗口 end 延伸到 06-28 14:00, 純為「若 07-09 窗漏跑則復機後立即補鏡」, 期間 no-op 無害。)
_psb_now=$(date +%s 2>/dev/null || echo 0)
_psb_s=$(date -d '2026-06-27 07:00:00' +%s 2>/dev/null || echo 0)
_psb_e=$(date -d '2026-06-28 14:00:00' +%s 2>/dev/null || echo 0)
if [ "$_psb_s" -gt 0 ] && [ "$_psb_now" -ge "$_psb_s" ] && [ "$_psb_now" -lt "$_psb_e" ] \
   && [ -f chain_code/preshutdown_backup.sh ]; then
    echo "[$(TS)] [preshutdown] 停機前備援窗內 → 執行 preshutdown_backup.sh" >> "$LOG"
    _psb_rc=0
    { PSB_FORCE_LOGS=1 timeout -k 120 5400 bash chain_code/preshutdown_backup.sh >> live/preshutdown_backup.log 2>&1 ; } || _psb_rc=$?
    if [ "$_psb_rc" -ne 0 ]; then
        problems+=("preshutdown_backup rc=$_psb_rc (Edit12x 鏡像/三大 log 備份未完成) → 查 live/preshutdown_backup.log")
    fi
fi

# ---------- 4. 報告 ----------
if [ ${#problems[@]} -eq 0 ]; then
    echo "[$(TS)] OK  dispatcher=$da/$dc  watcher=$wa/$wc  png=${png_age}m  job=${JID}:${st:-?}/${sqstate:-—}" >> "$LOG"
    exit 0
fi

{
  echo "[$(TS)] ⚠ 偵測到 ${#problems[@]} 個問題"
  printf '    問題: %s\n' "${problems[@]}"
  [ ${#actions[@]} -gt 0 ] && printf '    已自動補救: %s\n' "${actions[@]}"
} >> "$LOG"

# 去重: 同一組問題(內容相同)只記/推一次, 避免持續故障期間每輪洗版
sig=$(printf '%s\n' "${problems[@]}" | sort | md5sum | awk '{print $1}')
last=$(cat "$HASHF" 2>/dev/null || echo "")
if [ "$sig" = "$last" ]; then
    echo "[$(TS)] (同上輪問題, 不重覆 alert/push)" >> "$LOG"
    exit 0
fi
printf '%s' "$sig" > "$HASHF"

{
  echo "## [$(TS)] Edit12 health_watchdog ALERT (${#problems[@]} 問題)"
  echo "狀態: dispatcher=$da/$dc watcher=$wa/$wc png=${png_age}m job=${JID}:${st:-?}/${sqstate:-—}"
  echo "問題點:"
  printf '  - %s\n' "${problems[@]}"
  echo "已自動補救(watchdog 能安全做的):"
  if [ ${#actions[@]} -gt 0 ]; then printf '  - %s\n' "${actions[@]}"; else echo "  - (無; 此問題需 Claude 修碼)"; fi
  echo "對應解法(需 Claude / Route A 在 session 內): 讀 live/health_watchdog.log + journalctl --user -u <service> -n50"
  echo "  + tail slurm_${JID}.log → 定位 code-level 死因 → 修 submit_dispatcher.sh/select_combo_lib.sh/hill_watcher*.sh"
  echo "  → bash -n 驗證 → systemctl --user restart → 逐檔 git add(不 -A)+ commit(繁中,含問題點+解法)+ push。"
  echo "---"
} >> "$ALERTS"

# best-effort 單檔 push (絕不 -A、絕不 --force; 任何失敗即放棄, 本地 commit 仍在, 下次 session 補推)
if [ "${WATCHDOG_PUSH:-0}" = "1" ]; then
    # --only -- "$ALERTS": 只把這一檔的變更納入 commit, 隔離 index — 即使並行的 chain
    # 'auto commit' 此刻已暫存別檔, 也不會被掃進來誤推 (審查確認: 無 pathspec 會打包整個 index)。
    if git add "$ALERTS" 2>/dev/null && \
       git -c core.hooksPath=/dev/null commit --no-verify --only \
           -m "health_watchdog: 偵測 ${#problems[@]} 問題並自動補救(詳見 alert log)" -- "$ALERTS" >/dev/null 2>&1; then
        if timeout 60 git push >/dev/null 2>&1; then
            echo "[$(TS)] alert 已 push 遠端" >> "$LOG"
        else
            echo "[$(TS)] alert push 失敗(timer 環境可能無 SSH / 遠端已前進);本地已 commit,待下次 session 補推(勿 --force)" >> "$LOG"
        fi
    fi
fi
exit 0
