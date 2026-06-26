#!/bin/bash
#SBATCH --job-name=Edit12_bench
#SBATCH --account=MST115169
#SBATCH --partition=dev
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=4
#SBATCH --mem=48G
#SBATCH --time=00:15:00
#SBATCH --output=result/bench_computenode_%j.log
#SBATCH --open-mode=append
#
# ── benchmark 比對圖 float64 canonical (零誤差) ──
# 平均場 VTK 已達數十 GB。login node user.slice cgroup ~20GB 跨使用者共用 → float64
# 讀整顆會 OOM 且可能誤殺別專案程序。故 canonical(最精準)投 dev 計算節點(--mem=48G):
#   - 不帶 --lowmem → float64 (與 watcher float32 inline 雙軌分流)。
#   - ★--gres=gpu:1: NCHC H200 各 QOS 都有 gpu MinTRES(dev=1) → 0-GPU job 被 QOSMinGRES 永久
#     擋(實測 130642/130643 PENDING 永不跑)。dev QOS per-account cap=32, 用 1/32 不撞上限, 且與
#     主 job 的 p_32gpus QOS cap 分離 → 不影響主模擬。GPU 純為滿足 QOSMinGRES, 實際計算在 CPU。
#   - getrusage 量峰值 RSS (不依賴 NCHC login node 常缺的 /usr/bin/time)。
#   - 唯讀分析: 只讀最新 stat-stable VTK + 寫 result/fig_*.png; 不碰 solver/restart/checkpoint。
# Re 預設 5600 (本專案); 可 `sbatch --export=ALL,BENCH_RE=<N> result/bench_computenode.slurm` 覆寫。
set -uo pipefail
RE="${BENCH_RE:-5600}"
cd "$SLURM_SUBMIT_DIR/result" || exit 2
echo "[bench-job ${SLURM_JOB_ID:-?}] $(date '+%F %T') node=$(hostname) Re=$RE (float64 canonical, no --lowmem)"
python3 - "$RE" <<'PY'
import os, sys, resource, runpy
re_val = sys.argv[1]
sys.argv = ["2.Benchmark.py", "--Re", re_val, "--no-ask-scales", "--no-ask-density"]
# 預設 float64(canonical); BENCH_LOWMEM=1 → float32(供 A/B 交叉驗證, 與 watcher inline 軌同 dtype)
if os.environ.get("BENCH_LOWMEM") == "1":
    sys.argv.append("--lowmem")
    print("[bench-job] BENCH_LOWMEM=1 → float32 (A/B 用)")
rc = 0
try:
    runpy.run_path("2.Benchmark.py", run_name="__main__")
except SystemExit as e:
    rc = int(e.code) if isinstance(e.code, int) else (0 if e.code is None else 1)
except Exception as e:
    import traceback; traceback.print_exc(); rc = 1
finally:
    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(f"[bench-job] peak RSS = {peak_kb/1024.0/1024.0:.2f} GB (getrusage ru_maxrss)  rc={rc}")
sys.exit(rc)
PY
RC=$?
echo "[bench-job ${SLURM_JOB_ID:-?}] done rc=$RC $(date '+%F %T')"
ls -lt fig_*.png 2>/dev/null | head
exit $RC
