#!/usr/bin/env bash
#SBATCH --job-name=A2bench
#SBATCH --account=MST115169
#SBATCH --partition=16gpus
#SBATCH --nodes=4
#SBATCH --ntasks-per-node=8
#SBATCH --gres=gpu:8
#SBATCH --cpus-per-task=1
#SBATCH --time=01:00:00
#SBATCH --output=slurm_a2bench_%j.log
#SBATCH --error=slurm_a2bench_%j.err
# ════════════════════════════════════════════════════════════════════════════
#  Algorithm2 factorial benchmark — COORDS(gilbm_b) vs WEIGHTS(gilbm_a) [vs algo1]
#
#  testing-1 核心: 「預存 r,s + kernel 算權重」 vs 「預存 weight + kernel 讀」誰快。
#  per-class cache 後兩模式消費結構已對齊 (7×2×8), 唯一差別 = cache 填法 → 此量測
#  乾淨隔離 compute-fill vs read-fill (+ 表記憶體 1.27MiB vs 8.5MiB) 的 runtime 差。
#
#  方法: 同一 seed checkpoint、同網格(已備不重生)、同 32 GPU, 依序跑各 cell;
#        每 cell 跑 BENCH_SEC 秒 (延遲 touch STOP_CHAIN 優雅停), 取後段穩態 timing
#        block (跳 warm-load + warmup)。Iter_ms/Interior_ms 由 cudaEvent 量 (對 IO 穩健)。
#  安全: 無 chain 自投、無 dispatcher、不碰 Edit6_5600DNS live 目錄、各 cell 從同一 SEED
#        warm-load (顯式 --restart=SEED, 不受前一 cell 寫的 checkpoint 影響)。
#  用法 (使用者讓卡位後):  cd <worktree> && sbatch tests/algo2_benchmark.slurm ["cells"] [blocks]
#    cells   預設 "gilbm_b gilbm_a"  (testing-1 核心; 加 algo1 多一份 ~7min warm-load 當 baseline)
#    blocks  預設 5  (每 cell 在 warm-load 後再累積幾個 1000-step 計時 block 才停; 取後段穩態)
#  ★ 關鍵: warm-load ~7min, 故 NOT 固定 sleep (會在 load 期間就 touch STOP_CHAIN → 0 timing)。
#    改為輪詢 slurm log 的計時 block 數, 跨過 warm-load + 累積 blocks 後才 touch STOP_CHAIN。
# ════════════════════════════════════════════════════════════════════════════
set -eo pipefail
cd "$SLURM_SUBMIT_DIR"

if [[ "$(pwd -P)" == */Edit6_5600DNS ]]; then
    echo "[FATAL] refusing to benchmark in the live job directory" >&2
    exit 1
fi

CELLS="${1:-gilbm_b gilbm_a}"
TARGET_BLOCKS="${2:-5}"
HARDLIMIT="${3:-1200}"          # 每 cell 硬上限秒 (warm-load ~7min + blocks; 防卡死)
LOG="slurm_a2bench_${SLURM_JOB_ID}.log"

export HPCX_HOME=/opt/nvidia/hpc_sdk/Linux_x86_64/25.9/comm_libs/13.0/hpcx/hpcx-2.24
set +u; source "$HPCX_HOME/hpcx-init.sh"; hpcx_load    # 不重啟 set -u (poll 邏輯變數多, 避免誤觸 unbound)

# ★ 相對路徑 (solver 對非絕對 restart 路徑加 ./ 前綴; 絕對路徑會 .//home/... 開不了 metadata.dat)
SEED="restart/checkpoint/$(readlink restart/checkpoint/latest)"
[ -d "$SEED" ] || { echo "[FATAL] no seed checkpoint at $SEED" >&2; exit 1; }
echo "[bench] SEED=$SEED  np=$SLURM_NTASKS  cells='$CELLS'  blocks=$TARGET_BLOCKS  hardlimit=${HARDLIMIT}s"

for CELL in $CELLS; do
    BIN="./a.out.$CELL"
    if [ ! -x "$BIN" ]; then echo "[bench] SKIP $CELL (no $BIN — build_cell.sh $CELL first)"; continue; fi
    rm -f restart/STOP_CHAIN
    echo ""
    echo "===== BENCH CELL=$CELL ====="
    MARK=$(grep -c 'MLUPS (instant)' "$LOG" 2>/dev/null || true); MARK=${MARK:-0}
    set +e
    mpirun -np "$SLURM_NTASKS" --map-by ppr:8:node --bind-to none \
           stdbuf -oL "$BIN" --restart="$SEED" &
    MPID=$!
    # 輪詢: 等本 cell 累積 TARGET_BLOCKS 個計時 block (跨過 ~7min warm-load), 才 touch STOP_CHAIN
    T0=$SECONDS
    while :; do
        sleep 15
        NOW=$(grep -c 'MLUPS (instant)' "$LOG" 2>/dev/null || true); NOW=${NOW:-0}
        GOT=$(( NOW - MARK ))
        if [ "$GOT" -ge "$TARGET_BLOCKS" ]; then echo "[bench] $CELL 累積 $GOT 個 timing block → 停"; break; fi
        if ! kill -0 "$MPID" 2>/dev/null; then echo "[bench] $CELL mpirun 提早結束 (got $GOT blocks)"; break; fi
        if [ $(( SECONDS - T0 )) -ge "$HARDLIMIT" ]; then echo "[bench] $CELL 達 HARDLIMIT ${HARDLIMIT}s (got $GOT blocks)"; break; fi
    done
    touch restart/STOP_CHAIN          # solver ~100 步內偵測 → 優雅停機 + 寫 final checkpoint
    wait "$MPID"
    RC=$?
    set -e
    echo "===== END CELL=$CELL (RC=$RC) ====="
done

echo ""
echo "[bench] ===== STEADY-STATE SUMMARY (mean of last 3 timing blocks/cell, warmup skipped) ====="
awk '
  /===== BENCH CELL=/ { c=$3; sub(/CELL=/,"",c); order[++no]=c; seen[c]=1; n[c]=0; next }
  /MLUPS \(instant\)/ {
      iter=""; mg="";
      if (match($0,/Iter_ms=[0-9.]+/))      { iter=substr($0,RSTART+8,RLENGTH-8) }
      if (match($0,/[0-9.]+[ ]*\/GPU/))     { mg=substr($0,RSTART,RLENGTH); gsub(/[ ]*\/GPU/,"",mg) }
      if (c!="" && iter!="") { n[c]++; ITER[c,n[c]]=iter+0; MG[c,n[c]]=mg+0 }
      next
  }
  /Interior \(S1\)/ {
      if (match($0,/[0-9.]+[ ]*ms/)) { v=substr($0,RSTART,RLENGTH); gsub(/[ ]*ms/,"",v); if(c!=""&&n[c]>0) INT[c,n[c]]=v+0 }
      next
  }
  END {
      printf "  %-10s %10s %12s %12s %8s\n","cell","Iter_ms","Interior_ms","MLUPS/GPU","blocks";
      base_iter=0;
      for (o=1;o<=no;o++) {
          c=order[o]; cnt=n[c];
          if (cnt==0) { printf "  %-10s %10s\n",c,"(no blocks)"; continue }
          lo=cnt-2; if(lo<1)lo=1; k=0; si=0; ss=0; sm=0;
          for (b=lo;b<=cnt;b++){ si+=ITER[c,b]; ss+=INT[c,b]; sm+=MG[c,b]; k++ }
          ai=si/k; as=ss/k; am=sm/k;
          rel="";
          if (c=="algo1") base_iter=ai;
          else if (base_iter>0) rel=sprintf("  (%.2fx vs algo1)", ai/base_iter);
          printf "  %-10s %10.3f %12.3f %12.1f %8d%s\n", c, ai, as, am, cnt, rel;
      }
      if (("gilbm_b" in seen) && ("gilbm_a" in seen)) {
          cb=n["gilbm_b"]; ca=n["gilbm_a"];
          if (cb>0 && ca>0) {
              lb=cb-2; if(lb<1)lb=1; la=ca-2; if(la<1)la=1;
              ib=0;kb=0; for(b=lb;b<=cb;b++){ib+=ITER["gilbm_b",b];kb++}
              ia=0;ka=0; for(b=la;b<=ca;b++){ia+=ITER["gilbm_a",b];ka++}
              printf "\n  >>> testing-1: COORDS(gilbm_b)=%.3f ms  vs  WEIGHTS(gilbm_a)=%.3f ms  ->  WEIGHTS %s by %.2f%%\n",
                     ib/kb, ia/ka, (ia/ka < ib/kb ? "FASTER":"SLOWER"), 100.0*( (ib/kb)-(ia/ka) )/(ib/kb);
          }
      }
  }
' "$LOG"
echo "[bench] (raw per-block timing in $LOG between CELL markers; timing_log.dat also appended)"
