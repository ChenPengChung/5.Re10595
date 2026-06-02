#!/bin/bash
# ==============================================================================
# dispatcher_stop.sh   —  停止跨 partition 自動派工 daemon (方案 B)
# ==============================================================================
# 用法:
#   ./dispatcher_stop.sh             # 優雅停止 (建 STOP_DISPATCHER, 下一輪結束後收工)
#   ./dispatcher_stop.sh --kill-now  # 立刻 kill (SIGTERM 後 5s 再 SIGKILL)
#
# 注意: 停止 dispatcher 不會影響正在跑的 job. 若要停止本專案 chain 請另外:
#   ./run job-guard stop-chain
#   ./run job-guard scancel <jobid>   # 只允許取消本專案記錄的 job
# ==============================================================================

set -uo pipefail

# ── [方案 A path discipline] ──
_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || realpath "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
CHAIN_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
PROJECT_ROOT="$(cd "$CHAIN_DIR/.." && pwd)"
cd "$PROJECT_ROOT" || { echo "[dispatcher_stop] FATAL: cannot cd to $PROJECT_ROOT" >&2; exit 1; }

SENTINEL="DISPATCHER_ACTIVE"
STOP_SENTINEL="STOP_DISPATCHER"

KILL_NOW=0
for arg in "$@"; do
    case "$arg" in
        --kill-now) KILL_NOW=1 ;;
        -h|--help)
            sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "Unknown arg: $arg"; exit 2 ;;
    esac
done

# [restart-gap race fix] 明確停止 dispatcher → 移除持久 INTENT 標記, 讓 jobscript 回到 legacy
# 自我續投 (chain 仍續跑, 只是不再由 dispatcher 管理 partition/jp)。在所有停止路徑之前先移除,
# 確保即使 DISPATCHER_ACTIVE 已空 (daemon churn) 也能正確解除 dispatcher 模式。
rm -f restart/DISPATCHER_INTENT restart/dispatcher.heartbeat
echo "[dispatcher_stop] 已移除 DISPATCHER_INTENT (jobscript 回到自我續投模式)"

if [ ! -f "$SENTINEL" ]; then
    echo "[dispatcher_stop] 無 dispatcher 執行中 (沒有 $SENTINEL)"
    # 保險: 若有殘留 STOP_DISPATCHER 清掉
    rm -f "$STOP_SENTINEL"
    exit 0
fi

PID="$(cat "$SENTINEL" 2>/dev/null | tr -d '[:space:]')"
if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
    echo "[dispatcher_stop] Sentinel 存在但 PID ($PID) 已失效, 直接清理"
    rm -f "$SENTINEL" "$STOP_SENTINEL"
    exit 0
fi

if [ "$KILL_NOW" -eq 1 ]; then
    echo "[dispatcher_stop] 強制 kill PID=$PID ..."
    kill -TERM "$PID" 2>/dev/null || true
    for i in 1 2 3 4 5; do
        sleep 1
        if ! kill -0 "$PID" 2>/dev/null; then
            echo "[dispatcher_stop] ✓ 已停 (SIGTERM)"
            rm -f "$SENTINEL" "$STOP_SENTINEL"
            exit 0
        fi
    done
    kill -KILL "$PID" 2>/dev/null || true
    echo "[dispatcher_stop] ✓ 已強制 kill (SIGKILL)"
    rm -f "$SENTINEL" "$STOP_SENTINEL"
    exit 0
fi

# 優雅停止
touch "$STOP_SENTINEL"
echo "[dispatcher_stop] ✓ 已建 $STOP_SENTINEL"
echo "                  dispatcher (PID=$PID) 會在下次輪詢 (最多約 30 秒) 後 clean-exit"
echo "                  若要立刻停: ./dispatcher_stop.sh --kill-now"
