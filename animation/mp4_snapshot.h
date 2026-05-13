#ifndef MP4_SNAPSHOT_H
#define MP4_SNAPSHOT_H

// ════════════════════════════════════════════════════════════════════════════
// animation/mp4_snapshot.h — 模擬自動渲染動畫模組 (v3: lossless MP4 + 永久 PNG)
// ════════════════════════════════════════════════════════════════════════════
//
// v3 變更 (取代 v2 GIF 版):
//   - 全面改用 lossless MP4 + PNG 序列當主要資產
//   - PNG 永久保留於 animation/png_frames/ (續跑必備資料, 不刪)
//   - 每次 VTK 輸出後重 encode 完整 MP4 (從 PNG 序列重建, 永遠同步)
//   - 固定 33 fps 預設
//   - 找不到舊 PNG 序列就自動冷啟動 (從當前 step 開始累積)
//
// 為什麼這樣設計:
//   * PNG 是 source of truth, MP4 是 derived — 隨時可用 rebuild_mp4.sh 重建
//   * GIF palette 漂移/256 色限制/檔案爆大/續跑品質劣化等問題全部解決
//   * ffmpeg 失敗不會毀 PNG 歷史, 下輪照常產生
//
// 輸出:
//   animation/flow_cont.mp4   — 瞬時 u_streamwise KEY_COLORS 連續色階
//   animation/flow_RD.mp4     — 瞬時 u_streamwise Rainbow Desaturated
//
// 續跑資料 (每 VTK 步產生, 續跑時 glob + sort 還原時間序列):
//   animation/png_frames/frame_NNNNNN_cont.png
//   animation/png_frames/frame_NNNNNN_RD.png
//
// 可調參數 (在 main.cu 的 #include 之前 #define 即可覆蓋預設值):
//   ANIM_ENABLE       總開關: 1=啟用, 0=完全關閉
//   ANIM_EVERY_N_VTK  每 N 次 VTK 輸出渲染一幀 (1 = 每次都渲)
//   ANIM_FPS          MP4 播放幀率 (預設 33)
//   ANIM_WIDTH        PNG/MP4 寬度, 高度依比例自動算 (預設 3840 = 4K)
//   ANIM_CODEC        "libx264" (相容性好) 或 "ffv1" (真無損檔案更小)
//   ANIM_PIX_FMT      "yuv444p" (無 chroma 子採樣, 真無損)
//                     或 "yuv420p" (QuickTime/Safari 相容, 輕度損失)
//   ANIM_LOG          背景 pipeline 輸出 log 路徑
//
// 注意 (跨 arch):
//   ffmpeg 必須有對應 arch 版本. 若當前用的是 amd64 static build, GB200 (aarch64)
//   compute node 上會 Exec format error. 解法: PATH 裡放 aarch64 ffmpeg, 或讓
//   ffmpeg 失敗被吞 (pipeline.py 已做), PNG 仍會保留, 可在 login node 後補 MP4.
// ════════════════════════════════════════════════════════════════════════════

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>

#ifndef ANIM_ENABLE
#define ANIM_ENABLE        1
#endif
#ifndef ANIM_EVERY_N_VTK
#define ANIM_EVERY_N_VTK   1
#endif
#ifndef ANIM_FPS
#define ANIM_FPS           33
#endif
#ifndef ANIM_WIDTH
#define ANIM_WIDTH         3840
#endif
#ifndef ANIM_CODEC
#define ANIM_CODEC         "libx264"
#endif
#ifndef ANIM_PIX_FMT
#define ANIM_PIX_FMT       "yuv444p"
#endif
#ifndef ANIM_LOG
#define ANIM_LOG           "animation/anim_log.txt"
#endif

// ────────────────────────────────────────────────────────────────────────────
// §1  AnimRenderAndRebuild — 單步渲染 + MP4 重 encode (背景執行, 不阻塞)
// ────────────────────────────────────────────────────────────────────────────
//
//   每次 VTK 輸出後呼叫. 每 ANIM_EVERY_N_VTK 次才真正執行.
//   背景 subshell (&), 不阻塞 solver.
//
//   內部由 animation/pipeline.py 包辦:
//     1. pvbatch render_frame.py → 輸出 2 PNG 到 png_frames/ (保留)
//     2. video_encode_mp4.py 重 encode flow_cont.mp4 + flow_RD.mp4
//     3. 保留 PNG (不刪, 續跑需要)
// ────────────────────────────────────────────────────────────────────────────

void AnimRenderAndRebuild(int step) {
#if !ANIM_ENABLE
    return;
#endif
    if (myid != 0) return;

    static int vtk_anim_counter = 0;
    vtk_anim_counter++;
    if (vtk_anim_counter % ANIM_EVERY_N_VTK != 0) return;

    char vtk_path[256];
    sprintf(vtk_path, "./result/velocity_merged_%06d.vtk", step);

    struct stat st;
    if (stat(vtk_path, &st) != 0) {
        fprintf(stderr, "[ANIM] WARNING: VTK not found: %s (skip)\n", vtk_path);
        return;
    }

    if (stat("animation/png_frames", &st) != 0) {
        mkdir("animation/png_frames", 0755);
    }

    printf("[ANIM] Pipeline: step=%d (VTK #%d, every %d)\n",
           step, vtk_anim_counter, ANIM_EVERY_N_VTK);

    char cmd[1024];
    snprintf(cmd, sizeof(cmd),
        "( python3 animation/pipeline.py %s %d "
        "    --width %d --fps %d --codec %s --pix-fmt %s "
        "    >> %s 2>&1 ) &",
        vtk_path, step,
        (int)ANIM_WIDTH, (int)ANIM_FPS, ANIM_CODEC, ANIM_PIX_FMT,
        ANIM_LOG);

    system(cmd);
}

// ────────────────────────────────────────────────────────────────────────────
// §2  AnimFinalize — 模擬結束前等背景收尾 + 最後一幀同步 encode
// ────────────────────────────────────────────────────────────────────────────

void AnimFinalize(int step) {
#if !ANIM_ENABLE
    return;
#endif
    if (myid != 0) return;

    printf("[ANIM] Finalizing: waiting for background pipelines...\n");
    system("wait 2>/dev/null; sleep 2");

    char vtk_path[256];
    sprintf(vtk_path, "./result/velocity_merged_%06d.vtk", step);
    struct stat st;
    if (stat(vtk_path, &st) != 0) {
        fprintf(stderr, "[ANIM] WARNING: final VTK not found: %s\n", vtk_path);
        return;
    }

    printf("[ANIM] Final render step=%d (blocking)\n", step);
    char cmd[1024];
    snprintf(cmd, sizeof(cmd),
        "python3 animation/pipeline.py %s %d "
        "  --width %d --fps %d --codec %s --pix-fmt %s "
        "  >> %s 2>&1",
        vtk_path, step,
        (int)ANIM_WIDTH, (int)ANIM_FPS, ANIM_CODEC, ANIM_PIX_FMT,
        ANIM_LOG);
    system(cmd);

    printf("[ANIM] Animation finalized. Artifacts:\n");
    printf("[ANIM]   animation/flow_cont.mp4\n");
    printf("[ANIM]   animation/flow_RD.mp4\n");
    printf("[ANIM]   animation/png_frames/ (history PNGs, resume asset)\n");
}

#endif // MP4_SNAPSHOT_H
