#ifndef INITIALIZATION_FILE
#define INITIALIZATION_FILE

#include "initializationTool.h"

void InitialUsingDftFunc() {
    double e[19][3]={{0.0,0.0,0.0},{1.0,0.0,0.0},{-1.0,0.0,0.0},{0.0,1.0,0.0},{0.0,-1.0,0.0},{0.0,0.0,1.0},{0.0,0.0,-1.0},
					{1.0,1.0,0.0},{-1.0,1.0,0.0},{1.0,-1.0,0.0},{-1.0,-1.0,0.0},{1.0,0.0,1.0},{-1.0,0.0,1.0},{1.0,0.0,-1.0},
					{-1.0,0.0,-1.0},{0.0,1.0,1.0},{0.0,-1.0,1.0},{0.0,1.0,-1.0},{0.0,-1.0,-1.0}}; 
    double W[19]={(1.0/3.0),(1.0/18.0),(1.0/18.0),(1.0/18.0),(1.0/18.0),(1.0/18.0),(1.0/18.0),(1.0/36.0),(1.0/36.0)
				  ,(1.0/36.0),(1.0/36.0),(1.0/36.0),(1.0/36.0),(1.0/36.0),(1.0/36.0),(1.0/36.0),(1.0/36.0),(1.0/36.0)
				  ,(1.0/36.0)};

    double udot;

    for( int k = 0; k < NZ6;  k++ ) {
    for( int j = 0; j < NYD6; j++ ) {
    for( int i = 0; i < NX6;  i++ ) {
    
        const int index = j*NX6*NZ6 + k*NX6 + i;

        /* Initial condition for 3-D Taylor-Green vortex */
        //rho_h_p[index] = 1.0 + 3.0*U_0*U_0/16.0*(cos(2.0*2.0*pi*x_h[i]/LX)+cos(2.0*2.0*pi*y_h[j]/LY))*(2.0*cos(2.0*2.0*pi*z_h[k]/LZ));
        //u_h_p[index] = U_0*sin(2.0*pi*x_h[i]/LX)*cos(2.0*pi*y_h[j]/LY)*cos(2.0*pi*z_h[k]/LZ);
        //v_h_p[index] = -U_0*cos(2.0*pi*x_h[i]/LX)*sin(2.0*pi*y_h[j]/LY)*cos(2.0*pi*z_h[k]/LZ);
        //w_h_p[index] = 0.0;

        /* Initial condition for channel flow && periodic hills */
        rho_h_p[index] = 1.0;
        u_h_p[index] = 0.0;
        v_h_p[index] = 0.0;
        w_h_p[index] = 0.0;

        udot = u_h_p[index]*u_h_p[index] + v_h_p[index]*v_h_p[index] + w_h_p[index]*w_h_p[index];

        fh_p[0][index] = W[0]*rho_h_p[index]*(1.0-1.5*udot);
        for( int dir = 1; dir <= 18; dir++ ) {
            fh_p[dir][index] = W[dir] * rho_h_p[index] *( 1.0 + 
                                                          3.0 *( e[dir][0] * u_h_p[index] + e[dir][1] * v_h_p[index] + e[dir][2] * w_h_p[index])+ 
                                                          4.5 *( e[dir][0] * u_h_p[index] + e[dir][1] * v_h_p[index] + e[dir][2] * w_h_p[index] )*( e[dir][0] * u_h_p[index] + e[dir][1] * v_h_p[index] + e[dir][2] * w_h_p[index] )- 
                                                          1.5*udot );
        }
    
    }}}

// 初始力 = Poiseuille 理論值 × 2 (加速啟動，但不過分)
// 使用有效通道高度 h = LZ - H_HILL (扣除山丘)
{
    double h_eff = (double)LZ - (double)H_HILL;
    Force_h[0] = (8.0 * (double)niu * (double)Uref) / (h_eff * h_eff) * 2.0;
}
CHECK_CUDA( cudaMemcpy(Force_d, Force_h, sizeof(double), cudaMemcpyHostToDevice) );

}

void GenerateMesh_X() {
    double dx;
    int bfr = 3;

    if( Uniform_In_Xdir ){
		dx = LX / (double)(NX6-2*bfr-1);
		for( int i = 0; i < NX6; i++ ){
			x_h[i]  = dx*((double)(i-bfr));
		}
	} else {
        printf("Mesh needs to be uniform in periodic hill problem, exit...\n");
        exit(0);
    }

    FILE *meshX;
	meshX = fopen("meshX.DAT","w");
	for( int i = 0 ; i < NX6 ; i++ ){
		fprintf( meshX, "%.15lf\n", x_h[i]);
	}
	fclose(meshX);

    CHECK_CUDA( cudaMemcpy(x_d,  x_h,  NX6*sizeof(double), cudaMemcpyHostToDevice) );

    CHECK_CUDA( cudaDeviceSynchronize() );
}

// ════════════════════════════════════════════════════════════════
//  ReadExternalGrid_YZ: 讀取 Frohlich 外部網格 → y_2d_h, z_h
// ════════════════════════════════════════════════════════════════
// 取代舊的 GenerateMesh_Y() + GenerateMesh_Z()
// 輸入: Tecplot .dat 格式的 (x, y) 座標 (Frohlich 座標系)
// 映射: Frohlich x → code y (streamwise), Frohlich y → code z (wall-normal)
// 輸出: y_2d_h[NYD6*NZ6], z_h[NYD6*NZ6] (含 buffer=3 ghost zone 外插)
void ReadExternalGrid_YZ(double *y_2d_out, double *z_out, int rank) {
    int bfr = 3;

    // 構造檔案路徑 — 從 GRID_DAT_REF 擷取 stem (去掉 .dat 副檔名)
    char grid_ref_stem[256];
    strncpy(grid_ref_stem, GRID_DAT_REF, sizeof(grid_ref_stem) - 1);
    grid_ref_stem[sizeof(grid_ref_stem) - 1] = '\0';
    { char *ext = strrchr(grid_ref_stem, '.'); if (ext) *ext = '\0'; }

    char grid_dat_path[512];
    snprintf(grid_dat_path, sizeof(grid_dat_path),
             "%s/adaptive_%s_I%d_J%d_a%.1f.dat",
             GRID_DAT_DIR, grid_ref_stem,
             NY, NZ, (double)ALPHA);

    // 只 rank 0 讀取，然後 broadcast
    // 命名規則: NY=格點數 → I=NY, NZ=格點數 → J=NZ
    int NI = NY;      // streamwise nodes (= NY 格點)
    int NJ = NZ;      // wall-normal nodes (NZ 本身就是格點數)

    // 全域座標 (Frohlich 座標系): x_fro[NJ][NI], y_fro[NJ][NI]
    double *x_fro = NULL;  // Frohlich x → code y
    double *y_fro = NULL;  // Frohlich y → code z

    if (rank == 0) {
        x_fro = (double*)malloc(NI * NJ * sizeof(double));
        y_fro = (double*)malloc(NI * NJ * sizeof(double));

        FILE *fp = fopen(grid_dat_path, "r");
        if (!fp) {
            fprintf(stderr, "FATAL: Cannot open grid file: %s\n", grid_dat_path);
            MPI_Abort(MPI_COMM_WORLD, 1);
        }

        // ── 格點維度驗證: 讀取 Tecplot header 中的 I, J 並比對 NY, NZ ──
        char line[1024];
        int header_done = 0;
        int I_file = 0, J_file = 0;
        while (fgets(line, sizeof(line), fp)) {
            // 解析 I=, J= (不區分大小寫)
            char *pI = strstr(line, "I=");
            if (!pI) pI = strstr(line, "i=");
            if (pI) sscanf(pI + 2, "%d", &I_file);

            char *pj = strstr(line, "J=");
            if (!pj) pj = strstr(line, "j=");
            if (pj) sscanf(pj + 2, "%d", &J_file);

            // Header 結束標誌: 含 "DT=" 的行
            if (strstr(line, "DT=")) { header_done = 1; break; }
        }
        if (!header_done) {
            fprintf(stderr, "FATAL: Cannot parse Tecplot header in %s\n", grid_dat_path);
            MPI_Abort(MPI_COMM_WORLD, 1);
        }

        // ── 維度比對: 若不吻合則中止 ──
        if (I_file != NI || J_file != NJ) {
            fprintf(stderr, "\n");
            fprintf(stderr, "╔══════════════════════════════════════════════════════════╗\n");
            fprintf(stderr, "║  FATAL: 格點維度不吻合 — 不執行程式碼                    ║\n");
            fprintf(stderr, "╠══════════════════════════════════════════════════════════╣\n");
            fprintf(stderr, "║  Grid file: %s\n", grid_dat_path);
            fprintf(stderr, "║  檔案維度:     I=%d, J=%d\n", I_file, J_file);
            fprintf(stderr, "║  variables.h:  I=%d (=NY=%d), J=%d (=NZ)\n", NI, NY, NJ);
            if (I_file != NI)
                fprintf(stderr, "║  → xi  (streamwise)  格點數不吻合: 檔案 I=%d ≠ NY=%d\n", I_file, NI);
            if (J_file != NJ)
                fprintf(stderr, "║  → zeta (wall-normal) 格點數不吻合: 檔案 J=%d ≠ NZ=%d\n", J_file, NJ);
            fprintf(stderr, "║\n");
            fprintf(stderr, "║  因為輸入之格點與使用者設定不同，不執行程式碼。\n");
            fprintf(stderr, "║  請確認 variables.h 中 NY, NZ 的值與網格檔案一致。\n");
            fprintf(stderr, "╚══════════════════════════════════════════════════════════╝\n");
            fclose(fp);
            MPI_Abort(MPI_COMM_WORLD, 1);
        }
        printf("GRID: Dimension check PASSED: I=%d (=NY), J=%d (=NZ) ✓\n", NI, NJ);

        // 讀取 NI*NJ 個 (x, y) 座標點
        // Tecplot POINT format: J 為外層迴圈 (slow), I 為內層 (fast)
        for (int jj = 0; jj < NJ; jj++) {
            for (int ii = 0; ii < NI; ii++) {
                int idx = jj * NI + ii;
                if (fscanf(fp, "%lf %lf", &x_fro[idx], &y_fro[idx]) != 2) {
                    fprintf(stderr, "FATAL: Unexpected EOF at point (%d,%d) in %s\n", ii, jj, grid_dat_path);
                    MPI_Abort(MPI_COMM_WORLD, 1);
                }
            }
        }
        fclose(fp);

        printf("GRID: Read %s: I=%d (streamwise) x J=%d (wall-normal)\n", grid_dat_path, NI, NJ);
        printf("GRID: x range (raw): [%.6f, %.6f]\n", x_fro[0], x_fro[NI-1]);
        printf("GRID: y range (raw): [%.6f, %.6f]\n", y_fro[0], y_fro[(NJ-1)*NI]);

        // ── 無因次化: Frohlich 網格為物理尺寸 (h_physical ≈ 0.028)
        //    code 使用 H_HILL=1.0 無因次座標，需要 scale = H_HILL / h_physical
        //    h_physical 由 x_fro_max / LY 計算 (hill-to-hill = 9h → x_max = LY × h)
        double x_fro_max = x_fro[NI - 1];  // 流向最大值 = LY × h_physical
        double h_physical = x_fro_max / LY; // 物理 hill 高度
        double grid_scale = H_HILL / h_physical; // 無因次化縮放因子
        printf("GRID: h_physical = %.6e, scale factor = %.6f (H_HILL/h_phys)\n",
               h_physical, grid_scale);

        for (int idx = 0; idx < NI * NJ; idx++) {
            x_fro[idx] *= grid_scale;
            y_fro[idx] *= grid_scale;
        }

        printf("GRID: x range (scaled): [%.6f, %.6f] → code y (streamwise, expect LY=%.1f)\n",
               x_fro[0], x_fro[NI-1], (double)LY);
        printf("GRID: y range (scaled): [%.6f, %.6f] → code z (wall-normal, expect LZ=%.3f)\n",
               y_fro[0], y_fro[(NJ-1)*NI], (double)LZ);
    }

    // Broadcast 全域座標到所有 ranks
    if (rank != 0) {
        x_fro = (double*)malloc(NI * NJ * sizeof(double));
        y_fro = (double*)malloc(NI * NJ * sizeof(double));
    }
    MPI_Bcast(x_fro, NI * NJ, MPI_DOUBLE, 0, MPI_COMM_WORLD);
    MPI_Bcast(y_fro, NI * NJ, MPI_DOUBLE, 0, MPI_COMM_WORLD);

    // 映射到 code 座標系:
    //   Frohlich x_fro(I=0..NI-1, J=0..NJ-1) → code y_global(j=bfr..bfr+NI-1, k=bfr..bfr+NJ-1)
    //   Frohlich y_fro(I=0..NI-1, J=0..NJ-1) → code z_global(j=bfr..bfr+NI-1, k=bfr..bfr+NJ-1)
    // NI = NY (NY 格點映射到 j=bfr..bfr+NY-1 = j=3..NY+2 = j=3..NY6-4)
    //   (因為 NY6 = NY+6, NY6-4 = NY+2 = bfr+NY-1 ✓)
    // NJ = NZ (NZ 格點對應 k=3..3+NZ-1 = k=3..NZ6-4, NZ6-4 = NZ+2)

    // 建立全域二維座標 y_global[NY6*NZ6], z_global[NY6*NZ6]
    double *y_global = (double*)calloc(NY6 * NZ6, sizeof(double));
    double *z_global = (double*)calloc(NY6 * NZ6, sizeof(double));

    // 填充物理域 (j=bfr..bfr+NY, k=bfr..bfr+NZ)
    for (int jj = 0; jj < NI; jj++) {       // Frohlich I → code j
        for (int kk = 0; kk < NJ; kk++) {   // Frohlich J → code k
            int j_code = jj + bfr;
            int k_code = kk + bfr;
            int idx_fro = kk * NI + jj;      // Frohlich [J][I] layout
            int idx_code = j_code * NZ6 + k_code;

            y_global[idx_code] = x_fro[idx_fro];  // Frohlich x → code y
            z_global[idx_code] = y_fro[idx_fro];  // Frohlich y → code z
        }
    }

    // Ghost zone 外插 (k 方向)
    // Frohlich 網格 J=0..NZ-1 → k=bfr..bfr+NZ-1 = 3..NZ+2 = NZ6-4 (共 NZ 個節點)
    // k=NZ6-4 (=NZ+2) 是 Frohlich J=NZ-1 頂壁節點，已有格點資料，不可覆蓋！
    // 外插：k=2 (底壁下方 buffer), k=0,1 (底部 ghost)
    //       k=NZ6-3 (頂壁上方 buffer), k=NZ6-2, NZ6-1 (頂部 ghost)
    for (int j = bfr; j < bfr + NI; j++) {
        // 底部 buffer: k=2 (底壁 k=3 下方一格)
        y_global[j*NZ6+2]       = 2.0*y_global[j*NZ6+3]       - y_global[j*NZ6+4];
        z_global[j*NZ6+2]       = 2.0*z_global[j*NZ6+3]       - z_global[j*NZ6+4];

        // 底部 ghost: k=1, k=0
        y_global[j*NZ6+1]       = 2.0*y_global[j*NZ6+2]       - y_global[j*NZ6+3];
        y_global[j*NZ6+0]       = 2.0*y_global[j*NZ6+1]       - y_global[j*NZ6+2];
        z_global[j*NZ6+1]       = 2.0*z_global[j*NZ6+2]       - z_global[j*NZ6+3];
        z_global[j*NZ6+0]       = 2.0*z_global[j*NZ6+1]       - z_global[j*NZ6+2];

        // 頂部 buffer: k=NZ6-3 (=68, 頂壁 k=NZ6-4 上方一格)
        y_global[j*NZ6+(NZ6-3)] = 2.0*y_global[j*NZ6+(NZ6-4)] - y_global[j*NZ6+(NZ6-5)];
        z_global[j*NZ6+(NZ6-3)] = 2.0*z_global[j*NZ6+(NZ6-4)] - z_global[j*NZ6+(NZ6-5)];

        // 頂部 ghost: k=NZ6-2 (=69), k=NZ6-1 (=70)
        y_global[j*NZ6+(NZ6-2)] = 2.0*y_global[j*NZ6+(NZ6-3)] - y_global[j*NZ6+(NZ6-4)];
        y_global[j*NZ6+(NZ6-1)] = 2.0*y_global[j*NZ6+(NZ6-2)] - y_global[j*NZ6+(NZ6-3)];
        z_global[j*NZ6+(NZ6-2)] = 2.0*z_global[j*NZ6+(NZ6-3)] - z_global[j*NZ6+(NZ6-4)];
        z_global[j*NZ6+(NZ6-1)] = 2.0*z_global[j*NZ6+(NZ6-2)] - z_global[j*NZ6+(NZ6-3)];
    }

    // Ghost zone (j 方向: j=0,1,2 和 j=NY6-3,NY6-2,NY6-1)
    // j 方向為週期性邊界 → ghost zone 必須用周期包裹 (不能線性外插!)
    // 因為 ComputeMetricTerms_Full 在 MPI exchange 之前執行，
    // FD6 stencil 在 rank 邊界物理點 (j=3,4,5) 會讀取 ghost zone 座標。
    // 線性外插導致山丘頂部 (j=3) 度量項誤差 → 發散根因之一。
    //
    // 物理域: j=3..NY+2 (NI=NY nodes, j=3≡j=NY+2 週期)
    // 左 ghost: j=2←j=NY+1, j=1←j=NY, j=0←j=NY-1
    // 右 ghost: j=NY+3←j=4, j=NY+4←j=5, j=NY+5←j=6
    // y 座標連續遞增 → 包裹時需 ±LY 偏移; z 座標週期重複 → 不偏移
    double LY_scaled = (double)LY;  // 無因次化後的流向週期長度
    for (int k = 0; k < NZ6; k++) {
        // 左 ghost: 從右側物理域週期包裹 (y 需 -LY)
        y_global[2*NZ6+k] = y_global[(NY6-5)*NZ6+k] - LY_scaled;
        y_global[1*NZ6+k] = y_global[(NY6-6)*NZ6+k] - LY_scaled;
        y_global[0*NZ6+k] = y_global[(NY6-7)*NZ6+k] - LY_scaled;

        z_global[2*NZ6+k] = z_global[(NY6-5)*NZ6+k];
        z_global[1*NZ6+k] = z_global[(NY6-6)*NZ6+k];
        z_global[0*NZ6+k] = z_global[(NY6-7)*NZ6+k];

        // 右 ghost: 從左側物理域週期包裹 (y 需 +LY)
        y_global[(NY6-3)*NZ6+k] = y_global[4*NZ6+k] + LY_scaled;
        y_global[(NY6-2)*NZ6+k] = y_global[5*NZ6+k] + LY_scaled;
        y_global[(NY6-1)*NZ6+k] = y_global[6*NZ6+k] + LY_scaled;

        z_global[(NY6-3)*NZ6+k] = z_global[4*NZ6+k];
        z_global[(NY6-2)*NZ6+k] = z_global[5*NZ6+k];
        z_global[(NY6-1)*NZ6+k] = z_global[6*NZ6+k];
    }

    // 按 MPI rank 截取 per-rank 座標 (j_local ← j_global)
    for (int j_local = 0; j_local < NYD6; j_local++) {
        int j_global = rank * (NYD6 - 2*bfr - 1) + j_local;
        for (int k = 0; k < NZ6; k++) {
            int idx_local  = j_local * NZ6 + k;
            int idx_global = j_global * NZ6 + k;
            y_2d_out[idx_local] = y_global[idx_global];
            z_out[idx_local]    = z_global[idx_global];
        }
    }

    // 上傳座標到 GPU
    CHECK_CUDA( cudaMemcpy(y_2d_d, y_2d_out, NYD6*NZ6*sizeof(double), cudaMemcpyHostToDevice) );
    CHECK_CUDA( cudaMemcpy(z_d,    z_out,    NYD6*NZ6*sizeof(double), cudaMemcpyHostToDevice) );

    // 輸出診斷檔案 (rank 0 only)
    if (rank == 0) {
        FILE *meshYZ = fopen("meshYZ.DAT", "w");
        fprintf(meshYZ, "# j_global  k  y  z\n");
        for (int j = bfr; j < bfr + NI; j++) {
            for (int k = bfr; k < bfr + NJ; k++) {
                fprintf(meshYZ, "%d %d %.15e %.15e\n",
                        j, k, y_global[j*NZ6+k], z_global[j*NZ6+k]);
            }
        }
        fclose(meshYZ);
        printf("GRID: Wrote meshYZ.DAT (diagnostic)\n");
    }

    free(x_fro);
    free(y_fro);
    free(y_global);
    free(z_global);

    CHECK_CUDA( cudaDeviceSynchronize() );
}


#endif
