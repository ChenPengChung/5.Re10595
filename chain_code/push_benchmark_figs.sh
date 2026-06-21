#!/bin/bash
# push_benchmark_figs.sh — benchmark 比對圖 / 收斂圖「更新即個別推送」(比照 Edit11)
# ----------------------------------------------------------------------------
# 由 loop 日常巡檢每輪呼叫: 偵測 result/ 的 benchmark 圖有 git 變更就個別 commit+push,
# 讓使用者在他端透過 git 同步看到最新圖(watcher 每新 VTK ~1FTT 會刷新這些圖)。
# 兩類分開推(比照 Edit11):
#   ① benchmark 比對圖(fig_*×6 + tau_wall cf/cp) → "更新 benchmark 比對圖 FTT-N(step M)"
#   ② 收斂圖(monitor_convergence)              → "更新 Re5600 監控收斂圖"
# 守門: 逐檔 --only(禁 -A、隔離 index,不夾帶三大紀錄檔/其他); 非 fast-forward 被拒 → 回報
#       不 --force; FTT/step 從最新 slurm log 取。手動測試: bash chain_code/push_benchmark_figs.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1   # 專案根
JID=$(cat restart/chain_jobid 2>/dev/null || echo "")
LOG="slurm_${JID}.log"
FTT=$(grep -E '\[Step ' "$LOG" 2>/dev/null | tail -1 | grep -oE 'FTT=[0-9.]+' | head -1 | cut -d= -f2)
STEP=$(grep -E '\[Step ' "$LOG" 2>/dev/null | tail -1 | grep -oE 'Step [0-9]+' | head -1 | grep -oE '[0-9]+')
FTTr=$(printf "%.0f" "${FTT:-0}" 2>/dev/null || echo "?")
CO="Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
pushed=()

# 回傳「有 git 變更(vs HEAD)且存在」的檔案清單
_changed() { local out="" f; for f in "$@"; do
    [ -f "$f" ] && ! git diff --quiet HEAD -- "$f" 2>/dev/null && out="$out $f"
  done; echo "$out"; }

# 個別 commit + push 一組圖(隔離 index: --only 只提交這些路徑)
_commit_push() {  # $1=訊息  其餘=檔案
    local msg="$1"; shift
    git add -- "$@" 2>/dev/null || return 1
    git commit -q --only -m "$msg

$CO" -- "$@" 2>/dev/null || return 1
    if timeout 90 git push -q 2>/dev/null; then return 0
    else echo "★「$msg」已本地 commit 但 push 失敗(非ff/逾時/無SSH? 不 --force, 待下次補推)"; return 2; fi
}

# ① benchmark 比對圖
BENCH=$(_changed result/fig_mean_u.png result/fig_mean_v.png result/fig_uu.png \
                 result/fig_vv.png result/fig_uv.png result/fig_k.png \
                 result/tau_wall_signed_Re5600_cf.png result/tau_wall_signed_Re5600_cp.png)
if [ -n "$BENCH" ]; then
    _commit_push "更新 benchmark 比對圖 FTT-${FTTr}(step ${STEP:-?})" $BENCH && pushed+=("benchmark 比對圖 FTT-${FTTr}")
fi

# ② 收斂圖
CONV=$(_changed result/monitor_convergence_Re5600.png result/monitor_convergence_Re5600.pdf)
if [ -n "$CONV" ]; then
    _commit_push "更新 Re5600 監控收斂圖" $CONV && pushed+=("監控收斂圖")
fi

if [ ${#pushed[@]} -eq 0 ]; then echo "benchmark/收斂圖無更新, 未推送"; else echo "已推送: ${pushed[*]}"; fi
