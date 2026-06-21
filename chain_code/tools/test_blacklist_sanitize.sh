#!/bin/bash
# test_blacklist_sanitize.sh — 固化「blacklist 含 #註解/中文/空格等非節點 token 不得洩漏進
# sbatch --exclude」。背景: 2026-06-22 鏈斷根因 = bad_nodes 檔的中文註解整段洩漏進 --exclude →
# sbatch "Unable to open file Edit12" → jobscript 自投 + dispatcher 重投兩層全爆 → 停鏈。
# 修法: bl_effective_exclude 的 merged 步驟改 `grep -E '^[A-Za-z0-9._-]+$'`(只留合法節點名 token)。
set -uo pipefail
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB="$SELF_DIR/blacklist_lib.sh"
FAIL=0
PAT='^[A-Za-z0-9._-]+$'   # 與 blacklist_lib.sh 一致的 sanitize pattern

# ── Test 1: sanitize pattern 對污染輸入只留合法節點名 ──
poll=$(printf '25a-hgpn143\n# Edit12 project-local 精簡黑名單 (2026-06-20)\n25a-hgpn024\n#   只留真壞\n  \n其中 39 個其實 SLURM-健康=早被 NCHC 修好的誤排\n')
clean=$(printf '%s\n' "$poll" | grep -E "$PAT" | sort -u | paste -sd,)
if [ "$clean" = "25a-hgpn024,25a-hgpn143" ]; then
    echo "  [PASS] sanitize: 註解/中文/空行全擋, 只留 25a-hgpn024,25a-hgpn143"
else
    echo "  [FAIL] sanitize: 期望 '25a-hgpn024,25a-hgpn143' 得到 '$clean'"; FAIL=1
fi

# ── Test 2: 輸出絕無 # / 中文 / 空格洩漏 ──
if printf '%s' "$clean" | grep -qE '#|[一-龥]| '; then
    echo "  [FAIL] sanitize 輸出仍含 #/中文/空格(會讓 sbatch --exclude 爆)"; FAIL=1
else
    echo "  [PASS] sanitize 輸出無 #/中文/空格"
fi

# ── Test 3: blacklist_lib.sh 的 merged 步驟確實用 token-pattern(非舊的只濾空行) ──
if grep -qF "grep -E '^[A-Za-z0-9._-]+\$' | sort -u )" "$LIB"; then
    echo "  [PASS] blacklist_lib.sh merged 步驟已套 token sanitize"
else
    echo "  [FAIL] blacklist_lib.sh merged 步驟未見 token sanitize(回退風險)"; FAIL=1
fi

# ── Test 4: 合法節點名(含 . - 數字)通過; 帶空格/逗號/# 的垃圾被擋 ──
for good in 25a-hgpn143 25a-cpn001 node.example-1; do
    printf '%s\n' "$good" | grep -qE "$PAT" || { echo "  [FAIL] 合法名 '$good' 被誤擋"; FAIL=1; }
done
for bad in '# comment' 'node with space' '中文' '25a-hgpn143,extra'; do
    printf '%s\n' "$bad" | grep -qE "$PAT" && { echo "  [FAIL] 垃圾 '$bad' 未被擋"; FAIL=1; }
done
[ $FAIL -eq 0 ] && echo "  [PASS] 合法名通過 / 垃圾(空格/逗號/#/中文)被擋"

echo "=== $([ $FAIL -eq 0 ] && echo '✅ 全過' || echo "❌ $FAIL 項失敗") ==="
exit $FAIL
