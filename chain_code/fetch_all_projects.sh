#!/bin/bash
# fetch_all_projects.sh — 任一專案 git push 後,讓 Edit11/Edit12/Edit13 三個 repo 都 git fetch origin。
#
# 目的(使用者規則 2026-06-29):任何一個專案推送遠端時,三方都要 fetch,保持各自對 origin/* 的視圖
#   同步(避免 race / non-FF / 看到過時遠端狀態)。
# 安全:git fetch 純唯讀,只更新 origin/* refs + objects,**不碰工作樹 / index / checkpoint / job**;
#   跨專案 fetch 受 cross-project isolation 允許(讀取類)。每個 fetch 有 timeout,單一失敗不影響其他。
# 用法:bash chain_code/fetch_all_projects.sh   (或 LOG=<file> 指定記錄檔;預設靜默)
LOG="${LOG:-/dev/null}"
TS(){ date '+%F %T'; }
PROJECTS=(
  /home/s8313697/5.Re10595/Edit11_Krank5600
  /home/s8313697/5.Re10595/Edit12_Krank56002
  /home/s8313697/5.Re10595/Edit13_2800ITBLBM
)
for d in "${PROJECTS[@]}"; do
  # 用 rev-parse 偵測有效 repo(同時涵蓋 worktree:其 .git 是檔案而非目錄)
  git -C "$d" rev-parse --git-dir >/dev/null 2>&1 || { echo "[$(TS)] [fetch_all] skip (非 repo): $d" >>"$LOG"; continue; }
  if timeout 60 git -C "$d" fetch -q origin 2>>"$LOG"; then
    echo "[$(TS)] [fetch_all] ✓ fetched $d" >>"$LOG"
  else
    echo "[$(TS)] [fetch_all] ✗ $d (timeout/err, 不影響其他專案)" >>"$LOG"
  fi
done
exit 0
