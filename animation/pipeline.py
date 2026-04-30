#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline.py — 單步影片生成 orchestrator (v3: lossless MP4)
============================================================

取代舊 GIF 版 pipeline:
  * PNG 永久保留 (animation/png_frames/), 不再刪除
  * 每次都用完整 PNG 序列重 encode MP4 (lossless)
  * 續跑時自動從 png_frames/ 繼承歷史幀
  * 找不到 png_frames/ → 只用當前 step 當首幀冷啟動

流程:
  1. pvbatch render_frame.py <vtk> --video-mode
     → 產生 frame_NNNNNN_cont.png + frame_NNNNNN_RD.png (到 png_frames/)
  2. video_encode_mp4.py × 2
     → flow_cont.mp4 (從所有 frame_*_cont.png 重 encode)
     → flow_RD.mp4   (從所有 frame_*_RD.png   重 encode)
  3. (不做) 保留 PNG — 續跑必備

Usage:
  python3 pipeline.py <vtk_file> <step>
                      [--width 3840] [--fps 33]
                      [--codec libx264] [--pix-fmt yuv444p]
                      [--pvbatch /path/to/pvbatch]
"""
import os
import sys
import argparse
import shutil
import subprocess


# ─────────────────────────────────────────────────────────────────────────
# 專案根定位: 本 script 在 <project>/animation/pipeline.py
# ─────────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
ANIM_DIR = SCRIPT_DIR
FRAMES_DIR = os.path.join(ANIM_DIR, "png_frames")


def find_pvbatch():
    """找 pvbatch; 依序 env / PATH / 常見安裝位置."""
    env = os.environ.get("PVBATCH")
    if env and os.path.isfile(env):
        return env
    w = shutil.which("pvbatch")
    if w:
        return w
    for cand in [
        "/work/s8313697/software/ParaView-5.12.1-osmesa-MPI-Linux-Python3.10-x86_64/bin/pvbatch",
        "/usr/bin/pvbatch",
        "/usr/local/bin/pvbatch",
        "/opt/paraview/bin/pvbatch",
    ]:
        if os.path.isfile(cand):
            return cand
    return None


def main():
    ap = argparse.ArgumentParser(description="VTK -> 2 PNG -> 2 lossless MP4 (PNGs preserved)")
    ap.add_argument("vtk", help="Input VTK file (velocity_merged_NNNNNN.vtk)")
    ap.add_argument("step", type=int, help="Step number (for PNG filename)")
    ap.add_argument("--width", type=int, default=3840, help="Width in px (default 3840 = 4K)")
    ap.add_argument("--fps", type=int, default=33, help="MP4 fps (default 33)")
    ap.add_argument("--codec", default="libx264",
                    help="ffmpeg codec: libx264 (wide compat) or ffv1 (archive lossless)")
    ap.add_argument("--pix-fmt", default="yuv444p",
                    help="ffmpeg pixel format: yuv444p (true lossless) or yuv420p (wide compat)")
    ap.add_argument("--pvbatch", default=None, help="Override pvbatch path")
    ap.add_argument("--skip-encode", action="store_true",
                    help="Only render PNG, skip MP4 encode (useful in batch renders)")
    args = ap.parse_args()

    # 所有後續 cwd 固定 PROJECT_ROOT, 讓 relative path 在 render_frame 裡不會亂
    os.chdir(PROJECT_ROOT)

    if not os.path.isfile(args.vtk):
        print("[pipeline] ERROR: VTK not found: %s" % args.vtk, flush=True)
        sys.exit(1)

    os.makedirs(FRAMES_DIR, exist_ok=True)

    # --- Step 1: pvbatch render_frame.py --> 2 PNG ---
    pvbatch = args.pvbatch or find_pvbatch()
    if not pvbatch:
        print("[pipeline] ERROR: pvbatch not found (env PVBATCH / PATH / common paths all miss)",
              flush=True)
        sys.exit(2)

    render_script = os.path.join(ANIM_DIR, "render_frame.py")
    if not os.path.isfile(render_script):
        print("[pipeline] ERROR: render_frame.py not found: %s" % render_script,
              flush=True)
        sys.exit(3)

    cmd = [pvbatch, render_script,
           args.vtk,
           "--outdir", FRAMES_DIR,
           "--step", str(args.step),
           "--video-mode"]
    print("[pipeline] render: " + " ".join(cmd), flush=True)
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("[pipeline] ERROR: pvbatch render_frame.py failed rc=%d" % r.returncode,
              flush=True)
        sys.exit(4)

    if args.skip_encode:
        print("[pipeline] --skip-encode, PNG render done, skip MP4", flush=True)
        sys.exit(0)

    # --- Step 2: video_encode_mp4.py x 2 ---
    encode_script = os.path.join(ANIM_DIR, "video_encode_mp4.py")
    if not os.path.isfile(encode_script):
        print("[pipeline] ERROR: video_encode_mp4.py not found: %s" % encode_script,
              flush=True)
        sys.exit(5)

    encode_failures = 0
    for suffix, out_name in [("cont", "flow_cont.mp4"), ("RD", "flow_RD.mp4")]:
        cmd = ["python3", encode_script,
               "--out",     os.path.join(ANIM_DIR, out_name),
               "--pattern", os.path.join(FRAMES_DIR, "frame_*_%s.png" % suffix),
               "--fps",     str(args.fps),
               "--codec",   args.codec,
               "--pix-fmt", args.pix_fmt]
        print("[pipeline] encode %s: " % out_name + " ".join(cmd), flush=True)
        r = subprocess.run(cmd)
        if r.returncode != 0:
            # Non-fatal; PNG preserved, can rebuild later with rebuild_mp4.sh
            print("[pipeline] WARN: encode %s failed rc=%d (PNG preserved, use rebuild_mp4.sh to retry)"
                  % (out_name, r.returncode), flush=True)
            encode_failures += 1

    # --- Step 3: do NOT delete PNGs (resume asset) ---
    try:
        n_cont = len([f for f in os.listdir(FRAMES_DIR) if f.endswith("_cont.png")])
        n_rd = len([f for f in os.listdir(FRAMES_DIR) if f.endswith("_RD.png")])
    except OSError:
        n_cont = n_rd = -1
    print("[pipeline] step=%d done; png_frames has %d cont + %d RD (encode_failures=%d)"
          % (args.step, n_cont, n_rd, encode_failures), flush=True)
    sys.exit(0 if encode_failures == 0 else 10)


if __name__ == "__main__":
    main()
