#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gif_encode.py — 全場 GIF 動畫編碼 (ADDITIVE, 不動 video_encode_mp4.py)
=====================================================================
從 png_frames/ 的 frame_NNNNNN_<suffix>.png 序列, 用 ffmpeg 兩段式
單一全域調色盤 (palettegen stats_mode=full → paletteuse) 編成 GIF。

- 單一全域調色盤 → 跨幀顏色一致 (配合 render_frame.py --u-range 固定色階, 消閃爍)。
- 20 fps = 每幀 0.05s = 5 centisecond, GIF delay 可精確表示。
- 預設只取「等間隔 (固定 stride) 的最長連續 run」→ 保證影格時間間隔一致
  (相隔固定 step 數的 VTK), 避免 rolling-purge 造成的跳幀使動畫忽快忽慢。

用法:
  python3 gif_encode.py --frames-dir animation/png_frames \\
      --suffix cont    --out animation/flow_cont.gif --fps 20 --width 1600
  python3 gif_encode.py --frames-dir animation/png_frames \\
      --suffix RD_cont --out animation/flow_RD.gif   --fps 20 --width 1600

suffix:
  cont    → 連續 KEY_COLORS 配色 (frame_NNNNNN_cont.png, 排除 _RD_cont)
  RD_cont → 連續 Rainbow Desaturated 配色 (frame_NNNNNN_RD_cont.png)
"""
import os, sys, re, glob, argparse, subprocess, shutil

FFMPEG_DEFAULT = "/work/s8313697/software/ffmpeg-7.0.2-amd64-static/ffmpeg"


def find_ffmpeg(override):
    for c in (override, FFMPEG_DEFAULT, shutil.which("ffmpeg")):
        if c and os.path.isfile(c):
            return c
    return None


def collect(frames_dir, suffix):
    """回傳 [(step:int, path:str)] 依 step 排序。
    cont 與 RD_cont 用精確 regex 區分 (避免 *_cont.png 同時吃到 *_RD_cont.png)。"""
    if suffix == "cont":
        pat = re.compile(r"^frame_(\d+)_cont\.png$")
    elif suffix in ("RD_cont", "RD-cont"):
        pat = re.compile(r"^frame_(\d+)_RD_cont\.png$")
    else:
        pat = re.compile(r"^frame_(\d+)_%s\.png$" % re.escape(suffix))
    out = []
    for f in os.listdir(frames_dir):
        m = pat.match(f)
        if m:
            out.append((int(m.group(1)), os.path.join(frames_dir, f)))
    out.sort(key=lambda x: x[0])
    return out


def longest_uniform_run(items):
    """從 [(step,path)] 取「固定 stride 的最長連續 run」。
    stride = 相鄰 step 差的眾數。回傳 (run_items, stride)。"""
    if len(items) <= 1:
        return items, None
    steps = [s for s, _ in items]
    diffs = [steps[i + 1] - steps[i] for i in range(len(steps) - 1)]
    # 眾數 stride
    stride = max(set(diffs), key=diffs.count)
    best, cur = [items[0]], [items[0]]
    for i in range(1, len(items)):
        if steps[i] - steps[i - 1] == stride:
            cur.append(items[i])
        else:
            if len(cur) > len(best):
                best = cur
            cur = [items[i]]
    if len(cur) > len(best):
        best = cur
    return best, stride


def main():
    ap = argparse.ArgumentParser(description="PNG 序列 -> GIF (單一全域調色盤, ADDITIVE)")
    ap.add_argument("--frames-dir", required=True)
    ap.add_argument("--suffix", required=True, help="cont | RD_cont")
    ap.add_argument("--out", required=True)
    ap.add_argument("--fps", type=float, default=20)
    ap.add_argument("--width", type=int, default=1600, help="GIF 寬 (px), 高依比例 (default 1600)")
    ap.add_argument("--ffmpeg", default=None)
    ap.add_argument("--no-uniform", action="store_true",
                    help="不修剪成等間隔連續 run (預設會修剪以保證影格間隔一致)")
    ap.add_argument("--bayer-scale", type=int, default=3)
    args = ap.parse_args()

    ff = find_ffmpeg(args.ffmpeg)
    if not ff:
        print("[gif] ERROR: ffmpeg not found", flush=True); sys.exit(2)
    if not os.path.isdir(args.frames_dir):
        print("[gif] ERROR: frames-dir not found: %s" % args.frames_dir, flush=True); sys.exit(3)

    items = collect(args.frames_dir, args.suffix)
    if not items:
        print("[gif] ERROR: no frames matching suffix '%s' in %s" % (args.suffix, args.frames_dir),
              flush=True); sys.exit(4)

    if not args.no_uniform:
        run, stride = longest_uniform_run(items)
        if len(run) < len(items):
            print("[gif] WARN: %d frames total but longest uniform-stride(=%s) run = %d; "
                  "using the uniform run only (影格間隔一致)." % (len(items), stride, len(run)),
                  flush=True)
        items = run
    steps = [s for s, _ in items]
    print("[gif] suffix=%s frames=%d steps=[%d..%d] stride=%s -> %s" % (
        args.suffix, len(items), steps[0], steps[-1],
        (steps[1] - steps[0]) if len(steps) > 1 else "-", args.out), flush=True)

    # 連續編號 symlink → ffmpeg image2 demuxer 穩定排序
    tmp = os.path.join(args.frames_dir, ".gif_tmp_%s" % args.suffix)
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    for k, (_, p) in enumerate(items):
        os.symlink(os.path.abspath(p), os.path.join(tmp, "seq_%05d.png" % k))

    palette = os.path.join(tmp, "palette.png")
    scale = "scale=%d:-2:flags=lanczos" % args.width
    out_abs = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_abs), exist_ok=True)

    try:
        # pass 1: 全域調色盤
        p1 = [ff, "-y", "-framerate", str(args.fps),
              "-i", os.path.join(tmp, "seq_%05d.png"),
              "-vf", "%s,palettegen=stats_mode=full" % scale, palette]
        r = subprocess.run(p1, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode != 0:
            print("[gif] ERROR palettegen rc=%d\n%s" % (r.returncode, r.stderr.decode("utf-8", "ignore")[-800:]),
                  flush=True); sys.exit(5)
        # pass 2: 套用調色盤 -> GIF (無限迴圈播放)
        p2 = [ff, "-y", "-framerate", str(args.fps),
              "-i", os.path.join(tmp, "seq_%05d.png"), "-i", palette,
              "-lavfi", "%s[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=%d:diff_mode=rectangle"
              % (scale, args.bayer_scale),
              "-loop", "0", out_abs]
        r = subprocess.run(p2, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode != 0:
            print("[gif] ERROR paletteuse rc=%d\n%s" % (r.returncode, r.stderr.decode("utf-8", "ignore")[-800:]),
                  flush=True); sys.exit(6)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    sz = os.path.getsize(out_abs) / 1048576.0
    print("[gif] DONE %s  (%d frames @ %g fps = %.3gs/frame, %.1f MB)" % (out_abs, len(items), args.fps, 1.0/args.fps, sz), flush=True)


if __name__ == "__main__":
    main()
