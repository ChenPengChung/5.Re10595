#!/bin/bash
# partition_ctl.sh — partition 管理 CLI (arch-aware: x86_64→H200, aarch64→GB200)
# 用法: ./run partition [list|set|reset|<name>]
#
# [H200 / x86_64] 本專案 jp 鎖定 32，partition 鎖定 32gpus。此 CLI 重設鎖定值:
#   改寫 jobscript_chain.slurm.H200 的 #SBATCH --partition / --time (= 直接 ./run 投遞 +
#   jobscript 自我續投 fallback 的權威預設), 並記錄 restart/h200_partition。
#   dispatcher 候選集同樣鎖在 32gpus@jp32，不做跨 H200 partition 切換。
# [GB200 / aarch64] 沿用既有 pin-file (restart/gb200_partition) 機制, 行為不變。

set -eo pipefail

_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || realpath "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
CHAIN_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
PROJECT_ROOT="$(cd "$CHAIN_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

if [ ! -f "$CHAIN_DIR/tools/partition_lib.sh" ]; then
    echo "[FATAL] $CHAIN_DIR/tools/partition_lib.sh 不存在" >&2
    exit 1
fi
. "$CHAIN_DIR/tools/partition_lib.sh"

# ════════════════════ H200 (x86_64) 分支: 暫時鎖定 partition ════════════════════
JS_H200="$CHAIN_DIR/jobscript_chain.slurm.H200"

h200_header_partition() {  # 讀 jobscript header 目前的 --partition (= 權威鎖定值)
    awk -F= '/^#SBATCH[[:space:]]+--partition=/{gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2; exit}' "$JS_H200" 2>/dev/null
}

h200_show_list() {
    local hdr jp; hdr="$(h200_header_partition)"
    jp="$(grep -E '^#define[[:space:]]+jp[[:space:]]+[0-9]+' "$PROJECT_ROOT/variables.h" 2>/dev/null | grep -oE '[0-9]+' | head -1)"
    echo "═══════════════════════════════════════════════════════════"
    echo " H200 Partition 狀態  (jp 鎖定 ${jp:-?} = $(( ${jp:-0} / 8 )) node × 8 GPU)"
    echo "═══════════════════════════════════════════════════════════"
    echo " 暫時鎖定 (jobscript header 預設): ${hdr:-?}  (walltime=$(h200_partition_walltime "${hdr:-x}"))"
    [ -f "$H200_PARTITION_FILE" ] && echo " restart/h200_partition 記錄: $(cat "$H200_PARTITION_FILE" 2>/dev/null)"
    echo ""
    printf " %-10s %-11s %5s  %s\n" "PARTITION" "WALLTIME" "CAP" ""
    printf " %-10s %-11s %5s  %s\n" "─────────" "──────────" "─────" ""
    local p wt cap mark
    for p in $(h200_known_partitions); do
        wt="$(h200_partition_walltime "$p")"; cap="$(h200_partition_cap "$p")"; mark=""
        [ "$p" = "$hdr" ] && mark="<-- locked"
        printf " %-10s %-11s %5s  %s\n" "$p" "$wt" "$cap" "$mark"
    done
    echo "═══════════════════════════════════════════════════════════"
    echo ""
    echo " 用法:"
    echo "   ./run partition 32gpus                  重設鎖定的 partition"
    echo "   ./run partition reset                   清除 restart/h200_partition 記錄 (header 不變)"
    echo " 注意: dispatcher 候選集同樣鎖在 32gpus@jp32; 此設定同步直接投遞/fallback。"
}

h200_set_partition() {
    local part="$1" wt jp cap
    if [ -z "$part" ]; then
        echo "用法: ./run partition <name>"
        echo "  可用: $(h200_known_partitions)"
        exit 1
    fi
    wt="$(h200_partition_walltime "$part")"
    if [ -z "$wt" ]; then
        echo "[FATAL] 不認識/不可用的 H200 partition: $part"
        echo "  可用: $(h200_known_partitions)  (舊 normal/4nodes/large 已 INACTIVE)"
        exit 1
    fi
    jp="$(grep -E '^#define[[:space:]]+jp[[:space:]]+[0-9]+' "$PROJECT_ROOT/variables.h" 2>/dev/null | grep -oE '[0-9]+' | head -1)"
    cap="$(h200_partition_cap "$part")"
    if [ -n "$jp" ] && [ "$jp" -gt "$cap" ] 2>/dev/null; then
        echo "[FATAL] jp=$jp > $part per-account GPU cap=$cap → 會 PENDING (MaxGRESPerAccount)。"
        echo "        改投 cap 更大的 partition, 或先 claude_changejp 降規模。"
        exit 1
    fi
    mkdir -p "$PROJECT_ROOT/restart"
    echo "$part" > "$H200_PARTITION_FILE"
    # 改寫 jobscript header (直接投遞 + 自我續投的權威預設); --nodes 由 jp 決定, 不在此處更動。
    sed -E -i "s|^(#SBATCH --partition=).*|\1${part}|; s|^(#SBATCH --time=).*|\1${wt}|" "$JS_H200"
    echo "已鎖定 H200 partition=$part  walltime=$wt  (jp=${jp:-?} 不變, $(( ${jp:-0} / 8 )) node)"
    echo "  jobscript header 已更新; restart/h200_partition 已記錄。"
    if [ -f "$PROJECT_ROOT/DISPATCHER_ACTIVE" ]; then
        echo "  注意: dispatcher 候選集同樣鎖在 32gpus@jp32。"
    fi
}

if [ "$(uname -m)" = "x86_64" ]; then
    case "${1:-list}" in
        list|ls|status|-h|--help|help) h200_show_list ;;
        set)                           h200_set_partition "${2:-}" ;;
        reset|clear)
            if [ -f "$H200_PARTITION_FILE" ]; then
                rm -f "$H200_PARTITION_FILE"
                echo "已清除 restart/h200_partition 記錄 (jobscript header 預設不變)"
            else
                echo "沒有 restart/h200_partition 記錄需要清除"
            fi ;;
        *)                             h200_set_partition "$1" ;;
    esac
    exit 0
fi
# ════════════════════ 以下為 GB200 (aarch64) 既有邏輯, 完全保留 ════════════════════

show_list() {
    local CURRENT
    CURRENT="$(gb200_active_partition)"
    echo "═══════════════════════════════════════════════════════════"
    echo " GB200 Partition 狀態"
    echo "═══════════════════════════════════════════════════════════"
    if [ -n "$CURRENT" ]; then
        echo " 目前指定: $CURRENT  (walltime=$(gb200_partition_walltime "$CURRENT"))"
    else
        echo " 目前指定: (未設定, 使用 jobscript 預設)"
    fi
    echo ""
    printf " %-15s %-11s %6s %6s %6s  %s\n" "PARTITION" "WALLTIME" "IDLE" "MIX" "DOWN" ""
    printf " %-15s %-11s %6s %6s %6s  %s\n" "─────────────" "──────────" "────" "────" "────" ""
    for p in $(gb200_known_partitions); do
        local wt idle mix down mark
        wt="$(gb200_partition_walltime "$p")"
        idle=$(sinfo -h -p "$p" -t idle -o '%D' 2>/dev/null | awk '{s+=$1} END{print s+0}')
        mix=$(sinfo -h -p "$p" -t mix -o '%D' 2>/dev/null | awk '{s+=$1} END{print s+0}')
        down=$(sinfo -h -p "$p" -t 'down,drain,fail' -o '%D' 2>/dev/null | awk '{s+=$1} END{print s+0}')
        mark=""
        [ "$p" = "$CURRENT" ] && mark="<-- active"
        printf " %-15s %-11s %6s %6s %6s  %s\n" "$p" "$wt" "$idle" "$mix" "$down" "$mark"
    done
    echo "═══════════════════════════════════════════════════════════"
    echo ""
    echo " 用法:"
    echo "   ./run partition set <name>   設定 partition"
    echo "   ./run partition <name>       設定 partition (簡寫)"
    echo "   ./run partition reset        回復 jobscript 預設"
}

case "${1:-list}" in
    list|ls|status)
        show_list
        ;;
    set)
        part="${2:-}"
        if [ -z "$part" ]; then
            echo "用法: ./run partition set <partition-name>"
            echo "  可用: $(gb200_known_partitions)"
            exit 1
        fi
        wt="$(gb200_partition_walltime "$part")"
        if [ -z "$wt" ]; then
            echo "[FATAL] 不認識的 partition: $part"
            echo "  可用: $(gb200_known_partitions)"
            exit 1
        fi
        mkdir -p restart
        echo "$part" > "$GB200_PARTITION_FILE"
        echo "已設定: partition=$part  walltime=$wt"
        echo "下次投遞/chain 續投時生效 (sbatch --partition=$part --time=$wt)"
        if [ -f DISPATCHER_ACTIVE ]; then
            echo "注意: dispatcher 運行中, 它會用 ETA-compare 自動選 partition, 此設定僅在 dispatcher 停止後生效"
        fi
        ;;
    reset|clear)
        if [ -f "$GB200_PARTITION_FILE" ]; then
            rm -f "$GB200_PARTITION_FILE"
            echo "已清除 partition override, 回復 jobscript 預設"
        else
            echo "沒有 partition override 需要清除"
        fi
        ;;
    -h|--help|help)
        show_list
        ;;
    *)
        wt="$(gb200_partition_walltime "$1")"
        if [ -n "$wt" ]; then
            mkdir -p restart
            echo "$1" > "$GB200_PARTITION_FILE"
            echo "已設定: partition=$1  walltime=$wt"
            echo "下次投遞/chain 續投時生效 (sbatch --partition=$1 --time=$wt)"
            if [ -f DISPATCHER_ACTIVE ]; then
                echo "注意: dispatcher 運行中, 它會用 ETA-compare 自動選 partition, 此設定僅在 dispatcher 停止後生效"
            fi
        else
            echo "[ERROR] 不認識的子命令或 partition: $1"
            echo ""
            show_list
            exit 1
        fi
        ;;
esac
