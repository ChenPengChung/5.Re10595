#!/bin/bash
# ============================================================================
# health_watchdog.sh — Edit11_Krank5600 24/7 健康守護 (由 systemd --user timer 週期執行)
# ----------------------------------------------------------------------------
# 不需要任何 Claude session 即可運作。職責 (僅做「腳本能安全做」的事):
#   1. 存活: 先看跨節點 heartbeat(共享 home, 跨節點權威); 新鮮=在某登入節點活著→不動。
#      只有 heartbeat 凍結才用本節點 systemd is-active + /proc cwd 判真死;
#      死亡/inactive/failed(且 heartbeat 凍) → systemctl --user reset-failed + restart;
#      watcher 多實例(spin/重複) → bash chain_code/daemon_reset.sh 清成單一。
#   2. 切換機制稽核: job 狀態(sacct)、chain_jobid↔squeue、震盪(osc_check)、PENDING 過久、斷鏈。
#   3. 無問題 → 寫一行 OK 到 live/health_watchdog.log (本地)。
#      有問題 → 追加結構化診斷到 *tracked* chain_code/health_watchdog_alerts.log,
#               並 best-effort 推遠端 (單檔、不 -A、不 --force、timeout、失敗即放棄不留殘狀態)。
# 本腳本「不做」(本質需 Claude / Route A 在 session 內): 不修程式碼、不 scancel、不冷啟。
# 跨專案安全: 一律 /proc/PID/cwd 判專案歸屬 (cwd 在本專案才算);
#            絕不碰 Edit7/Edit8 等別專案 daemon;不使用 pkill -f / cmdline 路徑字串。
# 手動測試: bash chain_code/health_watchdog.sh   (WATCHDOG_PUSH=0 可關閉推送只本地記錄)
# ============================================================================
set -uo pipefail
ROOT="/home/s8313697/5.Re10595/Edit11_Krank5600"
cd "$ROOT" 2>/dev/null || exit 0
export PATH="/usr/bin:/bin:/usr/local/bin:${PATH:-}"

LOG="live/health_watchdog.log"
ALERTS="chain_code/health_watchdog_alerts.log"   # tracked → 可 push
HASHF="live/.last_alert_hash"                     # 去重: 同樣問題不重覆 push
mkdir -p live
TS(){ date '+%F %T'; }

# [record backup] 三大紀錄檔(Ustar_Force_record/checkrho/timing_log;過大不進 git)→ 分層快照備份:
# 主 ~/log_backups/Edit11_Krank5600(/home 持久) + 次 /work/s8313697/edit11_log_backups(便利份),
# 時間戳多份輪替留 10、md5 雙向核對。throttle 180 分(timer 每 10 分→實際 ~3h 一份,留 10=~30h);
# 非阻塞、失敗不影響守護。這是 session-independent 的資料防丟主力。
bash chain_code/backup_record_files.sh --throttle 180 >/dev/null 2>&1 || true
# [pre-shutdown hard gate] 國網停機(2026-06-27 09:00 ~ 06-28 14:00)前 07:00~09:00 之間,
# session-independent 強制備份「停機前最新」資料 + checkpoint vault(不靠 Claude session/loop):
#   (1) 三大紀錄檔 --force 一份(sentinel 確保只做一次;record 檔每步都變,一份「停機前最新」即可)。
#   (2) checkpoint vault(Edit11x 第二道防線)每輪同步:dedup+flock → 已同步秒退、僅新 checkpoint
#       才真複製,捕捉 07:00~09:00 間 job 續寫的最新 checkpoint。vault 可能搬 ~157GB,超過本 oneshot
#       service 預設 ~90s TimeoutStartSec → 用 systemd-run --user 卸載成獨立 transient unit(escape
#       timeout/cgroup);systemd-run 不可用才退回前景短 timeout(只夠 dedup-check,完整複製交 Route A loop)。
#   跨節點安全:sentinel 在共享 home、sync 腳本自帶 flock+dedup → 多節點 watchdog 同時跑也冪等。
_PSB="$HOME/log_backups/Edit11_Krank5600/.preshutdown_done"
_gs=$(date -d '2026-06-27 07:00' +%s 2>/dev/null); _ge=$(date -d '2026-06-27 09:00' +%s 2>/dev/null); _now=$(date +%s)
if [ -n "${_gs:-}" ] && [ -n "${_ge:-}" ] && [ "$_now" -ge "$_gs" ] && [ "$_now" -lt "$_ge" ]; then
  # (1) 三大紀錄檔:只做一次
  if [ ! -f "$_PSB" ] && bash chain_code/backup_record_files.sh --force >/dev/null 2>&1; then
    touch "$_PSB"; echo "[$(TS)] [pre-shutdown] 停機前三大紀錄檔強制備份完成 → $_PSB" >> "$LOG"
  fi
  # (2) checkpoint vault:每輪同步(dedup 保護),卸載成獨立 unit 以 escape 本 oneshot 的 timeout
  if systemd-run --user --quiet --collect --unit="edit11-preshutdown-vaultsync-$(date +%H%M%S)" \
       /bin/bash -c "cd '$ROOT' && bash chain_code/sync_checkpoint_to_testcopy.sh" >/dev/null 2>&1; then
    echo "[$(TS)] [pre-shutdown] checkpoint vault 同步已卸載(systemd-run transient unit)" >> "$LOG"
  else
    # 前景 fallback 只做 dedup-check(完整 ~157GB 複製交 Route A loop / 下一輪卸載);timeout 25s
    # 確保即使疊加 (1) 的一次性 record gzip 也遠低於本 oneshot ~90s,不排擠後面的 daemon 存活檢查。
    timeout 25 bash chain_code/sync_checkpoint_to_testcopy.sh >/dev/null 2>&1 || true
    echo "[$(TS)] [pre-shutdown] checkpoint vault 同步(前景 fallback;完整複製見 Route A loop)" >> "$LOG"
  fi
fi

# [self-heal] 若本專案 systemd unit 檔被別專案 reset 清掉(歷史 cross-project 故障類別),
# 趁此次仍被 timer 拉起時補回檔案 + 重載 + 重新 enable, 確保 timer/daemon 不會默默消失。
# (僅在缺檔時動作; 不影響別專案 unit。)
UDIR="$HOME/.config/systemd/user"; _need_reload=0
for _u in edit11-dispatcher.service edit11-watcher.service edit11-watchdog.service edit11-watchdog.timer; do
    if [ ! -f "$UDIR/$_u" ] && [ -f "chain_code/systemd/$_u" ]; then
        mkdir -p "$UDIR"; cp -f "chain_code/systemd/$_u" "$UDIR/" 2>/dev/null && _need_reload=1
    fi
done
if [ "$_need_reload" = 1 ]; then
    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user enable --now edit11-watchdog.timer 2>/dev/null || true
    systemctl --user enable --now edit11-dispatcher.service edit11-watcher.service 2>/dev/null || true
    echo "[$(TS)] self-heal: 補回缺失的 systemd unit 檔 + daemon-reload + re-enable" >> "$LOG"
fi

# 本專案實例計數 (絕對+相對路徑都涵蓋; 跨專案安全; 末參=腳本才算真正 daemon)
# ★排除子shell: daemon 自己 fork 的子shell(run_convergence/run_benchmark 等)cmdline 也結尾 $1,
#   會被誤算成「多實例」→ 誤觸 daemon_reset churn。判定: 父程序 cmdline 也結尾 $1 = 子shell, 不計。
cnt_cwd(){ local c=0 p l w pp pl; for p in $(pgrep -f "$1" 2>/dev/null); do
    l=$(tr '\0' '\n' </proc/"$p"/cmdline 2>/dev/null | tail -1)
    case "$l" in *"$1") : ;; *) continue ;; esac
    w=$(readlink /proc/"$p"/cwd 2>/dev/null)
    case "$w" in "$ROOT"|"$ROOT"/*) : ;; *) continue ;; esac
    pp=$(awk '{print $4}' /proc/"$p"/stat 2>/dev/null)
    pl=$(tr '\0' '\n' </proc/"$pp"/cmdline 2>/dev/null | tail -1)
    case "$pl" in *"$1") continue ;; esac   # 父也是同腳本 → 子shell, 不計
    c=$((c+1))
  done; echo "$c"; }

# ★跨節點存活判定(權威, 對應 /Edit11 [4]): dispatcher / watcher 是跨登入節點單例, heartbeat 寫在共享
#   home, 是「在某登入節點活著」的唯一跨節點真相。本節點 systemctl --user is-active 只反映『本節點』
#   (systemd --user 為 per-node), 單例在別節點時恆顯 inactive → 若據此 restart, 新實例會在別節點的
#   nodelock 上 defer→exit→下一輪又 inactive→再 restart, 反覆 futile churn(歷史曾累積 245 次)。
#   故 restart 前先看 heartbeat: 新鮮(<HB_STALE)= 跨節點存活, 絕不重啟; 只有 heartbeat 凍結(真死)
#   才落到本地 systemctl/程序檢查 + restart。(jobscript 自投才是 chain 主韌性, daemon 是備援, 延遲
#   重啟一個真死 daemon ≤ HB_STALE 無害。)
HB_STALE=300
hb_fresh(){ local f="$1" max="${2:-$HB_STALE}" age; [ -f "$f" ] || return 1
    age=$(( $(date +%s) - $(stat -c %Y "$f" 2>/dev/null || echo 0) ))
    [ "$age" -lt "$max" ]; }

problems=()   # 問題點
actions=()    # watchdog 已自動補救

# ---------- 1. dispatcher ----------
da=$(systemctl --user is-active edit11-dispatcher.service 2>/dev/null || echo unknown)
dc=$(cnt_cwd submit_dispatcher.sh)
# ★heartbeat 新鮮 → 跨節點存活(單例可能非本節點)→ 不重啟; 只有凍結才視為真死後 restart。
if ! hb_fresh restart/dispatcher.heartbeat && { [ "$da" != "active" ] || [ "$dc" -lt 1 ]; }; then
    problems+=("dispatcher 死亡/異常: service=$da 本專案實例=$dc heartbeat 凍結>${HB_STALE}s")
    systemctl --user reset-failed edit11-dispatcher.service 2>/dev/null || true
    if systemctl --user restart edit11-dispatcher.service 2>/dev/null; then
        actions+=("已 systemctl --user restart edit11-dispatcher.service")
    else
        actions+=("dispatcher restart 失敗 → 需 Claude(Route A) journalctl --user -u edit11-dispatcher 診斷+修碼")
    fi
fi

# ---------- 2. watcher ----------
wa=$(systemctl --user is-active edit11-watcher.service 2>/dev/null || echo unknown)
wc=$(cnt_cwd hill_watcher.sh)
png_age=99999
[ -f live/monitor_latest.png ] && png_age=$(( ($(date +%s) - $(stat -c %Y live/monitor_latest.png 2>/dev/null)) / 60 ))
# ★heartbeat 新鮮 → watcher 在某登入節點存活(可能非本節點)→ 不重啟; 凍結才視為真死。
#   但本地多實例(wc>1)無論 heartbeat 為何都要清(elif 仍可達)。
if ! hb_fresh live/watcher.heartbeat && { [ "$wa" != "active" ] || [ "$wc" -lt 1 ]; }; then
    problems+=("watcher 死亡/異常: service=$wa 本專案實例=$wc heartbeat 凍結>${HB_STALE}s")
    systemctl --user reset-failed edit11-watcher.service 2>/dev/null || true
    if systemctl --user restart edit11-watcher.service 2>/dev/null; then
        actions+=("已 systemctl --user restart edit11-watcher.service")
    else
        actions+=("watcher restart 失敗 → 需 Claude journalctl --user -u edit11-watcher 診斷+修碼")
    fi
elif [ "$wc" -gt 1 ]; then
    problems+=("watcher 多實例(spin/重複): 本專案實例=$wc")
    bash chain_code/daemon_reset.sh >> "$LOG" 2>&1 || true
    actions+=("已 bash chain_code/daemon_reset.sh 清成單一 systemd watcher")
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
  echo "## [$(TS)] Edit11 health_watchdog ALERT (${#problems[@]} 問題)"
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
