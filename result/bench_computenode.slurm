#!/bin/bash
#SBATCH --job-name=Edit11_benchplot
#SBATCH --account=MST115169
#SBATCH --partition=dev
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --gres=gpu:1
#SBATCH --mem=48G
#SBATCH --time=00:15:00
#SBATCH --output=bench_computenode_%j.log
#SBATCH --error=bench_computenode_%j.log
# ----------------------------------------------------------------------------
# Edit11 benchmark 比對圖 — 計算節點零誤差路線(float64,可選 canonical)
#   - 完整 float64(VTK 場本來就是 double),只留無損的「跳過未用場」patch
#   - 計算節點 RAM 數百 GB,沒有 login-node 20GB user.slice cgroup 牆
#   - 唯讀分析:只讀 VTK + 寫 fig_*.png,不碰流場/checkpoint/solver
# ⚠️ dev p_dev QOS 的 MinTRES = gres/gpu=1 → 必須 --gres=gpu:1(0-GPU 會 QOSMinGRES
#    永久 PENDING)。1 GPU 計入 dev per-account 上限(32),與主 job(64gpus)獨立。
#    dev 是 scavenger,仍可能 PENDING;非急需時 watcher 的 float32 圖(差 6e-8)已足夠。
# 提交建議:從專案根投(cd <root> && sbatch result/bench_computenode.slurm)→ WorkDir=根
#    → 可用 ./run job-guard scancel 取消(從 result/ 投會讓 hook 擋,需 ! scancel)。
# ----------------------------------------------------------------------------
set -uo pipefail
cd "${SLURM_SUBMIT_DIR}"
[ -f 2.Benchmark.py ] || cd result   # 從專案根投時進到 result/
echo "[bench] host=$(hostname) submit_dir=${SLURM_SUBMIT_DIR}"
echo "[bench] free -g:"; free -g
echo "[bench] start $(date '+%F %T')"

# getrusage 包裝量峰值 RSS(不依賴 /usr/bin/time,計算節點亦可能無)
python3 -c "
import resource, subprocess
rc = subprocess.call(['python3','2.Benchmark.py','--Re','5600','--no-ask-scales','--no-ask-density'])
peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
print(f'[bench] EXIT_RC={rc} PEAK_CHILD_RSS_KB={peak} (~{peak/1024/1024:.2f} GB, full float64)')
raise SystemExit(rc)
"
RC=$?

echo "[bench] EXIT_RC=${RC} (0=ok)"
echo "[bench] figs:"; ls -lt fig_*.png 2>/dev/null | head
echo "[bench] done $(date '+%F %T')"
