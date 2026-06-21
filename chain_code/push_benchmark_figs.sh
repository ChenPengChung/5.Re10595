#!/bin/bash
# push_benchmark_figs.sh — 自主 commit+push benchmark 比對圖(由 watcher 在 benchmark+tauwall
# 完成後呼叫)。目的:**免 Claude / 免 session** —— 即使 Claude 當機 / rate-limit / 關閉,
# benchmark 圖仍會被自動推到遠端(原本靠 Claude /loop 推,現改 watcher daemon 自主推)。
#
# ★安全設計(關鍵):用 git **plumbing** 在「origin/<branch> 之上」造 commit 再直接 push,
#   **完全不碰本機 dirty 工作樹 / 主 index**:
#     temp index <- read-tree origin  →  update-index(只塞 8 張圖的 blob)  →  write-tree
#     →  commit-tree -p origin  →  push commit:refs/heads/<branch>
#   watcher 每 30s 重生 monitor_convergence 等 → 工作樹永遠 dirty;一般 rebase/merge/autostash
#   會在這些 binary 上衝突並可能留衝突標記汙染工作樹。plumbing 路徑零碰觸,天然非破壞。
#   push 的 parent 永遠是「剛 fetch 的 origin」→ 對遠端永遠 fast-forward;origin 若在 race 中前進,
#   push 失敗 → 重新 fetch+重建+重試(最多 3 次);仍失敗 → 留待下個 VTK 重試,**絕不 --force**。
#
# 守門:只本專案、只這 8 張 benchmark 圖、不碰三大紀錄檔、不碰別專案;單例 flock 防並發。
# 用法:bash chain_code/push_benchmark_figs.sh [<step>]
set -uo pipefail
SELF="$(readlink -f "$0")"; ROOT="$(cd "$(dirname "$SELF")/.." && pwd)"; cd "$ROOT" || exit 0
LOG="live/push_benchmark_figs.log"; mkdir -p live; TS(){ date '+%F %T'; }
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" || { echo "[$(TS)] 無法解析 branch,跳過" >>"$LOG"; exit 0; }
{ [ -z "$BRANCH" ] || [ "$BRANCH" = "HEAD" ]; } && { echo "[$(TS)] detached HEAD, skip" >>"$LOG"; exit 0; }

RE=$(awk '$1=="#define"&&$2=="Re"{print $3;exit}' variables.h 2>/dev/null | tr -d '[:space:]'); RE=${RE:-5600}
FIGS=(result/fig_mean_u.png result/fig_mean_v.png result/fig_uu.png result/fig_vv.png result/fig_uv.png \
      result/fig_k.png "result/tau_wall_signed_Re${RE}_cf.png" "result/tau_wall_signed_Re${RE}_cp.png")
present=(); for f in "${FIGS[@]}"; do [ -f "$f" ] && present+=("$f"); done
[ ${#present[@]} -eq 0 ] && { echo "[$(TS)] 無 benchmark 圖檔存在,跳過" >>"$LOG"; exit 0; }

# 單例鎖(防 watcher 連續呼叫 / 多實例同時 push);live/ 已 gitignore
exec 9>"live/.push_figs.lock" 2>/dev/null || exit 0
flock -n 9 || { echo "[$(TS)] 另一 push 進行中,跳過" >>"$LOG"; exit 0; }

step="${1:-$(ls -t result/velocity_merged_*.vtk 2>/dev/null | head -1 | grep -oE '[0-9]+')}"
ftt=$(python3 -c "print(f'{${step:-0}/1579042:.0f}')" 2>/dev/null || echo '?')
msg=$(printf '更新 benchmark 比對圖 FTT-%s(step %s)\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>' "$ftt" "$step")

attempt=0
while [ "$attempt" -lt 3 ]; do
    attempt=$((attempt+1))
    git fetch -q origin "$BRANCH" 2>>"$LOG" || { echo "[$(TS)] fetch 失敗,放棄本輪(下個 VTK 重試)" >>"$LOG"; exit 0; }
    base="origin/$BRANCH"
    idx=$(mktemp "${TMPDIR:-/tmp}/pbf_idx.XXXXXX") || { echo "[$(TS)] mktemp 失敗,跳過本輪(不碰主 index)" >>"$LOG"; exit 0; }
    rm -f "$idx"
    if ! GIT_INDEX_FILE="$idx" git read-tree "$base" 2>>"$LOG"; then rm -f "$idx"; echo "[$(TS)] read-tree 失敗" >>"$LOG"; exit 0; fi
    changed=0
    for f in "${present[@]}"; do
        blob=$(git hash-object -w "$f" 2>>"$LOG") || continue
        [ "$blob" != "$(git rev-parse "$base:$f" 2>/dev/null || echo none)" ] && changed=1
        GIT_INDEX_FILE="$idx" git update-index --add --cacheinfo "100644,$blob,$f" 2>>"$LOG"
    done
    if [ "$changed" = 0 ]; then rm -f "$idx"; echo "[$(TS)] 圖與 origin 一致,無需 push" >>"$LOG"; exit 0; fi
    tree=$(GIT_INDEX_FILE="$idx" git write-tree 2>>"$LOG"); rm -f "$idx"
    [ -z "$tree" ] && { echo "[$(TS)] write-tree 失敗" >>"$LOG"; exit 0; }
    commit=$(printf '%s' "$msg" | git commit-tree "$tree" -p "$base" 2>>"$LOG")
    [ -z "$commit" ] && { echo "[$(TS)] commit-tree 失敗" >>"$LOG"; exit 0; }
    if timeout 120 git push -q origin "$commit:refs/heads/$BRANCH" 2>>"$LOG"; then
        # 本地 branch 對齊:僅當 FF(HEAD 是 $commit 祖先)才移 ref + 只刷這 8 張圖的 index 條目
        # (不 reset 全 index → 不干擾並行 stage;不 checkout → 不碰 dirty 工作樹)。
        old_head=$(git rev-parse HEAD 2>/dev/null || true)
        if [ -n "$old_head" ] && git merge-base --is-ancestor "$old_head" "$commit" 2>/dev/null; then
            # CAS 帶 expected old SHA:只在 branch ref 仍 == old_head 時才移動(原子;關 TOCTOU,
            #   期間若有人 commit 則 ref!=old → update-ref 失敗放棄,絕不倒退 / 不藏 local commit)。
            if git update-ref "refs/heads/$BRANCH" "$commit" "$old_head" 2>>"$LOG"; then
                git update-index --add -- "${present[@]}" 2>>"$LOG" || true
            fi
        fi
        echo "[$(TS)] ✅ pushed FTT-${ftt} (step ${step}) commit=${commit:0:8}" >>"$LOG"
        exit 0
    fi
    echo "[$(TS)] push attempt $attempt 失敗(origin 在 race 中前進?)→ 重新 fetch 重建" >>"$LOG"
done
echo "[$(TS)] push deferred(3 次競態失敗);圖留待下個 VTK benchmark 重試(不 --force)" >>"$LOG"
exit 0
