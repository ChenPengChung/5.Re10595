#!/bin/bash
# ==============================================================================
# run.sh — GILBM Periodic Hill 統一啟動入口 (cluster-aware dispatcher)
#
# 一個指令涵蓋所有續跑情境。自動偵測環境並作正確處置。
#
# 用法:
#   ./run.sh                  自動偵測情境 + 叢集,並投遞 (最常用)
#   ./run.sh --status         只看狀態,不投遞
#   ./run.sh --rebuild        強制重編 a.out 再投
#   ./run.sh --force-cold     清空所有 state / history 後從頭跑 (需確認)
#   ./run.sh --regrid-from-origin [--old-grid <OLD.dat>] [--new-grid <NEW.dat>]
#                             從唯一 origin 轉換到新 grid checkpoint; grid 可自動掃描 phase1_generategrid/
#   ./run.sh                  若 phase1_generategrid/ + phase2_generatecheckpoint/
#                             具備唯一 old/new grid + origin,會自動跑 regrid pipeline 再投遞
#   ./run.sh --force-regrid   搭配 --regrid-from-origin, 先清掉既有 checkpoint 再重建
#   ./run.sh --preflight-only  只跑 grid/regrid/provenance 前置檢查, 不編譯、不投遞
#   ./run.sh --h200           強制使用 H200 變體 (x86_64, sm_90, dev partition)
#   ./run.sh --gb200          強制使用 GB200 變體 (aarch64, sm_100)
#   ./run.sh --no-queue-check 關閉 partition 擁塞檢查 (CI/自動化用)
#   ./run.sh -h | --help      顯示此使用說明
#
# Partition 智能查詢 (方案一, 2026-04-21):
#   每次投遞前會用 sinfo/squeue 查詢當前叢集 partition 的:
#     - idle / alloc / down 節點數
#     - 使用者自己的 pending / running jobs
#     - 全 partition 的 pending queue 深度
#   若 partition 擁塞 (idle=0 且 pending>=5), 會印 advisory 提示可否切另一邊,
#   但不阻擋投遞 (純資訊). 用 --no-queue-check 可跳過此查詢.
#
# Cluster 偵測:
#   - 預設: uname -m → aarch64 ⇒ GB200 / x86_64 ⇒ H200
#   - 兩種變體的檔案嚴格分離 (「區分功能正確性」),不互相干擾:
#       GB200: build_and_submit.sh.GB200 + jobscript_chain.slurm.GB200
#       H200 : build_and_submit.sh.H200  + jobscript_chain.slurm.H200
#   - 可用 --h200 / --gb200 override (例如在 login-node 預編譯另一叢集的 a.out)
#
# 情境對照 (皆用 ./run.sh):
#   情境 1 冷啟動 (全新)       → 自動編譯 + 空 state + sbatch
#   情境 2 只有 checkpoint     → 自動偵測 + 備份 history + 建 Round 2 + sbatch
#   情境 3A 鏈正常在跑         → 偵測 queue 有 job,報告後退出 (不重投)
#   情境 3B 鏈斷了             → 保留 state,直接 sbatch 接續
#
# 安全措施:
#   - flock restart/.lock 防止兩個 run.sh 同時執行雙投
#   - 情境 2 前自動備份 checkrho.dat / Ustar_Force_record.dat
#   - --force-cold 必須人工確認
#   - 若 checkpoint 全數無效,FATAL,引導使用者用 --force-cold
# ==============================================================================

set -eo pipefail   # 不使用 -u: hpcx-init.sh 會踩 unbound variable

# ── [方案 A path discipline] 自我定位 + 鎖 cwd 到 PROJECT_ROOT ────────────────
# 本 script 位於 chain_code/, 但許多相對路徑 (restart/, a.out, result/, .run.lock)
# 都在 PROJECT_ROOT. 其他 chain_code/ 內的同伴 script 則透過 $CHAIN_DIR 絕對路徑呼叫.
_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || realpath "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
CHAIN_DIR="$(cd "$(dirname "$_SELF")" && pwd)"
PROJECT_ROOT="$(cd "$CHAIN_DIR/.." && pwd)"
cd "$PROJECT_ROOT" || { echo "[run.sh] FATAL: cannot cd to PROJECT_ROOT=$PROJECT_ROOT" >&2; exit 1; }

MODE_COLD=0
MODE_REBUILD=0
MODE_STATUS=0
MODE_NO_QCHECK=0   # 1 = 跳過 partition 擁塞查詢 (CI/自動化)
MODE_CLUSTER=""    # "" = auto-detect; "H200" or "GB200" = user override
MODE_REGRID=0
MODE_FORCE_REGRID=0
MODE_PREFLIGHT_ONLY=0
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
        --h200|--H200)     MODE_CLUSTER="H200" ;;
        --gb200|--GB200)   MODE_CLUSTER="GB200" ;;
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
if [ "$MODE_COLD" -eq 1 ] && [ "$MODE_PREFLIGHT_ONLY" -eq 1 ]; then
    echo "[run.sh] FATAL: --force-cold 與 --preflight-only 不能同時使用"
    echo "        --force-cold 會刪除 state, 與 preflight 的唯讀語義矛盾"
    exit 2
fi

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

_derive_solver_grid_path() {
    local ny="$1" nz="$2"
    local grid_dir grid_ref stretch_a ref_stem fname
    grid_dir="$(_read_string_define_value GRID_DAT_DIR)"
    grid_ref="$(_read_string_define_value GRID_DAT_REF)"
    stretch_a="$(_read_define_value STRETCH_A)"
    if [ -z "$grid_dir" ] || [ -z "$grid_ref" ] || [ -z "$stretch_a" ]; then
        echo "[FATAL] 無法從 variables.h 推導 solver grid path (GRID_DAT_DIR/GRID_DAT_REF/STRETCH_A)" >&2
        exit 1
    fi
    ref_stem="${grid_ref%.*}"
    stretch_a="$(awk -v a="$stretch_a" 'BEGIN { printf "%.6f", a + 0.0 }')"
    fname="adaptive_${ref_stem}_I${ny}_J${nz}_s${stretch_a}.dat"
    _project_abs_path "${grid_dir}/${fname}"
}

_grid_filename_stretch_a() {
    local base
    base="$(basename "$1")"
    if [[ "$base" =~ _s([0-9]+([.][0-9]+)?)\.dat$ ]]; then
        printf '%s\n' "${BASH_REMATCH[1]}"
    fi
}

_check_grid_filename_stretch_a() {
    local label="$1" path="$2" expected="$3" actual expected_fmt actual_fmt
    if [ -z "$expected" ]; then
        echo "[FATAL] 無法從 variables.h 讀取 STRETCH_A, 無法驗證 $label 檔名" >&2
        exit 1
    fi
    actual="$(_grid_filename_stretch_a "$path")"
    if [ -z "$actual" ]; then
        echo "[FATAL] $label 檔名缺少 _s{STRETCH_A}.dat tag: $path" >&2
        exit 1
    fi
    expected_fmt="$(awk -v a="$expected" 'BEGIN { printf "%.6f", a + 0.0 }')"
    actual_fmt="$(awk -v a="$actual" 'BEGIN { printf "%.6f", a + 0.0 }')"
    if [ "$actual_fmt" != "$expected_fmt" ]; then
        echo "[FATAL] $label 檔名 STRETCH_A 不一致" >&2
        echo "        filename: s=$actual_fmt" >&2
        echo "        variables.h STRETCH_A=$expected_fmt" >&2
        echo "        path: $path" >&2
        exit 1
    fi
    echo "[case-2] Step 3 OK: $label 檔名 STRETCH_A s=$actual_fmt matches variables.h"
}

_compare_grid_dat_coords_exact() {
    local phase_grid="$1" solver_grid="$2" expected_i="$3" expected_j="$4"
    python3 - "$phase_grid" "$solver_grid" "$expected_i" "$expected_j" <<'PY'
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

# ═════════════════════════════════════════════════════════════════════════
# Cluster 自動偵測 (partition-smart-ETA → idle-count → uname -m fallback) + override
# 優先順序:
#   [1] --h200 / --gb200 override (使用者明示)
#   [2] partition-smart-ETA: 雙 arch binary 都已預建 + sbatch 可用
#       → 用 `sbatch --test-only` 問 SLURM 兩邊 partition 真正的預期開始時間 (ETA),
#         選較早能跑的那個 (60s 內視為平手, 比 idle 節點數多的贏)
#   [3] partition-smart-IDLE (fallback): --test-only 失敗時改比 sinfo idle 節點數
#   [4] uname -m fallback: 單 arch 或 sinfo 不可用時
# 同時驗證對應檔案存在,不存在就明確報錯,不靜默退回他變體。
# ═════════════════════════════════════════════════════════════════════════
CLUSTER=""
CLUSTER_SRC=""

# [1] 使用者明示 override (最高優先)
if [ -n "$MODE_CLUSTER" ]; then
    CLUSTER="$MODE_CLUSTER"
    CLUSTER_SRC="override(--${MODE_CLUSTER,,})"
fi

# [2] Partition-smart-ETA: 掃描所有候選 partition, 選 ETA 最早的
# 候選清單與 dispatcher (submit_dispatcher.sh) 一致: GB200:gb200, gb200-full,
#   gb200-rack1, gb200-rack2, gb200-dev; H200 自由切換集 {8gpus,16gpus,32gpus}@jp=32 (每帳號 cap 皆=32)
# 每個候選用 sbatch --test-only --partition=<part> --time=<walltime> 查 ETA.
# 需要對應 arch 的 a.out.{CLUSTER} 存在才會列入.
# [LOCK_JP_PARTITION] 嚴格鎖定中: 整條鏈鎖死 H200@<pin>@jp=32 (Codex 8/9 全路徑一致)。
#   - 顯式 --gb200 與鎖衝突 → 拒絕 (此檢查在 MODE_CLUSTER override 之後, 故能攔截顯式覆寫)。
#   - 否則強制 CLUSTER=H200 + 走 h200_partition pin, 跳過 smart-ETA、不覆寫 pin。
#   - pin 缺失/空 → 還原為鎖定 partition 16gpus, 不讓 smart-ETA 介入 (pin-missing strict-safe)。
if [ -e restart/LOCK_JP_PARTITION ]; then
    if [ "$CLUSTER" = "GB200" ]; then
        echo "[run.sh][LOCK] FATAL: LOCK_JP_PARTITION 鎖定 H200@jp=32; 與顯式 --gb200 衝突。" >&2
        echo "             要切 GB200 請先解鎖: rm -f restart/LOCK_JP_PARTITION" >&2
        exit 2
    fi
    [ -s restart/h200_partition ] || { mkdir -p restart; echo 16gpus > restart/h200_partition; }
    CLUSTER="H200"
    CLUSTER_SRC="LOCK_JP_PARTITION(pin=$(tr -d '[:space:]' < restart/h200_partition))"
fi
if [ -z "$CLUSTER" ] \
   && command -v sbatch >/dev/null 2>&1 \
   && command -v sinfo  >/dev/null 2>&1; then

    # 載入 partition_lib (walltime 查詢)
    if [ -f "$CHAIN_DIR/tools/partition_lib.sh" ]; then
        . "$CHAIN_DIR/tools/partition_lib.sh"
    fi

    # 候選清單: ARCH:partition (順序 = 平手時的優先級, 與 dispatcher 一致)
    # [2026-06-05] 與 dispatcher 一致: H200 自由切換集 {8gpus,16gpus,32gpus}@jp=32 (每帳號 cap 皆=32)
    _RUNSH_CANDIDATES="${PARTITION_CANDIDATES:-GB200:gb200 GB200:gb200-full GB200:gb200-rack1 GB200:gb200-rack2 GB200:gb200-dev H200:8gpus H200:16gpus H200:32gpus}"
    _RUNSH_TIE_TOL=30   # ETA 差距 <= 30s 視為平手, 用候選順序先到先選
    # [PS-4] 讀 jp 供 GPU-cap 前過濾 (與 dispatcher pick_cluster 一致, 避免 jp>cap 候選永久 PENDING)
    _RUNSH_JP="$(awk '/^#define[[:space:]]+jp[[:space:]]/{print $3; exit}' variables.h 2>/dev/null)"; _RUNSH_JP="${_RUNSH_JP:-0}"

    _eta_epoch() {
        local js="$1" part="$2" out eta_str wt="" time_arg=""
        # [PS-3] 統一查 GB200+H200 walltime (原只查 gb200_*, H200 候選得空 --time →
        #        繼承 header 2d > dev 1h MaxTime → --test-only 被拒 → H200:dev 永遠被 skip)
        if [ -n "$part" ] && type partition_walltime >/dev/null 2>&1; then
            wt="$(partition_walltime "$part")"
        fi
        [ -n "$wt" ] && time_arg="--time=$wt"
        if [ -n "$part" ]; then
            out=$(sbatch --test-only --partition="$part" $time_arg "$js" 2>&1 || true)
        else
            out=$(sbatch --test-only $time_arg "$js" 2>&1 || true)
        fi
        if   echo "$out" | grep -qE "to start at[[:space:]]+[0-9]{4}-"; then
            eta_str=$(echo "$out" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+' | head -1)
            date -d "$eta_str" +%s 2>/dev/null || echo -1
        elif echo "$out" | grep -qE "allocation .*can be allocated|to start immediately|to start now"; then
            date +%s
        else
            echo -1
        fi
    }

    _fmt_wait() {
        local w=$1
        if   [ $w -lt 0 ];     then echo "unknown"
        elif [ $w -le 60 ];    then echo "now"
        elif [ $w -lt 3600 ];  then echo "~$((w/60))min"
        else                        echo "~$((w/3600))h$((w%3600/60))m"
        fi
    }

    _BEST_TARGET=""
    _BEST_EPOCH=0
    _BEST_SET=0
    _ETA_LOG=""

    for _entry in $_RUNSH_CANDIDATES; do
        _c="${_entry%%:*}"
        _part="${_entry#*:}"
        [ -z "$_c" ] || [ -z "$_part" ] || [ "$_c" = "$_part" ] && continue

        # 需要對應 binary
        [ -s "a.out.${_c}" ] || continue

        _js="$CHAIN_DIR/jobscript_chain.slurm.${_c}"
        [ -f "$_js" ] || continue

        # [PS-4] GPU-cap 前過濾: jp > 該 partition 每帳號上限 → 必永久 PENDING, 跳過
        if type partition_gpu_cap_per_account >/dev/null 2>&1; then
            _cap="$(partition_gpu_cap_per_account "$_part")"
            if [ "${_RUNSH_JP:-0}" -gt "${_cap:-100000}" ]; then
                _ETA_LOG="${_ETA_LOG}    ${_c}@${_part}: jp=${_RUNSH_JP} > cap=${_cap} (skip: MaxGRESPerAccount)\n"
                continue
            fi
        fi

        _eta=$(_eta_epoch "$_js" "$_part")
        if [ "$_eta" -lt 0 ]; then
            _ETA_LOG="${_ETA_LOG}    ${_c}@${_part}: ETA unknown (skip)\n"
            continue
        fi
        _now=$(date +%s)
        _wait=$((_eta - _now))
        [ $_wait -lt 0 ] && _wait=0
        _ETA_LOG="${_ETA_LOG}    ${_c}@${_part}: wait $(_fmt_wait $_wait)\n"

        if [ "$_BEST_SET" -eq 0 ]; then
            _BEST_TARGET="${_c}@${_part}"
            _BEST_EPOCH="$_eta"
            _BEST_SET=1
        else
            _delta=$((_BEST_EPOCH - _eta))
            if [ "$_delta" -gt "$_RUNSH_TIE_TOL" ]; then
                _BEST_TARGET="${_c}@${_part}"
                _BEST_EPOCH="$_eta"
            fi
        fi
    done

    if [ "$_BEST_SET" -eq 1 ] && [ -n "$_BEST_TARGET" ]; then
        _BEST_C="${_BEST_TARGET%%@*}"
        _BEST_P="${_BEST_TARGET#*@}"
        CLUSTER="$_BEST_C"
        CLUSTER_SRC="partition-smart-ETA(best=${_BEST_TARGET})"

        # 自動寫入 partition override (供 jobscript chain 續投使用)
        # [PS-2] 依 arch 選對 pin 檔: H200 投遞讀 restart/h200_partition, GB200 讀 restart/gb200_partition.
        #        原本一律寫 gb200_partition → H200 override 寫錯檔、H200 投遞讀不到 → 落回 header.
        _js_default_part="$(awk -F= '/^#SBATCH[[:space:]]+--partition=/{gsub(/^[[:space:]]+|[[:space:]]+$/,"",$2); print $2; exit}' "$CHAIN_DIR/jobscript_chain.slurm.${CLUSTER}")"
        _PINFILE="restart/gb200_partition"; _OTHERPIN="restart/h200_partition"
        [ "$CLUSTER" = "H200" ] && { _PINFILE="restart/h200_partition"; _OTHERPIN="restart/gb200_partition"; }
        mkdir -p restart/
        if [ "$_BEST_P" != "$_js_default_part" ]; then
            echo "$_BEST_P" > "$_PINFILE"
        else
            rm -f "$_PINFILE" 2>/dev/null
        fi
        rm -f "$_OTHERPIN" 2>/dev/null   # 清掉另一 arch 的舊 pin, 避免跨 arch 切換時殘留誤導
    fi
fi

# [3] Fallback: uname -m
if [ -z "$CLUSTER" ]; then
    case "$(uname -m)" in
        aarch64|arm64) CLUSTER="GB200"; CLUSTER_SRC="uname=aarch64" ;;
        x86_64|amd64)  CLUSTER="H200";  CLUSTER_SRC="uname=x86_64"  ;;
        *)
            echo "[run.sh] FATAL: 未知的 uname -m = $(uname -m)"
            echo "         請明確指定 --h200 或 --gb200"
            exit 3 ;;
    esac
fi

JOBSCRIPT="$CHAIN_DIR/jobscript_chain.slurm.${CLUSTER}"
BUILD_SCRIPT="$CHAIN_DIR/build_and_submit.sh.${CLUSTER}"

if [ ! -f "$JOBSCRIPT" ]; then
    echo "[run.sh] FATAL: 偵測到 $CLUSTER ($CLUSTER_SRC),但缺少 $JOBSCRIPT"
    echo "         可用變體: $(ls "$CHAIN_DIR"/jobscript_chain.slurm.* 2>/dev/null | tr '\n' ' ')"
    exit 3
fi
if [ ! -f "$BUILD_SCRIPT" ]; then
    echo "[run.sh] FATAL: 偵測到 $CLUSTER ($CLUSTER_SRC),但缺少 $BUILD_SCRIPT"
    exit 3
fi

mkdir -p restart/

# ═════════════════════════════════════════════════════════════════════════
# flock: 防止並發 run.sh 同時執行 (兩個 terminal 同時 ./run.sh → 雙投 bug)
# ═════════════════════════════════════════════════════════════════════════
exec 200>.run.lock
if ! flock -n 200; then
    echo "[run.sh] 另一個 run.sh 正在執行 (.run.lock 被佔用)"
    echo "         若確定沒有其他 run.sh,可移除 lock: rm .run.lock"
    exit 4
fi

# ═════════════════════════════════════════════════════════════════════════
# DISPATCHER 模式 sentinel check (防呆)
# ═════════════════════════════════════════════════════════════════════════
# 若 DISPATCHER_ACTIVE 存在，代表 dispatcher 正在接管續投。
# 使用者若直接 ./run.sh 會造成雙投 (dispatcher + 使用者手動同時投)。
# dispatcher 自己呼叫 run.sh 時會設 RUNSH_DISPATCHER_BYPASS=1 繞過此檢查。
# ─────────────────────────────────────────────────────────────────────────
if [ -f DISPATCHER_ACTIVE ] && [ "${RUNSH_DISPATCHER_BYPASS:-0}" != "1" ]; then
    echo "[run.sh] ⚠ 偵測到 DISPATCHER_ACTIVE — dispatcher 正在接管續投."
    echo "         若要手動投一輪,請先 ./dispatcher_stop.sh"
    echo "         若要看 dispatcher 狀態,請執行 ./dispatcher_status.sh"
    exit 5
fi

# ═════════════════════════════════════════════════════════════════════════
# [SINGLE-HEAD / MUTEX LAYER 3 — run.sh pre-flight]
# ─────────────────────────────────────────────────────────────────────────
# 中心準則 (Single-Head per Folder):
#   每格資料夾在 queue 內最多只能有 1 個 job. HEAD.lockdir 是 single source of truth.
#   若 HEAD.lockdir 被活 owner 佔住, 拒絕投遞; 若 owner 已死, 自動清理 stale entry.
#   (向後相容: 同時檢查 legacy RUNNING.lockdir / chain_jobid.)
# ═════════════════════════════════════════════════════════════════════════

# 先載入 head_lock_lib (提供 _head_squeue_state 等函式)
if [ -f "$CHAIN_DIR/tools/head_lock_lib.sh" ]; then
    # shellcheck disable=SC1091
    . "$CHAIN_DIR/tools/head_lock_lib.sh"
fi

# ── Primary 檢查: restart/HEAD.lockdir ──
if [ -d "${HEAD_LOCK_DIR:-restart/HEAD.lockdir}" ]; then
    HD="${HEAD_LOCK_DIR:-restart/HEAD.lockdir}"
    # 尾端 "|| true" 必要 — set -eo pipefail 下 grep/squeue 若無 match 回 1,
    # X="$(pipeline)" 會觸發 set -e 靜默退出 rc=1 (無 banner, 最難 debug)
    HEAD_STATE="$(grep '^state=' "$HD/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]' || true)"
    HEAD_JID="$(grep   '^jobid=' "$HD/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]' || true)"
    HEAD_EPOCH="$(grep '^submitted_at_epoch=' "$HD/owner" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]' || true)"
    HEAD_LIVE=""
    if [[ "$HEAD_JID" =~ ^[0-9]+$ ]]; then
        HEAD_LIVE="$(squeue -h -j "$HEAD_JID" -o '%T' 2>/dev/null | tr -d '[:space:]' || true)"
    fi
    NOW_EPOCH="$(date +%s)"
    AGE_S=$(( NOW_EPOCH - ${HEAD_EPOCH:-0} ))

    case "$HEAD_STATE" in
        SUBMITTING)
            if [ "$AGE_S" -gt "${HEAD_STALE_TIMEOUT:-30}" ]; then
                echo "[run.sh] 偵測到 stale HEAD.lockdir (state=SUBMITTING age=${AGE_S}s > ${HEAD_STALE_TIMEOUT:-30}s), 自動清理"
                rm -rf "$HD"
            else
                echo "[run.sh] ⚠ HEAD.lockdir 正被 submitter 鎖住 (state=SUBMITTING age=${AGE_S}s)"
                echo "         有人正在投遞,拒絕再投. 請稍後再試."
                exit 6
            fi
            ;;
        PENDING|RUNNING)
            case "$HEAD_LIVE" in
                PENDING|RUNNING|CONFIGURING|COMPLETING|RESIZING|SUSPENDED)
                    echo "[run.sh] ⚠ HEAD.lockdir 被 jobid=$HEAD_JID ($HEAD_STATE, squeue=$HEAD_LIVE) 持有"
                    echo "         拒絕投遞以維持「每格資料夾單一 head」準則"
                    echo "         若 $HEAD_JID 是本專案 orphan: ./run job-guard scancel $HEAD_JID"
                    echo "         切勿直接 scancel 未驗證的 jobid；避免干預其他專案 job"
                    exit 6
                    ;;
                "")
                    echo "[run.sh] 偵測到 stale HEAD.lockdir (jobid=$HEAD_JID 已不在 squeue), 自動清理"
                    rm -rf "$HD"
                    ;;
            esac
            ;;
        *)
            if [ "$AGE_S" -gt "${HEAD_STALE_TIMEOUT:-30}" ]; then
                echo "[run.sh] 偵測到 unknown-state HEAD.lockdir (state=$HEAD_STATE age=${AGE_S}s), 自動清理"
                rm -rf "$HD"
            fi
            ;;
    esac
fi

# ── Legacy 相容檢查: 舊 RUNNING.lockdir ──
if [ -d restart/RUNNING.lockdir ]; then
    # 尾端 "|| true" 必要 (同 HEAD.lockdir 區段解釋)
    LOCK_OWNER="$(grep '^jobid=' restart/RUNNING.lockdir/owner 2>/dev/null | cut -d= -f2 | tr -d '[:space:]' || true)"
    LOCK_STATE="$(squeue -h -j "$LOCK_OWNER" -o '%T' 2>/dev/null | tr -d '[:space:]' || true)"
    case "$LOCK_STATE" in
        PENDING|RUNNING|CONFIGURING|COMPLETING)
            echo "[run.sh] ⚠ legacy restart/RUNNING.lockdir 被 jobid=$LOCK_OWNER ($LOCK_STATE) 持有"
            echo "         拒絕投遞以維持單一寫入者準則"
            echo "         若 $LOCK_OWNER 是本專案 orphan: ./run job-guard scancel $LOCK_OWNER"
            echo "         切勿直接 scancel 未驗證的 jobid；避免干預其他專案 job"
            exit 6
            ;;
        "")
            echo "[run.sh] 偵測到 stale legacy RUNNING.lockdir (owner=$LOCK_OWNER 已死), 自動清理"
            rm -rf restart/RUNNING.lockdir
            ;;
    esac
fi

# ── Legacy 相容檢查: 舊 chain_jobid ──
if [ -f restart/chain_jobid ]; then
    # 尾端 "|| true" 必要 — chain_jobid 若是 stale ID, squeue 回 1,
    # set -eo pipefail 下 X="$(pipeline)" 會觸發靜默退出 rc=1 (實戰症狀根因)
    CUR_CHAIN_ID="$(cat restart/chain_jobid 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ "$CUR_CHAIN_ID" =~ ^[0-9]+$ ]]; then
        CUR_CHAIN_STATE="$(squeue -h -j "$CUR_CHAIN_ID" -o '%T' 2>/dev/null | tr -d '[:space:]' || true)"
        case "$CUR_CHAIN_STATE" in
            PENDING|RUNNING|CONFIGURING|COMPLETING)
                echo "[run.sh] ⚠ chain_jobid=$CUR_CHAIN_ID 仍 active ($CUR_CHAIN_STATE)"
                echo "         拒絕再投以免並發寫入 restart/"
                echo "         若要接手: 等本輪結束,或 ./run job-guard scancel $CUR_CHAIN_ID"
                exit 7
                ;;
        esac
    fi
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

HAS_STATE=0
if [ -f restart/chain_count ] && [ -f restart/chain_jobid ]; then
    HAS_STATE=1
fi

JOB_NAME=$(awk -F= '/^#SBATCH[[:space:]]+--job-name=/{gsub(/^[[:space:]]+|[[:space:]]+$/,"",$2); print $2; exit}' "$JOBSCRIPT" 2>/dev/null)
JOB_NAME="${JOB_NAME:-GILBM_PH}"

# ═════════════════════════════════════════════════════════════════════════
# [方案一 2026-04-21] Partition 智能查詢
# ═════════════════════════════════════════════════════════════════════════
PARTITION=""
PARTITION_SRC=""
if [ -f "$CHAIN_DIR/tools/partition_lib.sh" ]; then
    . "$CHAIN_DIR/tools/partition_lib.sh"
    PARTITION="$(gb200_active_partition 2>/dev/null || true)"
    [ -n "$PARTITION" ] && PARTITION_SRC="override(restart/gb200_partition)"
fi
if [ -z "$PARTITION" ]; then
    PARTITION=$(awk -F= '/^#SBATCH[[:space:]]+--partition=/{gsub(/^[[:space:]]+|[[:space:]]+$/,"",$2); print $2; exit}' "$JOBSCRIPT" 2>/dev/null)
    PARTITION_SRC="jobscript"
fi
PARTITION="${PARTITION:-unknown}"

PART_IDLE="?"
PART_ALLOC="?"
PART_DOWN="?"
PART_PEND_MINE="?"
PART_RUN_MINE="?"
PART_PEND_ALL="?"
PART_CONGESTED=0

query_partition_status() {
    [ "$MODE_NO_QCHECK" -eq 1 ] && return 0
    [ "$PARTITION" = "unknown" ] && return 0
    if ! command -v sinfo >/dev/null 2>&1; then
        return 0
    fi
    local _sum_state
    _sum_state() {
        sinfo -h -p "$PARTITION" -t "$1" -o "%D" 2>/dev/null \
            | awk '{s+=$1} END{print s+0}'
    }
    PART_IDLE=$(_sum_state idle)
    PART_ALLOC=$(_sum_state alloc)
    PART_DOWN=$(_sum_state 'down,drain,fail,drng' 2>/dev/null)
    if [ -z "$PART_DOWN" ] || [ "$PART_DOWN" = "0" ]; then
        local _d1 _d2 _d3 _d4
        _d1=$(_sum_state down)
        _d2=$(_sum_state drain)
        _d3=$(_sum_state fail 2>/dev/null)
        _d4=$(_sum_state drng 2>/dev/null)
        PART_DOWN=$((_d1 + _d2 + ${_d3:-0} + ${_d4:-0}))
    fi
    PART_PEND_MINE=$(squeue -h -u "$USER" -p "$PARTITION" -t PD -o "%i" 2>/dev/null | grep -c . || true)
    PART_RUN_MINE=$(squeue -h -u "$USER" -p "$PARTITION" -t R  -o "%i" 2>/dev/null | grep -c . || true)
    PART_PEND_ALL=$(squeue -h -p "$PARTITION" -t PD -o "%i" 2>/dev/null | grep -c . || true)
    if [ "${PART_IDLE:-0}" -eq 0 ] 2>/dev/null && [ "${PART_PEND_ALL:-0}" -ge 5 ] 2>/dev/null; then
        PART_CONGESTED=1
    fi
}

query_partition_status

QUEUE_JOBS=$(squeue -u "$USER" -h -o '%i %j %T %M %R' 2>/dev/null \
             | grep -E "[[:space:]]${JOB_NAME}([[:space:]]|$)" || true)

# ═════════════════════════════════════════════════════════════════════════
# 列印狀態 banner
# ═════════════════════════════════════════════════════════════════════════
CC_DISPLAY="(none)"
[ "$HAS_STATE" -eq 1 ] && CC_DISPLAY="count=$(cat restart/chain_count) jobid=$(cat restart/chain_jobid)"

echo "════════════════════════════════════════════════════════════════"
echo " run.sh 狀態偵測 @ $(date '+%F %T')"
echo "   pwd          : $(pwd)"
echo "   cluster      : $CLUSTER   ($CLUSTER_SRC)"
if [ -n "${_ETA_LOG:-}" ]; then
    echo "   ETA compare  :"
    printf '%b' "$_ETA_LOG" | sed 's/^/   /'
fi
echo "   partition    : $PARTITION   ($PARTITION_SRC)"
echo "   jobscript    : $JOBSCRIPT"
echo "   build script : $BUILD_SCRIPT"
echo "   a.out        : $([ "$HAS_BIN"   -eq 1 ] && echo 'YES' || echo 'NO')"
echo "   checkpoint   : $([ "$HAS_CKPT"  -eq 1 ] && echo 'YES (restart/checkpoint/)' || echo 'NO')"
echo "   chain state  : $CC_DISPLAY"
echo "   queue jobs   :"
if [ -n "$QUEUE_JOBS" ]; then
    echo "$QUEUE_JOBS" | sed 's/^/      /'
else
    echo "      (無)"
fi
if [ "$MODE_NO_QCHECK" -eq 1 ]; then
    echo "   partition    : (--no-queue-check, 已跳過 sinfo/squeue 查詢)"
elif [ "$PART_IDLE" = "?" ]; then
    echo "   partition    : sinfo 不可用 (本機開發環境?)"
else
    echo "   partition $PARTITION 狀態:"
    echo "      idle nodes   : $PART_IDLE"
    echo "      alloc nodes  : $PART_ALLOC"
    echo "      down/drain   : $PART_DOWN"
    echo "      我的 pending : $PART_PEND_MINE    running: $PART_RUN_MINE"
    echo "      全 pending   : $PART_PEND_ALL"
    if [ "$PART_CONGESTED" -eq 1 ]; then
        OTHER="H200"; OTHER_ARCH="x86_64"
        [ "$CLUSTER" = "H200" ] && OTHER="GB200" && OTHER_ARCH="aarch64"
        echo ""
        echo "   ⚠  [advisory] $PARTITION 擁塞中 (idle=0, pending=$PART_PEND_ALL)"
        echo "      若有 $OTHER login ($OTHER_ARCH) 可用, 考慮切過去:"
        echo "         ssh <$OTHER-login>  &&  cd $(pwd | sed "s|$HOME|~|")  &&  ./run.sh --rebuild"
        echo "      (checkpoint 可攜, 需在該 login 重編 a.out)"
    fi
fi
echo "════════════════════════════════════════════════════════════════"

if [ "$MODE_STATUS" -eq 1 ]; then
    echo "[--status] 只顯示狀態,退出."
    exit 0
fi

# ═════════════════════════════════════════════════════════════════════════
# 情境 3A: 鏈已在跑 → 不做事
# ═════════════════════════════════════════════════════════════════════════
if [ -n "$QUEUE_JOBS" ]; then
    echo ""
    echo "[3A] Chain 正常運行中 -- run.sh 不會重投."
    echo "     停鏈: ./run job-guard stop-chain   (solver 100 步內感應)"
    echo "     強停: ./run job-guard scancel <jobid>   (只允許本專案 job)"
    exit 0
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
    # [LOCK_JP_PARTITION] 保存鎖定狀態, 跨 --force-cold 還原 (NCHC 政策獨立於模擬狀態, 應存活冷重置)。
    _SAVED_LOCK=0; _SAVED_PIN=""
    [ -e restart/LOCK_JP_PARTITION ] && _SAVED_LOCK=1
    [ -s restart/h200_partition ] && _SAVED_PIN="$(tr -d '[:space:]' < restart/h200_partition)"
    rm -rf restart/ checkpoint/
    rm -f checkrho.dat Ustar_Force_record.dat timing_log.dat
    rm -rf statistics/
    mkdir -p restart/
    # [LOCK_JP_PARTITION] 還原鎖定狀態 (政策存活冷重置, 避免 --force-cold 靜默解鎖)
    if [ "$_SAVED_LOCK" = "1" ]; then
        touch restart/LOCK_JP_PARTITION
        echo "${_SAVED_PIN:-16gpus}" > restart/h200_partition
        echo "    [LOCK] 還原 LOCK_JP_PARTITION + h200_partition=${_SAVED_PIN:-16gpus} (政策跨 --force-cold 存活)"
    fi
    # 恢復 [2] ETA 選出的 partition override (剛被 rm -rf restart/ 刪掉)
    if [ -n "${_BEST_P:-}" ] && [ -n "${_BEST_C:-}" ]; then
        _fc_default="$(awk -F= '/^#SBATCH[[:space:]]+--partition=/{gsub(/^[[:space:]]+|[[:space:]]+$/,"",$2); print $2; exit}' "$CHAIN_DIR/jobscript_chain.slurm.${_BEST_C}" 2>/dev/null)"
        # [PS-2] 依 arch 選對 pin 檔 (H200→h200_partition, 否則 gb200_partition)
        _fc_pin="restart/gb200_partition"; [ "$_BEST_C" = "H200" ] && _fc_pin="restart/h200_partition"
        if [ "$_BEST_P" != "$_fc_default" ]; then
            echo "$_BEST_P" > "$_fc_pin"
            echo "    [partition] 恢復 ETA 選出的 partition=$_BEST_P ($_fc_pin)"
        fi
    fi
    HAS_CKPT=0
    HAS_STATE=0
    echo "[--force-cold] 已清理完畢, 進入 Scenario 1 冷啟動流程"
fi

# ═════════════════════════════════════════════════════════════════════════
# PIPELINE 三情境判定 (Three-Case Decision Tree)
#
#   Case 1: restart/ 有有效 checkpoint → 續跑 (verify/regenerate grid)
#   Case 2: regrid 輸入完整 → 插值 pipeline 生成 restart
#   Case 3: cold-start
#
# 決策優先:
#   --force-cold          → Case 3
#   --regrid-from-origin  → Case 2 (明確指定)
#   restart/ 有 checkpoint → Case 1
#   regrid 輸入完整        → Case 2 (自動偵測)
#   其他                   → Case 3
# ═════════════════════════════════════════════════════════════════════════

# ── Helper: 檢查 Case 2 regrid 三項前置條件是否全部滿足 ──
# 成功時設定: _AUTO_ORIGIN_DIR, _AUTO_OLD_GRID, _AUTO_NEW_GRID
_regrid_inputs_complete() {
    [ -d phase1_generategrid ] || return 1
    [ -d phase2_generatecheckpoint ] || return 1

    local _ny _nz
    _ny="$(_read_define_value NY)"
    _nz="$(_read_define_value NZ)"
    [ -n "$_ny" ] && [ -n "$_nz" ] || return 1

    _AUTO_ORIGIN_DIR=""
    local _cnt=0
    for _d in restart/step_*_origin*/ phase2_generatecheckpoint/step_*_origin*/ phase2_generatecheckpoint/oldcheckpoint_*/; do
        [ -s "${_d}metadata.dat" ] || continue
        _AUTO_ORIGIN_DIR="${_d%/}"
        _cnt=$((_cnt + 1))
    done
    [ "$_cnt" -eq 1 ] || return 1

    _AUTO_OLD_GRID="$(_discover_phase1_grid oldgrid OLD 2>/dev/null)" || return 1
    _AUTO_NEW_GRID="$(_discover_phase1_grid newgrid NEW 2>/dev/null)" || return 1

    return 0
}

# ── Helper: 執行 regrid 維度驗證 + 插值 + 產物檢查 ──
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

    # Step C: 執行 checkpoint interpolation
    echo "[case-2] 維度驗證通過, 執行 checkpoint interpolation (old grid → new grid)..."
    local _INTERP_CMD
    _INTERP_CMD=(python3 phase2_generatecheckpoint/interp_checkpoint.py --auto --step 1
                 --old-dir "$_ORIGIN_DIR"
                 --variables-h variables.h
                 --old-grid-dat "$REGRID_OLD_GRID"
                 --new-grid-dat "$REGRID_NEW_GRID")
    [ -n "${REGRID_OLD_GAMMA:-}" ] && _INTERP_CMD+=(--old-gamma "$REGRID_OLD_GAMMA")
    [ -n "${REGRID_OLD_ALPHA:-}" ] && _INTERP_CMD+=(--old-alpha "$REGRID_OLD_ALPHA")
    [ "$MODE_PREFLIGHT_ONLY" -eq 1 ] && _INTERP_CMD+=(--dry-run)

    if "${_INTERP_CMD[@]}"; then
        if [ "$MODE_PREFLIGHT_ONLY" -eq 1 ]; then
            echo "[case-2] dry-run OK: regrid inputs and solver-grid identity verified"
            HAS_CKPT=0; HAS_STATE=0
        else
            local _CKPT_DIR="restart/checkpoint/step_00000001"
            local _CKPT_OK=1
            [ -s "$_CKPT_DIR/metadata.dat" ] || _CKPT_OK=0
            [ -s "$_CKPT_DIR/f00_0.bin" ]    || _CKPT_OK=0
            [ -s "$_CKPT_DIR/rho_0.bin" ]    || _CKPT_OK=0
            [ -s "restart/grid_provenance" ]  || _CKPT_OK=0
            if [ "$_CKPT_OK" -eq 1 ]; then
                echo "[case-2] 插值成功: $_CKPT_DIR"
                HAS_CKPT=1
                rm -f restart/chain_count restart/chain_jobid
                HAS_STATE=0
            else
                echo "[FATAL] interp_checkpoint.py 回傳 0 但產物不完整 (缺 metadata/f00/rho/provenance)"
                rm -rf restart/checkpoint/step_00000001 restart/checkpoint/step_00000001.WRITING
                rm -f restart/grid_provenance restart/grid_provenance.WRITING
                exit 1
            fi
        fi
    else
        echo "[FATAL] Checkpoint interpolation 失敗 (exit=$?)"
        if [ "$MODE_PREFLIGHT_ONLY" -eq 0 ]; then
            rm -rf restart/checkpoint/step_00000001 restart/checkpoint/step_00000001.WRITING
            rm -f restart/grid_provenance restart/grid_provenance.WRITING
        fi
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
    if python3 J_Frohlich/grid_zeta_tool.py --auto; then
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
            for _d in restart/step_*_origin*/ phase2_generatecheckpoint/step_*_origin*/ phase2_generatecheckpoint/oldcheckpoint_*/; do
                [ -s "${_d}metadata.dat" ] || continue
                _ORIGIN_DIR="${_d%/}"
                _ORIGIN_COUNT=$((_ORIGIN_COUNT + 1))
            done
            if [ "$_ORIGIN_COUNT" -eq 0 ]; then
                echo "[FATAL] --regrid-from-origin 需要唯一 origin checkpoint (含 metadata.dat)"
                echo "        搜尋路徑: restart/step_*_origin*/, phase2_generatecheckpoint/step_*_origin*/, phase2_generatecheckpoint/oldcheckpoint_*/"
                exit 1
            fi
            if [ "$_ORIGIN_COUNT" -gt 1 ]; then
                echo "[FATAL] 多個 origin checkpoint 存在 ($_ORIGIN_COUNT 個), 無法自動選擇"
                ls -1d restart/step_*_origin*/ phase2_generatecheckpoint/step_*_origin*/ phase2_generatecheckpoint/oldcheckpoint_*/ 2>/dev/null
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
        [ -z "$REGRID_NEW_GRID" ] && { REGRID_NEW_GRID="$(_discover_phase1_grid newgrid NEW)"; echo "[case-2] Auto-found NEW grid: $REGRID_NEW_GRID"; }
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
            rm -f restart/chain_count restart/chain_jobid
            HAS_CKPT=0; HAS_STATE=0
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

    # ── (b) Step 1: 生成 solver grid ──
    echo ""
    echo "[case-2] Step 1: 生成模擬用 grid (grid_zeta_tool.py --auto)..."
    if python3 J_Frohlich/grid_zeta_tool.py --auto; then
        echo "[case-2] Step 1 OK: simulation grid ready"
    else
        echo "[FATAL] grid_zeta_tool.py --auto 失敗 (exit=$?)"
        exit 1
    fi

    # ── (c)(d) Step 2: 驗證維度 + 執行插值 ──
    echo ""
    echo "[case-2] Step 2: 驗證維度 + 執行 checkpoint interpolation..."
    _run_regrid_pipeline

    # ── (e) Step 3: 全座標比對 newgrid vs solver grid ──
    echo ""
    echo "[case-2] Step 3: 比對 phase1 newgrid 與 J_Frohlich solver grid (全座標)..."
    _SIM_GRID="$(_derive_solver_grid_path "$_VH_NY" "$_VH_NZ")"
    if [ -s "$_SIM_GRID" ]; then
        _VH_STRETCH_A="$(_read_define_value STRETCH_A)"
        _check_grid_filename_stretch_a "phase1 newgrid" "$REGRID_NEW_GRID" "$_VH_STRETCH_A"
        _check_grid_filename_stretch_a "solver grid" "$_SIM_GRID" "$_VH_STRETCH_A"
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
    if [ "$MODE_PREFLIGHT_ONLY" -eq 1 ]; then
        echo "[case-2] Regrid pipeline dry-run 完成"
    else
        echo "[case-2] Regrid pipeline 完成 -> 進入 Scenario 2 續跑"
    fi
fi

# ─────────────────────────────────────────────────────────────────────
# Case 3: cold-start — 確認 solver grid 存在 (匹配 variables.h)
# ─────────────────────────────────────────────────────────────────────
if [ "$_PIPELINE_CASE" -eq 3 ]; then
    echo "[case-3] 冷啟動: 確認 solver grid 匹配 variables.h..."
    if python3 J_Frohlich/grid_zeta_tool.py --auto; then
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
    echo "          (a) 重新插值: ./run --regrid-from-origin --old-grid <OLD.dat> --new-grid <NEW.dat>"
    echo "          (b) 完全重來: ./run --force-cold"
    echo "          (c) 手動清除 provenance 後冷啟動: rm -f restart/grid_provenance && ./run"
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
        echo "          ./run --regrid-from-origin --old-grid <OLD.dat> --new-grid <NEW.dat>"
        exit 1
    fi
fi

if [ "$MODE_PREFLIGHT_ONLY" -eq 1 ]; then
    echo "[preflight-only] OK: 前置 grid/regrid/provenance 檢查完成, 不編譯、不投遞."
    exit 0
fi

# ═════════════════════════════════════════════════════════════════════════
# 編譯 a.out (Scenario 1 / Scenario 2 缺 binary / --rebuild)
# ═════════════════════════════════════════════════════════════════════════
if [ "$HAS_BIN" -eq 0 ] || [ "$MODE_REBUILD" -eq 1 ]; then
    if [ "$MODE_REBUILD" -eq 1 ]; then
        echo "[build] --rebuild 指定, 強制重編 a.out ($CLUSTER)..."
    else
        echo "[build] a.out 缺失, 呼叫 $BUILD_SCRIPT --build-only 編譯..."
    fi
    bash "$BUILD_SCRIPT" --build-only
    if [ ! -x ./a.out ]; then
        echo "[FATAL] 編譯失敗, a.out 未產出."
        exit 1
    fi
    # 編譯後自動保存 arch-specific 副本, 供 partition-smart-ETA 使用
    if [ ! -s "a.out.${CLUSTER}" ] || [ a.out -nt "a.out.${CLUSTER}" ]; then
        cp -f a.out "a.out.${CLUSTER}"
        echo "[build] 已保存 a.out -> a.out.${CLUSTER}"
    fi
    HAS_BIN=1
fi

# ═════════════════════════════════════════════════════════════════════════
# 佈置 chain state (依三情境分派)
# ═════════════════════════════════════════════════════════════════════════
if   [ "$HAS_CKPT" -eq 0 ] && [ "$HAS_STATE" -eq 0 ]; then
    echo ""
    echo "[1] 冷啟動 (全新)"
    echo "    chain state 將由 $JOBSCRIPT 自動建立 (round=1)"

elif [ "$HAS_CKPT" -eq 1 ] && [ "$HAS_STATE" -eq 0 ]; then
    echo ""
    echo "[2] checkpoint 存在但 chain state 遺失 -> 自動佈置為 Round 2"

    TS=$(date +%Y%m%d_%H%M%S)
    BACKUP_DIR="restart/history_backup_${TS}"
    NEED_BACKUP=0
    for f in checkrho.dat Ustar_Force_record.dat timing_log.dat; do
        [ -f "$f" ] && NEED_BACKUP=1
    done
    if [ "$NEED_BACKUP" -eq 1 ]; then
        mkdir -p "$BACKUP_DIR"
        for f in checkrho.dat Ustar_Force_record.dat timing_log.dat; do
            [ -f "$f" ] && cp -p "$f" "$BACKUP_DIR/" 2>/dev/null
        done
        echo "    [safeguard] 備份 history 檔 -> $BACKUP_DIR/"
    fi
    if [ -f restart/MANIFEST.txt ]; then
        echo "    [info] MANIFEST.txt 存在, 請人工核對本次 a.out 是否與 checkpoint 相容"
        echo "           (NX/NY/NZ/jp 不符可能造成續跑後 checkrho.dat 欄位錯亂)"
    fi

    echo "auto_restore_${TS}" > restart/chain_jobid
    echo "2" > restart/chain_count
    echo "    -> restart/chain_count=2, restart/chain_jobid=auto_restore_${TS}"

elif [ "$HAS_CKPT" -eq 1 ] && [ "$HAS_STATE" -eq 1 ]; then
    CC=$(cat restart/chain_count)
    echo ""
    echo "[3B] 鏈斷了 -> 接續 chain_count=$CC"
    echo "     不動 chain state, 由 jobscript 讀取續跑"

elif [ "$HAS_CKPT" -eq 0 ] && [ "$HAS_STATE" -eq 1 ]; then
    CC=$(cat restart/chain_count)
    if [ "$CC" = "1" ]; then
        echo ""
        echo "[1-edge] chain_count=1 且無 checkpoint -> Round 1 冷啟動"
    else
        echo ""
        echo "[FATAL] 異常狀態: chain_count=$CC (>=2) 但 restart/checkpoint/ 不存在"
        echo "        可能誤刪 checkpoint 或 checkpoint 目錄損毀."
        echo "        若確定要從頭跑: ./run.sh --force-cold"
        exit 1
    fi
fi

if [ -f restart/STOP_CHAIN ]; then
    echo "[cleanup] 移除舊 restart/STOP_CHAIN sentinel"
    rm -f restart/STOP_CHAIN
fi

# ═════════════════════════════════════════════════════════════════════════
# 投遞
# ═════════════════════════════════════════════════════════════════════════

# ── [ARCH-GUARD] 確保 a.out 架構與目標 CLUSTER 一致 ──────────────────────
# 問題根因: run.sh 用 partition-smart-ETA 選定 CLUSTER (如 GB200),
#   但 a.out 可能是上次 build H200 留下的 x86_64 binary,
#   導致 aarch64 節點收到 x86_64 binary → RC=126 "cannot execute binary file".
#   dispatcher (submit_dispatcher.sh) 有 `cp a.out.$cluster a.out` 但 run.sh 沒有.
# 修法: 投遞前若 a.out.{CLUSTER} 存在, 自動切換; 若不存在則驗證架構.
ARCH_EXPECTED=""
case "$CLUSTER" in
    GB200) ARCH_EXPECTED="aarch64" ;;
    H200)  ARCH_EXPECTED="x86_64"  ;;
esac

if [ -s "a.out.${CLUSTER}" ]; then
    CUR_ARCH=$(file -b a.out 2>/dev/null | grep -oE 'x86-64|ARM aarch64' | head -1)
    WANT_ARCH=$(file -b "a.out.${CLUSTER}" 2>/dev/null | grep -oE 'x86-64|ARM aarch64' | head -1)
    if [ "$CUR_ARCH" != "$WANT_ARCH" ]; then
        echo "[ARCH-GUARD] a.out 架構 ($CUR_ARCH) != 目標 $CLUSTER ($WANT_ARCH)"
        echo "             cp a.out.${CLUSTER} -> a.out"
        cp -f "a.out.${CLUSTER}" a.out
    fi
elif [ -n "$ARCH_EXPECTED" ] && [ -x ./a.out ]; then
    CUR_ARCH=$(file -b a.out 2>/dev/null | grep -oE 'x86-64|ARM aarch64' | head -1)
    ARCH_OK=0
    case "$ARCH_EXPECTED" in
        aarch64) [[ "$CUR_ARCH" == "ARM aarch64" ]] && ARCH_OK=1 ;;
        x86_64)  [[ "$CUR_ARCH" == "x86-64" ]]      && ARCH_OK=1 ;;
    esac
    if [ "$ARCH_OK" -eq 0 ]; then
        echo "[ARCH-GUARD] ⚠ FATAL: a.out 架構 ($CUR_ARCH) 與目標 $CLUSTER ($ARCH_EXPECTED) 不符"
        echo "             且 a.out.${CLUSTER} 不存在, 無法自動修正."
        echo "             請先編譯: ./run build $CLUSTER --build-only && cp a.out a.out.${CLUSTER}"
        exit 1
    fi
fi

echo ""
echo "[submit] 投遞: bash $BUILD_SCRIPT --no-clean --no-build  ($CLUSTER)"

# ── Auto-summary hook ──────
# [方案一 2026-04-21] 透過 env var 把 partition 狀態傳給 chain_status.sh
export RUNSH_PARTITION="$PARTITION"
export RUNSH_PART_IDLE="$PART_IDLE"
export RUNSH_PART_ALLOC="$PART_ALLOC"
export RUNSH_PART_PEND_ALL="$PART_PEND_ALL"
export RUNSH_PART_CONGESTED="$PART_CONGESTED"
if [ -f "$CHAIN_DIR/chain_status.sh" ]; then
    bash "$CHAIN_DIR/chain_status.sh" --pre-submit --cluster="$CLUSTER" 2>/dev/null || true
fi

# ═════════════════════════════════════════════════════════════════════════
# [SINGLE-HEAD] 取 HEAD.lockdir 後再呼叫 build_and_submit.sh
# ─────────────────────────────────────────────────────────────────────────
# 中心準則: 每格資料夾在 queue 最多 1 個 job.
# run.sh 在 exec 進 build_and_submit.sh 前先鎖 HEAD.lockdir (state=SUBMITTING),
# build_and_submit.sh 成功 sbatch 後負責 write_head_jobid 升級 state=PENDING.
# 若 lock 取不到 -> 已經有人在投 (A+(a) 決策: 直接讓步).
# ═════════════════════════════════════════════════════════════════════════
if type acquire_head_lock >/dev/null 2>&1; then
    if ! acquire_head_lock "run.sh-$CLUSTER"; then
        echo "[run.sh] ⚠ [SINGLE-HEAD] acquire_head_lock 失敗 — 已有 submitter 或活 job 持有 HEAD.lockdir"
        echo "         依 Single-Head 準則放棄本次投遞 (A+(a)). 請稍後再試或檢查 restart/HEAD.lockdir/owner."
        exit 6
    fi
    echo "[run.sh] [SINGLE-HEAD] ✓ 取得 HEAD.lockdir (state=SUBMITTING), 進入 build_and_submit.sh"
    export HEAD_LOCK_ACQUIRED=1
    export RUNSH_CLUSTER="$CLUSTER"
else
    echo "[run.sh] WARN: head_lock_lib.sh 未載入, 以舊邏輯 (legacy chain_jobid only) 投遞"
fi

exec bash "$BUILD_SCRIPT" --no-clean --no-build
