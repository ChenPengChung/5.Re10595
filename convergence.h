#ifndef CONVERGENCE_H
#define CONVERGENCE_H

// ================================================================
// 收斂監控系統 (Convergence Monitoring System)
// ================================================================
//
// 功能:
//   1. 層流 (IS_LAMINAR): 體積流量殘差 δ = |U*(n)-U*(n-1)|/|U*(n)|
//      對齊 Python convergence_epsilon() — 使用 Ub/Uref 純量變化
//   2. 紊流 (!IS_LAMINAR): 統計 CV = σ/|μ| × 100% (Reynolds stress + TKE)
//   3. 收斂狀態管理: NOT_CONVERGED / NEAR / CONVERGED
//
// 使用方式 (main.cu):
//   - 層流: 每 NDTMIT 步在 Launch_Monitor() 中呼叫 ComputeFieldResidual(Ustar)
//   - 紊流: 每 NDTMIT 步餵入 uu/k 到環形緩衝, 每 NDTCONV 步呼叫 compute_cv()
//
// 依賴:
//   - variables.h (EPS_*, CV_*, IS_LAMINAR 等定義)
//   - common.h (CHECK_CUDA, MPI 等)
// ================================================================

#include "common.h"
#include <cmath>
#include <cstring>

// ================================================================
// 全域收斂狀態 (定義在 main.cu)
// ================================================================
extern double g_eps_current;     // 當前場級殘差 δ (層流用)
extern double g_cv_uu, g_cv_k;  // 當前 CV% (紊流用)
extern int    g_conv_status;     // 0=NOT_CONVERGED, 1=NEAR, 2=CONVERGED
extern int    g_conv_count;      // 連續確認計數


// ================================================================
// 收斂狀態字串
// ================================================================
inline const char* ConvStatusStr(int status) {
    switch (status) {
        case 2:  return "CONVERGED";
        case 1:  return "NEAR";
        default: return "NOT_CONVERGED";
    }
}

// ================================================================
// 層流: 體積流量迭代殘差 δ = |U*(n) - U*(n-1)| / |U*(n)|
// ================================================================
//
// 對齊 Python convergence_epsilon() (result/4.Ma_U_Time.py:684):
//   δ = |U*(n) - U*(n-1)| / max(|U*(n)|, 1e-30)
//   其中 U* = Ub/Uref (截面平均體積流量的無因次化)
//
// 每次 Launch_Monitor() 呼叫時更新 (每 NDTMIT 步)
// 零成本: 直接使用已算好的 Ub/Uref, 無需額外 GPU memcpy
//
// 首次呼叫: 初始化 prev, 回傳 1.0
// ================================================================
inline double ComputeFieldResidual(double Ustar_now) {
    static double Ustar_prev = 0.0;
    static bool initialized = false;

    if (!initialized) {
        Ustar_prev = Ustar_now;
        initialized = true;
        return 1.0;  // 首次無歷史
    }

    double diff = fabs(Ustar_now - Ustar_prev);
    double denom = fmax(fabs(Ustar_now), 1e-30);
    double delta = diff / denom;

    Ustar_prev = Ustar_now;
    return delta;
}

// ================================================================
// 紊流: Coefficient of Variation (CV = σ/|μ| × 100%)
// ================================================================
//
// 使用環形緩衝區 (ring buffer) 儲存 monitor 輸出的 uu_RS / k_check
// 計算最近 CV_WINDOW_FTT (10 FTT) 內的 CV
//
// 參數:
//   history   : 環形緩衝區陣列 (size = CV_WINDOW_SIZE)
//   ftt_hist  : 對應的 FTT 時間戳
//   count     : 已填入的筆數 (0..CV_WINDOW_SIZE)
//   idx       : 下一個寫入位置
//   ftt_now   : 當前 FTT
//   window_ftt: 計算視窗 (FTT 單位)
//
// 回傳: CV% (σ/|μ|×100), 資料不足時回傳 100.0
// ================================================================
inline double compute_cv(double *history, double *ftt_hist,
                         int count, int idx, double ftt_now, double window_ftt) {
    if (count < 10) return 100.0;  // 資料不足

    double ftt_start = ftt_now - window_ftt;
    double sum = 0.0, sum_sq = 0.0;
    int n = 0;

    for (int i = 0; i < count; i++) {
        int pos = (idx - count + i + CV_WINDOW_SIZE) % CV_WINDOW_SIZE;
        if (ftt_hist[pos] >= ftt_start) {
            sum    += history[pos];
            sum_sq += history[pos] * history[pos];
            n++;
        }
    }

    if (n < 10) return 100.0;  // 視窗內資料不足

    double mean = sum / (double)n;
    double var  = sum_sq / (double)n - mean * mean;
    double std  = sqrt(fabs(var));

    return (fabs(mean) > 1e-30) ? (std / fabs(mean) * 100.0) : 0.0;
}

#endif // CONVERGENCE_H
