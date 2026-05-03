#!/usr/bin/env bash
# ==============================================================================
# live_render.sh — 即時渲染 result/ 最新 VTK 到 live/ 資料夾
# ==============================================================================
# 持續監控 result/ 中新產生的 velocity_merged_*.vtk，
# 對每個新檔呼叫 pvpython render_frame.py 生成流場圖片。
#
# 用法:
#   nohup bash live_render.sh > live/render.log 2>&1 &
#   bash live_render.sh --once          # 只渲染最新一張就結束
#   bash live_render.sh --poll 30       # 改用 30 秒輪詢 (預設 20)
#
# 產出 (每個 step):
#   live/frame_NNNNNN_cont.png          瞬時 u 連續色標
#   live/frame_NNNNNN_RD.png            瞬時 u Rainbow Desaturated
#   live/latest_cont.png → symlink      永遠指向最新
#   live/latest_RD.png   → symlink
# ==============================================================================

set -uo pipefail

_SELF="$(readlink -f "$0" 2>/dev/null || realpath "$0" 2>/dev/null || echo "$0")"
PROJECT_ROOT="$(cd "$(dirname "$_SELF")" && pwd)"
cd "$PROJECT_ROOT"

OUTDIR="live"
POLL_SEC=20
MODE_ONCE=0
PVPYTHON="${PVPYTHON:-pvpython}"

while [ $# -gt 0 ]; do
    case "$1" in
        --once)      MODE_ONCE=1 ;;
        --poll)      shift; POLL_SEC="${1:-20}" ;;
        --outdir)    shift; OUTDIR="${1:-live}" ;;
        *)           echo "Unknown arg: $1"; exit 2 ;;
    esac
    shift
done

mkdir -p "$OUTDIR"

_log() { printf '[%s] [live_render] %s\n' "$(date '+%F %T')" "$*"; }

LAST_RENDERED=""

render_vtk() {
    local vtk="$1"
    local base step
    base="$(basename "$vtk" .vtk)"
    step="$(echo "$base" | grep -oP '\d+$')"

    if [ -z "$step" ]; then
        _log "WARN: cannot parse step from $base"
        return 1
    fi

    local out_cont="$OUTDIR/frame_${step}_cont.png"
    if [ -f "$out_cont" ]; then
        _log "SKIP: $out_cont already exists"
        return 0
    fi

    local sz
    sz=$(stat -c %s "$vtk" 2>/dev/null || echo 0)
    if [ "$sz" -lt 1024 ]; then
        _log "SKIP: $vtk too small (${sz} bytes, likely truncated)"
        return 1
    fi

    _log "RENDER: step=$step  vtk=$(basename "$vtk")  ($(( sz / 1048576 )) MB)"
    local t0
    t0=$(date +%s)

    if "$PVPYTHON" animation/render_frame.py "$vtk" --outdir "$OUTDIR" --step "$step" --video-mode 2>&1; then
        local elapsed=$(( $(date +%s) - t0 ))
        _log "OK: step=$step rendered in ${elapsed}s"

        # Update latest symlinks
        if [ -f "$out_cont" ]; then
            ln -sf "frame_${step}_cont.png" "$OUTDIR/latest_cont.png"
        fi
        local out_rd="$OUTDIR/frame_${step}_RD.png"
        if [ -f "$out_rd" ]; then
            ln -sf "frame_${step}_RD.png" "$OUTDIR/latest_RD.png"
        fi

        LAST_RENDERED="$vtk"
        return 0
    else
        local rc=$?
        _log "FAIL: pvpython exit=$rc for step=$step"
        return $rc
    fi
}

find_latest_vtk() {
    local latest=""
    local latest_step=0
    for f in result/velocity_merged_*.vtk; do
        [ -f "$f" ] || continue
        local s
        s=$(echo "$(basename "$f")" | grep -oP '\d+(?=\.vtk$)')
        [ -z "$s" ] && continue
        if [ "$s" -gt "$latest_step" ]; then
            latest_step=$s
            latest="$f"
        fi
    done
    echo "$latest"
}

_log "Started (poll=${POLL_SEC}s, outdir=$OUTDIR, pvpython=$PVPYTHON)"
_log "Project: $PROJECT_ROOT"

if [ "$MODE_ONCE" -eq 1 ]; then
    vtk="$(find_latest_vtk)"
    if [ -z "$vtk" ]; then
        _log "No VTK files found in result/"
        exit 1
    fi
    render_vtk "$vtk"
    exit $?
fi

# Continuous mode
while true; do
    vtk="$(find_latest_vtk)"

    if [ -z "$vtk" ]; then
        _log "No VTK files yet, waiting..."
    elif [ "$vtk" != "$LAST_RENDERED" ]; then
        render_vtk "$vtk" || true
    fi

    sleep "$POLL_SEC"
done
