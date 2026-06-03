#ifndef FILEIO_FILE
#define FILEIO_FILE

#include <unistd.h>//用到access, fsync
#include <sys/types.h>
#include <sys/stat.h>//用mkdir
#include <fcntl.h>   // open() for fsync on parent dir
#include <cstdio>    // std::rename for atomic rename
#include <cerrno>    // errno
#include <cstring>   // strerror
#include <cmath>     // fabs, fmax (Phase 5 dt drift check)
#include <fstream>
#include <sstream>
#include <string>
#include <iostream>
#include <iomanip>  // setprecision, fixed
#include <algorithm> // swap for byte-swap
using namespace std ;

static inline void vtk_swap_double(double *val) {
    char *p = reinterpret_cast<char*>(val);
    std::swap(p[0], p[7]); std::swap(p[1], p[6]);
    std::swap(p[2], p[5]); std::swap(p[3], p[4]);
}
static inline void vtk_write_double_be(ofstream &out, double v) {
    vtk_swap_double(&v);
    out.write(reinterpret_cast<const char*>(&v), sizeof(double));
} 
void wirte_ASCII_of_str(char * str, FILE *file);

/*第一段:創建資料夾*/
//PreCheckDir函數式的輔助函數1 
void ExistOrCreateDir(const char* doc) {
    //步驟一:創立資料夾 
	std::string path(doc);// path 是 C++ string 物件
	path = "./" + path;
    //檢查資料夾是否存在
	if (access(path.c_str(), F_OK) != 0) { //c++字串傳成對應的字元陣列 
        //不存在，用mkdir() 創建 
        if (mkdir(path.c_str(), S_IRWXU) == 0)
			std::cout << "folder " << path << " not exist, created"<< std::endl;// S_IRWXU = 擁有者可讀寫執行 
	}
}
//創立資料夾 "result" , "statistics" , "statistics/XXX" (XXX 為 32+3 個統計量子資料夾, 3 OMEGA 保留未使用)
void PreCheckDir() {
    //預先建立資料夾
	ExistOrCreateDir("result");//程式碼初始狀態使用
    //湍流統計//35 個子資料夾 (32 active + 3 OMEGA reserved)
	ExistOrCreateDir("statistics");
    if ( TBSWITCH ) {
		const int num_files = 36;
		std::string name[num_files] = {
        "U","V","W","P",//4
        "UU","UV","UW","VV","VW","WW",//6
        "PU","PV","PW","PP",//4
        "DUDX2","DUDY2","DUDZ2","DVDX2","DVDY2","DVDZ2","DWDX2","DWDY2","DWDZ2", //9
        "UUU","UUV","UUW","UVW","VVU","VVV","VVW","WWU","WWV","WWW",//10
        "OMEGA_X","OMEGA_Y","OMEGA_Z"};//3
		for( int i = 0; i < num_files; i++ ) {
			std::string fname = "./statistics/" + name[i];
			ExistOrCreateDir(fname.c_str());
		}
	}
    /*////////////////////////////////////////////*/
}
/*第二段:輸出速度場與分佈函數*/
//result系列輔助函數1.
void result_writebin(double* arr_h, const char *fname, const int myid){
    // 組合檔案路徑
    ostringstream oss;//輸出整數轉字串資料流
    oss << "./result/" << fname << "_" << myid << ".bin";
    string path = oss.str();

    // 用 C++ ofstream 開啟二進制檔案
    ofstream file(path.c_str(), ios::binary);
    if (!file) {
        cout << "Output data error, exit..." << endl;
        CHECK_MPI( MPI_Abort(MPI_COMM_WORLD, 1) );
    }

    // 寫入資料
    file.write(reinterpret_cast<char*>(arr_h), sizeof(double) * NX6 * NZ6 * NYD6);
    file.close();
}
// 寫入指定資料夾版本 (for binary checkpoint)
// ── 原子性寫入: 先寫 "<path>.part", 再 rename 為 "<path>".
//    rename(2) 在同一 filesystem 內為原子操作, 讀者看到的永遠是舊完整檔或新完整檔,
//    不會看到半寫入狀態. 可防止 timeout / OOM / crash 殺死 process 時留下壞檔.
void result_writebin_to(double* arr_h, const char *folder, const char *fname, const int myid){
    ostringstream oss;
    oss << "./" << folder << "/" << fname << "_" << myid << ".bin";
    string final_path = oss.str();
    string tmp_path   = final_path + ".part";

    // 1. 寫入 .part 暫存檔
    ofstream file(tmp_path.c_str(), ios::binary | ios::trunc);
    if (!file) {
        cout << "Checkpoint write error (open .part): " << tmp_path
             << " errno=" << errno << " (" << strerror(errno) << ")" << endl;
        CHECK_MPI( MPI_Abort(MPI_COMM_WORLD, 1) );
    }
    file.write(reinterpret_cast<char*>(arr_h), sizeof(double) * NX6 * NZ6 * NYD6);
    if (!file) {
        cout << "Checkpoint write error (write .part): " << tmp_path << endl;
        CHECK_MPI( MPI_Abort(MPI_COMM_WORLD, 1) );
    }
    file.flush();
    file.close();

    // 2. fsync .part 保證資料真的落盤 (防斷電/OOM 後 page cache 未刷新)
    int fd = open(tmp_path.c_str(), O_RDONLY);
    if (fd >= 0) { fsync(fd); close(fd); }

    // 3. 原子性 rename → final path
    if (std::rename(tmp_path.c_str(), final_path.c_str()) != 0) {
        cout << "Checkpoint rename error: " << tmp_path << " → " << final_path
             << " errno=" << errno << " (" << strerror(errno) << ")" << endl;
        CHECK_MPI( MPI_Abort(MPI_COMM_WORLD, 1) );
    }
}
//result系列輔助函數2.
void result_readbin(double *arr_h, const char *folder, const char *fname, const int myid){
    ostringstream oss;
    oss << "./" << folder << "/" << fname << "_" << myid << ".bin";
    string path = oss.str();

    ifstream file(path.c_str(), ios::binary);
    if (!file) {
        cout << "Read data error: " << path << ", exit...\n";
        CHECK_MPI( MPI_Abort(MPI_COMM_WORLD, 1) );
    }

    file.read(reinterpret_cast<char*>(arr_h), sizeof(double) * NX6 * NZ6 * NYD6);
    file.close();
}
//result系列主函數1.(寫檔案)
void result_writebin_velocityandf() {
    // 輸出 Paraview VTK (最終結果，分 GPU 子域輸出)
    ostringstream oss;
    oss << "./result/velocity_" << myid << "_Final.vtk";
    ofstream out(oss.str().c_str());
    // VTK Header
    out << "# vtk DataFile Version 3.0\n";
    out << "LBM Velocity Field\n";
    out << "ASCII\n";
    out << "DATASET STRUCTURED_GRID\n";
    // Z 計算點 k=3..NZ6-3 (含兩壁), 共 NZ6-6 點
    out << "DIMENSIONS " << NX6-6 << " " << NYD6-6 << " " << NZ6-6 << "\n";

    // 座標點
    int nPoints = (NX6-6) * (NYD6-6) * (NZ6-6);
    out << "POINTS " << nPoints << " double\n";
    out << fixed << setprecision(6);
    for( int k = 3; k < NZ6-3; k++ ){    // 包含壁面 k=3 和 k=NZ6-3
    for( int j = 3; j < NYD6-3; j++ ){
    for( int i = 3; i < NX6-3; i++ ){
        out << x_h[i] << " " << y_2d_h[j*NZ6+k] << " " << z_h[j*NZ6+k] << "\n";
    }}}

    // 速度向量
    out << "\nPOINT_DATA " << nPoints << "\n";
    out << "VECTORS velocity double\n";
    out << setprecision(15);
    for( int k = 3; k < NZ6-3; k++ ){    // 包含壁面 k=3 和 k=NZ6-3
    for( int j = 3; j < NYD6-3; j++ ){
    for( int i = 3; i < NX6-3; i++ ){
        int index = j*NZ6*NX6 + k*NX6 + i;
        out << u_h_p[index] << " " << v_h_p[index] << " " << w_h_p[index] << "\n";
    }}}
    out.close();
    ////////////////////////////////////////////////////////////////////////////
    
    cout << "\n----------- Start Output, myid = " << myid << " ----------\n";
    
    // 輸出力 (只有 rank 0)
    if( myid == 0 ) {
        ofstream fp_gg("./result/0_force.dat");
        fp_gg << fixed << setprecision(15) << Force_h[0];
        fp_gg.close();
    }
    
    // 輸出巨觀量 (rho, u, v, w)
    result_writebin(rho_h_p, "rho", myid);
    result_writebin(u_h_p,   "u",   myid);
    result_writebin(v_h_p,   "v",   myid);
    result_writebin(w_h_p,   "w",   myid);
    
    // 輸出分佈函數 (f0 ~ f18)
    for( int q = 0; q < 19; q++ ) {
        ostringstream fname;
        fname << "f" << q;
        result_writebin(fh_p[q], fname.str().c_str(), myid);
    }
}
//result系列主函數2.(讀檔案)
void result_readbin_velocityandf()
{
    PreCheckDir();

    const char* result = "result";

    ifstream fp_gg("./result/0_force.dat");
    fp_gg >> Force_h[0];
    fp_gg.close();

    CHECK_CUDA( cudaMemcpy(Force_d, Force_h, sizeof(double), cudaMemcpyHostToDevice) );

    result_readbin(rho_h_p, result, "rho", myid);
    result_readbin(u_h_p,   result, "u",   myid);
    result_readbin(v_h_p,   result, "v",   myid);
    result_readbin(w_h_p,   result, "w",   myid);

    result_readbin(fh_p[0],  result, "f0",  myid);
    result_readbin(fh_p[1],  result, "f1",  myid);
    result_readbin(fh_p[2],  result, "f2",  myid);
    result_readbin(fh_p[3],  result, "f3",  myid);
    result_readbin(fh_p[4],  result, "f4",  myid);
    result_readbin(fh_p[5],  result, "f5",  myid);
    result_readbin(fh_p[6],  result, "f6",  myid);
    result_readbin(fh_p[7],  result, "f7",  myid);
    result_readbin(fh_p[8],  result, "f8",  myid);
    result_readbin(fh_p[9],  result, "f9",  myid);
    result_readbin(fh_p[10], result, "f10", myid);
    result_readbin(fh_p[11], result, "f11", myid);
    result_readbin(fh_p[12], result, "f12", myid);
    result_readbin(fh_p[13], result, "f13", myid);
    result_readbin(fh_p[14], result, "f14", myid);
    result_readbin(fh_p[15], result, "f15", myid);
    result_readbin(fh_p[16], result, "f16", myid);
    result_readbin(fh_p[17], result, "f17", myid);
    result_readbin(fh_p[18], result, "f18", myid);
}

//=============================================================================
// Binary Checkpoint I/O — 保留完整 f 分佈函數 (含 f^neq)
//=============================================================================

// 週期性二進制 checkpoint (新格式, single accu_count):
//   永遠寫: f00~f18 + rho + meta.dat (20 files + metadata)
//   FTT >= 20 (accu_count > 0): + 36 統計量累加器 (33 RS + 3 渦度)
//   合計: 19(f) + 1(rho) + 33(stats) = 53 .bin + meta.dat
//
// ================================================================
// G1: Atomic directory-level staging (.WRITING → final)
// ----------------------------------------------------------------
// 所有檔案先寫入 "restart/checkpoint/step_X.WRITING/", 等全部落盤後, 最後
// 一次 rename(dir_writing, dir_final) 變成 "restart/checkpoint/step_X/".
// POSIX rename(dir) 在同一 filesystem 上保證原子性 — 整個 checkpoint
// 只有「完整」或「不存在」兩種狀態, 徹底消除半寫檔 race.
//
// 舊版的 metadata.dat.part / cv_*.bin.part 個別 rename 已移除 —
// 整個 dir rename 涵蓋所有檔案, 個別 .part 變成多此一舉.
//
// 失敗處理:
//   - 前次 save 被 SIGKILL 中斷 → 殘留 step_X.WRITING/: 本次開始時
//     rank 0 先 rm -rf 它 (ckpt_step 遞增, 舊殘留絕不會被誤用)
//   - 中途 rank crash → Barrier 不過 → 不會 rename → step_X/ 不存在
//     → jobscript 的 validate_checkpoint 會找更舊的候選
//   - rename 本身失敗 (EXDEV 跨 FS) → MPI_Abort fail loud
// ================================================================
void SaveBinaryCheckpoint(int ckpt_step) {
    // 1. 建立「.WRITING」暫存資料夾 — 所有 .bin/metadata 都寫進這裡
    //    (變數名保留 dir_name, 讓下方所有 result_writebin_to 不用改)
    ostringstream dir_final_oss, dir_writing_oss;
    dir_final_oss   << "restart/checkpoint/step_" << ckpt_step;
    dir_writing_oss << "restart/checkpoint/step_" << ckpt_step << ".WRITING";
    string dir_final = dir_final_oss.str();
    string dir_name  = dir_writing_oss.str();
    if (myid == 0) {
        ExistOrCreateDir("restart");
        ExistOrCreateDir("restart/checkpoint");
        // 清理前次中斷殘留的 .WRITING (若存在); 絕不觸及任何 step_X final dir.
        string rm_cmd = "rm -rf " + dir_name;
        int rc_rm = system(rm_cmd.c_str());
        (void)rc_rm;
        ExistOrCreateDir(dir_name.c_str());
    }
    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );

    // 2. 寫入 f00~f18 (分佈函數，含 f^neq)
    for (int q = 0; q < 19; q++) {
        char fname[8];
        sprintf(fname, "f%02d", q);
        result_writebin_to(fh_p[q], dir_name.c_str(), fname, myid);
    }

    // 3. 寫入 rho (瞬時密度)
    result_writebin_to(rho_h_p, dir_name.c_str(), "rho", myid);

    // 4. 寫入 36 統計量累加器 (FTT >= 20, accu_count > 0): 33 RS + 3 渦度
    if (accu_count > 0 && (int)TBSWITCH) {
        const size_t rs_bytes = (size_t)NX6 * NYD6 * NZ6 * sizeof(double);
        double *rs_tmp = (double*)malloc(rs_bytes);

        #define SAVE_STAT(gpu_ptr, name) \
            CHECK_CUDA(cudaMemcpy(rs_tmp, gpu_ptr, rs_bytes, cudaMemcpyDeviceToHost)); \
            result_writebin_to(rs_tmp, dir_name.c_str(), name, myid)

        // 一階矩 (3)
        SAVE_STAT(U, "sum_u");     SAVE_STAT(V, "sum_v");     SAVE_STAT(W, "sum_w");
        // 二階矩 (6)
        SAVE_STAT(UU, "sum_uu");   SAVE_STAT(UV, "sum_uv");   SAVE_STAT(UW, "sum_uw");
        SAVE_STAT(VV, "sum_vv");   SAVE_STAT(VW, "sum_vw");   SAVE_STAT(WW, "sum_ww");
        // 三階矩 (10)
        SAVE_STAT(UUU, "sum_uuu"); SAVE_STAT(UUV, "sum_uuv"); SAVE_STAT(UUW, "sum_uuw");
        SAVE_STAT(VVU, "sum_uvv"); SAVE_STAT(UVW, "sum_uvw"); SAVE_STAT(WWU, "sum_uww");
        SAVE_STAT(VVV, "sum_vvv"); SAVE_STAT(VVW, "sum_vvw"); SAVE_STAT(WWV, "sum_vww");
        SAVE_STAT(WWW, "sum_www");
        // 壓力 (5)
        SAVE_STAT(P, "sum_P");     SAVE_STAT(PP, "sum_PP");
        SAVE_STAT(PU, "sum_Pu");   SAVE_STAT(PV, "sum_Pv");   SAVE_STAT(PW, "sum_Pw");
        // 梯度平方 (9)
        SAVE_STAT(DUDX2, "sum_dudx2"); SAVE_STAT(DUDY2, "sum_dudy2"); SAVE_STAT(DUDZ2, "sum_dudz2");
        SAVE_STAT(DVDX2, "sum_dvdx2"); SAVE_STAT(DVDY2, "sum_dvdy2"); SAVE_STAT(DVDZ2, "sum_dvdz2");
        SAVE_STAT(DWDX2, "sum_dwdx2"); SAVE_STAT(DWDY2, "sum_dwdy2"); SAVE_STAT(DWDZ2, "sum_dwdz2");
        // 渦度累加器 (3) — restart 必存，否則 vorticity mean 歸零而其他累積量繼續，造成統計不一致
        SAVE_STAT(ox_tavg_d, "sum_ox"); SAVE_STAT(oy_tavg_d, "sum_oy"); SAVE_STAT(oz_tavg_d, "sum_oz");

        #undef SAVE_STAT
        free(rs_tmp);
    }

    // 5. CV 環形緩衝區 (rank 0 only, 紊流模式)
    //    只存 CV_WINDOW_FTT 視窗內的歷史數據 → restart 後可立即計算 CV
    //    格式: 時間排序 (oldest → newest), 3 個 binary 檔 + metadata 中的 cv_count
    int cv_saved_count = 0;
    if (myid == 0 && !IS_LAMINAR) {
        extern double uu_history[CV_WINDOW_SIZE], k_history[CV_WINDOW_SIZE], ftt_cv_history[CV_WINDOW_SIZE];
        extern int    cv_idx, cv_buf_count;

        double FTT_ckpt = (double)ckpt_step * dt_global / (double)flow_through_time;
        double ftt_cutoff = FTT_ckpt - CV_WINDOW_FTT;

        // 提取視窗內數據 (時間排序: oldest → newest)
        double *cv_uu_buf  = (double*)malloc(cv_buf_count * sizeof(double));
        double *cv_k_buf   = (double*)malloc(cv_buf_count * sizeof(double));
        double *cv_ftt_buf = (double*)malloc(cv_buf_count * sizeof(double));
        int n_saved = 0;

        for (int i = 0; i < cv_buf_count; i++) {
            int pos = (cv_idx - cv_buf_count + i + CV_WINDOW_SIZE) % CV_WINDOW_SIZE;
            if (ftt_cv_history[pos] >= ftt_cutoff) {
                cv_uu_buf[n_saved]  = uu_history[pos];
                cv_k_buf[n_saved]   = k_history[pos];
                cv_ftt_buf[n_saved] = ftt_cv_history[pos];
                n_saved++;
            }
        }
        cv_saved_count = n_saved;

        if (n_saved > 0) {
            // 寫入 3 個 binary 檔 — 直接寫 final name, 不再 .part → rename.
            // 原因 (G1): 整個 dir 會在函數末尾 rename(.WRITING → final), 所有
            // 檔案的原子性由 dir-level rename 保證, 個別 .part 變多此一舉.
            // fsync 保留 — 確保資料在 dir rename 前落盤.
            struct CVWrite { const char *name; double *buf; };
            CVWrite items[3] = {
                {"cv_uu_history.bin",  cv_uu_buf },
                {"cv_k_history.bin",   cv_k_buf  },
                {"cv_ftt_history.bin", cv_ftt_buf}
            };
            for (int t = 0; t < 3; ++t) {
                ostringstream pf;
                pf << "./" << dir_name << "/" << items[t].name;
                string p_final = pf.str();

                ofstream fout(p_final.c_str(), ios::binary | ios::trunc);
                fout.write(reinterpret_cast<char*>(items[t].buf), n_saved * sizeof(double));
                fout.flush();
                fout.close();

                int fd = open(p_final.c_str(), O_RDONLY);
                if (fd >= 0) { fsync(fd); close(fd); }
            }
        }

        free(cv_uu_buf);
        free(cv_k_buf);
        free(cv_ftt_buf);
    }

    // 6. metadata.dat (rank 0 only) — 直接寫 final name 到 .WRITING/ 內.
    //    不再個別 .part → rename, 整個 dir rename 保證原子性.
    //
    //    【重要】寫 metadata.dat 前 Barrier: 確保所有 rank 的 f*/rho/stats
    //    都已落盤完成. Dir rename 只應在所有 rank 的檔案都到位後執行.
    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );

    if (myid == 0) {
        double FTT_ckpt = (double)ckpt_step * dt_global / (double)flow_through_time;
        ostringstream meta_path_oss;
        meta_path_oss << "./" << dir_name << "/metadata.dat";
        string meta_path = meta_path_oss.str();

        ofstream meta(meta_path.c_str());
        // --- Schema header (Phase G6): 讓 load 端可以版本門檻拒絕未來不相容格式 ---
        meta << "checkpoint_version=2\n";
        meta << "mpi_rank_count=" << (int)jp << "\n";
        meta << "grid_dims=" << (int)NX6 << "," << (int)NYD6 << "," << (int)NZ6 << "\n";
        // --- 原有欄位 ---
        meta << "step=" << ckpt_step << "\n";
        meta << fixed << setprecision(15);
        meta << "FTT=" << FTT_ckpt << "\n";
        meta << "accu_count=" << accu_count << "\n";
        meta << "Force=" << Force_h[0] << "\n";
        // [RESTART-FIX] PID 控制器狀態 (evolution.h)
        //   缺這四行會讓 restart 後 Force_integral/error_prev 歸零 → F* 階躍
        extern double g_force_integral;
        extern double g_error_prev;
        extern bool   g_ctrl_initialized;
        extern bool   g_gehrke_activated;
        meta << "Force_integral=" << g_force_integral << "\n";
        meta << "error_prev=" << g_error_prev << "\n";
        meta << "ctrl_initialized=" << (g_ctrl_initialized ? 1 : 0) << "\n";
        meta << "gehrke_activated=" << (g_gehrke_activated ? 1 : 0) << "\n";
        meta << "dt_global=" << dt_global << "\n";
#if USE_TIMING
        meta << "gpu_time_ms=" << Timing_GetGPUTime_min() * 60000.0 << "\n";
#endif
        meta << "cv_count=" << cv_saved_count << "\n";
        meta.flush();
        meta.close();

        // fsync metadata (保證 dir rename 前資料已在磁碟上)
        int fd = open(meta_path.c_str(), O_RDONLY);
        if (fd >= 0) { fsync(fd); close(fd); }

        // ================================================================
        // G1: Atomic dir-level rename (.WRITING → final)
        // ----------------------------------------------------------------
        // 這是整個 SaveBinaryCheckpoint 的 commit point. 在這之前, 只存在
        // .WRITING/ 目錄; 之後, 只存在 step_X/ 目錄. 沒有中間狀態.
        //
        // 安全性: dir_final 應該不存在 (ckpt_step 單調遞增), 但保險起見
        // 先檢查; 若不幸已存在 (例如使用者手動複製過), 移除後再 rename.
        // ================================================================
        {
            struct stat st_final;
            if (stat(dir_final.c_str(), &st_final) == 0) {
                // dir_final 已存在 — 極罕見, 可能是 ckpt_step 撞到舊資料
                fprintf(stderr, "[CHECKPOINT] WARN: %s already exists, removing before rename\n",
                        dir_final.c_str());
                string rm_dup = "rm -rf " + dir_final;
                int rc_dup = system(rm_dup.c_str());
                (void)rc_dup;
            }
            if (std::rename(dir_name.c_str(), dir_final.c_str()) != 0) {
                fprintf(stderr, "[CHECKPOINT] FATAL: dir rename %s → %s failed (errno=%d, %s)\n",
                        dir_name.c_str(), dir_final.c_str(), errno, strerror(errno));
                fflush(stderr);
                CHECK_MPI( MPI_Abort(MPI_COMM_WORLD, 1) );
            }
        }

        int n_files = 20 + ((accu_count > 0 && (int)TBSWITCH) ? 36 : 0)
                         + (cv_saved_count > 0 ? 3 : 0);
        printf("[CHECKPOINT] Saved atomically: %s/ (FTT=%.2f, accu=%d, cv_buf=%d, %d files)\n",
               dir_final.c_str(), FTT_ckpt, accu_count, cv_saved_count, n_files);

        // === restart/checkpoint/latest symlink (G2 bonus) ===
        // 使用「相對路徑」作為 symlink target, 搬動專案目錄後仍有效.
        // 序列: unlink(latest) → symlink(step_X, latest).
        {
            ostringstream target_oss;
            target_oss << "step_" << ckpt_step;  // 相對路徑, 不含 "restart/checkpoint/"
            const char* link_path = "./restart/checkpoint/latest";
            unlink(link_path);  // 忽略 ENOENT (第一次不存在)
            if (symlink(target_oss.str().c_str(), link_path) != 0) {
                // 非致命: symlink 失敗只影響人工檢視便利, 不影響續跑正確性
                fprintf(stderr, "[CHECKPOINT] WARN: symlink latest → %s failed (errno=%d, %s)\n",
                        target_oss.str().c_str(), errno, strerror(errno));
            } else {
                printf("[CHECKPOINT] Updated restart/checkpoint/latest → %s\n",
                       target_oss.str().c_str());
            }
        }

        // === Rolling retention: keep newest 5 checkpoints ===
        // 使用 grep -v 過濾掉 .WRITING dir (正在寫的下一個 checkpoint, 不該被 purge).
        // version sort by step number; head -n -5 保留最後 5 個.
        int rc_ck = system(
            "ls -1dv ./restart/checkpoint/step_* 2>/dev/null | grep -v '\\.WRITING$' | head -n -5 | xargs -r rm -rf"
        );
        (void)rc_ck;
        printf("[CHECKPOINT] Rolling retention: kept newest 5, older purged\n");
    }
}

// 從 binary checkpoint 讀取 (新格式):
//   永遠讀: f00~f18 + rho + meta.dat
//   accu_count > 0: + 36 統計量累加器 (33 RS + 3 渦度)
//   u,v,w 宏觀量從 f0~f18 即時計算 (D3Q19 moments)
void LoadBinaryCheckpoint(const char* checkpoint_dir) {
    PreCheckDir();

    // D3Q19 速度向量 (for computing macroscopic from f)
    const int e_loc[19][3] = {
        {0,0,0},{1,0,0},{-1,0,0},{0,1,0},{0,-1,0},{0,0,1},{0,0,-1},
        {1,1,0},{-1,1,0},{1,-1,0},{-1,-1,0},
        {1,0,1},{-1,0,1},{1,0,-1},{-1,0,-1},
        {0,1,1},{0,-1,1},{0,1,-1},{0,-1,-1}
    };

    // 1. 讀取 metadata
    {
        ostringstream meta_path;
        meta_path << "./" << checkpoint_dir << "/metadata.dat";
        ifstream meta(meta_path.str().c_str());
        if (!meta.is_open()) {
            if (myid == 0) cout << "ERROR: Cannot open " << meta_path.str() << endl;
            CHECK_MPI( MPI_Abort(MPI_COMM_WORLD, 1) );
        }
        // Phase 5: dt_global 漂移檢查用 — -1 代表舊格式沒寫 dt_global
        double dt_saved = -1.0;

        // Phase G6: schema / 拓撲一致性檢查變數 (默認 = 舊格式, 缺欄位視為 v1)
        int ckpt_version  = 1;        // 1 = 無 checkpoint_version 欄位之舊檔
        int ckpt_rank_cnt = -1;       // -1 = 未寫; >=0 = 舊檔的 MPI rank 數
        int ckpt_nx6 = -1, ckpt_nyd6 = -1, ckpt_nz6 = -1;  // -1 = 未寫

        string line;
        while (getline(meta, line)) {
            if (line.find("checkpoint_version=") == 0)
                sscanf(line.c_str() + 19, "%d", &ckpt_version);
            else if (line.find("mpi_rank_count=") == 0)
                sscanf(line.c_str() + 15, "%d", &ckpt_rank_cnt);
            else if (line.find("grid_dims=") == 0)
                sscanf(line.c_str() + 10, "%d,%d,%d", &ckpt_nx6, &ckpt_nyd6, &ckpt_nz6);
            else if (line.find("step=") == 0)
                sscanf(line.c_str() + 5, "%d", &restart_step);
            else if (line.find("Force=") == 0)
                sscanf(line.c_str() + 6, "%lf", &Force_h[0]);
            // [RESTART-FIX] PID 控制器狀態讀取 (缺欄位時維持 extern 全域預設值 0/false)
            else if (line.find("Force_integral=") == 0) {
                extern double g_force_integral;
                sscanf(line.c_str() + 15, "%lf", &g_force_integral);
            }
            else if (line.find("error_prev=") == 0) {
                extern double g_error_prev;
                sscanf(line.c_str() + 11, "%lf", &g_error_prev);
            }
            else if (line.find("ctrl_initialized=") == 0) {
                extern bool g_ctrl_initialized;
                int v = 0; sscanf(line.c_str() + 17, "%d", &v);
                g_ctrl_initialized = (v != 0);
            }
            else if (line.find("gehrke_activated=") == 0) {
                extern bool g_gehrke_activated;
                int v = 0; sscanf(line.c_str() + 17, "%d", &v);
                g_gehrke_activated = (v != 0);
            }
            // ================================================================
            // [UNIFIED SPEC] accu_count 為統計累積唯一指標, 不接受任何別名.
            // ----------------------------------------------------------------
            // 歷史包袱清理 (2026-04 起): 舊版曾存過 vel_avg_count= / rey_avg_count=
            // 兩個欄位, load 端靠欄位名猜版本. 現統一規範如下:
            //   * Save 端 (本檔 line 372) 只寫 accu_count=
            //   * Load 端只認 accu_count=
            //   * 33 統計量累加器 (sum_u..sum_dwdz2) + 3 渦度全部共用此計數
            //   * 缺欄位 → accu_count 維持 main.cu 的初值 0 → FTT-gate 自動
            //     跳過 stats 讀取 (line 659: if(accu_count>0 && TBSWITCH))
            //     也就是說: 2026-04 前的舊 checkpoint 續跑會失去統計累積,
            //     但 f/rho/Force/dt_global 仍正確 — fail-safe 而非 fail-loud,
            //     因為統計可以從 FTT_STATS_START 重新累積,物理場不能.
            //   * 未來新增統計累加器一律併入此 accu_count,不再開新計數器.
            // ================================================================
            else if (line.find("accu_count=") == 0)
                sscanf(line.c_str() + 11, "%d", &accu_count);
            else if (line.find("gpu_time_ms=") == 0) {
                extern double g_restored_gpu_ms;
                sscanf(line.c_str() + 12, "%lf", &g_restored_gpu_ms);
            }
            // Phase 5: dt_global — 防網格/Re 被改過造成物理不一致續跑
            else if (line.find("dt_global=") == 0) {
                sscanf(line.c_str() + 10, "%lf", &dt_saved);
            }
        }
        meta.close();

        // ============================================================
        // Phase G6: schema / 拓撲一致性門檻
        // ------------------------------------------------------------
        // 檢查三件事, 任一失敗 → 所有 rank 同步 MPI_Abort, fail loud.
        //   (a) checkpoint_version: 僅接受 <= 2 (本二進位支援的最高版號)
        //       未來若再升到 v3 (例如加 dt_per_point for local time step),
        //       舊 a.out 載入新 checkpoint 會在這裡被擋, 避免靜默讀錯.
        //   (b) mpi_rank_count: 續跑的 rank 數必須與 checkpoint 一致
        //       per-rank 檔案大小 = 固定 NX6*NYD6*NZ6, rank 數錯直接讀錯資料.
        //       (舊檔缺欄位 -> ckpt_rank_cnt = -1 -> 跳過檢查, 維持向後相容)
        //   (c) grid_dims: NX6,NYD6,NZ6 必須與當前編譯一致
        //       Phase 5 的 dt_global 只能擋物理不一致, 擋不住「dt 湊巧相同
        //       但 NY 被改了」. grid_dims 是最後一道防線.
        // ============================================================
        {
            int abort_flag = 0;
            if (myid == 0) {
                const int BIN_SUPPORTED_VERSION = 2;
                if (ckpt_version > BIN_SUPPORTED_VERSION) {
                    fprintf(stderr,
                        "\n[FATAL][G6] checkpoint_version=%d newer than this binary supports (%d)\n"
                        "  Checkpoint dir : %s\n"
                        "  Policy         : refuse to load to prevent silent data corruption.\n"
                        "                   Rebuild with an updated a.out, or start cold.\n",
                        ckpt_version, BIN_SUPPORTED_VERSION, checkpoint_dir);
                    abort_flag = 1;
                }
                if (!abort_flag && ckpt_rank_cnt > 0 && ckpt_rank_cnt != (int)jp) {
                    fprintf(stderr,
                        "\n[FATAL][G6] mpi_rank_count mismatch\n"
                        "  Checkpoint saved with %d ranks, current run uses %d ranks\n"
                        "  Per-rank file layout is fixed; cannot load across different rank counts.\n"
                        "  Fix: resubmit with --ntasks-per-node=%d, or cold-start for new decomposition.\n",
                        ckpt_rank_cnt, (int)jp, ckpt_rank_cnt);
                    abort_flag = 1;
                }
                if (!abort_flag && ckpt_nx6 > 0 &&
                    (ckpt_nx6 != (int)NX6 || ckpt_nyd6 != (int)NYD6 || ckpt_nz6 != (int)NZ6))
                {
                    fprintf(stderr,
                        "\n[FATAL][G6] grid_dims mismatch\n"
                        "  Checkpoint : NX6=%d NYD6=%d NZ6=%d\n"
                        "  Current    : NX6=%d NYD6=%d NZ6=%d\n"
                        "  Probable cause : NX/NY/NZ or jp changed in variables.h after save.\n"
                        "  Policy         : refuse to load (bin size would mismatch -> garbage).\n",
                        ckpt_nx6, ckpt_nyd6, ckpt_nz6,
                        (int)NX6, (int)NYD6, (int)NZ6);
                    abort_flag = 1;
                }
                if (!abort_flag) {
                    printf("[G6] Schema OK: version=%d rank_count=%s grid=%s ✓\n",
                           ckpt_version,
                           (ckpt_rank_cnt > 0 ? std::to_string(ckpt_rank_cnt).c_str() : "(legacy, skipped)"),
                           (ckpt_nx6      > 0 ? "match" : "(legacy, skipped)"));
                }
            }
            // 廣播決策, 避免部分 rank 繼續跑
            CHECK_MPI( MPI_Bcast(&abort_flag, 1, MPI_INT, 0, MPI_COMM_WORLD) );
            if (abort_flag) {
                fflush(stderr);
                CHECK_MPI( MPI_Abort(MPI_COMM_WORLD, 1) );
            }
        }

        // ============================================================
        // Phase 5: dt_global 漂移檢查
        // ------------------------------------------------------------
        // 若 checkpoint 存檔時的 dt_global 與當前計算值不一致,
        // 代表使用者可能修改了網格 (NXD/NYD/NZ)、Re、CFL 等參數,
        // f 分布函數的物理時間尺度已與新 dt 脫節. 強制中止, fail loud.
        //
        // 兩段閾值:
        //   |drift_rel| > 1e-6  → FATAL MPI_Abort (明顯參數變更)
        //   |drift_rel| > 1e-10 → WARN 但繼續 (浮點累積誤差 / MPI_MIN 順序)
        //   舊格式 (dt_saved = -1) → WARN 跳過 (向後相容)
        //
        // 只在 rank 0 執行, 結論廣播後全體 abort 以避免部分 rank 繼續跑.
        // ============================================================
        {
            int abort_flag = 0;
            if (myid == 0) {
                if (dt_saved < 0.0) {
                    printf("[Phase5] WARN: metadata.dat 無 dt_global 欄位 (舊格式), 跳過漂移檢查\n");
                } else {
                    double diff     = dt_global - dt_saved;
                    double drift    = fabs(diff);
                    double drift_rel= drift / fmax(fabs(dt_saved), 1e-30);
                    printf("[Phase5] dt_global check: saved=%.15e  now=%.15e  |Δ|=%.3e  |Δ/dt|=%.3e\n",
                           dt_saved, dt_global, drift, drift_rel);
                    if (drift_rel > 1e-6) {
                        fprintf(stderr,
                            "\n[FATAL][Phase5] dt_global drift too large: |Δ/dt|=%.3e > 1e-6\n"
                            "  Checkpoint was saved with dt=%.15e\n"
                            "  Current run computed    dt=%.15e\n"
                            "  Probable cause : grid (NXD/NYD/NZ) or Re or CFL changed\n"
                            "                   between save and restart.\n"
                            "  Policy         : refuse to continue with mismatched physics.\n"
                            "                   Use a fresh cold start (INIT=0) or\n"
                            "                   restore the original parameters.\n",
                            drift_rel, dt_saved, dt_global);
                        fflush(stderr);
                        abort_flag = 1;
                    } else if (drift_rel > 1e-10) {
                        printf("[Phase5] WARN: dt drift %.3e > 1e-10 (floating-point noise, OK to continue)\n",
                               drift_rel);
                    } else {
                        printf("[Phase5] dt_global consistent within 1e-10 ✓\n");
                    }
                }
            }
            // 廣播 abort 決策, 所有 rank 一致動作
            CHECK_MPI( MPI_Bcast(&abort_flag, 1, MPI_INT, 0, MPI_COMM_WORLD) );
            if (abort_flag) {
                CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );
                MPI_Abort(MPI_COMM_WORLD, 3);
            }
        }
    }
    CHECK_CUDA( cudaMemcpy(Force_d, Force_h, sizeof(double), cudaMemcpyHostToDevice) );

    // 2. 讀取 f00~f18 (含完整 f^neq!)
    for (int q = 0; q < 19; q++) {
        char fname[8];
        sprintf(fname, "f%02d", q);
        // Try new format first (f00), fall back to old format (f0)
        ostringstream probe;
        probe << "./" << checkpoint_dir << "/" << fname << "_" << myid << ".bin";
        ifstream test_file(probe.str().c_str(), ios::binary);
        if (test_file.is_open()) {
            test_file.close();
            result_readbin(fh_p[q], checkpoint_dir, fname, myid);
        } else {
            // Old format: f0, f1, ..., f18
            ostringstream old_fname;
            old_fname << "f" << q;
            result_readbin(fh_p[q], checkpoint_dir, old_fname.str().c_str(), myid);
        }
    }

    // 3. 讀取 rho
    result_readbin(rho_h_p, checkpoint_dir, "rho", myid);

    // 4. 從 f0~f18 計算 u, v, w (不再從檔案讀取)
    {
        const size_t nTotal = (size_t)NX6 * NYD6 * NZ6;
        for (size_t idx = 0; idx < nTotal; idx++) {
            double rho_loc = rho_h_p[idx];
            double mx = 0.0, my = 0.0, mz = 0.0;
            for (int q = 0; q < 19; q++) {
                double fq = fh_p[q][idx];
                mx += e_loc[q][0] * fq;
                my += e_loc[q][1] * fq;
                mz += e_loc[q][2] * fq;
            }
            if (rho_loc > 1e-15) {
                u_h_p[idx] = mx / rho_loc;
                v_h_p[idx] = my / rho_loc;
                w_h_p[idx] = mz / rho_loc;
            } else {
                u_h_p[idx] = 0.0;
                v_h_p[idx] = 0.0;
                w_h_p[idx] = 0.0;
            }
        }
        if (myid == 0) printf("[CHECKPOINT] u,v,w computed from f00~f18 (D3Q19 moments)\n");
    }

    // 5. 讀取 36 統計量累加器 (if accu_count > 0): 33 RS + 3 渦度
    if (accu_count > 0 && (int)TBSWITCH) {
        const size_t rs_bytes = (size_t)NX6 * NYD6 * NZ6 * sizeof(double);
        double *rs_tmp = (double*)malloc(rs_bytes);

        #define LOAD_STAT(gpu_ptr, name, old_name) { \
            ostringstream _probe; \
            _probe << "./" << checkpoint_dir << "/" << name << "_" << myid << ".bin"; \
            ifstream _test(_probe.str().c_str(), ios::binary); \
            if (_test.is_open()) { _test.close(); \
                result_readbin(rs_tmp, checkpoint_dir, name, myid); \
            } else { \
                result_readbin(rs_tmp, checkpoint_dir, old_name, myid); \
            } \
            CHECK_CUDA(cudaMemcpy(gpu_ptr, rs_tmp, rs_bytes, cudaMemcpyHostToDevice)); \
        }

        // 一階矩 (3)
        LOAD_STAT(U, "sum_u", "RS_U");   LOAD_STAT(V, "sum_v", "RS_V");   LOAD_STAT(W, "sum_w", "RS_W");
        // 二階矩 (6)
        LOAD_STAT(UU, "sum_uu", "RS_UU"); LOAD_STAT(UV, "sum_uv", "RS_UV"); LOAD_STAT(UW, "sum_uw", "RS_UW");
        LOAD_STAT(VV, "sum_vv", "RS_VV"); LOAD_STAT(VW, "sum_vw", "RS_VW"); LOAD_STAT(WW, "sum_ww", "RS_WW");
        // 三階矩 (10)
        LOAD_STAT(UUU, "sum_uuu", "RS_UUU"); LOAD_STAT(UUV, "sum_uuv", "RS_UUV"); LOAD_STAT(UUW, "sum_uuw", "RS_UUW");
        LOAD_STAT(VVU, "sum_uvv", "RS_VVU"); LOAD_STAT(UVW, "sum_uvw", "RS_UVW"); LOAD_STAT(WWU, "sum_uww", "RS_WWU");
        LOAD_STAT(VVV, "sum_vvv", "RS_VVV"); LOAD_STAT(VVW, "sum_vvw", "RS_VVW"); LOAD_STAT(WWV, "sum_vww", "RS_WWV");
        LOAD_STAT(WWW, "sum_www", "RS_WWW");
        // 壓力 (5)
        LOAD_STAT(P, "sum_P", "RS_P");   LOAD_STAT(PP, "sum_PP", "RS_PP");
        LOAD_STAT(PU, "sum_Pu", "RS_PU"); LOAD_STAT(PV, "sum_Pv", "RS_PV"); LOAD_STAT(PW, "sum_Pw", "RS_PW");
        // 梯度平方 (9)
        LOAD_STAT(DUDX2, "sum_dudx2", "RS_DUDX2"); LOAD_STAT(DUDY2, "sum_dudy2", "RS_DUDY2"); LOAD_STAT(DUDZ2, "sum_dudz2", "RS_DUDZ2");
        LOAD_STAT(DVDX2, "sum_dvdx2", "RS_DVDX2"); LOAD_STAT(DVDY2, "sum_dvdy2", "RS_DVDY2"); LOAD_STAT(DVDZ2, "sum_dvdz2", "RS_DVDZ2");
        LOAD_STAT(DWDX2, "sum_dwdx2", "RS_DWDX2"); LOAD_STAT(DWDY2, "sum_dwdy2", "RS_DWDY2"); LOAD_STAT(DWDZ2, "sum_dwdz2", "RS_DWDZ2");
        // 渦度累加器 (3) — 對應 SaveBinaryCheckpoint 的 sum_ox/oy/oz
        LOAD_STAT(ox_tavg_d, "sum_ox", "sum_ox"); LOAD_STAT(oy_tavg_d, "sum_oy", "sum_oy"); LOAD_STAT(oz_tavg_d, "sum_oz", "sum_oz");

        #undef LOAD_STAT
        free(rs_tmp);

        // Copy sum_u/v/w → u_tavg_d (VTK backward compat: AccumulateTavg uses u_tavg)
        if (u_tavg_h != NULL) {
            CHECK_CUDA( cudaMemcpy(u_tavg_h, U, rs_bytes, cudaMemcpyDeviceToHost) );
            CHECK_CUDA( cudaMemcpy(v_tavg_h, V, rs_bytes, cudaMemcpyDeviceToHost) );
            CHECK_CUDA( cudaMemcpy(w_tavg_h, W, rs_bytes, cudaMemcpyDeviceToHost) );
            CHECK_CUDA( cudaMemcpy(u_tavg_d, U, rs_bytes, cudaMemcpyDeviceToDevice) );
            CHECK_CUDA( cudaMemcpy(v_tavg_d, V, rs_bytes, cudaMemcpyDeviceToDevice) );
            CHECK_CUDA( cudaMemcpy(w_tavg_d, W, rs_bytes, cudaMemcpyDeviceToDevice) );
        }

        // 同步渦度 host 端 mirror (避免 FTT_restart >= FTT_STATS_START 時 host/device 不一致)
        if (ox_tavg_h != NULL) {
            CHECK_CUDA( cudaMemcpy(ox_tavg_h, ox_tavg_d, rs_bytes, cudaMemcpyDeviceToHost) );
            CHECK_CUDA( cudaMemcpy(oy_tavg_h, oy_tavg_d, rs_bytes, cudaMemcpyDeviceToHost) );
            CHECK_CUDA( cudaMemcpy(oz_tavg_h, oz_tavg_d, rs_bytes, cudaMemcpyDeviceToHost) );
        }

        if (myid == 0)
            printf("[CHECKPOINT] Statistics loaded: 36 arrays (incl. vorticity) from %s/ (accu_count=%d)\n",
                   checkpoint_dir, accu_count);
    }

    // 6. CV 環形緩衝區恢復 (紊流模式, 如果 checkpoint 有存)
    //    讀取 cv_count + 3 個 binary 檔 → 填入環形緩衝區
    //    恢復後 cv_buf_count = cv_count, cv_idx = cv_count
    //    → 如果數據涵蓋完整 CV_WINDOW_FTT, restart 後立即可算 CV
    {
        extern double uu_history[CV_WINDOW_SIZE], k_history[CV_WINDOW_SIZE], ftt_cv_history[CV_WINDOW_SIZE];
        extern int    cv_idx, cv_buf_count;

        int cv_count_loaded = 0;

        // 從 metadata.dat 重新讀取 cv_count (之前的 parse 迴圈可能沒抓)
        {
            ostringstream mp;
            mp << "./" << checkpoint_dir << "/metadata.dat";
            ifstream mf(mp.str().c_str());
            if (mf.is_open()) {
                string ln;
                while (getline(mf, ln)) {
                    if (ln.find("cv_count=") == 0)
                        sscanf(ln.c_str() + 9, "%d", &cv_count_loaded);
                }
                mf.close();
            }
        }

        if (cv_count_loaded > 0 && !IS_LAMINAR) {
            double *cv_uu_buf  = (double*)malloc(cv_count_loaded * sizeof(double));
            double *cv_k_buf   = (double*)malloc(cv_count_loaded * sizeof(double));
            double *cv_ftt_buf = (double*)malloc(cv_count_loaded * sizeof(double));
            bool all_ok = true;

            {
                ostringstream p_uu;
                p_uu << "./" << checkpoint_dir << "/cv_uu_history.bin";
                ifstream f_uu(p_uu.str().c_str(), ios::binary);
                if (!f_uu.is_open()) { all_ok = false; }
                else { f_uu.read(reinterpret_cast<char*>(cv_uu_buf), cv_count_loaded * sizeof(double));
                       if (!f_uu.good()) all_ok = false; f_uu.close(); }
            }
            {
                ostringstream p_k;
                p_k << "./" << checkpoint_dir << "/cv_k_history.bin";
                ifstream f_k(p_k.str().c_str(), ios::binary);
                if (!f_k.is_open()) { all_ok = false; }
                else { f_k.read(reinterpret_cast<char*>(cv_k_buf), cv_count_loaded * sizeof(double));
                       if (!f_k.good()) all_ok = false; f_k.close(); }
            }
            {
                ostringstream p_ftt;
                p_ftt << "./" << checkpoint_dir << "/cv_ftt_history.bin";
                ifstream f_ftt(p_ftt.str().c_str(), ios::binary);
                if (!f_ftt.is_open()) { all_ok = false; }
                else { f_ftt.read(reinterpret_cast<char*>(cv_ftt_buf), cv_count_loaded * sizeof(double));
                       if (!f_ftt.good()) all_ok = false; f_ftt.close(); }
            }

            if (all_ok) {
                // 填入環形緩衝區 (index 0 → cv_count_loaded-1)
                int n_load = (cv_count_loaded < CV_WINDOW_SIZE)
                           ? cv_count_loaded : CV_WINDOW_SIZE;
                // 如果 checkpoint 存的數據超過 buffer 容量, 只取最新的
                int offset = cv_count_loaded - n_load;
                for (int i = 0; i < n_load; i++) {
                    uu_history[i]      = cv_uu_buf[offset + i];
                    k_history[i]       = cv_k_buf[offset + i];
                    ftt_cv_history[i]  = cv_ftt_buf[offset + i];
                }
                cv_buf_count = n_load;
                cv_idx       = n_load % CV_WINDOW_SIZE;

                if (myid == 0)
                    printf("[CHECKPOINT] CV buffer restored: %d entries "
                           "(FTT range [%.2f, %.2f]) → CV available immediately\n",
                           n_load, cv_ftt_buf[offset], cv_ftt_buf[cv_count_loaded - 1]);
            } else {
                if (myid == 0)
                    printf("[CHECKPOINT] CV buffer files not found or corrupt "
                           "— starting fresh (need %.0f FTT to fill)\n", CV_WINDOW_FTT);
            }

            free(cv_uu_buf);
            free(cv_k_buf);
            free(cv_ftt_buf);
        } else if (cv_count_loaded == 0 && !IS_LAMINAR) {
            if (myid == 0)
                printf("[CHECKPOINT] No CV buffer in checkpoint (old format) "
                       "— starting fresh (need %.0f FTT to fill)\n", CV_WINDOW_FTT);
        }
    }

    if (myid == 0)
        printf("[CHECKPOINT] Loaded: %s/ → step=%d, Force=%.6e, accu=%d\n",
               checkpoint_dir, restart_step, Force_h[0], accu_count);
}

/*第三段:輸出湍統計量*/
//1.statistics系列輔助函數1.(寫主機端資料入bin)
void statistics_writebin(double *arr_d, const char *fname, const int myid){
    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );
    //先傳入裝置端
    const size_t nBytes = NX6 * NYD6 * NZ6 * sizeof(double);
    double *arr_h = (double*)malloc(nBytes);
    //複製回主機端
    CHECK_CUDA( cudaMemcpy(arr_h, arr_d, nBytes, cudaMemcpyDeviceToHost) );

    // 組合檔案路徑//利用主機端變數寫檔案
    ostringstream oss;
    oss << "./statistics/" << fname << "/" << fname << "_" << myid << ".bin";
    
    // C++ ofstream 寫入二進制
    ofstream file(oss.str().c_str(), ios::binary);
    for( int k = 3; k < NZ6-3;  k++ ){    // 包含壁面 k=3 和 k=NZ6-3(top wall)
    for( int j = 3; j < NYD6-3; j++ ){
    for( int i = 3; i < NX6-3;  i++ ){
        int index = j*NX6*NZ6 + k*NX6 + i;
        file.write(reinterpret_cast<char*>(&arr_h[index]), sizeof(double));
    }}}
    file.close();
    free( arr_h );
}
//2.statistics系列輔助函數2.(讀bin到主機端資料)
void statistics_readbin(double * arr_d, const char *fname, const int myid){
    const size_t nBytes = NX6 * NYD6 * NZ6 * sizeof(double);
    double *arr_h = (double*)malloc(nBytes);

    // 組合檔案路徑
    ostringstream oss;
    oss << "./statistics/" << fname << "/" << fname << "_" << myid << ".bin";
    
    // C++ ifstream 讀取二進制
    ifstream file(oss.str().c_str(), ios::binary);
    for( int k = 3; k < NZ6-3;  k++ ){    // 包含壁面 k=3 和 k=NZ6-3(top wall)
    for( int j = 3; j < NYD6-3; j++ ){
    for( int i = 3; i < NX6-3;  i++ ){
        const int index = j*NX6*NZ6 + k*NX6 + i;
        file.read(reinterpret_cast<char*>(&arr_h[index]), sizeof(double));
    }}}
    file.close();
    //從主機端複製資料到裝置端
    CHECK_CUDA( cudaMemcpy(arr_d, arr_h, nBytes, cudaMemcpyHostToDevice) );
    //釋放主機端
    free( arr_h );
    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );
}
//3.statistics系列主函數1.
void statistics_writebin_stress(){
    if( myid == 0 ) {
        ofstream fp_accu("./statistics/accu.dat");
        fp_accu << accu_count;
        fp_accu.close();
    }
    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );
    statistics_writebin(U, "U", myid);
	statistics_writebin(V, "V", myid);
	statistics_writebin(W, "W", myid);
	statistics_writebin(P, "P", myid);//4
	statistics_writebin(UU, "UU", myid);
	statistics_writebin(UV, "UV", myid);
	statistics_writebin(UW, "UW", myid);
	statistics_writebin(VV, "VV", myid);
	statistics_writebin(VW, "VW", myid);
	statistics_writebin(WW, "WW", myid);
	statistics_writebin(PU, "PU", myid);
	statistics_writebin(PV, "PV", myid);
	statistics_writebin(PW, "PW", myid);
	statistics_writebin(PP, "PP", myid);//5
	statistics_writebin(DUDX2, "DUDX2", myid);
	statistics_writebin(DUDY2, "DUDY2", myid);
	statistics_writebin(DUDZ2, "DUDZ2", myid);
	statistics_writebin(DVDX2, "DVDX2", myid);
	statistics_writebin(DVDY2, "DVDY2", myid);
	statistics_writebin(DVDZ2, "DVDZ2", myid);
	statistics_writebin(DWDX2, "DWDX2", myid);
	statistics_writebin(DWDY2, "DWDY2", myid);
	statistics_writebin(DWDZ2, "DWDZ2", myid);//9.
	statistics_writebin(UUU, "UUU", myid);
	statistics_writebin(UUV, "UUV", myid);
	statistics_writebin(UUW, "UUW", myid);
	statistics_writebin(UVW, "UVW", myid);
	statistics_writebin(VVU, "VVU", myid);
	statistics_writebin(VVV, "VVV", myid);
	statistics_writebin(VVW, "VVW", myid);
	statistics_writebin(WWU, "WWU", myid);
	statistics_writebin(WWV, "WWV", myid);
	statistics_writebin(WWW, "WWW", myid);//9.
	//statistics_writebin(OMEGA_X, "OMEGA_X", myid);
	//statistics_writebin(OMEGA_Y, "OMEGA_Y", myid);
	//statistics_writebin(OMEGA_Z, "OMEGA_Z", myid);//3.
}
//4.statistics系列主函數2.
void statistics_readbin_stress() {
    ifstream fp_accu("./statistics/accu.dat");
    fp_accu >> accu_count;
    fp_accu.close();
    if (myid == 0) printf("  statistics_readbin_stress: accu_count=%d loaded from accu.dat\n", accu_count);

    statistics_readbin(U, "U", myid);
	statistics_readbin(V, "V", myid);
	statistics_readbin(W, "W", myid);
	statistics_readbin(P, "P", myid);
	statistics_readbin(UU, "UU", myid);
	statistics_readbin(UV, "UV", myid);
	statistics_readbin(UW, "UW", myid);
	statistics_readbin(VV, "VV", myid);
	statistics_readbin(VW, "VW", myid);
	statistics_readbin(WW, "WW", myid);
	statistics_readbin(PU, "PU", myid);
	statistics_readbin(PV, "PV", myid);
	statistics_readbin(PW, "PW", myid);
	statistics_readbin(PP, "PP", myid);
	statistics_readbin(DUDX2, "DUDX2", myid);
	statistics_readbin(DUDY2, "DUDY2", myid);
	statistics_readbin(DUDZ2, "DUDZ2", myid);
	statistics_readbin(DVDX2, "DVDX2", myid);
	statistics_readbin(DVDY2, "DVDY2", myid);
	statistics_readbin(DVDZ2, "DVDZ2", myid);
	statistics_readbin(DWDX2, "DWDX2", myid);
	statistics_readbin(DWDY2, "DWDY2", myid);
	statistics_readbin(DWDZ2, "DWDZ2", myid);
	statistics_readbin(UUU, "UUU", myid);
	statistics_readbin(UUV, "UUV", myid);
	statistics_readbin(UUW, "UUW", myid);
	statistics_readbin(UVW, "UVW", myid);
	statistics_readbin(VVU, "VVU", myid);
	statistics_readbin(VVV, "VVV", myid);
	statistics_readbin(VVW, "VVW", myid);
	statistics_readbin(WWU, "WWU", myid);
	statistics_readbin(WWV, "WWV", myid);
	statistics_readbin(WWW, "WWW", myid);
	//statistics_readbin(OMEGA_X, "OMEGA_X", myid);
	//statistics_readbin(OMEGA_Y, "OMEGA_Y", myid);
	//statistics_readbin(OMEGA_Z, "OMEGA_Z", myid);
	CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );
}
// ============================================================================
// GPU-count independent (merged) binary statistics I/O
// ============================================================================
// File format: raw double[(NZ6-6) × NY × (NX6-6)] in k→j_global→i order
// No header — dimensions implied by code's NX, NY, NZ defines.
// Only interior points stored (no ghost/buffer), same as per-rank version.
// j-mapping: j_global = myid * stride + (j_local - 3), stride = NY/jp
//
// Write: each rank packs stride unique j-points → MPI_Gather → rank 0 writes single file
// Read:  every rank reads full file → extracts stride+1 j-points (including overlap)

// Single-array merged write (GPU array → single merged .bin file)
void statistics_writebin_merged(double *arr_d, const char *fname) {
    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );
    const size_t nBytes = (size_t)NX6 * NYD6 * NZ6 * sizeof(double);
    double *arr_h = (double*)malloc(nBytes);
    CHECK_CUDA( cudaMemcpy(arr_h, arr_d, nBytes, cudaMemcpyDeviceToHost) );

    const int nx = NX6 - 6;       // interior x-points (i=3..NX6-4)
    const int ny = NY;             // total unique y-points (no overlap)
    const int nz = NZ6 - 6;       // interior z-points (k=3..NZ6-3, includes top wall)
    const int stride = NY / jp;    // unique j per rank

    // Pack local data: stride unique j-points (j_local = 3..3+stride-1, skip overlap at NYD6-4)
    const int local_count = nz * stride * nx;
    double *send_buf = (double*)malloc(local_count * sizeof(double));
    int idx = 0;
    for (int k = 3; k < NZ6 - 3; k++)
        for (int jl = 3; jl < 3 + stride; jl++)
            for (int i = 3; i < NX6 - 3; i++)
                send_buf[idx++] = arr_h[jl * NX6 * NZ6 + k * NX6 + i];

    // Gather to rank 0
    double *recv_buf = NULL;
    if (myid == 0) recv_buf = (double*)malloc((size_t)local_count * jp * sizeof(double));
    MPI_Gather(send_buf, local_count, MPI_DOUBLE,
               recv_buf, local_count, MPI_DOUBLE, 0, MPI_COMM_WORLD);

    // Rank 0: reorder [rank][k][j_local][i] → [k][j_global][i] and write
    if (myid == 0) {
        double *global_buf = (double*)malloc((size_t)nz * ny * nx * sizeof(double));
        for (int r = 0; r < jp; r++) {
            int j_offset = r * stride;
            double *rank_data = recv_buf + (size_t)r * local_count;
            int ridx = 0;
            for (int kk = 0; kk < nz; kk++)
                for (int jl = 0; jl < stride; jl++)
                    for (int ii = 0; ii < nx; ii++)
                        global_buf[(size_t)kk * ny * nx + (j_offset + jl) * nx + ii] = rank_data[ridx++];
        }

        ostringstream oss;
        oss << "./statistics/" << fname << "/" << fname << "_merged.bin";
        ofstream file(oss.str().c_str(), ios::binary);
        file.write(reinterpret_cast<char*>(global_buf), (size_t)nz * ny * nx * sizeof(double));
        file.close();
        free(global_buf);
        free(recv_buf);
    }

    free(send_buf);
    free(arr_h);
}

// Single-array merged read (single merged .bin file → GPU array, any jp)
void statistics_readbin_merged(double *arr_d, const char *fname) {
    const int nx = NX6 - 6;
    const int ny = NY;
    const int nz = NZ6 - 6;       // interior z-points (k=3..NZ6-3, includes top wall)
    const int stride = NY / jp;

    // Every rank reads the full merged file (small: ~4 MB per statistic)
    ostringstream oss;
    oss << "./statistics/" << fname << "/" << fname << "_merged.bin";
    ifstream file(oss.str().c_str(), ios::binary);
    if (!file.is_open()) {
        if (myid == 0) printf("[WARNING] statistics_readbin_merged: %s not found, skipping.\n", oss.str().c_str());
        return;
    }
    double *global_buf = (double*)malloc((size_t)nz * ny * nx * sizeof(double));
    file.read(reinterpret_cast<char*>(global_buf), (size_t)nz * ny * nx * sizeof(double));
    file.close();

    // Extract local portion (stride unique + 1 overlap point)
    const size_t nBytes = (size_t)NX6 * NYD6 * NZ6 * sizeof(double);
    double *arr_h = (double*)calloc(NX6 * NYD6 * NZ6, sizeof(double));
    int j_start = myid * stride;  // first global j for this rank

    for (int kk = 0; kk < nz; kk++) {
        int k = kk + 3;  // physical k index
        // Fill j_local = 3..3+stride-1 (stride unique points)
        for (int jl = 0; jl < stride; jl++) {
            int j_local = jl + 3;
            int j_global = j_start + jl;
            for (int ii = 0; ii < nx; ii++) {
                int i = ii + 3;
                arr_h[j_local * NX6 * NZ6 + k * NX6 + i] =
                    global_buf[(size_t)kk * ny * nx + j_global * nx + ii];
            }
        }
        // Fill overlap point: j_local = 3+stride = NYD6-4
        {
            int j_local = 3 + stride;  // = NYD6 - 4
            int j_global = (j_start + stride) % ny;  // wrap for periodic
            for (int ii = 0; ii < nx; ii++) {
                int i = ii + 3;
                arr_h[j_local * NX6 * NZ6 + k * NX6 + i] =
                    global_buf[(size_t)kk * ny * nx + j_global * nx + ii];
            }
        }
    }

    CHECK_CUDA( cudaMemcpy(arr_d, arr_h, nBytes, cudaMemcpyHostToDevice) );
    free(arr_h);
    free(global_buf);
    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );
}

// Master function: write all 33 statistics as merged binary (32 user-spec + UVW)
// All statistics share single accu_count (FTT >= FTT_STATS_START)
void statistics_writebin_merged_stress() {
    if (myid == 0) {
        ofstream fp_accu("./statistics/accu.dat");
        fp_accu << accu_count << " " << step;
        fp_accu.close();
    }
    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );
    // 一階矩 (3)
    statistics_writebin_merged(U, "U");
    statistics_writebin_merged(V, "V");
    statistics_writebin_merged(W, "W");
    // 壓力 (2: P, PP)
    statistics_writebin_merged(P, "P");
    statistics_writebin_merged(PP, "PP");
    // 二階矩 (6)
    statistics_writebin_merged(UU, "UU");
    statistics_writebin_merged(UV, "UV");
    statistics_writebin_merged(UW, "UW");
    statistics_writebin_merged(VV, "VV");
    statistics_writebin_merged(VW, "VW");
    statistics_writebin_merged(WW, "WW");
    // 壓力交叉項 (3)
    statistics_writebin_merged(PU, "PU");
    statistics_writebin_merged(PV, "PV");
    statistics_writebin_merged(PW, "PW");
    // 梯度平方 (9)
    statistics_writebin_merged(DUDX2, "DUDX2");
    statistics_writebin_merged(DUDY2, "DUDY2");
    statistics_writebin_merged(DUDZ2, "DUDZ2");
    statistics_writebin_merged(DVDX2, "DVDX2");
    statistics_writebin_merged(DVDY2, "DVDY2");
    statistics_writebin_merged(DVDZ2, "DVDZ2");
    statistics_writebin_merged(DWDX2, "DWDX2");
    statistics_writebin_merged(DWDY2, "DWDY2");
    statistics_writebin_merged(DWDZ2, "DWDZ2");
    // 三階矩 (10, includes UVW)
    statistics_writebin_merged(UUU, "UUU");
    statistics_writebin_merged(UUV, "UUV");
    statistics_writebin_merged(UUW, "UUW");
    statistics_writebin_merged(UVW, "UVW");
    statistics_writebin_merged(VVU, "VVU");
    statistics_writebin_merged(VVV, "VVV");
    statistics_writebin_merged(VVW, "VVW");
    statistics_writebin_merged(WWU, "WWU");
    statistics_writebin_merged(WWV, "WWV");
    statistics_writebin_merged(WWW, "WWW");
    if (myid == 0) printf("  statistics_writebin_merged_stress: 33 merged .bin files written (accu=%d, step=%d)\n",
                          accu_count, step);
}

// Master function: read all 33 statistics from merged binary (any jp)
void statistics_readbin_merged_stress() {
    // accu.dat format: "accu_count step" (backward compat: old "rey_avg_count [vel step]")
    ifstream fp_accu("./statistics/accu.dat");
    if (!fp_accu.is_open()) {
        if (myid == 0) printf("[WARNING] statistics_readbin_merged_stress: accu.dat not found, accu_count unchanged.\n");
        return;
    }
    int bin_step = -1;
    fp_accu >> accu_count;
    fp_accu >> bin_step;  // may fail silently if old format
    fp_accu.close();
    if (myid == 0)
        printf("  statistics_readbin_merged_stress: accu=%d, step=%d from accu.dat\n", accu_count, bin_step);

    statistics_readbin_merged(U, "U");
    statistics_readbin_merged(V, "V");
    statistics_readbin_merged(W, "W");
    statistics_readbin_merged(P, "P");
    statistics_readbin_merged(PP, "PP");
    statistics_readbin_merged(UU, "UU");
    statistics_readbin_merged(UV, "UV");
    statistics_readbin_merged(UW, "UW");
    statistics_readbin_merged(VV, "VV");
    statistics_readbin_merged(VW, "VW");
    statistics_readbin_merged(WW, "WW");
    statistics_readbin_merged(PU, "PU");
    statistics_readbin_merged(PV, "PV");
    statistics_readbin_merged(PW, "PW");
    statistics_readbin_merged(DUDX2, "DUDX2");
    statistics_readbin_merged(DUDY2, "DUDY2");
    statistics_readbin_merged(DUDZ2, "DUDZ2");
    statistics_readbin_merged(DVDX2, "DVDX2");
    statistics_readbin_merged(DVDY2, "DVDY2");
    statistics_readbin_merged(DVDZ2, "DVDZ2");
    statistics_readbin_merged(DWDX2, "DWDX2");
    statistics_readbin_merged(DWDY2, "DWDY2");
    statistics_readbin_merged(DWDZ2, "DWDZ2");
    statistics_readbin_merged(UUU, "UUU");
    statistics_readbin_merged(UUV, "UUV");
    statistics_readbin_merged(UUW, "UUW");
    statistics_readbin_merged(UVW, "UVW");
    statistics_readbin_merged(VVU, "VVU");
    statistics_readbin_merged(VVV, "VVV");
    statistics_readbin_merged(VVW, "VVW");
    statistics_readbin_merged(WWU, "WWU");
    statistics_readbin_merged(WWV, "WWV");
    statistics_readbin_merged(WWW, "WWW");
    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );
}


/*第四段:每1000步輸出可視化VTK檔案*/
/*
 * ═══════════════════════════════════════════════════════════
 * 座標映射：Code ↔ VTK/ERCOFTAC（統一公約）
 * ═══════════════════════════════════════════════════════════
 *
 *   Code    物理方向          VTK/ERCOFTAC 名稱
 *   ─────────────────────────────────────────────────
 *   x (i)   展向 spanwise    w  (W)
 *   y (j)   流向 streamwise  u  (U)
 *   z (k)   法向 wall-normal v  (V)
 *
 * 關鍵映射（ERCOFTAC 7 欄比較時）：
 *   ERCOFTAC col 2: <U>/Ub    = VTK U_mean  (streamwise = code v)
 *   ERCOFTAC col 3: <V>/Ub    = VTK V_mean  (wall-normal = code w)
 *   ERCOFTAC col 4: <u'u'>/Ub²= VTK uu_RS   (stream×stream)
 *   ERCOFTAC col 5: <v'v'>/Ub²= VTK vv_RS   (wallnorm×wallnorm)
 *   ERCOFTAC col 6: <u'v'>/Ub²= VTK uv_RS   (stream×wallnorm)
 *   ERCOFTAC col 7: k/Ub²     = VTK k_TKE
 *
 * 渦度映射（與速度 SCALAR 一致的 ERCOFTAC frame）：
 *   VTK omega_u (流向渦度, 繞 x_e=code y 軸) = code ω_y (oy)
 *   VTK omega_v (法向渦度, 繞 y_e=code z 軸) = code ω_z (oz)
 *   VTK omega_w (展向渦度, 繞 z_e=code x 軸) = code ω_x (ox)
 * ═══════════════════════════════════════════════════════════
 */
// 合併所有 GPU 結果，輸出單一 VTK 檔案 (Paraview)
void DiagnoseVTKMacroRows(int step) {
    static int diag_count = 0;
    if (diag_count >= 4) return;
    diag_count++;

    const int nface = NX6 * NZ6;
    int local_zero_rows = 0;
    int local_rows = 0;
    double local_max = 0.0;
    double local_min_row_max = 1.0e300;
    double row_max_report[NYD6];

    for (int j = 0; j < NYD6; j++) row_max_report[j] = -1.0;

    for (int j = 3; j < NYD6 - 3; j++) {
        double row_max = 0.0;
        for (int k = 3; k < NZ6 - 3; k++) {
        for (int i = 3; i < NX6 - 3; i++) {
            const int idx = j * nface + k * NX6 + i;
            const double au = fabs(u_h_p[idx]);
            const double av = fabs(v_h_p[idx]);
            const double aw = fabs(w_h_p[idx]);
            if (au > row_max) row_max = au;
            if (av > row_max) row_max = av;
            if (aw > row_max) row_max = aw;
        }}

        row_max_report[j] = row_max;
        local_rows++;
        if (row_max > local_max) local_max = row_max;
        if (row_max < local_min_row_max) local_min_row_max = row_max;
        if (row_max == 0.0) local_zero_rows++;
    }

    int global_zero_rows = 0;
    int global_rows = 0;
    double global_max = 0.0;
    double global_min_row_max = 0.0;
    CHECK_MPI(MPI_Allreduce(&local_zero_rows, &global_zero_rows, 1, MPI_INT, MPI_SUM, MPI_COMM_WORLD));
    CHECK_MPI(MPI_Allreduce(&local_rows, &global_rows, 1, MPI_INT, MPI_SUM, MPI_COMM_WORLD));
    CHECK_MPI(MPI_Allreduce(&local_max, &global_max, 1, MPI_DOUBLE, MPI_MAX, MPI_COMM_WORLD));
    CHECK_MPI(MPI_Allreduce(&local_min_row_max, &global_min_row_max, 1, MPI_DOUBLE, MPI_MIN, MPI_COMM_WORLD));

    if (global_zero_rows > 0) {
        if (myid == 0) {
            fprintf(stderr,
                    "[VTK-MACRO][WARN] step=%d: %d/%d local-y macro rows are exactly zero "
                    "after SendMacroCPU (global max=%.6e, min row max=%.6e). "
                    "Merged VTK would be sparse/corrupt.\n",
                    step, global_zero_rows, global_rows, global_max, global_min_row_max);
        }
        if (local_zero_rows > 0 && myid < 4) {
            fprintf(stderr, "[VTK-MACRO][rank %d] row max |u,v,w|:", myid);
            for (int j = 3; j < NYD6 - 3; j++)
                fprintf(stderr, " j%d=%.3e", j, row_max_report[j]);
            fprintf(stderr, "\n");
        }
    } else if (diag_count == 1 && myid == 0) {
        printf("[VTK-MACRO] row diagnostic OK: %d local-y rows checked, "
               "min row max=%.6e, max=%.6e\n",
               global_rows, global_min_row_max, global_max);
    }
}

void fileIO_velocity_vtk_merged(int step) {
    // 每個 GPU 內部有效區域的 y 層數 (不含 ghost)
    const int nyLocal = NYD6 - 6;  // 去除上下各3層ghost
    const int nxLocal = NX6 - 6;
    const int nzLocal = NZ6 - 6;  // NZ=64 個 k 計算點 (k=3..NZ6-4, loop k<NZ6-3), incl. top wall
    
    // 每個 GPU 發送的點數
    const int localPoints = nxLocal * nyLocal * nzLocal;
    const int zLocalSize = nyLocal * nzLocal;
    
    // 全域 y 層數
    const int nyGlobal = NY6 - 6;
    const int globalPoints = nxLocal * nyGlobal * nzLocal;  // VTK 輸出用
    // MPI_Gather 需要的緩衝區大小 = localPoints * nProcs
    const int gatherPoints = localPoints * nProcs;
    
    // 準備本地速度資料 (去除 ghost cells, 只取內部)
    double *u_local = (double*)malloc(localPoints * sizeof(double));
    double *v_local = (double*)malloc(localPoints * sizeof(double));
    double *w_local = (double*)malloc(localPoints * sizeof(double));
    double *z_local = (double*)malloc(zLocalSize * sizeof(double));
    double *y_local = (double*)malloc(zLocalSize * sizeof(double));  // Frohlich: y varies with (j,k)

    int idx = 0;
    for( int k = 3; k < NZ6-3; k++ ){    // 包含壁面 k=3 和 k=NZ6-3(top wall)
    for( int j = 3; j < NYD6-3; j++ ){
    for( int i = 3; i < NX6-3; i++ ){
        int index = j*NZ6*NX6 + k*NX6 + i;
        u_local[idx] = u_h_p[index];
        v_local[idx] = v_h_p[index];
        w_local[idx] = w_h_p[index];
        idx++;
    }}}

    // 準備本地時間平均資料 (若有累積), 正規化: ÷accu_count÷Uref
    // 只輸出 U_mean (streamwise=code v) 和 V_mean (wall-normal=code w)
    double *vt_local = NULL, *wt_local = NULL;
    if (accu_count > 0) {
        vt_local = (double*)malloc(localPoints * sizeof(double));
        wt_local = (double*)malloc(localPoints * sizeof(double));
        double inv_count_uref = 1.0 / ((double)accu_count * (double)Uref);
        int tidx = 0;
        for( int k = 3; k < NZ6-3; k++ ){
        for( int j = 3; j < NYD6-3; j++ ){
        for( int i = 3; i < NX6-3; i++ ){
            int index = j*NZ6*NX6 + k*NX6 + i;
            vt_local[tidx] = v_tavg_h[index] * inv_count_uref;  // U_mean (streamwise=code v)
            wt_local[tidx] = w_tavg_h[index] * inv_count_uref;  // V_mean (wall-normal=code w)
            tidx++;
        }}}
    }

    // 準備 Reynolds stress + P_mean (若有累積)
    // Level 0: 只輸出 ERCOFTAC 7 欄對應的 3 個 RS + k_TKE + P_mean
    //   VTK uu_RS  ← code (Σvv/N − v̄²) / Uref²   = ERCOFTAC <u'u'>/Ub²  [GPU: VV, V]
    //   VTK uv_RS  ← code (Σvw/N − v̄·w̄) / Uref²  = ERCOFTAC <u'v'>/Ub²  [GPU: VW, V, W]
    //   VTK vv_RS  ← code (Σww/N − w̄²) / Uref²    = ERCOFTAC <v'v'>/Ub²  [GPU: WW, W]
    //   VTK k_TKE  ← 0.5*(uu + vv + ww)            = ERCOFTAC k/Ub²
    //   ww (展向 RS = ERCOFTAC <w'w'>) 只用於 k_TKE 計算，不單獨輸出 (Level 0)
    double *uu_local = NULL, *uv_local = NULL, *vv_local = NULL;
    double *k_local = NULL, *p_mean_local = NULL;
    if (accu_count > 0 && (int)TBSWITCH) {
        uu_local = (double*)malloc(localPoints * sizeof(double));
        uv_local = (double*)malloc(localPoints * sizeof(double));
        vv_local = (double*)malloc(localPoints * sizeof(double));
        k_local  = (double*)malloc(localPoints * sizeof(double));
        p_mean_local = (double*)malloc(localPoints * sizeof(double));

        // Copy 8 MeanVars arrays from GPU → temporary host buffers
        // (UV, UW not needed: ERCOFTAC <u'w'>/<v'w'> 含展向，Level 0 不輸出)
        size_t grid_bytes_rs = (size_t)NX6 * NYD6 * NZ6 * sizeof(double);
        double *U_h_rs = (double*)malloc(grid_bytes_rs);
        double *V_h_rs = (double*)malloc(grid_bytes_rs);
        double *W_h_rs = (double*)malloc(grid_bytes_rs);
        double *P_h_rs = (double*)malloc(grid_bytes_rs);
        double *UU_h_rs = (double*)malloc(grid_bytes_rs);
        double *VV_h_rs = (double*)malloc(grid_bytes_rs);
        double *WW_h_rs = (double*)malloc(grid_bytes_rs);
        double *VW_h_rs = (double*)malloc(grid_bytes_rs);
        CHECK_CUDA(cudaMemcpy(U_h_rs,  U,  grid_bytes_rs, cudaMemcpyDeviceToHost));
        CHECK_CUDA(cudaMemcpy(V_h_rs,  V,  grid_bytes_rs, cudaMemcpyDeviceToHost));
        CHECK_CUDA(cudaMemcpy(W_h_rs,  W,  grid_bytes_rs, cudaMemcpyDeviceToHost));
        CHECK_CUDA(cudaMemcpy(P_h_rs,  P,  grid_bytes_rs, cudaMemcpyDeviceToHost));
        CHECK_CUDA(cudaMemcpy(UU_h_rs, UU, grid_bytes_rs, cudaMemcpyDeviceToHost));
        CHECK_CUDA(cudaMemcpy(VV_h_rs, VV, grid_bytes_rs, cudaMemcpyDeviceToHost));
        CHECK_CUDA(cudaMemcpy(WW_h_rs, WW, grid_bytes_rs, cudaMemcpyDeviceToHost));
        CHECK_CUDA(cudaMemcpy(VW_h_rs, VW, grid_bytes_rs, cudaMemcpyDeviceToHost));

        double inv_N = 1.0 / (double)accu_count;
        double inv_Uref2 = 1.0 / ((double)Uref * (double)Uref);
        int ridx = 0;
        for( int k = 3; k < NZ6-3; k++ ){
        for( int j = 3; j < NYD6-3; j++ ){
        for( int i = 3; i < NX6-3; i++ ){
            int index = j*NZ6*NX6 + k*NX6 + i;
            double u_m = U_h_rs[index]*inv_N;  // <u_code> (spanwise)
            double v_m = V_h_rs[index]*inv_N;  // <v_code> (streamwise)
            double w_m = W_h_rs[index]*inv_N;  // <w_code> (wall-normal)
            uu_local[ridx] = (VV_h_rs[index]*inv_N - v_m*v_m) * inv_Uref2;  // ERCOFTAC <u'u'>/Ub² (流向)
            uv_local[ridx] = (VW_h_rs[index]*inv_N - v_m*w_m) * inv_Uref2;  // ERCOFTAC <u'v'>/Ub² (流×法)
            vv_local[ridx] = (WW_h_rs[index]*inv_N - w_m*w_m) * inv_Uref2;  // ERCOFTAC <v'v'>/Ub² (法向)
            double ww_val  = (UU_h_rs[index]*inv_N - u_m*u_m) * inv_Uref2;  // ERCOFTAC <w'w'>/Ub² (展向, k_TKE 用)
            k_local[ridx]  = 0.5 * (uu_local[ridx] + vv_local[ridx] + ww_val);  // k/Ub²
            p_mean_local[ridx] = P_h_rs[index] * inv_N;  // <P>
            ridx++;
        }}}
        free(U_h_rs); free(V_h_rs); free(W_h_rs); free(P_h_rs);
        free(UU_h_rs); free(VV_h_rs); free(WW_h_rs); free(VW_h_rs);
    }

    // ── Host-side MPI ghost exchange for u/v/w before vorticity computation ──
    // SendDataToCPU copies entire GPU arrays including stale ghost zones.
    // VTK vorticity uses central differences at j=3 → reads u_h_p[j=2] (ghost).
    // Must fill host ghost zones via MPI to eliminate seam artifacts.
    {
        const int slice_size = NX6 * NZ6;
        const int ghost_count = 3 * slice_size;  // 3 j-slices

        // u_h_p ghost exchange
        MPI_Sendrecv(&u_h_p[4 * slice_size],       ghost_count, MPI_DOUBLE, l_nbr, 610,
                     &u_h_p[(NYD6-3) * slice_size], ghost_count, MPI_DOUBLE, r_nbr, 610,
                     MPI_COMM_WORLD, MPI_STATUS_IGNORE);
        MPI_Sendrecv(&u_h_p[(NYD6-7) * slice_size], ghost_count, MPI_DOUBLE, r_nbr, 611,
                     &u_h_p[0],                      ghost_count, MPI_DOUBLE, l_nbr, 611,
                     MPI_COMM_WORLD, MPI_STATUS_IGNORE);

        // v_h_p ghost exchange
        MPI_Sendrecv(&v_h_p[4 * slice_size],       ghost_count, MPI_DOUBLE, l_nbr, 612,
                     &v_h_p[(NYD6-3) * slice_size], ghost_count, MPI_DOUBLE, r_nbr, 612,
                     MPI_COMM_WORLD, MPI_STATUS_IGNORE);
        MPI_Sendrecv(&v_h_p[(NYD6-7) * slice_size], ghost_count, MPI_DOUBLE, r_nbr, 613,
                     &v_h_p[0],                      ghost_count, MPI_DOUBLE, l_nbr, 613,
                     MPI_COMM_WORLD, MPI_STATUS_IGNORE);

        // w_h_p ghost exchange
        MPI_Sendrecv(&w_h_p[4 * slice_size],       ghost_count, MPI_DOUBLE, l_nbr, 614,
                     &w_h_p[(NYD6-3) * slice_size], ghost_count, MPI_DOUBLE, r_nbr, 614,
                     MPI_COMM_WORLD, MPI_STATUS_IGNORE);
        MPI_Sendrecv(&w_h_p[(NYD6-7) * slice_size], ghost_count, MPI_DOUBLE, r_nbr, 615,
                     &w_h_p[0],                      ghost_count, MPI_DOUBLE, l_nbr, 615,
                     MPI_COMM_WORLD, MPI_STATUS_IGNORE);
    }

    // Compute instantaneous vorticity (omega_u, omega_v, omega_w) — ERCOFTAC coordinates
    // Full 2×2 inverse Jacobian:
    //   ∂φ/∂x = (1/dx) ∂φ/∂η
    //   ∂φ/∂y = ξ_y · ∂φ/∂ξ + ζ_y · ∂φ/∂ζ
    //   ∂φ/∂z = ξ_z · ∂φ/∂ξ + ζ_z · ∂φ/∂ζ
    double *ox_local  = (double*)malloc(localPoints * sizeof(double));
    double *oy_local  = (double*)malloc(localPoints * sizeof(double));
    double *oz_local  = (double*)malloc(localPoints * sizeof(double));
    {
        double dx_val = (double)LX / (double)(NX6 - 7);
        double dx_inv = 1.0 / dx_val;
        const int nface = NX6 * NZ6;
        int oidx = 0;
        for (int k = 3; k < NZ6-3; k++) {
        for (int j = 3; j < NYD6-3; j++) {
        for (int i = 3; i < NX6-3; i++) {
            int jk = j * NZ6 + k;
            double xiy  = xi_y_h[jk];
            double xiz  = xi_z_h[jk];
            double ztay = zeta_y_h[jk];
            double ztaz = zeta_z_h[jk];

            // 6th-order central: (-f[-3] + 9*f[-2] - 45*f[-1] + 45*f[+1] - 9*f[+2] + f[+3]) / 60
            int idx = j*nface + k*NX6 + i;
            double du_dj = (-u_h_p[idx - 3*nface] + 9.0*u_h_p[idx - 2*nface] - 45.0*u_h_p[idx - nface]
                           + 45.0*u_h_p[idx + nface] - 9.0*u_h_p[idx + 2*nface] + u_h_p[idx + 3*nface]) / 60.0;
            double du_dk = (-u_h_p[idx - 3*NX6] + 9.0*u_h_p[idx - 2*NX6] - 45.0*u_h_p[idx - NX6]
                           + 45.0*u_h_p[idx + NX6] - 9.0*u_h_p[idx + 2*NX6] + u_h_p[idx + 3*NX6]) / 60.0;

            double dv_di = (-v_h_p[idx - 3] + 9.0*v_h_p[idx - 2] - 45.0*v_h_p[idx - 1]
                           + 45.0*v_h_p[idx + 1] - 9.0*v_h_p[idx + 2] + v_h_p[idx + 3]) / 60.0;
            double dv_dj = (-v_h_p[idx - 3*nface] + 9.0*v_h_p[idx - 2*nface] - 45.0*v_h_p[idx - nface]
                           + 45.0*v_h_p[idx + nface] - 9.0*v_h_p[idx + 2*nface] + v_h_p[idx + 3*nface]) / 60.0;
            double dv_dk = (-v_h_p[idx - 3*NX6] + 9.0*v_h_p[idx - 2*NX6] - 45.0*v_h_p[idx - NX6]
                           + 45.0*v_h_p[idx + NX6] - 9.0*v_h_p[idx + 2*NX6] + v_h_p[idx + 3*NX6]) / 60.0;

            double dw_di = (-w_h_p[idx - 3] + 9.0*w_h_p[idx - 2] - 45.0*w_h_p[idx - 1]
                           + 45.0*w_h_p[idx + 1] - 9.0*w_h_p[idx + 2] + w_h_p[idx + 3]) / 60.0;
            double dw_dj = (-w_h_p[idx - 3*nface] + 9.0*w_h_p[idx - 2*nface] - 45.0*w_h_p[idx - nface]
                           + 45.0*w_h_p[idx + nface] - 9.0*w_h_p[idx + 2*nface] + w_h_p[idx + 3*nface]) / 60.0;
            double dw_dk = (-w_h_p[idx - 3*NX6] + 9.0*w_h_p[idx - 2*NX6] - 45.0*w_h_p[idx - NX6]
                           + 45.0*w_h_p[idx + NX6] - 9.0*w_h_p[idx + 2*NX6] + w_h_p[idx + 3*NX6]) / 60.0;

            // ω_x = ∂w/∂y - ∂v/∂z = (dw_dj·ξ_y + dw_dk·ζ_y) - (dv_dj·ξ_z + dv_dk·ζ_z)
            ox_local[oidx] = (dw_dj * xiy + dw_dk * ztay) - (dv_dj * xiz + dv_dk * ztaz);
            // ω_y = ∂u/∂z - ∂w/∂x = (du_dj·ξ_z + du_dk·ζ_z) - (1/dx)·dw_di
            oy_local[oidx] = (du_dj * xiz + du_dk * ztaz) - dx_inv * dw_di;
            // ω_z = ∂v/∂x - ∂u/∂y = (1/dx)·dv_di - (du_dj·ξ_y + du_dk·ζ_y)
            oz_local[oidx] = dx_inv * dv_di - (du_dj * xiy + du_dk * ztay);

            oidx++;
        }}}
    }

    // 準備本地 y, z 座標 (Frohlich: y_2d varies with j,k)
    int zidx = 0;
    for( int j = 3; j < NYD6-3; j++ ){
    for( int k = 3; k < NZ6-3; k++ ){    // 包含壁面 k=3 和 k=NZ6-4(top wall)
        y_local[zidx]   = y_2d_h[j*NZ6 + k];
        z_local[zidx++] = z_h[j*NZ6 + k];
    }}
    
    // rank 0 分配接收緩衝區
    double *u_global = NULL;
    double *v_global = NULL;
    double *w_global = NULL;
    double *y_global = NULL;
    double *z_global = NULL;

    if( myid == 0 ) {
        u_global = (double*)malloc(gatherPoints * sizeof(double));
        v_global = (double*)malloc(gatherPoints * sizeof(double));
        w_global = (double*)malloc(gatherPoints * sizeof(double));
        y_global = (double*)malloc(zLocalSize * nProcs * sizeof(double));
        z_global = (double*)malloc(zLocalSize * nProcs * sizeof(double));
    }

    double *vt_global = NULL, *wt_global = NULL;
    if( myid == 0 && accu_count > 0 ) {
        vt_global = (double*)malloc(gatherPoints * sizeof(double));
        wt_global = (double*)malloc(gatherPoints * sizeof(double));
    }

    double *uu_global = NULL, *uv_global = NULL, *vv_global = NULL;
    double *k_global = NULL, *p_mean_global = NULL;
    if( myid == 0 && accu_count > 0 && (int)TBSWITCH ) {
        uu_global = (double*)malloc(gatherPoints * sizeof(double));
        uv_global = (double*)malloc(gatherPoints * sizeof(double));
        vv_global = (double*)malloc(gatherPoints * sizeof(double));
        k_global  = (double*)malloc(gatherPoints * sizeof(double));
        p_mean_global = (double*)malloc(gatherPoints * sizeof(double));
    }

    double *ox_global = NULL, *oy_global = NULL, *oz_global = NULL;
    if( myid == 0 ) {
        ox_global  = (double*)malloc(gatherPoints * sizeof(double));
        oy_global  = (double*)malloc(gatherPoints * sizeof(double));
        oz_global  = (double*)malloc(gatherPoints * sizeof(double));
    }

    // [F] WENO activation contour (ζ): 每格點 19 方向中有幾個啟動 WENO (0~19)
    //     防禦性設計：若 host buffer 尚未初始化（restart path 時序問題），填零。
#if USE_WENO7
    extern unsigned char *weno_activation_zeta_h;  // main.cu 中 malloc
    double *wact_zeta_local  = (double*)malloc(localPoints * sizeof(double));
    double *wact_zeta_global = NULL;
    if (myid == 0) wact_zeta_global = (double*)malloc(gatherPoints * sizeof(double));
    {
        int widx = 0;
        if (weno_activation_zeta_h != NULL) {
            // 正常路徑：從 unsigned char [NZ6][NYD6][NX6] 轉為 double
            for (int k = 3; k < NZ6-3; k++) {
            for (int j = 3; j < NYD6-3; j++) {
            for (int i = 3; i < NX6-3; i++) {
                int flat = k * NYD6 * NX6 + j * NX6 + i;
                wact_zeta_local[widx++] = (double)weno_activation_zeta_h[flat];
            }}}
        } else {
            // 防禦路徑：buffer 尚未初始化（如 restart 初始 VTK），全填 0
            for (int n = 0; n < localPoints; n++)
                wact_zeta_local[n] = 0.0;
        }
    }
#endif

    // 所有 rank 一起呼叫 MPI_Gather
    MPI_Gather(u_local, localPoints, MPI_DOUBLE, u_global, localPoints, MPI_DOUBLE, 0, MPI_COMM_WORLD);
    MPI_Gather(v_local, localPoints, MPI_DOUBLE, v_global, localPoints, MPI_DOUBLE, 0, MPI_COMM_WORLD);
    MPI_Gather(w_local, localPoints, MPI_DOUBLE, w_global, localPoints, MPI_DOUBLE, 0, MPI_COMM_WORLD);
    MPI_Gather(y_local, zLocalSize, MPI_DOUBLE, y_global, zLocalSize, MPI_DOUBLE, 0, MPI_COMM_WORLD);
    MPI_Gather(z_local, zLocalSize, MPI_DOUBLE, z_global, zLocalSize, MPI_DOUBLE, 0, MPI_COMM_WORLD);

    if (accu_count > 0) {
        MPI_Gather(vt_local, localPoints, MPI_DOUBLE, vt_global, localPoints, MPI_DOUBLE, 0, MPI_COMM_WORLD);
        MPI_Gather(wt_local, localPoints, MPI_DOUBLE, wt_global, localPoints, MPI_DOUBLE, 0, MPI_COMM_WORLD);
    }
    if (accu_count > 0 && (int)TBSWITCH) {
        MPI_Gather(uu_local,     localPoints, MPI_DOUBLE, uu_global,     localPoints, MPI_DOUBLE, 0, MPI_COMM_WORLD);
        MPI_Gather(uv_local,     localPoints, MPI_DOUBLE, uv_global,     localPoints, MPI_DOUBLE, 0, MPI_COMM_WORLD);
        MPI_Gather(vv_local,     localPoints, MPI_DOUBLE, vv_global,     localPoints, MPI_DOUBLE, 0, MPI_COMM_WORLD);
        MPI_Gather(k_local,      localPoints, MPI_DOUBLE, k_global,      localPoints, MPI_DOUBLE, 0, MPI_COMM_WORLD);
        MPI_Gather(p_mean_local, localPoints, MPI_DOUBLE, p_mean_global, localPoints, MPI_DOUBLE, 0, MPI_COMM_WORLD);
    }
    MPI_Gather(ox_local,  localPoints, MPI_DOUBLE, ox_global,  localPoints, MPI_DOUBLE, 0, MPI_COMM_WORLD);
    MPI_Gather(oy_local,  localPoints, MPI_DOUBLE, oy_global,  localPoints, MPI_DOUBLE, 0, MPI_COMM_WORLD);
    MPI_Gather(oz_local,  localPoints, MPI_DOUBLE, oz_global,  localPoints, MPI_DOUBLE, 0, MPI_COMM_WORLD);
#if USE_WENO7
    MPI_Gather(wact_zeta_local, localPoints, MPI_DOUBLE, wact_zeta_global, localPoints, MPI_DOUBLE, 0, MPI_COMM_WORLD);
#endif

    // rank 0 輸出合併的 VTK
    if( myid == 0 ) {
        // y_global 已由 MPI_Gather 收集 (Frohlich body-fitted, y varies with j AND k)
        
        ostringstream oss;
        oss << "./result/velocity_merged_" << setfill('0') << setw(6) << step << ".vtk";
        const string vtk_path = oss.str();
        const string vtk_tmp_path = vtk_path + ".tmp";
        ofstream out(vtk_tmp_path.c_str(), ios::binary);

        if( !out.is_open() ) {
            cerr << "ERROR: Cannot open VTK temp file: " << vtk_tmp_path << endl;
            free(u_global); free(v_global); free(w_global); free(y_global); free(z_global);
            free(u_local); free(v_local); free(w_local); free(y_local); free(z_local);
            return;
        }

        // VTK header (text lines, even in BINARY mode)
        {
            ostringstream hdr;
            hdr << "# vtk DataFile Version 3.0\n";
            hdr << "LBM Velocity Field (merged) step=" << step << " Force=" << scientific << setprecision(8) << Force_h[0] << " accu_count=" << accu_count << "\n";
            hdr << "BINARY\n";
            hdr << "DATASET STRUCTURED_GRID\n";
            hdr << "DIMENSIONS " << nxLocal << " " << nyGlobal << " " << nzLocal << "\n";
            hdr << "POINTS " << globalPoints << " double\n";
            string h = hdr.str();
            out.write(h.c_str(), h.size());
        }

        // 輸出座標點 (binary, big-endian)
        const int stride = nyLocal - 1;  // = (NY-1)/jp (unique y-points per rank, overlap=1)
        for( int k = 0; k < nzLocal; k++ ){
        for( int jg = 0; jg < nyGlobal; jg++ ){
        for( int i = 0; i < nxLocal; i++ ){
            int gpu_id = jg / stride;
            if( gpu_id >= jp ) gpu_id = jp - 1;
            int j_local = jg - gpu_id * stride;

            int yz_gpu_offset = gpu_id * zLocalSize;
            int yz_local_idx = j_local * nzLocal + k;
            double y_val = y_global[yz_gpu_offset + yz_local_idx];
            double z_val = z_global[yz_gpu_offset + yz_local_idx];

            vtk_write_double_be(out, x_h[i+3]);
            vtk_write_double_be(out, y_val);
            vtk_write_double_be(out, z_val);
        }}}
        
        // 輸出速度向量 (binary, big-endian)
        double inv_Uref_vec = 1.0 / (double)Uref;
        {
            ostringstream vhdr;
            vhdr << "\nPOINT_DATA " << globalPoints << "\n";
            vhdr << "VECTORS velocity double\n";
            string vh = vhdr.str();
            out.write(vh.c_str(), vh.size());
        }

        for( int k = 0; k < nzLocal; k++ ){
        for( int jg = 0; jg < nyGlobal; jg++ ){
        for( int i = 0; i < nxLocal; i++ ){
            int gpu_id = jg / stride;
            if( gpu_id >= jp ) gpu_id = jp - 1;
            int j_local = jg - gpu_id * stride;

            int gpu_offset = gpu_id * localPoints;
            int local_idx = k * nyLocal * nxLocal + j_local * nxLocal + i;
            int global_idx = gpu_offset + local_idx;

            vtk_write_double_be(out, u_global[global_idx] * inv_Uref_vec);
            vtk_write_double_be(out, v_global[global_idx] * inv_Uref_vec);
            vtk_write_double_be(out, w_global[global_idx] * inv_Uref_vec);
        }}}

        // ================================================================
        // VTK Level 0: 13 SCALARS + 1 VECTORS
        // Coordinate mapping: code_u=spanwise(W), code_v=streamwise(U), code_w=wall-normal(V)
        // See coordinate mapping comment at top of this function
        // ================================================================

        // Helper macro: write one SCALAR field (binary, big-endian)
        #define VTK_WRITE_SCALAR(name, arr_global) \
            { string _sh = string("\nSCALARS ") + (name) + " double 1\nLOOKUP_TABLE default\n"; \
              out.write(_sh.c_str(), _sh.size()); } \
            for( int kk = 0; kk < nzLocal; kk++ ){ \
            for( int jg = 0; jg < nyGlobal; jg++ ){ \
            for( int ii = 0; ii < nxLocal; ii++ ){ \
                int gid = jg / stride; \
                if( gid >= jp ) gid = jp - 1; \
                int jl = jg - gid * stride; \
                int gidx = gid * localPoints + kk * nyLocal * nxLocal + jl * nxLocal + ii; \
                vtk_write_double_be(out, (arr_global)[gidx]); \
            }}}

        // [A] 瞬時速度 (3 SCALARS, binary): u_inst=v_code/Uref, v_inst=w_code/Uref, w_inst=u_code/Uref
        {
            double inv_Uref = 1.0 / (double)Uref;

            #define VTK_WRITE_SCALAR_SCALED(sname, arr, scale) \
                { string _sh2 = string("\nSCALARS ") + (sname) + " double 1\nLOOKUP_TABLE default\n"; \
                  out.write(_sh2.c_str(), _sh2.size()); } \
                for( int kk = 0; kk < nzLocal; kk++ ){ \
                for( int jg = 0; jg < nyGlobal; jg++ ){ \
                for( int ii = 0; ii < nxLocal; ii++ ){ \
                    int gid = jg / stride; \
                    if( gid >= jp ) gid = jp - 1; \
                    int jl = jg - gid * stride; \
                    int gidx = gid * localPoints + kk * nyLocal * nxLocal + jl * nxLocal + ii; \
                    vtk_write_double_be(out, (arr)[gidx] * (scale)); \
                }}}

            VTK_WRITE_SCALAR_SCALED("u_inst", v_global, inv_Uref)  // streamwise = code v
            VTK_WRITE_SCALAR_SCALED("v_inst", w_global, inv_Uref)  // wall-normal = code w
            VTK_WRITE_SCALAR_SCALED("w_inst", u_global, inv_Uref)  // spanwise = code u
            #undef VTK_WRITE_SCALAR_SCALED
        }

        // [B] 瞬時渦度 (3 SCALARS): ERCOFTAC frame (omega_u=流向, omega_v=法向, omega_w=展向)
        // 軸映射: x_e=code y(流向), y_e=code z(法向), z_e=code x(展向)
        // ⇒ ω_u(繞 x_e)=ω_y_code, ω_v(繞 y_e)=ω_z_code, ω_w(繞 z_e)=ω_x_code
        VTK_WRITE_SCALAR("omega_u", oy_global);  // 流向渦度 = code ω_y
        VTK_WRITE_SCALAR("omega_v", oz_global);  // 法向渦度 = code ω_z
        VTK_WRITE_SCALAR("omega_w", ox_global);  // 展向渦度 = code ω_x

        // [C] 平均速度 (2 SCALARS): U_mean, V_mean (÷count÷Uref)
        if (accu_count > 0) {
            VTK_WRITE_SCALAR("U_mean", vt_global);  // ERCOFTAC <U>/Ub = streamwise = code v
            VTK_WRITE_SCALAR("V_mean", wt_global);  // ERCOFTAC <V>/Ub = wall-normal = code w
        }

        // [D] Reynolds Stress (3 SCALARS) + k_TKE (1) + P_mean (1) — ERCOFTAC col 4-7
        // ============================================================
        // [TAIL-RECOVERY 2026-04] fileIO.h 原本在此截斷 (line 1704 mid-token).
        // 依 1506-1513 的 malloc gate & 1559-1564 的 MPI_Gather gate 重建。
        // 若原本另有 extra scalars, 請從備份比對後補回。
        // ============================================================
        if (accu_count > 0 && (int)TBSWITCH) {
            VTK_WRITE_SCALAR("uu_RS", uu_global);   // ERCOFTAC col 4: <u'u'>/Ub²
            VTK_WRITE_SCALAR("uv_RS", uv_global);   // ERCOFTAC col 6: <u'v'>/Ub²
            VTK_WRITE_SCALAR("vv_RS", vv_global);   // ERCOFTAC col 5: <v'v'>/Ub²
            VTK_WRITE_SCALAR("k_TKE", k_global);
            VTK_WRITE_SCALAR("P_mean", p_mean_global);
        }

#if USE_WENO7
        // [E] WENO activation contour (ζ): 0~19 scalar per point
        VTK_WRITE_SCALAR("weno_act_zeta", wact_zeta_global);
#endif

        #undef VTK_WRITE_SCALAR

        out.close();
        bool vtk_write_ok = false;
        if (!out) {
            cerr << "ERROR: Failed while writing VTK temp file: " << vtk_tmp_path << endl;
            remove(vtk_tmp_path.c_str());
        } else if (rename(vtk_tmp_path.c_str(), vtk_path.c_str()) != 0) {
            cerr << "ERROR: Cannot promote VTK temp file to final path: "
                 << vtk_tmp_path << " -> " << vtk_path << endl;
            remove(vtk_tmp_path.c_str());
        } else {
            vtk_write_ok = true;
        }

        // ============================================================
        // VTK output confirmation log
        // ============================================================
        if (vtk_write_ok) {
            cout << "Merged VTK output: velocity_merged_"
                 << setfill('0') << setw(6) << step << ".vtk";
            if (accu_count > 0) cout << " (accu=" << accu_count << ")";
            cout << "\n";
        } else {
            cerr << "[VTK][WARN] merged VTK step " << step
                 << " was not committed; final .vtk path was left untouched.\n";
        }

        // ============================================================
        // Rolling retention: keep only newest 10 VTK files
        // ------------------------------------------------------------
        // 重新加回 (commit e14b1b8 "modified the force can't connect"
        // 修外力時連帶誤刪). 若拿掉這段, 每次 NDTVTK 步會持續產生
        // velocity_merged_*.vtk 且不清理, 幾千步後塞爆磁碟。
        // version sort by step number; head -n -10 勾選除最新 10 個外全部
        // ============================================================
        int rc_vtk = system(
            "ls -1v ./result/velocity_merged_*.vtk 2>/dev/null | head -n -10 | xargs -r rm -f"
        );
        (void)rc_vtk;
        cout << "[VTK] Rolling retention: kept newest 10, older purged\n";
    }  // end if (myid == 0)

    // ============================================================
    // 釋放所有 *_local 與 rank0 的 *_global (對應 1297-1529 的 malloc)
    // ============================================================
    free(u_local); free(v_local); free(w_local);
    free(y_local); free(z_local);
    free(ox_local); free(oy_local); free(oz_local);
    if (vt_local) free(vt_local);
    if (wt_local) free(wt_local);
    if (uu_local) free(uu_local);
    if (uv_local) free(uv_local);
    if (vv_local) free(vv_local);
    if (k_local)  free(k_local);
    if (p_mean_local) free(p_mean_local);
#if USE_WENO7
    free(wact_zeta_local);
#endif

    if (myid == 0) {
        free(u_global); free(v_global); free(w_global);
        free(y_global); free(z_global);
        if (ox_global) free(ox_global);
        if (oy_global) free(oy_global);
        if (oz_global) free(oz_global);
        if (vt_global) free(vt_global);
        if (wt_global) free(wt_global);
        if (uu_global) free(uu_global);
        if (uv_global) free(uv_global);
        if (vv_global) free(vv_global);
        if (k_global)  free(k_global);
        if (p_mean_global) free(p_mean_global);
#if USE_WENO7
        if (wact_zeta_global) free(wact_zeta_global);
#endif
    }
}

#endif  // FILEIO_FILE
