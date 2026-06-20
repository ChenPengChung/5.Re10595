#!/usr/bin/env python3
# test_vtk_stream_read.py — 單元測試: _read_binary_stream(串流分塊讀 + 截斷守門)
#   驗 (1) 完整 bytes → np.array_equal bit-identical(float32 與 float64);
#       (2) 分塊邊界正確(小 chunk_elems 強制多塊);
#       (3) 截斷 bytes → raise ValueError(不靜默回傳垃圾尾);
#       (4) 兩檔 helper 原始碼一致。
# 抽出函式源碼 exec(不 import 整個 benchmark 模組, 避免 argparse 等 import 期副作用)。
import io, re, sys
import numpy as np

def extract_helper(path):
    code = open(path).read()
    m = re.search(r"def _read_binary_stream\(.*?\n    return out\n", code, re.DOTALL)
    assert m, f"helper _read_binary_stream not found in {path}"
    ns = {"np": np}
    exec(m.group(0), ns)
    return ns["_read_binary_stream"], m.group(0)

f_bench, src_bench = extract_helper("result/2.Benchmark.py")
f_tau,   src_tau   = extract_helper("result/10.tau_wall_benchmark.py")
assert src_bench == src_tau, "★兩檔 _read_binary_stream 源碼不一致"

n   = 5000
src = (np.arange(n, dtype=np.float64) * 1.234567 - 7.0)
raw = src.astype(">f8").tobytes()          # VTK binary = big-endian float64
fails = 0
for name, f in [("2.Benchmark.py", f_bench), ("10.tau_wall_benchmark.py", f_tau)]:
    try:
        # (1) 完整 → float32 bit-identical (chunk_elems=64 強制多塊邊界)
        out32 = f(io.BytesIO(raw), n, np.dtype(">f8"), 8, np.float32, chunk_elems=64)
        assert np.array_equal(out32, src.astype(np.float32)), "float32 非 bit-identical"
        # (2) 完整 → float64 identity
        out64 = f(io.BytesIO(raw), n, np.dtype(">f8"), 8, np.float64, chunk_elems=64)
        assert np.array_equal(out64, src.astype(np.float64)), "float64 非 identity"
        # (3) 截斷 → ValueError
        raised = False
        try:
            f(io.BytesIO(raw[:-200]), n, np.dtype(">f8"), 8, np.float32, chunk_elems=64)
        except ValueError:
            raised = True
        assert raised, "★截斷未 raise ValueError(會靜默產錯圖)"
        print(f"  [PASS] {name}: float32 bit-identical + float64 identity + 截斷 raise ValueError")
    except AssertionError as e:
        print(f"  [FAIL] {name}: {e}"); fails += 1

print("=== 結果:", "✅ 全過 (兩檔 helper 一致)" if fails == 0 else f"❌ {fails} 失敗", "===")
sys.exit(1 if fails else 0)
