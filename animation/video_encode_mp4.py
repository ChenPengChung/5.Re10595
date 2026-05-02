#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
video_encode_mp4.py — 從 PNG 序列 encode 一支 lossless MP4
===========================================================

設計:
  * PNG = source of truth; MP4 = derived artifact, 每次從頭 encode
  * 續跑安全: PNG 檔名 zero-padded step → glob 排序 = 時間順序
  * 原子寫入: tmp.mp4 → rename (失敗時絕不留半成品)
  * 並發保護: flock 防止兩個 encode 撞寫同一個 MP4

預設 lossless 設定:
  * libx264 + crf=0 + yuv444p = 純數學無損 (檔案中等, 幾乎所有 modern player 支援)
  * 替代: ffv1 + level=3 = 真 archive 無損 (檔案更小, 需 VLC/mpv/ffmpeg 播放)

Usage:
  python3 video_encode_mp4.py \\
      --out     animation/flow_cont.mp4 \\
      --pattern 'animation/png_frames/frame_*_cont.png' \\
      --fps     33 \\
      [--codec libx264|ffv1] [--pix-fmt yuv444p|yuv420p]

Exit codes:
  0  成功
  1  PNG 列表為空 / pattern 無 match
  2  ffmpeg 未找到
  3  ffmpeg encode 失敗
  4  並發被 lock 擋下 (其他 encode 在跑, 視為成功 skip)
"""
import os
import re
import sys
import glob
import argparse
import subprocess
import shutil
import fcntl
import tempfile


# Extract the step number from filenames like `frame_1116001_cont.png`.
# Used to sort PNG frames numerically — lexicographic sort fails once the
# step number outgrows the `%06d` zero-padding (e.g. step 1,050,001 as
# `frame_1050001_...` sorts before step 105,001 as `frame_105001_...`,
# producing out-of-order frames ~5s into the video at 33 fps).
_STEP_RE = re.compile(r'frame_(\d+)_')

def _step_of(path):
    m = _STEP_RE.search(os.path.basename(path))
    # Unknown-name files sort before valid ones (step 0 default) but stable
    # on the path string, so behaviour is deterministic.
    return (int(m.group(1)) if m else -1, path)


def find_ffmpeg():
    env = os.environ.get("FFMPEG")
    if env and os.path.isfile(env):
        return env
    w = shutil.which("ffmpeg")
    if w:
        return w
    # 使用者可能手動裝在 /work/
    for cand in [
        "/work/s8313697/software/ffmpeg-7.0.2-amd64-static/ffmpeg",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ]:
        if os.path.isfile(cand):
            return cand
    return None


def main():
    ap = argparse.ArgumentParser(description="Encode PNG sequence to lossless MP4")
    ap.add_argument("--out", required=True, help="Output MP4 path")
    ap.add_argument("--pattern", required=True,
                    help="Glob pattern for PNGs, e.g. 'png_frames/frame_*_cont.png'")
    ap.add_argument("--fps", type=int, default=33, help="MP4 playback fps (default 33)")
    ap.add_argument("--codec", default="libx264",
                    help="Video codec: libx264 (default, wide compat) or ffv1 (archive)")
    ap.add_argument("--pix-fmt", default="yuv444p",
                    help="Pixel format: yuv444p (true lossless) or yuv420p (wide compat)")
    ap.add_argument("--last-pause-sec", type=float, default=0.0,
                    help="Extend last frame duration by N sec (default 0 = uniform)")
    args = ap.parse_args()

    # Collect PNGs, sorted by step number extracted from filename (robust to
    # variable-width padding — see `_step_of` above).
    pngs = sorted(glob.glob(args.pattern), key=_step_of)
    if not pngs:
        print("[encode] no PNG matched pattern: %s" % args.pattern, flush=True)
        sys.exit(1)
    print("[encode] %d PNG frames matched for %s" % (len(pngs), args.out), flush=True)

    # ffmpeg
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("[encode] ERROR: ffmpeg not found on PATH/env/common paths", flush=True)
        sys.exit(2)

    # Concurrency lock (per-output)
    lock_path = args.out + ".lock"
    lock_fd = None
    try:
        # Best-effort lock; if already held, skip (the other encode will catch up anyway)
        lock_fd = open(lock_path, "w")
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[encode] another encode is running for %s, skip" % args.out, flush=True)
            sys.exit(4)

        # Write concat demuxer list: stable, supports arbitrary filenames + per-frame duration
        frame_dur = 1.0 / float(args.fps)
        out_dir = os.path.dirname(os.path.abspath(args.out)) or "."
        os.makedirs(out_dir, exist_ok=True)

        list_fd, list_path = tempfile.mkstemp(
            suffix=".concat.txt", prefix=".mp4list_", dir=out_dir, text=True)
        try:
            with os.fdopen(list_fd, "w") as fh:
                for p in pngs:
                    fh.write("file '%s'\n" % os.path.abspath(p))
                    fh.write("duration %.6f\n" % frame_dur)
                # concat demuxer needs last file repeated + (optional) hold duration
                last_hold = frame_dur + (args.last_pause_sec if args.last_pause_sec > 0 else 0)
                fh.write("file '%s'\n" % os.path.abspath(pngs[-1]))
                if args.last_pause_sec > 0:
                    fh.write("duration %.6f\n" % last_hold)
                    fh.write("file '%s'\n" % os.path.abspath(pngs[-1]))

            tmp_out = args.out + ".tmp.mp4"

            # Build ffmpeg cmd
            cmd = [ffmpeg, "-y",
                   "-loglevel", "warning",
                   "-f", "concat", "-safe", "0",
                   "-i", list_path,
                   "-vsync", "vfr",
                   "-c:v", args.codec]
            if args.codec == "libx264":
                cmd += ["-preset", "ultrafast",
                        "-crf", "0",           # crf=0 + yuv444p = true lossless
                        "-pix_fmt", args.pix_fmt]
            elif args.codec == "ffv1":
                cmd += ["-level", "3",
                        "-g", "1",             # all keyframes
                        "-slices", "12",
                        "-slicecrc", "1",
                        "-pix_fmt", args.pix_fmt]
            else:
                # User-supplied codec — pass pix_fmt, no additional tuning
                cmd += ["-pix_fmt", args.pix_fmt]
            cmd += [tmp_out]

            print("[encode] " + " ".join(cmd), flush=True)
            r = subprocess.run(cmd)
            if r.returncode != 0:
                print("[encode] ERROR: ffmpeg rc=%d" % r.returncode, flush=True)
                try:
                    os.unlink(tmp_out)
                except OSError:
                    pass
                sys.exit(3)

            # Atomic rename
            os.replace(tmp_out, args.out)

            sz_mb = os.path.getsize(args.out) / (1024.0 * 1024.0)
            print("[encode] %s written: frames=%d fps=%d codec=%s pix=%s size=%.2f MB"
                  % (args.out, len(pngs), args.fps, args.codec, args.pix_fmt, sz_mb),
                  flush=True)
            sys.exit(0)

        finally:
            try:
                os.unlink(list_path)
            except OSError:
                pass

    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                lock_fd.close()
            except Exception:
                pass
            try:
                os.unlink(lock_path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
