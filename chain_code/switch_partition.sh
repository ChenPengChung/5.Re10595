#!/bin/bash
# =============================================================================
# switch_partition.sh — Edit11_Krank5600 GPU jobscript partition 任意切換工具
# -----------------------------------------------------------------------------
# 背景: NCHC 取消了 federated "h200"/"gb200" partition,改為單一 hpc 叢集。
#       本工具讓你把 jobscript 的 #SBATCH --partition / --time 安全切換到
#       「帳號實際可用」的 partition,walltime 自動 cap 到該 partition 上限。
#
# 用法:
#   bash chain_code/switch_partition.sh list
#        列出所有「含 H200 GPU 的 partition」+ 帳號可否用 + walltime 上限 + idle GPU 節點數
#
#   bash chain_code/switch_partition.sh <partition>
#        切換到該 partition,walltime 自動設為該 partition 的上限(最長)
#
#   bash chain_code/switch_partition.sh <partition> <D-HH:MM:SS>
#        指定 walltime(超過 partition 上限會自動 cap)
#
# 安全: 切換前以 sbatch --test-only 做權威驗證(帳號授權 + partition 有效 +
#       8 節點 GPU 可排)。不可用 → 拒絕切換,不動 jobscript。
#       只改 #SBATCH --partition / --time 兩行;先備份 jobscript。
# =============================================================================
set -u
ROOT="/home/s8313697/5.Re10595/Edit11_Krank5600"
JS="$ROOT/chain_code/jobscript_chain.slurm.H200"
ACCT="${SWITCH_ACCT:-MST115169}"
NODES="${SWITCH_NODES:-8}"      # 本案 jp=64 → 8 節點 × 8 GPU
GPN="${SWITCH_GPN:-8}"          # gpus / node

[ -f "$JS" ] || { echo "FATAL: jobscript 不存在: $JS"; exit 1; }

to_sec() {  # D-HH:MM:SS / HH:MM:SS / infinite -> seconds
  local t="$1"
  case "$t" in infinite|INFINITE|UNLIMITED|"") echo 999999999; return;; esac
  local d=0 rest="$t"
  case "$t" in *-*) d=${t%%-*}; rest=${t#*-};; esac
  local a b c; IFS=: read -r a b c <<<"$rest"
  if   [ -n "${c:-}" ]; then echo $((10#${d}*86400 + 10#${a}*3600 + 10#${b}*60 + 10#${c}))
  elif [ -n "${b:-}" ]; then echo $((10#${d}*86400 + 10#${a}*3600 + 10#${b}*60))
  else echo $((10#${d}*86400 + 10#${a}*60)); fi
}

# 帳號授權(快速): 讀 partition 的 AllowAccounts。空/ALL=放行;否則需含本帳號。
acct_allowed() {  # $1=partition -> 0=allowed 1=denied
  local aa; aa=$(scontrol show partition "$1" 2>/dev/null | tr ' ' '\n' | grep '^AllowAccounts=' | cut -d= -f2)
  [ -z "$aa" ] || [ "$aa" = "ALL" ] && return 0
  printf '%s' "$aa" | tr ',' '\n' | grep -qiE "^${ACCT}$" && return 0 || return 1
}

# 權威驗證(慢): sbatch --test-only 用本案規模探測
probe_ok() {  # $1=partition $2=time -> echo result string; rc 0=ok
  sbatch --test-only --partition="$1" --account="$ACCT" \
    --nodes="$NODES" --ntasks-per-node="$GPN" --gres=gpu:"$GPN" --time="$2" \
    --exclude=25a-hgpn207 "$JS" 2>&1 | head -1
}

cmd="${1:-}"
[ -z "$cmd" ] && { sed -n '2,18p' "$0"; exit 0; }

# ---- list 模式 ----
if [ "$cmd" = "list" ]; then
  printf "%-10s %-9s %-12s %-9s %s\n" PARTITION ACCT-OK MAXWALL idleGPU note
  for P in $(sinfo -h -o "%P" | tr -d '*' | sort -u); do
    # 只列含 H200 GPU 的 partition
    sinfo -p "$P" -h -o "%G" 2>/dev/null | grep -qi "gpu:H200" || continue
    lim=$(sinfo -h -p "$P" -o "%l" | head -1)
    gn=$(sinfo -p "$P" -N -h -o "%t %G" 2>/dev/null | awk '/gpu:H200/ && $1=="idle"{n++} END{print n+0}')
    if acct_allowed "$P"; then ok="YES"; else ok="no(gov)"; fi
    printf "%-10s %-9s %-12s %-9s %s\n" "$P" "$ok" "$lim" "$gn" ""
  done
  echo ""
  echo "目前 jobscript: $(grep -E '^#SBATCH --(partition|time)=' "$JS" | tr '\n' ' ')"
  exit 0
fi

# ---- switch 模式 ----
P="$cmd"
sinfo -h -p "$P" -o "%P" >/dev/null 2>&1 || { echo "[switch] FATAL: partition '$P' 不存在"; exit 2; }
acct_allowed "$P" || { echo "[switch] FATAL: 帳號 $ACCT 無權使用 '$P'(AllowAccounts 限制,如 large/slinky 為 gov 專用)— 不切換"; exit 2; }

lim=$(sinfo -h -p "$P" -o "%l" | head -1)
WANT="${2:-$lim}"
[ "$WANT" = "infinite" ] && WANT="7-00:00:00"
ws=$(to_sec "$WANT"); ls=$(to_sec "$lim")
EFF="$WANT"; [ "$ws" -gt "$ls" ] && EFF="$lim"
[ "$EFF" = "infinite" ] && EFF="7-00:00:00"

# 權威驗證
res=$(probe_ok "$P" "$EFF")
case "$res" in
  *"Invalid account"*|*"Invalid partition"*|*"not available"*|*"invalid"*)
    echo "[switch] FATAL: sbatch --test-only 拒絕: $res"; echo "  不切換 jobscript。"; exit 3 ;;
esac

cp -f "$JS" "$JS.bak.$(date +%s)"
sed -i "s|^#SBATCH --partition=.*|#SBATCH --partition=$P|" "$JS"
sed -i "s|^#SBATCH --time=.*|#SBATCH --time=$EFF|" "$JS"
echo "[switch] OK: partition=$P  time=$EFF  (partition 上限=$lim)"
echo "  test-only: $res"
grep -nE "^#SBATCH --(partition|time)=" "$JS"
echo ""
echo "下一步: 直接續投  →  rm -f .run.lock; ./run --h200 --no-queue-check"
echo "  (--h200 只是選用 H200 jobscript 檔;實際 partition 由上面 #SBATCH 決定)"
