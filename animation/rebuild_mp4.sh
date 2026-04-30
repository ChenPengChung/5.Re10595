#!/usr/bin/env bash
# ==============================================================================
# animation/rebuild_mp4.sh — 手動從 png_frames/ 重建兩個 MP4
# ==============================================================================
# 用途:
#   * ffmpeg 曾失敗 (arch mismatch, 斷電, 人為中斷) → PNG 還在, MP4 壞掉時重建
#   * 改了 codec/fps/pix-fmt 想套用到整條影片
#   * 從別的 chain 的 png_frames/ 合併重建
#
# 用法 (在 project root 或任意位置都行):
#   bash animation/rebuild_mp4.sh                  # 預設 33fps, libx264, yuv444p
#   bash animation/rebuild_mp4.sh --fps 60         # 改 fps
#   bash animation/rebuild_mp4.sh --codec ffv1     # 改用 ffv1 archive codec
#   bash animation/rebuild_mp4.sh --pix-fmt yuv420p # QuickTime 相容模式
# ==============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

FPS=33
CODEC="libx264"
PIX_FMT="yuv444p"

while [ $# -gt 0 ]; do
    case "$1" in
        --fps)     FPS="$2"; shift 2 ;;
        --codec)   CODEC="$2"; shift 2 ;;
        --pix-fmt) PIX_FMT="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

FRAMES_DIR="$SCRIPT_DIR/png_frames"
ENCODE_PY="$SCRIPT_DIR/video_encode_mp4.py"

if [ ! -d "$FRAMES_DIR" ]; then
    echo "[rebuild] ✗ $FRAMES_DIR 不存在, 無 PNG 可重建"
    exit 3
fi

n_cont=$(ls "$FRAMES_DIR"/frame_*_cont.png 2>/dev/null | wc -l)
n_rd=$(ls "$FRAMES_DIR"/frame_*_RD.png 2>/dev/null | wc -l)
echo "[rebuild] png_frames 現有: $n_cont cont + $n_rd RD"
echo "[rebuild] 設定: fps=$FPS codec=$CODEC pix_fmt=$PIX_FMT"

if [ "$n_cont" -eq 0 ] && [ "$n_rd" -eq 0 ]; then
    echo "[rebuild] ✗ 沒有任何 PNG 可 encode, 離開"
    exit 4
fi

echo ""
echo "[rebuild] === encode flow_cont.mp4 ==="
if [ "$n_cont" -gt 0 ]; then
    python3 "$ENCODE_PY" \
        --out     "$SCRIPT_DIR/flow_cont.mp4" \
        --pattern "$FRAMES_DIR/frame_*_cont.png" \
        --fps "$FPS" --codec "$CODEC" --pix-fmt "$PIX_FMT"
else
    echo "[rebuild] skip cont (無 frame_*_cont.png)"
fi

echo ""
echo "[rebuild] === encode flow_RD.mp4 ==="
if [ "$n_rd" -gt 0 ]; then
    python3 "$ENCODE_PY" \
        --out     "$SCRIPT_DIR/flow_RD.mp4" \
        --pattern "$FRAMES_DIR/frame_*_RD.png" \
        --fps "$FPS" --codec "$CODEC" --pix-fmt "$PIX_FMT"
else
    echo "[rebuild] skip RD (無 frame_*_RD.png)"
fi

echo ""
echo "[rebuild] 完成. 產出:"
ls -la "$SCRIPT_DIR"/flow_*.mp4 2>/dev/null
