#!/bin/bash
# ============================================================================
# chain_code_guard.sh — Edit12_Krank56002 chain_code 完整性守門 + watchdog 啟動器
# ----------------------------------------------------------------------------
# 此檔刻意放在 PROJECT_ROOT (chain_code/ 之外), 故 chain_code/ 整個被刪時它仍存活,
# 能在 systemd watchdog timer 每次 tick 把缺失的 chain_code/ tracked 檔從 git HEAD
# 自動補回, 再執行真正的 health_watchdog.sh。
#
# 動機 (2026-07-01 事故): edit12-watchdog.service 的 ExecStart 原本直接指向
# chain_code/health_watchdog.sh —— 一旦 chain_code/ 被誤刪 (非 git 的檔案系統 rm),
# 連守護者自己都一起消失 → watchdog 默默啞掉、續投鏈失去 jobscript。本啟動器消除此單點失效。
#
# 防護 (對應鑑識預防 1 + 3):
#   (1) bootstrap: ExecStart 改指向「chain_code 之外」的本檔, 缺 chain_code 先還原再跑 watchdog。
#   (3) 完整性巡檢: 每 tick 用 `git ls-files --deleted` 偵測 chain_code/ 缺失的 tracked 檔,
#       只 surgically 還原「缺的那些」(絕不覆寫任何 work-in-progress 已修改/未追蹤檔)。
# 安全: 只還原本專案 chain_code/; 只補「deleted」狀態檔, 不碰 modified/untracked;
#       絕不冷啟、不 scancel、不碰別專案; git 不可用時安靜跳過, 仍嘗試跑 watchdog。
# 手動測試: bash chain_code_guard.sh   (等同一次 watchdog timer tick)
# ============================================================================
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" 2>/dev/null || exit 0
export PATH="/usr/bin:/bin:/usr/local/bin:${PATH:-}"

WD="chain_code/health_watchdog.sh"
LLOG="live/health_watchdog.log"            # 永遠在 chain_code 之外, 隨時可寫
ALERTS="chain_code/health_watchdog_alerts.log"   # tracked → 復原後可離線推送
mkdir -p live 2>/dev/null || true
_ts(){ date '+%F %T'; }

# ── 完整性巡檢: 偵測 chain_code/ 內被刪的 tracked 檔 → 只還原「缺的那些」 ──
if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
    _deleted="$(git ls-files --deleted -- chain_code/ 2>/dev/null)"
    if [ -n "$_deleted" ]; then
        _n="$(printf '%s\n' "$_deleted" | grep -c .)"
        echo "[$(_ts)] [GUARD] ALERT: chain_code/ 偵測到 $_n 個 tracked 檔遺失 → 從 git HEAD 自動還原(只補缺檔, 不覆寫已修改檔)" >> "$LLOG"
        # 只還原 deleted 的那些 tracked 檔 (NUL 安全; git restore 不可用時退回 git checkout)
        # timeout 30 防 NFS .git 卡住整個 watchdog tick (本環境有 NFS 偶發停滯前科)
        git ls-files --deleted -z -- chain_code/ 2>/dev/null \
            | xargs -0 -r timeout 30 git restore --source=HEAD --worktree -- 2>/dev/null \
            || git ls-files --deleted -z -- chain_code/ 2>/dev/null \
            | xargs -0 -r timeout 30 git checkout HEAD -- 2>/dev/null || true
        _left="$(git ls-files --deleted -- chain_code/ 2>/dev/null | grep -c .)"
        echo "[$(_ts)] [GUARD] 還原完成: 原缺 $_n, 還原後仍缺 $_left" >> "$LLOG"
        # 復原後把告警補進 tracked 檔, 供 watcher 離線推送 (提醒 Claude 查根因)
        if [ -f "$ALERTS" ]; then
            echo "[$(_ts)] [GUARD] chain_code/ 自動還原 $_n 檔 (事故類: 2026-07-01 chain_code 檔案系統 rm)。仍缺 $_left。需 Claude 查根因。" >> "$ALERTS" 2>/dev/null || true
        fi
    fi
fi

# ── 跑真正的 health_watchdog (現已盡力確保存在) ──
if [ -f "$WD" ]; then
    exec /bin/bash "$WD"
else
    echo "[$(_ts)] [GUARD] FATAL: $WD 仍不存在且無法還原 (git 不可用或 chain_code 非 tracked)。本輪 watchdog 跳過, 不讓 timer 報 failed。" >> "$LLOG"
    exit 0
fi
