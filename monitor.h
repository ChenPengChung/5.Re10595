#ifndef MONITOR_FILE
#define MONITOR_FILE

// ================================================================
// RS convergence check point
// ================================================================
// ERCOFTAC x/h=2.0 (streamwise, hill-rear shear layer, RS 峰值區)
// ERCOFTAC y/h=1.0 (wall-normal, above hill bottom)
// Code mapping: streamwise=y(j), wall-normal=z(k), spanwise=x(i)
int rs_check_rank = -1;
int rs_check_i = -1, rs_check_j = -1, rs_check_k = -1;

// 暴露給 main.cu 的 monitor 輸出值 (供收斂系統 CV 計算)
double g_uu_RS_check = 0.0;
double g_k_check_val = 0.0;

// 初始化代表點 index (在 ReadExternalGrid_YZ 之後呼叫)
// 升級: 使用 2D y_2d_h/z_h 座標搜尋最近格點 (非均勻 y-z 網格)
void InitMonitorCheckPoint() {
    // Target: ERCOFTAC x/h=2.0 → code y=2.0, y/h=1.0 → code z = hill(2.0) + 1.0
    double y_target = 2.0 * (double)H_HILL;
    double z_above  = 1.0 * (double)H_HILL;

    // 2D search: 每個 rank 在自己的 local domain 搜尋最近 (j,k) 格點
    // y_2d_h[j*NZ6+k] 在非正交網格中隨 k 變化, 必須做完整 2D 搜尋
    // Step 1: 先用 y 座標在底壁 (k=3) 找最近的 j_local (初估)
    //         在 Frohlich 網格中 y 隨 k 變化較小, 底壁行足夠初估 rank
    double min_dist_y = 1e30;
    int best_j_local = 3;
    for (int j = 3; j < NYD6 - 3; j++) {
        double y_val = y_2d_h[j * NZ6 + 3];  // 底壁行的 y 座標
        double dist = fabs(y_val - y_target);
        if (dist < min_dist_y) {
            min_dist_y = dist;
            best_j_local = j;
        }
    }

    // Step 2: 用 MPI_Allreduce 找出哪個 rank 的 y 最接近 target
    struct { double dist; int rank; } local_best, global_best;
    local_best.dist = min_dist_y;
    local_best.rank = myid;
    MPI_Allreduce(&local_best, &global_best, 1, MPI_DOUBLE_INT, MPI_MINLOC, MPI_COMM_WORLD);
    rs_check_rank = global_best.rank;

    if (myid == rs_check_rank) {
        // Step 3: 在最近 j 附近做完整 2D 搜尋 (j-3..j+3 範圍)
        //         同時找最近的 (j, k) 使得 y≈y_target 且 z≈hill(y)+z_above
        int j_lo = best_j_local - 3;
        int j_hi = best_j_local + 3;
        if (j_lo < 3)        j_lo = 3;
        if (j_hi > NYD6 - 4) j_hi = NYD6 - 4;

        // 先確定 z_target: 使用 best_j_local 處的壁面高度
        double z_wall = z_h[best_j_local * NZ6 + 3];
        double z_target = z_wall + z_above;

        double min_dist_2d = 1e30;
        rs_check_j = best_j_local;
        rs_check_k = 3;
        rs_check_i = NX6 / 2;  // mid-span

        for (int j = j_lo; j <= j_hi; j++) {
            for (int k = 3; k < NZ6 - 3; k++) {
                int idx = j * NZ6 + k;
                double dy2 = y_2d_h[idx] - y_target;
                double dz2 = z_h[idx] - z_target;
                double dist2 = dy2 * dy2 + dz2 * dz2;
                if (dist2 < min_dist_2d) {
                    min_dist_2d = dist2;
                    rs_check_j = j;
                    rs_check_k = k;
                }
            }
        }
    }

    // Broadcast check point info to all ranks (for MPI_Bcast root)
    MPI_Bcast(&rs_check_rank, 1, MPI_INT, 0, MPI_COMM_WORLD);

    // Print check point info
    if (myid == rs_check_rank) {
        printf("[Monitor] RS check point: rank=%d  j=%d  k=%d  i=%d\n",
               myid, rs_check_j, rs_check_k, rs_check_i);
        printf("          y=%.4f (target=%.1f)  z=%.4f (target=hill+%.1f)  hill=%.4f\n",
               y_2d_h[rs_check_j * NZ6 + rs_check_k], y_target,
               z_h[rs_check_j * NZ6 + rs_check_k], z_above,
               z_h[rs_check_j * NZ6 + 3]);
    }

    // Write header comment to monitor file (tab-separated, easy to read)
    if (myid == 0) {
        FILE *f = fopen("Ustar_Force_record.dat", "a");
        fprintf(f, "#%13s  %14s  %14s  %10s  %8s  %13s  %13s  %10s  %13s  %13s  %13s  %5s\n",
                "FTT", "Ub/Uref", "Force*", "Ma_max", "accu_cnt",
                "uu_RS_chk", "k_chk", "GPU_min",
                "Error", "CV_uu%", "CV_k%", "Conv");
        fflush(f);
        fclose(f);
    }
    MPI_Barrier(MPI_COMM_WORLD);
}

// ── Pinned host buffers for monitor D2H transfers ──
// 配置一次，終身重用。cudaMallocHost 產生 page-locked memory：
//   (1) 消除 cudaMemcpy 的 on-the-fly page pinning 開銷
//   (2) 消除 malloc/free 造成的 heap 碎片化 (MLUPS 衰退根因)
//   (3) 啟用 DMA 直傳，降低 TLB miss sensitivity (V100 受益尤大)
static double *g_mon_u_h = NULL;
static double *g_mon_v_h = NULL;
static double *g_mon_w_h = NULL;
static double *g_mon_vslice_h = NULL;  // rank 0 only, for Ub_inst

void InitMonitorBuffers() {
    const size_t gs = (size_t)NX6 * NYD6 * NZ6;
    CHECK_CUDA( cudaMallocHost(&g_mon_u_h, gs * sizeof(double)) );
    CHECK_CUDA( cudaMallocHost(&g_mon_v_h, gs * sizeof(double)) );
    CHECK_CUDA( cudaMallocHost(&g_mon_w_h, gs * sizeof(double)) );
    CHECK_CUDA( cudaMallocHost(&g_mon_vslice_h, (size_t)NX6 * NZ6 * sizeof(double)) );
}

void FreeMonitorBuffers() {
    if (g_mon_u_h)      { cudaFreeHost(g_mon_u_h);      g_mon_u_h = NULL; }
    if (g_mon_v_h)      { cudaFreeHost(g_mon_v_h);      g_mon_v_h = NULL; }
    if (g_mon_w_h)      { cudaFreeHost(g_mon_w_h);      g_mon_w_h = NULL; }
    if (g_mon_vslice_h) { cudaFreeHost(g_mon_vslice_h); g_mon_vslice_h = NULL; }
}

// 計算全場最大 Ma 數 (所有 rank 參與, MPI_Allreduce MAX)
// 使用 pinned buffer，零 malloc/free 開銷
double ComputeMaMax(){
    double local_max_sq = 0.0;
    const size_t gs = (size_t)NX6 * NYD6 * NZ6;
    CHECK_CUDA( cudaMemcpy(g_mon_u_h, u, gs * sizeof(double), cudaMemcpyDeviceToHost) );
    CHECK_CUDA( cudaMemcpy(g_mon_v_h, v, gs * sizeof(double), cudaMemcpyDeviceToHost) );
    CHECK_CUDA( cudaMemcpy(g_mon_w_h, w, gs * sizeof(double), cudaMemcpyDeviceToHost) );

    for (int j = 3; j < NYD6-3; j++)
    for (int k = 3; k < NZ6-3; k++)
    for (int i = 3; i < NX6-3; i++) {
        int idx = j*NX6*NZ6 + k*NX6 + i;
        double sq = g_mon_u_h[idx]*g_mon_u_h[idx] + g_mon_v_h[idx]*g_mon_v_h[idx] + g_mon_w_h[idx]*g_mon_w_h[idx];
        if (sq > local_max_sq) local_max_sq = sq;
    }

    double local_max_mag = sqrt(local_max_sq);
    double global_max_mag;
    MPI_Allreduce(&local_max_mag, &global_max_mag, 1, MPI_DOUBLE, MPI_MAX, MPI_COMM_WORLD);
    return global_max_mag / (double)cs;
}

void Launch_Monitor(){
    // --- 1. 計算瞬時 Ub (rank 0, j=3 hill-crest section) ---
    double Ub_inst = 0.0;
    if (myid == 0) {
        CHECK_CUDA( cudaMemcpy(g_mon_vslice_h, &v[3*NX6*NZ6], NX6*NZ6*sizeof(double), cudaMemcpyDeviceToHost) );
        //輸出入口端瞬時，空間平均速度場
        // Bilinear cell-average: Σ v_cell × dx_cell × dz_cell / A_total
        for (int k = 3; k < NZ6-4; k++) {   // cell centers: k=3..NZ6-5, top wall at k=NZ6-4
        for (int i = 3; i < NX6-4; i++) {
            double v_cell = (g_mon_vslice_h[k*NX6+i] + g_mon_vslice_h[(k+1)*NX6+i]
                           + g_mon_vslice_h[k*NX6+i+1] + g_mon_vslice_h[(k+1)*NX6+i+1]) / 4.0;
            Ub_inst += v_cell * (x_h[i+1] - x_h[i]) * (z_h[3*NZ6+k+1] - z_h[3*NZ6+k]);
        }}
        Ub_inst /= A_cross_j3;   // 使用實際格點面積 (取代 LX*(LZ-1))
    }

    // --- 2. 計算全場 Ma_max (all ranks) ---
    double Ma_max = ComputeMaMax();

    // --- 3. RS 收斂檢查 (代表點單點值) ---
    double uu_RS_check = 0.0;
    double k_check_val = 0.0;

    if (accu_count > 0 && rs_check_rank >= 0 && (int)TBSWITCH) {
        double check_vals[2] = {0.0, 0.0};

        if (myid == rs_check_rank) {
            int idx = rs_check_j * NX6 * NZ6 + rs_check_k * NX6 + rs_check_i;
            double v_sum, vv_sum, u_sum, uu_sum, w_sum, ww_sum;

            // 從 GPU 複製 6 個單點累積值
            CHECK_CUDA( cudaMemcpy(&v_sum,  &V[idx],  sizeof(double), cudaMemcpyDeviceToHost) );
            CHECK_CUDA( cudaMemcpy(&vv_sum, &VV[idx], sizeof(double), cudaMemcpyDeviceToHost) );
            CHECK_CUDA( cudaMemcpy(&u_sum,  &U[idx],  sizeof(double), cudaMemcpyDeviceToHost) );
            CHECK_CUDA( cudaMemcpy(&uu_sum, &UU[idx], sizeof(double), cudaMemcpyDeviceToHost) );
            CHECK_CUDA( cudaMemcpy(&w_sum,  &W[idx],  sizeof(double), cudaMemcpyDeviceToHost) );
            CHECK_CUDA( cudaMemcpy(&ww_sum, &WW[idx], sizeof(double), cudaMemcpyDeviceToHost) );

            double N = (double)accu_count;
            double inv_Uref2 = 1.0 / ((double)Uref * (double)Uref);

            // ERCOFTAC <u'u'> (流向): code v 的 RS = (<v²>-<v>²)/Uref²
            double v_mean = v_sum / N;
            double uu_phys = (vv_sum / N - v_mean * v_mean) * inv_Uref2;

            // ERCOFTAC <v'v'> (法向): code w 的 RS = (<w²>-<w>²)/Uref²
            double w_mean = w_sum / N;
            double vv_phys = (ww_sum / N - w_mean * w_mean) * inv_Uref2;

            // ERCOFTAC <w'w'> (展向): code u 的 RS = (<u²>-<u>²)/Uref²
            double u_mean = u_sum / N;
            double ww_phys = (uu_sum / N - u_mean * u_mean) * inv_Uref2;

            check_vals[0] = uu_phys;                                     // uu_RS_check
            check_vals[1] = 0.5 * (uu_phys + vv_phys + ww_phys);        // k_check
        }

        MPI_Bcast(check_vals, 2, MPI_DOUBLE, rs_check_rank, MPI_COMM_WORLD);
        uu_RS_check = check_vals[0];
        k_check_val = check_vals[1];
    }

    // --- 4. 暴露 uu/k 給收斂系統 (供 main.cu CV 環形緩衝區) ---
    g_uu_RS_check = uu_RS_check;
    g_k_check_val = k_check_val;

    // --- 5. Ub 迭代殘差更新 (每 NDTMIT 步, 層流+紊流皆計算) ---
    // 層流: g_eps_current 是收斂依據
    // 紊流: g_eps_current 僅供 Error 欄參考 (收斂依據是 CV_uu/CV_k)
    double Ustar_now = Ub_inst / (double)Uref;
    MPI_Bcast(&Ustar_now, 1, MPI_DOUBLE, 0, MPI_COMM_WORLD);
    g_eps_current = ComputeFieldResidual(Ustar_now);

    // --- 6. 輸出 Ustar_Force_record.dat (10 欄) ---
    double FTT = step * dt_global / (double)flow_through_time;
    double F_star = Force_h[0] * (double)LY / ((double)Uref * (double)Uref);

    // 格式 (12 欄):
    //   FTT  Ub/Uref  Force*  Ma_max  accu_cnt  uu_RS_chk  k_chk  GPU_min  Error  CV_uu%  CV_k%  Conv
    //
    // Error   = 層流迭代殘差 δ (紊流模式下仍計算但僅供參考，非收斂依據)
    // CV_uu%  = <u'u'> 的 CV% (紊流收斂依據之一; 層流時輸出 -1)
    // CV_k%   = k 的 CV% (紊流收斂依據之二; 層流時輸出 -1)
    // Conv    = 0=NOT_CONVERGED, 1=NEAR, 2=CONVERGED
    extern double g_gpu_time_min;
    extern double g_cv_uu, g_cv_k;
    extern int    g_conv_status;

    // Error 欄: 始終是 Ub 迭代殘差 (層流/紊流皆計算)
    double error_val = g_eps_current;

    // CV 欄: 三道條件全部滿足才輸出有效值
    //   (1) 非層流模式
    //   (2) FTT ≥ FTT_STATS_START + CV_WINDOW_FTT (視窗填滿)
    //   (3) cv_buf_count ≥ 10 (環形緩衝有足夠數據, 防止 restart 後空緩衝)
    // 任何條件不滿足 → -1.0 (不適用/尚未計算)
    extern int cv_buf_count;
    bool cv_valid = !IS_LAMINAR
                 && (FTT >= (FTT_STATS_START + CV_WINDOW_FTT))
                 && (cv_buf_count >= 10);
    double cv_uu_out = cv_valid ? g_cv_uu : -1.0;
    double cv_k_out  = cv_valid ? g_cv_k  : -1.0;

    if (myid == 0) {
        FILE *fhist = fopen("Ustar_Force_record.dat", "a");
        fprintf(fhist, "%14.6f  %14.10f  %14.10f  %10.6f  %8d  %13.6e  %13.6e  %10.4f  %13.6e  %13.6e  %13.6e  %5d\n",
                FTT, Ustar_now, F_star, Ma_max,
                accu_count, uu_RS_check, k_check_val, g_gpu_time_min,
                error_val, cv_uu_out, cv_k_out, g_conv_status);
        fflush(fhist);
        fclose(fhist);
    }

    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );
}

#endif