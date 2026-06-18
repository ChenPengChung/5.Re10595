#!/bin/bash
# ==============================================================================
# run.sh (LOCAL fork) — GILBM Periodic Hill 本地 V100-8 + cfdq 一條龍入口
#
# fork 自 chain_code_nchc/run.sh (SLURM/H200/GB200). 本地版抽掉所有 SLURM 段
# (sbatch/squeue/sinfo/partition/dispatcher/HEAD.lockdir), 改用:
#   - 母機 python3.12 (numpy/scipy) 做 phase1 網格 + phase2 種子內插
#   - chain_code_local/build_local.sh 本地編譯 (nvcc -arch=sm_70 → ./a.out)
#   - cfdq (~/bin/cfdq) 排隊 / 整台獨佔 / 續鏈, 投 chain_code_local/hill_local_chain.sh
#
# 用法:
#   bash chain_code_local/run.sh                 自動偵測情境並投遞 (最常用)
#   bash chain_code_local/run.sh --status        只看狀態, 不投遞
#   bash chain_code_local/run.sh --rebuild       強制重編 a.out 再投
#   bash chain_code_local/run.sh --force-cold    清空所有 state / history 後從頭跑 (需確認)
#   bash chain_code_local/run.sh --regrid-from-origin [--old-grid <OLD.dat>] [--new-grid <NEW.dat>]
#                                                從唯一 origin 內插到現格 checkpoint
#   bash chain_code_local/run.sh                 若 phase1 網格 + phase2 種子齊全,
#                                                會自動跑 regrid pipeline 再投遞 (預設)
#   bash chain_code_local/run.sh --force-regrid  搭配 --regrid-from-origin, 先清掉既有 checkpoint 再重建
#   bash chain_code_local/run.sh --preflight-only  只跑 grid/regrid/provenance/編譯 備料, 不投遞
#   bash chain_code_local/run.sh --no-submit     完成全部備料 (含編譯), 不投遞
#   bash chain_code_local/run.sh --np N          cfdq 申請 GPU 數 (預設 8)
#   bash chain_code_local/run.sh --name X         cfdq job 名稱 (預設 edit14)
#   bash chain_code_local/run.sh --no-queue-check 跳過 cfdq 重投 guard (CI/自動化用)
#   bash chain_code_local/run.sh -h | --help     顯示此使用說明
#
# 情境對照 (皆用 bash chain_code_local/run.sh):
#   情境 1 冷啟動 (全新)       → 自動編譯 + 空 state + cfdq add
#   情境 2 只有 phase2 種子    → 自動內插成現格 checkpoint + cfdq add
#   情境 3A 本專案已有 cfdq job → 偵測後退出 (不重投)
#   情境 3B 鏈斷了             → 保留 state, 直接 cfdq add 接續
#
# 安全措施:
#   - flock .run.lock 防止兩個 run.sh 同時執行雙投
#   - --force-cold 必須人工確認
#   - cfdq guard: 本專案已有 active cfdq job 時拒絕重投 (唯讀掃描, 不動別專案 job)
# ==============================================================================

set -eo pipefail   # 不使用 -u: hpcx-init.sh 會踩 unbound variable

# ── [方案 A path discipline] 自我定位 + 鎖 cwd 到 PROJECT_ROOT ────────────────
# 本 script 位於 chain_code_local/, 但許多相對路徑 (restart/, a.out, result/, .run.lock)
# 都在 PROJECT_ROOT (= CHAIN_DIR/.., 與 NCHC 相同, chain_code_local/ 的 .. 一樣是專案根).
_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || realpath "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
CHAIN_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
PROJECT_ROOT="$(cd "$CHAIN_DIR/.." && pwd)"
cd "$PROJECT_ROOT" || { echo "[run.sh] FATAL: cannot cd to PROJECT_ROOT=$PROJECT_ROOT" >&2; exit 1; }

MODE_COLD=0
MODE_REBUILD=0
MODE_STATUS=0
MODE_NO_QCHECK=0   # 1 = 跳過 cfdq 重投 guard (CI/自動化)
MODE_REGRID=0
MODE_FORCE_REGRID=0
MODE_PREFLIGHT_ONLY=0
MODE_NO_SUBMIT=0   # 1 = 完成全部備料 (含編譯) 但不投遞
MODE_NP=8          # cfdq --np (本地 1 台 8×V100 整台獨佔)
MODE_NAME="edit14" # cfdq --name
REGRID_OLD_GRID=""
REGRID_NEW_GRID=""
REGRID_OLD_GAMMA=""
REGRID_OLD_ALPHA=""
REGRID_ORIGIN_DIR=""

while [ $# -gt 0 ]; do
    arg="$1"
    case "$arg" in
        --force-cold)      MODE_COLD=1 ;;
        --rebuild)         MODE_REBUILD=1 ;;
        --status)          MODE_STATUS=1 ;;
        --no-queue-check)  MODE_NO_QCHECK=1 ;;
        --no-submit)       MODE_NO_SUBMIT=1 ;;
        --np)
            shift
            if [ $# -eq 0 ]; then echo "[run.sh] Missing value after $arg"; exit 2; fi
            MODE_NP="$1" ;;
        --np=*)
            MODE_NP="${arg#*=}" ;;
        --name)
            shift
            if [ $# -eq 0 ]; then echo "[run.sh] Missing value after $arg"; exit 2; fi
            MODE_NAME="$1" ;;
        --name=*)
            MODE_NAME="${arg#*=}" ;;
        --regrid-from-origin) MODE_REGRID=1 ;;
        --force-regrid)    MODE_FORCE_REGRID=1 ;;
        --preflight-only)  MODE_PREFLIGHT_ONLY=1 ;;
        --origin-dir)
            shift
            if [ $# -eq 0 ]; then echo "[run.sh] Missing value after $arg"; exit 2; fi
            REGRID_ORIGIN_DIR="$1" ;;
        --origin-dir=*)
            REGRID_ORIGIN_DIR="${arg#*=}" ;;
        --old-grid|--old-grid-dat)
            shift
            if [ $# -eq 0 ]; then
                echo "[run.sh] Missing value after $arg"
                exit 2
            fi
            REGRID_OLD_GRID="$1" ;;
        --old-grid=*|--old-grid-dat=*)
            REGRID_OLD_GRID="${arg#*=}" ;;
        --new-grid|--new-grid-dat)
            shift
            if [ $# -eq 0 ]; then
                echo "[run.sh] Missing value after $arg"
                exit 2
            fi
            REGRID_NEW_GRID="$1" ;;
        --new-grid=*|--new-grid-dat=*)
            REGRID_NEW_GRID="${arg#*=}" ;;
        --old-gamma)
            shift
            if [ $# -eq 0 ]; then echo "[run.sh] Missing value after $arg"; exit 2; fi
            REGRID_OLD_GAMMA="$1" ;;
        --old-gamma=*)
            REGRID_OLD_GAMMA="${arg#*=}" ;;
        --old-alpha)
            shift
            if [ $# -eq 0 ]; then echo "[run.sh] Missing value after $arg"; exit 2; fi
            REGRID_OLD_ALPHA="$1" ;;
        --old-alpha=*)
            REGRID_OLD_ALPHA="${arg#*=}" ;;
        -h|--help)
            sed -n '2,46p' "$0"
            exit 0 ;;
        *)
            echo "[run.sh] Unknown arg: $arg"
            echo "         請用 -h / --help 查看合法參數"
            exit 2 ;;
    esac
    shift
done

if [ "$MODE_COLD" -eq 1 ] && [ "$MODE_REGRID" -eq 1 ]; then
    echo "[run.sh] FATAL: --force-cold 與 --regrid-from-origin 不能同時使用"
    exit 2
fi
if [ "$MODE_FORCE_REGRID" -eq 1 ] && [ "$MODE_REGRID" -eq 0 ]; then
    echo "[run.sh] FATAL: --force-regrid 必須搭配 --regrid-from-origin"
    exit 2
fi
# 註: 本地刻意「不」拒絕 --force-cold + --preflight-only (NCHC 在此 FATAL).
#     本地此組合合法 = wipe + cold 備料 + 不投遞 (V14 要用).

# ── PY 強制 (母機 python3.12 才有 numpy/scipy; 系統 python3=3.6.8 無 numpy) ──
PY="${LOCAL_PY:-python3.12}"
command -v "$PY" >/dev/null || { echo "[FATAL] 找不到 $PY"; exit 42; }
"$PY" - <<'EOF' || { echo "[FATAL] $PY 缺 numpy/scipy"; exit 42; }
import numpy, scipy
EOF

_project_abs_path() {
    case "$1" in
        /*) printf '%s\n' "$1" ;;
        *)  printf '%s/%s\n' "$PROJECT_ROOT" "$1" ;;
    esac
}

_read_define_value() {
    local key="$1"
    awk -v key="$key" '
        $1 == "#define" && $2 == key {
            val=$3
            gsub(/[()"]/, "", val)
            print val
            exit
        }
    ' variables.h 2>/dev/null
}

_grid_dim_value() {
    local file="$1" key="$2"
    awk -v key="$key" '
        {
            gsub(/,/, " ")
            for (i = 1; i <= NF; i++) {
                if ($i ~ "^" key "=") {
                    sub("^" key "=", "", $i)
                    print $i
                    exit
                }
            }
        }
    ' "$file" 2>/dev/null
}

_read_string_define_value() {
    local key="$1"
    awk -v key="$key" '
        $1 == "#define" && $2 == key {
            if (match($0, /"[^"]+"/)) {
                print substr($0, RSTART + 1, RLENGTH - 2)
                exit
            }
        }
    ' variables.h 2>/dev/null
}

# solver 端真正讀的網格檔名 (initialization.h:99 / main.cu:271 / grid_zeta_tool.py:2272):
#   <GRID_DAT_DIR>/adaptive_<stem>_I<NY>_J<NZ>_s<STRETCH_A:.6f>.dat
# stem = GRID_DAT_REF 去掉 .dat ("3.fine grid.dat" → "3.fine grid"). 路徑含空白, 全程雙引號.
_derive_solver_grid_path() {
    local dir stem ny nz sa
    dir="$(_read_string_define_value GRID_DAT_DIR)"        # J_Frohlich
    stem="$(_read_string_define_value GRID_DAT_REF)"; stem="${stem%.dat}"   # "3.fine grid"
    ny="$(_read_define_value NY)"           # 513
    nz="$(_read_define_value NZ)"           # 257
    sa="$(_read_define_value STRETCH_A)"    # 0.95 (純數字; 不要讀 GAMMA, 它是 log() 運算式)
    if [ -z "$dir" ] || [ -z "$stem" ] || [ -z "$ny" ] || [ -z "$nz" ] || [ -z "$sa" ]; then
        echo "[FATAL] 無法從 variables.h 推導 solver grid path (GRID_DAT_DIR/GRID_DAT_REF/NY/NZ/STRETCH_A)" >&2
        exit 1
    fi
    printf '%s/adaptive_%s_I%d_J%d_s%.6f.dat' "$dir" "$stem" "$ny" "$nz" "$sa"
}

_compare_grid_dat_coords_exact() {
    local phase_grid="$1" solver_grid="$2" expected_i="$3" expected_j="$4"
    "$PY" - "$phase_grid" "$solver_grid" "$expected_i" "$expected_j" <<'PY'
import sys

phase_grid, solver_grid, expected_i, expected_j = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])

def read_grid(path):
    dims = {}
    coords = []
    in_data = False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.strip()
            if not in_data:
                for token in stripped.replace(",", " ").split():
                    upper = token.upper()
                    if upper.startswith("I="):
                        dims["I"] = int(token.split("=", 1)[1])
                    elif upper.startswith("J="):
                        dims["J"] = int(token.split("=", 1)[1])
                if stripped.upper().startswith("DT="):
                    in_data = True
                continue
            parts = stripped.split()
            if len(parts) >= 2:
                try:
                    coords.append((float(parts[0]), float(parts[1])))
                except ValueError:
                    pass
    if dims.get("I") != expected_i or dims.get("J") != expected_j:
        raise SystemExit(f"{path}: I/J={dims.get('I')}/{dims.get('J')} != expected {expected_i}/{expected_j}")
    expected_count = expected_i * expected_j
    if len(coords) != expected_count:
        raise SystemExit(f"{path}: coordinate rows={len(coords)} != expected {expected_count}")
    return coords

a = read_grid(phase_grid)
b = read_grid(solver_grid)
if len(a) != len(b):
    raise SystemExit(f"row count mismatch: {len(a)} != {len(b)}")

for idx, ((ax, ay), (bx, by)) in enumerate(zip(a, b), 1):
    if ax != bx or ay != by:
        raise SystemExit(
            "coordinate mismatch at row {}: phase=({:.17g}, {:.17g}) solver=({:.17g}, {:.17g})".format(
                idx, ax, ay, bx, by
            )
        )

print(f"exact coordinate match: {len(a)} rows")
PY
}

_discover_phase1_grid() {
    local prefix="$1" label="$2"
    local -a candidates=()
    while IFS= read -r f; do
        [ -n "$f" ] && candidates+=("$f")
    done < <(find phase1_generategrid -maxdepth 1 -type f -name "${prefix}*.dat" | sort)

    if [ "${#candidates[@]}" -eq 0 ]; then
        echo "[FATAL] phase1_generategrid/ 找不到 ${label} grid: ${prefix}*.dat" >&2
        exit 1
    fi
    if [ "${#candidates[@]}" -gt 1 ]; then
        echo "[FATAL] phase1_generategrid/ 有多個 ${label} grid 候選, 請用 --${label,,}-grid 明確指定:" >&2
        printf '        %s\n' "${candidates[@]}" >&2
        exit 1
    fi
    printf '%s\n' "${candidates[0]}"
}

# 本地固定 V100 (無 SLURM/分區偵測). BUILD_SCRIPT = 本地編譯腳本.
CLUSTER="V100"
BUILD_SCRIPT="$CHAIN_DIR/build_local.sh"

if [ ! -f "$BUILD_SCRIPT" ]; then
    echo "[run.sh] FATAL: 缺少本地編譯腳本 $BUILD_SCRIPT"
    exit 3
fi

mkdir -p restart/

# ═════════════════════════════════════════════════════════════════════════
# flock: 防止並發 run.sh 同時執行 (兩個 terminal 同時 run.sh → 雙投 bug)
# ═════════════════════════════════════════════════════════════════════════
exec 200>.run.lock
if ! flock -n 200; then
    echo "[run.sh] 另一個 run.sh 正在執行 (.run.lock 被佔用)"
    echo "         若確定沒有其他 run.sh,可移除 lock: rm .run.lock"
    exit 4
fi

# ═════════════════════════════════════════════════════════════════════════
# cfdq 重投 guard (取代 NCHC 的 RUNNING.lockdir/chain_jobid + scenario-3A squeue)
# ─────────────────────────────────────────────────────────────────────────
# 掃所有 cfdq job, 只認 cwd==本專案 且 status∈{queued,launching,running} 者.
# 唯讀: 只 echo + exit, 絕不 rm/cancel; 別專案 (絕對路徑 cwd 不等) 自動略過.
# --no-queue-check 可跳過本 guard.
# ═════════════════════════════════════════════════════════════════════════
CFDQ_ACTIVE_JOB=""
CFDQ_ACTIVE_STATE=""
if [ "$MODE_NO_QCHECK" -eq 0 ]; then
    for s in "$HOME"/.cfdq/jobs/*/spec; do
        [ -f "$s" ] || continue
        cwd="$(sed -n 's/^cwd=//p' "$s")"
        st="$(cat "${s%/spec}/status" 2>/dev/null || true)"
        if [ "$cwd" = "$PWD" ]; then
            case "$st" in
                queued|launching|running)
                    CFDQ_ACTIVE_JOB="$(basename "${s%/spec}")"
                    CFDQ_ACTIVE_STATE="$st"
                    ;;
            esac
        fi
    done
fi

# ═════════════════════════════════════════════════════════════════════════
# 狀態偵測
# ═════════════════════════════════════════════════════════════════════════
HAS_BIN=0
[ -x ./a.out ] && HAS_BIN=1

HAS_CKPT=0
while IFS= read -r _d; do
    _d=${_d%/}
    case "$_d" in *.WRITING) continue ;; esac
    if [ -s "$_d/metadata.dat" ] && [ -s "$_d/f00_0.bin" ] && [ -s "$_d/rho_0.bin" ]; then
        HAS_CKPT=1
        break
    fi
done < <(ls -1d restart/checkpoint/step_*/ 2>/dev/null)

# ═════════════════════════════════════════════════════════════════════════
# 列印狀態 banner (本地版: 無 cluster/partition/jobscript/queue-jobs/chain-state)
# ═════════════════════════════════════════════════════════════════════════
echo "════════════════════════════════════════════════════════════════"
echo " run.sh 狀態偵測 @ $(date '+%F %T')"
echo "   pwd          : $(pwd)"
echo "   a.out        : $([ "$HAS_BIN"  -eq 1 ] && echo 'YES' || echo 'NO')"
echo "   checkpoint   : $([ "$HAS_CKPT" -eq 1 ] && echo 'YES (restart/checkpoint/)' || echo 'NO')"
echo "   build script : $BUILD_SCRIPT"
if [ "$MODE_NO_QCHECK" -eq 1 ]; then
    echo "   cfdq job     : (--no-queue-check, 已跳過 cfdq guard)"
elif [ -n "$CFDQ_ACTIVE_JOB" ]; then
    echo "   cfdq job     : $CFDQ_ACTIVE_JOB ($CFDQ_ACTIVE_STATE, 本專案)"
else
    echo "   cfdq job     : (本專案無 active job)"
fi
echo "════════════════════════════════════════════════════════════════"

if [ "$MODE_STATUS" -eq 1 ]; then
    echo "[--status] 只顯示狀態,退出."
    exit 0
fi

# ═════════════════════════════════════════════════════════════════════════
# 情境 3A: 本專案已有 active cfdq job → 不重投
# ═════════════════════════════════════════════════════════════════════════
if [ -n "$CFDQ_ACTIVE_JOB" ]; then
    echo ""
    echo "[3A] 本專案已有 cfdq job ($CFDQ_ACTIVE_JOB, $CFDQ_ACTIVE_STATE) -- run.sh 不重投."
    echo "     若要重投, 請先等本輪結束 (或用 --no-queue-check 跳過此 guard)."
    exit 1
fi

# ═════════════════════════════════════════════════════════════════════════
# --force-cold: 清空所有 state / history / checkpoint, 從頭跑
# ═════════════════════════════════════════════════════════════════════════
if [ "$MODE_COLD" -eq 1 ]; then
    echo ""
    echo "WARN --force-cold 會刪除:"
    echo "   restart/ (含 checkpoint)"
    echo "   checkrho.dat / Ustar_Force_record.dat / timing_log.dat"
    echo "   statistics/ / checkpoint/ (legacy root) "
    read -r -p "   確認從頭跑? 舊 chain 資料將永久消失 [y/N]: " ok
    if [ "$ok" != "y" ] && [ "$ok" != "Y" ]; then
        echo "已取消."
        exit 0
    fi
    rm -rf restart/ checkpoint/
    rm -f checkrho.dat Ustar_Force_record.dat timing_log.dat
    rm -rf statistics/
    mkdir -p restart/
    HAS_CKPT=0
    echo "[--force-cold] 已清理完畢, 進入 Scenario 1 冷啟動流程"
fi

# ═════════════════════════════════════════════════════════════════════════
# PIPELINE 三情境判定 (Three-Case Decision Tree)
#
#   Case 1: restart/ 有有效 checkpoint → 續跑 (verify/regenerate grid)
#   Case 2: regrid 輸入完整 (phase1 網格 + phase2 種子齊全) → 內插 pipeline
#   Case 3: cold-start
#
# 決策優先:
#   --force-cold              → Case 3
#   --regrid-from-origin      → Case 2 (明確指定)
#   restart/ 有 checkpoint    → Case 1
#   _regrid_inputs_complete   → Case 2 (★ 自動觸發, 預設路徑)
#   其他                      → Case 3
# ═════════════════════════════════════════════════════════════════════════

# ── Helper: phase2 種子是否齊全 (依 R=mpi_rank_count 動態算期望檔數) ──
# 齊全條件: metadata.dat 可讀 mpi_rank_count(R)+grid_dims+step+accu_count,
#   且 .bin 檔數 = (19 f + 1 rho + 36 sum_*)×R + 3 cv_*history = (56)*R + 3,
#   加 1 個 metadata.dat → total = 56*R + 4 (R=8 → 452).
_seed_complete() {
    local dir="$1"
    local meta="$dir/metadata.dat"
    [ -s "$meta" ] || { echo "[seed] 缺 metadata.dat: $meta" >&2; return 1; }
    local _R _DIMS _STEP _ACCU
    _R="$(awk -F= '$1=="mpi_rank_count"{print $2; exit}' "$meta" 2>/dev/null)"
    _DIMS="$(awk -F= '$1=="grid_dims"{print $2; exit}' "$meta" 2>/dev/null)"
    _STEP="$(awk -F= '$1=="step"{print $2; exit}' "$meta" 2>/dev/null)"
    _ACCU="$(awk -F= '$1=="accu_count"{print $2; exit}' "$meta" 2>/dev/null)"
    if [ -z "$_R" ] || [ -z "$_DIMS" ] || [ -z "$_STEP" ] || [ -z "$_ACCU" ]; then
        echo "[seed] metadata 缺 mpi_rank_count/grid_dims/step/accu_count: $meta" >&2
        return 1
    fi
    case "$_R" in *[!0-9]*|"") echo "[seed] mpi_rank_count 非整數: $_R" >&2; return 1 ;; esac
    # 期望檔數 (依 R 動態): (19+1+36)*R bin + 3 cv_*history bin + 1 metadata.dat
    local _EXPECT_BIN=$(( (19 + 1 + 36) * _R + 3 ))
    local _EXPECT_TOTAL=$(( _EXPECT_BIN + 1 ))
    local _N_BIN _N_TOTAL
    _N_BIN=$(find "$dir" -maxdepth 1 -type f -name '*.bin' 2>/dev/null | wc -l)
    _N_TOTAL=$(find "$dir" -maxdepth 1 -type f 2>/dev/null | wc -l)
    if [ "$_N_BIN" -lt "$_EXPECT_BIN" ]; then
        echo "[seed] .bin 檔數不足: 有 $_N_BIN < 期望 $_EXPECT_BIN (R=$_R)" >&2
        return 1
    fi
    if [ "$_N_TOTAL" -lt "$_EXPECT_TOTAL" ]; then
        echo "[seed] 總檔數不足: 有 $_N_TOTAL < 期望 $_EXPECT_TOTAL (R=$_R)" >&2
        return 1
    fi
    # (強) 若同層有 *.VERIFY_RESULT.txt, 須含 VERDICT.*PASS; 否則僅警告 (母機已驗過一次, 全 sha256 很慢)
    local _vr
    _vr="$(ls -1 "${dir%/}".VERIFY_RESULT.txt 2>/dev/null | head -1)"
    [ -z "$_vr" ] && _vr="$(ls -1 "$(dirname "$dir")"/*.VERIFY_RESULT.txt 2>/dev/null | head -1)"
    if [ -n "$_vr" ] && [ -f "$_vr" ]; then
        if ! grep -qiE 'VERDICT.*PASS' "$_vr"; then
            echo "[seed] 警告: 種子驗證檔未 PASS: $_vr (僅警告, 不阻擋)" >&2
        fi
    fi
    return 0
}

# ── Helper: 檢查 Case 2 regrid 前置條件是否全部滿足 (使用者的「齊全才發動」閘) ──
# 成功時設定: _AUTO_ORIGIN_DIR, _AUTO_OLD_GRID, _AUTO_NEW_GRID
# 條件 A (phase1 網格齊全): NEW_GRID 存在且表頭 I==NY, J==NZ; OLD grid 存在.
# 條件 B (phase2 種子齊全): _seed_complete "$_AUTO_ORIGIN_DIR" 為真.
_regrid_inputs_complete() {
    [ -d phase1_generategrid ] || return 1
    [ -d phase2_generatecheckpoint ] || return 1

    local _ny _nz
    _ny="$(_read_define_value NY)"
    _nz="$(_read_define_value NZ)"
    [ -n "$_ny" ] && [ -n "$_nz" ] || return 1

    # ── A. phase1 NEW grid (= solver 現格) 存在 + 表頭維度相符 ──
    local _new_grid _new_i _new_j
    _new_grid="$(_derive_solver_grid_path)"
    [ -s "$_new_grid" ] || { echo "[regrid-gate] NEW grid 不存在: $_new_grid" >&2; return 1; }
    _new_i="$(_grid_dim_value "$_new_grid" I)"
    _new_j="$(_grid_dim_value "$_new_grid" J)"
    if [ "$_new_i" != "$_ny" ] || [ "$_new_j" != "$_nz" ]; then
        echo "[regrid-gate] NEW grid 表頭 I=$_new_i J=$_new_j != NY=$_ny NZ=$_nz" >&2
        return 1
    fi

    # ── A. OLD grid (唯一 phase1_generategrid/oldgrid_*) 存在 ──
    _AUTO_OLD_GRID="$(_discover_phase1_grid oldgrid OLD 2>/dev/null)" || return 1
    _AUTO_NEW_GRID="$_new_grid"

    # ── 唯一 origin (含 metadata.dat) ──
    _AUTO_ORIGIN_DIR=""
    local _cnt=0
    for _d in restart/step_*_origin*/ \
              phase2_generatecheckpoint/step_*_origin*/ \
              phase2_generatecheckpoint/oldcheckpoint_*/ \
              phase2_generatecheckpoint/step_*/; do
        [ -s "${_d}metadata.dat" ] || continue
        _AUTO_ORIGIN_DIR="${_d%/}"
        _cnt=$((_cnt + 1))
    done
    [ "$_cnt" -eq 1 ] || return 1

    # ── B. phase2 種子齊全 ──
    _seed_complete "$_AUTO_ORIGIN_DIR" || return 1

    return 0
}

# ── Helper: 執行 regrid 維度驗證 + 內插 + 產物檢查 ──
# 呼叫前需設定: REGRID_OLD_GRID, REGRID_NEW_GRID, _ORIGIN_DIR (絕對路徑)
#               _VH_NY, _VH_NZ (variables.h 值)
_run_regrid_pipeline() {
    # Step A: NEW grid header vs variables.h
    local _NEW_I _NEW_J
    _NEW_I="$(_grid_dim_value "$REGRID_NEW_GRID" I)"
    _NEW_J="$(_grid_dim_value "$REGRID_NEW_GRID" J)"
    if [ "$_NEW_I" != "$_VH_NY" ] || [ "$_NEW_J" != "$_VH_NZ" ]; then
        echo "[FATAL] NEW grid header 與 variables.h 不一致"
        echo "        NEW grid: I=$_NEW_I J=$_NEW_J"
        echo "        variables.h: NY=$_VH_NY NZ=$_VH_NZ"
        exit 1
    fi

    # Step B: OLD grid header vs origin metadata
    local _ORIGIN_META _OLD_JP _OLD_DIMS _OLD_NX6 _OLD_NYD6 _OLD_NZ6 _OLD_NY _OLD_NZ _OLD_I _OLD_J
    _ORIGIN_META="$_ORIGIN_DIR/metadata.dat"
    _OLD_JP=$(awk -F= '$1=="mpi_rank_count"{print $2; exit}' "$_ORIGIN_META" 2>/dev/null)
    _OLD_DIMS=$(awk -F= '$1=="grid_dims"{print $2; exit}' "$_ORIGIN_META" 2>/dev/null)
    IFS=, read -r _OLD_NX6 _OLD_NYD6 _OLD_NZ6 <<< "$_OLD_DIMS"
    if [ -z "$_OLD_JP" ] || [ -z "$_OLD_NYD6" ] || [ -z "$_OLD_NZ6" ]; then
        echo "[FATAL] origin metadata 缺 mpi_rank_count 或 grid_dims: $_ORIGIN_META"
        exit 1
    fi
    _OLD_NY=$(( (_OLD_NYD6 - 7) * _OLD_JP + 1 ))
    _OLD_NZ=$(( _OLD_NZ6 - 6 ))
    _OLD_I="$(_grid_dim_value "$REGRID_OLD_GRID" I)"
    _OLD_J="$(_grid_dim_value "$REGRID_OLD_GRID" J)"
    if [ "$_OLD_I" != "$_OLD_NY" ] || [ "$_OLD_J" != "$_OLD_NZ" ]; then
        echo "[FATAL] OLD grid header 與 origin metadata 不一致"
        echo "        OLD grid: I=$_OLD_I J=$_OLD_J"
        echo "        origin: NY=$_OLD_NY NZ=$_OLD_NZ"
        exit 1
    fi

    # Step C: 執行 checkpoint interpolation (母機 python3.12, 即時; 真內插, 不 dry-run)
    # 硬擋: NEW grid 必須已在 §6.3 受控生成 (interp 不自行補生).
    [ -s "$REGRID_NEW_GRID" ] || { echo "[FATAL] solver 網格未生成 (先跑 grid_zeta_tool.py --auto): $REGRID_NEW_GRID"; exit 1; }

    echo "[case-2] 維度驗證通過, 執行 checkpoint interpolation (old grid → new grid)..."
    local _INTERP_CMD
    _INTERP_CMD=("$PY" phase2_generatecheckpoint/interp_checkpoint.py --auto --step 1
                 --old-dir "$_ORIGIN_DIR"
                 --variables-h variables.h
                 --old-grid-dat "$REGRID_OLD_GRID"
                 --new-grid-dat "$REGRID_NEW_GRID"
                 --solver-grid-dat "$REGRID_NEW_GRID"
                 --no-generate-solver-grid
                 --skip-drift-check)
    [ -n "${REGRID_OLD_GAMMA:-}" ] && _INTERP_CMD+=(--old-gamma "$REGRID_OLD_GAMMA")
    [ -n "${REGRID_OLD_ALPHA:-}" ] && _INTERP_CMD+=(--old-alpha "$REGRID_OLD_ALPHA")

    if "${_INTERP_CMD[@]}"; then
        local _CKPT_DIR="restart/checkpoint/step_00000001"
        local _CKPT_OK=1
        [ -s "$_CKPT_DIR/metadata.dat" ] || _CKPT_OK=0
        [ -s "$_CKPT_DIR/f00_0.bin" ]    || _CKPT_OK=0
        [ -s "$_CKPT_DIR/rho_0.bin" ]    || _CKPT_OK=0
        [ -s "restart/grid_provenance" ]  || _CKPT_OK=0
        if [ "$_CKPT_OK" -eq 1 ]; then
            echo "[case-2] 插值成功: $_CKPT_DIR"
            HAS_CKPT=1
        else
            echo "[FATAL] interp_checkpoint.py 回傳 0 但產物不完整 (缺 metadata/f00/rho/provenance)"
            rm -rf restart/checkpoint/step_00000001 restart/checkpoint/step_00000001.WRITING
            rm -f restart/grid_provenance restart/grid_provenance.WRITING
            exit 1
        fi
    else
        echo "[FATAL] Checkpoint interpolation 失敗 (exit=$?)"
        rm -rf restart/checkpoint/step_00000001 restart/checkpoint/step_00000001.WRITING
        rm -f restart/grid_provenance restart/grid_provenance.WRITING
        exit 1
    fi
}

# ── 決策判定 ──
_PIPELINE_CASE=0

if [ "$MODE_COLD" -eq 1 ]; then
    _PIPELINE_CASE=3

elif [ "$MODE_REGRID" -eq 1 ]; then
    _PIPELINE_CASE=2

elif [ "$HAS_CKPT" -eq 1 ]; then
    _PIPELINE_CASE=1

elif _regrid_inputs_complete; then
    _PIPELINE_CASE=2

else
    _PIPELINE_CASE=3
fi

echo ""
case "$_PIPELINE_CASE" in
    1) echo "[pipeline] Case 1 -- restart/ 有效 checkpoint, 續跑" ;;
    2) echo "[pipeline] Case 2 -- regrid interpolation pipeline" ;;
    3) echo "[pipeline] Case 3 -- cold-start" ;;
esac
echo ""

# ─────────────────────────────────────────────────────────────────────
# Case 1: restart/ 存在 → 直接使用
#   確認 J_Frohlich solver grid 存在且匹配 variables.h
#   grid_zeta_tool.py --auto 是冪等的: 參數匹配的 grid 已存在則秒回,
#   不匹配或不存在則自動重新生成
# ─────────────────────────────────────────────────────────────────────
if [ "$_PIPELINE_CASE" -eq 1 ]; then
    echo "[case-1] 確認 solver grid 匹配 variables.h (grid_zeta_tool.py --auto)..."
    if "$PY" J_Frohlich/grid_zeta_tool.py --auto; then
        git diff --quiet variables.h || { echo "[FATAL] grid_zeta --auto 改動了 variables.h，需人工複核"; exit 1; }
        echo "[case-1] Grid OK -> 使用既有 checkpoint 續跑"
    else
        echo "[FATAL] Grid generation 失敗 (J_Frohlich/grid_zeta_tool.py --auto exit=$?)"
        exit 1
    fi
fi

# ─────────────────────────────────────────────────────────────────────
# Case 2: regrid interpolation pipeline
#   (a) 解析 origin checkpoint + old/new grids
#   (b) 生成 solver grid (grid_zeta_tool.py --auto)
#   (c) 驗證 grid 維度一致性
#   (d) 執行 checkpoint interpolation
#   (e) 全座標比對: phase1 newgrid vs J_Frohlich solver grid
# ─────────────────────────────────────────────────────────────────────
if [ "$_PIPELINE_CASE" -eq 2 ]; then
    _VH_NY="$(_read_define_value NY)"
    _VH_NZ="$(_read_define_value NZ)"
    if [ -z "$_VH_NY" ] || [ -z "$_VH_NZ" ]; then
        echo "[FATAL] 無法從 variables.h 讀取 NY/NZ"
        exit 1
    fi

    # ── (a) 解析 origin checkpoint ──
    _ORIGIN_DIR=""
    if [ "$MODE_REGRID" -eq 1 ]; then
        if [ -n "$REGRID_ORIGIN_DIR" ]; then
            _ORIGIN_DIR="$(_project_abs_path "$REGRID_ORIGIN_DIR")"
            [ -s "$_ORIGIN_DIR/metadata.dat" ] || {
                echo "[FATAL] --origin-dir 缺 metadata.dat: $_ORIGIN_DIR"
                exit 1
            }
        else
            _ORIGIN_COUNT=0
            for _d in restart/step_*_origin*/ \
                      phase2_generatecheckpoint/step_*_origin*/ \
                      phase2_generatecheckpoint/oldcheckpoint_*/ \
                      phase2_generatecheckpoint/step_*/; do
                [ -s "${_d}metadata.dat" ] || continue
                _ORIGIN_DIR="${_d%/}"
                _ORIGIN_COUNT=$((_ORIGIN_COUNT + 1))
            done
            if [ "$_ORIGIN_COUNT" -eq 0 ]; then
                echo "[FATAL] --regrid-from-origin 需要唯一 origin checkpoint (含 metadata.dat)"
                echo "        搜尋路徑: restart/step_*_origin*/, phase2_generatecheckpoint/step_*_origin*/, phase2_generatecheckpoint/oldcheckpoint_*/, phase2_generatecheckpoint/step_*/"
                exit 1
            fi
            if [ "$_ORIGIN_COUNT" -gt 1 ]; then
                echo "[FATAL] 多個 origin checkpoint 存在 ($_ORIGIN_COUNT 個), 無法自動選擇"
                ls -1d restart/step_*_origin*/ phase2_generatecheckpoint/step_*_origin*/ phase2_generatecheckpoint/oldcheckpoint_*/ phase2_generatecheckpoint/step_*/ 2>/dev/null
                echo "        請移除不需要的 origin, 只保留一個; 或用 --origin-dir 明確指定"
                exit 1
            fi
        fi
    else
        _ORIGIN_DIR="$_AUTO_ORIGIN_DIR"
    fi

    # ── (a) 解析 old/new grids ──
    if [ "$MODE_REGRID" -eq 1 ]; then
        [ -z "$REGRID_OLD_GRID" ] && { REGRID_OLD_GRID="$(_discover_phase1_grid oldgrid OLD)"; echo "[case-2] Auto-found OLD grid: $REGRID_OLD_GRID"; }
        [ -z "$REGRID_NEW_GRID" ] && { REGRID_NEW_GRID="$(_derive_solver_grid_path)"; echo "[case-2] Auto-found NEW grid: $REGRID_NEW_GRID"; }
        REGRID_OLD_GRID="$(_project_abs_path "$REGRID_OLD_GRID")"
        REGRID_NEW_GRID="$(_project_abs_path "$REGRID_NEW_GRID")"
    else
        REGRID_OLD_GRID="$(_project_abs_path "$_AUTO_OLD_GRID")"
        REGRID_NEW_GRID="$(_project_abs_path "$_AUTO_NEW_GRID")"
    fi

    [ -s "$REGRID_OLD_GRID" ] || { echo "[FATAL] OLD grid 不存在或為空: $REGRID_OLD_GRID"; exit 1; }
    [ -s "$REGRID_NEW_GRID" ] || { echo "[FATAL] NEW grid 不存在或為空: $REGRID_NEW_GRID"; exit 1; }

    # ── 清除既有 checkpoint (--force-regrid) ──
    if [ "$HAS_CKPT" -eq 1 ]; then
        if [ "$MODE_FORCE_REGRID" -eq 1 ] && [ "$MODE_PREFLIGHT_ONLY" -eq 0 ]; then
            echo "[case-2] --force-regrid: 清除既有 checkpoint/provenance 後重建"
            rm -rf restart/checkpoint/
            rm -f restart/grid_provenance restart/grid_provenance.WRITING restart/checkpoint/grid_provenance
            HAS_CKPT=0
        else
            echo "[FATAL] Case 2 但 restart/checkpoint/ 已有 checkpoint"
            echo "        若確定要用 origin 重建, 請加 --force-regrid --regrid-from-origin"
            if [ "$MODE_PREFLIGHT_ONLY" -eq 1 ]; then
                echo "        (--preflight-only 不會跳過此檢查)"
            fi
            exit 1
        fi
    fi

    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║  [case-2] Regrid Interpolation Pipeline                    ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo "  Origin : $_ORIGIN_DIR"
    echo "  OLD grid: $REGRID_OLD_GRID"
    echo "  NEW grid: $REGRID_NEW_GRID"

    # ── (b) Step 1: 生成 solver grid (若缺) ──
    echo ""
    echo "[case-2] Step 1: 生成模擬用 grid (grid_zeta_tool.py --auto)..."
    if [ -s "$REGRID_NEW_GRID" ]; then
        echo "[case-2] Step 1 OK: simulation grid 已存在"
    elif "$PY" J_Frohlich/grid_zeta_tool.py --auto; then
        git diff --quiet variables.h || { echo "[FATAL] grid_zeta --auto 改動了 variables.h，需人工複核"; exit 1; }
        echo "[case-2] Step 1 OK: simulation grid ready"
    else
        echo "[FATAL] grid_zeta_tool.py --auto 失敗 (exit=$?)"
        exit 1
    fi

    # ── (c)(d) Step 2: 驗證維度 + 執行插值 ──
    echo ""
    echo "[case-2] Step 2: 驗證維度 + 執行 checkpoint interpolation..."
    _run_regrid_pipeline

    # ── (e) Step 3: 全座標比對 newgrid vs solver grid (無條件) ──
    echo ""
    echo "[case-2] Step 3: 比對 phase1 newgrid 與 J_Frohlich solver grid (全座標)..."
    _SIM_GRID="$(_derive_solver_grid_path)"
    if [ -s "$_SIM_GRID" ]; then
        if _compare_grid_dat_coords_exact "$REGRID_NEW_GRID" "$_SIM_GRID" "$_VH_NY" "$_VH_NZ"; then
            echo "[case-2] Step 3 OK: newgrid 與 solver grid 全座標一致"
            echo "        phase1: $REGRID_NEW_GRID"
            echo "        solver: $_SIM_GRID"
        else
            echo "[FATAL] phase1 newgrid 與 solver grid 座標不一致"
            echo "        phase1: $REGRID_NEW_GRID"
            echo "        solver: $_SIM_GRID"
            echo "        此不一致代表插值使用的 grid 與模擬使用的 grid 不同, 不可續跑"
            exit 1
        fi
    else
        echo "[FATAL] 找不到 variables.h 對應的 solver grid: $_SIM_GRID"
        echo "        grid_zeta_tool.py --auto 應已在 Step 1 生成此檔案"
        exit 1
    fi

    echo ""
    echo "[case-2] Regrid pipeline 完成 -> 進入 Scenario 2 續跑"
fi

# ─────────────────────────────────────────────────────────────────────
# Case 3: cold-start — 確認 solver grid 存在 (匹配 variables.h)
# ─────────────────────────────────────────────────────────────────────
if [ "$_PIPELINE_CASE" -eq 3 ]; then
    echo "[case-3] 冷啟動: 確認 solver grid 匹配 variables.h..."
    _C3_GRID="$(_derive_solver_grid_path)"
    if [ -s "$_C3_GRID" ]; then
        echo "[case-3] Grid OK (已存在): $_C3_GRID"
    elif "$PY" J_Frohlich/grid_zeta_tool.py --auto; then
        git diff --quiet variables.h || { echo "[FATAL] grid_zeta --auto 改動了 variables.h，需人工複核"; exit 1; }
        echo "[case-3] Grid OK"
    else
        echo "[FATAL] Grid generation 失敗 (J_Frohlich/grid_zeta_tool.py --auto exit=$?)"
        exit 1
    fi
fi

# ═════════════════════════════════════════════════════════════════════════
# Preflight C-0: provenance 存在但 checkpoint 不存在 → 不一致
# ═════════════════════════════════════════════════════════════════════════
if [ "$HAS_CKPT" -eq 0 ] && [ "$MODE_COLD" -eq 0 ] && [ "$MODE_REGRID" -eq 0 ] && [ -e restart/grid_provenance ]; then
    echo "[FATAL] restart/grid_provenance 存在但無有效 checkpoint"
    echo "        這是 regrid chain 的不一致狀態 (checkpoint 被刪但 provenance 殘留)"
    echo "        選擇:"
    echo "          (a) 重新插值: bash chain_code_local/run.sh --regrid-from-origin --old-grid <OLD.dat> --new-grid <NEW.dat>"
    echo "          (b) 完全重來: bash chain_code_local/run.sh --force-cold"
    echo "          (c) 手動清除 provenance 後冷啟動: rm -f restart/grid_provenance && bash chain_code_local/run.sh"
    exit 1
fi

# ═════════════════════════════════════════════════════════════════════════
# Preflight C: grid_provenance 一致性驗證
#   restart/grid_provenance 記錄本 chain 使用的 grid 身份 (session-level)
# ═════════════════════════════════════════════════════════════════════════
if [ "$HAS_CKPT" -eq 1 ] && [ "$MODE_COLD" -eq 0 ] && [ -e restart/grid_provenance ]; then
    _PROV="restart/grid_provenance"

    _prov_get() {
        awk -F= -v key="$1" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$_PROV"
    }
    _PROV_BAD=0
    _STALE=0
    _check_prov_mtime() {
        local label="$1" path="$2" saved="$3"
        if [ -z "$path" ] || [ -z "$saved" ]; then
            echo "[preflight-C] invalid provenance: missing $label path/mtime"
            _PROV_BAD=1
            return
        fi
        if [ ! -f "$path" ]; then
            echo "[preflight-C] invalid provenance: $label missing: $path"
            _PROV_BAD=1
            return
        fi
        local cur
        cur=$(stat -c %Y "$path" 2>/dev/null || true)
        if [ -z "$cur" ]; then
            echo "[preflight-C] invalid provenance: cannot stat $label: $path"
            _PROV_BAD=1
            return
        fi
        if [ "$cur" != "$saved" ]; then
            echo "[preflight-C] $label 已變更 (saved=$saved current=$cur)"
            _STALE=1
        fi
    }

    _SAVED_ORIGIN="$(_prov_get origin)"
    _SAVED_ORIGIN_MT="$(_prov_get origin_metadata_mtime)"
    _SAVED_VH="$(_prov_get variables_h)"
    _SAVED_VH_MT="$(_prov_get variables_h_mtime)"
    _SAVED_NEW_GRID="$(_prov_get new_grid)"
    _SAVED_NEW_MT="$(_prov_get new_grid_mtime)"
    _SAVED_OLD_GRID="$(_prov_get old_grid)"
    _SAVED_OLD_MT="$(_prov_get old_grid_mtime)"

    if [ -z "$_SAVED_ORIGIN" ]; then
        echo "[preflight-C] invalid provenance: missing origin path"
        _PROV_BAD=1
    else
        _check_prov_mtime "origin metadata" "$_SAVED_ORIGIN/metadata.dat" "$_SAVED_ORIGIN_MT"
    fi
    _check_prov_mtime "variables.h" "$_SAVED_VH" "$_SAVED_VH_MT"
    _check_prov_mtime "NEW grid" "$_SAVED_NEW_GRID" "$_SAVED_NEW_MT"
    _check_prov_mtime "OLD grid" "$_SAVED_OLD_GRID" "$_SAVED_OLD_MT"

    if [ "$_PROV_BAD" -eq 1 ] || [ "$_STALE" -eq 1 ]; then
        echo ""
        if [ "$_PROV_BAD" -eq 1 ]; then
            echo "[FATAL] restart/grid_provenance 格式不完整或指向不存在的檔案"
        else
            echo "[FATAL] restart/grid_provenance 與當前 grid/variables/origin 不一致"
        fi
        echo "        checkpoint 是在不同或不可驗證的 grid 設定下產生的, 不可續跑"
        echo "        修正步驟:"
        echo "          rm -rf restart/checkpoint/"
        echo "          rm -f  restart/grid_provenance"
        echo "          bash chain_code_local/run.sh --regrid-from-origin --old-grid <OLD.dat> --new-grid <NEW.dat>"
        exit 1
    fi
fi

# ═════════════════════════════════════════════════════════════════════════
# 編譯 a.out (Scenario 1 / Scenario 2 缺 binary / --rebuild)
# ═════════════════════════════════════════════════════════════════════════
if [ "$HAS_BIN" -eq 0 ] || [ "$MODE_REBUILD" -eq 1 ]; then
    if [ "$MODE_REBUILD" -eq 1 ]; then
        echo "[build] --rebuild 指定, 強制重編 a.out (本地 V100, sm_70)..."
    else
        echo "[build] a.out 缺失, 呼叫 $BUILD_SCRIPT 編譯..."
    fi
    bash "$BUILD_SCRIPT"
    if [ ! -x ./a.out ]; then
        echo "[FATAL] 編譯失敗, a.out 未產出."
        exit 1
    fi
    HAS_BIN=1
fi

# ── [ARCH-GUARD] a.out 必須是 x86-64 ELF (防誤用 aarch64 binary) ──
if ! file a.out 2>/dev/null | grep -q 'x86-64'; then
    echo "[ARCH-GUARD] ⚠ FATAL: a.out 不是 x86-64 ELF (本地 V100 需 x86-64)"
    echo "             file a.out: $(file a.out 2>/dev/null)"
    echo "             請重編: bash chain_code_local/build_local.sh"
    exit 1
fi

if [ -f restart/STOP_CHAIN ]; then
    echo "[cleanup] 移除舊 restart/STOP_CHAIN sentinel"
    rm -f restart/STOP_CHAIN
fi

# ═════════════════════════════════════════════════════════════════════════
# --preflight-only / --no-submit 退出點
#   NCHC 在 build 之前 (1152) 就早退 → --preflight-only --rebuild 不會編譯.
#   本地把退出點下移到 build+ELF 檢查之後、cfdq 投遞之前, 故
#   --preflight-only --rebuild 也會真的編譯出 a.out 才停; 備料齊全, 不投遞.
# ═════════════════════════════════════════════════════════════════════════
if [ "$MODE_PREFLIGHT_ONLY" -eq 1 ]; then
    echo "[preflight-only] OK: 前置 grid/regrid/provenance/編譯 備料完成, 不投遞."
    exit 0
fi
if [ "$MODE_NO_SUBMIT" -eq 1 ]; then
    echo "[OK] 備料完成, --no-submit 不投遞."
    exit 0
fi

# ═════════════════════════════════════════════════════════════════════════
# 投遞 (cfdq; 取代 NCHC 的 exec sbatch)
#   投遞當下 cwd 必須 = 專案根 (腳本開頭已 cd PROJECT_ROOT), cfdq 才把 job cwd 記成本專案.
#   cfdq --chain + hill_local_chain.sh 自理續鏈; run.sh 絕不直接呼叫 schedule/launch/daemon.
#   註: --exclusive 是 cosmetic no-op (cfdq 只寫進不被讀的 mode 欄位); 真正整台獨佔
#       由 is_fully_free 全節點空閘 + node_ok_for 保證, 與此旗標無關.
# ═════════════════════════════════════════════════════════════════════════
echo ""
echo "[submit] cfdq add --np $MODE_NP --model V100 --exclusive --chain --name $MODE_NAME"
cfdq add --np "$MODE_NP" --model V100 --exclusive --chain --name "$MODE_NAME" \
     -- bash chain_code_local/hill_local_chain.sh

# ── daemon 存活提醒 (對齊 cfdq 權威存活窗 INTERVAL*3+30=90s, 非硬寫 -mmin) ──
A="$HOME/.cfdq/daemon.lock/alive"; ITV="${CFDQ_INTERVAL:-20}"
if ! { [ -e "$A" ] && [ $(( $(date +%s) - $(stat -c %Y "$A") )) -le $(( ITV*3 + 30 )) ]; }; then
    echo "[note] 未偵測到活著的 cfdq daemon → 去母機 tmux 執行: cfdq daemon"
fi
