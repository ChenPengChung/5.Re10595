#!/usr/bin/env bash
# uniform100.sh — Capture next 100 consecutive VTKs, validate every frame, encode GIF
# Usage: bash animation/uniform100.sh [target_frames]
set -euo pipefail

cd "$(dirname "$0")/.." || exit 1
PROJECT_ROOT="$(pwd)"
ANIM_DIR="${PROJECT_ROOT}/animation"
FRAMES_DIR="${ANIM_DIR}/png_uniform_100"
LOG="${ANIM_DIR}/uniform100.log"
PVBATCH="/work/s8313697/software/ParaView-5.12.1-osmesa-MPI-Linux-Python3.10-x86_64/bin/pvbatch"
FFMPEG="/work/s8313697/software/ffmpeg-7.0.2-amd64-static/ffmpeg"
PIPELINE="${ANIM_DIR}/pipeline.py"

TARGET=${1:-100}
MIN_VTK_SIZE=$((4*1024*1024*1024))
MIN_PNG_SIZE=5000
MIN_PNG_STD=5.0
MAX_RETRIES=3

mkdir -p "$FRAMES_DIR"
: > "$LOG"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

validate_png() {
    local png="$1"
    if [ ! -f "$png" ]; then echo "MISSING"; return 1; fi
    local sz; sz=$(stat -c%s "$png" 2>/dev/null || echo 0)
    if [ "$sz" -lt "$MIN_PNG_SIZE" ]; then echo "TOO_SMALL(${sz}B)"; return 1; fi
    local std
    std=$(python3 -c "
from PIL import Image; import numpy as np
img = Image.open('${png}')
print('%.2f' % np.array(img).std())
" 2>/dev/null || echo "0.00")
    local ok; ok=$(python3 -c "print(1 if float('${std}') > ${MIN_PNG_STD} else 0)")
    if [ "$ok" = "1" ]; then echo "OK(std=${std})"; return 0
    else echo "BLANK(std=${std})"; return 1; fi
}

log "=== Uniform 100: capturing next ${TARGET} consecutive VTKs ==="

VALID=0
SEQ=0
LAST_RENDERED=""

while [ "$VALID" -lt "$TARGET" ]; do
    newest=$(ls -1t result/velocity_merged_*.vtk 2>/dev/null | head -1 || true)
    if [ -z "$newest" ]; then
        log "No VTK found, waiting... (${VALID}/${TARGET})"
        sleep 10; continue
    fi

    step=$(echo "$newest" | grep -oP '\d+(?=\.vtk)')
    if [ -z "$step" ]; then sleep 5; continue; fi

    if [ "$step" = "$LAST_RENDERED" ]; then
        sleep 5; continue
    fi

    sz=$(stat -c%s "$newest" 2>/dev/null || echo 0)
    if [ "$sz" -lt "$MIN_VTK_SIZE" ]; then
        sleep 3; continue
    fi

    sleep 5
    sz2=$(stat -c%s "$newest" 2>/dev/null || echo 0)
    if [ "$sz" != "$sz2" ]; then
        log "SKIP ${step}: still writing (${sz} -> ${sz2})"
        continue
    fi
    if [ ! -f "$newest" ]; then
        log "SKIP ${step}: disappeared"
        continue
    fi

    retry=0
    frame_ok=false
    while [ $retry -lt $MAX_RETRIES ] && ! $frame_ok; do
        log "RENDER step=${step} seq=${SEQ} (${VALID}/${TARGET}) attempt=$((retry+1))"

        if python3 "$PIPELINE" "$newest" "$step" --skip-encode --width 1920 --pvbatch "$PVBATCH" >> "$LOG" 2>&1; then
            src_cont="${FRAMES_DIR}/frame_${step}_cont.png"
            src_rd="${FRAMES_DIR}/frame_${step}_RD.png"
            dst_cont="${FRAMES_DIR}/seq_${SEQ}_cont.png"
            dst_rd="${FRAMES_DIR}/seq_${SEQ}_RD.png"

            # pipeline.py outputs to png_frames/, move to our dir
            orig_cont="${ANIM_DIR}/png_frames/frame_${step}_cont.png"
            orig_rd="${ANIM_DIR}/png_frames/frame_${step}_RD.png"

            all_valid=true
            for png in "$orig_cont" "$orig_rd"; do
                result=$(validate_png "$png")
                rc=$?
                if [ $rc -eq 0 ]; then
                    log "  VALID: $(basename "$png") ${result}"
                else
                    log "  REJECT: $(basename "$png") ${result}"
                    rm -f "$png"
                    all_valid=false
                fi
            done

            if $all_valid; then
                mv "$orig_cont" "$dst_cont"
                mv "$orig_rd" "$dst_rd"
                VALID=$((VALID + 1))
                SEQ=$((SEQ + 1))
                LAST_RENDERED="$step"
                frame_ok=true
                log "  ACCEPTED seq=$((SEQ-1)) step=${step} (${VALID}/${TARGET})"
            else
                retry=$((retry + 1))
                if [ $retry -lt $MAX_RETRIES ]; then
                    log "  RETRY in 5s..."
                    sleep 5
                fi
            fi
        else
            log "  RENDER FAILED step=${step}"
            retry=$((retry + 1))
            if [ $retry -lt $MAX_RETRIES ]; then
                log "  RETRY in 5s..."
                sleep 5
            fi
        fi
    done

    if ! $frame_ok; then
        log "  GIVE UP step=${step} after ${MAX_RETRIES} attempts, moving to next VTK"
        LAST_RENDERED="$step"
    fi
done

# === Phase 2: Encode GIF ===
TAG=$(date +%Y%m%d_%H%M%S)
log "=== Encoding GIF: 20fps, ${VALID} frames ==="

for suffix in cont RD; do
    # Rename to sequential for ffmpeg glob
    TMP_ENC="${ANIM_DIR}/enc_tmp_${suffix}"
    mkdir -p "$TMP_ENC"
    rm -f "$TMP_ENC"/*.png
    i=0
    for f in $(ls -1 "$FRAMES_DIR"/seq_*_${suffix}.png 2>/dev/null | sort -V); do
        ln -sf "$(realpath "$f")" "$TMP_ENC/$(printf '%04d' $i).png"
        i=$((i+1))
    done

    out="${ANIM_DIR}/uniform_${TAG}_${suffix}.gif"
    log "Encoding ${suffix}: ${i} frames -> $(basename "$out")"
    "$FFMPEG" -y -framerate 20 \
        -i "$TMP_ENC/%04d.png" \
        -vf "split[s0][s1];[s0]palettegen=max_colors=256:stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3" \
        "$out" >> "$LOG" 2>&1
    if [ $? -eq 0 ]; then
        log "  OK: $(ls -lh "$out" | awk '{print $5}')"
    else
        log "  ENCODE FAILED"
    fi
    rm -rf "$TMP_ENC"
done

log "=== DONE: ${VALID} valid frames, uniform Δ1000 steps ==="
log "GIF files:"
ls -lh "${ANIM_DIR}"/uniform_${TAG}_*.gif 2>/dev/null | while read l; do log "  $l"; done
