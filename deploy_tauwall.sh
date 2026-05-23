#!/usr/bin/env bash
# deploy_tauwall.sh — 一鍵移植 tau_wall benchmark 到 Edit4/Edit5
# 從 Edit6_5600DNS 複製 Python 腳本 + patch watcher
set -euo pipefail

SRC="/home/s8313697/5.Re10595/Edit6_5600DNS/result/10.tau_wall_benchmark.py"

for proj in Edit4_ChapmannForpart Edit5_Rebuild; do
    TARGET="/home/s8313697/5.Re10595/$proj"
    echo "=== $proj ==="

    # 1) 複製 Python 腳本
    cp -f "$SRC" "$TARGET/result/10.tau_wall_benchmark.py"
    echo "  [OK] result/10.tau_wall_benchmark.py"

    # 2) Patch watcher: 加入 TAUWALL_SCRIPT 定義
    WATCHER="$TARGET/watcher/hill_watcher.sh"
    if ! grep -q 'TAUWALL_SCRIPT' "$WATCHER"; then
        sed -i '/^BENCH_SCRIPT=/a TAUWALL_SCRIPT="$RESULT_DIR/10.tau_wall_benchmark.py"' "$WATCHER"
        echo "  [OK] added TAUWALL_SCRIPT variable"
    else
        echo "  [SKIP] TAUWALL_SCRIPT already defined"
    fi

    # 3) Patch watcher: 加入 run_tauwall() 函式 (在 run_benchmark 函式結束後)
    if ! grep -q 'run_tauwall' "$WATCHER"; then
        # 找到 startup log block 的位置，在其前面插入 run_tauwall 函式
        sed -i '/^log "=========================================="/i\
run_tauwall() {\
    local step="$1" capture rc\
    local before_marker="$LIVE_DIR/.tauwall.marker.$$"\
    : > "$before_marker"\
\
    capture=$(cd "$RESULT_DIR" \\&\\& timeout "$BENCH_TIMEOUT" python3 "$TAUWALL_SCRIPT" \\\
        --Re "$RE" --auto 2>\\&1)\
    rc=$?\
\
    if (( rc == 124 )); then\
        log "TAUWALL step=$step  TIMEOUT after ${BENCH_TIMEOUT}s"; rm -f "$before_marker"; return 1\
    fi\
    if (( rc != 0 )); then\
        log "TAUWALL step=$step  FAILED rc=$rc :: $(printf '"'"'%s'"'"' \\"$capture\\" | tail -c 300 | tr '"'"'\\n'"'"' '"'"' '"'"')"\
        rm -f "$before_marker"; return 1\
    fi\
\
    local src copied=""\
    for pat in "tau_wall_signed_Re${RE}_cf.png" "tau_wall_signed_Re${RE}_cp.png"; do\
        src="$RESULT_DIR/$pat"\
        if [[ -f "$src" ]] \\&\\& [[ "$src" -nt "$before_marker" ]]; then\
            cp -f "$src" "$LIVE_DIR/$pat"; copied="$copied $pat"\
        fi\
    done\
    rm -f "$before_marker"\
\
    log "TAUWALL step=$step  Re=$RE  outputs:${copied:- (none)}"\
    return 0\
}\
' "$WATCHER"
        echo "  [OK] added run_tauwall() function"
    else
        echo "  [SKIP] run_tauwall already exists"
    fi

    # 4) Patch watcher: 加入 tauwall startup log
    if ! grep -q 'tauwall' "$WATCHER"; then
        sed -i '/log "  bench    /a\log "  tauwall  = $TAUWALL_SCRIPT"' "$WATCHER"
        echo "  [OK] added tauwall startup log"
    fi

    # 5) Patch watcher: 在 run_benchmark 後加入 run_tauwall 呼叫
    if ! grep -q 'run_tauwall "$step"' "$WATCHER"; then
        sed -i '/run_benchmark "$step" || true/a\                    run_tauwall "$step" || true' "$WATCHER"
        echo "  [OK] added run_tauwall call in G2 gate"
    else
        echo "  [SKIP] run_tauwall call already present"
    fi

    echo "  Done: $proj"
    echo
done

echo "=== 部署完成 ==="
echo "Edit4: Re=5600, Edit5: Re=10595"
echo "所有 watcher 已 patch，下次啟動時自動啟用 tau_wall 輸出"
