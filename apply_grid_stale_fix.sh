#!/bin/bash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Grid stale-detection fix: mtime → parameter-based
#  移除 grid_zeta_tool.py 的 mtime 依賴，改為參數式判斷
#
#  影響: Edit4, Edit5 (D3Q19) + Edit1, Edit2 (D3Q27)
#  Edit6 已在本 session 直接修改完成，此腳本只處理其他 4 個專案
#
#  用法: bash apply_grid_stale_fix.sh [--dry-run]
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -euo pipefail

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[0;33m'; NC='\033[0m'

ok()   { echo -e "${GRN}[OK]${NC} $*"; }
warn() { echo -e "${YEL}[WARN]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; return 1; }

patch_count=0
fail_count=0

# ─────────────────────────────────────────────────────────────────────
# Helper: patch main.cu — remove grid_zeta_tool.py from deps[]
# ─────────────────────────────────────────────────────────────────────
patch_main_cu() {
    local proj="$1" file="$2"
    echo ""
    echo "═══ Patching main.cu: $proj ═══"

    if [ ! -f "$file" ]; then
        fail "$file not found"
        ((fail_count++))
        return
    fi

    # Check if already patched
    if ! grep -q 'GRID_DAT_DIR "/grid_zeta_tool.py"' "$file"; then
        ok "$file — already patched (no grid_zeta_tool.py in deps)"
        return
    fi

    if $DRY_RUN; then
        warn "[DRY-RUN] Would remove grid_zeta_tool.py dep from $file"
        grep -n 'grid_zeta_tool.py' "$file" | head -3
        return
    fi

    # Remove the line containing GRID_DAT_DIR "/grid_zeta_tool.py" from deps[]
    # Also handle Edit4's extra "variables.h" and Edit5's "grid_params.py" dep
    local tmpfile="${file}.patch_tmp"
    cp "$file" "$tmpfile"

    # Pattern: line inside deps[] array that references grid_zeta_tool.py
    sed -i '/GRID_DAT_DIR "\/grid_zeta_tool.py"/d' "$file"

    # Also remove "variables.h" dep if present in deps[] (Edit4)
    # Only remove if it's inside the deps[] array (between 'const char *deps' and 'NULL')
    python3 -c "
import re, sys
with open('$file', 'r') as f:
    lines = f.readlines()
in_deps = False
remove_indices = []
for i, line in enumerate(lines):
    if 'const char *deps[]' in line:
        in_deps = True
    if in_deps and 'NULL' in line:
        in_deps = False
    if in_deps:
        stripped = line.strip().strip(',').strip('\"')
        if stripped in ('variables.h', 'grid_params.py'):
            remove_indices.append(i)
if remove_indices:
    for idx in sorted(remove_indices, reverse=True):
        del lines[idx]
    with open('$file', 'w') as f:
        f.writelines(lines)
    print(f'  Removed {len(remove_indices)} extra dep(s): variables.h / grid_params.py')
"

    # Update the comment
    sed -i 's|// 新鮮度: 全部輸入依賴 vs 格點檔 mtime|// 新鮮度: 只比較實際影響網格座標的資料依賴\n            // grid_zeta_tool.py 不列入 — 改註解/防呆不應觸發重生|' "$file"
    sed -i 's|// 新鮮度: grid tool / reference 檔 vs 格點檔 mtime|// 新鮮度: 只比較實際影響網格座標的資料依賴\n            // grid_zeta_tool.py 不列入 — 改註解/防呆不應觸發重生|' "$file"
    # Remove the old comment about variables.h if present
    sed -i '/\/\/ (variables.h 不列入: 網格參數已編碼於檔名, 無關參數變動不應觸發重建)/d' "$file"

    # Verify
    if grep -q 'GRID_DAT_DIR "/grid_zeta_tool.py"' "$file"; then
        fail "$file — patch failed, grid_zeta_tool.py still in deps"
        cp "$tmpfile" "$file"
        ((fail_count++))
    else
        ok "$file — grid_zeta_tool.py removed from deps"
        ((patch_count++))
    fi
    rm -f "$tmpfile"
}

# ─────────────────────────────────────────────────────────────────────
# Helper: patch grid_zeta_tool.py — mtime → parameter-based idempotency
# ─────────────────────────────────────────────────────────────────────
patch_grid_zeta_tool() {
    local proj="$1" file="$2" is_duct="${3:-false}"
    echo ""
    echo "═══ Patching grid_zeta_tool.py: $proj ═══"

    if [ ! -f "$file" ]; then
        fail "$file not found"
        ((fail_count++))
        return
    fi

    # Check if already patched (look for the old mtime pattern)
    if ! grep -q 'deps_mtime' "$file"; then
        ok "$file — already patched (no deps_mtime)"
        return
    fi

    if $DRY_RUN; then
        warn "[DRY-RUN] Would replace mtime idempotency in $file"
        grep -n 'deps_mtime\|artifacts_mtime\|Path(__file__)' "$file" | head -5
        return
    fi

    if [ "$is_duct" = "true" ]; then
        # Edit2 (Duct) — has sx_suffix
        python3 -c "
import re
with open('$file', 'r') as f:
    content = f.read()

OLD_BLOCK = '''    # ── Idempotency check: skip only when all generated artifacts are fresh ──
    sa_expected = float(np.tanh(gamma / 2.0))
    grid_key_early = ref_path.stem
    sx_suffix = f\"_sx{float(stretch_a_x):.6f}\" if is_duct else \"\"
    expected_name = f\"adaptive_{grid_key_early}_I{NI}_J{NJ}_s{sa_expected:.6f}{sx_suffix}.dat\"
    expected_path = script_dir / expected_name
    grid_data_expected = script_dir / f\"grid_data_I{NI}_J{NJ}_s{sa_expected:.6f}{sx_suffix}.txt\"
    if expected_path.exists() and grid_data_expected.exists():
        try:
            ok, ni_a, nj_a, ni_e, nj_e = validate_grid_dimensions(
                str(expected_path), NY, NZ)
            deps = [Path(variables_h_path), ref_path, Path(__file__)]
            deps_mtime = max(p.stat().st_mtime for p in deps if p.exists())
            artifacts_mtime = min(expected_path.stat().st_mtime,
                                  grid_data_expected.stat().st_mtime)
            if ok and artifacts_mtime >= deps_mtime:
                print(f\"  [auto] Grid already exists and dimensions match: {expected_name}\")
                print(f\"  [auto] grid_data exists and artifacts are fresh - skipping generation\")
                print(f\"  [auto] I={ni_a} J={nj_a} OK\")
                return str(expected_path)
        except Exception:
            pass'''

NEW_BLOCK = '''    # ── Idempotency check: parameter-based, not mtime-based ──
    # Skip conditions: .dat exists + header I/J == NY/NZ + filename s{STRETCH_A} matches.
    # If .dat is valid but grid_data diagnostic is missing, only regenerate grid_data.
    sa_expected = float(np.tanh(gamma / 2.0))
    grid_key_early = ref_path.stem
    sx_suffix = f\"_sx{float(stretch_a_x):.6f}\" if is_duct else \"\"
    expected_name = f\"adaptive_{grid_key_early}_I{NI}_J{NJ}_s{sa_expected:.6f}{sx_suffix}.dat\"
    expected_path = script_dir / expected_name
    if expected_path.exists():
        try:
            ok, ni_a, nj_a, ni_e, nj_e = validate_grid_dimensions(
                str(expected_path), NY, NZ)
            if ok:
                sa_in_name = abs(sa_expected - stretch_a) < 1e-8
                if not sa_in_name:
                    print(f\"  [auto] Grid exists but STRETCH_A mismatch: \"
                          f\"file={sa_expected:.6f} vs variables.h={stretch_a:.6f}\")
                else:
                    print(f\"  [auto] Grid already exists and parameters match: {expected_name}\")
                    print(f\"  [auto] I={ni_a} J={nj_a}, s={sa_expected:.6f} ✓\")
                    diag_name = f\"grid_data_I{NI}_J{NJ}_s{sa_expected:.6f}{sx_suffix}.txt\"
                    diag_path = script_dir / diag_name
                    if not diag_path.exists():
                        print(f\"  [auto] Diagnostics missing — regenerating {diag_name} only\")
                        x_dat, y_dat, _, _ = parse_tecplot_dat(expected_path)
                        write_grid_data(diag_path, x_dat, y_dat,
                                        NY=NY, NZ=NZ, GAMMA=gamma, ALPHA=alpha,
                                        LZ=LZ, source_dat=expected_name)
                        print(f\"  [auto] Diagnostics written: {diag_name}\")
                    return str(expected_path)
        except Exception:
            pass'''

if OLD_BLOCK in content:
    content = content.replace(OLD_BLOCK, NEW_BLOCK, 1)
    with open('$file', 'w') as f:
        f.write(content)
    print('  Patched successfully')
else:
    print('  ERROR: old block not found (manual check needed)')
    import sys; sys.exit(1)
"
    else
        # Edit1 (Channel) — no sx_suffix
        python3 -c "
import re
with open('$file', 'r') as f:
    content = f.read()

OLD_BLOCK = '''    # ── Idempotency check: skip only when all generated artifacts are fresh ──
    sa_expected = float(np.tanh(gamma / 2.0))
    grid_key_early = ref_path.stem
    expected_name = f\"adaptive_{grid_key_early}_I{NI}_J{NJ}_s{sa_expected:.6f}.dat\"
    expected_path = script_dir / expected_name
    grid_data_expected = script_dir / f\"grid_data_I{NI}_J{NJ}_s{sa_expected:.6f}.txt\"
    if expected_path.exists() and grid_data_expected.exists():
        try:
            ok, ni_a, nj_a, ni_e, nj_e = validate_grid_dimensions(
                str(expected_path), NY, NZ)
            deps = [Path(variables_h_path), ref_path, Path(__file__)]
            deps_mtime = max(p.stat().st_mtime for p in deps if p.exists())
            artifacts_mtime = min(expected_path.stat().st_mtime,
                                  grid_data_expected.stat().st_mtime)
            if ok and artifacts_mtime >= deps_mtime:
                print(f\"  [auto] Grid already exists and dimensions match: {expected_name}\")
                print(f\"  [auto] grid_data exists and artifacts are fresh - skipping generation\")
                print(f\"  [auto] I={ni_a} J={nj_a} OK\")
                return str(expected_path)
        except Exception:
            pass'''

NEW_BLOCK = '''    # ── Idempotency check: parameter-based, not mtime-based ──
    # Skip conditions: .dat exists + header I/J == NY/NZ + filename s{STRETCH_A} matches.
    # If .dat is valid but grid_data diagnostic is missing, only regenerate grid_data.
    sa_expected = float(np.tanh(gamma / 2.0))
    grid_key_early = ref_path.stem
    expected_name = f\"adaptive_{grid_key_early}_I{NI}_J{NJ}_s{sa_expected:.6f}.dat\"
    expected_path = script_dir / expected_name
    if expected_path.exists():
        try:
            ok, ni_a, nj_a, ni_e, nj_e = validate_grid_dimensions(
                str(expected_path), NY, NZ)
            if ok:
                sa_in_name = abs(sa_expected - stretch_a) < 1e-8
                if not sa_in_name:
                    print(f\"  [auto] Grid exists but STRETCH_A mismatch: \"
                          f\"file={sa_expected:.6f} vs variables.h={stretch_a:.6f}\")
                else:
                    print(f\"  [auto] Grid already exists and parameters match: {expected_name}\")
                    print(f\"  [auto] I={ni_a} J={nj_a}, s={sa_expected:.6f} ✓\")
                    diag_name = f\"grid_data_I{NI}_J{NJ}_s{sa_expected:.6f}.txt\"
                    diag_path = script_dir / diag_name
                    if not diag_path.exists():
                        print(f\"  [auto] Diagnostics missing — regenerating {diag_name} only\")
                        x_dat, y_dat, _, _ = parse_tecplot_dat(expected_path)
                        write_grid_data(diag_path, x_dat, y_dat,
                                        NY=NY, NZ=NZ, GAMMA=gamma, ALPHA=alpha,
                                        LZ=LZ, source_dat=expected_name)
                        print(f\"  [auto] Diagnostics written: {diag_name}\")
                    return str(expected_path)
        except Exception:
            pass'''

if OLD_BLOCK in content:
    content = content.replace(OLD_BLOCK, NEW_BLOCK, 1)
    with open('$file', 'w') as f:
        f.write(content)
    print('  Patched successfully')
else:
    print('  ERROR: old block not found (manual check needed)')
    import sys; sys.exit(1)
"
    fi

    if [ $? -eq 0 ]; then
        # Verify no deps_mtime remains
        if grep -q 'deps_mtime' "$file"; then
            fail "$file — patch incomplete, deps_mtime still present"
            ((fail_count++))
        else
            ok "$file — parameter-based idempotency applied"
            ((patch_count++))
        fi
    else
        fail "$file — patch script error"
        ((fail_count++))
    fi
}

# ─────────────────────────────────────────────────────────────────────
# Helper: git commit + push for a project
# ─────────────────────────────────────────────────────────────────────
commit_and_push() {
    local proj_dir="$1" proj_name="$2"
    echo ""
    echo "═══ Committing: $proj_name ═══"

    if $DRY_RUN; then
        warn "[DRY-RUN] Would commit and push in $proj_dir"
        return
    fi

    cd "$proj_dir"
    if git diff --quiet && git diff --cached --quiet; then
        ok "No changes to commit in $proj_name"
        return
    fi

    git add -A main.cu J_Frohlich/grid_zeta_tool.py 2>/dev/null || true
    git commit -m "$(cat <<'EOFMSG'
網格 stale 判斷改為參數式：移除 grid_zeta_tool.py mtime 依賴

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOFMSG
)" || { warn "Nothing to commit in $proj_name"; return; }
    git push && ok "$proj_name pushed" || fail "$proj_name push failed"
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo "━━━ Grid stale-detection fix: mtime → parameter-based ━━━"
echo "Mode: $( $DRY_RUN && echo 'DRY-RUN' || echo 'LIVE' )"
echo ""

# ── D3Q19 ──
patch_main_cu "Edit4" "/home/s8313697/5.Re10595/Edit4_ChapmannForpart/main.cu"
patch_main_cu "Edit5" "/home/s8313697/5.Re10595/Edit5_Rebuild/main.cu"

# ── D3Q27 ──
patch_main_cu "Edit1(D3Q27)" "/home/s8313697/D3Q27_PeriodicHill/Edit1_PeriodicHIllchannel/main.cu"
patch_main_cu "Edit2(D3Q27)" "/home/s8313697/D3Q27_PeriodicHill/Edit2_PeriodicHillDuct/main.cu"

# ── grid_zeta_tool.py (D3Q27 only — Edit4/Edit5 have diverged grid_params.py architecture) ──
patch_grid_zeta_tool "Edit1(D3Q27)" "/home/s8313697/D3Q27_PeriodicHill/Edit1_PeriodicHIllchannel/J_Frohlich/grid_zeta_tool.py" "false"
patch_grid_zeta_tool "Edit2(D3Q27)" "/home/s8313697/D3Q27_PeriodicHill/Edit2_PeriodicHillDuct/J_Frohlich/grid_zeta_tool.py" "true"

echo ""
echo "━━━ Patch summary: ${patch_count} applied, ${fail_count} failed ━━━"

if [ "$fail_count" -gt 0 ]; then
    echo -e "${RED}Some patches failed — check output above${NC}"
    exit 1
fi

if $DRY_RUN; then
    echo ""
    echo "Re-run without --dry-run to apply and commit."
    exit 0
fi

# ── Commit & push each project ──
commit_and_push "/home/s8313697/5.Re10595/Edit4_ChapmannForpart" "Edit4"
commit_and_push "/home/s8313697/5.Re10595/Edit5_Rebuild" "Edit5"
commit_and_push "/home/s8313697/D3Q27_PeriodicHill/Edit1_PeriodicHIllchannel" "Edit1(D3Q27)"
commit_and_push "/home/s8313697/D3Q27_PeriodicHill/Edit2_PeriodicHillDuct" "Edit2(D3Q27)"

echo ""
echo "━━━ All done ━━━"
