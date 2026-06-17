#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gif_render_loop.py — 全場 GIF 批次渲染 driver (lbm-render-1 快路徑) [Edit6_5600DNS 版]
====================================================================================
依 CLAUDE.md「全場 GIF 動畫設定」+ 使用者更正 (claude_animation):
  * 用 lbm-render-1 快路徑 (render_frame.py --slice-only, fast_slice X=mid 薄板),
    不用耗時的完整 lbm-render (跳過 Path D Q-criterion 三維)。
  * 固定 u_streamwise 色階 [--u-range] → 跨幀統一, 消閃爍。
  * 兩組連續配色: cont (KEY_COLORS) + RD_cont (Rainbow Desaturated)。
  * 影格等間隔: 逐顆「完整」complete VTK 各渲一幀 (本專案 NDTVTK=50000 → stride 50000),
    由舊到新先抓 (舊檔最快被 rolling-purge), 確保不挖洞、時間間隔一致。
  * 累積到 TARGET 幀後停; 期間每 ENCODE_EVERY 幀重編一次 2 個 GIF 供預覽。

不修改 video_encode_mp4.py / pipeline.py 的 MP4 流程 (本檔為追加)。
不觸碰任何 Slurm job / restart / checkpoint。

Edit6 與 Edit7 差異:
  * grid = 257x513x257 (Edit7 = 449x897x449) → fast_slice.py 自動讀 DIMENSIONS, 無需改。
  * 完整 VTK = 5,150,237,653 B (~5.15GB; Edit7 ~17GB) → FULL_MIN 調為 5.10e9。
  * velocity 場已 ÷Uref (velocity_Y 範圍實測 ~[-0.48,1.33]) → 固定色階 [-2,2] 安全涵蓋。
  * NDTVTK=50000 → 一顆 VTK ~400s; 1000 幀需 ~4.6 天 (用 GIF_MAX_WALL 覆寫上限)。

環境變數:
  GIF_TARGET       目標幀數 (default 100)
  GIF_MAX_WALL     全程上限秒數 (default 4h); 1000 幀請設大 (e.g. 450000 ≈ 5.2 天)
  GIF_WIDTH        GIF 寬 px (default 1600)
  GIF_FPS          播放 fps (default 20 = 每幀 0.05s)
  GIF_ENCODE_EVERY 每渲滿幾幀重編一次 GIF 預覽 (default 10)
  GIF_UMIN/GIF_UMAX 固定 u_streamwise 色階 (default -2.0 / 2.0)
"""
import os, sys, re, glob, time, subprocess

ROOT = "/home/s8313697/5.Re10595/Edit6_5600DNS"
PV = "/work/s8313697/software/ParaView-5.12.1-osmesa-MPI-Linux-Python3.10-x86_64/bin/pvbatch"
RENDER = os.path.join(ROOT, "animation", "render_frame.py")
GIF_ENC = os.path.join(ROOT, "animation", "gif_encode.py")
FRAMES_DIR = os.path.join(ROOT, "animation", "png_frames")
RESULT_DIR = os.path.join(ROOT, "result")
LOG = os.path.join(ROOT, "animation", "gif_render_loop.log")

TARGET = int(os.environ.get("GIF_TARGET", "100"))
UMIN = float(os.environ.get("GIF_UMIN", "-2.0"))
UMAX = float(os.environ.get("GIF_UMAX", "2.0"))
WIDTH = int(os.environ.get("GIF_WIDTH", "1600"))
FPS = float(os.environ.get("GIF_FPS", "20"))
FULL_MIN = int(os.environ.get("GIF_FULL_MIN", "5100000000"))   # 完整 5.15GB; 半截檔 (<5.1GB) 視為未寫完
POLL = 15                        # s, 等新 VTK 的輪詢間隔
PER_FRAME_TIMEOUT = 360          # s, 單幀渲染上限
MAX_WALL = int(os.environ.get("GIF_MAX_WALL", str(4 * 3600)))          # s, 全程上限
ENCODE_EVERY = int(os.environ.get("GIF_ENCODE_EVERY", "10"))          # 每渲滿幾幀重編一次 GIF 預覽

OUT_CONT = os.path.join(ROOT, "animation", "flow_cont.gif")
OUT_RD = os.path.join(ROOT, "animation", "flow_RD.gif")

_re_step = re.compile(r"velocity_merged_(\d+)\.vtk$")


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def complete_vtks():
    out = []
    for f in glob.glob(os.path.join(RESULT_DIR, "velocity_merged_*.vtk")):
        m = _re_step.search(f)
        if not m:
            continue
        try:
            sz = os.path.getsize(f)
        except OSError:
            continue
        if sz >= FULL_MIN:
            out.append((int(m.group(1)), f))
    out.sort()
    return out


def is_rendered(step):
    return (os.path.isfile(os.path.join(FRAMES_DIR, "frame_%06d_cont.png" % step)) and
            os.path.isfile(os.path.join(FRAMES_DIR, "frame_%06d_RD_cont.png" % step)))


def render_one(step, vtk):
    t0 = time.time()
    cmd = [PV, RENDER, vtk, "--slice-only", "--step", str(step),
           "--outdir", FRAMES_DIR, "--u-range", str(UMIN), str(UMAX)]
    try:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=PER_FRAME_TIMEOUT)
    except subprocess.TimeoutExpired:
        log("  render step=%d TIMEOUT (>%ds)" % (step, PER_FRAME_TIMEOUT))
        return False
    dt = time.time() - t0
    ok = (r.returncode == 0) and is_rendered(step)
    if not ok:
        tail = r.stderr.decode("utf-8", "ignore")[-300:] if r.stderr else ""
        log("  render step=%d FAILED rc=%d %.1fs %s" % (step, r.returncode, dt, tail))
    else:
        log("  render step=%d OK %.1fs" % (step, dt))
    return ok


def encode_gifs(nframes):
    for suffix, out in (("cont", OUT_CONT), ("RD_cont", OUT_RD)):
        cmd = ["python3", GIF_ENC, "--frames-dir", FRAMES_DIR, "--suffix", suffix,
               "--out", out, "--fps", str(FPS), "--width", str(WIDTH)]
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=600)
            msg = r.stdout.decode("utf-8", "ignore").strip().splitlines()
            log("  encode %s rc=%d :: %s" % (suffix, r.returncode, msg[-1] if msg else ""))
        except subprocess.TimeoutExpired:
            log("  encode %s TIMEOUT" % suffix)


def main():
    os.makedirs(FRAMES_DIR, exist_ok=True)
    rendered = set()
    for f in os.listdir(FRAMES_DIR):
        m = re.match(r"frame_(\d+)_cont\.png$", f)
        if m and is_rendered(int(m.group(1))):
            rendered.add(int(m.group(1)))
    log("START target=%d  already-rendered=%d  width=%d fps=%g range=[%.1f,%.1f] "
        "max_wall=%ds full_min=%d" %
        (TARGET, len(rendered), WIDTH, FPS, UMIN, UMAX, MAX_WALL, FULL_MIN))

    start = time.time()
    last_enc = len(rendered)
    while len(rendered) < TARGET and (time.time() - start) < MAX_WALL:
        progressed = False
        for step, vtk in complete_vtks():       # ascending: 由舊到新先抓 (舊的最快被 purge)
            if len(rendered) >= TARGET:
                break
            if step in rendered:
                continue
            if is_rendered(step):
                rendered.add(step); continue
            if render_one(step, vtk):
                rendered.add(step)
                progressed = True
                log("PROGRESS %d/%d (step=%d)" % (len(rendered), TARGET, step))
                if len(rendered) - last_enc >= ENCODE_EVERY:
                    encode_gifs(len(rendered)); last_enc = len(rendered)
        if len(rendered) < TARGET and not progressed:
            time.sleep(POLL)

    if (time.time() - start) >= MAX_WALL and len(rendered) < TARGET:
        log("MAX_WALL reached: %d/%d frames. (raise GIF_MAX_WALL to continue capturing)" %
            (len(rendered), TARGET))
    log("RENDER LOOP DONE: %d frames (target %d). Final encode..." % (len(rendered), TARGET))
    encode_gifs(len(rendered))
    log("ALL DONE. GIFs: %s | %s" % (OUT_CONT, OUT_RD))


if __name__ == "__main__":
    main()
