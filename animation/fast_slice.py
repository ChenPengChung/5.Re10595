#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fast_slice.py — 從超大 BINARY STRUCTURED_GRID VTK 直接抽取 X=mid 薄板
====================================================================
動機:
  velocity_merged_*.vtk 每個 ~17GB / 180M 點。render_frame.py 的 Path A/B/C
  其實只用到 X=mid 的 YZ 切片 (NX=449 為奇數 → xmid 落在節點 i=(NX-1)/2),
  但 ParaView 的 LegacyVTKReader 必須把整顆 17GB 灌進記憶體才能切。

  本工具用 numpy 直接解析 VTK legacy binary,**只抽出 i ∈ [ic-half, ic+half]
  的薄板** (預設 3 個 i-plane),寫成一個小 STRUCTURED_GRID VTK
  (DIMENSIONS = (2*half+1) × NY × NZ, ~數十 MB)。

  下游 render_frame.py 用 --slice-only 載入這個小檔,
  其 Slice(Origin=xmid) 落在薄板正中平面 = 原始 i=ic → 逐點精確、零插值誤差,
  配色 / 解析度 / 雙色階 全部沿用,效果與讀整顆 17GB 完全一致。

VTK legacy binary 規格要點:
  * binary 區塊一律 big-endian → numpy dtype '>f8' (double)。
  * 只對 ASCII 表頭 readline();binary 區塊一律用「計算出的位元組大小」seek 跳過,
    絕不跨 binary readline (binary 內含 0x0A 會誤判換行)。
  * vtkDataWriter 在每個 binary 區塊後會補一個 '\n',解析時略過空行即可。

幾何快取:
  POINTS (座標) 每個時間步都一樣 → 第一次抽出後快取成 .npy,
  之後每步只讀變動的場 (velocity 等),省下 4.3GB 的座標重讀。

用法:
  python3 fast_slice.py --introspect <vtk>
  python3 fast_slice.py <vtk> <out_slab.vtk> [--half 1] [--fields velocity,U_mean,...]
  python3 fast_slice.py --auto --outdir result/_slice_cache [--half 1]
        (--auto: 自動挑 result/ 最新「完整」的 velocity_merged_*.vtk;
         印出 STEP=<n> 與 SLAB=<path> 供 shell 取用)
"""
import os, sys, glob, struct, time

import numpy as np


# ─────────────────────────────────────────────────────────────────────
# §1  解析 VTK legacy header → 場目錄 (name, ncomp, dtype, offset, nbytes)
# ─────────────────────────────────────────────────────────────────────
_VTK_DTYPE = {
    "double": (">f8", 8),
    "float":  (">f4", 4),
    "int":    (">i4", 4),
    "unsigned_int": (">u4", 4),
    "long":   (">i8", 8),
    "unsigned_char": ("u1", 1),
    "char":   ("i1", 1),
    "short":  (">i2", 2),
    "unsigned_short": (">u2", 2),
}


def _readline_ascii(f):
    """讀一行 ASCII header line;回傳 (str, raw_bytes)。EOF 回傳 (None, b'')。"""
    raw = f.readline()
    if not raw:
        return None, b""
    return raw.decode("latin-1"), raw


def parse_vtk_structured_binary(path):
    """解析一個 BINARY STRUCTURED_GRID legacy VTK。
    回傳 dict:
      dims      = (NX, NY, NZ)
      npoints   = NX*NY*NZ
      points    = {'offset':int, 'nbytes':int, 'dtype':str, 'ncomp':3}
      arrays    = [ {'name','ncomp','dtype','itemsize','offset','nbytes','kind'} ... ]
    kind ∈ {'SCALARS','VECTORS','FIELD','NORMALS','TENSORS'}
    """
    info = {"dims": None, "npoints": None, "points": None, "arrays": [],
            "fmt": None, "dataset": None}
    f = open(path, "rb")
    try:
        # ── 表頭直到 POINTS ──
        # line0: # vtk DataFile Version
        _readline_ascii(f)            # magic
        _readline_ascii(f)            # comment/title
        while True:
            s, raw = _readline_ascii(f)
            if s is None:
                raise ValueError("EOF before POINTS")
            t = s.strip()
            tu = t.upper()
            if tu in ("BINARY", "ASCII"):
                info["fmt"] = tu
                if tu == "ASCII":
                    raise ValueError("fast_slice 只支援 BINARY VTK,本檔是 ASCII")
            elif tu.startswith("DATASET"):
                info["dataset"] = t.split()[1]
            elif tu.startswith("DIMENSIONS"):
                parts = t.split()
                info["dims"] = (int(parts[1]), int(parts[2]), int(parts[3]))
            elif tu.startswith("POINTS"):
                parts = t.split()
                npts = int(parts[1])
                dt_name = parts[2].lower()
                dtype, itemsize = _VTK_DTYPE[dt_name]
                info["npoints"] = npts
                off = f.tell()
                nbytes = npts * 3 * itemsize
                info["points"] = {"offset": off, "nbytes": nbytes,
                                  "dtype": dtype, "itemsize": itemsize, "ncomp": 3}
                f.seek(off + nbytes)
                break

        npts = info["npoints"]

        # ── POINT_DATA / CELL_DATA 與後續欄位 ──
        cur_n = npts
        while True:
            s, raw = _readline_ascii(f)
            if s is None:
                break
            t = s.strip()
            if t == "":
                continue                          # binary 區塊後的 '\n'
            tu = t.upper()
            parts = t.split()
            if tu.startswith("POINT_DATA"):
                cur_n = int(parts[1]); continue
            if tu.startswith("CELL_DATA"):
                cur_n = int(parts[1]); continue
            if tu.startswith("SCALARS"):
                name = parts[1]; dt_name = parts[2].lower()
                ncomp = int(parts[3]) if len(parts) > 3 else 1
                dtype, itemsize = _VTK_DTYPE[dt_name]
                # 下一行: LOOKUP_TABLE ...
                s2, _ = _readline_ascii(f)
                off = f.tell()
                nbytes = cur_n * ncomp * itemsize
                info["arrays"].append({"name": name, "ncomp": ncomp, "dtype": dtype,
                                       "itemsize": itemsize, "offset": off,
                                       "nbytes": nbytes, "kind": "SCALARS"})
                f.seek(off + nbytes); continue
            if tu.startswith("VECTORS") or tu.startswith("NORMALS"):
                name = parts[1]; dt_name = parts[2].lower()
                dtype, itemsize = _VTK_DTYPE[dt_name]
                off = f.tell()
                nbytes = cur_n * 3 * itemsize
                info["arrays"].append({"name": name, "ncomp": 3, "dtype": dtype,
                                       "itemsize": itemsize, "offset": off,
                                       "nbytes": nbytes, "kind": tu.split()[0]})
                f.seek(off + nbytes); continue
            if tu.startswith("TENSORS"):
                name = parts[1]; dt_name = parts[2].lower()
                dtype, itemsize = _VTK_DTYPE[dt_name]
                off = f.tell()
                nbytes = cur_n * 9 * itemsize
                info["arrays"].append({"name": name, "ncomp": 9, "dtype": dtype,
                                       "itemsize": itemsize, "offset": off,
                                       "nbytes": nbytes, "kind": "TENSORS"})
                f.seek(off + nbytes); continue
            if tu.startswith("FIELD"):
                num_arrays = int(parts[2])
                for _ in range(num_arrays):
                    s3, _ = _readline_ascii(f)
                    while s3 is not None and s3.strip() == "":
                        s3, _ = _readline_ascii(f)
                    fp = s3.split()
                    fname = fp[0]; ncomp = int(fp[1]); ntup = int(fp[2])
                    dt_name = fp[3].lower()
                    dtype, itemsize = _VTK_DTYPE[dt_name]
                    off = f.tell()
                    nbytes = ncomp * ntup * itemsize
                    info["arrays"].append({"name": fname, "ncomp": ncomp, "dtype": dtype,
                                           "itemsize": itemsize, "offset": off,
                                           "nbytes": nbytes, "kind": "FIELD"})
                    f.seek(off + nbytes)
                continue
            # 未知 keyword → 停止 (避免誤解析)
            break
    finally:
        f.close()
    return info


def introspect(path):
    t0 = time.time()
    info = parse_vtk_structured_binary(path)
    NX, NY, NZ = info["dims"]
    fsize = os.path.getsize(path)
    print("File   : %s (%.2f GB)" % (path, fsize / 1073741824.0))
    print("Format : %s  DATASET %s" % (info["fmt"], info["dataset"]))
    print("DIMS   : NX=%d NY=%d NZ=%d  npoints=%d" % (NX, NY, NZ, info["npoints"]))
    p = info["points"]
    print("POINTS : offset=%d nbytes=%d (%.2f GB) dtype=%s" %
          (p["offset"], p["nbytes"], p["nbytes"] / 1073741824.0, p["dtype"]))
    last = p["offset"] + p["nbytes"]
    print("ARRAYS : %d" % len(info["arrays"]))
    total_data = p["nbytes"]
    for a in info["arrays"]:
        gap = a["offset"] - last
        print("  %-14s kind=%-8s ncomp=%d dtype=%s offset=%d nbytes=%d (%.0f MB) gap=%d" %
              (a["name"], a["kind"], a["ncomp"], a["dtype"], a["offset"],
               a["nbytes"], a["nbytes"] / 1048576.0, gap))
        last = a["offset"] + a["nbytes"]
        total_data += a["nbytes"]
    tail = fsize - last
    print("tail bytes after last array: %d" % tail)
    print("sum(points+arrays)=%d  filesize=%d  diff=%d" %
          (total_data, fsize, fsize - total_data))
    print("parse time: %.3fs" % (time.time() - t0))
    return info


# ─────────────────────────────────────────────────────────────────────
# §2  抽取 X=mid 薄板 + 寫小 VTK
# ─────────────────────────────────────────────────────────────────────
def _memmap_field(path, a, NX, NY, NZ):
    """回傳 memmap view, shape (NZ,NY,NX) 或 (NZ,NY,NX,ncomp)。"""
    nc = a["ncomp"]
    if nc == 1:
        shape = (NZ, NY, NX)
    else:
        shape = (NZ, NY, NX, nc)
    return np.memmap(path, dtype=a["dtype"], mode="r",
                     offset=a["offset"], shape=shape)


def extract_slab(path, out_path, half=1, fields=None, geom_cache=None,
                 verbose=True):
    """抽 i ∈ [ic-half, ic+half] 薄板,寫 BINARY STRUCTURED_GRID 小 VTK。
    fields=None → 全部 point-data 場; 否則只抽指定名單。
    回傳 (ic, i0, i1, dims_out)。"""
    t0 = time.time()
    info = parse_vtk_structured_binary(path)
    NX, NY, NZ = info["dims"]
    ic = (NX - 1) // 2
    i0 = max(0, ic - half)
    i1 = min(NX - 1, ic + half)
    W = i1 - i0 + 1
    if verbose:
        print("[fast_slice] dims=%dx%dx%d  ic=%d  slab i=[%d..%d] (W=%d)" %
              (NX, NY, NZ, ic, i0, i1, W), flush=True)

    # ── 決定要抽的場 + 截斷防護 (在任何 memmap 讀取前) ──
    #    只要 file 還沒寫到「points + 要抽的場」所需的最後位元組, 就視為半截檔 (solver 正在寫),
    #    乾淨報錯而非讀到 EOF 後的零/垃圾。omega_* 等不抽的場即使未寫完也不影響。
    want = None if fields is None else set(fields)
    to_extract = []
    for a in info["arrays"]:
        if a["kind"] not in ("SCALARS", "VECTORS", "NORMALS"):
            continue                # FIELD/TENSORS 暫不下傳 (render 用不到)
        if a["dtype"] not in (">f8", ">f4"):
            continue                # 只取浮點場
        if want is not None and a["name"] not in want:
            continue
        to_extract.append(a)

    p = info["points"]
    needed_end = p["offset"] + p["nbytes"]
    for a in to_extract:
        needed_end = max(needed_end, a["offset"] + a["nbytes"])
    fsize = os.path.getsize(path)
    if fsize < needed_end:
        raise ValueError(
            "VTK 疑似半截檔/正在寫入: size=%d < needed=%d (%s)。"
            "請改用較舊的完整檔, 或稍候重試。" % (fsize, needed_end, os.path.basename(path)))

    # ── 幾何 (POINTS) — 可快取 ──
    geom_key = None
    pts_slab = None
    if geom_cache:
        geom_key = os.path.join(
            geom_cache, "geom_%dx%dx%d_ic%d_h%d.npy" % (NX, NY, NZ, ic, half))
        if os.path.isfile(geom_key):
            pts_slab = np.load(geom_key)
            if verbose:
                print("[fast_slice] geom cache hit: %s" % geom_key, flush=True)
    if pts_slab is None:
        mm = np.memmap(path, dtype=p["dtype"], mode="r",
                       offset=p["offset"], shape=(NZ, NY, NX, 3))
        pts_slab = np.ascontiguousarray(mm[:, :, i0:i1 + 1, :], dtype="<f8")
        del mm
        if geom_key:
            os.makedirs(geom_cache, exist_ok=True)
            np.save(geom_key, pts_slab)
        if verbose:
            print("[fast_slice] points slab read %.2fs" % (time.time() - t0), flush=True)

    # ── 點資料場 ──
    out_arrays = []
    for a in to_extract:
        ta = time.time()
        mm = _memmap_field(path, a, NX, NY, NZ)
        if a["ncomp"] == 1:
            sl = np.ascontiguousarray(mm[:, :, i0:i1 + 1], dtype="<f8")
        else:
            sl = np.ascontiguousarray(mm[:, :, i0:i1 + 1, :], dtype="<f8")
        del mm
        out_arrays.append((a["name"], a["ncomp"], a["kind"], sl))
        if verbose:
            print("[fast_slice]   %-14s slab %.2fs" % (a["name"], time.time() - ta),
                  flush=True)

    _write_structured_grid_binary(out_path, (W, NY, NZ), pts_slab, out_arrays)
    if verbose:
        sz = os.path.getsize(out_path) / 1048576.0
        print("[fast_slice] wrote %s (%.1f MB) total %.2fs" %
              (out_path, sz, time.time() - t0), flush=True)
    _prune_old_slabs(os.path.dirname(os.path.abspath(out_path)), keep=6, verbose=verbose)
    return ic, i0, i1, (W, NY, NZ)


def _prune_old_slabs(slab_dir, keep=6, verbose=False):
    """只刪自己產生的 slab_*.vtk (留最新 keep 個);絕不碰 geom_*.npy 或其他檔。"""
    try:
        slabs = glob.glob(os.path.join(slab_dir, "slab_*.vtk"))
        if len(slabs) <= keep:
            return
        slabs.sort(key=lambda p: os.path.getmtime(p))   # 舊→新
        for old in slabs[:-keep]:
            try:
                os.remove(old)
                if verbose:
                    print("[fast_slice] pruned old slab: %s" % os.path.basename(old),
                          flush=True)
            except OSError:
                pass
    except Exception:
        pass


def _write_structured_grid_binary(out_path, dims_out, pts_slab, out_arrays):
    """寫 BINARY STRUCTURED_GRID。pts_slab shape (NZ,NY,W,3);
    out_arrays = [(name, ncomp, kind, slab_array)]。big-endian。"""
    W, NY, NZ = dims_out
    npts = W * NY * NZ
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(b"# vtk DataFile Version 3.0\n")
        f.write(b"fast_slice X-mid slab\n")
        f.write(b"BINARY\n")
        f.write(b"DATASET STRUCTURED_GRID\n")
        f.write(("DIMENSIONS %d %d %d\n" % (W, NY, NZ)).encode())
        f.write(("POINTS %d double\n" % npts).encode())
        # ravel C-order of (NZ,NY,W,3) → comp innermost, i' fastest → VTK order ✓
        f.write(np.ascontiguousarray(pts_slab, dtype=">f8").tobytes())
        f.write(b"\n")
        f.write(("POINT_DATA %d\n" % npts).encode())
        for name, ncomp, kind, sl in out_arrays:
            big = np.ascontiguousarray(sl, dtype=">f8")
            if ncomp == 1 or kind == "SCALARS":
                f.write(("SCALARS %s double %d\n" % (name, ncomp)).encode())
                f.write(b"LOOKUP_TABLE default\n")
            else:
                f.write(("VECTORS %s double\n" % name).encode())
            f.write(big.tobytes())
            f.write(b"\n")


# ─────────────────────────────────────────────────────────────────────
# §3  --auto: 挑最新「完整」VTK
# ─────────────────────────────────────────────────────────────────────
def find_latest_complete(search_dirs=("result", "../result", ".")):
    cands = []
    for d in search_dirs:
        cands += glob.glob(os.path.join(d, "velocity_merged_*.vtk"))
        if cands:
            break
    if not cands:
        return None, None
    sized = []
    for c in cands:
        try:
            sized.append((c, os.path.getsize(c)))
        except OSError:
            pass
    if not sized:
        return None, None
    maxsz = max(s for _, s in sized)
    # 只留尺寸 ≥ 99% max 的 (排除正在寫入的半截檔),取檔名最大 (最新 step)
    complete = [(c, s) for c, s in sized if s >= 0.99 * maxsz]
    complete.sort(key=lambda cs: cs[0])
    path = complete[-1][0]
    base = os.path.splitext(os.path.basename(path))[0]
    step = None
    for tok in base.split("_"):
        if tok.isdigit():
            step = int(tok)
    return os.path.abspath(path), step


# ─────────────────────────────────────────────────────────────────────
# §4  CLI
# ─────────────────────────────────────────────────────────────────────
def main(argv):
    if not argv:
        print(__doc__); return 1
    half = 1
    fields = None
    outdir = "result/_slice_cache"
    out_path = None
    in_path = None
    do_introspect = False
    do_auto = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--introspect":
            do_introspect = True; i += 1
        elif a == "--auto":
            do_auto = True; i += 1
        elif a == "--half" and i + 1 < len(argv):
            half = int(argv[i + 1]); i += 2
        elif a == "--fields" and i + 1 < len(argv):
            fields = [x for x in argv[i + 1].split(",") if x]; i += 2
        elif a == "--outdir" and i + 1 < len(argv):
            outdir = argv[i + 1]; i += 2
        elif a == "--out" and i + 1 < len(argv):
            out_path = argv[i + 1]; i += 2
        elif not a.startswith("--"):
            if in_path is None:
                in_path = a
            elif out_path is None:
                out_path = a
            i += 1
        else:
            i += 1

    step = None
    if do_auto:
        in_path, step = find_latest_complete()
        if in_path is None:
            print("ERROR: no velocity_merged_*.vtk found"); return 1
        print("[fast_slice] auto latest complete: %s (step=%s)" % (in_path, step))

    if in_path is None:
        print("ERROR: no input VTK"); return 1

    if do_introspect:
        introspect(in_path); return 0

    if out_path is None:
        tag = "%06d" % step if step is not None else \
            os.path.splitext(os.path.basename(in_path))[0]
        out_path = os.path.join(outdir, "slab_%s.vtk" % tag)

    extract_slab(in_path, out_path, half=half, fields=fields,
                 geom_cache=outdir)
    # 供 shell 取用
    print("STEP=%s" % (step if step is not None else ""))
    print("SLAB=%s" % os.path.abspath(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
