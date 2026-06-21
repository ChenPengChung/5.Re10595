#!/bin/bash
# test_blacklist_sanitize.sh — 固化「blacklist 含 #註解/中文/空格等非節點 token 不得洩漏進
# sbatch --exclude」+「空集合在 set -eo pipefail 下不中止」。
# 背景: 2026-06-22 鏈斷根因 = bad_nodes 檔的中文註解整段洩漏進 --exclude → sbatch
# "Unable to open file Edit12" → jobscript 自投 + dispatcher 重投兩層全爆 → 停鏈。
# 修法: bl_effective_exclude 的 merged/keep_live/picked 三處改 `{ grep -E '^[A-Za-z0-9._-]+$' || true; }`
#       (只留合法節點名 token; || true 防空集合 grep exit 1 在 set -e 下中止)。
set -uo pipefail
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB="$SELF_DIR/blacklist_lib.sh"
FAIL=0
PAT='^[A-Za-z0-9._-]+$'        # 與 blacklist_lib.sh 一致的 sanitize pattern
LEAK='[^A-Za-z0-9._,-]'        # locale-safe 洩漏偵測: 任何非「節點名字元或逗號」= 洩漏(#/空格/中文/括號…)

# ── Test 1: sanitize 對污染輸入只留合法節點名 ──
poll=$(printf '25a-hgpn143\n# Edit12 project-local 精簡黑名單 (2026-06-20)\n25a-hgpn024\n#   只留真壞\n  \n其中 39 個其實 SLURM-健康=早被 NCHC 修好的誤排\n')
clean=$(printf '%s\n' "$poll" | { grep -E "$PAT" || true; } | sort -u | paste -sd,)
if [ "$clean" = "25a-hgpn024,25a-hgpn143" ]; then
    echo "  [PASS] sanitize: 註解/中文/空行全擋, 只留 25a-hgpn024,25a-hgpn143"
else
    echo "  [FAIL] sanitize: 期望 '25a-hgpn024,25a-hgpn143' 得到 '$clean'"; FAIL=1
fi

# ── Test 2: 輸出絕無洩漏(locale-safe, 不用 [一-龥] 字元範圍) ──
if printf '%s' "$clean" | grep -qE "$LEAK"; then
    echo "  [FAIL] sanitize 輸出仍含非節點字元(#/空格/中文/括號 → sbatch --exclude 會爆)"; FAIL=1
else
    echo "  [PASS] sanitize 輸出僅含節點名字元+逗號"
fi

# ── Test 3: blacklist_lib.sh 三處都用 { grep -E ... || true; }(token sanitize + 空集合防護) ──
n_sani=$(grep -cF "{ grep -E '^[A-Za-z0-9._-]+\$' || true; }" "$LIB")
if [ "$n_sani" -ge 3 ]; then
    echo "  [PASS] blacklist_lib.sh 套 token-sanitize+||true 共 $n_sani 處(merged/keep_live/picked)"
else
    echo "  [FAIL] blacklist_lib.sh 只 $n_sani 處套 sanitize(應>=3; 有未防護路徑)"; FAIL=1
fi

# ── Test 4: 合法名通過 / 垃圾被擋 ──
sub=0
for good in 25a-hgpn143 25a-cpn001 node.example-1; do
    printf '%s\n' "$good" | grep -qE "$PAT" || { echo "  [FAIL] 合法名 '$good' 被誤擋"; FAIL=1; sub=1; }
done
for bad in '# comment' 'node with space' '中文' '25a-hgpn143,extra'; do
    printf '%s\n' "$bad" | grep -qE "$PAT" && { echo "  [FAIL] 垃圾 '$bad' 未被擋"; FAIL=1; sub=1; }
done
[ $sub -eq 0 ] && echo "  [PASS] 合法名通過 / 垃圾(空格/逗號/#/中文)被擋"

# ── Test 5: ★空集合/全垃圾輸入在 set -eo pipefail 下不中止(|| true 防護, codex 抓到的 regression) ──
if ( set -eo pipefail; out=$(printf '# only comment\n中文\n  \n' | { grep -E "$PAT" || true; } | sort -u); [ -z "$out" ] ); then
    echo "  [PASS] 全垃圾/空集合: sanitize 回空且 set -eo pipefail 不中止(grep no-match 被 ||true 吸收)"
else
    echo "  [FAIL] 空集合在 set -eo pipefail 下中止 或 輸出非空"; FAIL=1
fi

# ── Test 6: ★完全空輸入同樣不中止 ──
if ( set -eo pipefail; out=$(printf '' | { grep -E "$PAT" || true; } | sort -u); [ -z "$out" ] ); then
    echo "  [PASS] 完全空輸入: 不中止、回空"
else
    echo "  [FAIL] 空輸入在 set -eo pipefail 下中止"; FAIL=1
fi

echo "=== $([ $FAIL -eq 0 ] && echo '✅ 全過' || echo "❌ $FAIL 項失敗") ==="
exit $FAIL
