#ifndef TIMING_H
#define TIMING_H

// Header 自足性: 確保即使單獨 include 也能編譯
#include "common.h"
#include "variables.h"  // NX6, NYD6, NZ6 for GPU-only MLUPS

// ================================================================
// 計時系統 (Timing System) v2
// ================================================================
// 功能:
//   1. 總模擬時間 (wall-clock via MPI_Wtime)
//   2. GPU 累積計時 (cudaEvent, 精確到 0.5μs, 跨續跑持續)
//   3. 每 TIMING_INTERVAL 步輸出 MLUPS 效能指標
//   4. [TIMING_DETAIL=1] Per-kernel 時間分解 (Step1, Step2, MPI)
//   5. 固定寬度欄位 timing_log.dat (易讀對齊)
//   6. Timing_FinalSummary() 模擬結束摘要框
//
// GPU 累積計時精度保護:
//   cudaEventElapsedTime 內部用 float32 (max ~24 天 @ ms 精度)
//   每次 Timing_Report 時: 累加區段時間到 gpu_cumul_ms + 重錄起點
//   → 每段測量僅 TIMING_INTERVAL 步 (幾分鐘), 不會溢出
//
// 安全原則:
//   - 所有計時碼均在 #if USE_TIMING 內, USE_TIMING=0 時完全消除
//   - 不修改任何核心物理邏輯
//   - Per-kernel 計時僅在 sample step 啟用
//   - cudaEventRecord 本身是異步的 (不引入額外 GPU 同步)
// ================================================================

#if USE_TIMING

// ── 計時狀態結構 ──
struct TimingState {
    // Wall-clock (MPI_Wtime)
    double wall_start;          // 模擬開始時間
    double wall_last_report;    // 上次報告時間
    int    steps_init;          // 本次 session 起始 step (續跑時 > 0)
    int    steps_last_report;   // 上次報告時的 step

    // GPU 累積計時 (cudaEvent)
    cudaEvent_t ev_gpu_start;   // 每段計時起點 (每次 Report 後重錄)
    cudaEvent_t ev_gpu_stop;    // 臨時停點 (查詢用)
    double gpu_cumul_ms;        // 累積 GPU 時間 (ms), 跨 Report 累加
    double gpu_restored_ms;     // 續跑還原量 (cold start = 0)

    // Per-kernel 分解 (cudaEvent, 僅 sample step 使用)
    // ── P0 v2 Event layout ──
    //   ev_iter:   [iter_start .......................... iter_stop]  ← 完整 sub-step
    //   ev_step1:  ............[step1_start ... step1_stop].........  ← Interior kernel only
    //   ev_psw:    ........................................[psw_s..psw_e]  ← periodicSW only
    //   ev_mpi:    ............[mpi_start ............... mpi_stop]  ← Phase2+3 (cross-stream, 內部用)
    cudaEvent_t ev_step1_start,  ev_step1_stop;   // Interior kernel (stream0)
    cudaEvent_t ev_mpi_start,    ev_mpi_stop;     // Phase2+3 含 sync+PSW (stream1→stream0, 內部校驗)
    cudaEvent_t ev_iter_start,   ev_iter_stop;    // 完整 sub-step (stream0)
    cudaEvent_t ev_psw_start,    ev_psw_stop;     // periodicSW (stream0)

    // ── P0 v2 分解時間 (ms) ──
    // 7 個輸出欄位 + 2 個內部校驗欄位
    //
    // 時間線:
    //   |←─ Buf ─→|←─ max(Int, MPI) ─→|←─ PSW ─→|
    //   |  Phase1  |      Phase2       |  Phase3  |
    //   |  Buffer  | Int (stream0)     | periodic |
    //   | (alone)  | MPI (host/str1)   |   SW     |
    //   |          |  ↕ concurrent     |          |
    //
    // Iter ≈ Buf + max(Int, MPI) + PSW + overhead
    // Idle = max(0, MPI - Int) = 殘留未覆蓋時間 (★ 核心判據 ★)
    //
    float last_buf_ms;          // [P0 v2] Phase1: Buffer 獨佔 GPU (MPI_Wtime, ms)
    float last_step1_ms;        // [P0 v2] Phase2-GPU: Interior kernel (cudaEvent, stream0)
    float last_mpi_wtime_ms;    // [P0 v2] Phase2-Host: Pack+MPI+Unpack 實際 (MPI_Wtime, ms)
    float last_psw_ms;          // [P0 v2] Phase3: periodicSW kernel (cudaEvent, stream0)
    float last_iter_ms;         // 完整 sub-step (cudaEvent, stream0)
    float last_gap_ms;          // 內部: Iter - Int - PSW (= Buf + Idle)
    float last_idle_ms;         // ★ max(0, Gap - Buf) — MPI 未被覆蓋的殘留
    // 內部校驗 (不輸出到 timing_log.dat)
    float last_mpi_ms;          // cudaEvent cross-stream: ev_mpi_start→ev_mpi_stop (Phase2+3)

    // Mid-step mass correction 計時 (MPI_Wtime, ms)
    // 在 main.cu 主迴圈中量測, 每個 iteration 更新一次
    double last_masscorr_ms;    // ← mass correction 時間 (含 kernel+D2H+MPI+H2D)

    // 最近一次 Report 的區間 GPU 時間 (s)
    double last_gpu_interval_s;
};

// 全域計時狀態 (定義在 main.cu 的全域區)
extern TimingState g_timing;

// Per-kernel 取樣旗標 (evolution.h 檢查此旗標)
#if TIMING_DETAIL
extern bool g_timing_sample;
#endif

// ── 初始化 ──
// loop_start:       主迴圈起始 step (restart 時 > 0)
// restored_gpu_ms:  從 checkpoint 還原的 GPU 累積時間 (cold start = 0)
inline void Timing_Init(int loop_start = 0, double restored_gpu_ms = 0.0) {
    g_timing.wall_start        = MPI_Wtime();
    g_timing.wall_last_report  = g_timing.wall_start;
    g_timing.steps_init        = loop_start;   // 記錄本次 session 起始 step
    g_timing.steps_last_report = loop_start;
    g_timing.last_buf_ms       = 0.0f;
    g_timing.last_step1_ms     = 0.0f;
    g_timing.last_mpi_wtime_ms = 0.0f;
    g_timing.last_psw_ms       = 0.0f;
    g_timing.last_iter_ms      = 0.0f;
    g_timing.last_gap_ms       = 0.0f;
    g_timing.last_idle_ms      = 0.0f;
    g_timing.last_mpi_ms       = 0.0f;
    g_timing.last_masscorr_ms  = 0.0;
    g_timing.last_gpu_interval_s = 0.0;

    // GPU 累積計時
    g_timing.gpu_cumul_ms   = restored_gpu_ms;
    g_timing.gpu_restored_ms = restored_gpu_ms;
    CHECK_CUDA(cudaEventCreate(&g_timing.ev_gpu_start));
    CHECK_CUDA(cudaEventCreate(&g_timing.ev_gpu_stop));
    CHECK_CUDA(cudaEventRecord(g_timing.ev_gpu_start, 0));

    // Per-kernel events
    CHECK_CUDA(cudaEventCreate(&g_timing.ev_step1_start));
    CHECK_CUDA(cudaEventCreate(&g_timing.ev_step1_stop));
    CHECK_CUDA(cudaEventCreate(&g_timing.ev_mpi_start));
    CHECK_CUDA(cudaEventCreate(&g_timing.ev_mpi_stop));
    CHECK_CUDA(cudaEventCreate(&g_timing.ev_iter_start));
    CHECK_CUDA(cudaEventCreate(&g_timing.ev_iter_stop));
    CHECK_CUDA(cudaEventCreate(&g_timing.ev_psw_start));
    CHECK_CUDA(cudaEventCreate(&g_timing.ev_psw_stop));
}

// ── 銷毀 ──
inline void Timing_Destroy() {
    CHECK_CUDA(cudaEventDestroy(g_timing.ev_gpu_start));
    CHECK_CUDA(cudaEventDestroy(g_timing.ev_gpu_stop));
    CHECK_CUDA(cudaEventDestroy(g_timing.ev_step1_start));
    CHECK_CUDA(cudaEventDestroy(g_timing.ev_step1_stop));
    CHECK_CUDA(cudaEventDestroy(g_timing.ev_mpi_start));
    CHECK_CUDA(cudaEventDestroy(g_timing.ev_mpi_stop));
    CHECK_CUDA(cudaEventDestroy(g_timing.ev_iter_start));
    CHECK_CUDA(cudaEventDestroy(g_timing.ev_iter_stop));
    CHECK_CUDA(cudaEventDestroy(g_timing.ev_psw_start));
    CHECK_CUDA(cudaEventDestroy(g_timing.ev_psw_stop));
}

// ── 查詢 GPU 累積時間 (分鐘), 唯讀 ──
// 不重錄 ev_gpu_start, 不影響累積邏輯
// 可在任意時機呼叫 (Monitor, VTK 輸出等)
inline double Timing_GetGPUTime_min() {
    CHECK_CUDA(cudaEventRecord(g_timing.ev_gpu_stop, 0));
    CHECK_CUDA(cudaEventSynchronize(g_timing.ev_gpu_stop));
    float segment_ms = 0.0f;
    CHECK_CUDA(cudaEventElapsedTime(&segment_ms, g_timing.ev_gpu_start, g_timing.ev_gpu_stop));
    return (g_timing.gpu_cumul_ms + (double)segment_ms) / 60000.0;
}

// ================================================================
// MLUPS 定義 — 完全對齊 Jin et al. (2025)
// ================================================================
//
// 四個指標, 全部使用相同的 Jin 定義:
//   格點 = NX6 × NYD6 × NZ6 (每 GPU 含 ghost zone)
//   時間 = cudaEventElapsedTime (兩個 CUDA event 之間的 wall-clock 間距)
//
//   ┌─────────────────┬──────────────┬──────────────────────┐
//   │                 │  瞬時(1步)   │  session 累積平均     │
//   ├─────────────────┼──────────────┼──────────────────────┤
//   │  全部 GPU 加總  │  MLUPS       │  MLUPS_avg           │
//   │  單一 GPU       │  MLUPS_pergpu│  MLUPS_avg_pergpu    │
//   └─────────────────┴──────────────┴──────────────────────┘
//
// 重要: cudaEventElapsedTime 量測的是兩個 GPU event 之間的
// wall-clock 間距。Jin 的計時方式完全相同 — **包含 MPI 通訊時間**。
// 四個指標的計時基準一致, 差別僅在:
//   (1) 取樣視窗: 瞬時 (Iter_ms, 1 sub-step) vs 累積 (gpu_cumul_ms)
//   (2) GPU 數量: ×jp (total) vs ×1 (per-GPU)
//
// Jin 報告的 825+ MLUPS/GPU 對應 MLUPS_pergpu 或 MLUPS_avg_pergpu。
// ================================================================

// ── 瞬時 MLUPS (Jin 定義, 單一 sub-step) ──
// per-GPU: pts / time
// total:   pts * jp / time
inline double Timing_ComputeMLUPS_Instant_PerGPU(float iter_ms) {
    if (iter_ms <= 0.0f) return 0.0;
    double pts = (double)(NX6) * (double)(NYD6) * (double)(NZ6);
    return pts / ((double)iter_ms * 1.0e-3) / 1.0e6;
}
inline double Timing_ComputeMLUPS_Instant_Total(float iter_ms) {
    return Timing_ComputeMLUPS_Instant_PerGPU(iter_ms) * (double)(jp);
}

// ── 累積平均 MLUPS (Jin 定義, session average) ──
// per-GPU: pts * steps * 2 / time
// total:   pts * steps * 2 * jp / time
inline double Timing_ComputeMLUPS_Avg_PerGPU(int steps_elapsed, double gpu_seconds) {
    if (gpu_seconds <= 0.0 || steps_elapsed <= 0) return 0.0;
    double pts = (double)(NX6) * (double)(NYD6) * (double)(NZ6);
    double updates = pts * (double)steps_elapsed * 2.0;
    return updates / gpu_seconds / 1.0e6;
}
inline double Timing_ComputeMLUPS_Avg_Total(int steps_elapsed, double gpu_seconds) {
    return Timing_ComputeMLUPS_Avg_PerGPU(steps_elapsed, gpu_seconds) * (double)(jp);
}

// ── 取得 per-kernel 分解 (僅在 sample step 後呼叫) ──
//
// P0 v2 時間線:
//   |←─ Buf ─→|←── max(Int, MPI) ──→|←─ PSW ─→|
//   | Phase 1  |       Phase 2       |  Phase 3 |
//   | Buffer   | Interior (stream0)  | periodic |
//   | (alone)  | MPI (host/stream1)  |   SW     |
//              |   ↕ concurrent      |
//
// 量測來源:
//   Buf_ms  = MPI_Wtime (evolution.h, 含 launch+kernel+sync)
//   Int_ms  = cudaEvent (ev_step1_start → ev_step1_stop, stream0)
//   MPI_ms  = MPI_Wtime (evolution.h, Pack+cudaSync+MPI+Unpack)
//   PSW_ms  = cudaEvent (ev_psw_start → ev_psw_stop, stream0)
//   Iter_ms = cudaEvent (ev_iter_start → ev_iter_stop, stream0)
//
// 衍生:
//   Gap_ms  = Iter - Int - PSW        (= Buf + Idle + overhead)
//   Idle_ms = max(0, Gap - Buf)       (★ 核心判據: =0 → overlap 成功)
//
// 校驗 (不輸出到 log):
//   last_mpi_ms = cudaEvent cross-stream (ev_mpi_start→ev_mpi_stop)
//               ≈ max(Int, MPI) + PSW  (Phase2+3 整體)
//   校驗: Iter ≈ Buf + last_mpi_ms (容許 ±0.05 ms event jitter)
//
#if TIMING_DETAIL
inline void Timing_CollectKernelBreakdown() {
    // ── Primary measurements ──
    CHECK_CUDA(cudaEventElapsedTime(&g_timing.last_step1_ms,
               g_timing.ev_step1_start, g_timing.ev_step1_stop));
    CHECK_CUDA(cudaEventElapsedTime(&g_timing.last_iter_ms,
               g_timing.ev_iter_start, g_timing.ev_iter_stop));
    CHECK_CUDA(cudaEventElapsedTime(&g_timing.last_psw_ms,
               g_timing.ev_psw_start, g_timing.ev_psw_stop));
    // last_buf_ms, last_mpi_wtime_ms: 已在 evolution.h 中由 MPI_Wtime 直接賦值

    // ── Cross-stream 校驗 (internal only) ──
    CHECK_CUDA(cudaEventElapsedTime(&g_timing.last_mpi_ms,
               g_timing.ev_mpi_start, g_timing.ev_mpi_stop));

    // ── Derived: Gap and Idle ──
    //   Gap = Iter - Int - PSW = Phase1(Buf) + 未覆蓋殘留(Idle) + overhead
    //     Idle_ms: MPI 未被 Interior 覆蓋的時間 (★ 核心判據 ★)
    //       Idle ≈ 0 → overlap 成功, MPI 完全被 Interior 隱藏
    //       Idle > 0 → max(0, T_mpi - T_interior), MPI 部分暴露
    g_timing.last_gap_ms = g_timing.last_iter_ms - g_timing.last_step1_ms - g_timing.last_psw_ms;
    if (g_timing.last_gap_ms < 0.0f) g_timing.last_gap_ms = 0.0f;
    g_timing.last_idle_ms = g_timing.last_gap_ms - g_timing.last_buf_ms;
    if (g_timing.last_idle_ms < 0.0f) g_timing.last_idle_ms = 0.0f;
}
#endif

// ── 寫入 timing_log.dat 表頭 (固定寬度, 含單位, 含欄位說明) ──
inline void Timing_WriteHeader(int myid) {
    if (myid == 0) {
        FILE *fp = fopen("timing_log.dat", "a");
        if (fp) {
            fprintf(fp, "# ═══════════════════════════════════════════════════════════════════════════════════════════════════\n");
            fprintf(fp, "# Timing log — GILBM Periodic Hill (Re=%d, %dx%dx%d, %d GPUs)\n",
                    (int)Re, (int)NX, (int)NY, (int)NZ, (int)jp);
            fprintf(fp, "# Architecture: P0 v2 Buffer-first + Interior-MPI overlap, P1 16-dir packed MPI\n");
            fprintf(fp, "# ═══════════════════════════════════════════════════════════════════════════════════════════════════\n");
            fprintf(fp, "#\n");
            fprintf(fp, "# Data types: [T] Total-cumul  [S] Session-cumul  [I] Interval(%d steps)  [P] Point-sample\n", (int)TIMING_INTERVAL);
            fprintf(fp, "#\n");
            fprintf(fp, "# Column definitions:\n");
            fprintf(fp, "#   Step      [T] : Time step (1 iteration = 2 sub-steps: even+odd)\n");
            fprintf(fp, "#   FTT       [T] : Flow-Through Time = step*dt/(LY/Uref)\n");
            fprintf(fp, "#   GPU_min   [T] : Cumulative GPU time (min), CUDA Events, persists across restarts\n");
            fprintf(fp, "#   Wall_min  [S] : Wall-clock since session start (min), MPI_Wtime\n");
            fprintf(fp, "#   MLUPS     [P] : Instantaneous total MLUPS (%d GPUs), Jin definition\n", (int)jp);
            fprintf(fp, "#   MLUPS/GPU [P] : Instantaneous per-GPU MLUPS (compare to Jin's 825+)\n");
            fprintf(fp, "#   MLUPS_avg [S] : Session-average total MLUPS\n");
            fprintf(fp, "#   MLUPSa/GPU[S] : Session-average per-GPU MLUPS\n");
            fprintf(fp, "#   GPU_int_s [I] : GPU time for last reporting interval (seconds)\n");
#if TIMING_DETAIL
            fprintf(fp, "#\n");
            fprintf(fp, "#   ── P0 v2 Kernel Breakdown (7 columns) ──\n");
            fprintf(fp, "#\n");
            fprintf(fp, "#   Timeline:  |<- Buf ->|<- max(Int,MPI) ->|<- PSW ->|\n");
            fprintf(fp, "#               Phase 1     Phase 2          Phase 3\n");
            fprintf(fp, "#               Buffer      Interior(str0)   periodic\n");
            fprintf(fp, "#               (alone)     MPI(host/str1)     SW\n");
            fprintf(fp, "#                            concurrent\n");
            fprintf(fp, "#\n");
            fprintf(fp, "#   Iter = Buf + max(Int, MPI) + PSW + overhead\n");
            fprintf(fp, "#   Idle = max(0, Iter - Buf - Int - PSW)  [= MPI exposed time]\n");
            fprintf(fp, "#\n");
            fprintf(fp, "#   Buf_ms  [P] : Phase1 - Buffer kernel alone (MPI_Wtime)\n");
            fprintf(fp, "#                  Left j=3..6, Right j=NYD6-7..NYD6-4, 142 blocks\n");
            fprintf(fp, "#   Int_ms  [P] : Phase2-GPU - Interior kernel (cudaEvent, stream0)\n");
            fprintf(fp, "#                  j=7..NYD6-8, %d rows, concurrent with MPI\n", (int)(NYD6-14));
            fprintf(fp, "#   MPI_ms  [P] : Phase2-Host - Pack+MPI+Unpack actual (MPI_Wtime)\n");
            fprintf(fp, "#                  16-dir packed persistent, concurrent with Interior\n");
            fprintf(fp, "#   PSW_ms  [P] : Phase3 - periodicSW kernel (cudaEvent, stream0)\n");
            fprintf(fp, "#                  eta-periodic BC, 10/19 dirs (delta_eta!=0 only)\n");
            fprintf(fp, "#   Iter_ms [P] : Full sub-step (cudaEvent, stream0)\n");
            fprintf(fp, "#   Idle_ms [P] : * CORE METRIC * = max(0, Iter-Buf-Int-PSW)\n");
            fprintf(fp, "#                  =0 -> overlap SUCCESS, MPI fully hidden\n");
            fprintf(fp, "#                  >0 -> MPI partially exposed on critical path\n");
            fprintf(fp, "#   MC_ms   [P] : Mass correction between even/odd sub-steps (MPI_Wtime)\n");
#endif
            fprintf(fp, "#\n");
            fprintf(fp, "# MLUPS (Jin et al. 2025): pts/GPU=%lld, MLUPS/GPU comparable to Jin's 825+\n",
                    (long long)NX6 * (long long)NYD6 * (long long)NZ6);
            fprintf(fp, "#\n");
#if TIMING_DETAIL
            fprintf(fp, "# Quick diagnostics:\n");
            fprintf(fp, "#   Idle ~ 0           -> P0 overlap OK, MPI fully hidden by Interior\n");
            fprintf(fp, "#   Idle > 0           -> MPI exposed, need faster Interior or less MPI\n");
            fprintf(fp, "#   Int >> MPI         -> GPU-bound (normal), overlap has headroom\n");
            fprintf(fp, "#   Int << MPI         -> MPI-bound, need comm optimization\n");
            fprintf(fp, "#   Iter ~ Buf+Int+PSW -> perfect overlap (Idle~0)\n");
            fprintf(fp, "#\n");
#endif
            fprintf(fp, "# Output: every %d steps\n", (int)TIMING_INTERVAL);
            fprintf(fp, "#\n");
            // ── Column name row ──
            fprintf(fp, "# %9s %9s %9s %9s %10s %10s %10s %10s %9s",
                    "Step", "FTT", "GPU_min", "Wall_min",
                    "MLUPS", "MLUPS/GPU", "MLUPS_avg", "MLUPSa/GPU", "GPU_int_s");
#if TIMING_DETAIL
            fprintf(fp, " %9s %9s %9s %9s %9s %9s %9s",
                    "Buf_ms", "Int_ms", "MPI_ms", "PSW_ms", "Iter_ms", "Idle_ms", "MC_ms");
#endif
            fprintf(fp, "\n");
            // ── Unit row ──
            fprintf(fp, "# %9s %9s %9s %9s %10s %10s %10s %10s %9s",
                    "[-]", "[-]", "[min]", "[min]",
                    "[MLU/s]", "[MLU/s]", "[MLU/s]", "[MLU/s]", "[sec]");
#if TIMING_DETAIL
            fprintf(fp, " %9s %9s %9s %9s %9s %9s %9s",
                    "[ms]", "[ms]", "[ms]", "[ms]", "[ms]", "[ms]", "[ms]");
#endif
            fprintf(fp, "\n");
            fprintf(fp, "# ───────────────────────────────────────────────────────────────────────────────────────────────────\n");
            fflush(fp);
            fclose(fp);
        }
    }
}

// ── 定期報告 (每 TIMING_INTERVAL 步呼叫一次) ──
inline void Timing_Report(int step, int myid, double FTT_now, const char *argv0) {
    // ── GPU 累積計時: 累加區段 + 重錄起點 ──
    CHECK_CUDA(cudaEventRecord(g_timing.ev_gpu_stop, 0));
    CHECK_CUDA(cudaEventSynchronize(g_timing.ev_gpu_stop));
    float segment_ms = 0.0f;
    CHECK_CUDA(cudaEventElapsedTime(&segment_ms, g_timing.ev_gpu_start, g_timing.ev_gpu_stop));
    g_timing.gpu_cumul_ms += (double)segment_ms;
    g_timing.last_gpu_interval_s = (double)segment_ms / 1000.0;
    CHECK_CUDA(cudaEventRecord(g_timing.ev_gpu_start, 0));  // 重錄起點

    double gpu_total_min = g_timing.gpu_cumul_ms / 60000.0;

    // ── Wall-clock ──
    double wall_now    = MPI_Wtime();
    double wall_total  = wall_now - g_timing.wall_start;
    int    steps_since = step - g_timing.steps_last_report;
    double wall_since  = wall_now - g_timing.wall_last_report;

    if (myid == 0) {
        // ── MLUPS (Jin 定義, 全部 4 指標) ──
        // 瞬時: from last_iter_ms (cudaEvent, 1 sub-step, incl MPI)
        // 若無取樣數據, 用區間 GPU 時間估算
        float iter_ms_used = g_timing.last_iter_ms;
        if (iter_ms_used <= 0.0f && steps_since > 0 && g_timing.last_gpu_interval_s > 0.0) {
            iter_ms_used = (float)(g_timing.last_gpu_interval_s * 1000.0 / (double)(steps_since * 2));
        }
        double mlups_total   = Timing_ComputeMLUPS_Instant_Total(iter_ms_used);
        double mlups_pergpu  = Timing_ComputeMLUPS_Instant_PerGPU(iter_ms_used);

        // 累積平均: from gpu_cumul_ms (cudaEvent, session, incl MPI)
        int    steps_session  = step - g_timing.steps_init;
        double gpu_session_s  = (g_timing.gpu_cumul_ms - g_timing.gpu_restored_ms) / 1000.0;
        double mlups_avg      = Timing_ComputeMLUPS_Avg_Total(steps_session, gpu_session_s);
        double mlups_avg_pgpu = Timing_ComputeMLUPS_Avg_PerGPU(steps_session, gpu_session_s);

        // ── Console 輸出 ──
        printf("+================================================================+\n");
        printf("| Step = %-10d  FTT = %.4f\n", step, FTT_now);
        printf("| %s  %dx%dx%d grids/GPU, %d GPUs  (%lld pts/GPU)\n",
               argv0, (int)NX6, (int)NYD6, (int)NZ6, (int)jp,
               (long long)NX6 * (long long)NYD6 * (long long)NZ6);
        printf("| GPU  time: %.2f min (cumul)\n", gpu_total_min);
        printf("| Wall time: %.2f min (total), %.2f s (last %d sub-steps)\n",
               wall_total / 60.0, wall_since, steps_since * 2);
        printf("| MLUPS (instant):  %8.2f total, %8.2f /GPU  (Iter_ms=%.3f ms)\n",
               mlups_total, mlups_pergpu, iter_ms_used);
        printf("| MLUPS (avg):      %8.2f total, %8.2f /GPU\n",
               mlups_avg, mlups_avg_pgpu);
        printf("| Stats: %s  accu_count=%d\n",
               (FTT_now >= FTT_STATS_START) ? "ON" : "OFF", accu_count);

#if TIMING_DETAIL
        if (g_timing.last_iter_ms > 0.0f) {
            float pct_s1  = 100.0f * g_timing.last_step1_ms / g_timing.last_iter_ms;
            printf("| Kernel breakdown (last sample, per sub-step):\n");
            printf("|   Buffer (alone):        %8.3f ms           <- 邊界 rows 獨佔 GPU\n",
                   g_timing.last_buf_ms);
            printf("|   Interior (S1):         %8.3f ms  (%5.1f%%) <- j=7..NYD6-8\n",
                   g_timing.last_step1_ms, pct_s1);
            printf("|   MPI actual (Wtime):    %8.3f ms           <- Pack+MPI+Unpack 實際\n",
                   g_timing.last_mpi_wtime_ms);
            if (g_timing.last_mpi_wtime_ms > 0.0f && g_timing.last_step1_ms > 0.0f) {
                float overlap_pct = (1.0f - g_timing.last_mpi_wtime_ms / g_timing.last_step1_ms) * 100.0f;
                if (overlap_pct < 0.0f) overlap_pct = 0.0f;
                printf("|   P0 overlap:            %5.1f%%              <- MPI 被 Interior 隱藏%%\n", overlap_pct);
            }
            printf("|   periodicSW:            %8.3f ms           <- η-periodic BC (10/19 dirs)\n",
                   g_timing.last_psw_ms);
            printf("|   Gap (Buf+Idle):        %8.3f ms           <- Iter - S1 - PSW\n",
                   g_timing.last_gap_ms);
            printf("|   ★ Idle bubble:         %8.3f ms           <- Gap - Buf (=0 成功!)\n",
                   g_timing.last_idle_ms);
            printf("|   Sub-step total:        %8.3f ms  = Buf %.3f + Int %.3f + PSW %.3f + Idle %.3f\n",
                   g_timing.last_iter_ms,
                   g_timing.last_buf_ms, g_timing.last_step1_ms, g_timing.last_psw_ms, g_timing.last_idle_ms);
            printf("|   Cross-check:           %8.3f ms  = Buf + mpi_event (%.3f) → diff %.3f ms\n",
                   g_timing.last_buf_ms + g_timing.last_mpi_ms,
                   g_timing.last_mpi_ms,
                   g_timing.last_iter_ms - g_timing.last_buf_ms - g_timing.last_mpi_ms);
            printf("|   MassCorr (mid-step):   %8.3f ms  <- between even/odd sub-steps\n",
                   g_timing.last_masscorr_ms);
        }
#endif
        printf("+================================================================+\n");

        // ── 追加至 timing_log.dat (固定寬度) ──
        FILE *fp = fopen("timing_log.dat", "a");
        if (fp) {
            fprintf(fp, "%10d %9.4f %9.2f %9.2f %10.2f %10.2f %10.2f %10.2f %9.3f",
                    step, FTT_now, gpu_total_min, wall_total / 60.0,
                    mlups_total, mlups_pergpu, mlups_avg, mlups_avg_pgpu,
                    g_timing.last_gpu_interval_s);
#if TIMING_DETAIL
            fprintf(fp, " %9.3f %9.3f %9.3f %9.3f %9.3f %9.3f %9.3f",
                    g_timing.last_buf_ms, g_timing.last_step1_ms,
                    g_timing.last_mpi_wtime_ms,
                    g_timing.last_psw_ms, g_timing.last_iter_ms,
                    g_timing.last_idle_ms, g_timing.last_masscorr_ms);
#endif
            fprintf(fp, "\n");
            fflush(fp);
            fclose(fp);
        }
    }

    // 更新報告基準點
    g_timing.wall_last_report  = wall_now;
    g_timing.steps_last_report = step;
}

// ── 模擬結束摘要 (含收斂資訊) ──
// 需要 convergence.h 的 ConvStatusStr(), g_eps_current, g_cv_uu, g_cv_k, g_conv_status, g_conv_count
inline void Timing_FinalSummary(int step, double FTT_now, int accu_count_val,
                                const char *reason_str, int myid) {
    if (myid != 0) return;

    // convergence.h globals
    extern double g_eps_current, g_cv_uu, g_cv_k;
    extern int    g_conv_status, g_conv_count;

    double gpu_min = Timing_GetGPUTime_min();
    double wall_min = (MPI_Wtime() - g_timing.wall_start) / 60.0;
    int    steps_session = step - g_timing.steps_init;

    // MLUPS (Jin 定義, session average)
    double gpu_session_s  = (g_timing.gpu_cumul_ms - g_timing.gpu_restored_ms) / 1000.0;
    double mlups_avg      = Timing_ComputeMLUPS_Avg_Total(steps_session, gpu_session_s);
    double mlups_avg_pgpu = Timing_ComputeMLUPS_Avg_PerGPU(steps_session, gpu_session_s);

    // 收斂資訊
    double error_val = IS_LAMINAR ? g_eps_current : fmax(g_cv_uu, g_cv_k);
    const char *conv_str = ConvStatusStr(g_conv_status);

    printf("\n");
    printf("+==============================================================+\n");
    printf("|              SIMULATION COMPLETE                             |\n");
    printf("+==============================================================+\n");
    printf("|  Reason      : %-44s|\n", reason_str);
    printf("|  Final Step  : %-44d|\n", step);
    printf("|  Final FTT   : %-44.3f|\n", FTT_now);
    printf("|  GPU Time    : %.2f min (%.4f hrs) %28s|\n", gpu_min, gpu_min/60.0, "");
    printf("|  Wall Time   : %.2f min %38s|\n", wall_min, "");
    printf("|  MLUPS_avg   : %.2f total, %.2f /GPU %20s|\n", mlups_avg, mlups_avg_pgpu, "");
    printf("|  accu_count  : %-44d|\n", accu_count_val);
    if (IS_LAMINAR) {
        printf("|  Final delta : %.2e  (%s, %d consecutive)%14s|\n",
               g_eps_current, conv_str, g_conv_count, "");
    } else {
        printf("|  CV(uu)      : %.2f %%  (%s)%32s|\n", g_cv_uu, conv_str, "");
        printf("|  CV(k)       : %.2f %%  %40s|\n", g_cv_k, "");
        printf("|  Conv checks : %d consecutive%30s|\n", g_conv_count, "");
    }
    printf("+==============================================================+\n");
    printf("\n");
}

#endif // USE_TIMING

#endif // TIMING_H