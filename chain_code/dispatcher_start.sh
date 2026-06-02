#!/bin/bash
# ==============================================================================
# dispatcher_start.sh   —  啟動跨 partition 自動派工 daemon (方案 B)
# ==============================================================================
# 用法:
#   ./dispatcher_start.sh                 # 背景啟動 (nohup + &)
#   ./dispatcher_start.sh --foreground    # 前景執行 (debug 用, Ctrl+C 可停)
#
# 先決條件:
#   1. a.out.GB200 或 a.out.H200 至少一個存在 (越多 partition 可選越好)
#      產生方式:
#        bash build_and_submit.sh.GB200 --build-only && cp a.out a.out.GB200
#        bash build_and_submit.sh.H200  --build-only && cp a.out a.out.H200
#
#   2. restart/ 目錄存在 (若無, submit_dispatcher.sh 會自動建立)
#
#   3. 若要啟動新 chain, 先 ./run.sh 投首輪 (cold start)
#      若要接手既有 chain, 直接啟動 dispatcher 即可 (它會等當前 job 結束)
#
# 停止:
#   ./dispatcher_stop.sh   (會建 STOP_DISPATCHER → daemon 收到後 clean-exit)
# ==============================================================================

set -uo pipefail

# ── [方案 A path discipline] ──
_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || realpath "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
CHAIN_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
PROJECT_ROOT="$(cd "$CHAIN_DIR/.." && pwd)"
cd "$PROJECT_ROOT" || { echo "[dispatcher_start] FATAL: cannot cd to $PROJECT_ROOT" >&2; exit 1; }

SENTINEL="DISPATCHER_ACTIVE"
DAEMON="$CHAIN_DIR/submit_dispatcher.sh"
LOG_FILE="restart/dispatcher.log"
PID_FILE="restart/dispatcher.pid"

FOREGROUND=0
for arg in "$@"; do
    case "$arg" in
        --foreground|-f) FOREGROUND=1 ;;
        -h|--help)
            sed -n '2,25p' "$0"; exit 0 ;;
        *)
            echo "Unknown arg: $arg"; exit 2 ;;
    esac
done

# ── 防重複啟動 ──
if [ -f "$SENTINEL" ]; then
    EXISTING_PID="$(cat "$SENTINEL" 2>/dev/null | tr -d '[:space:]')"
    if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
        echo "[dispatcher_start] ✗ dispatcher 已在執行 (PID=$EXISTING_PID)"
        echo "                   若要看狀態: ./dispatcher_status.sh"
        echo "                   若要停止:   ./dispatcher_stop.sh"
        exit 1
    else
        echo "[dispatcher_start] ⚠ 發現殘留 $SENTINEL 但 PID 不活躍, 清理並繼續"
        rm -f "$SENTINEL"
    fi
fi

# ── Binary 檢查 (早退省事) ──
if [ ! -s "a.out.GB200" ] && [ ! -s "a.out.H200" ]; then
    echo "[dispatcher_start] ✗ 兩個 arch 的 binary 都不存在:"
    echo "    a.out.GB200  (aarch64/sm_100, GB200 用)"
    echo "    a.out.H200   (x86_64/sm_90,   H200 用)"
    echo ""
    echo "    請先執行:"
    echo "      ./run build GB200 --build-only && cp a.out a.out.GB200"
    echo "      ./run build H200  --build-only && cp a.out a.out.H200"
    exit 3
fi

# ── Daemon 檢查 ──
if [ ! -f "$DAEMON" ]; then
    echo "[dispatcher_start] ✗ 找不到 $DAEMON"
    exit 4
fi
chmod +x "$DAEMON" 2>/dev/null || true

mkdir -p restart/

# 清掉舊 STOP_DISPATCHER (萬一上次沒清乾淨)
rm -f STOP_DISPATCHER

# [P0 TRAP #2 FIX] 若上次是因「叢集長時間無空位」停機, 要求使用者明確確認再啟動
# 避免 restart chain 但叢集還是滿的 → 立即又觸發同樣停機 → 無意義的反覆
if [ -f restart/STOP_NOCAPACITY ]; then
    echo "[dispatcher_start] ✗ 偵測到 restart/STOP_NOCAPACITY (上次因長時間無空位停機):"
    sed 's/^/                      /' restart/STOP_NOCAPACITY
    echo ""
    echo "    請先確認叢集現況有空位 (./run status 或 sinfo), 然後清除 sentinel 再啟動:"
    echo "      rm restart/STOP_NOCAPACITY && ./run dispatcher start"
    echo "    若要放寬容忍度 (預設 30 分鐘, 改成 1 小時):"
    echo "      rm restart/STOP_NOCAPACITY && NOCAPACITY_LIMIT=120 ./run dispatcher start"
    exit 6
fi

# [restart-gap race fix] 建立持久 INTENT 標記: 宣告本 chain 由 dispatcher 管理。
# jobscript 據此 + heartbeat 新鮮度決定「交棒 vs 自投」, 不再用瞬時 DISPATCHER_ACTIVE
# (daemon 重啟/crash 時該檔短暫消失, 會害 jobscript 誤判自投舊 jp → 與 jp 切換衝突)。
# 只有 dispatcher_stop 才移除 INTENT; daemon 重啟/crash 都保留。heartbeat 先給初值, daemon 每輪 touch。
touch restart/DISPATCHER_INTENT restart/dispatcher.heartbeat

# [一鍵全有] 開 dispatcher 即自動確保「本專案」watchdog crontab 存在 (layer 3 自動綁上)。
# 冪等: 已存在 → 不動; 缺 → 補上。只加本專案的行, 用 (crontab -l; echo) | crontab - 保留其他行(含別專案)。
if command -v crontab >/dev/null 2>&1; then
    _WD="$PROJECT_ROOT/chain_code/tools/daemon_keepalive.sh"
    if crontab -l 2>/dev/null | grep -qF "$_WD"; then
        echo "[dispatcher_start] watchdog crontab 已存在 (略過)"
    elif { crontab -l 2>/dev/null; echo "*/5 * * * * $_WD >/dev/null 2>&1"; } | crontab - 2>/dev/null; then
        echo "[dispatcher_start] ✓ 已自動裝 watchdog crontab (*/5min keep-alive) → $_WD"
    else
        echo "[dispatcher_start] ⚠ 無法寫 crontab; 請手動加: */5 * * * * $_WD >/dev/null 2>&1"
    fi
fi

if [ "$FOREGROUND" -eq 1 ]; then
    echo "[dispatcher_start] 前景模式啟動 (Ctrl+C 可停)"
    exec bash "$DAEMON"
else
    echo "[dispatcher_start] 背景啟動 dispatcher daemon..."
    nohup bash "$DAEMON" >> "$LOG_FILE" 2>&1 &
    DAEMON_PID=$!
    echo "$DAEMON_PID" > "$PID_FILE"
    sleep 2
    if kill -0 "$DAEMON_PID" 2>/dev/null; then
        echo "[dispatcher_start] ✓ dispatcher 已啟動, PID=$DAEMON_PID"
        echo "                   Log: $LOG_FILE"
        echo "                   狀態: ./dispatcher_status.sh"
        echo "                   停止: ./dispatcher_stop.sh"
    else
        echo "[dispatcher_start] ✗ daemon 啟動後立即死亡, 查 $LOG_FILE"
        exit 5
    fi
fi
