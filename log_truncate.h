#ifndef LOG_TRUNCATE_H
#define LOG_TRUNCATE_H

// ================================================================
// Phase 6: Log Truncation on Restart
// ----------------------------------------------------------------
// Restart 時, 把 run-log 檔截斷到 <= restart_step (或 <= restart_FTT)
// 的最後一筆, 避免 (step N → crash → step N-K restart) 造成同一個
// step 範圍在 log 中出現兩次, 導致 post-processing 看到鋸齒。
//
// 策略 (rank 0 only, 其他 rank 等 barrier):
//   1) stat() 檢查檔存在 + 非空 (不存在 → 視為成功)
//   2) 逐行讀原檔 (fgets, 4 KB 緩衝 — log 每行 < 200 字)
//   3) 決定保留 / 丟棄, 寫入 ${file}.part
//   4) fclose(part) — 寫入 kernel
//   5) rename(.part, file) — POSIX 原子替換
//   6) 失敗時 unlink(.part), 絕不留孤兒
//
// 省略 fsync:
//   log 不如 checkpoint 重要; 若系統崩潰在 rename 前, 原檔完好;
//   崩潰在 rename 後, 新檔內容 (可能未 flush) 只是少最後幾行 log,
//   不影響科學正確性。Trade-off 值得。
//
// 目標檔案 (4):
//   - Ustar_Force_record.dat   col1 = FTT (double)         → by FTT
//   - timing_log.dat           col1 = step (int)           → by step
//   - checkrho.dat             col1 = step (int, tab-sep)  → by step
//   - weno7_diag.log           block-based, 含 "Step %d"   → by step
//
// 註解 (#), 空白行, 框線 (+===, |...) 在 block 模式外一律保留。
// 使用者規範: "直接了當" — 不做 backup (.bak).
// ================================================================

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cctype>
#include <cerrno>
#include <sys/stat.h>
#include <unistd.h>

#define LT_LINEBUF   4096   // log 每行實測 < 200 字, 4 KB 足夠

// ----------------------------------------------------------------
// rename(part → dst); 失敗時清除 .part 殘留。
// ----------------------------------------------------------------
static inline int _lt_atomic_replace(const char *part, const char *dst) {
    if (rename(part, dst) != 0) {
        fprintf(stderr, "[Phase6] rename(%s, %s) failed: %s\n",
                part, dst, strerror(errno));
        unlink(part);     // 清孤兒, 避免下次再 restart 看到 .part 疑惑
        return -1;
    }
    return 0;
}

// ----------------------------------------------------------------
// 檢測一行是否為「保留行」(comment / blank):
//   - 全空白 / 空字串
//   - 以 # 開頭 (header 註解)
// ----------------------------------------------------------------
static inline bool _lt_is_comment_or_blank(const char *line) {
    const char *p = line;
    while (*p == ' ' || *p == '\t') p++;
    if (*p == '\0' || *p == '\n' || *p == '\r') return true;
    if (*p == '#') return true;
    return false;
}

// ----------------------------------------------------------------
// 以 int step 為鍵截斷 (timing_log.dat, checkrho.dat)
//   保留: comment / blank / col1 <= cutoff_step
//   丟棄: col1 > cutoff_step 或 parse 失敗的資料行
// sscanf("%d") 會跳過前導空白 (空格 + tab + \n), 適用於 checkrho.dat
// 的 tab-separated 格式以及 timing_log.dat 的 space-separated 格式。
// ----------------------------------------------------------------
inline int TruncateLogByStep(const char *path, int cutoff_step) {
    struct stat st;
    if (stat(path, &st) != 0) return 0;   // 不存在: 冷啟動第一 round 視為成功
    if (st.st_size == 0)       return 0;

    FILE *fin = fopen(path, "r");
    if (!fin) {
        fprintf(stderr, "[Phase6] open %s for read failed: %s\n", path, strerror(errno));
        return -1;
    }

    char part[512];
    snprintf(part, sizeof(part), "%s.part", path);
    FILE *fout = fopen(part, "w");
    if (!fout) {
        fprintf(stderr, "[Phase6] open %s for write failed: %s\n", part, strerror(errno));
        fclose(fin);
        return -1;
    }

    char line[LT_LINEBUF];
    long kept = 0, dropped = 0, comments = 0;
    while (fgets(line, sizeof(line), fin) != NULL) {
        if (_lt_is_comment_or_blank(line)) {
            fputs(line, fout);
            comments++;
            continue;
        }
        int s = -1;
        if (sscanf(line, "%d", &s) == 1 && s <= cutoff_step) {
            fputs(line, fout);
            kept++;
        } else {
            dropped++;
        }
    }
    fclose(fin);
    fclose(fout);

    if (_lt_atomic_replace(part, path) != 0) return -1;
    printf("[Phase6] %-28s trimmed: kept=%ld dropped=%ld comments=%ld (cutoff step=%d)\n",
           path, kept, dropped, comments, cutoff_step);
    return 0;
}

// ----------------------------------------------------------------
// 以 double FTT 為鍵截斷 (Ustar_Force_record.dat)
// cutoff_FTT 由 restart_step * dt_global / flow_through_time 計算, 與
// monitor.h 寫入 log 時的公式完全一致 → 精度一致, 不會誤判。
// ----------------------------------------------------------------
inline int TruncateLogByFTT(const char *path, double cutoff_FTT) {
    struct stat st;
    if (stat(path, &st) != 0) return 0;
    if (st.st_size == 0)       return 0;

    FILE *fin = fopen(path, "r");
    if (!fin) {
        fprintf(stderr, "[Phase6] open %s for read failed: %s\n", path, strerror(errno));
        return -1;
    }

    char part[512];
    snprintf(part, sizeof(part), "%s.part", path);
    FILE *fout = fopen(part, "w");
    if (!fout) {
        fprintf(stderr, "[Phase6] open %s for write failed: %s\n", part, strerror(errno));
        fclose(fin);
        return -1;
    }

    char line[LT_LINEBUF];
    long kept = 0, dropped = 0, comments = 0;
    while (fgets(line, sizeof(line), fin) != NULL) {
        if (_lt_is_comment_or_blank(line)) {
            fputs(line, fout);
            comments++;
            continue;
        }
        double ftt = -1.0;
        if (sscanf(line, "%lf", &ftt) == 1 && ftt <= cutoff_FTT) {
            fputs(line, fout);
            kept++;
        } else {
            dropped++;
        }
    }
    fclose(fin);
    fclose(fout);

    if (_lt_atomic_replace(part, path) != 0) return -1;
    printf("[Phase6] %-28s trimmed: kept=%ld dropped=%ld comments=%ld (cutoff FTT=%.4f)\n",
           path, kept, dropped, comments, cutoff_FTT);
    return 0;
}

// ----------------------------------------------------------------
// weno7_diag.log block-aware 截斷
//
// 檔案結構 (由 main.cu:1367-1386 生成):
//   # ══════════════════════════════════════════
//   # WENO7-Z Diagnostic Log                   ← 初始 header (無 Step)
//   # NZ6=... | threshold=...
//   # ══════════════════════════════════════════
//   \n                                          ← blank line 分隔
//   +=========================+
//   |  Step N        | FTT=... | ...
//   |  ...詳細表格...
//   +=========================+
//   \n
//   +=========================+
//   |  Step M        | FTT=... | ...
//   ...
//
// 策略:
//   - 以「空白行」為 block 分隔
//   - 在讀入時把每個 block 累積到 buffer, 同時掃描 "Step N"
//   - 空白行 (或 EOF) 觸發 flush:
//       * cur_step == -1 (初始 header 無 step)  → 保留
//       * cur_step <= cutoff                    → 保留
//       * cur_step >  cutoff                    → 丟棄
//
// 健全性:
//   - 末 block 無空白行結尾 → EOF 後 flush_block 補最後一發
//   - Block 大小遠小於 10 KB, 靜態 16 KB 初始 + 動態 realloc 綽綽有餘
// ----------------------------------------------------------------
inline int TruncateWenoDiagLog(const char *path, int cutoff_step) {
    struct stat st;
    if (stat(path, &st) != 0) return 0;
    if (st.st_size == 0)       return 0;

    FILE *fin = fopen(path, "r");
    if (!fin) {
        fprintf(stderr, "[Phase6] open %s for read failed: %s\n", path, strerror(errno));
        return -1;
    }

    char part[512];
    snprintf(part, sizeof(part), "%s.part", path);
    FILE *fout = fopen(part, "w");
    if (!fout) {
        fprintf(stderr, "[Phase6] open %s for write failed: %s\n", part, strerror(errno));
        fclose(fin);
        return -1;
    }

    size_t buf_cap = 16384;
    char  *buf     = (char*)malloc(buf_cap);
    size_t buf_len = 0;
    int    cur_step = -1;
    long   kept = 0, dropped = 0, header_blocks = 0;

    // flush_block: 根據 cur_step 決定寫 / 丟, 歸零狀態。
    auto flush_block = [&]() {
        if (buf_len == 0) return;
        bool keep = (cur_step < 0) || (cur_step <= cutoff_step);
        if (keep) {
            fwrite(buf, 1, buf_len, fout);
            if (cur_step >= 0) kept++;
            else               header_blocks++;
        } else {
            dropped++;
        }
        buf_len  = 0;
        cur_step = -1;
    };

    char line[LT_LINEBUF];
    while (fgets(line, sizeof(line), fin) != NULL) {
        size_t n = strlen(line);

        // is_blank? (全空白)
        const char *p = line;
        while (*p == ' ' || *p == '\t') p++;
        bool is_blank = (*p == '\0' || *p == '\n' || *p == '\r');

        // append to block buffer
        size_t need = buf_len + n + 1;
        if (need > buf_cap) {
            while (buf_cap < need) buf_cap *= 2;
            buf = (char*)realloc(buf, buf_cap);
        }
        memcpy(buf + buf_len, line, n);
        buf_len += n;
        buf[buf_len] = '\0';

        // scan "Step N" (first occurrence per block wins)
        if (cur_step < 0) {
            const char *sp = strstr(line, "Step ");
            if (sp) {
                int s = -1;
                if (sscanf(sp + 5, "%d", &s) == 1 && s >= 0) {
                    cur_step = s;
                }
            }
        }

        if (is_blank) flush_block();
    }
    // 檔尾若無空白行, 補最後一 block
    flush_block();

    free(buf);
    fclose(fin);
    fclose(fout);

    if (_lt_atomic_replace(part, path) != 0) return -1;
    printf("[Phase6] %-28s trimmed: blocks kept=%ld dropped=%ld headers=%ld (cutoff step=%d)\n",
           path, kept, dropped, header_blocks, cutoff_step);
    return 0;
}

// ----------------------------------------------------------------
// 一鍵截斷全部 4 個 log (rank 0 only)
// 呼叫點: main.cu 的 INIT=3 Phase4 tripwire OK 之後。
// 前提: restart_step > 0 (tripwire 已保證)。
// 呼叫者需要接一個 MPI_Barrier 同步其他 rank。
// ----------------------------------------------------------------
inline void TruncateAllLogsOnRestart(int myid, int cutoff_step, double cutoff_FTT) {
    if (myid != 0) return;

    printf("[Phase6] =========================================================\n");
    printf("[Phase6] Truncating run-logs on restart\n");
    printf("[Phase6]   cutoff step = %d\n", cutoff_step);
    printf("[Phase6]   cutoff FTT  = %.6f\n", cutoff_FTT);
    printf("[Phase6] ---------------------------------------------------------\n");

    TruncateLogByFTT   ("Ustar_Force_record.dat", cutoff_FTT);
    TruncateLogByStep  ("timing_log.dat",         cutoff_step);
    TruncateLogByStep  ("checkrho.dat",           cutoff_step);
    TruncateWenoDiagLog("weno7_diag.log",         cutoff_step);

    printf("[Phase6] =========================================================\n");
    fflush(stdout);
}

#endif // LOG_TRUNCATE_H
