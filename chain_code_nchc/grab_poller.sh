#!/bin/bash
# ============================================================================
# grab_poller.sh — Edit13 復機自動搶投 (option-A 薄包裝; 與 Edit11 session 對齊)
# ----------------------------------------------------------------------------
# 由 edit13-grab.timer (~30s) 觸發。架構 = robot 專屬外殼 + 共用 grab 引擎:
#   ① 視窗閘 (robot-specific): 只在 6/28 12:00→6/29 12:00 內動作; 窗外便宜 exit。
#   ② 冪等: restart/.grab_disarmed 存在 = 已成功搶投過 → 直接 exit, 不再呼叫。
#   ③ source ~/.lbm_nchc_grab.sh → 呼叫 `lbm-grab edit13` (= 你手動 `ll edit13` 的
#      同一引擎: Pass-0 fail-closed + alive 雙源 + Gate-A/B + cfg 守鎖 + warm 投 +
#      序列武裝 + verify, codex 五輪複檢。partition/account/jp 全由 registry+jobscript
#      決定, 不在本檔重造)。
#   ④ grep 該次 VERDICT, 只在「本 poller 自己 LAUNCH」(LAUNCHED/LAUNCHED-PENDING) 才
#      寫 restart/.grab_disarmed + `systemctl --user disable --now` 自我解除 timer。
#      ALREADY-RUNNING / ABORT-* / FAIL / Pass-0-未過 → 不解除 (續輪詢或無害空轉)。
# 安全: 全繼承 lbm-grab (warm-only, 單頭, 不冷啟, 跨專案隔離); robot 只多「視窗閘 +
#       自我解除」。雙頭防護: lbm-grab 的 ALREADY-RUNNING dedup + HEAD.lockdir。
# 測試: LBM_GRAB_FORCE_WINDOW=1 略過視窗閘; LBM_DRY=1 → lbm-grab 走 DRY (verdict
#       DRY-LAUNCH, 本 poller 只記 would-disarm、不真寫 flag/不真解除)。
# ============================================================================
set -uo pipefail
_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
CHAIN_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
ROOT="$(cd "$CHAIN_DIR/.." && pwd)"
LOGDIR="$ROOT/live"; mkdir -p "$LOGDIR" 2>/dev/null
LOG="$LOGDIR/grab_poller.log"
GRAB_SH="$HOME/.lbm_nchc_grab.sh"
DISARMED="$ROOT/restart/.grab_disarmed"
TIMER_UNIT="edit13-grab.timer"
DRY="${LBM_DRY:-0}"
log(){ printf '[%s] %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

# ① 視窗閘 (robot-specific; 窗外便宜 exit, 絕不干擾)
WINDOW_START="2026-06-28 12:00:00"; WINDOW_END="2026-06-29 12:00:00"
NOW=$(date +%s)
WS=$(date -d "$WINDOW_START" +%s 2>/dev/null || echo 0)
WE=$(date -d "$WINDOW_END" +%s 2>/dev/null || echo 0)
if [ "${LBM_GRAB_FORCE_WINDOW:-0}" != 1 ]; then
    { [ "$NOW" -lt "$WS" ] || [ "$NOW" -gt "$WE" ]; } && exit 0
fi

# ② 已成功搶投過 → 冪等 exit (不再呼叫 lbm-grab)
[ -f "$DISARMED" ] && exit 0

# ③ 載入共用 grab 引擎 + 呼叫 lbm-grab edit13 (Pass-0 fail-closed 全在裡面)
[ -f "$GRAB_SH" ] || { log "FATAL: $GRAB_SH 不存在 → 無法搶投"; exit 0; }
source "$GRAB_SH" 2>/dev/null
type lbm-grab >/dev/null 2>&1 || { log "FATAL: lbm-grab 函式未定義 (source 失敗?)"; exit 0; }
OUT=$(lbm-grab edit13 2>&1)
VERDICT=$(printf '%s\n' "$OUT" | awk '$1=="edit13"{print $2; exit}')
log "lbm-grab edit13 → VERDICT=${VERDICT:-<無表/Pass-0未過>}"

# ④ 依 VERDICT 決策 (只在本 poller 自己 LAUNCH 才解除)
disarm_real(){
    printf 'grabbed %s via lbm-grab edit13 (verdict=%s)\n' "$(date '+%F %T')" "$1" > "$DISARMED"
    systemctl --user disable --now "$TIMER_UNIT" 2>/dev/null
    log "✓ GRABBED (verdict=$1) → 寫 .grab_disarmed + 解除 $TIMER_UNIT (不再輪詢)"
}
case "${VERDICT:-}" in
    LAUNCHED|LAUNCHED-PENDING)
        disarm_real "$VERDICT" ;;                      # 真投成功 (DRY 不會到此, DRY→DRY-LAUNCH)
    DRY-LAUNCH)
        log "would-GRAB + would-disarm (DRY; verdict=DRY-LAUNCH, 不真寫 flag/不真解除)" ;;
    ALREADY-RUNNING|RESUME-ARM)
        log "Edit13 已活/重武裝 (verdict=$VERDICT) → 不解除 (只認本 poller 自己 LAUNCH); 無害空轉至視窗關" ;;
    ABORT-*|FAIL)
        log "未搶到 (verdict=$VERDICT) → 保留 timer 下輪重試 [$(printf '%s' "$OUT" | grep -iE 'MISCONFIG|ABORT|FAIL' | head -1 | tr -s ' ' | cut -c1-90)]" ;;
    *)
        log "Pass-0 未過/維護中 (無 edit13 verdict 行) → 繼續輪詢" ;;
esac
exit 0
