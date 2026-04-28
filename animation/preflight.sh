#!/usr/bin/env bash
# preflight.sh — 編譯 / 跑 pipeline 前的環境檢查
# 涵蓋 4 個已知踩雷點：pvbatch / Pillow / CRLF / 舊殘留
# 額外：Python 語法 / 磁碟空間 / 檔案完整性
#
# Usage:
#   bash animation/preflight.sh          # 完整檢查 + 自動修 CRLF
#   bash animation/preflight.sh --dry    # 只檢查不動任何檔
#   bash animation/preflight.sh --fix    # 有問題的自動修（CRLF / 殘留）

set -u
cd "$(dirname "$0")/.."   # 確保 cwd 是專案根

DRY=0
FIX=0
for a in "$@"; do
    case "$a" in
        --dry) DRY=1 ;;
        --fix) FIX=1 ;;
        -h|--help) sed -n '1,15p' "$0"; exit 0 ;;
    esac
done

if [[ -t 1 ]]; then
    G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; CY=$'\e[36m'; B=$'\e[1m'; Z=$'\e[0m'
else
    G= R= Y= CY= B= Z=
fi

pass() { printf '  %s[PASS]%s %s\n' "$G" "$Z" "$*"; }
fail() { printf '  %s[FAIL]%s %s\n' "$R" "$Z" "$*"; }
warn() { printf '  %s[WARN]%s %s\n' "$Y" "$Z" "$*"; }
hdr()  { printf '\n%s== %s ==%s\n' "$B" "$1" "$Z"; }

N_FAIL=0

# ═══════════════════════════════════════════════════════════════════════════
# [1] pvbatch 可執行
# ═══════════════════════════════════════════════════════════════════════════
hdr "1. pvbatch"
PVBATCH_BIN=""
if [[ -n "${PVBATCH:-}" && -x "$PVBATCH" ]]; then
    PVBATCH_BIN="$PVBATCH"
    pass "PVBATCH env var: $PVBATCH_BIN"
elif command -v pvbatch &>/dev/null; then
    PVBATCH_BIN=$(command -v pvbatch)
    pass "pvbatch in PATH: $PVBATCH_BIN"
else
    for p in \
        /opt/paraview/bin/pvbatch \
        /usr/local/bin/pvbatch \
        /usr/bin/pvbatch \
        /opt/ParaView-5.13/bin/pvbatch \
        "/c/Program Files/ParaView 5.13.3/bin/pvbatch.exe"; do
        if [[ -x "$p" ]]; then PVBATCH_BIN="$p"; break; fi
    done
    if [[ -n "$PVBATCH_BIN" ]]; then
        warn "pvbatch not in PATH, found at: $PVBATCH_BIN"
        warn "Fix: export PVBATCH='$PVBATCH_BIN'  (加到 ~/.bashrc 或 sbatch script)"
    else
        fail "pvbatch not found anywhere"
        fail "Fix 選項："
        fail "  (a) module load paraview/5.13  (HPC cluster)"
        fail "  (b) export PVBATCH=/path/to/pvbatch"
        fail "  (c) 安裝 ParaView: https://www.paraview.org/download/"
        N_FAIL=$((N_FAIL+1))
    fi
fi

# 測試 pvbatch 能 launch
if [[ -n "$PVBATCH_BIN" ]]; then
    PV_VERSION=$("$PVBATCH_BIN" --version 2>&1 | head -1 || echo "N/A")
    if [[ "$PV_VERSION" == *paraview* || "$PV_VERSION" == *ParaView* ]]; then
        pass "pvbatch 啟動測試: $PV_VERSION"
    else
        warn "pvbatch 啟動輸出異常: $PV_VERSION"
    fi
fi

# ═══════════════════════════════════════════════════════════════════════════
# [2] Python + Pillow
# ═══════════════════════════════════════════════════════════════════════════
hdr "2. Python + Pillow"
if command -v python3 &>/dev/null; then
    PY_VERSION=$(python3 --version 2>&1)
    pass "python3: $PY_VERSION"
else
    fail "python3 not found"
    N_FAIL=$((N_FAIL+1))
fi

if python3 -c "from PIL import Image; print(Image.__version__)" &>/dev/null; then
    PIL_VERSION=$(python3 -c "from PIL import Image; print(Image.__version__)")
    pass "Pillow installed: $PIL_VERSION"
else
    fail "Pillow (PIL) not installed"
    fail "Fix 選項："
    fail "  (a) pip install --user Pillow"
    fail "  (b) module load python/3.x-sci  (HPC 通常預裝)"
    fail "  (c) conda install pillow  (若用 Anaconda)"
    N_FAIL=$((N_FAIL+1))
fi

# ═══════════════════════════════════════════════════════════════════════════
# [3] CRLF 檢查 / 修復
# ═══════════════════════════════════════════════════════════════════════════
hdr "3. CRLF line endings"
CRLF_FILES=()
# 收集候選檔（nullglob 避免無配對時出錯）
shopt -s nullglob
CANDIDATES=(animation/*.py animation/*.h animation/*.sh main.cu *.h)
shopt -u nullglob
for f in "${CANDIDATES[@]}"; do
    [[ -f "$f" ]] || continue
    # 用 python 偵測較可靠
    if python3 -c "import sys; sys.exit(0 if b'\r' in open(sys.argv[1],'rb').read() else 1)" "$f" 2>/dev/null; then
        CRLF_FILES+=("$f")
    fi
done

if [[ ${#CRLF_FILES[@]} -eq 0 ]]; then
    pass "所有檔案都是 LF（無 CRLF）"
else
    warn "發現 ${#CRLF_FILES[@]} 個檔案含 CRLF："
    for f in "${CRLF_FILES[@]}"; do echo "      $f"; done
    if [[ "$FIX" -eq 1 || "$DRY" -eq 0 ]]; then
        warn "自動修復中..."
        for f in "${CRLF_FILES[@]}"; do
            python3 -c "
import sys
p = sys.argv[1]
with open(p, 'rb') as fh: data = fh.read()
data = data.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
with open(p, 'wb') as fh: fh.write(data)
" "$f"
            echo "      fixed: $f"
        done
        pass "CRLF → LF 修復完成"
    else
        warn "Fix: bash $0 --fix   (或手動) sed -i 's/\\r\$//' <file>"
    fi
fi

# ═══════════════════════════════════════════════════════════════════════════
# [4] 背景任務 / 訊號處理
# ═══════════════════════════════════════════════════════════════════════════
hdr "4. MPI_Finalize vs 背景 pipeline 任務"
if grep -q "sleep 2" animation/mp4_snapshot.h 2>/dev/null; then
    pass "mp4_snapshot.h 含 sleep 2（AnimFinalize 等背景任務 2 秒）"
else
    warn "AnimFinalize 無等待機制 — 若 MP4 最後幀遺漏，這是主因"
fi
if grep -q "AnimFinalize" main.cu 2>/dev/null; then
    pass "main.cu 有呼叫 AnimFinalize"
else
    fail "main.cu 未呼叫 AnimFinalize — 模擬結束時背景任務會被 MPI_Finalize kill"
    N_FAIL=$((N_FAIL+1))
fi

# ═══════════════════════════════════════════════════════════════════════════
# [5] 必要檔案完整性
# ═══════════════════════════════════════════════════════════════════════════
hdr "5. Pipeline 檔案"
MISSING=()
for f in \
    animation/pipeline.py \
    animation/video_encode_mp4.py \
    animation/render_frame.py \
    animation/mp4_snapshot.h \
    main.cu; do
    if [[ ! -f "$f" ]]; then
        MISSING+=("$f")
        fail "缺 $f"
    fi
done
[[ ${#MISSING[@]} -eq 0 ]] && pass "5 個必要檔全部存在"
[[ ${#MISSING[@]} -gt 0 ]] && N_FAIL=$((N_FAIL+1))

# Python 語法檢查（pipeline 三個 script）
for f in animation/pipeline.py animation/video_encode_mp4.py animation/render_frame.py; do
    [[ -f "$f" ]] || continue
    if python3 -c "import ast; ast.parse(open('$f', encoding='utf-8').read())" 2>/dev/null; then
        pass "Syntax OK: $f"
    else
        fail "Syntax ERROR in $f — 下面是錯誤："
        python3 -c "import ast; ast.parse(open('$f', encoding='utf-8').read())" 2>&1 | sed 's/^/      /'
        N_FAIL=$((N_FAIL+1))
    fi
done

# ═══════════════════════════════════════════════════════════════════════════
# [6] 舊殘留檔
# ═══════════════════════════════════════════════════════════════════════════
hdr "6. 舊系統殘留"
STALE=()
for f in animation/build_gif.py animation/test_gif_system.py \
         animation/gif_snapshot.h animation/video_append.py \
         animation/flow_cont.gif animation/flow_RD.gif \
         animation/flow_cont.gif.state.json animation/flow_RD.gif.state.json \
         animation/flow_animation.mp4 animation/Umean_animation.mp4; do
    [[ -f "$f" ]] && STALE+=("$f")
done
if [[ ${#STALE[@]} -eq 0 ]]; then
    pass "無舊系統殘留"
else
    warn "發現 ${#STALE[@]} 個舊系統檔："
    for f in "${STALE[@]}"; do echo "      $f"; done
    if [[ "$FIX" -eq 1 ]]; then
        for f in "${STALE[@]}"; do rm -f "$f"; echo "      removed: $f"; done
        pass "舊殘留已清"
    else
        warn "Fix: bash $0 --fix  刪除舊殘留"
    fi
fi

# ═══════════════════════════════════════════════════════════════════════════
# [7] 磁碟空間（估算 10000 幀 × 4K PNG + 2 lossless MP4 ≈ 50-100 GB）
# ═══════════════════════════════════════════════════════════════════════════
hdr "7. 磁碟空間"
AVAIL_KB=$(df -Pk . 2>/dev/null | awk 'NR==2 {print $4}')
if [[ -n "$AVAIL_KB" ]]; then
    AVAIL_GB=$((AVAIL_KB / 1024 / 1024))
    if [[ "$AVAIL_GB" -gt 100 ]]; then
        pass "可用空間: ${AVAIL_GB} GB (充足，10000 幀 + 2 lossless MP4 預估需 ~50-100 GB)"
    elif [[ "$AVAIL_GB" -gt 5 ]]; then
        warn "可用空間: ${AVAIL_GB} GB (夠用一陣子；若 > 1000 幀可能吃緊)"
    else
        fail "可用空間僅 ${AVAIL_GB} GB — 太少！"
        fail "Fix: 降 ANIM_MAX_FRAMES、降 ANIM_WIDTH、或清其他檔"
        N_FAIL=$((N_FAIL+1))
    fi
fi

# ═══════════════════════════════════════════════════════════════════════════
# [8] ANIM_ENABLE 開關確認
# ═══════════════════════════════════════════════════════════════════════════
hdr "8. main.cu 設定"
ANIM_EN=$(grep -E "^#define ANIM_ENABLE" main.cu 2>/dev/null | awk '{print $3}')
ANIM_EVERY=$(grep -E "^#define ANIM_EVERY_N_VTK" main.cu 2>/dev/null | awk '{print $3}')
ANIM_FPS_V=$(grep -E "^#define ANIM_FPS" main.cu 2>/dev/null | awk '{print $3}')
ANIM_WIDTH_V=$(grep -E "^#define ANIM_WIDTH" main.cu 2>/dev/null | awk '{print $3}')

if [[ "$ANIM_EN" == "1" ]]; then
    pass "ANIM_ENABLE = 1 (啟用)"
else
    warn "ANIM_ENABLE = ${ANIM_EN:-?} — 若要跑 pipeline 需改 1"
fi
[[ -n "$ANIM_EVERY"   ]] && pass "ANIM_EVERY_N_VTK = $ANIM_EVERY"
[[ -n "$ANIM_FPS_V"   ]] && pass "ANIM_FPS = $ANIM_FPS_V"
[[ -n "$ANIM_WIDTH_V" ]] && pass "ANIM_WIDTH = $ANIM_WIDTH_V"

# ═══════════════════════════════════════════════════════════════════════════
# 總結
# ═══════════════════════════════════════════════════════════════════════════
echo ""
if [[ "$N_FAIL" -eq 0 ]]; then
    printf '%s%s======================================================%s\n' "$G" "$B" "$Z"
    printf '%s  PRE-FLIGHT PASSED — 可以編譯 + 跑 pipeline 了   %s\n' "$G" "$Z"
    printf '%s%s======================================================%s\n' "$G" "$B" "$Z"
    echo ""
    echo "下一步建議："
    echo "  1. bash build_and_submit.sh --build-only      # 編譯 main.cu"
    echo "  2. python3 animation/test_pipeline.py --fast  # 2 VTK smoke test"
    echo "  3. bash build_and_submit.sh                   # 正式 submit"
    exit 0
else
    printf '%s%s======================================================%s\n' "$R" "$B" "$Z"
    printf '%s  PRE-FLIGHT FAILED (%d blockers) — 先修再編譯         %s\n' "$R" "$N_FAIL" "$Z"
    printf '%s%s======================================================%s\n' "$R" "$B" "$Z"
    echo ""
    echo "常用 Fix 指令："
    echo "  bash $0 --fix            # 自動修 CRLF + 清舊殘留"
    echo "  module load paraview     # 補 pvbatch（HPC）"
    echo "  pip install --user Pillow # 補 Pillow"
    exit 1
fi
