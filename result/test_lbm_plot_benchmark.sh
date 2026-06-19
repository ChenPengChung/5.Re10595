#!/usr/bin/env bash
# test_lbm_plot_benchmark.sh — 單元測試:lbm-plot-benchmark → dev job 提交路徑
# ============================================================================
# 只「測試提交路徑」,不真的算 benchmark(不投真 job、不跑 2.Benchmark.py)。
# 核心保證:手動 canonical 圖走的 result/bench_computenode.slurm 必須是「gpu>0」的
# 可排程 job,**不會像 0-GPU 那樣被 QOSMinGRES 永久 PENDING**。
#
# 背景(2026-06-19 實測):NCHC dev p_dev QOS 的 MinTRES=gres/gpu=1。
#   --gres=gpu:0 → QOSMinGRES 永久 PENDING(永遠不跑)。
#   --gres=gpu:1 → 可排程;128-GPU/人 滿載時暫排 QOSMaxGRESPerUser(等 budget,終會跑)。
# 用法: bash result/test_lbm_plot_benchmark.sh   (exit 0 = 全過)
# ============================================================================
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"            # → result/
SLURM="bench_computenode.slurm"
PASS=0; FAIL=0
ok()   { printf '  [PASS] %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  [FAIL] %s\n' "$1"; FAIL=$((FAIL+1)); }

echo "=== lbm-plot-benchmark 提交路徑單元測試 ==="

# T1: slurm 腳本存在
[[ -f "$SLURM" ]] && ok "bench_computenode.slurm 存在" || bad "找不到 $SLURM"

# T2: bash 語法正確
if bash -n "$SLURM" 2>/dev/null; then ok "bash -n 語法正確"; else bad "bash -n 語法錯誤"; fi

# T3 (★核心): --gres=gpu:N 且 N>=1(gpu>0,避開 QOSMinGRES 永久 PENDING)
# 硬化(Codex 建議): 只取「行首」有效 #SBATCH directive(排除被註解/縮排的偽指令);
# 多個時取最後一個(Slurm 對重複選項取最後值)。
gpu=$(grep -oE '^#SBATCH[[:space:]]+--gres=gpu:[0-9]+' "$SLURM" | grep -oE '[0-9]+$' | tail -1)
if [[ -n "${gpu:-}" && "$gpu" -ge 1 ]]; then
    ok "gres=gpu:$gpu (>=1 → 不會被 QOSMinGRES 永久擋)"
else
    bad "gres=gpu:${gpu:-缺} —— 0 或缺會觸發 QOSMinGRES 永久 PENDING!"
fi

# T4: partition=dev
grep -qE '#SBATCH[[:space:]]+--partition=dev' "$SLURM" && ok "partition=dev" || bad "partition 非 dev"

# T5: account 有設
grep -qE '#SBATCH[[:space:]]+--account=[A-Za-z0-9]+' "$SLURM" && ok "account 已設定" || bad "缺 --account"

# T6 (★精度): 跑 2.Benchmark.py 且「不帶 --lowmem」(= float64 高精度 canonical)
# 硬化(Codex 建議): 查「整支腳本任何位置」皆無 --lowmem(canonical 永不降精度),
# 不只查與 2.Benchmark.py 同一行(防參數被拆到續行漏判)。
if grep -q '2.Benchmark.py' "$SLURM"; then
    if grep -q -- '--lowmem' "$SLURM"; then
        bad "canonical job 出現 --lowmem(任何位置;會變 float32,非最精準)"
    else
        ok "呼叫 2.Benchmark.py 且全腳本無 --lowmem(float64 零誤差 canonical)"
    fi
else
    bad "腳本未呼叫 2.Benchmark.py"
fi

# T7: 從專案根投也能進到 result/(WorkDir=根 → 可 job-guard scancel)
grep -q 'cd result' "$SLURM" && ok "含 'cd result' fallback(支援從專案根提交)" \
    || bad "缺從根提交的 cd result fallback"

# T8: sbatch --test-only 接受此腳本(可解析、不被 submit-time 拒絕);不投真 job
if command -v sbatch >/dev/null 2>&1; then
    out=$(sbatch --test-only "$SLURM" 2>&1)
    if echo "$out" | grep -qiE 'to start|Submitted'; then
        ok "sbatch --test-only 接受(可排程;真實 PENDING 可能因 QOSMaxGRESPerUser 等 budget)"
    else
        bad "sbatch --test-only 拒絕: $(echo "$out" | head -1)"
    fi
else
    echo "  [SKIP] 無 sbatch(非 HPC 登入節點)"
fi

echo "=== 結果: PASS=$PASS  FAIL=$FAIL ==="
[[ "$FAIL" -eq 0 ]] && { echo "✅ 全過:lbm-plot-benchmark 提交路徑正確(gpu>0 可排程、float64 canonical)"; exit 0; }
echo "❌ 有失敗項,見上"; exit 1
