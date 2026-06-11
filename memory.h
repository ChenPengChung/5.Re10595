#ifndef MEMORY_FILE
#define MEMORY_FILE

void AllocateHostArray(const size_t nBytes, const int num_arrays, ...) {
	va_list args;
	va_start( args, num_arrays );

	for( int i = 0; i < num_arrays; i++ ) {
        double **tmp = va_arg(args, double**);
		CHECK_CUDA( cudaMallocHost( (void**)tmp, nBytes) );
	}

	va_end( args );
}

void AllocateDeviceArray(const size_t nBytes, const int num_arrays, ...) {
	va_list args;
	va_start( args, num_arrays );

	for( int i = 0; i < num_arrays; i++ ) {
        double **tmp = va_arg(args, double**);
		CHECK_CUDA( cudaMalloc( (void**)tmp, nBytes) );
	}

	va_end( args );
}

void FreeHostArray(const int num_arrays, ...) {
    va_list args;
    va_start( args, num_arrays );

    for( int i = 0; i < num_arrays; i++ ) {
        CHECK_CUDA( cudaFreeHost( (void*)(va_arg(args, double*)) ) );
    }

    va_end( args );

}

void FreeDeviceArray(const int num_arrays, ...) {
    va_list args;
    va_start( args, num_arrays );

    for( int  i = 0; i < num_arrays; i++ ) {
        CHECK_CUDA( cudaFree( (void*)(va_arg(args, double*)) ) );
    }

    va_end( args );
}

void AllocateMemory() {
    size_t nBytes;

    nBytes = NX6 * NYD6 * NZ6 * sizeof(double);

    AllocateHostArray( nBytes, 4, &rho_h_p, &u_h_p, &v_h_p, &w_h_p );
    for( int i = 0; i < 19; i++ ) {
        CHECK_CUDA( cudaMallocHost( (void**)&fh_p[i], nBytes ) );
        memset(fh_p[i], 0.0, nBytes);
    }

    AllocateDeviceArray(nBytes, 8,  &rho_d, &u, &v, &w, &rho_d2, &u2, &v2, &w2);
    AllocateHostArray( nBytes, 1,  &rho_cv_weight_h );
    AllocateDeviceArray(nBytes, 1,  &rho_cv_weight_d );
    memset(rho_cv_weight_h, 0, nBytes);
    CHECK_CUDA( cudaMemset(rho_cv_weight_d, 0, nBytes) );
    for( int i = 0; i < 19; i++ ) {
        CHECK_CUDA( cudaMalloc( &fd[i], nBytes ) );     CHECK_CUDA( cudaMemset( fd[i], 0.0, nBytes ) );
        CHECK_CUDA( cudaMalloc( &ft[i], nBytes ) );     CHECK_CUDA( cudaMemset( ft[i], 0.0, nBytes ) );
    }

    if( TBSWITCH ) {
        AllocateDeviceArray(nBytes, 4,  &U,  &V,  &W,  &P);
        CHECK_CUDA(cudaMemset(U, 0, nBytes));  CHECK_CUDA(cudaMemset(V, 0, nBytes));
        CHECK_CUDA(cudaMemset(W, 0, nBytes));  CHECK_CUDA(cudaMemset(P, 0, nBytes));

        AllocateDeviceArray(nBytes, 10, &UU, &UV, &UW, &VV, &VW, &WW, &PU, &PV, &PW, &PP);
        CHECK_CUDA(cudaMemset(UU, 0, nBytes)); CHECK_CUDA(cudaMemset(UV, 0, nBytes));
        CHECK_CUDA(cudaMemset(UW, 0, nBytes)); CHECK_CUDA(cudaMemset(VV, 0, nBytes));
        CHECK_CUDA(cudaMemset(VW, 0, nBytes)); CHECK_CUDA(cudaMemset(WW, 0, nBytes));
        CHECK_CUDA(cudaMemset(PU, 0, nBytes)); CHECK_CUDA(cudaMemset(PV, 0, nBytes));
        CHECK_CUDA(cudaMemset(PW, 0, nBytes)); CHECK_CUDA(cudaMemset(PP, 0, nBytes));

        AllocateDeviceArray(nBytes, 9,  &DUDX2, &DUDY2, &DUDZ2, &DVDX2, &DVDY2, &DVDZ2, &DWDX2, &DWDY2, &DWDZ2);
        CHECK_CUDA(cudaMemset(DUDX2, 0, nBytes)); CHECK_CUDA(cudaMemset(DUDY2, 0, nBytes));
        CHECK_CUDA(cudaMemset(DUDZ2, 0, nBytes)); CHECK_CUDA(cudaMemset(DVDX2, 0, nBytes));
        CHECK_CUDA(cudaMemset(DVDY2, 0, nBytes)); CHECK_CUDA(cudaMemset(DVDZ2, 0, nBytes));
        CHECK_CUDA(cudaMemset(DWDX2, 0, nBytes)); CHECK_CUDA(cudaMemset(DWDY2, 0, nBytes));
        CHECK_CUDA(cudaMemset(DWDZ2, 0, nBytes));

    	AllocateDeviceArray(nBytes, 10, &UUU,   &UUV,   &UUW,   &UVW,   &VVU,   &VVV,   &VVW,   &WWU,   &WWV,   &WWW);
        CHECK_CUDA(cudaMemset(UUU, 0, nBytes)); CHECK_CUDA(cudaMemset(UUV, 0, nBytes));
        CHECK_CUDA(cudaMemset(UUW, 0, nBytes)); CHECK_CUDA(cudaMemset(UVW, 0, nBytes));
        CHECK_CUDA(cudaMemset(VVU, 0, nBytes));
        CHECK_CUDA(cudaMemset(VVV, 0, nBytes)); CHECK_CUDA(cudaMemset(VVW, 0, nBytes));
        CHECK_CUDA(cudaMemset(WWU, 0, nBytes)); CHECK_CUDA(cudaMemset(WWV, 0, nBytes));
        CHECK_CUDA(cudaMemset(WWW, 0, nBytes));
    }

    // --- x 座標 (均勻, 一維) ---
    nBytes = NX6 * sizeof(double);
    AllocateHostArray(  nBytes, 4,  &x_h, &Xdep_h[0], &Xdep_h[1], &Xdep_h[2]);
    AllocateDeviceArray(nBytes, 4,  &x_d, &Xdep_d[0], &Xdep_d[1], &Xdep_d[2]);

    // --- y 座標 (非均勻, 二維 [NYD6*NZ6]) ---
    nBytes = NYD6 * NZ6 * sizeof(double);
    AllocateHostArray(  nBytes, 4,  &y_2d_h, &Ydep_h[0], &Ydep_h[1], &Ydep_h[2]);
    AllocateDeviceArray(nBytes, 4,  &y_2d_d, &Ydep_d[0], &Ydep_d[1], &Ydep_d[2]);

    // --- z 座標 (非均勻, 二維 [NYD6*NZ6]) ---
    AllocateHostArray(  nBytes, 4,  &z_h, &Zdep_h[0], &Zdep_h[1], &Zdep_h[2]);
    AllocateDeviceArray(nBytes, 4,  &z_d, &Zdep_d[0], &Zdep_d[1], &Zdep_d[2]);

    // --- GILBM 正 Jacobian 度量項 ([NYD6*NZ6]) ---
    // y_xi, y_zeta, z_xi: Host only（僅用於計算逆 Jacobian）
    // z_zeta: Host + Device（WENO7 stretch factor R = max(∂z/∂ζ)/min(∂z/∂ζ) 直接使用）
    AllocateHostArray(nBytes, 4, &y_xi_h, &y_zeta_h, &z_xi_h, &z_zeta_h);
    AllocateDeviceArray(nBytes, 1, &z_zeta_d);
    // Jacobian 行列式
    AllocateHostArray(nBytes, 1, &J_2D_h);

    // --- GILBM 逆 Jacobian 度量項 (Host + Device, [NYD6*NZ6]) ---
    // 取代舊的 dk_dz_h/d, dk_dy_h/d
    AllocateHostArray(  nBytes, 4,  &xi_y_h,   &xi_z_h,   &zeta_y_h,   &zeta_z_h);
    AllocateDeviceArray(nBytes, 4,  &xi_y_d,   &xi_z_d,   &zeta_y_d,   &zeta_z_d);

    // [REMOVED] delta_xi_h/d, delta_zeta_h/d allocation
    // 2026-04 重構: δξ, δζ 全部移至 Step1 kernel 即時計算，不再需要 GPU 記憶體。
    // Precomputed stencil base k [NZ6] (int array, wall-clamped, direct k indexing)
    CHECK_CUDA( cudaMallocHost((void**)&bk_precomp_h, NZ6 * sizeof(int)) );
    CHECK_CUDA( cudaMalloc(&bk_precomp_d, NZ6 * sizeof(int)) );

    // GILBM architecture arrays: f_post 雙緩衝 (一點一值)
    // [方案A] feq_d 已移除 — collision 自行計算 feq
    // [方案B] f_post_d + f_post_d2 雙緩衝: FusedKernel 讀 f_post_read、寫 f_post_write
    //   每步結束後 swap 指標。總記憶體 = 2×19×GRID = 原 f_post + fd[19] 相同。
    {
        size_t grid_size = (size_t)NX6 * NYD6 * NZ6;

        // ── GTS: f_post_d, f_post_d2 [19 × GRID_SIZE] (一點一值, 雙緩衝) ──
        {
            size_t f_post_bytes = 19ULL * grid_size * sizeof(double);
            CHECK_CUDA( cudaMalloc(&f_post_d,  f_post_bytes) );
            CHECK_CUDA( cudaMemset(f_post_d,  0, f_post_bytes) );
            CHECK_CUDA( cudaMalloc(&f_post_d2, f_post_bytes) );
            CHECK_CUDA( cudaMemset(f_post_d2, 0, f_post_bytes) );
            if (myid == 0) {
                printf("[Memory] GTS ALG1: f_post×2=%.1f MB/rank (double-buffer)\n",
                    2.0 * f_post_bytes / 1048576.0);
            }
        }
    }

    // ── [P2] MPI packed exchange buffers (16 directions × icount_sw doubles per buffer) ──
    // 4 buffers: send_left, send_right, recv_left, recv_right
    // 每個 = 16 * Buffer * NX6 * NZ6 * sizeof(double)
    //       = 16 * 3 * 39 * 71 * 8 = 1,063,296 bytes ≈ 1.01 MB
    // 合計 4 buffers = ~4.05 MB (vs 原方案零額外記憶體但 128 次 MPI staging)
    {
        const int n_mpi_dirs = 16;  // D3Q19 中 δξ≠0 的方向數 (= N_MPI_XI_DIRS in communication.h)
        size_t packed_bytes = (size_t)n_mpi_dirs * (size_t)icount_sw * sizeof(double);
        CHECK_CUDA( cudaMalloc(&mpi_send_buf_left_d,  packed_bytes) );
        CHECK_CUDA( cudaMalloc(&mpi_send_buf_right_d, packed_bytes) );
        CHECK_CUDA( cudaMalloc(&mpi_recv_buf_left_d,  packed_bytes) );
        CHECK_CUDA( cudaMalloc(&mpi_recv_buf_right_d, packed_bytes) );
        if (myid == 0) {
            printf("[Memory] MPI packed buffers (P2): 4 x %.2f MB = %.2f MB/rank (%d dirs x %d halo)\n",
                packed_bytes / 1048576.0, 4.0 * packed_bytes / 1048576.0,
                n_mpi_dirs, icount_sw);
        }
    }

    // ── [P2-macro] MPI packed exchange buffers for macro (4 components × icount_sw each) ──
    // 用於 AccumulateVorticity 前的 ρ/u/v/w ghost zone 交換
    // 每個 buffer = 4 * icount_sw * sizeof(double) = 4 * 3*39*71 * 8 ≈ 266 KB
    // 合計 4 buffers ≈ 1.04 MB
    {
        size_t macro_packed_bytes = (size_t)MACRO_COMPONENTS * (size_t)icount_sw * sizeof(double);
        CHECK_CUDA( cudaMalloc(&macro_send_buf_left_d,  macro_packed_bytes) );
        CHECK_CUDA( cudaMalloc(&macro_send_buf_right_d, macro_packed_bytes) );
        CHECK_CUDA( cudaMalloc(&macro_recv_buf_left_d,  macro_packed_bytes) );
        CHECK_CUDA( cudaMalloc(&macro_recv_buf_right_d, macro_packed_bytes) );
        if (myid == 0) {
            printf("[Memory] MPI macro buffers (P2-macro): 4 x %.2f MB = %.2f MB/rank (%d comps x %d halo)\n",
                macro_packed_bytes / 1048576.0, 4.0 * macro_packed_bytes / 1048576.0,
                MACRO_COMPONENTS, icount_sw);
        }
    }

    // Time-average accumulation (GPU-side): u_tavg(spanwise), v_tavg(streamwise), w_tavg(wall-normal)
    {
        size_t tavg_bytes = (size_t)NX6 * NYD6 * NZ6 * sizeof(double);
        CHECK_CUDA( cudaMalloc(&u_tavg_d, tavg_bytes) );
        CHECK_CUDA( cudaMalloc(&v_tavg_d, tavg_bytes) );
        CHECK_CUDA( cudaMalloc(&w_tavg_d, tavg_bytes) );
        CHECK_CUDA( cudaMemset(u_tavg_d, 0, tavg_bytes) );
        CHECK_CUDA( cudaMemset(v_tavg_d, 0, tavg_bytes) );
        CHECK_CUDA( cudaMemset(w_tavg_d, 0, tavg_bytes) );
        // Vorticity mean accumulation (same Stage 1 window)
        CHECK_CUDA( cudaMalloc(&ox_tavg_d, tavg_bytes) );
        CHECK_CUDA( cudaMalloc(&oy_tavg_d, tavg_bytes) );
        CHECK_CUDA( cudaMalloc(&oz_tavg_d, tavg_bytes) );
        CHECK_CUDA( cudaMemset(ox_tavg_d, 0, tavg_bytes) );
        CHECK_CUDA( cudaMemset(oy_tavg_d, 0, tavg_bytes) );
        CHECK_CUDA( cudaMemset(oz_tavg_d, 0, tavg_bytes) );
    }

    // GPU reduction partial sums for mass conservation (replaces SendDataToCPU every step)
    {
        int rho_reduce_total = (NX6 - 7) * (NYD6 - 7) * (NZ6 - 6);
        int rho_reduce_blocks = (rho_reduce_total + 255) / 256;
        CHECK_CUDA( cudaMallocHost((void**)&rho_partial_h, rho_reduce_blocks * sizeof(double)) );
        CHECK_CUDA( cudaMalloc(&rho_partial_d, rho_reduce_blocks * sizeof(double)) );
    }

    // xi_h/xi_d 移除 (外部網格取代正規化座標)
    // nBytes = NZ6 * sizeof(double);
    // CHECK_CUDA( cudaMallocHost( (void**)&xi_h, nBytes ) );
    // CHECK_CUDA( cudaMalloc( &xi_d, nBytes ) );

    nBytes = NZ6 * NX6 * sizeof(double);
    CHECK_CUDA( cudaMallocHost( (void**)&Ub_avg_h, nBytes ) );
    CHECK_CUDA( cudaMalloc( &Ub_avg_d, nBytes ) );
    CHECK_CUDA( cudaMemset(Ub_avg_d, 0, nBytes) );
    

    nBytes = sizeof(double);
    CHECK_CUDA( cudaMallocHost( (void**)&Force_h, nBytes ) );
    CHECK_CUDA( cudaMalloc( &Force_d, nBytes ) );
    CHECK_CUDA( cudaMallocHost( (void**)&rho_modify_h, nBytes ) );
    CHECK_CUDA( cudaMalloc( &rho_modify_d, nBytes ) );
    // 確保 rho_modify 初始化為 0（防止第一步 collision 前讀到垃圾值）
    rho_modify_h[0] = 0.0;
    CHECK_CUDA( cudaMemset(rho_modify_d, 0, nBytes) );

    CHECK_CUDA( cudaStreamCreate( &stream0 ) );
    CHECK_CUDA( cudaStreamCreate( &stream1 ) );
    CHECK_CUDA( cudaStreamCreate( &stream2 ) );
    for( int i = 0; i < 2; i++ )
        CHECK_CUDA( cudaStreamCreate( &tbsum_stream[i] ) );

    CHECK_CUDA( cudaEventCreate( &start  ) );
    CHECK_CUDA( cudaEventCreate( &stop   ) );
    CHECK_CUDA( cudaEventCreate( &start1 ) );
    CHECK_CUDA( cudaEventCreate( &stop1  ) );
}

void FreeSource() {

    for( int i = 0; i < 19; i++ )
        CHECK_CUDA( cudaFreeHost( fh_p[i] ) );
        
    FreeHostArray(  4,  rho_h_p, u_h_p, v_h_p, w_h_p);

    for( int i = 0; i < 19; i++ ) {
        CHECK_CUDA( cudaFree( ft[i] ) );
        CHECK_CUDA( cudaFree( fd[i] ) );
    }
    FreeDeviceArray(8,  rho_d, u, v, w, rho_d2, u2, v2, w2);

    if( TBSWITCH ) {
        FreeDeviceArray(4,  U,  V,  W,  P);
        FreeDeviceArray(10, UU, UV, UW, VV, VW, WW, PU, PV, PW, PP);
        FreeDeviceArray(9,  DUDX2, DUDY2, DUDZ2, DVDX2, DVDY2, DVDZ2, DWDX2, DWDY2, DWDZ2);
        FreeDeviceArray(10, UUU, UUV, UUW, UVW, VVU, VVV, VVW, WWU, WWV, WWW);
    }

    // GPU reduction partial sums
    CHECK_CUDA( cudaFreeHost(rho_partial_h) );
    CHECK_CUDA( cudaFree(rho_partial_d) );
    CHECK_CUDA( cudaFreeHost(rho_cv_weight_h) );
    CHECK_CUDA( cudaFree(rho_cv_weight_d) );

    FreeHostArray(  1,  x_h);
    FreeDeviceArray(1,  x_d);
    // y 座標 (二維)
    FreeHostArray(  1,  y_2d_h);
    FreeDeviceArray(1,  y_2d_d);
    // z 座標 (二維)
    FreeHostArray(  1,  z_h);
    FreeDeviceArray(1,  z_d);
    // 正 Jacobian 度量項
    FreeHostArray(  4,  y_xi_h, y_zeta_h, z_xi_h, z_zeta_h);
    FreeDeviceArray(1,  z_zeta_d);
    FreeHostArray(  1,  J_2D_h);
    // 逆 Jacobian 度量項
    FreeHostArray(  4,  xi_y_h,  xi_z_h,  zeta_y_h,  zeta_z_h);
    FreeDeviceArray(4,  xi_y_d,  xi_z_d,  zeta_y_d,  zeta_z_d);
    // [REMOVED] delta_xi/zeta Free — no longer allocated
    // Precomputed stencil base k
    CHECK_CUDA( cudaFreeHost(bk_precomp_h) );
    CHECK_CUDA( cudaFree(bk_precomp_d) );
    // GILBM architecture arrays (GTS, 雙緩衝)
    CHECK_CUDA( cudaFree(f_post_d) );
    CHECK_CUDA( cudaFree(f_post_d2) );
    // [方案A] feq_d 已移除 — 不再配置/釋放
    // [P2] MPI packed exchange buffers
    CHECK_CUDA( cudaFree(mpi_send_buf_left_d) );
    CHECK_CUDA( cudaFree(mpi_send_buf_right_d) );
    CHECK_CUDA( cudaFree(mpi_recv_buf_left_d) );
    CHECK_CUDA( cudaFree(mpi_recv_buf_right_d) );
    // Time-average accumulation (GPU) + vorticity mean
    FreeDeviceArray(3,  u_tavg_d, v_tavg_d, w_tavg_d);
    FreeDeviceArray(3,  ox_tavg_d, oy_tavg_d, oz_tavg_d);

    for( int i = 0; i < 3; i++ ){
        FreeHostArray(  3,  Xdep_h[i], Ydep_h[i], Zdep_h[i]);
        FreeDeviceArray(3,  Xdep_d[i], Ydep_d[i], Zdep_d[i]);
    }

    CHECK_CUDA( cudaFreeHost( Ub_avg_h ) );
    CHECK_CUDA( cudaFree( Ub_avg_d ) );

    CHECK_CUDA( cudaFreeHost( Force_h ) );
    CHECK_CUDA( cudaFree( Force_d ) );
    CHECK_CUDA( cudaFreeHost( rho_modify_h ) );
    CHECK_CUDA( cudaFree( rho_modify_d ) );
    CHECK_CUDA( cudaStreamDestroy( stream0 ) );
    CHECK_CUDA( cudaStreamDestroy( stream1 ) );
    CHECK_CUDA( cudaStreamDestroy( stream2 ) );
    for( int i = 0; i < 2; i++ )
        CHECK_CUDA( cudaStreamDestroy( tbsum_stream[i] ) );

    CHECK_CUDA( cudaEventDestroy( start  ) );
    CHECK_CUDA( cudaEventDestroy( stop   ) );
    CHECK_CUDA( cudaEventDestroy( start1 ) );
    CHECK_CUDA( cudaEventDestroy( stop1  ) );
}

#endif
