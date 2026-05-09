# Regrid Restart 修復實作計畫

> **目的**：修復 Re5600 GAMMA 變更後 regrid restart 發散問題，把 [interp_checkpoint.py](interp_checkpoint.py) 從 prototype 拉到生產等級。
>
> **狀態**：~40% 完成。f_neq 已 CE 重建、壁面 4 階 FD、PID 保留、accu_count 修正、wall clamp 已修。
>
> **剩餘 3 個核心 bug** + 對應 3 個 phase（**C → A → B**，依依賴與用戶優先級排）。
>
> **使用方式**：執行時逐 phase 對照本文件，每個 phase 完工後跑該 phase 的 acceptance criteria，全綠才進下一個 phase。
>
> **修訂版本**：v3.1（2026-05-09）— 在 v3 基礎上加：Phase C mapping 拆 cache（4 個場共用一次 cell search，效能 ~3.3× 改善）、Phase B max_component 敘述放寬（eta 不是錯）。詳見 §9 v3.1 條目。

---

## 0. 背景與已修狀態

### 已修 (P0 + P1 + Audit)

| Finding | 位置 | 狀態 |
|---|---|---|
| Chapman-Enskog f_neq 重建 | [interp_checkpoint.py:967](interp_checkpoint.py:967) | ✅ |
| 壁面 4 階單側 FD（底+頂） | [interp_checkpoint.py:967-983](interp_checkpoint.py:967) | ✅ |
| Wall velocity clamp 至 0 | [interp_checkpoint.py:1395-1416](interp_checkpoint.py:1395) | ✅ |
| Controller state 保留 — **僅** `Force_integral, error_prev, ctrl_initialized, gehrke_activated`；**FTT 與 accu_count 強制 reset 為 0**（見下兩行） | [interp_checkpoint.py:1530+](interp_checkpoint.py:1530) | ✅ |
| `accu_count=0` 強制（避免 stats binary 缺檔導致 [fileIO.h:748](../fileIO.h:748) abort） | [interp_checkpoint.py:1546+](interp_checkpoint.py:1546) | ✅ |
| `FTT=0` 強制（regrid 等於新 mesh fresh start） | [interp_checkpoint.py:1546+](interp_checkpoint.py:1546) | ✅ |
| Origin `FTT/accu_count` 寫入 metadata `interp_origin_*` 作為 provenance audit | metadata `interp_origin_ftt`, `interp_origin_accu_count` | ✅ |
| Docstring + CE-mode 不寫 fneq_scale | [interp_checkpoint.py:14-30](interp_checkpoint.py:14) | ✅ |

### 未修 (3 個核心 bug)

| # | Bug | 嚴重度 | Phase |
|---|---|---|---|
| 1 | `interpolate_comp_3d` 索引空間插值（u_new、rho_new） | **Critical** | **C — 先做** |
| 2 | 2 階中央差分 vs solver 6 階 Fornberg adaptive metric | Medium | A — 中做 |
| 3 | `dt_global=-1.0` 跳過 Phase 5 drift check | High | B — 後做 |

**Phase 順序理由**（v3 修正：C → A → B，原 v2 是 C → B → A）：
- **Phase C 是真正修發散的 patch**——索引空間 → 物理空間插值是 GAMMA 改變導致紊流結構錯置的核心。優先做。
- **Phase A 必須在 Phase B 之前**——B 寫入的 `dt_global` 需要跟 solver 自己算的 `dt_runtime` 在 `< 1e-6` 內一致才能通過 [fileIO.h:658](../fileIO.h:658) drift check。若 A 還沒做（metric 仍是 2 階），B 寫入的低精度 dt 反而會把現況「-1.0 silent skip」變成「noisy abort」，比修前更糟。所以 A 先把 metric 升到 6 階 adaptive，B 再寫入時 drift check 一次就過。
- **Phase B 是 fail-fast 守護收尾**——不修發散，只還原 dt drift check。最後做。
- **獨立性**：C 不依賴 A/B；A 不依賴 C/B；B 依賴 A 的 6 階 metric。

---

## 1. Phase C — 物理空間插值（**最優先**）

### 1.1 Bug 描述

**現況**：[interpolate_comp_3d](interp_checkpoint.py:750) 對每個新節點 `(j_n, k_n, i_n)` 用索引比例 `(j_n/(NY_n−1), k_n/(NZ_n−1), i_n/(NX_n−1))` 在 OLD 域線性插值。

**問題**：當 GAMMA 改變時，同一索引比例對應到不同物理 z 高度。例如：
- OLD GAMMA=2.0：k=10 → z=0.05
- NEW GAMMA=3.0：k=10 → z=0.02
- 索引插值把 z=0.05 的紊流結構塞到 z=0.02 → **物理錯置 → 發散**

### 1.2 數學公式

```
For each NEW node (j_n, k_n, i_n):

Step 1: Compute physical (x_n, y_n, z_n) from build_grid_xyz(NEW)

Step 2: Inverse mapping to OLD computational coords:
  - i_o_float = x_n / dx_old                       [direct, periodic spanwise]
  - (j*, k*): solve  y_old(j*, k*) = y_n
                     z_old(j*, k*) = z_n           [2D nonlinear, Newton]

Step 3: Trilinear interpolate using:
  - i fraction from i_o_float (let ghost handle wrap)
  - (ξ_j, ξ_k): bilinear inverse coords in cell (j_floor, k_floor)
```

### 1.3 為什麼 (j, k) 是耦合的

[build_grid_xyz](interp_checkpoint.py:595) 載入 Frohlich Tecplot grid：

```python
fro_x = coords[:, 0].reshape(cfg.NZ, cfg.NY)  # y_phys(j, k)
fro_y = coords[:, 1].reshape(cfg.NZ, cfg.NY)  # z_phys(j, k)
```

`y_phys` 跟 `z_phys` **都是 (j, k) 的函數**——Mode 2 Poisson 正交網格特性，wall-normal 線在 hill 附近會彎。需要 2D inverse mapping。

### 1.4 演算法選擇：numpy brute search + Newton 2×2

| 步驟 | 方法 | 理由 |
|---|---|---|
| Cell 候選篩選 | Bounding-box prefilter（向量化） | 無 scipy 依賴；filter 後候選 ~1% |
| 細部 cell 確認 | Newton 2×2 解 bilinear inverse | quadratic convergence；3-5 iter 收斂 |
| 退化 cell fallback | 切兩個三角形，barycentric coordinate | Newton 失敗（det J ≈ 0）時的保險網 |
| 找不到 cell | `ValueError`（fail-fast） | OLD domain 外應該不會發生 |

### 1.5 實作項目

**檔案**：`restart_tools/interp_checkpoint.py`

#### C1. `build_old_cell_search_index` 函式

簽名：`build_old_cell_search_index(y_old, z_old) -> (bbox_y_min, bbox_y_max, bbox_z_min, bbox_z_max)`

行為：對 OLD 內部 (NY-1) × (NZ-1) 個 cell 預計算 axis-aligned bounding box（4 角 min/max）。

```python
def build_old_cell_search_index(y_old, z_old):
    """Per-cell bounding boxes for fast point-in-cell prefilter.

    y_old, z_old: shape (NY, NZ) interior arrays (no ghost).
    Returns 4 arrays of shape (NY-1, NZ-1).
    """
    cy = np.stack([y_old[:-1, :-1], y_old[1:, :-1], y_old[:-1, 1:], y_old[1:, 1:]], axis=-1)
    cz = np.stack([z_old[:-1, :-1], z_old[1:, :-1], z_old[:-1, 1:], z_old[1:, 1:]], axis=-1)
    return cy.min(-1), cy.max(-1), cz.min(-1), cz.max(-1)
```

#### C2. `bilinear_inverse_newton` 函式

Newton 2×2 解 `(ξ, η) ∈ [0,1]²`。

```python
class _DegenerateCellError(Exception):
    """Bilinear inverse failed (cell ill-conditioned or non-convex)."""

def bilinear_inverse_newton(y_n, z_n, y_corners, z_corners,
                            max_iter=8, tol=1e-12):
    """Newton 2×2 solve for (xi, eta) ∈ [0,1]² in bilinear cell.

    Bilinear:
      y(xi, eta) = (1-xi)(1-eta)·y_a + xi(1-eta)·y_b
                 + (1-xi)·eta·y_c + xi·eta·y_d
    Same for z.
    Corner indexing: a=(0,0), b=(1,0), c=(0,1), d=(1,1) in (xi, eta).

    Returns (xi, eta). Raises _DegenerateCellError if Jacobian collapses.
    """
    y_a, y_b, y_c, y_d = y_corners
    z_a, z_b, z_c, z_d = z_corners
    xi, eta = 0.5, 0.5
    for _ in range(max_iter):
        one_xi  = 1.0 - xi
        one_et  = 1.0 - eta
        y_int = one_xi*one_et*y_a + xi*one_et*y_b + one_xi*eta*y_c + xi*eta*y_d
        z_int = one_xi*one_et*z_a + xi*one_et*z_b + one_xi*eta*z_c + xi*eta*z_d
        ry, rz = y_int - y_n, z_int - z_n
        if abs(ry) < tol and abs(rz) < tol:
            return xi, eta
        dy_dxi  = -one_et*y_a + one_et*y_b - eta*y_c + eta*y_d
        dy_deta = -one_xi*y_a - xi*y_b + one_xi*y_c + xi*y_d
        dz_dxi  = -one_et*z_a + one_et*z_b - eta*z_c + eta*z_d
        dz_deta = -one_xi*z_a - xi*z_b + one_xi*z_c + xi*z_d
        det = dy_dxi*dz_deta - dy_deta*dz_dxi
        if abs(det) < 1e-30:
            raise _DegenerateCellError()
        inv = 1.0 / det
        xi  -= ( dz_deta*ry - dy_deta*rz) * inv
        eta -= (-dz_dxi *ry + dy_dxi *rz) * inv
    raise _DegenerateCellError()
```

#### C3. `bilinear_inverse_triangle_fallback` 函式

Newton 失敗時用：把 cell 切成兩個三角形（abd 與 acd），對每個三角形解 barycentric。任一個 barycentric 全在 `[0, 1]³` 內就接受，再轉回 `(ξ, η)`。

```python
def bilinear_inverse_triangle_fallback(y_n, z_n, y_corners, z_corners, eps=1e-9):
    """Split cell into 2 triangles; solve barycentric. Returns (xi, eta) or raises.

    Triangle 1: a=(0,0), b=(1,0), d=(1,1)  → covers xi >= eta region
    Triangle 2: a=(0,0), c=(0,1), d=(1,1)  → covers eta >= xi region
    """
    y_a, y_b, y_c, y_d = y_corners
    z_a, z_b, z_c, z_d = z_corners

    def _solve_tri(y0, z0, y1, z1, y2, z2):
        # Barycentric (w0, w1, w2) s.t. w0+w1+w2=1, w0·p0 + w1·p1 + w2·p2 = (y_n, z_n)
        det = (y1-y0)*(z2-z0) - (z1-z0)*(y2-y0)
        if abs(det) < 1e-30:
            return None
        w1 = ((y_n-y0)*(z2-z0) - (z_n-z0)*(y2-y0)) / det
        w2 = ((y1-y0)*(z_n-z0) - (z1-z0)*(y_n-y0)) / det
        w0 = 1.0 - w1 - w2
        return w0, w1, w2

    # Triangle 1: a, b, d  →  xi=w1+w2, eta=w2
    w = _solve_tri(y_a, z_a, y_b, z_b, y_d, z_d)
    if w is not None and all(-eps <= wi <= 1 + eps for wi in w):
        return np.clip(w[1] + w[2], 0, 1), np.clip(w[2], 0, 1)

    # Triangle 2: a, c, d  →  xi=w2, eta=w1+w2
    w = _solve_tri(y_a, z_a, y_c, z_c, y_d, z_d)
    if w is not None and all(-eps <= wi <= 1 + eps for wi in w):
        return np.clip(w[2], 0, 1), np.clip(w[1] + w[2], 0, 1)

    raise _DegenerateCellError()
```

#### C4. `find_containing_cell_2d` 函式

對單一 (y_n, z_n) 找 OLD cell：先 bbox filter，候選 cell 跑 Newton；**Newton 失敗或收斂到 cell 外都試 triangle fallback**（reviewer v3 Finding #4）。

> **關鍵邏輯**：v2 寫成「Newton raise → triangle」，但 Newton 可能 converged 但回傳 `xi=-0.05`（出界），這時 v2 會**直接跳下個 candidate cell，連 triangle 都不試**。實際上 Newton 因 cell 扭曲微微外漂時，triangle barycentric 反而能解出 valid 結果。v3 邏輯：**Newton fail 或 out-of-bounds 都當作這個 cell 的 Newton 失敗，繼續走 triangle fallback；triangle 也失敗才跳下個 candidate**。

```python
def find_containing_cell_2d(y_n, z_n, y_old, z_old, bboxes, eps=1e-9):
    """Locate OLD cell containing (y_n, z_n). Returns (j*, k*, xi, eta).

    Per-candidate cell strategy:
      1. Newton 2x2; accept if converged AND within [0,1]² (with eps tolerance).
      2. If Newton failed OR converged out-of-bounds → triangle fallback.
      3. If both failed → next candidate.
      4. All candidates exhausted → ValueError (point outside OLD domain).
    """
    bbox_y_min, bbox_y_max, bbox_z_min, bbox_z_max = bboxes
    candidates = ((bbox_y_min - eps <= y_n) & (y_n <= bbox_y_max + eps) &
                  (bbox_z_min - eps <= z_n) & (z_n <= bbox_z_max + eps))
    cand_jk = np.argwhere(candidates)
    if len(cand_jk) == 0:
        raise ValueError(f'No OLD cell brackets ({y_n:.6e}, {z_n:.6e})')

    def _in_bounds(xi, eta):
        return -eps <= xi <= 1+eps and -eps <= eta <= 1+eps

    for j, k in cand_jk:
        y_corners = (y_old[j, k],   y_old[j+1, k],
                     y_old[j, k+1], y_old[j+1, k+1])
        z_corners = (z_old[j, k],   z_old[j+1, k],
                     z_old[j, k+1], z_old[j+1, k+1])

        xi, eta = None, None  # accepted result

        # Step 1: Newton — accept only if converged AND in-bounds
        try:
            xi_n, eta_n = bilinear_inverse_newton(y_n, z_n, y_corners, z_corners)
            if _in_bounds(xi_n, eta_n):
                xi, eta = xi_n, eta_n
            # Else: Newton converged but out-of-bounds — fall through to triangle
        except _DegenerateCellError:
            pass

        # Step 2: Triangle fallback (Newton failed OR Newton out-of-bounds)
        if xi is None:
            try:
                xi_t, eta_t = bilinear_inverse_triangle_fallback(y_n, z_n, y_corners, z_corners)
                if _in_bounds(xi_t, eta_t):
                    xi, eta = xi_t, eta_t
            except _DegenerateCellError:
                pass

        if xi is not None:
            return int(j), int(k), float(np.clip(xi, 0, 1)), float(np.clip(eta, 0, 1))

    raise ValueError(f'Point ({y_n:.6e}, {z_n:.6e}) not in any OLD cell after Newton+triangle')
```

#### C5a. `precompute_phys_mapping_2d` 函式（**reusable cache**，v3.1 新增）

> **設計變更（reviewer v3.1 #1）**：原 `interpolate_phys_3d` 把「2D cell search + i 方向均勻 mapping」混在一起，每呼叫一次就重做最貴的 cell search。主流程要對 `rho, ux, uy, uz` 呼叫 4 次 → cell search 重複 4 倍工。改成先預計算一次 mapping，4 個場共用。

```python
class PhysMapping2D:
    """Precomputed mapping from NEW (j_n, k_n) to OLD cell + bilinear weights.

    Built once per OLD/NEW grid pair; shared across all field interpolations
    (rho, ux, uy, uz). Cell search is the dominant cost; this avoids redoing it.
    """
    __slots__ = ('jstar', 'kstar', 'xistar', 'etastar',
                 'i_o_arr', 'xi_i_arr',
                 'cfg_old', 'cfg_new')

    def __init__(self, jstar, kstar, xistar, etastar, i_o_arr, xi_i_arr, cfg_old, cfg_new):
        self.jstar   = jstar    # (NY_new, NZ_new) int32 — OLD cell j index
        self.kstar   = kstar    # (NY_new, NZ_new) int32 — OLD cell k index
        self.xistar  = xistar   # (NY_new, NZ_new) float64 — bilinear ξ ∈ [0,1]
        self.etastar = etastar  # (NY_new, NZ_new) float64 — bilinear η ∈ [0,1]
        self.i_o_arr = i_o_arr  # (NX_new,) int64 — OLD i floor index per NEW i
        self.xi_i_arr = xi_i_arr  # (NX_new,) float64 — i fraction
        self.cfg_old = cfg_old
        self.cfg_new = cfg_new


def precompute_phys_mapping_2d(y2d_old, z2d_old, y2d_new, z2d_new, cfg_old, cfg_new):
    """Build PhysMapping2D once for a given OLD/NEW grid pair.

    Cell search (find_containing_cell_2d) is O(NY_new × NZ_new × candidate_count).
    Reusing this across rho/ux/uy/uz saves 4× cost on the dominant operation.

    Returns PhysMapping2D ready for interpolate_phys_3d_with_mapping().
    """
    y_int_old = y2d_old[BFR:BFR+cfg_old.NY, BFR:BFR+cfg_old.NZ]
    z_int_old = z2d_old[BFR:BFR+cfg_old.NY, BFR:BFR+cfg_old.NZ]
    bboxes = build_old_cell_search_index(y_int_old, z_int_old)

    # 2D cell search (the expensive part)
    jstar = np.empty((cfg_new.NY, cfg_new.NZ), dtype=np.int32)
    kstar = np.empty((cfg_new.NY, cfg_new.NZ), dtype=np.int32)
    xistar = np.empty((cfg_new.NY, cfg_new.NZ), dtype=np.float64)
    etastar = np.empty((cfg_new.NY, cfg_new.NZ), dtype=np.float64)
    for j_n in range(cfg_new.NY):
        for k_n in range(cfg_new.NZ):
            y_n = y2d_new[BFR + j_n, BFR + k_n]
            z_n = z2d_new[BFR + j_n, BFR + k_n]
            j_o, k_o, xi, eta = find_containing_cell_2d(y_n, z_n, y_int_old, z_int_old, bboxes)
            jstar[j_n, k_n] = j_o
            kstar[j_n, k_n] = k_o
            xistar[j_n, k_n] = xi
            etastar[j_n, k_n] = eta

    # i mapping: uniform spanwise; ghost-wrap handles periodic boundary (no clamp)
    dx_old = LX / (cfg_old.NX - 1)
    dx_new = LX / (cfg_new.NX - 1)
    i_o_float_arr = (np.arange(cfg_new.NX, dtype=np.float64) * dx_new) / dx_old
    i_o_arr = np.floor(i_o_float_arr).astype(np.int64)
    xi_i_arr = i_o_float_arr - i_o_arr

    print('      Phys mapping cache built: NY_new×NZ_new = {}×{} cells located'.format(
        cfg_new.NY, cfg_new.NZ))
    return PhysMapping2D(jstar, kstar, xistar, etastar, i_o_arr, xi_i_arr, cfg_old, cfg_new)
```

#### C5b. `interpolate_phys_3d_with_mapping` 函式（**uses cache**）

```python
def interpolate_phys_3d_with_mapping(field_old, mapping):
    """Interpolate one (NY6_old, NZ6_old, NX6_old) field using precomputed mapping.

    Reuses PhysMapping2D — no cell search, just trilinear blend with cached weights.
    """
    cfg_old, cfg_new = mapping.cfg_old, mapping.cfg_new
    field_new = np.zeros((cfg_new.NY6, cfg_new.NZ6, cfg_new.NX6), dtype=np.float64)

    i_o_arr  = mapping.i_o_arr
    xi_i_arr = mapping.xi_i_arr
    ib0 = BFR + i_o_arr        # shape (NX_new,)
    ib1 = BFR + i_o_arr + 1    # ghost wrap handles periodic last-point case

    for j_n in range(cfg_new.NY):
        for k_n in range(cfg_new.NZ):
            j_o = int(mapping.jstar[j_n, k_n])
            k_o = int(mapping.kstar[j_n, k_n])
            xi  = float(mapping.xistar[j_n, k_n])
            eta = float(mapping.etastar[j_n, k_n])

            w_a = (1-xi)*(1-eta)
            w_b = xi    *(1-eta)
            w_c = (1-xi)*eta
            w_d = xi    *eta

            jb, kb = BFR + j_o, BFR + k_o
            a0 = field_old[jb,   kb,   ib0]; a1 = field_old[jb,   kb,   ib1]
            b0 = field_old[jb+1, kb,   ib0]; b1 = field_old[jb+1, kb,   ib1]
            c0 = field_old[jb,   kb+1, ib0]; c1 = field_old[jb,   kb+1, ib1]
            d0 = field_old[jb+1, kb+1, ib0]; d1 = field_old[jb+1, kb+1, ib1]

            v0 = w_a*a0 + w_b*b0 + w_c*c0 + w_d*d0
            v1 = w_a*a1 + w_b*b1 + w_c*c1 + w_d*d1
            field_new[BFR + j_n, BFR + k_n, BFR:BFR + cfg_new.NX] = (1-xi_i_arr)*v0 + xi_i_arr*v1

    fill_ghost(field_new, cfg_new)
    return field_new
```

#### C5c. `interpolate_phys_3d` 便利包裝（**單次呼叫用**）

```python
def interpolate_phys_3d(field_old, cfg_old, cfg_new,
                        y2d_old, z2d_old, y2d_new, z2d_new):
    """One-shot convenience wrapper: build mapping + interp single field.

    For multi-field workflows (rho, ux, uy, uz), prefer:
        mapping = precompute_phys_mapping_2d(...)
        rho_new = interpolate_phys_3d_with_mapping(rho, mapping)
        ux_new  = interpolate_phys_3d_with_mapping(ux,  mapping)
        ...
    to avoid redundant cell search.
    """
    mapping = precompute_phys_mapping_2d(y2d_old, z2d_old, y2d_new, z2d_new, cfg_old, cfg_new)
    return interpolate_phys_3d_with_mapping(field_old, mapping)
```

**i 方向 ghost-wrap 修正**（reviewer v2 Finding #4）：
- 不對 `i_o` 做 clamp
- field_old 的 ghost 已被 [fill_ghost](interp_checkpoint.py:789) 用 spanwise periodic wrap 填好
- 當 `x_n = LX` 時 `i_o_float = NX_old - 1`，`i_o = NX_old - 1`，`xi_i = 0`
- 讀 `field_old[BFR + NX_old - 1]`（最後一個 interior）權重 1，`field_old[BFR + NX_old]`（ghost wrap = field_old[BFR + 1]）權重 0 → 正確

#### C6. CLI flag

```python
p.add_argument('--interp-mode', choices=['comp', 'phys'], default='phys',
               help='Macro field interpolation mode. "phys" = physical-space '
                    '(default; correct for GAMMA changes). "comp" = legacy '
                    'computational-space remap.')
```

#### C7. 主流程整合（**使用 mapping cache，cell search 只跑一次**）

[interp_checkpoint.py:1296-1305](interp_checkpoint.py:1296)：

```python
# OLD grid coords needed for phys interp
_, y2d_old, z2d_old = build_grid_xyz(OLD)

if args.interp_mode == 'phys':
    print('[5/8] Interpolating macros (rho, ux, uy, uz) to NEW grid in PHYSICAL space')
    # Build mapping cache once; reuse for all 4 fields
    t = time.time()
    mapping = precompute_phys_mapping_2d(y2d_old, z2d_old, y2d_new, z2d_new, OLD, NEW)
    print('      mapping cache build:  {:.1f}s'.format(time.time() - t))
    t = time.time(); rho_new = interpolate_phys_3d_with_mapping(rho_g, mapping)
    print('      rho:  {:.1f}s'.format(time.time() - t))
    t = time.time(); ux_new  = interpolate_phys_3d_with_mapping(ux_g,  mapping)
    print('      ux:   {:.1f}s'.format(time.time() - t))
    t = time.time(); uy_new  = interpolate_phys_3d_with_mapping(uy_g,  mapping)
    print('      uy:   {:.1f}s'.format(time.time() - t))
    t = time.time(); uz_new  = interpolate_phys_3d_with_mapping(uz_g,  mapping)
    print('      uz:   {:.1f}s'.format(time.time() - t))
else:
    print('[5/8] Interpolating macros in COMPUTATIONAL space (legacy)')
    rho_new = interpolate_comp_3d(rho_g, OLD, NEW)
    ux_new  = interpolate_comp_3d(ux_g,  OLD, NEW)
    uy_new  = interpolate_comp_3d(uy_g,  OLD, NEW)
    uz_new  = interpolate_comp_3d(uz_g,  OLD, NEW)
```

> **效能影響**：cell search 是 Phase C 最貴的操作（~80% 總時間）。沒 cache 時 4 次 interp 重做 4 次 → 總時間 ~T_search·4 + T_blend·4。有 cache 後 → ~T_search + T_blend·4。對 NY×NZ ≈ 200×200，這把總時間從 ~5 分鐘降到 ~1.5 分鐘。

### 1.6 Acceptance Criteria（v1 — Reviewer 簡化版）

僅 3 個強制測試。

> **重要前置（reviewer v3 Finding #3）**：unit test 構造 synthetic `field_old` 時，**必須先呼叫 `fill_ghost(field_old, OLD)` 再呼叫 `interpolate_phys_3d`**。原因：interpolate_phys_3d 內部 i-loop 會讀 `field_old[BFR + NX_old]`（spanwise ghost）支援週期 wrap；synthetic field 只賦值 interior 時 ghost 區仍是 0，會導致邊界節點插值錯誤。真 checkpoint 已被 solver 寫入 ghost，無此問題；synthetic test **一定要明寫**這步。

#### Test 1：Identity（OLD = NEW，bit-exact 還原）
- 同一 grid 跑 phys interp
- **Setup**：建構 synthetic field_old → **`fill_ghost(field_old, OLD)`** → `interpolate_phys_3d(...)`
- 預期：`max |field_new[interior] - field_old[interior]| < 1e-12`
- 失敗代表 inverse mapping 或 trilinear 算錯

#### Test 2：Linear analytic（不同 GAMMA 下精確還原）
- 測試函數：`f(x, y, z) = 10·y + 100·z + 0.5·x`
- OLD 跟 NEW 用不同 GAMMA（例如 OLD=2.0、NEW=3.0）
- **Setup**：
  ```python
  # Build synthetic field on OLD interior
  field_old = np.zeros((OLD.NY6, OLD.NZ6, OLD.NX6))
  for j_o in range(OLD.NY):
      for k_o in range(OLD.NZ):
          for i_o in range(OLD.NX):
              x_o = i_o * (LX/(OLD.NX-1))
              y_o = y2d_old[BFR+j_o, BFR+k_o]
              z_o = z2d_old[BFR+j_o, BFR+k_o]
              field_old[BFR+j_o, BFR+k_o, BFR+i_o] = 10*y_o + 100*z_o + 0.5*x_o
  fill_ghost(field_old, OLD)  # CRITICAL — without this, edge interp reads 0
  field_new = interpolate_phys_3d(field_old, OLD, NEW, y2d_old, z2d_old, y2d_new, z2d_new)
  ```
- phys interp 到 NEW grid → 應該等於 `f(x_new, y_new, z_new)` 在 FP 精度內
- 預期：`max |field_new[interior] - f(x_new, y_new, z_new)| < 1e-10`
- 失敗代表 bilinear inverse 不精確或 mapping 錯

#### Test 3：Real checkpoint positivity / conservation
- 拿真 OLD checkpoint regrid 到 NEW grid（real checkpoint 已含 ghost，不需手動 fill_ghost）
- 預期：
  - `min(rho_new) > 0`，`max(rho_new) < 2·max(rho_old)`（無 NaN/Inf、無爆衝）
  - `mean(rho_new) - mean(rho_old) < 1e-4`（mean drift 容忍）
  - `min(rho_new), max(rho_new)` 在 `min(rho_old), max(rho_old)` ±0.1% 內（range preservation）
  - `max |Σ_q f_q^new - rho_new| < 1e-10`（既有 sum(f)=rho 自洽 check 不變）
  - **不要求**嚴格 mass conservation（trilinear 是 pointwise，不是 finite-volume remap，本來就不保守）

### 1.7 預估規模

| 項目 | 行數 |
|---|---|
| build_old_cell_search_index | ~12 |
| bilinear_inverse_newton + DegenerateCellError | ~40 |
| bilinear_inverse_triangle_fallback | ~30 |
| find_containing_cell_2d | ~45 |
| PhysMapping2D + precompute_phys_mapping_2d | ~50 |
| interpolate_phys_3d_with_mapping | ~35 |
| interpolate_phys_3d (一次性 wrapper) | ~10 |
| CLI flag + 主流程整合 | ~30 |
| Test 1/2/3 | ~80 |
| **小計** | **~330** |

---

## 2. Phase A — 6 階 Fornberg Mixed-Stencil Metric（中做）

### 2.1 Bug 描述

**現況**：[compute_inverse_metric_2d](interp_checkpoint.py:881) 對 `y_2d, z_2d` 計算 `y_j, y_k, z_j, z_k` 用 2 階中央差分。

**問題**：solver 用 6 階 Fornberg（[metric_terms.h:71-95](../gilbm/metric_terms.h:71) k 方向 adaptive skew、[metric_terms.h:100-109](../gilbm/metric_terms.h:100) j 方向純 central）。同一個物理位置，2 階 vs 6 階算出的 `ζ_y, ζ_z, ξ_y, ξ_z` 在 hill crest 與近壁拉伸區會差到 ~O(h⁴)，影響：
- CE 重建 f_neq 的精度（不致發散，但跟 solver 不自洽）
- Phase B 的 `dt_global` 跟 solver 對齊精度（drift check 1e-6 門檻）

### 2.2 Solver 真實作法（**j 方向 central、k 方向 adaptive skew，不對稱**）

**關鍵修正（reviewer v2 Finding #2）**：v2 計畫寫 j/k 都用 `fd6_axis_adaptive` **錯誤**。Solver 兩個方向處理不一樣，因為**ghost 的有效性不同**：

| 方向 | Ghost 性質 | Solver 作法 |
|---|---|---|
| **j（streamwise）** | 週期（[build_grid_xyz:672-687](interp_checkpoint.py:672) 用 ±LY 週期填） — **ghost 是真實 fluid 節點的拷貝，6 階精度有效** | 純 6 階 central（[metric_terms.h:100-109](../gilbm/metric_terms.h:100) `FD6_j_central`），讀 j±3 ghost |
| **k（wall-normal）** | 線性外推（[build_grid_xyz:656-670](interp_checkpoint.py:656) `y_2d[j, 2] = 2·y_2d[j, 3] - y_2d[j, 4]`） — **ghost 只有 2 階精度，6 階 stencil 讀進去會退化** | 6 階 adaptive skew（[metric_terms.h:71-95](../gilbm/metric_terms.h:71)），**不讀 ghost**，stencil 全部落在 fluid 節點內 |

**Solver j-central 完整程式碼**（[metric_terms.h:100-109](../gilbm/metric_terms.h:100)）：

```c
static inline double FD6_j_central(const double *field, int j, int k, int NZ6_local)
{
    return ( -field[(j-3)*NZ6_local + k]
        + 9.0*field[(j-2)*NZ6_local + k]
       - 45.0*field[(j-1)*NZ6_local + k]
       + 45.0*field[(j+1)*NZ6_local + k]
        - 9.0*field[(j+2)*NZ6_local + k]
            + field[(j+3)*NZ6_local + k] ) / 60.0;
}
```

**Solver k-adaptive 完整邏輯**（[metric_terms.h:71-95](../gilbm/metric_terms.h:71)）：

```c
} else if (k >= k_lo && k <= k_hi) {
    // 物理域: 六階 Fornberg 自適應偏斜
    int s = k - 3;                  // 預設 stencil 起點
    if (s < k_lo)     s = k_lo;     // 太靠近底部 → 強制起點
    if (s > k_hi - 6) s = k_hi - 6; // 太靠近頂部 → 強制起點
    int p = k - s;                  // 評估點在 stencil 中的偏移
    deriv = 0.0;
    for (int m = 0; m < 7; m++)
        deriv += FD6_COEFF[p][m] * field[base_j + s + m];
    deriv /= 60.0;
}
```

**邏輯（k 方向 adaptive skew）**：
- 對 fluid 節點 `k`，目標讓 stencil 7 個點都落在物理域 `[k_lo, k_hi]` 內
- 預設 stencil 起點 `s = k - 3`（評估點在中間，p=3 → 純 central）
- 若 `s < k_lo` → 推到 `k_lo`，`p = k - k_lo`（forward 偏斜，p < 3）
- 若 `s + 6 > k_hi` → 拉回 `k_hi - 6`，`p = k - (k_hi - 6)`（backward 偏斜，p > 3）
- `FD6_COEFF[p]` 第 p 列就是該偏斜 stencil 的係數

### 2.3 實作項目

#### A1. FD6_COEFF 係數表

```python
# 7-point Fornberg coefficients, 1st derivative, unit spacing.
# FD6_COEFF[p, m] : evaluation point at offset p in stencil window [s, s+6]
# Mirror gilbm/metric_terms.h:34-42. Divisor 60 absorbed into table.
FD6_COEFF = np.array([
    [-147,  360, -450,  400, -225,   72,  -10],   # p=0 forward
    [ -10,  -77,  150, -100,   50,  -15,    2],   # p=1
    [   2,  -24,  -35,   80,  -30,    8,   -1],   # p=2
    [  -1,    9,  -45,    0,   45,   -9,    1],   # p=3 central (6th-order)
    [   1,   -8,   30,  -80,   35,   24,   -2],   # p=4
    [  -2,   15,  -50,  100, -150,   77,   10],   # p=5
    [  10,  -72,  225, -400,  450, -360,  147],   # p=6 backward
], dtype=np.float64) / 60.0
```

#### A2a. `fd6_axis_central` 函式（用於 j 方向，讀週期 ghost）

```python
def fd6_axis_central(arr, k_lo, k_hi, axis):
    """6th-order pure central FD using p=3 row of Fornberg table.

    For each evaluation point k in [k_lo, k_hi]:
      deriv[k] = Σ FD6_COEFF[3, m] · arr[k + m - 3]   for m in [0, 6]
              = (-arr[k-3] + 9·arr[k-2] - 45·arr[k-1] + 45·arr[k+1] - 9·arr[k+2] + arr[k+3]) / 60

    REQUIRES: k_lo - 3 >= 0 and k_hi + 3 < arr.shape[axis]
              (i.e., 3 ghost layers on each side filled with valid 6th-order data,
               which holds for j-direction periodic ghost from build_grid_xyz).

    Mirrors gilbm/metric_terms.h:100-109 FD6_j_central exactly.
    """
    if axis == 0:
        arr = np.moveaxis(arr, 0, -1)
    deriv = np.zeros_like(arr)
    coef = FD6_COEFF[3]  # p=3 central
    for k in range(k_lo, k_hi + 1):
        for m in range(7):
            deriv[..., k] += coef[m] * arr[..., k + m - 3]
    if axis == 0:
        deriv = np.moveaxis(deriv, -1, 0)
    return deriv
```

#### A2b. `fd6_axis_adaptive` 函式（用於 k 方向，避開不可靠 ghost）

```python
def fd6_axis_adaptive(arr, k_lo, k_hi, axis):
    """6th-order Fornberg adaptive-skew derivative along one axis.

    For each evaluation point k in [k_lo, k_hi]:
      s = clip(k - 3, k_lo, k_hi - 6)        # stencil start, all 7 pts in [k_lo, k_hi]
      p = k - s                              # eval point's offset within stencil
      deriv[k] = Σ FD6_COEFF[p, m] · arr[s + m]   for m in [0, 6]

    Outside [k_lo, k_hi]: returns 0 (caller should not use those values).
    Stencil never reads outside [k_lo, k_hi] → safe even when ghosts are unreliable
    (e.g., k-direction with linear extrapolated ghosts from build_grid_xyz).

    Mirrors gilbm/metric_terms.h:71-95 k-direction adaptive Fornberg exactly.
    """
    if axis == 0:
        arr = np.moveaxis(arr, 0, -1)
    deriv = np.zeros_like(arr)
    for k in range(k_lo, k_hi + 1):
        s = max(k_lo, min(k_hi - 6, k - 3))
        p = k - s
        for m in range(7):
            deriv[..., k] += FD6_COEFF[p, m] * arr[..., s + m]
    if axis == 0:
        deriv = np.moveaxis(deriv, -1, 0)
    return deriv
```

(實際實作可以向量化掉 `for k`，但先寫清楚邏輯。)

#### A3. `compute_inverse_metric_2d_fornberg` 函式（**j-central + k-adaptive**）

```python
def compute_inverse_metric_2d_fornberg(y_2d, z_2d):
    """6th-order Fornberg version of compute_inverse_metric_2d.

    Mirrors solver: j-direction pure central (periodic ghost OK),
                    k-direction adaptive skew (wall ghost unreliable).

    j-direction range: j_lo=BFR, j_hi=NY6-1-BFR (fluid nodes; ghost reads OK)
    k-direction range: k_lo=BFR, k_hi=NZ6-1-BFR (fluid nodes; no ghost reads)
    """
    NY6, NZ6 = y_2d.shape
    j_lo, j_hi = BFR, NY6 - 1 - BFR
    k_lo, k_hi = BFR, NZ6 - 1 - BFR

    # j-direction: periodic ghost from build_grid_xyz is 6th-order valid
    # → pure central (mirrors metric_terms.h:100-109 FD6_j_central)
    y_j = fd6_axis_central(y_2d, j_lo, j_hi, axis=0)
    z_j = fd6_axis_central(z_2d, j_lo, j_hi, axis=0)

    # k-direction: wall ghost is linear extrap (only 2nd-order valid)
    # → adaptive skew, never reads ghost (mirrors metric_terms.h:71-95)
    y_k = fd6_axis_adaptive(y_2d, k_lo, k_hi, axis=1)
    z_k = fd6_axis_adaptive(z_2d, k_lo, k_hi, axis=1)

    det = y_j * z_k - y_k * z_j
    eps = 1e-30
    sing = int(np.sum(np.abs(det) <= eps))
    if sing > 0:
        print('      WARN: Jacobian near-singular at {} grid points'.format(sing))
    inv_det = 1.0 / np.where(np.abs(det) > eps, det, eps)

    dj_dy =  z_k * inv_det
    dj_dz = -y_k * inv_det
    dk_dy = -z_j * inv_det
    dk_dz =  y_j * inv_det
    return dj_dy, dj_dz, dk_dy, dk_dz
```

#### A4. CLI flag

```python
p.add_argument('--metric-order', type=int, choices=[2, 6], default=6,
               help='Order of FD for inverse metric. 6 mirrors solver '
                    '(j-central + k-adaptive Fornberg, gilbm/metric_terms.h). '
                    '2 is legacy.')
```

#### A5. CE branch 整合

[interp_checkpoint.py:1396](interp_checkpoint.py:1396) 改：

```python
if args.metric_order == 6:
    dj_dy, dj_dz, dk_dy, dk_dz = compute_inverse_metric_2d_fornberg(y2d_new, z2d_new)
else:
    dj_dy, dj_dz, dk_dy, dk_dz = compute_inverse_metric_2d(y2d_new, z2d_new)
print('      metric order = {}'.format(args.metric_order))
```

### 2.4 Acceptance Criteria

- [ ] `python -m py_compile` pass
- [ ] **Convergence test**：對解析函數 `y(j,k) = sin(j·dy)·cos(k·dz)`、`z(j,k) = exp(-j·dy)·k·dz`，6 階版本誤差比 2 階小至少 4 個量級
- [ ] **j-direction periodic test**：對 `y(j,k) = sin(2π·j/NY)`，j_lo=BFR、j_hi=NY6-1-BFR，6 階 central 使用週期 ghost 應該誤差 < 1e-10
- [ ] **k-direction adaptive test**：對 `y(j,k) = (k-BFR)⁵`（5 次多項式），k 方向 6 階 adaptive 應該精確還原（5 階以下多項式 6 階 FD 應 bit-exact）
- [ ] **CE conservation regression**：`max |Σ f_neq| < 1e-22` 不變
- [ ] **準備給 Phase B**：6 階 metric 算 dt_global，OLD=NEW self-consistency 預期 < 1e-8（為 Phase B drift check < 1e-6 留 margin）
- [ ] CLI `--metric-order 2` 仍可走 legacy 路徑

### 2.5 預估規模

| 項目 | 行數 |
|---|---|
| FD6_COEFF + fd6_axis_central + fd6_axis_adaptive | ~70 |
| compute_inverse_metric_2d_fornberg | ~35 |
| Convergence + periodic + polynomial unit tests | ~60 |
| CLI flag + 整合 | ~10 |
| **小計** | **~175** |

---

## 3. Phase B — Real `dt_global`（最後）

### 3.1 Bug 描述

**現況**：[interp_checkpoint.py:1490](interp_checkpoint.py:1490) 寫 `'dt_global': '-1.0'`，[fileIO.h:650](../fileIO.h:650) 「無 dt_global 欄位 → 跳過漂移檢查」。**solver 唯一的 dt 一致性 fail-fast 守護被自己關掉**。

### 3.2 數學公式（**含 c_eta**）

來自 [gilbm/precompute.h:78-115](../gilbm/precompute.h:78) 的真正 solver 邏輯：

```
dt_global = CFL_λ / max|c~|

max|c~| 是以下三者中最大值：
  c~_eta = 1 / dx                       (spanwise, scalar — uniform)

對 D3Q19 dirs α ∈ [3, 18] 與所有內部 (j, k):
  c~_ξ(α, j, k)   = ξ_y · e_y[α] + ξ_z · e_z[α]
  c~_ζ(α, j, k)   = ζ_y · e_y[α] + ζ_z · e_z[α]

CFL_λ = 0.5 (typical, configurable)
```

> **重要修正（reviewer v2 Finding #3）**：[grid_zeta_tool.py:407-412](grid_zeta_tool.py:407) 的版本只有 c~_ξ 跟 c~_ζ，**漏掉 c~_eta = 1/dx**。那個函式是 2D 平面分析工具，跟 solver 不一致。本 phase 必須照 [precompute.h:84](../gilbm/precompute.h:84) 加入 c_eta。

對 Re5600 case：
- `dx ≈ 4.5/256 ≈ 0.0176` → `c~_eta ≈ 57`
- 近壁 `c~_ζ` 可達 O(1000)（壁拉伸主導 dt）
- 此 case 加 c~_eta 不會改 dt
- 但對 spanwise 細 / 壁向粗的網格 **會反轉，dt 被 c~_eta 限制**
- 為跟 solver 對齊到 `< 1e-6 drift`，**必須**加 c~_eta

### 3.3 與 fileIO.h:658 的關係（**Phase A 完成後 drift check 應一次過關**）

solver 啟動時做 [Phase 5 drift check](../fileIO.h:658)：

```c
if (fabs(dt_runtime - dt_saved) > 1e-6) MPI_Abort(...);
```

**Phase B 寫入的 `dt_saved` 必須跟 solver 自己算的 `dt_runtime` 差 < 1e-6**。

| 前置條件 | 結果 |
|---|---|
| **Phase A 已完成（v3 順序）**：6 階 Fornberg metric 跟 solver 對齊 | drift 預期 < 1e-6 → **drift check 一次通過** |
| ~~Phase A 未完成（v2 順序）~~ | ~~2 階 metric 誤差 → drift 可能 > 1e-6 → fail-fast~~ |

→ v3 順序 C → A → B：B 開工時 metric 已是 6 階，**B 的 acceptance 直接要求 drift < 1e-6**，不再放寬到 1e-3。

### 3.4 實作項目

#### B1. `compute_dt_global_gilbm` 函式

```python
def compute_dt_global_gilbm(cfg, cfl=0.5, metric_order=6):
    """Mirror gilbm/precompute.h:78-115 ComputeGlobalTimeStep.

    dt_global = cfl / max|c~|, where max is over:
      c~_eta  = 1/dx                                      (scalar)
      c~_xi   = xi_y · e_y[α] + xi_z · e_z[α]              (per α, per (j,k))
      c~_zeta = zeta_y · e_y[α] + zeta_z · e_z[α]          (per α, per (j,k))
    """
    _, y_2d, z_2d = build_grid_xyz(cfg)
    if metric_order == 6:
        dj_dy, dj_dz, dk_dy, dk_dz = compute_inverse_metric_2d_fornberg(y_2d, z_2d)
    else:
        dj_dy, dj_dz, dk_dy, dk_dz = compute_inverse_metric_2d(y_2d, z_2d)

    sl = (slice(BFR, BFR + cfg.NY), slice(BFR, BFR + cfg.NZ))
    zeta_y, zeta_z = dk_dy[sl], dk_dz[sl]
    xi_y,   xi_z   = dj_dy[sl], dj_dz[sl]
    dx = LX / (cfg.NX - 1)

    # spanwise: c~_eta = 1/dx (constant)
    max_c = 1.0 / dx
    max_component = 'eta'

    # streamwise / wall-normal: scan D3Q19 dirs
    for alpha in range(3, 19):
        ey, ez = E[alpha, 1], E[alpha, 2]
        c_zeta_max = float(np.abs(zeta_y * ey + zeta_z * ez).max())
        c_xi_max   = float(np.abs(xi_y   * ey + xi_z   * ez).max())
        if c_xi_max > max_c:
            max_c, max_component = c_xi_max, f'xi (α={alpha})'
        if c_zeta_max > max_c:
            max_c, max_component = c_zeta_max, f'zeta (α={alpha})'

    if max_c <= 0.0:
        raise ValueError('compute_dt_global_gilbm: max|c~|=0')
    print('      dt limited by {} component, max|c~| = {:.6e}'.format(max_component, max_c))
    return cfl / max_c
```

#### B2. CLI flags

```python
p.add_argument('--cfl', type=float, default=0.5,
               help='CFL lambda for dt_global computation (default: %(default)s)')
p.add_argument('--skip-drift-check', action='store_true',
               help='Write dt_global=-1.0 to bypass solver Phase 5 drift check '
                    '(debug only).')
```

#### B3. metadata 寫入修改

```python
if args.skip_drift_check:
    dt_for_meta = '-1.0'
    print('      WARN: --skip-drift-check; dt_global=-1.0; Phase 5 drift check WILL be skipped.')
else:
    dt_real = compute_dt_global_gilbm(NEW, cfl=args.cfl, metric_order=args.metric_order)
    dt_for_meta = '{:.15e}'.format(dt_real)
    print('      dt_global = {:.6e}  (CFL={}, metric_order={})'.format(
        dt_real, args.cfl, args.metric_order))
new_meta['dt_global'] = dt_for_meta
```

注意 `compute_dt_global_gilbm` 不需要 `niu` 參數——`dt_global` 公式本身只跟 grid metric 與 CFL 有關，跟黏度無關。

### 3.5 Acceptance Criteria（v3：drift < 1e-6 直接要求）

- [ ] `--cfl 0.5 --metric-order 6`：metadata 內 `dt_global` 是合理正數（O(1e-3) ~ O(1e-2)）
- [ ] **OLD=NEW self-consistency**：對同一 grid 算的 dt 跟 origin metadata 的 dt **差 < 1e-6**（v3 直接要求 solver drift check 門檻；前提是 Phase A 已完成）
- [ ] **真實 regrid drift check**：跑 small-scale integration test，solver 啟動 Phase 5 drift check 應 pass
- [ ] `--skip-drift-check`：仍寫 `-1.0`，warn 訊息出現
- [ ] **`max_component` 印出來作為 audit**：值合理即可（v3.1 修正：先前寫「不應永遠是 'eta'」過於嚴格，因為 §3.2 已承認某些網格 dx 確實會主導 dt）。判讀邏輯：
  - **eta 是合理結果，不是 bug**——表示 spanwise dx 比 streamwise/wall-normal 的 metric 更限制 dt
  - 若顯示 eta，**只需確認** `dx ≈ LX/(NX-1)` 的值跟 `1/c~_ζ`、`1/c~_ξ` 的 min 比較確實是 dx 較小
  - 若 dx 明顯較大但仍顯示 eta → 表示 `c_xi/c_zeta` 計算有 bug（例如 metric 反 Jacobian 算錯）
  - 若 dx 明顯較小且顯示 eta → 正常，跳過此檢查

### 3.6 預估規模

| 項目 | 行數 |
|---|---|
| compute_dt_global_gilbm | ~35 |
| CLI flag x2 + 整合 | ~15 |
| Self-consistency test | ~30 |
| **小計** | **~80** |

---

## 4. 整體驗證計畫

### 4.1 Per-Phase Acceptance（已列在各 phase）

### 4.2 Integration test（每完成一 phase 跑一次）

**小規模 case**（1-2 小時就能跑完）：
- Re=150 或 Re=700（已知穩定的低 Re）
- OLD GAMMA=2.0, NEW GAMMA=3.0
- 跑 origin checkpoint regrid → restart → ≥ 5000 步無發散

### 4.3 Production test（三 phase 全部完成後）

**目標 case**：Re=5600 GAMMA 變更 regrid，跑 ≥ 50000 步不發散，且統計量在 1 FTT 後達到 ERCOFTAC profile 的 < 5% 誤差。

---

## 5. 執行 Checklist

### Phase C（先做）
- [ ] C1 build_old_cell_search_index
- [ ] C2 bilinear_inverse_newton + _DegenerateCellError
- [ ] C3 bilinear_inverse_triangle_fallback
- [ ] C4 find_containing_cell_2d（Newton + out-of-bounds 都試 triangle fallback）
- [ ] C5a PhysMapping2D + precompute_phys_mapping_2d（**reusable cache**）
- [ ] C5b interpolate_phys_3d_with_mapping
- [ ] C5c interpolate_phys_3d 一次性 wrapper
- [ ] C6 --interp-mode CLI flag
- [ ] C7 主流程整合（**用 mapping cache，cell search 只跑一次**）+ y2d_old/z2d_old 計算
- [ ] py_compile pass
- [ ] **Test 1**: Identity (OLD=NEW) residual < 1e-12（**setup 含 fill_ghost(field_old, OLD)**）
- [ ] **Test 2**: Linear analytic `10y + 100z + 0.5x` 在不同 GAMMA 下 < 1e-10（**setup 含 fill_ghost**）
- [ ] **Test 3**: Real checkpoint positivity / range / mean drift / sum(f)=rho（real checkpoint 已含 ghost，不需 fill_ghost）
- [ ] **效能 check**: NY×NZ ≈ 200×200 下，mapping build + 4 fields interp 總時間 < 2 分鐘
- [ ] git commit (zh-TW): `Phase C：物理空間插值（核心 regrid bug 修復）`
- [ ] Integration test (small Re) pass

### Phase A（中做）
- [ ] A1 FD6_COEFF
- [ ] A2a fd6_axis_central（j 方向用，讀 ±3 ghost）
- [ ] A2b fd6_axis_adaptive（k 方向用，不讀 ghost）
- [ ] A3 compute_inverse_metric_2d_fornberg（j-central + k-adaptive 混合）
- [ ] A4 --metric-order CLI flag
- [ ] A5 CE branch 整合
- [ ] py_compile pass
- [ ] **Convergence test**：sin/cos 解析函數，6 階比 2 階小 4 量級
- [ ] **j-direction periodic test**：sin(2π·j/NY) 6 階 central 用週期 ghost 誤差 < 1e-10
- [ ] **k-direction polynomial test**：5 次多項式 6 階 adaptive 應 bit-exact
- [ ] CE conservation regression pass
- [ ] git commit: `Phase A：6 階 Fornberg metric（j-central + k-adaptive，mirror solver）`

### Phase B（最後）
- [ ] B1 compute_dt_global_gilbm（含 c_eta = 1/dx）
- [ ] B2 --cfl, --skip-drift-check CLI flags
- [ ] B3 metadata 寫入修改
- [ ] py_compile pass
- [ ] dt 在合理量級
- [ ] **OLD=NEW self-consistency < 1e-6**（v3 標準：Phase A 已完成，drift check 應一次過）
- [ ] max_component 輸出作為 audit（eta 不是錯，需確認 dx 確實主導 dt；判讀邏輯見 §3.5）
- [ ] **真實 regrid 跑起來**：solver 啟動 Phase 5 drift check pass
- [ ] git commit: `Phase B：實際 dt_global 計算（含 c_eta，啟用 Phase 5 drift check）`

### 整合驗證
- [ ] 三 phase 完成後 docstring 再次 audit
- [ ] metadata 欄位 audit（新增 metric_order, cfl, interp_mode 等）
- [ ] 跑 small-scale integration test
- [ ] 跑 production Re5600 regrid（最終驗證）

---

## 6. Decision Log

| # | 議題 | 決策 | 來源 |
|---|---|---|---|
| 1 | Cell search 演算法 | numpy 向量化 brute search（無 scipy 依賴） | 初版 |
| 2 | Bilinear inverse 退化處理 | **改 Newton 2×2 + triangle barycentric fallback；Newton 出界也走 triangle** | v2 reviewer #5 + v3 reviewer #4 |
| 3 | 邊界 fail-fast 嚴格度 | OLD domain 外 → ValueError；週期方向 ghost 自動處理 | 初版 |
| 4 | Phase 順序 | **C → A → B**（先修發散，再 6 階 metric，最後 fail-fast；A 必須在 B 之前否則 B 寫入低精度 dt 反而比現況糟） | v3 reviewer #1（v1: A→B→C；v2: C→B→A；v3: C→A→B） |
| 5 | Legacy fallback | 4 個獨立 flag：`--metric-order`, `--interp-mode`, `--fneq-mode`, `--skip-drift-check` | 初版 |
| 6 | 測試策略 | 每 phase 加 unit test，最後跑 integration | 初版 |
| 7 | Commit 切分 | 三 phase 獨立 commit | 初版 |
| 8 | dt_global 公式 | **包含 c_eta = 1/dx**（mirror precompute.h:84） | v2 reviewer #3 |
| 9 | Phase A metric 計算 | **j 方向 6 階 central（讀週期 ghost）+ k 方向 adaptive skew（不讀外推 ghost）** | v3 reviewer #2（v2 誤寫 j/k 都用 adaptive） |
| 10 | i 方向插值邊界 | **不 clamp，讓 ghost wrap 處理** | v2 reviewer #4 |
| 11 | Mass conservation 標準 | **改成 range + mean drift + sum(f)=rho**（trilinear 非保守） | v2 reviewer #6 |
| 12 | Phase C v1 acceptance | **3 個簡化測試**（identity / linear analytic / positivity）；synthetic test 必須先 fill_ghost(field_old, OLD) | v2 reviewer 簡化版 + v3 reviewer #3 |
| 13 | Docstring 同步 | interp_checkpoint.py:27-29 docstring 第 6 點明寫「FTT/accu_count NOT preserved」 | v3 reviewer #5 |
| 14 | Phase C mapping cache | **拆 `precompute_phys_mapping_2d` + `interpolate_phys_3d_with_mapping`**；4 個場共用一次 cell search，總時間從 ~5min 降到 ~1.5min | v3.1 reviewer #1 |
| 15 | Phase B max_component 判讀 | eta **不是錯**，是 dx 主導 dt 的合理結果；audit 時確認 dx 是否確實較小 | v3.1 reviewer #2 |

---

## 7. Open Questions

| # | 問題 | 候選 |
|---|---|---|
| Q1 | `--cfl` 預設值 | 0.5（grid_zeta_tool 用 0.5）vs 0.45（更保守） |
| Q2 | Production 測試的 origin checkpoint 從哪取 | 需用戶提供 |
| Q3 | Phase C 效能目標 | NY×NZ ≈ 200×200 下完成時間 < 2 分鐘 |

> **v2 Q3 已關閉**：v2 列出「Phase A 開工時要先 confirm `FD6_j_central` 是否 adaptive」，v3 已在驗證程式碼後 confirm 是**純 central**（[metric_terms.h:100-109](../gilbm/metric_terms.h:100)），Decision Log #9 已更新對應。

---

## 8. 附錄：關鍵 source-of-truth 引用

| 公式 / 概念 | 引用位置 |
|---|---|
| D3Q19 lattice (E, W) | [interp_checkpoint.py:547-556](interp_checkpoint.py:547) |
| Fornberg 7-point coefficients | [gilbm/metric_terms.h:34-42](../gilbm/metric_terms.h:34) |
| Solver k-direction adaptive skew | [gilbm/metric_terms.h:71-95](../gilbm/metric_terms.h:71) |
| Solver j-direction central | [gilbm/metric_terms.h:100+](../gilbm/metric_terms.h:100) |
| Wall CE BC formula | [gilbm/boundary_conditions.h:13-21](../gilbm/boundary_conditions.h:13) |
| Wall 4th-order one-sided FD | [gilbm/boundary_conditions.h:30-33](../gilbm/boundary_conditions.h:30) |
| `dt_global = CFL/max\|c~\|` 公式（**含 c_eta**） | [gilbm/precompute.h:78-115](../gilbm/precompute.h:78) |
| `omega = 0.5 + 3·niu/dt` | [variables.h:363](../variables.h:363) |
| Phase 5 drift check threshold (1e-6) | [fileIO.h:658](../fileIO.h:658) |
| Stats binary loading guard | [fileIO.h:748](../fileIO.h:748) |
| `niu = Uref/Re` | [variables.h:152](../variables.h:152) |

---

## 9. 修訂歷史

### v3.1（2026-05-09，依 reviewer v3 → v3.1 2 條小調整）

| Finding | 嚴重度 | 修正內容 |
|---|---|---|
| #1 Phase C mapping cache | (perf, 非 correctness blocker) | §1.5 拆 C5 為 C5a `precompute_phys_mapping_2d` + C5b `interpolate_phys_3d_with_mapping` + C5c 一次性 wrapper；§1.5 C7 主流程改成 build mapping 一次、4 個場共用；§1.7 規模重算（~290 → ~330 行）。Decision Log #14 新增。 |
| #2 Phase B max_component 敘述放寬 | (wording) | §3.5 acceptance 改成「eta 不是錯，是 dx 主導 dt 的合理結果」，加判讀邏輯；§5 Phase B checklist 對應更新。Decision Log #15 新增。 |

### v3（2026-05-09，依 reviewer v2 → v3 5 條 finding）

| Finding | 嚴重度 | 修正內容 |
|---|---|---|
| #1 Phase B/A 順序仍有實作風險 | High | C→B→A 重排為 **C→A→B**（A 必須在 B 之前，否則 B 寫入低精度 dt 反而比現況糟）。§0 Phase 順序理由重寫，§2 改為 Phase A、§3 改為 Phase B。Decision Log #4 更新。 |
| #2 Phase A j 方向不該 adaptive | High | §2.2 拆解 j（純 central + 週期 ghost）vs k（adaptive skew + 不讀 ghost）的對照表。§2.3 拆 A2 為 A2a `fd6_axis_central`（j 用）+ A2b `fd6_axis_adaptive`（k 用）。A3 改成 j-central + k-adaptive 混合。Decision Log #9 更新。 |
| #3 Synthetic test 需明寫 fill_ghost | Medium | §1.6 三個測試前置加「`fill_ghost(field_old, OLD)` 必要」說明；Test 2 setup 程式碼明示這步；§5 checklist Test 1/2 加註 setup 含 fill_ghost。Decision Log #12 更新。 |
| #4 Newton 出界不會 fallback | Medium | §1.5 C4 邏輯重寫：Newton fail **或** out-of-bounds 都試 triangle fallback；triangle 也失敗才跳下個 candidate。Decision Log #2 更新。 |
| #5 Docstring 未同步 | Low | 直接 patch [interp_checkpoint.py:27-29](interp_checkpoint.py:27) docstring 第 6 點，明寫「FTT/accu_count NOT preserved」與 fileIO.h:748 stats binary 缺檔風險。Decision Log #13 新增。 |

### v2（2026-05-09，依 reviewer v1 → v2 7 條 finding）

| Finding | 嚴重度 | 修正內容 |
|---|---|---|
| #1 phase 順序 | Critical | A→B→C 重排為 C→B→A。Phase 順序理由章節重寫。Decision Log #4 更新。 |
| #2 Phase A 沒真正 mirror solver | High | §3.2 加入 solver adaptive skew 邏輯說明。§3.3 A2 改成 `fd6_axis_adaptive`，A3 用 adaptive 而非純 central。Decision Log #9 新增。 |
| #3 dt_global 漏 c_eta | High | §2.2 公式加入 `c~_eta = 1/dx`。§2.4 B1 範例實作加入 c_eta scan。Decision Log #8 新增。 |
| #4 i 方向插值 bug | High | §1.5 C5 改成 ghost-wrap 邏輯（不 clamp i_o）；附說明為什麼這樣對。Decision Log #10 新增。 |
| #5 bilinear_inverse 半推導 | Medium | §1.5 C2/C3 改成 Newton 2×2 + triangle barycentric fallback。Decision Log #2 更新。 |
| #6 mass conservation 不適用 | Medium | §1.6 acceptance Test 3 改成 range/mean drift/sum(f)=rho。Decision Log #11 新增。 |
| #7 文件已修狀態描述模糊 | Low | §0 Controller state 行明寫「**僅** 4 欄位；**FTT, accu_count 強制 reset**」。 |

### v1（2026-05-09 早期）

初版計畫（A→B→C，含 reviewer 後標出的 7 個問題）。

---

**作者**：Claude Code 協作  
**版本**：v3.1（2026-05-09）  
**狀態**：v3.1 已反映 mapping cache 拆分 + max_component audit 敘述放寬。Reviewer 已判定「計畫已可執行」，**下一步直接進入 Phase C 實作**。
