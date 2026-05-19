#include <time.h>
#include <math.h>
#include <cuda.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <dirent.h>
#include <mpi.h>
#include <stdarg.h>
#include <signal.h>        // 必須在 variables.h 之前 — variables.h 定義 #define cs (1.0/sqrt(3))
                           // 會與 <bits/sigcontext.h> 的 cs 欄位衝突。先 include signal.h
                           // 讓 sigcontext struct 先被解析, 之後 macro 才生效不影響。
#include "variables.h"
using namespace std;
/************************** Host Variables **************************/
double  *fh_p[19]; //主機端一般態分佈函數
double  *rho_h_p,  *u_h_p,  *v_h_p,  *w_h_p;


/************************** Device Variables **************************/
double  *ft[19], *fd[19];
double  *rho_d,  *u,  *v,  *w;

/* double  *KT,    *DISS,
        *DUDX2, *DUDY2, *DUDZ2,
        *DVDX2, *DVDY2, *DVDZ2,
        *DWDX2, *DWDY2, *DWDZ2; */
double  *U,  *V,  *W,  *P, 
        *UU, *UV, *UW, *VV, *VW, *WW, *PU, *PV, *PW, *PP,
        *DUDX2, *DUDY2, *DUDZ2,
        *DVDX2, *DVDY2, *DVDZ2, 
        *DWDX2, *DWDY2, *DWDZ2,
        *UUU, *UUV, *UUW, *UVW,
        *VVU, *VVV, *VVW,
        *WWU, *WWV, *WWW;

/************************** Other Variables **************************/
double  *x_h,                     // x 座標 (均勻, 一維 [NX6])
        *x_d;
double  *y_2d_h, *y_2d_d;        // y 座標 (非均勻, 二維 [NYD6*NZ6])
double  *z_h,    *z_d;           // z 座標 (非均勻, 二維 [NYD6*NZ6])
double  *Xdep_h[3], *Ydep_h[3], *Zdep_h[3],
        *Xdep_d[3], *Ydep_d[3], *Zdep_d[3];

//======== GILBM 度量項（升級: 完整 2×2 Jacobian）========
// 座標變換 (x,y,z) → 計算空間 (η=i, ξ=j, ζ=k)
//
// 正 Jacobian (物理→計算座標的偏微分):
//   ∂y/∂ξ = y_xi,   ∂y/∂ζ = y_zeta
//   ∂z/∂ξ = z_xi,   ∂z/∂ζ = z_zeta
// Host only (用於計算逆 Jacobian):
double *y_xi_h, *y_zeta_h;       // [NYD6*NZ6]
double *z_xi_h;                   // [NYD6*NZ6] Host only
double *z_zeta_h, *z_zeta_d;     // [NYD6*NZ6] ∂z/∂ζ：WENO7 stretch factor R 直接使用
double *J_2D_h;                   // Jacobian 行列式 [NYD6*NZ6]
//
// 逆 Jacobian (計算→物理):
//   ∂ξ/∂y = xi_y  =  z_zeta / J_2D
//   ∂ξ/∂z = xi_z  = -y_zeta / J_2D
//   ∂ζ/∂y = zeta_y = -z_xi  / J_2D   (取代舊 dk_dy)
//   ∂ζ/∂z = zeta_z =  y_xi  / J_2D   (取代舊 dk_dz)
double *xi_y_h,   *xi_y_d;       // [NYD6*NZ6]
double *xi_z_h,   *xi_z_d;       // [NYD6*NZ6]
double *zeta_y_h, *zeta_y_d;     // [NYD6*NZ6]
double *zeta_z_h, *zeta_z_d;     // [NYD6*NZ6]

// [REMOVED] delta_xi_h/d, delta_zeta_h/d, delta_eta_h[19]
// 2026-04 重構: δη, δξ, δζ 全部移至 Step1 kernel 即時計算，不再預計算存儲。

// Precomputed stencil base k [NZ6] (int, wall-clamped)
int *bk_precomp_h, *bk_precomp_d;

// Phase 3: Curvilinear global time step (runtime, from CFL on contravariant velocities)
// NOTE: dt (= minSize) is derived from GAMMA in variables.h (tanh analytic formula).
//       dt_global is the actual curvilinear time step, computed at runtime.
double dt_global;
double omega_global;     // = 3·niu/dt_global + 0.5 (dimensionless relaxation time)
double omegadt_global;   // = omega_global · dt_global (dimensional relaxation time τ)

// GILBM GTS architecture: persistent global arrays (方案B 雙緩衝)
double *f_post_d;         // GTS: 碰後分佈 buffer A [19 * NX6*NYD6*NZ6]
double *f_post_d2;        // GTS: 碰後分佈 buffer B [19 * NX6*NYD6*NZ6]
double *f_post_read;      // 方案B: 指向本步讀取的 buffer (swap after each sub-step)
double *f_post_write;     // 方案B: 指向本步寫入的 buffer (swap after each sub-step)
// [方案A] feq_d 已移除 — collision 自行計算 feq
// [方案B] f_new[19] 不再使用 — f_streamed 在 register 直接碰撞

//
// 逆變速度 (升級: ξ 和 ζ 均為 y-z 平面變數):
//   ẽ_α_η = e[α][0] / dx                                (常數)
//   ẽ_α_ξ = e[α][1]*xi_y(j,k)   + e[α][2]*xi_z(j,k)   (二維變數)
//   ẽ_α_ζ = e[α][1]*zeta_y(j,k) + e[α][2]*zeta_z(j,k)  (二維變數)
//
// RK2 上風點座標是 kernel 局部變量，不需全場存儲


//Variables for forcing term modification.
double  *Ub_avg_h,  *Ub_avg_d;
double  Ub_avg_global = 0.0;   // Bcast 後的全場代表 u_bulk (rank 0 入口截面)
double  A_cross_j3   = 0.0;   // 入口截面 (j=3) 實際格點面積 (startup 計算, 取代 LX*(LZ-1))

double  *Force_h,   *Force_d;

double *rho_modify_h, *rho_modify_d;

// GPU reduction partial sums for mass conservation (replaces SendDataToCPU every step)
double *rho_partial_h, *rho_partial_d;
double *rho_cv_weight_h, *rho_cv_weight_d;  // control-volume weights for volume-weighted mass correction
double rho_cv_global_volume = 0.0;          // Σ control-volume weights across all ranks

// Time-average accumulation (FTT-gated)
// u=spanwise, v=streamwise, w=wall-normal; GPU-side accumulation
double *u_tavg_h = NULL, *v_tavg_h = NULL, *w_tavg_h = NULL;   // host (for VTK output)
double *u_tavg_d = NULL, *v_tavg_d = NULL, *w_tavg_d = NULL;   // device (accumulated on GPU)
// Vorticity mean accumulation (same Stage 1 window as velocity mean)
double *ox_tavg_h = NULL, *oy_tavg_h = NULL, *oz_tavg_h = NULL; // host
double *ox_tavg_d = NULL, *oy_tavg_d = NULL, *oz_tavg_d = NULL; // device
int accu_count = 0;         // Unified statistics accumulation count (FTT >= FTT_STATS_START)
bool stage1_announced = false;

int nProcs, myid;

int step;
int restart_step = 0;  // 續跑起始步 (INIT=1 從 accu.dat, INIT=3 從 metadata.dat)
int accu_num = 0;
double g_restored_gpu_ms = 0.0;  // 續跑還原的 GPU 累積時間 (ms)
double g_gpu_time_min = 0.0;     // 供 monitor.h 使用的 GPU 時間 (min)

// Phase 7: 實時停止控制全域 (定義於此, 宣告於 stop_control.h)
volatile sig_atomic_t g_signal_received = 0;  // 由 signal handler 設定
int                   g_stop_reason     = 0;   // StopReason enum

// [RESTART-FIX] PID + Gehrke 控制器狀態 (evolution.h 以 extern 引用)
//   必須跨 restart 保持, 否則 Force 會在續跑邊界出現階躍不連續
double g_force_integral    = 0.0;
double g_error_prev        = 0.0;
bool   g_ctrl_initialized  = false;
bool   g_gehrke_activated  = false;

// Phase 8: runtime argv 覆寫 (定義於此, 宣告於 runtime_args.h)
//   預設 = compile-time #define, argv --cold / --restart=<dir> 可覆寫
int         g_init_runtime    = INIT;
const char *g_restart_bin_dir = RESTART_BIN_DIR;
// [REVIEW-FIX #3] 預設 false;ParseRuntimeArgs 收到 --restart=<dir> 才置 true。
// 用途:Phase 4 tripwire 若 INIT=3 但此旗標仍為 false,代表使用者踩到
//   「忘了傳 argv 結果吃到 variables.h hardcoded 步數」的 silent-failure 路徑。
bool        g_restart_dir_set_by_argv = false;

// 收斂監控全域變數
double g_eps_current = 1.0;      // 場級殘差 δ (層流)
double g_cv_uu = 100.0;          // CV% of uu_RS (紊流)
double g_cv_k  = 100.0;          // CV% of k_TKE (紊流)
int    g_conv_status = 0;        // 0=NOT_CONVERGED, 1=NEAR, 2=CONVERGED
int    g_conv_count  = 0;        // 連續確認計數

// 紊流 CV 環形緩衝區
double uu_history[CV_WINDOW_SIZE];
double k_history[CV_WINDOW_SIZE];
double ftt_cv_history[CV_WINDOW_SIZE];
int    cv_idx = 0, cv_buf_count = 0;

// WENO VTK contour: per-grid-point activation count — ζ 方向 (host buffer)
#if USE_WENO7
unsigned char *weno_activation_zeta_h = NULL;  // [NZ6][NYD6][NX6], malloc'd at startup
#endif

int l_nbr, r_nbr;

MPI_Status    istat[8];

MPI_Request   request[23][4];
MPI_Status    status[23][4];

MPI_Datatype  DataSideways;

cudaStream_t  stream0, stream1, stream2;
cudaStream_t  tbsum_stream[2];
cudaEvent_t   start,   stop;
cudaEvent_t   start1,  stop1;

int Buffer     = 3;
int icount_sw  = Buffer * NX6 * NZ6;
int iToLeft    = (Buffer+1) * NX6 * NZ6;
int iFromLeft  = 0;
int iToRight   = NX6 * NYD6 * NZ6 - (Buffer*2+1) * NX6 * NZ6;
int iFromRight = iToRight + (Buffer+1) * NX6 * NZ6;

MPI_Request reqToLeft[23], reqToRight[23],   reqFromLeft[23], reqFromRight[23];
MPI_Request reqToTop[23],  reqToBottom[23],  reqFromTop[23],  reqFromBottom[23];

// [P2] Packed MPI exchange buffers (device memory, 16 directions × icount_sw each)
// 4 buffers × 16 × icount_sw doubles ≈ 4.15 MB total
double *mpi_send_buf_left_d  = NULL;
double *mpi_send_buf_right_d = NULL;
double *mpi_recv_buf_left_d  = NULL;
double *mpi_recv_buf_right_d = NULL;
MPI_Request req_persist[4];   // MPI persistent communication handles

// [P2-macro] Macro exchange: ρ/u/v/w ghost zone 交換 (AccumulateVorticity 前)
#define MACRO_COMPONENTS 4
double *macro_send_buf_left_d  = NULL;
double *macro_send_buf_right_d = NULL;
double *macro_recv_buf_left_d  = NULL;
double *macro_recv_buf_right_d = NULL;
MPI_Request req_persist_macro[4];   // macro MPI persistent handles (tags 700/701)

int itag_f3[23] = {250,251,252,253,254,255,256,257,258,259,260,261,262,263,264,265,266,267,268,269,270,271,272};
int itag_f4[23] = {200,201,202,203,204,205,206,207,208,209,210,211,212,213,214,215,216,217,218,219,220,221,222};
int itag_f5[23] = {300,301,302,303,304,305,306,307,308,309,310,311,312,313,314,315,316,317,318,319,320,321,322};
int itag_f6[23] = {400,401,402,403,404,405,406,407,408,409,410,411,412,413,414,415,416,417,418,419,420,421,422};


#include "common.h"
#include "model.h"
#include "memory.h"
#include "initialization.h"
#include "gilbm/metric_terms.h"
#include "gilbm/precompute.h"
#include "gilbm/diagnostic_gilbm.h"
#include "communication.h"
#include "convergence.h"
#include "stop_control.h"
#include "log_truncate.h"
#include "runtime_args.h"
#include "monitor.h"
#include "statistics.h"
#include "timing.h"

// ── Timing system globals ──
// 必須在 evolution.h 之前宣告, 因為 evolution.h 中的 TIMING macro 引用這些變數
#if USE_TIMING
TimingState g_timing;
#if TIMING_DETAIL
bool g_timing_sample = false;
#endif
#endif

#include "evolution.h"
#include "fileIO.h"
#include "MRT_Matrix.h"
#include "MRT_Process.h"
#include "mrt_projection_host.h"

static void BuildCurrentGridDatPath(char *grid_dat_path, size_t n)
{
    char grid_ref_stem[256];
    strncpy(grid_ref_stem, GRID_DAT_REF, sizeof(grid_ref_stem) - 1);
    grid_ref_stem[sizeof(grid_ref_stem) - 1] = '\0';
    { char *ext = strrchr(grid_ref_stem, '.'); if (ext) *ext = '\0'; }

    snprintf(grid_dat_path, n,
             "%s/adaptive_%s_I%d_J%d_g%.2f_a%.1f.dat",
             GRID_DAT_DIR, grid_ref_stem,
             NY, NZ, (double)GAMMA, (double)ALPHA);
}

static void TrimLineValue(char *s)
{
    size_t len = strlen(s);
    while (len > 0 && (s[len - 1] == '\n' || s[len - 1] == '\r' ||
                       s[len - 1] == ' '  || s[len - 1] == '\t')) {
        s[--len] = '\0';
    }
}

static int ReadGridParamsSha256(const char *path, char *out, size_t out_n)
{
    FILE *fp = fopen(path, "r");
    if (!fp) return 0;

    const char *key = "GRID_PARAMS_SHA256=";
    const size_t key_len = strlen(key);
    char line[4096];
    int found = 0;
    while (fgets(line, sizeof(line), fp)) {
        if (strstr(line, "DT=")) break;
        char *p = strstr(line, key);
        if (!p) continue;
        p += key_len;
        TrimLineValue(p);
        strncpy(out, p, out_n - 1);
        out[out_n - 1] = '\0';
        found = 1;
        break;
    }
    fclose(fp);
    return found;
}

static int ReadCheckpointMetaValue(const char *checkpoint_dir,
                                   const char *key,
                                   char *out,
                                   size_t out_n)
{
    char meta_path[1024];
    if (checkpoint_dir && checkpoint_dir[0] == '/')
        snprintf(meta_path, sizeof(meta_path), "%s/metadata.dat", checkpoint_dir);
    else
        snprintf(meta_path, sizeof(meta_path), "./%s/metadata.dat", checkpoint_dir);

    FILE *fp = fopen(meta_path, "r");
    if (!fp) return 0;

    char prefix[256];
    snprintf(prefix, sizeof(prefix), "%s=", key);
    const size_t prefix_len = strlen(prefix);
    char line[4096];
    int found = 0;
    while (fgets(line, sizeof(line), fp)) {
        if (strncmp(line, prefix, prefix_len) != 0) continue;
        char *value = line + prefix_len;
        TrimLineValue(value);
        strncpy(out, value, out_n - 1);
        out[out_n - 1] = '\0';
        found = 1;
        break;
    }
    fclose(fp);
    return found;
}

static void PrecheckCheckpointGridConsistency(const char *checkpoint_dir, int rank)
{
    if (rank != 0) return;

    char match_flag[32] = {0};
    if (ReadCheckpointMetaValue(checkpoint_dir, "interp_solver_grid_match",
                                match_flag, sizeof(match_flag)) &&
        strcmp(match_flag, "0") == 0) {
        fprintf(stderr,
                "\n[FATAL][GRID] checkpoint metadata says Phase 1 NEW grid did not match solver grid\n"
                "  Checkpoint dir : %s\n"
                "  Field          : interp_solver_grid_match=0\n"
                "  Policy         : refuse to load checkpoint data.\n",
                checkpoint_dir);
        MPI_Abort(MPI_COMM_WORLD, 1);
    }

    char expected_fp[128] = {0};
    int has_expected = ReadCheckpointMetaValue(
        checkpoint_dir, "interp_solver_grid_params_sha256",
        expected_fp, sizeof(expected_fp));
    if (!has_expected) {
        has_expected = ReadCheckpointMetaValue(
            checkpoint_dir, "interp_new_grid_params_sha256",
            expected_fp, sizeof(expected_fp));
    }

    char grid_dat_path[512];
    BuildCurrentGridDatPath(grid_dat_path, sizeof(grid_dat_path));
    char current_fp[128] = {0};
    int has_current = ReadGridParamsSha256(grid_dat_path, current_fp, sizeof(current_fp));

    if (has_expected && has_current && strcmp(expected_fp, current_fp) != 0) {
        fprintf(stderr,
                "\n[FATAL][GRID] grid parameter fingerprint mismatch before checkpoint load\n"
                "  Checkpoint dir : %s\n"
                "  Runtime grid   : %s\n"
                "  checkpoint fp  : %s\n"
                "  runtime fp     : %s\n"
                "  Probable cause : Phase 1 and runtime grids were generated with different\n"
                "                   Poisson/grid parameters (for example n_iter mismatch).\n"
                "  Policy         : regenerate Phase 1 grids and checkpoint from shared grid_params.py.\n",
                checkpoint_dir, grid_dat_path, expected_fp, current_fp);
        MPI_Abort(MPI_COMM_WORLD, 1);
    }

    if (has_expected && !has_current) {
        printf("[GRID] WARN: checkpoint has grid fingerprint but runtime .dat lacks one; legacy grid header, skipping hash check\n");
    } else if (!has_expected && has_current) {
        printf("[GRID] WARN: runtime grid has fingerprint but checkpoint metadata lacks one; legacy checkpoint, skipping hash check\n");
    } else if (has_expected && has_current) {
        printf("[GRID] Parameter fingerprint OK: %s\n", current_fp);
    }
}
// ── Animation 自動渲染參數（可自由調整）──
// 新 pipeline (v3 lossless MP4): 每次 VTK 輸出 → pipeline.py 背景呼叫
//   → 產 2 張 4K PNG 到 animation/png_frames/ (永久保留, 續跑必備資料)
//   → ffmpeg 從所有 PNG 序列重 encode 2 支 lossless MP4:
//       animation/flow_cont.mp4  — KEY_COLORS 連續色階
//       animation/flow_RD.mp4    — Rainbow Desaturated step 33
//   續跑安全: png_frames/ 存在就直接繼承; 不存在就當冷啟動
//   ffmpeg 失敗不會毀歷史; 可用 animation/rebuild_mp4.sh 手動重建
#define ANIM_ENABLE        0              // 1=啟用動畫輸出, 0=完全關閉
#define ANIM_EVERY_N_VTK   1              // 每 N 次 VTK 輸出渲染 1 幀
#define ANIM_FPS           33             // MP4 播放幀率 (33 = 固定每張 ~30ms)
#define ANIM_WIDTH         3840           // PNG/MP4 寬度 (3840 = 4K), 高度自動算
#define ANIM_CODEC         "libx264"      // lossless: "libx264" (廣相容) / "ffv1" (更小)
#define ANIM_PIX_FMT       "yuv444p"      // true lossless: "yuv444p" / 相容: "yuv420p"
#include "animation/mp4_snapshot.h"

int main(int argc, char *argv[])
{
    CHECK_MPI( MPI_Init(&argc, &argv) );
    CHECK_MPI( MPI_Comm_size(MPI_COMM_WORLD, &nProcs) );
    CHECK_MPI( MPI_Comm_rank(MPI_COMM_WORLD, &myid) );

    // ===== Phase 7: 安裝 signal handlers (SIGUSR1/USR2/TERM) =====
    // SLURM --signal=USR1@120 會在 walltime 前 120s 送 SIGUSR1 → 觸發乾淨退出
    // 使用者可手動 `scancel --signal=USR2 $JID` 要求優雅停止
    InstallStopHandlers();
    if (myid == 0) {
        printf("[Phase7] Signal handlers installed: SIGUSR1, SIGUSR2, SIGTERM\n");
    }

    // ===== Phase 8: Parse argv (--cold | --restart=<dir> | --help) =====
    //   預設 = compile-time INIT (from variables.h)。
    //   argv 覆寫 → 設 g_init_runtime / g_restart_bin_dir。
    ParseRuntimeArgs(argc, argv, myid);
    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );

    // Safety check: compiled jp must match runtime MPI rank count
    // [POLICY-C1] 改走 FatalUnavoidable → exit 42 (不可避免錯誤,停鏈)
    if (nProcs != jp) {
        char _msg[256];
        snprintf(_msg, sizeof(_msg),
            "nProcs=%d but compiled with jp=%d. Recompile with correct jp.",
            nProcs, jp);
        FatalUnavoidable(myid, "ranks-vs-jp", _msg);
    }

	l_nbr = myid - 1;       r_nbr = myid + 1;
    if (myid == 0)    l_nbr = jp-1;
	if (myid == jp-1) r_nbr = 0;

	int iDeviceCount = 0;
    CHECK_CUDA( cudaGetDeviceCount( &iDeviceCount ) );
    CHECK_CUDA( cudaSetDevice( myid % iDeviceCount ) );

    if (myid == 0)  printf("\n%s running with %d GPUs...\n\n", argv[0], (int)(jp));          CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );
    printf( "[ Info ] Rank Rank %2d/%2d, localrank: %d/%d\n", myid, nProcs-1, myid, iDeviceCount );

    // ── Runtime 安全檢查:MPI 分解相容性 ──
    // [POLICY-C1] 分解 / grid 不匹配是 compile-time 配置錯誤,續跑也不會修好 → 停鏈
    if (nProcs != jp) {
        char _msg[256];
        snprintf(_msg, sizeof(_msg), "MPI ranks (%d) != jp (%d).", nProcs, (int)jp);
        FatalUnavoidable(myid, "ranks-vs-jp-2", _msg);
    }
    if ((NY - 1) % jp != 0) {
        if (myid == 0) {
            printf("[FATAL] Grid-GPU mismatch: (NY-1)=%d is not divisible by jp=%d!\n",
                   (int)(NY-1), (int)jp);
            printf("        Actual MPI interior j-points: jp*(NYD6-7) = %d*%d = %d\n",
                   (int)jp, (int)(NYD6-7), (int)(jp*(NYD6-7)));
            printf("        Expected (NY-1) = %d → missing %d j-rows!\n",
                   (int)(NY-1), (int)((NY-1) - jp*(NYD6-7)));
            printf("        Fix: set NY so (NY-1)%%%d == 0. Suggested: NY=%d or NY=%d\n",
                   (int)jp, (int)(((NY-1)/jp)*jp + 1), (int)((((NY-1)/jp)+1)*jp + 1));
        }
        FatalUnavoidable(myid, "grid-gpu-mismatch",
            "NY-1 not divisible by jp (見上方 hint)。");
    }
    if (myid == 0) {
        printf("[CHECK] Grid-GPU OK: NY=%d, jp=%d, NYD6=%d, "
               "global interior j = jp*(NYD6-7) = %d*%d = %d = (NY-1) ✓\n",
               (int)NY, (int)jp, (int)NYD6,
               (int)jp, (int)(NYD6-7), (int)(jp*(NYD6-7)));
    }

    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );

    AllocateMemory();

    // Allocate time-average accumulation arrays early (before possible VTK restart read)
    {
        size_t nTotal = (size_t)NX6 * NYD6 * NZ6;
        u_tavg_h = (double*)calloc(nTotal, sizeof(double));
        v_tavg_h = (double*)calloc(nTotal, sizeof(double));
        w_tavg_h = (double*)calloc(nTotal, sizeof(double));
        ox_tavg_h = (double*)calloc(nTotal, sizeof(double));
        oy_tavg_h = (double*)calloc(nTotal, sizeof(double));
        oz_tavg_h = (double*)calloc(nTotal, sizeof(double));
        accu_count = 0;
    }

    //pre-check whether the directories exit or not
    PreCheckDir();
    CreateDataType();

    // [P2] MPI Persistent Communication 初始化
    // 預建 4 個 persistent request，每步只需 MPI_Startall + MPI_Waitall
    // 必須在 AllocateMemory() 之後（buffer 已配置）且在主迴圈之前
    MPI_Persistent_Init(
        mpi_send_buf_left_d,  mpi_send_buf_right_d,
        mpi_recv_buf_left_d,  mpi_recv_buf_right_d,
        req_persist);

    // [P2-macro] Macro MPI Persistent Communication 初始化
    MPI_Persistent_Init_Macro(
        macro_send_buf_left_d,  macro_send_buf_right_d,
        macro_recv_buf_left_d,  macro_recv_buf_right_d,
        req_persist_macro);

    // ════════════════════════════════════════════════════════════════
    //  Stage 1: 外部網格讀取 (取代舊的 GenerateMesh_Y / GenerateMesh_Z)
    //
    //  ── GRID PIPELINE REGULATION (規範) ─────────────────────────────
    //
    //  Production (本 stage 唯一接觸的路徑):
    //    J_Frohlich/grid_zeta_tool.py    ← main 偵測到網格不存在/過期時呼叫
    //    J_Frohlich/adaptive_*.dat        ← main 讀取的網格檔
    //    J_Frohlich/grid_data_*.txt       ← 配套診斷檔
    //
    //  Phase 1/2 (與本 stage 完全隔離, main 永不觸碰):
    //    phase1_generategrid/grid_zeta_tool.py
    //        → 獨立工具, 輸出留在 phase1_generategrid/, 僅供 Phase 2 使用
    //    phase2_generatecheckpoint/interp_checkpoint.py
    //        → 唯一讀取 phase1_generategrid/ 輸出的程式
    //
    //  兩條 pipeline 的目的不同, 切勿混用 (執行路徑 / 輸出目錄 / 工具腳本).
    //
    //  檔名約定 (Python 與 C 必須一致):
    //    adaptive_<grid_stem>_I<NY>_J<NZ>_g<GAMMA>_a<ALPHA>.dat
    //    grid_data_I<NY>_J<NZ>_g<GAMMA>_a<ALPHA>.txt
    // ════════════════════════════════════════════════════════════════

    // 1.1 啟動前 Guard: 檢查外部網格檔案是否存在 + 新鮮度檢查
    {
        // 從 GRID_DAT_REF 擷取 stem (去掉 .dat 副檔名)
        char grid_ref_stem[256];
        strncpy(grid_ref_stem, GRID_DAT_REF, sizeof(grid_ref_stem) - 1);
        grid_ref_stem[sizeof(grid_ref_stem) - 1] = '\0';
        { char *ext = strrchr(grid_ref_stem, '.'); if (ext) *ext = '\0'; }

        char grid_dat_path[512];
        snprintf(grid_dat_path, sizeof(grid_dat_path),
                 "%s/adaptive_%s_I%d_J%d_g%.2f_a%.1f.dat",
                 GRID_DAT_DIR, grid_ref_stem,
                 NY, NZ, (double)GAMMA, (double)ALPHA);

        // need_generate: 0=OK, 1=missing, 2=stale(input newer), 3=diagnostics missing
        int need_generate = 0;
        FILE *grid_test = fopen(grid_dat_path, "r");

        if (!grid_test) {
            need_generate = 1;
        } else {
            fclose(grid_test);
            // 新鮮度: 全部輸入依賴 vs 格點檔 mtime
            struct stat grid_st, dep_st;
            if (stat(grid_dat_path, &grid_st) == 0) {
                const char *deps[] = {
                    "grid_params.py",
                    GRID_DAT_DIR "/grid_zeta_tool.py",
                    GRID_DAT_DIR "/" GRID_DAT_REF,
#ifdef UTAU_BOT_DAT
                    GRID_DAT_DIR "/" UTAU_BOT_DAT,
#endif
#ifdef UTAU_TOP_DAT
                    GRID_DAT_DIR "/" UTAU_TOP_DAT,
#endif
                    NULL
                };
                for (int d = 0; deps[d]; d++) {
                    int sr = stat(deps[d], &dep_st);
                    if (sr != 0) {
                        // 依賴檔遺失 → 觸發 regen, 讓 --auto fail loud
                        need_generate = 2;
                        if (myid == 0)
                            fprintf(stderr, "[GRID] dep missing: %s\n", deps[d]);
                    } else if (dep_st.st_mtime > grid_st.st_mtime) {
                        need_generate = 2;
                        if (myid == 0)
                            fprintf(stderr, "[GRID] stale: %s is newer\n", deps[d]);
                    }
                }
                // base topology: 掃描同 grid_key 同尺寸的其他 adaptive grid
                if (!need_generate) {
                    char prefix[512];
                    snprintf(prefix, sizeof(prefix),
                             "adaptive_%s_I%d_J%d_g%.2f_", grid_ref_stem, NY, NZ, (double)GAMMA);
                    int pfx_len = (int)strlen(prefix);
                    DIR *dp = opendir(GRID_DAT_DIR);
                    if (dp) {
                        struct dirent *ent;
                        char fpath[512];
                        while ((ent = readdir(dp)) != NULL) {
                            if (strncmp(ent->d_name, prefix, pfx_len) != 0) continue;
                            snprintf(fpath, sizeof(fpath), "%s/%s",
                                     GRID_DAT_DIR, ent->d_name);
                            if (strcmp(fpath, grid_dat_path) == 0) continue;
                            if (stat(fpath, &dep_st) == 0 &&
                                dep_st.st_mtime > grid_st.st_mtime) {
                                need_generate = 2;
                                if (myid == 0)
                                    fprintf(stderr, "[GRID] stale: base topology %s is newer\n",
                                            ent->d_name);
                                break;
                            }
                        }
                        closedir(dp);
                    }
                }
            }
            // 診斷檔檢查: grid_data 不存在 → 補齊
            if (!need_generate) {
                char diag_path[512];
                snprintf(diag_path, sizeof(diag_path),
                         "%s/grid_data_I%d_J%d_g%.2f_a%.1f.txt",
                         GRID_DAT_DIR, NY, NZ, (double)GAMMA, (double)ALPHA);
                if (stat(diag_path, &dep_st) != 0)
                    need_generate = 3;
            }
        }

        if (need_generate) {
            if (myid == 0) {
                fprintf(stderr, "\n");
                fprintf(stderr, "╔══════════════════════════════════════════════════════════╗\n");
                if (need_generate == 1)
                    fprintf(stderr, "║  Grid NOT FOUND — auto-generating ...                  ║\n");
                else if (need_generate == 2)
                    fprintf(stderr, "║  Grid STALE (input dependency newer) — regenerating ... ║\n");
                else
                    fprintf(stderr, "║  Grid OK, diagnostics missing — regenerating ...        ║\n");
                fprintf(stderr, "║  Target: %s\n", grid_dat_path);
                fprintf(stderr, "║  NY=%d, NZ=%d, ALPHA=%.1f, REF=%s\n",
                        NY, NZ, (double)ALPHA, GRID_DAT_REF);
                fprintf(stderr, "╚══════════════════════════════════════════════════════════╝\n");

                char cmd[1024];
                snprintf(cmd, sizeof(cmd),
                         "python3 %s/grid_zeta_tool.py --auto", GRID_DAT_DIR);
                fprintf(stderr, "  Running: %s\n", cmd);
                int ret = system(cmd);
                if (ret != 0) {
                    fprintf(stderr, "\n");
                    fprintf(stderr, "╔══════════════════════════════════════════════════════════╗\n");
                    fprintf(stderr, "║  FATAL: Python grid generation failed (exit=%d)         ║\n", ret);
                    fprintf(stderr, "║  Check: %s/grid_zeta_tool.py\n", GRID_DAT_DIR);
                    fprintf(stderr, "╚══════════════════════════════════════════════════════════╝\n");
                    MPI_Abort(MPI_COMM_WORLD, 1);
                }
            }
            MPI_Barrier(MPI_COMM_WORLD);

            FILE *grid_verify = fopen(grid_dat_path, "r");
            if (!grid_verify) {
                if (myid == 0)
                    fprintf(stderr, "FATAL: Grid file not found after generation: %s\n",
                            grid_dat_path);
                MPI_Abort(MPI_COMM_WORLD, 1);
            }
            fclose(grid_verify);
        }
        if (myid == 0) printf("GRID: %s external grid: %s\n",
                              need_generate ? "Generated" : "Found", grid_dat_path);
    }

    // 1.2 生成均勻 x 座標 (不變)
    GenerateMesh_X();

    // 1.3 讀取外部二維 (y, z) 座標 (取代 GenerateMesh_Y + GenerateMesh_Z)
    ReadExternalGrid_YZ(y_2d_h, z_h, myid);

    // Mass correction uses physical control-volume weights from the curvilinear
    // y-z grid. Build before the later coordinate MPI exchange because
    // ReadExternalGrid_YZ still has the correct +/-LY periodic ghost offsets.
    InitializeMassCorrectionWeights();

    // 初始化 monitor RS 代表點 (需在座標填充後)
    InitMonitorCheckPoint();
    // 配置 pinned host buffer (消除 ComputeMaMax 的 malloc/free 碎片化)
    InitMonitorBuffers();

    // ════════════════════════════════════════════════════════════════
    //  Stage 2: 度量項計算 (完整 2×2 Jacobian)
    // ════════════════════════════════════════════════════════════════

    // 2.1 計算 4 個正 Jacobian + 行列式求逆 → 4 個逆 Jacobian
    ComputeMetricTerms_Full(y_xi_h, y_zeta_h, z_xi_h, z_zeta_h,
                            J_2D_h, xi_y_h, xi_z_h, zeta_y_h, zeta_z_h,
                            y_2d_h, z_h, NYD6, NZ6);

    // 2.2 MPI 交換逆 Jacobian + 座標 ghost zones
    {
        int ghost_count = Buffer * NZ6;
        int j_send_left  = (Buffer + 1) * NZ6;
        int j_recv_right = (NYD6 - Buffer) * NZ6;
        int j_send_right = (NYD6 - 2*Buffer - 1) * NZ6;
        int j_recv_left  = 0;
        int tag = 600;

        // 交換 6 個陣列: xi_y, xi_z, zeta_y, zeta_z, y_2d, z
        double *arrays_to_exchange[] = { xi_y_h, xi_z_h, zeta_y_h, zeta_z_h, y_2d_h, z_h };
        int n_arrays = 6;
        for (int a = 0; a < n_arrays; a++) {
            MPI_Sendrecv(&arrays_to_exchange[a][j_send_left],  ghost_count, MPI_DOUBLE, l_nbr, tag,
                         &arrays_to_exchange[a][j_recv_right], ghost_count, MPI_DOUBLE, r_nbr, tag,
                         MPI_COMM_WORLD, MPI_STATUS_IGNORE);
            tag++;
            MPI_Sendrecv(&arrays_to_exchange[a][j_send_right], ghost_count, MPI_DOUBLE, r_nbr, tag,
                         &arrays_to_exchange[a][j_recv_left],  ghost_count, MPI_DOUBLE, l_nbr, tag,
                         MPI_COMM_WORLD, MPI_STATUS_IGNORE);
            tag++;
        }
        if (myid == 0) printf("GILBM: Metric terms + coordinates ghost zones exchanged.\n");
    }

    // 2.3 全 Jacobian 版度量項診斷 (使用外部網格實際計算的度量項)
    DiagnoseMetricTerms_Full(y_xi_h, y_zeta_h, z_xi_h, z_zeta_h,
                             J_2D_h, xi_y_h, xi_z_h, zeta_y_h, zeta_z_h,
                             y_2d_h, z_h, NYD6, NZ6, myid);

    // ════════════════════════════════════════════════════════════════
    //  Stage 3: Global Time Step 
    // ════════════════════════════════════════════════════════════════

    double dx_val = LX / (double)(NX6 - 7);

    // Phase 3: Global time step (遍歷全場全編號 η/ξ/ζ 逆變速度最大值)
    double dt_rank = ComputeGlobalTimeStep(xi_y_h, xi_z_h, zeta_y_h, zeta_z_h,
                                           dx_val, NYD6, NZ6, CFL, myid, nProcs);
    CHECK_MPI( MPI_Allreduce(&dt_rank, &dt_global, 1, MPI_DOUBLE, MPI_MIN, MPI_COMM_WORLD) );

    omega_global   = (3.0 * niu / dt_global) + 0.5;
    omegadt_global = omega_global * dt_global;

    if (myid == 0) {
        printf("  ─────────────────────────────────────────────────────────\n");
        printf("  GAMMA = %.6f (tanh stretching parameter)\n", (double)GAMMA);
        printf("  minSize = %.6e (derived from GAMMA, wall-nearest spacing)\n", (double)minSize);
        printf("  dt_global = MIN(all ranks) = %.6e\n", dt_global);
        printf("  ratio dt_global / minSize = %.4f\n", dt_global / (double)minSize);
        printf("  Speedup cost: %.1fx more timesteps per physical time\n", (double)minSize / dt_global);
        printf("  omega_global = %.6f, 1/omega_global = %.6f\n", omega_global, 1.0 / omega_global);
        printf("  =============================================================\n\n");
    }

    // [REMOVED] PrecomputeGILBM_DeltaAll + MPI delta_xi exchange + ValidateDepartureCFL
    // 2026-04 重構: δη, δξ, δζ 全部移至 Step1 kernel 即時計算。
    // CFL 驗證由 ComputeGlobalTimeStep 本身保證 (dt = λ/max|c̃|)。

    // Precompute stencil base k (wall-clamped)
    PrecomputeGILBM_StencilBaseK(bk_precomp_h, NZ6);

    // ════════════════════════════════════════════════════════════════
    //  Upload to GPU
    // ════════════════════════════════════════════════════════════════
    // 逆 Jacobian 度量項
    CHECK_CUDA( cudaMemcpy(xi_y_d,   xi_y_h,   NYD6*NZ6*sizeof(double), cudaMemcpyHostToDevice) );
    CHECK_CUDA( cudaMemcpy(xi_z_d,   xi_z_h,   NYD6*NZ6*sizeof(double), cudaMemcpyHostToDevice) );
    CHECK_CUDA( cudaMemcpy(zeta_y_d, zeta_y_h, NYD6*NZ6*sizeof(double), cudaMemcpyHostToDevice) );
    CHECK_CUDA( cudaMemcpy(zeta_z_d, zeta_z_h, NYD6*NZ6*sizeof(double), cudaMemcpyHostToDevice) );
    // 正 Jacobian ∂z/∂ζ → GPU（WENO7 stretch factor R 直接使用）
    CHECK_CUDA( cudaMemcpy(z_zeta_d, z_zeta_h, NYD6*NZ6*sizeof(double), cudaMemcpyHostToDevice) );
    // 座標
    CHECK_CUDA( cudaMemcpy(y_2d_d, y_2d_h, NYD6*NZ6*sizeof(double), cudaMemcpyHostToDevice) );
    // [REMOVED] delta_xi_d, delta_zeta_d GPU uploads — no longer exist
    // [REMOVED] GILBM_delta_eta, GILBM_L_eta_precomp __constant__ uploads — on-the-fly in kernel
    // __constant__ symbols: dt_global + inv_dx (新增)
    CHECK_CUDA( cudaMemcpyToSymbol(GILBM_dt, &dt_global, sizeof(double)) );
    {
        double inv_dx_val = (double)(NX6 - 7) / LX;
        double L_eta_shared_h[2][7];
        PrecomputeGILBM_EtaSharedWeights(L_eta_shared_h, dt_global, inv_dx_val);

        double eta_max_coeff_abs = 0.0;
        double eta_max_interp_abs = 0.0;
        VerifyGILBM_EtaSharedWeights(L_eta_shared_h, dt_global, inv_dx_val,
                                     &eta_max_coeff_abs, &eta_max_interp_abs);
        if (myid == 0) {
            printf("GILBM eta shared weights verification:\n");
            printf("  max coeff diff = %.17e\n", eta_max_coeff_abs);
            printf("  max interp diff = %.17e\n", eta_max_interp_abs);
        }
        if (eta_max_coeff_abs > 1.0e-12 || eta_max_interp_abs > 1.0e-12) {
            if (myid == 0) {
                fprintf(stderr,
                    "ERROR: shared eta weight verification failed: coeff=%.17e interp=%.17e\n",
                    eta_max_coeff_abs, eta_max_interp_abs);
            }
            MPI_Abort(MPI_COMM_WORLD, 43);
        }

        CHECK_CUDA( cudaMemcpyToSymbol(GILBM_inv_dx, &inv_dx_val, sizeof(double)) );
        CHECK_CUDA( cudaMemcpyToSymbol(GILBM_L_eta_shared, L_eta_shared_h, sizeof(L_eta_shared_h)) );
        if (myid == 0) {
            printf("GILBM-GTS: inv_dx = %.8e and shared eta weights -> __constant__ memory.\n",
                   inv_dx_val);
        }
    }
    // Precomputed stencil base k → GPU
    CHECK_CUDA( cudaMemcpy(bk_precomp_d, bk_precomp_h, NZ6*sizeof(int), cudaMemcpyHostToDevice) );

#if USE_MRT
    // Phase 3.5: MRT nonequilibrium projection tables -> __constant__ memory
    {
        Matrix;           // MRT_Matrix.h -> double M[19][19] = { ... };
        Inverse_Matrix;   // MRT_Matrix.h -> double Mi[19][19] = { ... };
        double s_visc_val_mrt = 1.0 / omega_global;
        double GILBM_MRT_K_h[19][19];
        double GILBM_MRT_Fproj_h[19];
        double GILBM_MRT_Fproj_u_h[19];
        double GILBM_MRT_Fproj_v_h[19];
        double GILBM_MRT_Fproj_w_h[19];

        BuildMrtProjectionTablesHost(M, Mi, s_visc_val_mrt,
                                     GILBM_MRT_K_h,
                                     GILBM_MRT_Fproj_h,
                                     GILBM_MRT_Fproj_u_h,
                                     GILBM_MRT_Fproj_v_h,
                                     GILBM_MRT_Fproj_w_h);

        MrtProjectionVerification mrt_v =
            VerifyMrtProjectionHost(M, Mi,
                                    GILBM_MRT_K_h,
                                    GILBM_MRT_Fproj_h,
                                    GILBM_MRT_Fproj_u_h,
                                    GILBM_MRT_Fproj_v_h,
                                    GILBM_MRT_Fproj_w_h,
                                    s_visc_val_mrt,
                                    dt_global);

        if (myid == 0) {
            printf("GILBM-MRT projection verification (%d samples):\n", mrt_v.samples);
            printf("  max |Mi*M-I|                 = %.17e\n", mrt_v.max_identity_error);
            printf("  max |M*feq-meq|              = %.17e\n", mrt_v.max_equilibrium_moment_error);
            printf("  max conserved |M_c*K|        = %.17e\n", mrt_v.max_conserved_relax_error);
            printf("  max Guo basis split diff     = %.17e\n", mrt_v.max_force_basis_error);
            printf("  max Guo projection split diff= %.17e\n", mrt_v.max_force_projection_error);
            printf("  max conserved force moment   = %.17e\n", mrt_v.max_force_moment_error);
            printf("  max collision abs diff       = %.17e\n", mrt_v.max_collision_abs_error);
            printf("  max collision rel diff       = %.17e\n", mrt_v.max_collision_rel_error);
        }
        if (mrt_v.max_identity_error > 1.0e-12 ||
            mrt_v.max_equilibrium_moment_error > 1.0e-12 ||
            mrt_v.max_conserved_relax_error > 1.0e-12 ||
            mrt_v.max_force_basis_error > 1.0e-12 ||
            mrt_v.max_force_projection_error > 1.0e-12 ||
            mrt_v.max_force_moment_error > 1.0e-12 ||
            mrt_v.max_collision_abs_error > 1.0e-12) {
            if (myid == 0) {
                fprintf(stderr, "ERROR: MRT projection verification failed strict 1e-12 tolerance.\n");
            }
            MPI_Abort(MPI_COMM_WORLD, 42);
        }

        CHECK_CUDA( cudaMemcpyToSymbol(GILBM_MRT_K, GILBM_MRT_K_h, sizeof(GILBM_MRT_K_h)) );
        CHECK_CUDA( cudaMemcpyToSymbol(GILBM_MRT_Fproj, GILBM_MRT_Fproj_h, sizeof(GILBM_MRT_Fproj_h)) );
        CHECK_CUDA( cudaMemcpyToSymbol(GILBM_MRT_Fproj_u, GILBM_MRT_Fproj_u_h, sizeof(GILBM_MRT_Fproj_u_h)) );
        CHECK_CUDA( cudaMemcpyToSymbol(GILBM_MRT_Fproj_v, GILBM_MRT_Fproj_v_h, sizeof(GILBM_MRT_Fproj_v_h)) );
        CHECK_CUDA( cudaMemcpyToSymbol(GILBM_MRT_Fproj_w, GILBM_MRT_Fproj_w_h, sizeof(GILBM_MRT_Fproj_w_h)) );

        if (myid == 0) {
            printf("GILBM-MRT: K and Guo forcing projections copied to __constant__ memory.\n");
        }
    }
#endif  // USE_MRT

    // ── GTS: 全場均一鬆弛常數存入 __constant__ memory ──
    // ★ BUG FIX: s_visc / omega 上傳原本被包在 #if USE_MRT 裡面，
    //   導致 BGK 模式 (COLLISION_MODE=0) 讀到未初始化的 __constant__。
    //   這些值 MRT/BGK 都需要 — 必須在 #if USE_MRT 外面。
    {
        double s_visc_val = 1.0 / omega_global;
        CHECK_CUDA( cudaMemcpyToSymbol(GILBM_s_visc_global, &s_visc_val, sizeof(double)) );
        CHECK_CUDA( cudaMemcpyToSymbol(GILBM_omega_global,  &omega_global, sizeof(double)) );
        if (myid == 0) {
            printf("GILBM-GTS: __constant__ uploaded:\n");
            printf("  s_visc_global  = 1/omega = %.8f\n", s_visc_val);
            printf("  omega_global   = %.8f\n", omega_global);
            printf("  dt (GILBM_dt)  = %.8e\n", dt_global);
#if USE_MRT
            printf("  collision mode = MRT (d'Humieres D3Q19)\n");
#else
            printf("  collision mode = BGK/SRT (Single Relaxation Time)\n");
#endif
        }
    }

    if (myid == 0) printf("GILBM: Jacobian + __constant__(dt,inv_dx) + bk_precomp copied to GPU.\n");
    if (myid == 0) printf("GILBM: FORCE_HERMITE_ORDER = %d\n", FORCE_HERMITE_ORDER);

    if ( g_init_runtime == 0 ) {
        printf("Initializing by default function...\n");
        InitialUsingDftFunc();
    } else if ( g_init_runtime == 1 ) {
        printf("Initializing by backup data...\n");
        result_readbin_velocityandf();
        if( TBINIT && TBSWITCH ) statistics_readbin_merged_stress();
    } else if ( g_init_runtime == 2 ) {
        // Phase 9: INIT=2 (merged VTK restart) removed.
        //   VTK path 僅能續跑速度場 (無 f, 無 cumulative stats) → 精度較差,
        //   且已被 Phase 2 atomic binary checkpoint (INIT=3) 完全取代。
        // [POLICY-C1] 走 FatalUnavoidable → exit 42 → 停鏈
        FatalUnavoidable(myid, "Phase9-INIT=2-removed",
            "INIT=2 (VTK restart) 已移除。請改用 --restart=<checkpoint_dir>。");
    } else if ( g_init_runtime == 3 ) {
        printf("Initializing from binary checkpoint: %s\n", g_restart_bin_dir);
        PrecheckCheckpointGridConsistency(g_restart_bin_dir, myid);
        LoadBinaryCheckpoint(g_restart_bin_dir);

        // ============================================================
        // Phase 4: INIT=3 tripwire (Phase 8 後: 來源改為 argv/--restart)
        // ------------------------------------------------------------
        // 防 --restart=<empty> / metadata.dat 解析失敗 造成 "INIT=3 但
        // 其實冷啟動" 的靜默錯誤 (會讓 FTT 從 0 重來)。
        //
        // 條件:
        //   (a) g_restart_bin_dir 為空 (ParseRuntimeArgs 已擋, 此處為雙保險)
        //   (b) LoadBinaryCheckpoint 回來後 restart_step 仍為 0
        //       (metadata.dat 的 step= 行必 > 0; == 0 代表未讀到)
        //
        // 依使用者規範: 絕不靜默冷啟動, 要 fail loud.
        //   若真的要冷啟動, 須用 --cold flag 或設 compile-time INIT=0。
        // ============================================================
        {
            bool bad_dir  = (g_restart_bin_dir == NULL) || (g_restart_bin_dir[0] == '\0');
            bool bad_step = (restart_step <= 0);
            // [REVIEW-FIX #3] 新增第三道閘: compile-time RESTART_BIN_DIR 吃不到 argv。
            //   若 INIT=3 但 g_restart_dir_set_by_argv == false,
            //   代表 jobscript 沒傳 --restart=<dir> (或 typo),
            //   而 variables.h 的 #define RESTART_BIN_DIR "step_4001" 仍然「有效」,
            //   舊版會靜默讀進 stale 步數 → solver 狀態大錯亂。
            //   這裡只在 chain-style 環境(CHAIN_COUNT>=2 對應 argv 必到)絕不接受。
            const char *env_cc = std::getenv("CHAIN_COUNT_FROM_JOBSCRIPT");
            bool chain_expected = (env_cc != NULL && env_cc[0] != '\0' && env_cc[0] != '1');
            bool compile_time_fallback =
                (g_init_runtime == 3) && (!g_restart_dir_set_by_argv) && chain_expected;

            if (bad_dir || bad_step || compile_time_fallback) {
                if (myid == 0) {
                    fprintf(stderr,
                        "\n[FATAL][Phase4-TRIPWIRE] INIT=3 requested but checkpoint not loaded.\n"
                        "  g_restart_bin_dir      = \"%s\"\n"
                        "  restart_step           = %d  (must be > 0)\n"
                        "  set_by_argv            = %s\n"
                        "  CHAIN_COUNT (env)      = \"%s\"\n"
                        "  Probable cause : --restart=<dir> path invalid, or\n"
                        "                   metadata.dat missing 'step=' line, or\n"
                        "                   directory was empty, or\n"
                        "                   jobscript forgot to pass --restart and\n"
                        "                   solver fell back to variables.h RESTART_BIN_DIR.\n"
                        "  Policy         : NEVER silently cold-start on INIT=3, and\n"
                        "                   NEVER trust compile-time RESTART_BIN_DIR in\n"
                        "                   chain mode. Chain must pass --restart=<dir>.\n"
                        "                   Intentional cold start = --cold flag.\n",
                        (g_restart_bin_dir ? g_restart_bin_dir : "(null)"),
                        restart_step,
                        (g_restart_dir_set_by_argv ? "true" : "false"),
                        (env_cc ? env_cc : "(unset)"));
                    fflush(stderr);
                }
                // [POLICY-C1] Tripwire 命中 = 不可避免錯誤(restart 資料不對 / argv 錯 /
                //   configurtion 錯),續跑也修不好。改用 MPI_Abort(42) → 停鏈。
                CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );
                MPI_Abort(MPI_COMM_WORLD, EXIT_CODE_UNAVOIDABLE);
            }
            if (myid == 0) {
                printf("[Phase4-TRIPWIRE] OK  g_restart_bin_dir=\"%s\"  restart_step=%d  set_by_argv=%s\n",
                       g_restart_bin_dir, restart_step,
                       (g_restart_dir_set_by_argv ? "true" : "false"));
            }

            // ========================================================
            // Phase 6: run-log 截斷到 step <= restart_step
            //   避免 (step N → crash → step N-K restart) 造成 log 重疊
            //   只在 rank 0 動檔案, 其他 rank 由 MPI_Barrier 等待。
            //   restart_FTT = restart_step * dt_global / flow_through_time
            // ========================================================
            {
                double restart_FTT = (double)restart_step * dt_global
                                   / (double)flow_through_time;
                TruncateAllLogsOnRestart(myid, restart_step, restart_FTT);
                CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );
            }
        }
    }

    // Force sanity guard (applies to all restart paths: INIT=1 merged binary, INIT=3 atomic checkpoint)
    if (g_init_runtime > 0) {
        if (std::isnan(Force_h[0]) || std::isinf(Force_h[0])) {
            if (myid == 0) printf("[FORCE-GUARD] Invalid Force=%.5E (NaN/Inf), reset to 0\n", Force_h[0]);
            Force_h[0] = 0.0;
            CHECK_CUDA( cudaMemcpy(Force_d, Force_h, sizeof(double), cudaMemcpyHostToDevice) );
        }
        // ----------------------------------------------------------------
        // [FIX 2026-04] 移除 "Force < 0 → 0" 的 clamp。
        //
        // 原因: Periodic Hill 在 SIMPLE-PROP 控制下, 當 Ub 衝過 Uref (OVERSHOOT)
        //       時, 控制器會暫時把 Force 推成負值來煞車 (等效反向 body force,
        //       物理合法)。此為控制器正常運作狀態。
        //
        // 舊行為: checkpoint 若存到 OVERSHOOT 區的 Force=-4.5E-5, restart 後
        //         被 clamp 成 0, 導致:
        //           (a) Force 階躍 → F* vs FTT 圖出現明顯不連續
        //           (b) Ub 失去煞車, 需要好幾步才能重新把 Force 壓回負值
        //           (c) 污染後續統計窗口
        //
        // NaN/Inf guard 保留 (那是真正的損壞資料)。
        // ----------------------------------------------------------------

        // ----------------------------------------------------------------
        // [FIX 2026-04-16] 移除 restart-time ANTI-WINDUP cap。
        //
        // 根因: SIMPLE-PROP (FORCE_CTRL_MODE=0) 正常運行 Force 可達
        //   ±4.6E-05 (1100× F_Poiseuille), 但 restart cap 僅允許
        //   ±4.1E-06 (100× F_Poiseuille) → 每次 restart 都被截斷
        //   → Round 間 Force 不連續 → Ub 需數千步重新收斂。
        //
        // 分析:
        //   Mode 0 (SIMPLE-PROP): 運行時無 cap → restart-time cap 不一致
        //   Mode 1 (PID/Gehrke):  運行時有自己的 FORCE_CAP_MULT cap
        //                          → restart-time cap 冗餘
        //   → 兩種模式下此 cap 都不該存在。
        //
        // 保留: NaN/Inf guard (上方 line 615-618) 仍在, 那是真正的損壞偵測。
        // 若未來需要 restart cap, 應使用與 evolution.h 相同的 cap 基底,
        // 或直接讓 evolution.h 第一步 force-update 做 clamp。
        // ----------------------------------------------------------------
        if (g_init_runtime == 1 || g_init_runtime == 3) {
            if (myid == 0) {
                printf("[FORCE-RESTART] Loaded Force=%.6E from checkpoint (no restart cap applied)\n",
                       Force_h[0]);
            }
        }
    }

    // ---- Perturbation injection: break spanwise symmetry to trigger 3D turbulence ----//加入擾動量
    // 使用 additive δfeq 方法: f[q] += feq(ρ, u+δu) - feq(ρ, u)
    // 保留已發展流場的非平衡部分 (viscous stress), 只注入速度擾動
#if PERTURB_INIT
    {
        double e_lbm[19][3] = {
            {0,0,0},{1,0,0},{-1,0,0},{0,1,0},{0,-1,0},{0,0,1},{0,0,-1},
            {1,1,0},{-1,1,0},{1,-1,0},{-1,-1,0},{1,0,1},{-1,0,1},{1,0,-1},
            {-1,0,-1},{0,1,1},{0,-1,1},{0,1,-1},{0,-1,-1}};
        double W_lbm[19] = {
            1.0/3,  1.0/18, 1.0/18, 1.0/18, 1.0/18, 1.0/18, 1.0/18,
            1.0/36, 1.0/36, 1.0/36, 1.0/36, 1.0/36, 1.0/36, 1.0/36,
            1.0/36, 1.0/36, 1.0/36, 1.0/36, 1.0/36};

        double amp = (PERTURB_PERCENT / 100.0) * (double)Uref;
        // 每個 rank 用不同的 seed → 不同的擾動 pattern
        srand(42 + myid * 13579);

        int count = 0;
        for (int j = 3; j < NYD6 - 3; j++)
        for (int k = 3; k < NZ6 - 3; k++)
        for (int i = 3; i < NX6 - 3; i++) { //遍歷每一個物理空間計算點 
            int index = j * NX6 * NZ6 + k * NX6 + i;

            // 壁面距離 envelope: sin(π·z_norm), 壁面=0, 中心=1
            double z_bot  = z_h[j * NZ6 + 3];
            double z_top  = z_h[j * NZ6 + (NZ6 - 4)];
            double z_norm = (z_h[j * NZ6 + k] - z_bot) / (z_top - z_bot);
            double envelope = sin(pi * z_norm);

            // 三分量隨機擾動 [-amp, +amp] × envelope
            double du = amp * envelope * (2.0 * rand() / (double)RAND_MAX - 1.0);
            double dv = amp * envelope * (2.0 * rand() / (double)RAND_MAX - 1.0);
            double dw = amp * envelope * (2.0 * rand() / (double)RAND_MAX - 1.0);

            double rho_p = rho_h_p[index];
            double u_old = u_h_p[index], v_old = v_h_p[index], w_old = w_h_p[index];
            double u_new = u_old + du,    v_new = v_old + dv,    w_new = w_old + dw;

            // 更新宏觀速度
            u_h_p[index] = u_new;
            v_h_p[index] = v_new;
            w_h_p[index] = w_new;

            // Additive δfeq: 保留 f_neq, 只加入擾動的平衡態差值
            //S_{i}= (feq(ρ, u+δu) - feq(ρ, u)) 相當於一個外力進去，理論根據 : Kupershtokh2004-
            double udot_old = u_old * u_old + v_old * v_old + w_old * w_old;
            double udot_new = u_new * u_new + v_new * v_new + w_new * w_new;
            for (int q = 0; q < 19; q++) {
                double eu_old = e_lbm[q][0]*u_old + e_lbm[q][1]*v_old + e_lbm[q][2]*w_old;
                double eu_new = e_lbm[q][0]*u_new + e_lbm[q][1]*v_new + e_lbm[q][2]*w_new;
                double feq_old = W_lbm[q] * rho_p * (1.0 + 3.0*eu_old + 4.5*eu_old*eu_old - 1.5*udot_old);
                double feq_new = W_lbm[q] * rho_p * (1.0 + 3.0*eu_new + 4.5*eu_new*eu_new - 1.5*udot_new);
                fh_p[q][index] += (feq_new - feq_old);
            }
            count++;
        }
        if (myid == 0)
            printf("Perturbation injected: amp=%.2e (%d%% Uref), %d interior points/rank, envelope=sin(pi*z_norm)\n",
                   amp, (int)PERTURB_PERCENT, count);
    }
#endif

    // Phase 1.5 acceptance diagnostic: Jacobian-based CFL check, C-E BC spot-check
    DiagnoseGILBM_Phase1(xi_y_h, xi_z_h, zeta_y_h, zeta_z_h, fh_p, NYD6, NZ6, myid, dt_global, g_init_runtime);

    SendDataToGPU();

    // === Ub integration self-test (runs every startup, aborts on failure) ===
    {
        int ub_test_fail = 0;
        if (myid == 0) {
            // Test 1: Σ dx_cell × dz_cell 面積驗證
            // (a) 望遠鏡和 = 格點邊界差 (純算術恆等式, 容差 1e-12)
            // (b) 與 LX × (LZ - H_HILL) 比較 (外部網格可能有微小不匹配, 容差 1e-3)
            // k=3..NZ6-4: Frohlich J=0..NZ-1 (NZ=64 nodes at k=3..66=NZ6-4)
            //   cell integration: k=3..NZ6-5, 每格用 z[k+1]-z[k], 最後一格 k=NZ6-5→k+1=NZ6-4=頂壁
            double A_sum = 0.0;
            for (int k = 3; k < NZ6-4; k++)
            for (int i = 3; i < NX6-4; i++)
                A_sum += (x_h[i+1] - x_h[i]) * (z_h[3*NZ6+k+1] - z_h[3*NZ6+k]);

            // (a) 望遠鏡恆等式: A_sum 必須 = (x_right - x_left) × (z_top - z_bot)
            double A_tele = (x_h[NX6-4] - x_h[3]) * (z_h[3*NZ6+NZ6-4] - z_h[3*NZ6+3]);
            double tele_err = (A_tele > 0.0) ? fabs(A_sum - A_tele) / A_tele : fabs(A_sum);
            printf("[Ub-CHECK] Test 1a — Telescoping: Sum=%.12f  Tele=%.12f  err=%.2e  %s\n",
                   A_sum, A_tele, tele_err, (tele_err < 1e-12) ? "PASS" : "FAIL");
            if (tele_err >= 1e-12) {
                fprintf(stderr, "[FATAL] Ub telescoping sum identity failed: grid array corrupted.\n");
                ub_test_fail = 1;
            }

            // (b) 與期望值比較 (外部網格容差放寬)
            double A_expected = LX * (LZ - H_HILL);
            double grid_err = fabs(A_sum - A_expected) / A_expected;
            printf("[Ub-CHECK] Test 1b — vs LX*(LZ-H): Sum=%.12f  Expected=%.12f  rel_err=%.2e  %s\n",
                   A_sum, A_expected, grid_err, (grid_err < 1e-3) ? "PASS" : "FAIL");
            // 打印實際非因次尺寸，方便使用者驗證
            double z_bot_j3 = z_h[3*NZ6+3];
            double z_top_j3 = z_h[3*NZ6+NZ6-4];  // k=NZ6-4=66 = Frohlich J=NZ-1 (頂壁)
            printf("[Ub-CHECK]   z_bot(j=3)=%.6f (expect H_HILL=%.1f)  z_top(j=3)=%.6f (expect LZ=%.3f)\n",
                   z_bot_j3, (double)H_HILL, z_top_j3, (double)LZ);
            if (grid_err >= 1e-3) {
                fprintf(stderr, "[FATAL] Ub area vs expected mismatch >0.1%%: grid scaling or LZ parameter wrong.\n");
                ub_test_fail = 1;
            }

            // Test 2: Uniform v=Uref → Ub must = Uref (以 A_sum 正規化)
            double Ub_test = 0.0;
            for (int k = 3; k < NZ6-4; k++)
            for (int i = 3; i < NX6-4; i++)
                Ub_test += (double)Uref * (x_h[i+1] - x_h[i]) * (z_h[3*NZ6+k+1] - z_h[3*NZ6+k]);
            Ub_test /= A_sum;   // 使用實際格點面積正規化
            double uniform_err = fabs(Ub_test - (double)Uref) / (double)Uref;
            printf("[Ub-CHECK] Test 2 — Uniform: Ub=%.15f  Uref=%.15f  rel_err=%.2e  %s\n",
                   Ub_test, (double)Uref, uniform_err, (uniform_err < 1e-12) ? "PASS" : "FAIL");
            if (uniform_err >= 1e-12) {
                fprintf(stderr, "[FATAL] Ub uniform field test failed: integration formula or normalization bug.\n");
                ub_test_fail = 1;
            }

            // Test 3: Actual field Ub (sanity: 0 < U* < 2)
            double Ub_actual = 0.0;
            for (int k = 3; k < NZ6-4; k++)
            for (int i = 3; i < NX6-4; i++) {
                double v00 = v_h_p[3*NX6*NZ6 + k*NX6 + i];
                double v10 = v_h_p[3*NX6*NZ6 + (k+1)*NX6 + i];
                double v01 = v_h_p[3*NX6*NZ6 + k*NX6 + (i+1)];
                double v11 = v_h_p[3*NX6*NZ6 + (k+1)*NX6 + (i+1)];
                double v_cell = (v00 + v10 + v01 + v11) / 4.0;
                Ub_actual += v_cell * (x_h[i+1] - x_h[i]) * (z_h[3*NZ6+k+1] - z_h[3*NZ6+k]);
            }
            Ub_actual /= A_sum;   // 使用實際格點面積正規化
            double Ustar_actual = Ub_actual / (double)Uref;
            int t3_ok = (Ub_actual >= 0.0 && Ustar_actual < 2.0) || (g_init_runtime == 0);  // cold start: Ub=0 is OK
            printf("[Ub-CHECK] Test 3 — Field:   Ub=%.10f  U*=%.6f  %s\n",
                   Ub_actual, Ustar_actual, t3_ok ? "PASS" : "WARNING");
            if (!t3_ok) {
                fprintf(stderr, "[WARNING] Ub field test: U*=%.4f outside [0,2). VTK data may be corrupted.\n", Ustar_actual);
                // Warning only — don't abort (flow might just not have developed yet)
            }

            // 存儲實際截面面積供後續 monitor / restart Ub 計算使用
            A_cross_j3 = A_sum;
        }
        MPI_Bcast(&A_cross_j3, 1, MPI_DOUBLE, 0, MPI_COMM_WORLD);
        MPI_Bcast(&ub_test_fail, 1, MPI_INT, 0, MPI_COMM_WORLD);
        if (ub_test_fail) {
            if (myid == 0) fprintf(stderr, "[FATAL] Ub self-test FAILED. Aborting.\n");
            MPI_Abort(MPI_COMM_WORLD, 1);
        }
    }

    // GILBM initialization: feq, f_post
    {
        dim3 init_block(8, 8, 4);
        dim3 init_grid((NX6 + init_block.x - 1) / init_block.x,
                       (NYD6 + init_block.y - 1) / init_block.y,
                       (NZ6 + init_block.z - 1) / init_block.z);

        // [方案A] Init_Feq_Kernel 已移除 — feq_d 不再配置，collision 自算 feq

        // GTS: Init_FPost_Kernel — 簡單 19 copy per point
        Algorithm1_Step0Kernel_GTS<<<init_grid, init_block>>>(
            fd[0], fd[1], fd[2], fd[3], fd[4], fd[5], fd[6], fd[7], fd[8], fd[9],
            fd[10], fd[11], fd[12], fd[13], fd[14], fd[15], fd[16], fd[17], fd[18],
            f_post_d
        );
        CHECK_CUDA( cudaDeviceSynchronize() );

        // [方案B] 初始化雙緩衝指標:
        //   第一個 sub-step 讀 f_post_d (剛初始化), 寫 f_post_d2
        //   每步結束後 swap → 下一步讀上一步的 output
        f_post_read  = f_post_d;
        f_post_write = f_post_d2;
        if (myid == 0) printf("GILBM ALG1 GTS: f_post initialized (double-buffer).\n");
    }
    
    // ---- GILBM Initialization Parameter Summary ----
    {
        if (myid == 0) {
            printf("\n+================================================================+\n");
            printf("| GILBM Initialization Parameter Summary (GTS)                   |\n");
            printf("+================================================================+\n");
            printf("| [Input]  Re               = %d\n", (int)Re);
            printf("| [Input]  Uref             = %.6f\n", (double)Uref);
            printf("| [Output] niu              = %.6e\n", (double)niu);
            printf("+----------------------------------------------------------------+\n");
            printf("| [Output] dt_global        = %.6e\n", dt_global);
            printf("|   -> Omega                = 3*niu/dt + 0.5 = %.6f\n", omega_global);
            printf("|   -> tau (omegadt)        = 3*niu + 0.5*dt = %.6e\n", omegadt_global);
            printf("+================================================================+\n\n");
        }

        // ── GTS Runtime Diagnostic ──
        if (myid == 0) {
            size_t grid_size_diag = (size_t)NX6 * NYD6 * NZ6;
            size_t collision_buf_bytes = 19ULL * grid_size_diag * sizeof(double);
            printf("  GTS collision buffer: f_post×2 = %.1f MB/rank (double-buffer)\n",
                   2.0 * collision_buf_bytes / 1048576.0);
        }
    }
    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );

    // Restore time-average from restart (if available)
    if (restart_step > 0 && accu_count > 0) {
        const size_t nTotal = (size_t)NX6 * NYD6 * NZ6;
        const size_t tavg_bytes = nTotal * sizeof(double);
        // Phase 9: INIT=1 (merged binary) + INIT=3 (atomic checkpoint) 都存
        //   raw cumulative sums → 直接上 GPU, 無需 scale。VTK 路徑 (INIT=2)
        //   的 ×accu_count hack 已隨 InitFromMergedVTK 一併移除。
        // Copy accumulated sums to GPU
        CHECK_CUDA( cudaMemcpy(u_tavg_d, u_tavg_h, tavg_bytes, cudaMemcpyHostToDevice) );
        CHECK_CUDA( cudaMemcpy(v_tavg_d, v_tavg_h, tavg_bytes, cudaMemcpyHostToDevice) );
        CHECK_CUDA( cudaMemcpy(w_tavg_d, w_tavg_h, tavg_bytes, cudaMemcpyHostToDevice) );
        if (myid == 0)
            printf("Statistics restored: accu_count=%d, copied to GPU (%.1f MB each). [%s]\n",
                   accu_count, tavg_bytes / 1.0e6, (g_init_runtime==3) ? "atomic" : "merged");
        stage1_announced = true;
    } else {
        if (myid == 0) {
            size_t nTotal = (size_t)NX6 * NYD6 * NZ6;
            printf("Time-average arrays allocated (%.1f MB each), starting fresh.\n",
                   nTotal * sizeof(double) / 1.0e6);
        }
    }

    // Restore Reynolds stress from merged binary (INIT=1 only; INIT=3 已在 LoadBinaryCheckpoint 內讀取)
    if (restart_step > 0 && accu_count > 0 && (int)TBSWITCH && g_init_runtime != 3) {
        statistics_readbin_merged_stress();
        if (myid == 0)
            printf("Reynolds stress restored from ./statistics/ (accu_count=%d)\n", accu_count);
    }

    // FTT-gate check: discard old statistics if restart FTT is below threshold
    if (restart_step > 0) {
        double FTT_restart = (double)restart_step * dt_global / (double)flow_through_time;
        const size_t nTotal_gate = (size_t)NX6 * NYD6 * NZ6;
        const size_t tavg_bytes_gate = nTotal_gate * sizeof(double);

        if (FTT_restart < FTT_STATS_START && accu_count > 0) {
            if (myid == 0)
                printf("[FTT-GATE] FTT_restart=%.2f < FTT_STATS_START=%.1f: discarding ALL old statistics (accu_count=%d -> 0)\n",
                       FTT_restart, FTT_STATS_START, accu_count);
            accu_count = 0;
            stage1_announced = false;

            // Zero all 33 MeanVars+MeanDerivatives arrays (LOAD block overwrites memory.h zeros)
            if ((int)TBSWITCH) {
                CHECK_CUDA( cudaMemset(U,  0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(V,  0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(W,  0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(P,  0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(UU, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(UV, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(UW, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(VV, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(VW, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(WW, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(PU, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(PV, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(PW, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(PP, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(UUU, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(UUV, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(UUW, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(UVW, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(VVU, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(VVV, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(VVW, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(WWU, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(WWV, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(WWW, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(DUDX2, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(DUDY2, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(DUDZ2, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(DVDX2, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(DVDY2, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(DVDZ2, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(DWDX2, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(DWDY2, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(DWDZ2, 0, tavg_bytes_gate) );
            }
            // Tavg mirrors (host + device)
            memset(u_tavg_h, 0, tavg_bytes_gate);
            memset(v_tavg_h, 0, tavg_bytes_gate);
            memset(w_tavg_h, 0, tavg_bytes_gate);
            CHECK_CUDA( cudaMemset(u_tavg_d, 0, tavg_bytes_gate) );
            CHECK_CUDA( cudaMemset(v_tavg_d, 0, tavg_bytes_gate) );
            CHECK_CUDA( cudaMemset(w_tavg_d, 0, tavg_bytes_gate) );
            // Vorticity (host + device)
            if (ox_tavg_h) {
                memset(ox_tavg_h, 0, tavg_bytes_gate);
                memset(oy_tavg_h, 0, tavg_bytes_gate);
                memset(oz_tavg_h, 0, tavg_bytes_gate);
                CHECK_CUDA( cudaMemset(ox_tavg_d, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(oy_tavg_d, 0, tavg_bytes_gate) );
                CHECK_CUDA( cudaMemset(oz_tavg_d, 0, tavg_bytes_gate) );
            }
        }
    }

    CHECK_CUDA( cudaEventRecord(start,0) );
	CHECK_CUDA( cudaEventRecord(start1,0) );
    // 續跑初始狀態輸出: 從 CPU 資料計算 Ub，完整顯示重啟狀態
    if (restart_step > 0) {
        // Compute Ub from CPU data (rank 0 only, j=3 hill-crest cross-section)
        // 同 AccumulateUbulk kernel: Σ v(j=3,k,i) * dx * dz / (LX*(LZ-1))
        double Ub_init = 0.0;
        if (myid == 0) {
            // Bilinear cell-average: Σ v_cell × dx_cell × dz_cell / A_total
            for (int k = 3; k < NZ6-4; k++) {
            for (int i = 3; i < NX6-4; i++) {
                double v00 = v_h_p[3*NX6*NZ6 + k*NX6 + i];
                double v10 = v_h_p[3*NX6*NZ6 + (k+1)*NX6 + i];
                double v01 = v_h_p[3*NX6*NZ6 + k*NX6 + (i+1)];
                double v11 = v_h_p[3*NX6*NZ6 + (k+1)*NX6 + (i+1)];
                double v_cell = (v00 + v10 + v01 + v11) / 4.0;
                Ub_init += v_cell * (x_h[i+1] - x_h[i]) * (z_h[3*NZ6+k+1] - z_h[3*NZ6+k]);
            }}
            Ub_init /= A_cross_j3;   // 使用實際格點面積
        }
        MPI_Bcast(&Ub_init, 1, MPI_DOUBLE, 0, MPI_COMM_WORLD);
        Ub_avg_global = Ub_init;

        // Ma_max: 需所有 rank 參與 MPI_Allreduce，放在 if(myid==0) 之外
        double Ma_max_init = ComputeMaMax();

        if (myid == 0) {
            double FTT_init = (double)restart_step * dt_global / (double)flow_through_time;
            double Ustar = Ub_init / (double)Uref;
            double Fstar = Force_h[0] * (double)LY / ((double)Uref * (double)Uref);
            double Re_now = Ub_init / ((double)Uref / (double)Re);
            double Ma_init = Ub_init / (double)cs;

            printf("+----------------------------------------------------------------+\n");
            printf("| Step = %d    FTT = %.2f\n", restart_step, FTT_init);
            printf("|%s running with %4dx%4dx%4d grids\n", argv[0], (int)NX6, (int)NY6, (int)NZ6);
            printf("| Loop %d more steps, end at step %d\n", (int)loop, restart_step + 1 + (int)loop);
            printf("+----------------------------------------------------------------+\n");
            printf("[Step %d | FTT=%.2f] Ub=%.6f  U*=%.4f  Force=%.5E  F*=%.4f  Re(now)=%.1f  Ma=%.4f  Ma_max=%.4f\n",
                   restart_step, FTT_init, Ub_init, Ustar, Force_h[0], Fstar, Re_now, Ma_init, Ma_max_init);

            if (Ma_max_init > 0.35)
                printf("  >>> [WARNING] Ma_max=%.4f > 0.35 — BGK stability risk, consider reducing Uref\n", Ma_max_init);

            if (Ustar > 1.3)
                printf("  >>> [NOTE] U*=%.4f >> 1.0 — VTK velocity from old Uref, flow will decelerate to new target\n", Ustar);
        }
        // ----------------------------------------------------------------
        // [FIX 2026-04-16] 移除初始化段的 Anti-windup Force cap。
        //
        // 同根因: SIMPLE-PROP (Mode 0) 正常 Force 達 ±4.6E-05 (1100×
        //   F_Poiseuille), 但 100× Poiseuille cap = 4.1E-06 → 每次
        //   restart 初始化都被截斷 11 倍。
        //
        // 舊註解寫「典型 ~10-100×」僅對 Mode 1 (PID/Gehrke) 成立;
        // Mode 0 (SIMPLE-PROP) 在 OVERSHOOT 階段 Force 可達 1000×+。
        // NaN/Inf guard (main.cu:615-618) 已保護真正的損壞資料。
        // ----------------------------------------------------------------
        // ── WENO VTK host buffer 必須在任何 fileIO_velocity_vtk_merged 呼叫之前初始化 ──
        // restart 時此處會先輸出一次初始 VTK，fileIO 中若 weno_activation_zeta_h == NULL → segfault
#if USE_WENO7
        if (weno_activation_zeta_h == NULL) {
            const size_t weno_act_bytes = (size_t)NX6 * NYD6 * NZ6 * sizeof(unsigned char);
            weno_activation_zeta_h = (unsigned char*)malloc(weno_act_bytes);
            memset(weno_activation_zeta_h, 0, weno_act_bytes);
            if (myid == 0) {
                printf("[WENO7] VTK contour (zeta) ON: per-point activation [0..19] (%.1f KB/rank)\n",
                       weno_act_bytes / 1024.0);
            }
        }
#endif
        // 輸出初始 VTK (驗證重啟載入正確 + 使用修正後的 stride mapping)
        fileIO_velocity_vtk_merged(restart_step);
    }
    // GTS 雙步進: loop_start 必須為偶數, 確保 step+=1 後在奇數步觸發 step%N==1
    // (restart_step+1)&~1: 奇數→+1=偶數, 偶數→+1=奇數→&~1=偶數(重算被中斷的 even sub-step)
    int loop_start = (restart_step > 0) ? ((restart_step + 1) & ~1) : 0;

#if USE_TIMING
    Timing_Init(loop_start, g_restored_gpu_ms);
    Timing_WriteHeader(myid);
    if (myid == 0) printf("[TIMING] Timing system initialized (interval=%d, detail=%d, restored_gpu=%.2f min)\n",
                          TIMING_INTERVAL, TIMING_DETAIL, g_restored_gpu_ms / 60000.0);
#endif

#if USE_WENO7
    // ── WENO 診斷計數器初始化 ──
    {
        void *diag_ptr = NULL;
        CHECK_CUDA( cudaGetSymbolAddress(&diag_ptr, g_weno_diag_zeta) );
        CHECK_CUDA( cudaMemset(diag_ptr, 0, 19 * (int)NZ6 * sizeof(unsigned int)) );
        if (myid == 0) {
            printf("[WENO7] Diagnostic ON (interval=%d steps, NZ6=%d, per-q×k)\n",
                   (int)NDTWENO, (int)NZ6);
            printf("[WENO7] Log file: weno7_diag.log (append mode)\n");
            FILE *flog = fopen("weno7_diag.log", "a");
            if (flog) {
                fprintf(flog, "# ════════════════════════════════════════\n");
                fprintf(flog, "# WENO7-Z Diagnostic Log (per-q × per-k)\n");
                fprintf(flog, "# NZ6=%d | threshold=%.2f | q=%d | interval=%d steps\n",
                        (int)NZ6, gilbm_weno7::kActivationThreshold,
                        gilbm_weno7::kWENOZ_q, (int)NDTWENO);
                fprintf(flog, "# ════════════════════════════════════════\n");
                fflush(flog);
                fclose(flog);
            }
        }
    }
    // ── WENO VTK contour 完整初始化 — ζ 方向 ──
    // 若 restart path 已提前初始化 (上方 NULL guard), 此處跳過。
    // 冷啟動 (INIT != 3) 時 restart path 不執行，由此處初始化。
    if (weno_activation_zeta_h == NULL) {
        const size_t weno_act_bytes = (size_t)NX6 * NYD6 * NZ6 * sizeof(unsigned char);
        weno_activation_zeta_h = (unsigned char*)malloc(weno_act_bytes);
        memset(weno_activation_zeta_h, 0, weno_act_bytes);
        if (myid == 0) {
            printf("[WENO7] VTK contour (zeta) ON: per-point activation [0..19] (%.1f KB/rank)\n",
                   weno_act_bytes / 1024.0);
        }
    }
#endif

    // ── 流態模式宣告 ──
    if (myid == 0) {
        if (IS_LAMINAR) {
            printf("[MODE] Laminar (Re=%d <= %d): FTT statistics DISABLED, using field residual delta (Eq.37)\n",
                   (int)Re, LAMINAR_RE_THRESHOLD);
        } else {
            printf("[MODE] Turbulent (Re=%d > %d): FTT statistics at FTT >= %.1f, using CV criterion\n",
                   (int)Re, LAMINAR_RE_THRESHOLD, FTT_STATS_START);
        }
    }

    // ── checkrho.dat header (冷啟動時寫一次) ──
    if (myid == 0 && restart_step == 0) {
        FILE *fhdr = fopen("checkrho.dat", "w");
        if (fhdr) {
            fprintf(fhdr, "# checkrho.dat — Mass Conservation Monitor\n");
            fprintf(fhdr, "# Col1: step          — time step number\n");
            fprintf(fhdr, "# Col2: FTT           — flow-through time\n");
            fprintf(fhdr, "# Col3: rho_target    — target density (always 1.0)\n");
            fprintf(fhdr, "# Col4: rho_avg       — global volume-weighted average density (full precision)\n");
            fprintf(fhdr, "# Col5: rho_drift     — rho_avg - 1.0 (positive = heavier than target)\n");
            fprintf(fhdr, "# Col6: rho_correction— mass correction applied NEXT step (= -rho_drift)\n");
            fprintf(fhdr, "# Col7: SKIP_MC       — 0=mass correction ON, 1=mid-step correction OFF\n");
            fprintf(fhdr, "# NOTE: rho_avg uses control-volume weights from the curvilinear y-z grid.\n");
            fprintf(fhdr, "# NOTE: |Col5| == |Col6| is EXPECTED — both are computed from the same\n");
            fprintf(fhdr, "#       density field. Col6 = -Col5 because the correction exactly\n");
            fprintf(fhdr, "#       compensates the drift. Check Col5 trend for mass conservation.\n");
            fprintf(fhdr, "#\n");
            fflush(fhdr);
            fclose(fhdr);
        }
    }

    //從此開始進入迴圈 (FTT-gated two-stage time averaging)
    for( step = loop_start ; step < loop_start + loop ; step++, accu_num++ ) {
        double FTT_now = step * dt_global / (double)flow_through_time;

        // ===== Phase 7: 每步停止條件檢查 (collective) =====
        // 4 種正常停止: signal / STOP_CHAIN / converged / diverged / FTT_STOP
        // 任一觸發 → break → 下方 final checkpoint 區塊儲存狀態後退出
        {
            int reason = CheckStopConditions(step, FTT_now, myid,
                                             Force_h[0], g_conv_status);
            if (reason != STOP_NONE) {
                g_stop_reason = reason;
                if (myid == 0) {
                    printf("\n[Phase7-STOP] Triggered at step=%d FTT=%.4f — reason: %s\n",
                           step, FTT_now, StopReasonStr(reason));
                    if (reason == STOP_SIGNAL) {
                        printf("[Phase7-STOP] signal number received: %d\n",
                               (int)g_signal_received);
                    }
                    fflush(stdout);
                }
                break;
            }
        }

#if USE_TIMING && TIMING_DETAIL
        // 取樣旗標: 僅在報告步的 odd sub-step 啟用 per-kernel 計時
        // (odd step 是報告前最後一個 sub-step, 其分解最具代表性)
        g_timing_sample = false;
#endif

        // ===== Sub-step 1: even step =====
        // [方案B] 讀 f_post_read → 插值+碰撞 → 寫 f_post_write → MPI/periodic
        Launch_CollisionStreaming( f_post_read, f_post_write );
        // Swap: 下一步讀本步的 output
        { double *tmp = f_post_read; f_post_read = f_post_write; f_post_write = tmp; }

        // Statistics accumulation (FTT >= FTT_STATS_START): mean + RS + derivatives
        // IS_LAMINAR: 層流模式跳過統計累積 (不需要 Reynolds stress / TKE)
        if (!IS_LAMINAR && FTT_now >= FTT_STATS_START && step > 0) {
            CHECK_CUDA( cudaDeviceSynchronize() );

            // ── Macro ghost zone exchange (ξ-MPI + η-periodic) ──
            // AccumulateVorticity 在 j=3 讀 u[j=2], 需要正確的 ghost zone
            MPI_Exchange_Macro_Packed(
                rho_d, u, v, w,
                macro_send_buf_left_d,  macro_send_buf_right_d,
                macro_recv_buf_left_d,  macro_recv_buf_right_d,
                req_persist_macro, stream1);
            {   // η-periodic for macro fields
                dim3 griddimSW_m(1, NYD6/NT+1, NZ6);
                dim3 blockdimSW_m(3, NT, 1);
                periodicSW_macro<<<griddimSW_m, blockdimSW_m, 0, stream1>>>(
                    rho_d, u, v, w);
            }
            CHECK_CUDA( cudaStreamSynchronize(stream1) );

            if ((int)TBSWITCH) Launch_TurbulentSum();
            Launch_AccumulateTavg();
            Launch_AccumulateVorticity();
            accu_count++;
        } else {
            CHECK_CUDA( cudaDeviceSynchronize() );
        }

        // Stage transition message
        if (!IS_LAMINAR && !stage1_announced && FTT_now >= FTT_STATS_START) {
            stage1_announced = true;
            if (myid == 0) printf("\n>>> [FTT=%.2f] Statistics accumulation STARTED (accu_count=%d) <<<\n\n", FTT_now, accu_count);
        }

        // ===== Mid-step mass correction (between even and odd) =====
        // 由 SKIP_MIDSTEP_MASSCORR 開關控制 (variables.h)
        // Mid-step mass correction 區塊
        // 包含: ReduceRhoWeightedSum_Kernel → cudaMemcpy D2H → MPI_Reduce → MPI_Bcast → cudaMemcpy H2D
        // ★ 這是全域 MPI barrier, MPI_Wtime 計時可測量其實際開銷
#if !SKIP_MIDSTEP_MASSCORR
        {
#if USE_TIMING && TIMING_DETAIL
            double t_mc_start = MPI_Wtime();
#endif
            UpdateVolumeWeightedMassCorrection();

#if USE_TIMING && TIMING_DETAIL
            g_timing.last_masscorr_ms = (MPI_Wtime() - t_mc_start) * 1000.0;
#endif
        }
#else
        // SKIP_MIDSTEP_MASSCORR=1: 跳過 mid-step mass correction
        // 僅保留 NDTFRC 週期的主要修正 (Launch_ModifyForcingTerm)










        
#if USE_TIMING && TIMING_DETAIL
        g_timing.last_masscorr_ms = 0.0;
#endif
#endif // !SKIP_MIDSTEP_MASSCORR
        // ===== Sub-step 2: odd step =====
        step += 1;
        accu_num += 1;
        FTT_now = step * dt_global / (double)flow_through_time;

#if USE_TIMING && TIMING_DETAIL
        // 在報告步的 odd sub-step 啟用 per-kernel 計時取樣
        g_timing_sample = (step % TIMING_INTERVAL == 1);
#endif

        // [方案B] 讀 f_post_read → 插值+碰撞 → 寫 f_post_write → MPI/periodic
        Launch_CollisionStreaming( f_post_read, f_post_write );
        // Swap: 下一步讀本步的 output
        { double *tmp = f_post_read; f_post_read = f_post_write; f_post_write = tmp; }

        // Statistics accumulation (FTT >= FTT_STATS_START)
        // IS_LAMINAR: 層流模式跳過統計累積
        if (!IS_LAMINAR && FTT_now >= FTT_STATS_START) {
            CHECK_CUDA( cudaDeviceSynchronize() );

            // ── Macro ghost zone exchange (ξ-MPI + η-periodic) ──
            MPI_Exchange_Macro_Packed(
                rho_d, u, v, w,
                macro_send_buf_left_d,  macro_send_buf_right_d,
                macro_recv_buf_left_d,  macro_recv_buf_right_d,
                req_persist_macro, stream1);
            {   // η-periodic for macro fields
                dim3 griddimSW_m(1, NYD6/NT+1, NZ6);
                dim3 blockdimSW_m(3, NT, 1);
                periodicSW_macro<<<griddimSW_m, blockdimSW_m, 0, stream1>>>(
                    rho_d, u, v, w);
            }
            CHECK_CUDA( cudaStreamSynchronize(stream1) );

            if ((int)TBSWITCH) Launch_TurbulentSum();
            Launch_AccumulateTavg();
            Launch_AccumulateVorticity();
            accu_count++;
        } else {
            CHECK_CUDA( cudaDeviceSynchronize() );
        }

        // ===== Status display + timing report =====
#if USE_TIMING
        if ( step % TIMING_INTERVAL == 1 ) {
    #if TIMING_DETAIL
            // 收集 per-kernel 分解 (GPU 已在上方 cudaDeviceSynchronize 完成同步)
            if (g_timing_sample) Timing_CollectKernelBreakdown();
    #endif
            Timing_Report(step, myid, FTT_now, argv[0]);
        }
#else
        // 舊版計時 (USE_TIMING=0 時保留)
        if ( myid == 0 && step%5000 == 1 ) {
            CHECK_CUDA( cudaEventRecord( stop1,0 ) );
            CHECK_CUDA( cudaEventSynchronize( stop1 ) );
            float cudatime1;
            CHECK_CUDA( cudaEventElapsedTime( &cudatime1,start1,stop1 ) );

            printf("+----------------------------------------------------------------+\n");
            printf("| Step = %d    FTT = %.2f \n", step, FTT_now);
            printf("|%s running with %4dx%4dx%4d grids            \n", argv[0], (int)NX6, (int)NY6, (int)NZ6 );
            printf("| Running %6f mins                                           \n", (cudatime1/60/1000) );
            printf("| Stats: %s  accu_count=%d\n",
                   (FTT_now >= FTT_STATS_START) ? "ON" : "OFF", accu_count);
            printf("+----------------------------------------------------------------+\n");

            cudaEventRecord(start1,0);
        }
#endif

#if USE_WENO7
        // ===== WENO Diagnostic Output (every NDTWENO steps) =====
        // USE_WENO7=1 時每 NDTWENO 步讀取 per-z-layer 啟用統計
        //
        // 輸出目標：
        //   (1) stdout (printf) — 即時監控
        //   (2) weno7_diag.log  — 持久化記錄，可事後分析
        //
        // 輸出內容：
        //   - 每一層 ζ=k 的 WENO 非線性啟動次數和比例
        //   - 震盪偵測摘要：哪些區域觸發了 WENO，壓制是否成功
        //
        // ── WENO 診斷：所有 rank 參與 ──────────────────────────
        // 每個 rank 的 GPU 各自累計 g_weno_diag_zeta[q][k]，
        // 必須 MPI_Reduce(SUM) 聚合後才是全域統計。
        // MPI_Reduce 放在 if(myid==0) 之外，所有 rank 都要參與。
        if (step % (int)NDTWENO == 1) {
            // Step 1: 每個 rank 從 GPU 讀取 2D per-q×per-k 計數器
            const int diag_total = 19 * (int)NZ6;
            unsigned int weno_diag_local[19 * NZ6] = {0};
            unsigned int weno_diag_global[19 * NZ6] = {0};
            {
                void *diag_ptr = NULL;
                CHECK_CUDA( cudaGetSymbolAddress(&diag_ptr, g_weno_diag_zeta) );
                CHECK_CUDA( cudaMemcpy(weno_diag_local, diag_ptr,
                                       diag_total * sizeof(unsigned int),
                                       cudaMemcpyDeviceToHost) );
                CHECK_CUDA( cudaMemset(diag_ptr, 0, diag_total * sizeof(unsigned int)) );
            }

            // Step 2: MPI_Reduce — 所有 rank 求和到 rank 0
            MPI_Reduce(weno_diag_local, weno_diag_global, diag_total,
                       MPI_UNSIGNED, MPI_SUM, 0, MPI_COMM_WORLD);

            // Step 3: rank 0 彙總並輸出
            if (myid == 0) {
                // 分母：每層每方向 = (NX6-6)×(NYD6-6) × jp × NDTWENO
                const unsigned long long denom_per_layer =
                    (unsigned long long)(NX6 - 6) * (NYD6 - 6) * jp * (unsigned long long)NDTWENO;
                const int k_lo = 3;
                const int k_hi = (int)NZ6 - 4;  // inclusive
                const int n_layers = k_hi - k_lo + 1;

                // D3Q19 速度名稱表
                const char *q_name[19] = {
                    "(0,0,0)", "(+,0,0)","(-,0,0)","(0,+,0)","(0,-,0)","(0,0,+)","(0,0,-)",
                    "(+,+,0)","(-,+,0)","(+,-,0)","(-,-,0)",
                    "(+,0,+)","(-,0,+)","(+,0,-)","(-,0,-)",
                    "(0,+,+)","(0,-,+)","(0,+,-)","(0,-,-)"
                };
                // e_z 值查表
                const int ez_table[19] = {0, 0,0,0,0,1,-1, 0,0,0,0, 1,1,-1,-1, 1,1,-1,-1};

                // ── 實際經過 ζ 插值的方向 ──
                // q=0 (rest): 不插值; q=1,2 (±x): δζ=0 → 1D η-only
                // q=3-6, 7-14, 15-18 (共 16 個): 經過 gilbm_zeta_collapse → WENO7 統計有效
                const int n_zeta_dirs = 16;

                // ── 全域統計（僅計入 16 個 ζ-interpolated 方向）──
                unsigned long long grand_total = 0;
                for (int qq = 0; qq < 19; qq++)
                    for (int kk = k_lo; kk <= k_hi; kk++)
                        grand_total += weno_diag_global[qq * (int)NZ6 + kk];

                // ── stdout 輸出（框線表格）──────────────────────
                printf("\n");
                printf("[WENO7] +============================================================+\n");
                printf("[WENO7] |  Step %-8d | FTT=%.4f | %d steps | %d ranks           |\n",
                       step, FTT_now, (int)NDTWENO, (int)jp);
                printf("[WENO7] |  Denom/layer/q = %-12llu (grid x ranks x steps)      |\n", denom_per_layer);

                if (grand_total == 0) {
                    printf("[WENO7] |  Status: ALL SMOOTH -- zero activations (%d zeta-dirs) |\n", n_zeta_dirs);
                    printf("[WENO7] +============================================================+\n\n");
                } else {
                    double grand_pct = (double)grand_total /
                        ((double)denom_per_layer * n_layers * (double)n_zeta_dirs) * 100.0;
                    printf("[WENO7] |  Status: WENO ACTIVE | total=%llu (%.4f%%)         |\n",
                           grand_total, grand_pct);
                    printf("[WENO7] +============================================================+\n");

                    // ── Table 1: Per-direction summary ──
                    printf("[WENO7] +----+------------+----+------------+-----------+\n");
                    printf("[WENO7] | q  | e_vector   | ez | activations| avg %%     |\n");
                    printf("[WENO7] +----+------------+----+------------+-----------+\n");
                    for (int qq = 0; qq < 19; qq++) {
                        unsigned long long q_total = 0;
                        for (int kk = k_lo; kk <= k_hi; kk++)
                            q_total += weno_diag_global[qq * (int)NZ6 + kk];
                        if (q_total == 0) continue;
                        double avg_pct = (denom_per_layer > 0 && n_layers > 0)
                            ? (double)q_total / ((double)denom_per_layer * n_layers) * 100.0 : 0.0;
                        printf("[WENO7] | %2d | %-10s | %+d | %10llu | %8.4f%% |\n",
                               qq, q_name[qq], ez_table[qq], q_total, avg_pct);
                    }
                    printf("[WENO7] +----+------------+----+------------+-----------+\n");

                    // ── Table 2: Per-q x Per-k detail (non-zero) ──
                    printf("[WENO7] +----+------------+----+------------+-----------+-----------+\n");
                    printf("[WENO7] | q  | e_vector   |  k | activations| ratio %%   | zone      |\n");
                    printf("[WENO7] +----+------------+----+------------+-----------+-----------+\n");
                    for (int qq = 0; qq < 19; qq++) {
                        for (int kk = k_lo; kk <= k_hi; kk++) {
                            unsigned int cnt = weno_diag_global[qq * (int)NZ6 + kk];
                            if (cnt == 0) continue;
                            double pct = (denom_per_layer > 0)
                                ? (double)cnt / (double)denom_per_layer * 100.0 : 0.0;
                            const char *zone;
                            if      (kk <= 10)              zone = "near-wall";
                            else if (kk <= 25)              zone = "shear";
                            else if (kk >= (int)NZ6 - 7)    zone = "top-wall";
                            else                             zone = "interior";
                            printf("[WENO7] | %2d | %-10s | %2d | %10u | %8.4f%% | %-9s |\n",
                                   qq, q_name[qq], kk, cnt, pct, zone);
                        }
                    }
                    printf("[WENO7] +----+------------+----+------------+-----------+-----------+\n");

                    // ── Table 3: Per-layer summary (分母 = denom × 16 ζ-dirs) ──
                    printf("[WENO7] +----+------------+-----------+-----------+\n");
                    printf("[WENO7] |  k | activations| avg %%     | zone      |\n");
                    printf("[WENO7] +----+------------+-----------+-----------+\n");
                    for (int kk = k_lo; kk <= k_hi; kk++) {
                        unsigned long long k_total = 0;
                        for (int qq = 0; qq < 19; qq++)
                            k_total += weno_diag_global[qq * (int)NZ6 + kk];
                        if (k_total == 0) continue;
                        double pct = (denom_per_layer > 0)
                            ? (double)k_total / ((double)denom_per_layer * (double)n_zeta_dirs) * 100.0 : 0.0;
                        const char *zone;
                        if      (kk <= 10)              zone = "near-wall";
                        else if (kk <= 25)              zone = "shear";
                        else if (kk >= (int)NZ6 - 7)    zone = "top-wall";
                        else                             zone = "interior";
                        printf("[WENO7] | %2d | %10llu | %8.4f%% | %-9s |\n",
                               kk, k_total, pct, zone);
                    }
                    printf("[WENO7] +----+------------+-----------+-----------+\n");
                }
                printf("\n");

                // ── log 檔案輸出（框線表格）────────────────────
                {
                    FILE *flog = fopen("weno7_diag.log", "a");
                    if (flog) {
                        fprintf(flog, "\n");
                        fprintf(flog, "+============================================================+\n");
                        fprintf(flog, "|  Step %-8d | FTT=%.4f | %d steps | %d ranks           |\n",
                                step, FTT_now, (int)NDTWENO, (int)jp);
                        fprintf(flog, "|  Denom/layer/q = %-12llu (grid x ranks x steps)      |\n", denom_per_layer);

                        if (grand_total == 0) {
                            fprintf(flog, "|  Status: ALL SMOOTH -- zero activations (%d zeta-dirs) |\n", n_zeta_dirs);
                            fprintf(flog, "+============================================================+\n\n");
                        } else {
                            double grand_pct = (double)grand_total /
                                ((double)denom_per_layer * n_layers * (double)n_zeta_dirs) * 100.0;
                            fprintf(flog, "|  Status: WENO ACTIVE | total=%llu (%.4f%%)         |\n",
                                    grand_total, grand_pct);
                            fprintf(flog, "+============================================================+\n");

                            // ── Table 1: Per-direction summary ──
                            fprintf(flog, "+----+------------+----+------------+-----------+\n");
                            fprintf(flog, "| q  | e_vector   | ez | activations| avg %%     |\n");
                            fprintf(flog, "+----+------------+----+------------+-----------+\n");
                            for (int qq = 0; qq < 19; qq++) {
                                unsigned long long q_total = 0;
                                for (int kk = k_lo; kk <= k_hi; kk++)
                                    q_total += weno_diag_global[qq * (int)NZ6 + kk];
                                if (q_total == 0) continue;
                                double avg_pct = (denom_per_layer > 0 && n_layers > 0)
                                    ? (double)q_total / ((double)denom_per_layer * n_layers) * 100.0 : 0.0;
                                fprintf(flog, "| %2d | %-10s | %+d | %10llu | %8.4f%% |\n",
                                        qq, q_name[qq], ez_table[qq], q_total, avg_pct);
                            }
                            fprintf(flog, "+----+------------+----+------------+-----------+\n");

                            // ── Table 2: Per-q x Per-k detail (non-zero) ──
                            fprintf(flog, "+----+------------+----+------------+-----------+-----------+\n");
                            fprintf(flog, "| q  | e_vector   |  k | activations| ratio %%   | zone      |\n");
                            fprintf(flog, "+----+------------+----+------------+-----------+-----------+\n");
                            for (int qq = 0; qq < 19; qq++) {
                                for (int kk = k_lo; kk <= k_hi; kk++) {
                                    unsigned int cnt = weno_diag_global[qq * (int)NZ6 + kk];
                                    if (cnt == 0) continue;
                                    double pct = (denom_per_layer > 0)
                                        ? (double)cnt / (double)denom_per_layer * 100.0 : 0.0;
                                    const char *zone;
                                    if      (kk <= 10)              zone = "near-wall";
                                    else if (kk <= 25)              zone = "shear";
                                    else if (kk >= (int)NZ6 - 7)    zone = "top-wall";
                                    else                             zone = "interior";
                                    fprintf(flog, "| %2d | %-10s | %2d | %10u | %8.4f%% | %-9s |\n",
                                            qq, q_name[qq], kk, cnt, pct, zone);
                                }
                            }
                            fprintf(flog, "+----+------------+----+------------+-----------+-----------+\n");

                            // ── Table 3: Per-layer summary (分母 = denom × 16 ζ-dirs) ──
                            fprintf(flog, "+----+------------+-----------+-----------+\n");
                            fprintf(flog, "|  k | activations| avg %%     | zone      |\n");
                            fprintf(flog, "+----+------------+-----------+-----------+\n");
                            for (int kk = k_lo; kk <= k_hi; kk++) {
                                unsigned long long k_total = 0;
                                for (int qq = 0; qq < 19; qq++)
                                    k_total += weno_diag_global[qq * (int)NZ6 + kk];
                                if (k_total == 0) continue;
                                double pct = (denom_per_layer > 0)
                                    ? (double)k_total / ((double)denom_per_layer * (double)n_zeta_dirs) * 100.0 : 0.0;
                                const char *zone;
                                if      (kk <= 10)              zone = "near-wall";
                                else if (kk <= 25)              zone = "shear";
                                else if (kk >= (int)NZ6 - 7)    zone = "top-wall";
                                else                             zone = "interior";
                                fprintf(flog, "| %2d | %10llu | %8.4f%% | %-9s |\n",
                                        kk, k_total, pct, zone);
                            }
                            fprintf(flog, "+----+------------+-----------+-----------+\n");
                        }
                        fprintf(flog, "\n");
                        fflush(flog);
                        fclose(flog);
                    }
                }
            } // myid == 0
        }
#endif // USE_WENO7

        // ===== Force modification (every NDTFRC steps) =====
        if ( (step%(int)NDTFRC == 1) ) {
            Launch_ModifyForcingTerm();
        }

        // ===== Monitor output (every NDTMIT steps) =====
        // ★ 層流 g_eps_current 在 Launch_Monitor() 內每步更新
		if ( step%(int)NDTMIT == 1 ) {
#if USE_TIMING
			g_gpu_time_min = Timing_GetGPUTime_min();
#endif
			Launch_Monitor();

            // 紊流 CV 環形緩衝區更新 (每 NDTMIT 步)
            if (!IS_LAMINAR && FTT_now >= FTT_STATS_START && accu_count > 0) {
                uu_history[cv_idx] = g_uu_RS_check;
                k_history[cv_idx]  = g_k_check_val;
                ftt_cv_history[cv_idx] = FTT_now;
                cv_idx = (cv_idx + 1) % CV_WINDOW_SIZE;
                if (cv_buf_count < CV_WINDOW_SIZE) cv_buf_count++;
            }
		}

        // ===== Convergence check (every NDTCONV steps) =====
        if ( step % NDTCONV == 1 ) {
            if (IS_LAMINAR) {
                // ── 層流: g_eps_current 已由 Launch_Monitor() 每 NDTMIT 步更新 ──
                // ── δ = |U*(n)-U*(n-1)| / |U*(n)| (對齊 Python Eq.37) ──

                if (g_eps_current < EPS_CONVERGED) {
                    g_conv_status = 2; g_conv_count++;
                } else if (g_eps_current < EPS_NEAR) {
                    g_conv_status = 1; g_conv_count = 0;
                } else {
                    g_conv_status = 0; g_conv_count = 0;
                }

                if (myid == 0) {
#if USE_TIMING
                    double gpu_min = Timing_GetGPUTime_min();
                    printf("[CONV] Step=%-8d | FTT=%7.3f | GPU=%7.2f min | delta=%.2e | Status=%s (%d/%d)\n",
                           step, FTT_now, gpu_min, g_eps_current,
                           ConvStatusStr(g_conv_status), g_conv_count, N_CONFIRM_LAMINAR);
#else
                    printf("[CONV] Step=%-8d | FTT=%7.3f | delta=%.2e | Status=%s (%d/%d)\n",
                           step, FTT_now, g_eps_current,
                           ConvStatusStr(g_conv_status), g_conv_count, N_CONFIRM_LAMINAR);
#endif
                }

                // 層流自動終止
                if (g_conv_count >= N_CONFIRM_LAMINAR) {
                    if (myid == 0)
                        printf("\n[CONVERGED] Laminar steady-state reached: delta=%.2e (< %.0e for %d consecutive checks)\n",
                               g_eps_current, EPS_CONVERGED, N_CONFIRM_LAMINAR);
#if USE_TIMING
                    Timing_FinalSummary(step, FTT_now, accu_count, "CONVERGED (laminar delta)", myid);
#endif
                    break;  // → 跳到 final exit checkpoint
                }
            } else {
                // ── 紊流: CV 收斂 ──
                if (FTT_now >= FTT_STATS_START + CV_WINDOW_FTT && cv_buf_count >= 10) {
                    g_cv_uu = compute_cv(uu_history, ftt_cv_history, cv_buf_count, cv_idx, FTT_now, CV_WINDOW_FTT);
                    g_cv_k  = compute_cv(k_history,  ftt_cv_history, cv_buf_count, cv_idx, FTT_now, CV_WINDOW_FTT);

                    if (g_cv_uu < CV_CONVERGED && g_cv_k < CV_CONVERGED) {
                        g_conv_status = 2; g_conv_count++;
                    } else if (g_cv_uu < CV_NEAR && g_cv_k < CV_NEAR) {
                        g_conv_status = 1; g_conv_count = 0;
                    } else {
                        g_conv_status = 0; g_conv_count = 0;
                    }

                    if (myid == 0) {
#if USE_TIMING
                        double gpu_min = Timing_GetGPUTime_min();
                        printf("[CONV] Step=%-8d | FTT=%7.3f | GPU=%7.2f min | CV(uu)=%.1f%% CV(k)=%.1f%% | Status=%s (%d/%d)\n",
                               step, FTT_now, gpu_min, g_cv_uu, g_cv_k,
                               ConvStatusStr(g_conv_status), g_conv_count, N_CONFIRM_TURB);
#else
                        printf("[CONV] Step=%-8d | FTT=%7.3f | CV(uu)=%.1f%% CV(k)=%.1f%% | Status=%s (%d/%d)\n",
                               step, FTT_now, g_cv_uu, g_cv_k,
                               ConvStatusStr(g_conv_status), g_conv_count, N_CONFIRM_TURB);
#endif
                    }

                    // 紊流自動終止
                    if (g_conv_count >= N_CONFIRM_TURB) {
                        if (myid == 0)
                            printf("\n[CONVERGED] Turbulent statistics converged: CV(uu)=%.2f%%, CV(k)=%.2f%%\n",
                                   g_cv_uu, g_cv_k);
#if USE_TIMING
                        Timing_FinalSummary(step, FTT_now, accu_count, "CONVERGED (turbulent CV)", myid);
#endif
                        break;
                    }
                } else if (FTT_now >= FTT_STATS_START) {
                    if (myid == 0) {
#if USE_TIMING
                        double gpu_min = Timing_GetGPUTime_min();
                        printf("[CONV] Step=%-8d | FTT=%7.3f | GPU=%7.2f min | Accumulating... (need FTT>%.1f) | accu=%d\n",
                               step, FTT_now, gpu_min, FTT_STATS_START + CV_WINDOW_FTT, accu_count);
#endif
                    }
                } else {
                    if (myid == 0) {
#if USE_TIMING
                        double gpu_min = Timing_GetGPUTime_min();
                        printf("[CONV] Step=%-8d | FTT=%7.3f | GPU=%7.2f min | Waiting for FTT>=%.1f | accu=%d\n",
                               step, FTT_now, gpu_min, FTT_STATS_START, accu_count);
#endif
                    }
                }
            }
        }

        // ===== VTK output (every NDTVTK steps) + binary checkpoint (every NDTBIN steps) =====
        if ( step % NDTVTK == 1 ) {
            Launch_UnpackFPost_Direct(f_post_read);  // [FIX] direct f_post → fh_p[q] (bypass ft[])
            SendMacroCPU();                           // only u/v/w/rho D2H (f already in fh_p)
            const size_t tavg_bytes = (size_t)NX6 * NYD6 * NZ6 * sizeof(double);
            CHECK_CUDA( cudaMemcpy(u_tavg_h, u_tavg_d, tavg_bytes, cudaMemcpyDeviceToHost) );
            CHECK_CUDA( cudaMemcpy(v_tavg_h, v_tavg_d, tavg_bytes, cudaMemcpyDeviceToHost) );
            CHECK_CUDA( cudaMemcpy(w_tavg_h, w_tavg_d, tavg_bytes, cudaMemcpyDeviceToHost) );

            // VTK-step status
            double Ma_max_vtk = ComputeMaMax();
            if (myid == 0) {
                // Bilinear cell-average: Σ v_cell × dx_cell × dz_cell / A_total
                double Ub_vtk = 0.0;
                for (int kk = 3; kk < NZ6-4; kk++)
                for (int ii = 3; ii < NX6-4; ii++) {
                    double v00 = v_h_p[3*NX6*NZ6 + kk*NX6 + ii];
                    double v10 = v_h_p[3*NX6*NZ6 + (kk+1)*NX6 + ii];
                    double v01 = v_h_p[3*NX6*NZ6 + kk*NX6 + (ii+1)];
                    double v11 = v_h_p[3*NX6*NZ6 + (kk+1)*NX6 + (ii+1)];
                    double v_cell = (v00 + v10 + v01 + v11) / 4.0;
                    Ub_vtk += v_cell * (x_h[ii+1] - x_h[ii]) * (z_h[3*NZ6+kk+1] - z_h[3*NZ6+kk]);
                }
                Ub_vtk /= A_cross_j3;   // 使用實際格點面積
                double vtk_gpu_min = 0.0;
#if USE_TIMING
                vtk_gpu_min = Timing_GetGPUTime_min();
#endif
                double vtk_error = IS_LAMINAR ? g_eps_current : fmax(g_cv_uu, g_cv_k);
                printf("[VTK] Step=%-8d | FTT=%7.3f | GPU=%7.2f min | Ub=%.6f | U*=%.4f | Ma_max=%.4f | Error=%.2e | Conv=%s\n",
                       step, FTT_now, vtk_gpu_min,
                       Ub_vtk, Ub_vtk / (double)Uref, Ma_max_vtk,
                       vtk_error, ConvStatusStr(g_conv_status));
                printf("      Force=%.5E  F*=%.4f  Re_eff=%.1f  Ma_bulk=%.4f  accu=%d\n",
                       Force_h[0],
                       Force_h[0] * (double)LY / ((double)Uref * (double)Uref),
                       Ub_vtk / ((double)Uref / (double)Re),
                       Ub_vtk / (double)cs, accu_count);
            }

#if USE_WENO7
            // ── WENO activation contour (ζ): D2H transfer ──
            // g_weno_activation_count_zeta[NZ6][NYD6][NX6] (unsigned char)
            // 每格點記錄 19 個速度方向中有幾個啟動 WENO 非線性權重 [0..19]
            // kernel 每步自動重置 → 此處讀到的是最後一步的瞬時快照
            // 不需要 cudaMemset：kernel 每步 q-loop 前已歸零
            {
                const size_t weno_act_bytes = (size_t)NX6 * NYD6 * NZ6 * sizeof(unsigned char);
                void *wact_ptr = NULL;
                CHECK_CUDA( cudaGetSymbolAddress(&wact_ptr, g_weno_activation_count_zeta) );
                CHECK_CUDA( cudaMemcpy(weno_activation_zeta_h, wact_ptr, weno_act_bytes, cudaMemcpyDeviceToHost) );
            }
#endif

            fileIO_velocity_vtk_merged( step );

            // ===== Animation: pipeline.py render PNG + append to 2 GIFs (background) =====
            AnimRenderAndRebuild( step );

            // Binary checkpoint (every NDTBIN steps, piggyback on VTK's SendDataToCPU)
            if (step % NDTBIN == 1) {
                SaveBinaryCheckpoint( step );
            }
        }

        // ===== Global Mass Conservation Modify (volume-weighted GPU reduction — no SendDataToCPU) =====
        cudaDeviceSynchronize();
        cudaMemcpy(Force_h, Force_d, sizeof(double), cudaMemcpyDeviceToHost);
        UpdateVolumeWeightedMassCorrection();

        // ===== Mass Conservation Check + NaN early stop (every 100 steps) =====
        // rho_modify_h was just computed from the volume-weighted average:
        //   rho_modify = 1 - <rho>_V  →  <rho>_V = 1 - rho_modify
        if (step % 100 == 1) {
            int nan_flag = 0;
            double rho_avg_check = 0.0;
            if (myid == 0) {
                rho_avg_check = 1.0 - rho_modify_h[0];
                if (std::isnan(rho_avg_check) || std::isinf(rho_avg_check) || fabs(rho_avg_check - 1.0) > 0.01) {
                    printf("[FATAL] Divergence detected at step %d: rho_avg_V = %.6e, stopping.\n", step, rho_avg_check);
                    nan_flag = 1;
                }
            }
            MPI_Bcast(&nan_flag, 1, MPI_INT, 0, MPI_COMM_WORLD);
            if (nan_flag) {
                Launch_UnpackFPost_Direct(f_post_read);  // [FIX] direct f_post → fh_p[q]
                SendMacroCPU();
                fileIO_velocity_vtk_merged(step);
                break;
            }

            if (myid == 0) {
                double FTT_rho = step * dt_global / (double)flow_through_time;
                double rho_avg_out = rho_avg_check;
                FILE *checkrho = fopen("checkrho.dat", "a");
                // step  FTT  rho_target  rho_avg  rho_drift  rho_correction  SKIP_MC
                // NOTE: rho_correction = -rho_drift (by definition), see header for details
                fprintf(checkrho, "%d\t%.4f\t%.6f\t%.12e\t%+.6e\t%+.6e\t%d\n",
                        step, FTT_rho, 1.0, rho_avg_out,
                        rho_avg_out - 1.0, rho_modify_h[0], SKIP_MIDSTEP_MASSCORR);
                fflush(checkrho);
                fclose(checkrho);
            }
        }

        // [Phase 7] FTT_STOP 已由迴圈頂部的 CheckStopConditions 統一處理,
        //           此處舊的 in-loop FTT_STOP 檢查已移除, 避免雙重路徑。
    }

    // ===== Phase 7: 若迴圈自然退出 (for 迴圈條件 step < loop_start+loop 不再成立)
    //                而非被 break, 標記為 loop limit reached =====
    if (g_stop_reason == STOP_NONE) {
        g_stop_reason = STOP_LOOP_LIMIT;
        if (myid == 0) {
            printf("\n[Phase7-STOP] Loop range exhausted at step=%d — reason: %s\n",
                   step, StopReasonStr(g_stop_reason));
        }
    }

#if USE_TIMING
    // Timing summary 使用 Phase 7 的 reason
    {
        double FTT_exit = step * dt_global / (double)flow_through_time;
        Timing_FinalSummary(step, FTT_exit, accu_count,
                            StopReasonStr(g_stop_reason), myid);
    }
#endif

    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );

    // ===== Final exit checkpoint: always save state =====
    // [Phase 7] 由 g_stop_reason 決定的 4 種正常停止 (signal/converged/
    //           diverged/FTT_STOP/loop_limit) 都經由此區塊儲存最終狀態。
    {
        double FTT_final = step * dt_global / (double)flow_through_time;
        if (myid == 0) {
            printf("[FINAL] Saving checkpoint at step=%d FTT=%.4f (stop reason: %s)\n",
                   step, FTT_final, StopReasonStr(g_stop_reason));
            fflush(stdout);
        }
        Launch_UnpackFPost_Direct(f_post_read);  // [FIX] direct f_post → fh_p[q]
        SendMacroCPU();
        result_writebin_velocityandf();   // legacy: ./result/ 平面 checkpoint

        // Copy GPU tavg → host for final VTK + binary checkpoint
        const size_t tavg_bytes_final = (size_t)NX6 * NYD6 * NZ6 * sizeof(double);
        CHECK_CUDA( cudaMemcpy(u_tavg_h, u_tavg_d, tavg_bytes_final, cudaMemcpyDeviceToHost) );
        CHECK_CUDA( cudaMemcpy(v_tavg_h, v_tavg_d, tavg_bytes_final, cudaMemcpyDeviceToHost) );
        CHECK_CUDA( cudaMemcpy(w_tavg_h, w_tavg_d, tavg_bytes_final, cudaMemcpyDeviceToHost) );
        fileIO_velocity_vtk_merged( step );
        SaveBinaryCheckpoint( step );     // binary checkpoint (f^neq + tavg + RS + metadata)

        // ===== Animation: final GIF append (blocking, 等背景任務收尾) =====
        AnimFinalize( step );

        // Write merged statistics to ./statistics/ (backward compat for Python analysis scripts)
        if (accu_count > 0 && (int)TBSWITCH) {
            if (myid == 0) {
                printf("[FINAL] Writing merged statistics (33 arrays), accu_count=%d\n", accu_count);
            }
            statistics_writebin_merged_stress();
        } else if (myid == 0) {
            printf("[FINAL] No statistics to write (accu_count=%d).\n", accu_count);
        }

        // [Phase 7] Timing_FinalSummary 已在 for-loop 後統一呼叫 (使用 g_stop_reason),
        //           此處重複的摘要已移除。
    }

    free(u_tavg_h);
    free(v_tavg_h);
    free(w_tavg_h);
    free(ox_tavg_h);
    free(oy_tavg_h);
    free(oz_tavg_h);

    // ============================================================
    // [TAIL-RECOVERY 2026-04] 原 main.cu 於此處被截斷 (line 1834 mid-token).
    // 以下為最小化收尾: 印 stop reason, MPI_Finalize, return exit code.
    // 若原本尾端還有 cudaFree / fileIO 收尾, 請從備份補回。
    // ============================================================
    if (myid == 0) {
        printf("\n[STOP] Reason: %s\n", StopReasonStr(g_stop_reason));
        fflush(stdout);
    }

    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );
    CHECK_MPI( MPI_Finalize() );

    return StopReasonExitCode(g_stop_reason);
}
