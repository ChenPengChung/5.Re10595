#!/bin/bash
# partition_ctl.sh — GB200 partition 管理 CLI
# 用法: ./run partition [list|set|reset|<name>]

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
