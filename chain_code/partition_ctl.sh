#!/bin/bash
# partition_ctl.sh — partition 管理 CLI (依登入節點架構自動選 H200/GB200)
# 用法: ./run partition [list|set <name>|reset|<name>]
#   x86_64 登入節點 → 操作 H200 partition (normal/4nodes/dev), 寫 restart/h200_partition
#   aarch64 登入節點 → 操作 GB200 partition, 寫 restart/gb200_partition
# 此 pin 在「直接 ./run 投遞」時生效; dispatcher 運行中會用 2-tier/ETA 自動選, pin 僅在 dispatcher 停止後生效。

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

# ── 依架構選叢集 ──
case "$(uname -m)" in
    aarch64) CL="GB200"; PFX="gb200"; PFILE="$GB200_PARTITION_FILE" ;;
    *)       CL="H200";  PFX="h200";  PFILE="$H200_PARTITION_FILE"  ;;
esac
wt_of()  { "${PFX}_partition_walltime" "$1"; }
known()  { "${PFX}_known_partitions"; }
active() { "${PFX}_active_partition"; }

show_list() {
    local CURRENT; CURRENT="$(active)"
    echo "═══════════════════════════════════════════════════════════"
    echo " $CL Partition 狀態  (登入節點 $(uname -m))"
    echo "═══════════════════════════════════════════════════════════"
    if [ -n "$CURRENT" ]; then
        echo " 目前 pin: $CURRENT  (walltime=$(wt_of "$CURRENT"))"
    else
        echo " 目前 pin: (未設定, 由 dispatcher 自動選 / jobscript 預設)"
    fi
    echo ""
    printf " %-12s %-12s %6s %6s %6s  %s\n" "PARTITION" "WALLTIME" "IDLE" "MIX" "DOWN" ""
    printf " %-12s %-12s %6s %6s %6s  %s\n" "──────────" "──────────" "────" "────" "────" ""
    for p in $(known); do
        local wt idle mix down mark
        wt="$(wt_of "$p")"
        idle=$(sinfo -h -p "$p" -t idle -o '%D' 2>/dev/null | awk '{s+=$1} END{print s+0}')
        mix=$(sinfo -h -p "$p" -t mix -o '%D' 2>/dev/null | awk '{s+=$1} END{print s+0}')
        down=$(sinfo -h -p "$p" -t 'down,drain,fail' -o '%D' 2>/dev/null | awk '{s+=$1} END{print s+0}')
        mark=""; [ "$p" = "$CURRENT" ] && mark="<-- pin"
        printf " %-12s %-12s %6s %6s %6s  %s\n" "$p" "$wt" "$idle" "$mix" "$down" "$mark"
    done
    echo "═══════════════════════════════════════════════════════════"
    echo "  注意: $CL 每帳號 GPU 上限 — normal/4nodes=32, dev 無上限 (見 partition_lib)。"
    echo "  用法: ./run partition set <name> | ./run partition <name> | ./run partition reset"
}

_set_pin() {
    local part="$1" wt
    wt="$(wt_of "$part")"
    if [ -z "$wt" ]; then
        echo "[FATAL] $CL 不認識的 partition: $part"; echo "  可用: $(known)"; exit 1
    fi
    mkdir -p restart
    echo "$part" > "$PFILE"
    echo "已設定 $CL pin: partition=$part  walltime=$wt  ($PFILE)"
    echo "直接 ./run 投遞時生效 (sbatch --partition=$part --time=$wt)。"
    if [ -f DISPATCHER_ACTIVE ]; then
        echo "注意: dispatcher 運行中會用 2-tier/ETA 自動選 partition; 此 pin 僅在 dispatcher 停止後的直接投遞生效。"
    fi
}

case "${1:-list}" in
    list|ls|status) show_list ;;
    set)
        [ -n "${2:-}" ] || { echo "用法: ./run partition set <name>"; echo "  可用: $(known)"; exit 1; }
        _set_pin "$2" ;;
    reset|clear)
        if [ -f "$PFILE" ]; then rm -f "$PFILE"; echo "已清除 $CL partition pin, 回復自動/預設"; else echo "沒有 $CL partition pin 需要清除"; fi ;;
    -h|--help|help) show_list ;;
    *) _set_pin "$1" ;;
esac
