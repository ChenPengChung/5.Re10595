#!/bin/bash
# ==============================================================================
# watch_local.sh — 本地監看 (取代 NCHC watcher/hill_watcher.sh 的「核心健康監控」)
# ------------------------------------------------------------------------------
# 不依賴 matplotlib/python:純讀 solver log + checkpoint + cfdq 狀態。
#   * solver 進度 ([CONV]/[VTK]/[Step]/MLUPS) 有沒有前進
#   * NaN/DIVERG/FATAL/MPI_Abort 警報
#   * checkpoint 有沒有持續長出 (斷鍊續跑的依據)
#   * cfdq job 狀態 + daemon 近期事件 (搶佔/續鏈)
# 用法:  bash watch_local.sh [間隔秒=30]
# ==============================================================================
cd "$(dirname "$(readlink -f "$0")")"
INT="${1:-30}"
R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[1m'; Z=$'\e[0m'
while :; do
  clear 2>/dev/null
  printf '%s==== cfdq 本地監看  %s  (每 %ss, Ctrl-C 停) ====%s\n' "$B" "$(date '+%F %T')" "$INT" "$Z"
  cfdq ls 2>/dev/null | sed -n '1,4p'
  LG=$(ls -t run_local_*.log 2>/dev/null | head -1)
  printf '\n%s--- solver (%s) ---%s\n' "$B" "${LG:-無}" "$Z"
  if [ -n "$LG" ]; then
    grep -hE '^\[CONV\]|^\[Step |MLUPS \(instant\)' "$LG" 2>/dev/null | tail -4
    n=$(grep -ciE 'nan|diverg|MPI_Abort|\[FATAL\]' "$LG" 2>/dev/null)
    if [ "${n:-0}" -gt 0 ]; then printf '%s  ⚠️⚠️ 偵測到 NaN/DIVERG/FATAL (%s 處)! 建議 cfdq rm 0001 檢查%s\n' "$R" "$n" "$Z"
    else printf '%s  ✓ 無 NaN/DIVERG%s\n' "$G" "$Z"; fi
  fi
  printf '\n%s--- checkpoint (最新3) ---%s\n' "$B" "$Z"
  ls -1dv restart/checkpoint/step_* 2>/dev/null | grep -v WRITING | tail -3
  printf '\n%s--- cfdq daemon 近期事件 ---%s\n' "$B" "$Z"
  tail -4 "$HOME/.cfdq/daemon.log" 2>/dev/null
  sleep "$INT"
done
