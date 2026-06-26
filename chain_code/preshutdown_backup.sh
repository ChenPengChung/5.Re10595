#!/bin/bash
# ============================================================================
# preshutdown_backup.sh — NCHC 維護停機(2026-06-27 09:00 ~ 06-28 14:00)前的
#   「資料點 (checkpoint + 三大 log)」session-independent 備援。
# ----------------------------------------------------------------------------
# 由 health_watchdog.sh(systemd --user timer, 每 10 min)在停機前閘窗自動呼叫,
# 亦可手動執行: bash chain_code/preshutdown_backup.sh
#
# 做兩件事(皆 idempotent + 安全守門, 不需任何 Claude session):
#   (A) 把本專案最新 checkpoint 鏡像到 Edit12x(第二道防線)— 僅當 Edit12x 落後
#       且其無 active job;原子複製(.WRITING → mv -T → ln -sfn),檔數+位元組大小一致
#       才切 latest,絕不先刪 Edit12x 既有好份。停機前刻意「不 prune」(空間充裕, 保留還原點)。
#       具自癒: 前次 mv 成功但 ln 失敗遺留的完整 step_N → 直接重指 latest;殘缺 orphan → 移除重做。
#   (B) 三大 append-only log(Ustar_Force_record / timing_log / checkrho)gzip 到
#       /home(主, 專案樹外抗 reset)+ /work(次),md5 兩地核對,各 stem 各留最近 7 份。
#
# 並行安全: 自身持 flock(`live/.preshutdown_backup.lock`)→ timer tick 與手動執行序列化,
#           不靠呼叫端;避免兩寫者同時動同一 `.WRITING` 而促成 content-corrupt latest。
# 守門: 只動「本專案 + Edit12x」(write_guard 放行 Bash cp,使用者授權備援);df 容量檢查;
#       失敗回非 0(呼叫端會轉成 alert)且「絕不」破壞既有資料。
# 測試: PSB_ROOT / PSB_X 可覆寫路徑做沙箱測試(production 預設不變)。
# ============================================================================
set -uo pipefail
ROOT="${PSB_ROOT:-/home/s8313697/5.Re10595/Edit12_Krank56002}"
X="${PSB_X:-/home/s8313697/5.Re10595/Edit12x_Krank56002}"
cd "$ROOT" 2>/dev/null || { echo "FATAL: cannot cd $ROOT"; exit 2; }
export PATH="/usr/bin:/bin:/usr/local/bin:${PATH:-}"
# 在精簡 systemd --user 環境補 USER/HOME 預設(set -u 下避免未綁定中止 → 仍能跑備份)
: "${USER:=$(id -un 2>/dev/null)}"
: "${HOME:=$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)}"
TS(){ date '+%F %T'; }
log(){ echo "[$(TS)] $*"; }

# --- 自身單例鎖: timer tick × 手動執行序列化(不靠呼叫端 flock) ---
mkdir -p live 2>/dev/null || true
exec 9>"live/.preshutdown_backup.lock"
if ! flock -n 9 2>/dev/null; then
    log "另一實例持有鎖 → 跳過本次(避免並行寫 Edit12x)"; exit 0
fi

rc=0
log "===== preshutdown_backup 開始 (ROOT=$ROOT) ====="

# 最新 checkpoint(latest 只會指向已完成的 checkpoint, 讀取安全)
src_latest=$(readlink restart/checkpoint/latest 2>/dev/null || echo "")
[ -z "$src_latest" ] && { log "FATAL: 本專案無 restart/checkpoint/latest"; exit 2; }
src_step="${src_latest#step_}"
src_dir="restart/checkpoint/$src_latest"
[ -d "$src_dir" ] || { log "FATAL: $src_dir 不存在"; exit 2; }
log "本專案 latest = $src_latest (step $src_step)"

# ---------------------------------------------------------------------------
# (A) Edit12x checkpoint 鏡像
# ---------------------------------------------------------------------------
if [ -d "$X/restart/checkpoint" ]; then
    x_latest=$(readlink "$X/restart/checkpoint/latest" 2>/dev/null || echo "")
    x_ok=0
    [ -n "$x_latest" ] && [ -d "$X/restart/checkpoint/$x_latest" ] && x_ok=1
    x_step="${x_latest#step_}"
    if [ "$x_ok" = 1 ] && [ "$x_latest" = "$src_latest" ]; then
        log "(A) Edit12x 已同步 latest=$src_latest → 跳過鏡像 ✓"
    elif [ "$x_ok" = 1 ] && [[ "$x_step" =~ ^[0-9]+$ ]] && [ "$x_step" -ge "$src_step" ]; then
        log "(A) Edit12x latest=$x_latest 不落後本專案($src_latest)→ 跳過"
    else
        # Edit12x 是否有 active job(以 WorkDir 精確判歸屬)? 有則不碰, 避免撞其 restart。
        xjob=$(squeue -u "$USER" -h -o '%Z %i' 2>/dev/null | awk -v d="$X" '$1==d{print $2}' | head -1)
        if [ -n "$xjob" ]; then
            log "(A) Edit12x 有 active job=$xjob → 不鏡像(避免撞其 restart)"; rc=1
        else
            # 先掃殘留 *.WRITING orphan(>2h;自身 flock 保證無並行寫者)— 防累積吃光空間
            find "$X/restart/checkpoint" -maxdepth 1 -name '*.WRITING' -mmin +120 -exec rm -rf {} + 2>/dev/null || true
            dst_tmp="$X/restart/checkpoint/${src_latest}.WRITING"
            dst_fin="$X/restart/checkpoint/$src_latest"
            n_ref=$(find "$src_dir" -type f 2>/dev/null | wc -l)
            sz_ref=$(du -sb "$src_dir" 2>/dev/null | cut -f1)
            promoted=0

            # 自癒: 前次 mv 成功但 ln 失敗 → dst_fin 已是完整複本, 只需重指 latest;殘缺則移除重做。
            if [ -d "$dst_fin" ]; then
                n_fin=$(find "$dst_fin" -type f 2>/dev/null | wc -l)
                sz_fin=$(du -sb "$dst_fin" 2>/dev/null | cut -f1)
                if [ "$n_ref" -gt 0 ] && [ "$n_fin" = "$n_ref" ] && [ -n "$sz_ref" ] && [ "$sz_fin" = "$sz_ref" ]; then
                    if ln -sfn "$src_latest" "$X/restart/checkpoint/latest" 2>/dev/null; then
                        log "(A) ✓ 既有完整 $src_latest(前次 ln 遺留)→ 重指 latest 自癒"; promoted=1
                    else log "(A) FATAL: 自癒 ln 失敗"; rc=1; promoted=1; fi
                else
                    log "(A) 既有 $dst_fin 殘缺(檔 $n_fin/$n_ref 大小 ${sz_fin:-?}/${sz_ref:-?})→ 移除重做"
                    rm -rf "$dst_fin" 2>/dev/null || true
                fi
            fi

            if [ "$promoted" = 0 ]; then
                # df 容量: 需 ~一個 checkpoint(du)+20% 裕度
                need_kb=$(du -sk "$src_dir" 2>/dev/null | awk '{print int($1*1.2)}')
                avail_kb=$(df -Pk "$X/restart/checkpoint" 2>/dev/null | awk 'NR==2{print $4}')
                if [ -n "${need_kb:-}" ] && [ -n "${avail_kb:-}" ] && [ "$avail_kb" -lt "$need_kb" ]; then
                    log "(A) FATAL: Edit12x FS 空間不足 need=${need_kb}KB avail=${avail_kb}KB → 跳過鏡像"; rc=1
                else
                    rm -rf "$dst_tmp" 2>/dev/null || true
                    log "(A) 鏡像 $src_dir → Edit12x ($(du -sh "$src_dir" 2>/dev/null | awk '{print $1}')) ..."
                    if cp -a "$src_dir" "$dst_tmp" 2>/dev/null; then
                        n_dst=$(find "$dst_tmp" -type f 2>/dev/null | wc -l)
                        sz_dst=$(du -sb "$dst_tmp" 2>/dev/null | cut -f1)
                        if [ "$n_ref" -gt 0 ] && [ "$n_dst" = "$n_ref" ] && [ -n "$sz_ref" ] && [ "$sz_dst" = "$sz_ref" ]; then
                            if mv -T "$dst_tmp" "$dst_fin" 2>/dev/null && \
                               ln -sfn "$src_latest" "$X/restart/checkpoint/latest" 2>/dev/null; then
                                log "(A) ✓ Edit12x latest 更新為 $src_latest (檔數 $n_dst, $sz_dst bytes, 原子切換)"
                            else
                                log "(A) FATAL: mv/ln 失敗 → Edit12x 既有好份不動"; rc=1
                            fi
                        else
                            log "(A) FATAL: 複製檔數/大小不符 n=$n_dst/$n_ref sz=${sz_dst:-?}/${sz_ref:-?} → 不切 latest"; rc=1
                        fi
                    else
                        log "(A) FATAL: cp 失敗 → 清 .WRITING, Edit12x 既有好份不動"
                        rm -rf "$dst_tmp" 2>/dev/null || true; rc=1
                    fi
                fi
            fi
        fi
    fi
else
    log "(A) Edit12x checkpoint 目錄不存在 → 跳過鏡像"; rc=1
fi

# ---------------------------------------------------------------------------
# (B) 三大 log gzip 備份(主 /home + 次 /work, md5 核對, 各 stem 留 7 份)
#     節流: 預設僅在「距上次備份 >12h」才做(讓 /Edit12 loop 每輪呼叫本腳本也不會每輪重壓);
#     `PSB_FORCE_LOGS=1` 強制(停機前硬閘 / watchdog backstop 想要最新一份時)。
# ---------------------------------------------------------------------------
do_logs=1
if [ "${PSB_FORCE_LOGS:-0}" != "1" ]; then
    _mk=$(cat live/.last_log_backup 2>/dev/null || echo 0)
    [[ "$_mk" =~ ^[0-9]+$ ]] || _mk=0
    _age=$(( $(date +%s) - _mk ))
    if [ "$_mk" -ne 0 ] && [ "$_age" -lt 43200 ]; then
        do_logs=0
        log "(B) 距上次 log 備份 $((_age/3600))h (<12h) → 跳過(PSB_FORCE_LOGS=1 可強制)"
    fi
fi
if [ "$do_logs" = 1 ]; then
BK_HOME="$HOME/log_backups/edit12_Krank56002"
BK_WORK="/work/s8313697/edit12_log_backups"
mkdir -p "$BK_HOME" 2>/dev/null || true
mkdir -p "$BK_WORK" 2>/dev/null || true
TSf=$(date +%Y%m%d_%H%M%S)
logs_ok=0
for f in Ustar_Force_record.dat timing_log.dat checkrho.dat; do
    [ -f "$f" ] || { log "(B) $f 不存在 → 跳過"; continue; }
    stem="${f%.dat}"
    out="$BK_HOME/${stem}_${TSf}_step${src_step}.dat.gz"
    if gzip -c "$f" > "$out" 2>/dev/null && gzip -t "$out" 2>/dev/null; then
        logs_ok=1
        cp -a "$out" "$BK_WORK/" 2>/dev/null || true
        m1=$(md5sum "$out" 2>/dev/null | awk '{print $1}')
        m2=$(md5sum "$BK_WORK/$(basename "$out")" 2>/dev/null | awk '{print $1}')
        if [ -n "$m1" ] && [ "$m1" = "$m2" ]; then
            log "(B) ✓ $f → $(basename "$out") (md5 兩地一致)"
        else
            log "(B) ⚠ $f /work 副本缺/不一致(scratch 可能清), 主份 OK"
        fi
        # 輪替: 兩地各 stem 只留最近 7 份
        ls -t "$BK_HOME/${stem}"_*.gz 2>/dev/null | tail -n +8 | xargs -r rm -f
        ls -t "$BK_WORK/${stem}"_*.gz 2>/dev/null | tail -n +8 | xargs -r rm -f
    else
        log "(B) FATAL: $f gzip 失敗 → 刪半成品"; rm -f "$out" 2>/dev/null || true; rc=1
    fi
done
# marker 只在至少一份 log 成功時更新(否則 /Edit12 第8步會誤信 marker 而跳過真正的備份)
if [ "$logs_ok" = 1 ]; then date +%s > live/.last_log_backup 2>/dev/null || true; fi
fi   # do_logs

log "===== preshutdown_backup 結束 (rc=$rc, latest=$src_latest) ====="
exit "$rc"
