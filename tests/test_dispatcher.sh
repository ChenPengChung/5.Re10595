#!/usr/bin/env bash
# ==============================================================================
# test_dispatcher.sh — cfdq「續鏈 dispatcher」契約單元測試
# ------------------------------------------------------------------------------
# 驗證的核心契約 (來自 solver stop_control.h + wrapper + cfdq):
#
#   【停鏈 STOP】(chain 終止, 不再 resume) — 只有這些:
#     converged          → a.out exit 0
#     NaN / Diverged     → a.out exit 0  (STOP_DIVERGED)
#     FTT_STOP / loop    → a.out exit 0
#     pkill a.out=SIGTERM / SIGUSR2 / STOP_CHAIN → a.out exit 0
#     不可避免錯誤        → a.out exit 42
#
#   【續鏈 RESUME】(從 checkpoint 換節點續, 絕不斷鍊) — 其他全部:
#     SIGUSR1 優雅被搶    → a.out exit 124
#     crash (segfault…)  → a.out exit 1-9
#     kill -9 a.out      → 無 exit 檔 + pid 消失
#     節點失聯 > TTL      → 無法 probe
#   且 RESUME 時必須【清掉節點殘留 orphan】(Bug B), 否則自己的孤兒佔住節點 → 斷鍊。
#
# 作法: 用 `CFDQ_LIB=1 source cfdq` 只載函數, mock 掉 probe_one/now/cleanup_orphans,
#       直接驅動 reconcile/finish_job, 斷言 job 狀態轉移。不需真 GPU/SSH。
# 用法:  bash tests/test_dispatcher.sh        (exit 0 = 全通過)
# ==============================================================================
export CFDQ_HOME="$(mktemp -d)"
export CFDQ_LOST_TTL=180
export CFDQ_LIB=1
source "${CFDQ_BIN:-$HOME/bin/cfdq}"   # 只載函數 (CFDQ_LIB 守衛)
set +u

PASS=0; FAIL=0
ck(){ # desc expected actual
  if [ "$2" = "$3" ]; then printf '  \e[32m✓\e[0m %-46s = %s\n' "$1" "$3"; PASS=$((PASS+1))
  else printf '  \e[31m✗ %-46s 期望 %s, 得到 %s\e[0m\n' "$1" "$2" "$3"; FAIL=$((FAIL+1)); fi
}

# ---- mock 覆寫 (source 之後定義 → 蓋掉 cfdq 內的同名函數) ----
MOCK_PROBE=""; probe_one(){ printf '%s' "$MOCK_PROBE"; }   # 忽略參數, 回 mock
NOWVAL="";     now(){ [ -n "$NOWVAL" ] && echo "$NOWVAL" || date +%s; }
CLEANUP_CALLED=0; cleanup_orphans(){ CLEANUP_CALLED=1; }   # 記錄是否被呼叫, 不真 ssh

reset(){ rm -rf "$CFDQ_HOME/jobs"; mkdir -p "$CFDQ_HOME/jobs"; CLEANUP_CALLED=0; NOWVAL=""; MOCK_PROBE=""; }
mkrun(){ # id chain [lastalive]
  local id="$1" chain="$2" la="${3:-$(date +%s)}" d="$CFDQ_HOME/jobs/$1"
  mkdir -p "$d"
  printf 'cwd=%s\ncmd=bash x\nnp=8\nmodel=V100\nmode=exclusive\nchain=%s\nname=t%s\nnodes=\ncreated=0\n' "$CFDQ_HOME" "$chain" "$id" > "$d/spec"
  printf 'node=FakeNode\npid=999999\nstartid=111\nstarted=0\nlastalive=%s\nlaunchepoch=0\n' "$la" > "$d/run"
  printf running > "$d/status"
}
S(){ cat "$CFDQ_HOME/jobs/$1/status" 2>/dev/null; }   # 取狀態
PROBE_UP_ALIVE=$'H|FakeNode|56|0.1|b\nM|V100|8\nL|999999|alive\nEND|FakeNode|ok'
PROBE_UP_DEAD=$'H|FakeNode|56|0.1|b\nM|V100|8\nL|999999|dead\nEND|FakeNode|ok'
PROBE_DOWN="DOWN"   # 無 END → unreachable

echo "═══ A. 停鏈 STOP (合法終止, 不續) ═══"
reset; mkrun 01 1; echo 0  > "$CFDQ_HOME/jobs/01/exit"; reconcile; ck "converged (a.out exit0)"        done   "$(S 01)"
reset; mkrun 02 1; echo 0  > "$CFDQ_HOME/jobs/02/exit"; reconcile; ck "NaN/Diverged (exit0)"           done   "$(S 02)"
reset; mkrun 03 1; echo 0  > "$CFDQ_HOME/jobs/03/exit"; reconcile; ck "pkill a.out=SIGTERM/FTT (exit0)" done   "$(S 03)"
reset; mkrun 04 1; echo 42 > "$CFDQ_HOME/jobs/04/exit"; reconcile; ck "不可避免錯誤 (exit42)"          failed "$(S 04)"

echo "═══ B. 續鏈 RESUME (其他中斷必須續跑) ═══"
reset; mkrun 11 1; echo 124 > "$CFDQ_HOME/jobs/11/exit"; reconcile; ck "SIGUSR1 優雅被搶 (exit124)" queued "$(S 11)"
ck "  └ 優雅退出不需清 orphan (a.out已退)"  0 "$CLEANUP_CALLED"
reset; mkrun 12 1; echo 6   > "$CFDQ_HOME/jobs/12/exit"; reconcile; ck "crash (exit6)"             queued "$(S 12)"
reset; mkrun 13 1; rm -f "$CFDQ_HOME/jobs/13/exit"; MOCK_PROBE="$PROBE_UP_DEAD"; reconcile; ck "kill -9 a.out (無exit檔+pid死)" queued "$(S 13)"
ck "  └ [Bug B] killed → 清節點 orphan"     1 "$CLEANUP_CALLED"
reset; mkrun 14 1 0; rm -f "$CFDQ_HOME/jobs/14/exit"; NOWVAL=999999; MOCK_PROBE="$PROBE_DOWN"; reconcile; ck "節點失聯 > TTL" queued "$(S 14)"
ck "  └ [Bug B] node-lost → 清節點 orphan"  1 "$CLEANUP_CALLED"

echo "═══ C. 安全性 (不可誤判/誤停/誤搶) ═══"
reset; mkrun 21 1; rm -f "$CFDQ_HOME/jobs/21/exit"; MOCK_PROBE="$PROBE_DOWN"; reconcile; ck "節點短暫失聯 < TTL → 維持 running" running "$(S 21)"
reset; mkrun 22 1; rm -f "$CFDQ_HOME/jobs/22/exit"; MOCK_PROBE="$PROBE_UP_ALIVE"; reconcile; ck "alive job (daemon重啟後) → 維持 running" running "$(S 22)"

echo "═══ D. chain=0 (不續鏈模式) ═══"
reset; mkrun 31 0; echo 124 > "$CFDQ_HOME/jobs/31/exit"; reconcile; ck "exit124 但 chain=0 → 停"   done "$(S 31)"
reset; mkrun 32 0; rm -f "$CFDQ_HOME/jobs/32/exit"; MOCK_PROBE="$PROBE_UP_DEAD"; reconcile; ck "kill 但 chain=0 → 停" done "$(S 32)"

echo
echo "═══════════ 結果: PASS=$PASS  FAIL=$FAIL ═══════════"
rm -rf "$CFDQ_HOME"
[ "$FAIL" = 0 ] && { echo "全部通過 ✓"; exit 0; } || { echo "有失敗 ✗"; exit 1; }
