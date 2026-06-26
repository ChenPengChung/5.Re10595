#!/bin/bash
# ============================================================================
# grab_poller.sh — Edit12_Krank56002 NCHC 復機自動偵測搶投遞 (Edit12-only)
# ----------------------------------------------------------------------------
# 由 edit12-grab.timer (systemd --user) 每 ~30s 觸發,session-independent
# (linger=yes → 即使無 Claude session、甚至 login node 重開機後也自動續跑)。
#
# 目標:NCHC 維護停機 (2026-06-27 09:00 ~ 06-28 14:00) 結束、叢集一開放,就在
#   ~30s 內自動 warm 搶投 Edit12 的 job(免人手動打 lbm-grab,搶 GPU slot)。
#
# 設計:thin wrapper around `lbm-grab edit12`(已 Codex 驗證的 relauncher),所有
# 「難」的邏輯都重用它:
#   - 偵測「叢集是否已開放」= lbm-grab Pass-0(fail-closed):ACTIVE MAINT reservation /
#     partition 非 up / scontrol 不可用 → 不投;真開放才 Pass-1a warm 投 + Pass-1b 武裝 daemon。
#   - 防重複投遞 = lbm-grab 內建 ALREADY-RUNNING(job 活著=no-op)+ 本檔 .grab_launched 去抖。
#   - Edit12-only = `lbm-grab edit12` 的 only-filter,絕不碰 Edit11/Edit13(遵守跨專案隔離)。
#   - 維護期間不狂投 sbatch:Pass-0 在任何 sbatch 之前就 fail-closed,只做唯讀 scontrol/sinfo。
#
# ★對抗審查 FIX(2026-06-26):自動解除的判據是「曾真的 warm-submit(verdict=LAUNCHED)」,
#   不是「lbm-grab rc=0」。因為 ALREADY-RUNNING 也回 rc=0;若據 rc=0 設旗標,則停機前 job 還活著
#   時的一個 squeue 暫態空檔(正常 chain handoff 或 NCHC squeue federation 顯示 quirk)會被誤判成
#   「搶到了」→ 在真正復機前就自動解除 → 永遠錯過搶投。故只在輸出含 LAUNCHED 時才記 .grab_launched;
#   並把窗口起點移到 job 確定已被砍之後(10:00 > 09:00 停機),雙重消除此誤觸。
#
# 狀態檔(restart/):
#   .grab_disarmed  存在 → 立即退出(成功搶到後自動寫、或手動 touch 停用)
#   .grab_launched  「曾真的 warm-submit(LAUNCHED)」持久旗標 → step-2 據此自動解除;兼作去抖時戳
#   .grab_window    可選兩行(start / end)覆寫預設窗口
#
# 手動停用:touch restart/.grab_disarmed
# 手動重新武裝:rm -f restart/.grab_disarmed restart/.grab_launched && \
#               systemctl --user enable --now edit12-grab.timer
# ============================================================================
set -uo pipefail
ROOT="/home/s8313697/5.Re10595/Edit12_Krank56002"
cd "$ROOT" 2>/dev/null || exit 0
export PATH="/usr/bin:/bin:/usr/local/bin:${PATH:-}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
LOG="live/grab_poller.log"
mkdir -p live restart
TS(){ date '+%F %T'; }

DISARM="restart/.grab_disarmed"
LAUNCHED="restart/.grab_launched"
WINF="restart/.grab_window"

# --- 0. 已解除 → 立即退出(成功搶到後 / 手動停用) ---
[ -f "$DISARM" ] && exit 0

# --- 1. 窗口閘:只在復機窗口內動作。預設起點 06/27 10:00(> 09:00 停機,job 已被砍 → 不會與
#         「停機前還活著的 job」重疊;且早於 06/28 14:00 預期復機,可搶提早開放)。窗口外完全 dormant。
ws='2026-06-27 10:00'; we='2026-06-29 12:00'
if [ -f "$WINF" ]; then
  _w1=$(sed -n 1p "$WINF" 2>/dev/null); _w2=$(sed -n 2p "$WINF" 2>/dev/null)
  [ -n "${_w1:-}" ] && ws="$_w1"; [ -n "${_w2:-}" ] && we="$_w2"
fi
gs=$(date -d "$ws" +%s 2>/dev/null); ge=$(date -d "$we" +%s 2>/dev/null); now=$(date +%s)
if [ -z "${gs:-}" ] || [ -z "${ge:-}" ]; then
  echo "[$(TS)] ⚠ 窗口時間無法解析(ws='$ws' we='$we')→ fail-closed dormant(檢查 restart/.grab_window)" >> "$LOG"
  exit 0
fi
if [ "$now" -lt "$gs" ] || [ "$now" -ge "$ge" ]; then
  exit 0   # 窗口外:零行為,絕不干擾正常運行 / 正常 chain handoff
fi

# --- 2. Edit12 job 是否已在佇列?(以 WorkDir 驗歸屬,跨專案安全,不靠 job 名) ---
mine=$(squeue -h -u "$USER" -o '%i|%T|%Z' 2>/dev/null | awk -F'|' -v r="$ROOT" '$3==r{print $1" "$2; exit}')
if [ -n "$mine" ]; then
  if [ -f "$LAUNCHED" ]; then
    # 我們先前真的 warm-submit 過(.grab_launched)→ 現在 job 在佇列 = 搶到了 → 自動解除 poller。
    # (daemon dispatcher/watcher/watchdog 由 lbm-grab Pass-1b 武裝;此處不碰 daemon,以免在
    #  「daemon 其實活在別 login node」時誤製造跨節點重複。)
    touch "$DISARM"
    systemctl --user disable --now edit12-grab.timer >/dev/null 2>&1 || true
    echo "[$(TS)] GRABBED ✅ Edit12 job 進佇列 ($mine) → 自動解除 poller(.grab_disarmed + disable timer)" >> "$LOG"
  fi
  # 無 .grab_launched = 我們沒投過,這是別人/停機前還活著的 job → 靜默待命,不解除、不動作。
  exit 0
fi

# --- 3. 無 Edit12 job → 嘗試搶(lbm-grab edit12;Pass-0 會在維護未結束時擋下)---
# 去抖:只有「上次真的 LAUNCHED」才等 job 現身(<120s 不重投);維護中 Pass-0 fail-closed(無
# LAUNCHED)不設去抖,故維護一結束就能在下一個 ~30s tick 立刻投出。
if [ -f "$LAUNCHED" ]; then
  lage=$(( now - $(stat -c %Y "$LAUNCHED" 2>/dev/null || echo 0) ))
  if [ "$lage" -lt 120 ]; then
    echo "[$(TS)] 已 LAUNCHED ${lage}s 前,等新 job 在 squeue 現身(<120s 不重投)" >> "$LOG"
    exit 0
  fi
fi
echo "[$(TS)] 偵測無 Edit12 job → lbm-grab edit12(Pass-0 會擋維護中;rc=2=維護未結束屬正常)..." >> "$LOG"
out=$(bash -c 'source "$HOME/.lbm_nchc_grab.sh" 2>/dev/null; lbm-grab edit12' 2>&1); rc=$?
printf '%s\n' "$out" | sed 's/^/    /' >> "$LOG"
echo "[$(TS)] lbm-grab edit12 rc=$rc" >> "$LOG"
# ★只有輸出含 LAUNCHED(真 warm-submit / LAUNCHED-PENDING)才記持久旗標;ALREADY-RUNNING /
#   Pass-0 fail-closed(維護中)/ ABORT 一律不記 → 杜絕「job 還活著時誤觸自動解除」。
if printf '%s' "$out" | grep -qiE 'LAUNCHED'; then
  : > "$LAUNCHED"
  echo "[$(TS)] ✅ 偵測到 LAUNCHED → 記 .grab_launched(下輪 squeue 見 job 即自動解除)" >> "$LOG"
fi
exit 0
