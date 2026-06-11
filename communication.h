#ifndef COMMUNICATION_FILE
#define COMMUNICATION_FILE

void CreateDataType() {

    CHECK_MPI( MPI_Type_vector(icount_sw, 1, 1, MPI_DOUBLE, &DataSideways) );
    CHECK_MPI( MPI_Type_commit(&DataSideways) );

}

void ISend_LtRtBdry(
    double *f_new[19], 
    const int istart,       const int nbr,      const int itag[23], 
    const int req,          const int num_arrays, ...)
{
    va_list args;
    va_start( args, num_arrays );

    for( int i = 0; i < num_arrays; i++ ) {
        const int dir = va_arg(args, int);
        CHECK_MPI(
            MPI_Isend((void *)&f_new[dir][istart],     1,   DataSideways,   nbr,   itag[i],
                      MPI_COMM_WORLD,   &(request[i][req]))
        );
    }

    va_end( args );

    CHECK_MPI(
        MPI_Isend((void *)&u[istart],           1,      DataSideways,      nbr,     itag[19],
                  MPI_COMM_WORLD,   &(request[19][req]))
    );
    CHECK_MPI(
        MPI_Isend((void *)&v[istart],           1,      DataSideways,      nbr,     itag[20],
                  MPI_COMM_WORLD,   &(request[20][req]))
    );
    CHECK_MPI(
        MPI_Isend((void *)&w[istart],           1,      DataSideways,      nbr,     itag[21],
                  MPI_COMM_WORLD,   &(request[21][req]))
    );
    CHECK_MPI(
        MPI_Isend((void *)&rho_d[istart],       1,      DataSideways,      nbr,     itag[22],
                  MPI_COMM_WORLD,   &(request[22][req]))
    );

}

void IRecv_LtRtBdry(
    double *f_new[19],
    const int istart,       const int nbr,      const int itag[23],
    const int req,          const int num_arrays, ...)
{
    va_list args;
    va_start( args, num_arrays );

    for( int i = 0; i < num_arrays; i++ ) {
        const int dir = va_arg(args, int);
        CHECK_MPI(
            MPI_Irecv((void *)&f_new[dir][istart],     1,    DataSideways,   nbr,  itag[i],
                      MPI_COMM_WORLD,   &(request[i][req]))
        );
    }

    va_end( args );

    CHECK_MPI(
        MPI_Irecv((void *)&u[istart],           1,      DataSideways,      nbr,     itag[19],
                  MPI_COMM_WORLD,   &(request[19][req]))
    );
    CHECK_MPI(
        MPI_Irecv((void *)&v[istart],           1,      DataSideways,      nbr,     itag[20],
                  MPI_COMM_WORLD,   &(request[20][req]))
    );
    CHECK_MPI(
        MPI_Irecv((void *)&w[istart],           1,      DataSideways,      nbr,     itag[21],
                  MPI_COMM_WORLD,   &(request[21][req]))
    );
    CHECK_MPI(
        MPI_Irecv((void *)&rho_d[istart],       1,      DataSideways,      nbr,     itag[22],
                  MPI_COMM_WORLD,   &(request[22][req]))
    );
}

// ────────────────────────────────────────────────────────────────────────────
// f_post packed MPI exchange: 只交換 f_post[19*GRID], 不交換 u/v/w/rho
// f_post 佈局: [q=0..18][j][k][i], stride = NX6*NYD6*NZ6 per direction
// ────────────────────────────────────────────────────────────────────────────
// [P1 優化] 選擇性方向交換：MPI 沿 ξ (j) 方向切分。
// 曲線座標系中 δξ[q] = dt × (e_y[q]·ξ_y + e_z[q]·ξ_z)，
// 只要 e_y ≠ 0 或 e_z ≠ 0，δξ 就 ≠ 0 → 7-point stencil 跨 ghost zone → 需交換。
//
// 座標對應：i → η (uniform x), j → ξ (curvilinear, MPI分割), k → ζ (curvilinear)
//
// δξ ≠ 0 的 16 個方向（e_y≠0 或 e_z≠0）：
//   q=3(+y), q=4(-y), q=5(+z), q=6(-z),
//   q=7(+x+y), q=8(-x+y), q=9(+x-y), q=10(-x-y),
//   q=11(+x+z), q=12(-x+z), q=13(+x-z), q=14(-x-z),
//   q=15(+y+z), q=16(-y+z), q=17(+y-z), q=18(-y-z)
// 不需交換的 3 個方向（e_y=e_z=0 → δξ=0）：
//   q=0(rest), q=1(+x), q=2(-x)
// ────────────────────────────────────────────────────────────────────────────
static const int MPI_XI_DIRS[]  = {3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18};
static const int N_MPI_XI_DIRS  = 16;

void ISend_FPost_LtRtBdry(
    double *f_post, const size_t grid_size,
    const int istart, const int nbr, const int itag[23],
    const int req)
{
    for (int d = 0; d < N_MPI_XI_DIRS; d++) {
        const int q = MPI_XI_DIRS[d];
        CHECK_MPI(
            MPI_Isend((void *)&f_post[q * GRID_SIZE + istart], 1, DataSideways, nbr, itag[d],
                      MPI_COMM_WORLD, &(request[d][req]))
        );
    }
}

void IRecv_FPost_LtRtBdry(
    double *f_post, const size_t grid_size,
    const int istart, const int nbr, const int itag[23],
    const int req)
{
    for (int d = 0; d < N_MPI_XI_DIRS; d++) {
        const int q = MPI_XI_DIRS[d];
        CHECK_MPI(
            MPI_Irecv((void *)&f_post[q * GRID_SIZE + istart], 1, DataSideways, nbr, itag[d],
                      MPI_COMM_WORLD, &(request[d][req]))
        );
    }
}

// ════════════════════════════════════════════════════════════════════════════
// [P2] Packed MPI exchange: 16 方向打包成 1 連續 buffer → 4 個 MPI 呼叫
// ════════════════════════════════════════════════════════════════════════════
//
// 取代上方 ISend/IRecv_FPost_LtRtBdry (每方向 1 個 65KB MPI 訊息 × 64 呼叫)
// 改為 GPU kernel 打包 16 方向到連續 buffer → 4 個 ~1.04 MB MPI 訊息 + 1 個 Waitall
//
// 記憶體佈局:
//   f_post: [q=0..18][j=0..NYD6-1][k=0..NZ6-1][i=0..NX6-1]
//   halo slab per q: icount_sw = Buffer * NX6 * NZ6 個連續 doubles
//   pack_buf: [dir0 的 icount_sw doubles][dir1 的 icount_sw doubles]...[dir15]
//   dir_idx (0..15) → q = GILBM_MPI_XI_DIRS[dir_idx]
//
// 效能對比:
//   舊: 64 × MPI_Isend + 64 × MPI_Irecv + 16 × MPI_Waitall(4)
//   新:  2 × Pack kernel + 4 × MPI + 1 × Waitall + 2 × Unpack kernel
//   GPU staging: 64 × 65 KB → 4 × 1.04 MB (16× fewer staging ops)
// ════════════════════════════════════════════════════════════════════════════

// ── GPU __constant__ 方向表: dir_idx (0..15) → q (3..18) ──
// D3Q19 中 δξ ≠ 0 的 16 個方向 (e_y≠0 或 e_z≠0)
// 此 __constant__ 必須在 main.cu 的 translation unit 中 communication.h include 處可見
// （communication.h 先於 0.shared_code.h 被 include）
__constant__ int GILBM_MPI_XI_DIRS[16] = {
    3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18
};

// ── GPU Pack kernel: f_post[16 × scattered slabs] → pack_buf[16 × contiguous] ──
// 2D grid: blockIdx.y = dir_idx (0..15), blockIdx.x = local index blocks
// 每個 thread 處理一個 (dir_idx, local_idx) 對
__global__ void PackHaloSelective_Kernel(
    const double * __restrict__ f_post,
    double * __restrict__ pack_buf,
    const int istart,       // halo 在每個 q slice 內的起始 offset
    const int icount_sw,    // 每個 q 的 halo 大小 (doubles)
    const int grid_size)    // GRID_SIZE = NX6*NYD6*NZ6
{
    const int dir_idx = blockIdx.y;    // 0..15
    const int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= icount_sw) return;

    const int q = GILBM_MPI_XI_DIRS[dir_idx];  // __constant__ lookup
    pack_buf[dir_idx * icount_sw + idx] = f_post[(size_t)q * grid_size + istart + idx];
}

// ── GPU Unpack kernel: pack_buf[16 × contiguous] → f_post[16 × scattered slabs] ──
__global__ void UnpackHaloSelective_Kernel(
    double * __restrict__ f_post,
    const double * __restrict__ pack_buf,
    const int istart,
    const int icount_sw,
    const int grid_size)
{
    const int dir_idx = blockIdx.y;
    const int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= icount_sw) return;

    const int q = GILBM_MPI_XI_DIRS[dir_idx];
    f_post[(size_t)q * grid_size + istart + idx] = pack_buf[dir_idx * icount_sw + idx];
}

// ── MPI Persistent Communication 初始化 ──
// 預建 4 個 persistent request (一次性，取代每步 MPI_Isend/Irecv 的 internal setup)
// 每步只需 MPI_Startall + MPI_Waitall，省去 per-message registration overhead
void MPI_Persistent_Init(
    double *send_buf_left,  double *send_buf_right,
    double *recv_buf_left,  double *recv_buf_right,
    MPI_Request req_persist[4])
{
    const int packed_count = N_MPI_XI_DIRS * icount_sw;
    CHECK_MPI( MPI_Send_init(send_buf_left,  packed_count, MPI_DOUBLE,
                             l_nbr, 500, MPI_COMM_WORLD, &req_persist[0]) );
    CHECK_MPI( MPI_Recv_init(recv_buf_right, packed_count, MPI_DOUBLE,
                             r_nbr, 500, MPI_COMM_WORLD, &req_persist[1]) );
    CHECK_MPI( MPI_Send_init(send_buf_right, packed_count, MPI_DOUBLE,
                             r_nbr, 501, MPI_COMM_WORLD, &req_persist[2]) );
    CHECK_MPI( MPI_Recv_init(recv_buf_left,  packed_count, MPI_DOUBLE,
                             l_nbr, 501, MPI_COMM_WORLD, &req_persist[3]) );
}

void MPI_Persistent_Free(MPI_Request req_persist[4]) {
    for (int i = 0; i < 4; i++)
        MPI_Request_free(&req_persist[i]);
}

// ── Packed MPI exchange (主函數): Pack → Persistent Start → Wait → Unpack ──
// 時間線:
//   stream_pack 上: PackHalo×2 → cudaStreamSync → MPI_Startall → MPI_Waitall → UnpackHalo×2
//   stream0 上:     Full kernel 仍在執行（與 MPI 重疊）
//
// 此函數在 cudaStreamSynchronize(stream1) 之後呼叫，
// 確保 Buffer kernel 已完成 halo rows → f_post_write 的 halo 區域可安全打包。
void MPI_Exchange_FPost_Packed(
    double *f_post,
    double *send_buf_left,   double *send_buf_right,
    double *recv_buf_left,   double *recv_buf_right,
    MPI_Request req_persist[4],
    cudaStream_t stream_pack)
{
    const int n_dirs = N_MPI_XI_DIRS;   // 16
    const int pack_threads = 256;
    const int pack_blocks  = (icount_sw + pack_threads - 1) / pack_threads;
    dim3 grid_pack(pack_blocks, n_dirs);       // (33, 16) blocks
    dim3 block_pack(pack_threads);             // 256 threads

    // ── Step 1: GPU Pack (兩個方向的 halo 打包到連續 buffer) ──
    PackHaloSelective_Kernel<<<grid_pack, block_pack, 0, stream_pack>>>(
        f_post, send_buf_left, iToLeft, icount_sw, GRID_SIZE);
    PackHaloSelective_Kernel<<<grid_pack, block_pack, 0, stream_pack>>>(
        f_post, send_buf_right, iToRight, icount_sw, GRID_SIZE);
    CHECK_CUDA( cudaStreamSynchronize(stream_pack) );

    // ── Step 2: MPI Persistent Start + Wait ──
    // 4 個 persistent request 已在初始化時建立，每步只需 Start + Wait
    // buffer 地址不變 → MPI internal 無需重新 registration
    CHECK_MPI( MPI_Startall(4, req_persist) );

    MPI_Status stat_packed[4];
    CHECK_MPI( MPI_Waitall(4, req_persist, stat_packed) );

    // ── Step 3: GPU Unpack (recv buffer → f_post ghost zones) ──
    UnpackHaloSelective_Kernel<<<grid_pack, block_pack, 0, stream_pack>>>(
        f_post, recv_buf_right, iFromRight, icount_sw, GRID_SIZE);
    UnpackHaloSelective_Kernel<<<grid_pack, block_pack, 0, stream_pack>>>(
        f_post, recv_buf_left,  iFromLeft,  icount_sw, GRID_SIZE);
    // 不在此處 sync — 呼叫端 (evolution.h) 負責雙流同步:
    //   cudaStreamSynchronize(stream0)  等 Full kernel 完成
    //   cudaStreamSynchronize(stream1)  等 Unpack 完成
    // 兩者都完成後才執行 periodicSW_fpost (在 stream0 上)
}


// ════════════════════════════════════════════════════════════════════════════
// Macro halo exchange (ρ, u, v, w — 4 arrays) — 所有碰撞模式均需
// ════════════════════════════════════════════════════════════════════════════
// 渦度/統計 kernel 使用中央差分讀取 ghost zone 的 u/v/w → 需要 MPI 交換
// BGK 路徑: f_post MPI 不含 macro → 必須在統計前獨立交換
//
// 佈局: 4 arrays [GRID_SIZE] — rho_d, u, v, w (same layout as f_post slabs)
// 交換區域: 同 f_post 的 iToLeft/iToRight/iFromLeft/iFromRight
// ════════════════════════════════════════════════════════════════════════════

// ── GPU Pack kernel for macro: 4 arrays × icount_sw ──
// 2D grid: blockIdx.y = array index (0=rho, 1=u, 2=v, 3=w)
__global__ void PackMacro_Kernel(
    const double * __restrict__ rho_d,
    const double * __restrict__ u_d,
    const double * __restrict__ v_d,
    const double * __restrict__ w_d,
    double * __restrict__ pack_buf,
    const int istart,
    const int icount_sw)
{
    const int comp = blockIdx.y;    // 0..3
    const int idx  = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= icount_sw) return;

    const int src = istart + idx;
    double val;
    switch (comp) {
        case 0: val = rho_d[src]; break;
        case 1: val = u_d[src];   break;
        case 2: val = v_d[src];   break;
        case 3: val = w_d[src];   break;
    }
    pack_buf[comp * icount_sw + idx] = val;
}

// ── GPU Unpack kernel for macro ──
__global__ void UnpackMacro_Kernel(
    double * __restrict__ rho_d,
    double * __restrict__ u_d,
    double * __restrict__ v_d,
    double * __restrict__ w_d,
    const double * __restrict__ pack_buf,
    const int istart,
    const int icount_sw)
{
    const int comp = blockIdx.y;
    const int idx  = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= icount_sw) return;

    const double val = pack_buf[comp * icount_sw + idx];
    const int dst = istart + idx;
    switch (comp) {
        case 0: rho_d[dst] = val; break;
        case 1: u_d[dst]   = val; break;
        case 2: v_d[dst]   = val; break;
        case 3: w_d[dst]   = val; break;
    }
}

// ── MPI Persistent Init for macro ──
void MPI_Persistent_Init_Macro(
    double *send_buf_left,  double *send_buf_right,
    double *recv_buf_left,  double *recv_buf_right,
    MPI_Request req_persist_macro[4])
{
    const int packed_count = MACRO_COMPONENTS * icount_sw;
    CHECK_MPI( MPI_Send_init(send_buf_left,  packed_count, MPI_DOUBLE,
                             l_nbr, 700, MPI_COMM_WORLD, &req_persist_macro[0]) );
    CHECK_MPI( MPI_Recv_init(recv_buf_right, packed_count, MPI_DOUBLE,
                             r_nbr, 700, MPI_COMM_WORLD, &req_persist_macro[1]) );
    CHECK_MPI( MPI_Send_init(send_buf_right, packed_count, MPI_DOUBLE,
                             r_nbr, 701, MPI_COMM_WORLD, &req_persist_macro[2]) );
    CHECK_MPI( MPI_Recv_init(recv_buf_left,  packed_count, MPI_DOUBLE,
                             l_nbr, 701, MPI_COMM_WORLD, &req_persist_macro[3]) );
}

// ── Packed MPI exchange for macro (ρ, u, v, w) ──
void MPI_Exchange_Macro_Packed(
    double *rho_d, double *u_d, double *v_d, double *w_d,
    double *send_buf_left,   double *send_buf_right,
    double *recv_buf_left,   double *recv_buf_right,
    MPI_Request req_persist_macro[4],
    cudaStream_t stream_pack)
{
    const int pack_threads = 256;
    const int pack_blocks  = (icount_sw + pack_threads - 1) / pack_threads;
    dim3 grid_macro(pack_blocks, MACRO_COMPONENTS);    // (blocks, 4)
    dim3 block_macro(pack_threads);

    // Pack
    PackMacro_Kernel<<<grid_macro, block_macro, 0, stream_pack>>>(
        rho_d, u_d, v_d, w_d, send_buf_left, iToLeft, icount_sw);
    PackMacro_Kernel<<<grid_macro, block_macro, 0, stream_pack>>>(
        rho_d, u_d, v_d, w_d, send_buf_right, iToRight, icount_sw);
    CHECK_CUDA( cudaStreamSynchronize(stream_pack) );

    // MPI Persistent Start + Wait
    CHECK_MPI( MPI_Startall(4, req_persist_macro) );
    MPI_Status stat_macro[4];
    CHECK_MPI( MPI_Waitall(4, req_persist_macro, stat_macro) );

    // Unpack
    UnpackMacro_Kernel<<<grid_macro, block_macro, 0, stream_pack>>>(
        rho_d, u_d, v_d, w_d, recv_buf_right, iFromRight, icount_sw);
    UnpackMacro_Kernel<<<grid_macro, block_macro, 0, stream_pack>>>(
        rho_d, u_d, v_d, w_d, recv_buf_left,  iFromLeft,  icount_sw);
}


void Isend_Sideways(const int istart, const int sw_nbr, const int itag_sw[23], MPI_Request reqSideways[23], const int num_arrays, ...) {
    va_list args;
    va_start( args, num_arrays );

    for( int i = 0; i < num_arrays; i++ ) {
        const int dir = va_arg(args, int);
        CHECK_MPI(
            MPI_Isend((void *)&fh_p[dir][istart],     1,   DataSideways,   sw_nbr,   itag_sw[i],
                      MPI_COMM_WORLD,   &reqSideways[i])
        );
    }

    va_end( args );

    CHECK_MPI(
        MPI_Isend((void *)&u_h_p[istart],       1,      DataSideways,   sw_nbr,     itag_sw[19],
                  MPI_COMM_WORLD,   &reqSideways[19])
    );
    CHECK_MPI(
        MPI_Isend((void *)&v_h_p[istart],       1,      DataSideways,   sw_nbr,     itag_sw[20],
                  MPI_COMM_WORLD,   &reqSideways[20])
    );
    CHECK_MPI(
        MPI_Isend((void *)&w_h_p[istart],       1,      DataSideways,   sw_nbr,     itag_sw[21],
                  MPI_COMM_WORLD,   &reqSideways[21])
    );
    CHECK_MPI(
        MPI_Isend((void *)&rho_h_p[istart],     1,      DataSideways,   sw_nbr,     itag_sw[22],
                  MPI_COMM_WORLD,   &reqSideways[22])
    );

}

void Irecv_Sideways(const int istart, const int sw_nbr, const int itag_sw[23], MPI_Request reqSideways[23], const int num_arrays, ...) {
    va_list args;
    va_start( args, num_arrays );

    for( int i = 0; i < num_arrays; i++ ) {
        const int dir = va_arg(args, int);
        CHECK_MPI(
            MPI_Irecv((void *)&fh_p[dir][istart],     1,    DataSideways,   sw_nbr,  itag_sw[i],
                      MPI_COMM_WORLD,   &reqSideways[i])
        );
    }

    va_end( args );

    CHECK_MPI(
        MPI_Irecv((void *)&u_h_p[istart],       1,      DataSideways,   sw_nbr,     itag_sw[19],
                  MPI_COMM_WORLD,   &reqSideways[19])
    );
    CHECK_MPI(
        MPI_Irecv((void *)&v_h_p[istart],       1,      DataSideways,   sw_nbr,     itag_sw[20],
                  MPI_COMM_WORLD,   &reqSideways[20])
    );
    CHECK_MPI(
        MPI_Irecv((void *)&w_h_p[istart],       1,      DataSideways,   sw_nbr,     itag_sw[21],
                  MPI_COMM_WORLD,   &reqSideways[21])
    );
    CHECK_MPI(
        MPI_Irecv((void *)&rho_h_p[istart],     1,      DataSideways,   sw_nbr,     itag_sw[22],
                  MPI_COMM_WORLD,   &reqSideways[22])
    );

}

void Wait_Sideways(
    double *f_new[19], const int iend_sw, 
    MPI_Request reqSend[23], MPI_Request reqRecv[23], const int transfsize, cudaStream_t stream0,
    const int num_arrays, ...)
{    
    va_list args;
    va_start( args, num_arrays );

    for( int i = 0; i < num_arrays; i++ ) {
        const int dir = va_arg(args, int);

        CHECK_MPI( MPI_Wait(&reqSend[i], istat) );
        CHECK_MPI( MPI_Wait(&reqRecv[i], istat) );
        CHECK_CUDA(
            cudaMemcpyAsync((void *)&f_new[dir][iend_sw],	(void *)&fh_p[dir][iend_sw],    transfsize*sizeof(double),
					        cudaMemcpyHostToDevice,		stream0)
        );
    }

    va_end( args );

    CHECK_MPI( MPI_Wait(&reqSend[19], istat) );
    CHECK_MPI( MPI_Wait(&reqRecv[19], istat) );
    CHECK_CUDA(
        cudaMemcpyAsync((void *)&u[iend_sw],    (void *)&u_h_p[iend_sw],   transfsize*sizeof(double),
                        cudaMemcpyHostToDevice,     stream0)
    );
    CHECK_MPI( MPI_Wait(&reqSend[20], istat) );
    CHECK_MPI( MPI_Wait(&reqRecv[20], istat) );
    CHECK_CUDA(
        cudaMemcpyAsync((void *)&v[iend_sw],    (void *)&v_h_p[iend_sw],   transfsize*sizeof(double),
                        cudaMemcpyHostToDevice,     stream0)
    );
    CHECK_MPI( MPI_Wait(&reqSend[21], istat) );
    CHECK_MPI( MPI_Wait(&reqRecv[21], istat) );
    CHECK_CUDA(
        cudaMemcpyAsync((void *)&w[iend_sw],    (void *)&w_h_p[iend_sw],   transfsize*sizeof(double),
                        cudaMemcpyHostToDevice,     stream0)
    );
    CHECK_MPI( MPI_Wait(&reqSend[22], istat) );
    CHECK_MPI( MPI_Wait(&reqRecv[22], istat) );
    CHECK_CUDA(
        cudaMemcpyAsync((void *)&rho_d[iend_sw],(void *)&rho_h_p[iend_sw], transfsize*sizeof(double),
                        cudaMemcpyHostToDevice,     stream0)
    );

}

void SendBdryToCPU_Sideways(cudaStream_t stream, double *f_new[19], const int istart, const int num_arrays, ...) {
    va_list args;
    va_start( args, num_arrays );

    const size_t nBytes = 3 * NX6 * NZ6 * sizeof(double);

    for( int i = 0; i < num_arrays; i++ ) {
        const int dir = va_arg(args, int);
        CHECK_CUDA( cudaMemcpyAsync((void *)&fh_p[dir][istart],(void *)&f_new[dir][istart], nBytes, cudaMemcpyDeviceToHost, stream) );
    }

    CHECK_CUDA( cudaMemcpyAsync((void *)&u_h_p[istart],   (void *)&u[istart],     nBytes, cudaMemcpyDeviceToHost, stream) );
    CHECK_CUDA( cudaMemcpyAsync((void *)&v_h_p[istart],   (void *)&v[istart],     nBytes, cudaMemcpyDeviceToHost, stream) );
    CHECK_CUDA( cudaMemcpyAsync((void *)&w_h_p[istart],   (void *)&w[istart],     nBytes, cudaMemcpyDeviceToHost, stream) );
    CHECK_CUDA( cudaMemcpyAsync((void *)&rho_h_p[istart], (void *)&rho_d[istart], nBytes, cudaMemcpyDeviceToHost, stream) );

    CHECK_CUDA( cudaStreamSynchronize(stream) );

    va_end( args );
}

void SendSrcToGPU(const size_t nBytes, const int num_arrays, ...) {
    va_list args;
    va_start( args, num_arrays );

    for( int i = 0; i < num_arrays; i++ ) {
        const int dir = va_arg(args, int);
        CHECK_CUDA( cudaMemcpy( (void*)fd[dir], (void*)fh_p[dir], nBytes, cudaMemcpyHostToDevice ) );
        CHECK_CUDA( cudaMemcpy( (void*)ft[dir], (void*)fh_p[dir], nBytes, cudaMemcpyHostToDevice ) );
    }

    CHECK_CUDA( cudaDeviceSynchronize() );
    va_end( args );
}

void SendSrcToCPU(double *f_new[19], const size_t nBytes, const int num_arrays, ...) {
    va_list args;
    va_start( args, num_arrays );

    for( int i = 0; i < num_arrays; i++ ) {
        const int dir = va_arg(args, int);
        CHECK_CUDA( cudaMemcpy(fh_p[dir],f_new[dir],nBytes,cudaMemcpyDeviceToHost); )
    }

    va_end( args );
}

void SendDataToGPU() {
    const size_t nBytes = NX6 * NYD6 * NZ6 * sizeof(double);

    CHECK_CUDA( cudaMemcpy(rho_d, rho_h_p, nBytes, cudaMemcpyHostToDevice) );
    CHECK_CUDA( cudaMemcpy(u,     u_h_p,   nBytes, cudaMemcpyHostToDevice) );
    CHECK_CUDA( cudaMemcpy(v,     v_h_p,   nBytes, cudaMemcpyHostToDevice) );
    CHECK_CUDA( cudaMemcpy(w,     w_h_p,   nBytes, cudaMemcpyHostToDevice) );
    CHECK_CUDA( cudaMemcpy(rho_d2, rho_h_p, nBytes, cudaMemcpyHostToDevice) );
    CHECK_CUDA( cudaMemcpy(u2,     u_h_p,   nBytes, cudaMemcpyHostToDevice) );
    CHECK_CUDA( cudaMemcpy(v2,     v_h_p,   nBytes, cudaMemcpyHostToDevice) );
    CHECK_CUDA( cudaMemcpy(w2,     w_h_p,   nBytes, cudaMemcpyHostToDevice) );

    SendSrcToGPU(nBytes, 19, 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18);

    CHECK_CUDA( cudaDeviceSynchronize() );
    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );

}

// [FIX] SendMacroCPU: only copy u/v/w/rho from GPU to host.
// f distributions are now handled directly by Launch_UnpackFPost_Direct → fh_p[q].
void SendMacroCPU() {
    const size_t nBytes = NX6 * NYD6 * NZ6 * sizeof(double);
    CHECK_CUDA( cudaMemcpy(u_h_p,   u,     nBytes, cudaMemcpyDeviceToHost) );
    CHECK_CUDA( cudaMemcpy(v_h_p,   v,     nBytes, cudaMemcpyDeviceToHost) );
    CHECK_CUDA( cudaMemcpy(w_h_p,   w,     nBytes, cudaMemcpyDeviceToHost) );
    CHECK_CUDA( cudaMemcpy(rho_h_p, rho_d, nBytes, cudaMemcpyDeviceToHost) );
    CHECK_CUDA( cudaDeviceSynchronize() );
    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );
}

// [LEGACY] SendDataToCPU: kept for backward compatibility.
// In the new direct-copy path, call Launch_UnpackFPost_Direct + SendMacroCPU instead.
void SendDataToCPU(double *f_new[19]) {
    const size_t nBytes = NX6 * NYD6 * NZ6 * sizeof(double);

    CHECK_CUDA( cudaMemcpy(u_h_p,   u,     nBytes, cudaMemcpyDeviceToHost) );
	CHECK_CUDA( cudaMemcpy(v_h_p,   v,     nBytes, cudaMemcpyDeviceToHost) );
	CHECK_CUDA( cudaMemcpy(w_h_p,   w,     nBytes, cudaMemcpyDeviceToHost) );
	CHECK_CUDA( cudaMemcpy(rho_h_p, rho_d, nBytes, cudaMemcpyDeviceToHost) );

    SendSrcToCPU(f_new, nBytes, 19, 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18);

    CHECK_CUDA( cudaDeviceSynchronize() );
    CHECK_MPI( MPI_Barrier(MPI_COMM_WORLD) );

}

#endif


































