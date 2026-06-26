#!/usr/bin/env bash
# test_lbm_plot_benchmark.sh — 單元測試:lbm-plot-benchmark → dev job 提交路徑(Edit12)
# 只測提交路徑,不真算。核心:bench_computenode.slurm 必為 gpu>0 可排程(非 0-GPU QOSMinGRES 永久 PENDING)。
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"            # → result/
SLURM="bench_computenode.slurm"
PASS=0; FAIL=0
ok()  { printf '  [PASS] %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  [FAIL] %s\n' "$1"; FAIL=$((FAIL+1)); }
echo "=== lbm-plot-benchmark 提交路徑單元測試 (Edit12) ==="

[[ -f "$SLURM" ]] && ok "bench_computenode.slurm 存在" || bad "找不到 $SLURM"
bash -n "$SLURM" 2>/dev/null && ok "bash -n 語法正確" || bad "bash -n 語法錯誤"

# T3 (★核心): 行首 #SBATCH --gres=gpu:N 且 N>=1(避 QOSMinGRES 永久 PENDING)
gpu=$(grep -oE '^#SBATCH[[:space:]]+--gres=gpu:[0-9]+' "$SLURM" | grep -oE '[0-9]+$' | tail -1)
[[ -n "${gpu:-}" && "$gpu" -ge 1 ]] && ok "gres=gpu:$gpu (>=1 → 不被 QOSMinGRES 永久擋)" \
    || bad "gres=gpu:${gpu:-缺} → 0/缺會觸發 QOSMinGRES 永久 PENDING!"

grep -qE '^#SBATCH[[:space:]]+--partition=dev' "$SLURM" && ok "partition=dev" || bad "partition 非 dev"
grep -qE '^#SBATCH[[:space:]]+--account=[A-Za-z0-9]+' "$SLURM" && ok "account 已設定" || bad "缺 --account"

# T6 (★精度): 預設 float64 — 基礎 sys.argv 不含 --lowmem;--lowmem 只在 BENCH_LOWMEM=1 附加
if grep -q '2.Benchmark.py' "$SLURM"; then
    if grep -E 'sys\.argv *= *\[' "$SLURM" | grep -q -- '--lowmem'; then
        bad "基礎 sys.argv 含 --lowmem(預設會變 float32,非最精準)"
    elif grep -q 'BENCH_LOWMEM' "$SLURM"; then
        ok "預設 float64(base argv 無 --lowmem;--lowmem 受 BENCH_LOWMEM env 閘控,供 A/B)"
    else
        ok "預設 float64(base argv 無 --lowmem)"
    fi
else
    bad "腳本未呼叫 2.Benchmark.py"
fi

# T7: 有 cd 進 result/(Edit12: cd "$SLURM_SUBMIT_DIR/result")→ 從根投 WorkDir=根可 job-guard scancel
grep -qE 'cd[[:space:]].*result' "$SLURM" && ok "含 cd 進 result/(支援從專案根提交)" || bad "缺 cd 進 result/"

# T8: sbatch --test-only 接受(可解析、不被 submit-time 拒絕);不投真 job
if command -v sbatch >/dev/null 2>&1; then
    out=$(sbatch --test-only "$SLURM" 2>&1)
    echo "$out" | grep -qiE 'to start|Submitted' \
        && ok "sbatch --test-only 接受(可排程;真實 PENDING 可能 QOSMaxGRESPerUser 等 budget)" \
        || bad "sbatch --test-only 拒絕: $(echo "$out" | head -1)"
else
    echo "  [SKIP] 無 sbatch(非 HPC 登入節點)"
fi

echo "=== 結果: PASS=$PASS  FAIL=$FAIL ==="
[[ "$FAIL" -eq 0 ]] && { echo "✅ 全過"; exit 0; }
echo "❌ 有失敗項"; exit 1
