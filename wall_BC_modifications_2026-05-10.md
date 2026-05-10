# CE Wall BC Code Review — 壁面三項修正完整指南

**日期**: 2026-05-10  
**修改者**: ChenPengChung  
**來源專案**: `Channel_65x17x17` (Poiseuille Re=100 精度測試)  
**目標專案**: `5.Re10595/Edit3_5600newmesh/`, `5.Re10595/Edit2_restart/`  
**狀態**: Edit2/Edit3 已完成全部三項移植，待 `--build-only` 編譯驗證  
**移植腳本**: `port_ce_bc_fixes.sh` (修改 A+B), `port_wall_guo_fixes.sh` (修改 C)

---

## 0. 一句話摘要 (TL;DR)

壁面 Chapman-Enskog BC 有三項缺陷：**(A)** f_neq 係數層級錯誤、**(B)** 高階 FD 引起回饋增益不穩定、**(C)** Guo-Forcing 二階修正使壁面速度不為零。三項修正合併後：**L_inf 誤差降低 108 倍，空間精度達到 O(h²)，壁面 no-slip 條件精確滿足**。

---

## 1. 修改動機 — 三項缺陷分析

### 1.1 缺陷 A：CE 係數層級錯誤

原代碼的 CE BC 使用 `(τ-0.5)·Δt = 3ν` 作為 f_neq 的比例係數。
這對應的是 **Navier-Stokes 物理應力**，而非 **lattice distribution f^(1)**。

**理論依據** — Imamura (2005) Eq.(A.9)：

```
LBM BGK collision: f* = f - (1/τ)(f - f_eq)
Chapman-Enskog 展開: f = f_eq + ε·f^(1) + O(ε²)

f^(1) = -τ·Δt · f_eq/c_s² · Σ (c_α·c_β - c_s²·δ_αβ) · ∂_β u_α
                 ↑
           lattice f^(1) 的係數是 τ·Δt，不是 (τ-0.5)·Δt

CE 約束自動保證: Σ f^(1)·c_α·c_β = -ρ·ν·(∂_α u_β + ∂_β u_α)
其中 ν = (τ-0.5)·Δt·c_s²
```

**關鍵區別**：
- **構建 lattice f^(1) → 係數用 `τ·Δt`** ← 正確選擇
- **匹配 NS 物理應力 → 係數用 `(τ-0.5)·Δt`** ← 原代碼（錯誤層級）

**數值後果**：
```
係數比值 = (τ-0.5)/τ = 0.0144/0.5144 ≈ 0.028
→ 原代碼的 CE BC 僅提供了正確 f_neq 的 2.8%
→ 壁面處殘留 97% 的 f_neq 誤差
→ 空間收斂率無法量測（被壁面誤差主導）
```

### 1.2 缺陷 B：高階 FD 回饋增益不穩定

CE BC 對壁面速度梯度 du/dk 存在 feedback loop：
```
u_interior ─→ FD ─→ du/dk ─→ C_alpha ─→ f_wall ─→ collision ─→ u_interior (下一步)
```

修正 A 將係數放大 ~35 倍後，6th-order FD 的最大係數 (360/60=6.0) 導致回饋增益 G > 1，引起震盪→發散。

### 1.3 缺陷 C：Guo-Forcing 二階修正使壁面 v ≠ 0

**Guo-Shi-Zheng (2002)** 的二階精確外力方案在宏觀速度計算中加入半力修正：
```
ρu = Σ f_q · e_q + (Δt/2) · F
```

對 Poiseuille/Hill 流（y 方向體積力 `F = (0, Force, 0)`）：
```
v_local = (my_stream + Δt·Force/2) / ρ
```

**問題**：在壁面（k=3 或 k=NZ6-4），streaming 後的 `my_stream = Σ f_q · e_y[q]` 包含 14 個非 BC 方向帶來的殘留假動量，使得：
```
my_stream ≠ 0  →  v_local = (my_stream + ½FΔt)/ρ ≠ 0  →  壁面 no-slip 不滿足
```

這是因為：
1. q-loop 中只有 ~5 個方向被 `NeedsBoundaryCondition()` 判定需要 CE BC
2. 其餘 ~14 個方向仍使用 streaming 值，其 `Σ f_q · e_y` ≠ 0
3. 即使 Guo 修正在內部節點給出正確 v，壁面上的 `my_stream` 偏差破壞了 no-slip

**數值後果**：壁面 v ≠ 0 → 壁面剪應力和質量通量不正確 → 精度受損

---

## 2. 修改內容 — 三項修正總覽

共 3 項修正，涉及 2 個檔案、2 個移植腳本：

| 修改 | 內容 | 檔案 | 移植腳本 |
|------|------|------|---------|
| **A** | CE 係數 `(τ-0.5)·Δt` → `τ·Δt` | `boundary_conditions.h` | `port_ce_bc_fixes.sh` |
| **B** | 壁面 FD `6th-order` → `2nd-order` | `1.algorithm1.h` | `port_ce_bc_fixes.sh` |
| **C** | Guo-Forcing 壁面修正 (C1+C2+C3) | `boundary_conditions.h` + `1.algorithm1.h` | `port_wall_guo_fixes.sh` |

**修改 C 的三個子項**：

| 子項 | 內容 | 作用 |
|------|------|------|
| **C1** | 新增 `WallCERegularize` 函式 | 全 19 方向 CE 重建 + 動量預修正 |
| **C2** | 兩個 kernel 中呼叫 `WallCERegularize` | q-loop 後覆寫壁面所有 f 和動量 |
| **C3** | 碰撞外力 `Force_collision = is_wall ? 0 : Force[0]` | 壁面禁止外力注入 |

---

## 3. 逐修改點 Code Review

### 3.1 修改 A: CE 係數修正

**檔案**: `gilbm/boundary_conditions.h`  
**位置**: line 109  
**函數**: `__device__ double ChapmanEnskogBC(...)`  
**風險等級**: **高** — 此處決定所有壁面 f 值的 f_neq 比例

```c
// 修改前 (line 109):
    C_alpha *= -(omega_global - 0.5) * dt_global;
//               ├──────────────────┘
//               (τ-0.5)·Δt = 3ν  ← NS 應力層級 (錯誤)

// 修改後 (line 109):
    C_alpha *= -(omega_global) * dt_global;
//               ├────────────┘
//               τ·Δt          ← lattice f^(1) 層級 (正確, Imamura Eq. A.9)
```

**另有 host-side 副本** (`gilbm/diagnostic_gilbm.h` line 295)：
```c
// 修改前:  C_alpha *= -(omega_global - 0.5) * dt_global_val;
// 修改後:  C_alpha *= -(omega_global) * dt_global_val;
```
> 不影響 GPU 計算，但影響 VTK 診斷輸出。遺漏此處 → device/host 不一致。

### 3.2 修改 B: 壁面速度梯度 FD 降階

**檔案**: `gilbm/evolution_gilbm/1.algorithm1.h`  
**位置**: 2 kernel × (底壁 + 頂壁) = **4 處**，每處 3 行 (du/dv/dw)  
**風險等級**: **高** — 直接影響穩定性

```c
// 底壁 (is_bottom, k=3) — Kernel 1: line 193, Kernel 2: line 499
// 修改前 (6th-order one-sided FD, 使用 6 個鄰點):
    du_dk = (360.0*u3 - 450.0*u4 + 400.0*u5 - 225.0*u6 + 72.0*u7 - 10.0*u8) / 60.0;
//           ├── 最大係數 360/60 = 6.0

// 修改後 (2nd-order one-sided FD, 使用 2 個鄰點):
    du_dk = (4.0*u3 - u4) / 2.0;
//           ├── 最大係數 4/2 = 2.0,  精度 O(h²)

// 頂壁 (is_top, k=NZ6-4) — 底壁公式加負號
// 修改前: du_dk = -(360.0*um1 - 450.0*um2 + ... - 10.0*um6) / 60.0;
// 修改後: du_dk = -(4.0*um1 - um2) / 2.0;
```

**2nd-order FD 推導** (已知 u_wall = 0, 令 u₁=u(k±1), u₂=u(k±2)):
```
u₁ = h·u'(0) + h²/2·u''(0) + O(h³)
u₂ = 2h·u'(0) + 2h²·u''(0) + O(h³)
4u₁ - u₂ = 2h·u'(0) + O(h³)
→ u'(0) = (4u₁ - u₂) / (2h),  h=1 in computational space
→ leading truncation error: -(1/3)·h²·u'''(0) = O(h²)
```

### 3.3 修改 C: Guo-Forcing 壁面三項修正

#### C1: `WallCERegularize` 函式定義

**檔案**: `gilbm/boundary_conditions.h`  
**位置**: `#endif` 之前新增（line 115-144）  
**作用**: 壁面全 19 方向 CE 重建 + Guo 動量預減

```c
__device__ __forceinline__ void WallCERegularize(
    double f_arr[19],
    double &mx_stream, double &my_stream, double &mz_stream,
    double &rho_stream,
    double rho_wall,
    double du_dk, double dv_dk, double dw_dk,
    double zeta_y_val, double zeta_z_val,
    double omega_global, double dt_global,
    double half_Fdt        // ← Guo 半力: 0.5·Δt·Force[0]
) {
    // C1a: 全 19 方向覆寫為 CE 重建值
    for (int q = 0; q < 19; q++)
        f_arr[q] = ChapmanEnskogBC(q, rho_wall, du_dk, dv_dk, dw_dk,
                                    zeta_y_val, zeta_z_val, omega_global, dt_global);
    // C1b: 動量預修正 — 確保 Guo 修正後 v_wall = 0
    rho_stream = rho_wall;
    mx_stream = 0.0;
    my_stream = -half_Fdt;   // ← 關鍵: 預減半力
    mz_stream = 0.0;
}
```

**為什麼 `my_stream = -half_Fdt`？**

Guo (2002) 的宏觀速度計算在後續 STEP 1.5 中執行：
```
v_local = (my_stream + half_Fdt) / rho_local
```

若壁面設 `my_stream = -half_Fdt`：
```
v_local = (-half_Fdt + half_Fdt) / rho_local = 0   ← exact no-slip
```

若壁面不修正（原代碼行為），`my_stream` 含殘留假動量 → `v_wall ≠ 0`。

**CE 重建為什麼覆寫全 19 方向而非僅 BC 方向？**

| 方案 | 做法 | 質量守恆 | 動量歸零 | 壁面剪應力 |
|------|------|---------|---------|-----------|
| per-direction | 僅覆寫 ~5 個 BC 方向 | ✗ 不保證 | ✗ 殘留假動量 | ✗ 不完整 |
| **全 19 方向 (v3)** | **覆寫全部** | **✓ Σ W_q·ρ = ρ** | **✓ 顯式歸零** | **✓ C_q 編碼完整剪應力** |

兩者都呼叫同一個 `ChapmanEnskogBC()` 函式，差別僅在覆寫範圍。全覆寫後加上 `mx=0, my=-½FΔt, mz=0` 的顯式重設，無條件保證壁面動量正確。

#### C2: 兩個 kernel 中呼叫 `WallCERegularize`

**檔案**: `gilbm/evolution_gilbm/1.algorithm1.h`  
**位置**: q-loop 結束後、STEP 1.5 Macroscopic 之前  
**每個 kernel 插入位置**:
- Kernel 1 (non-smem): q-loop `}` (line 368) 之後
- Kernel 2 (smem): `if (!valid) return;` (line 720) 之後

```c
    // q-loop 結束 ↑

    const bool is_wall = (is_bottom || is_top);

    // ── Wall CE regularization (v3) ──
#ifndef DISABLE_WALL_MOMENTUM_CORRECTION
    if (is_wall) {
#if USE_GUO_FORCING
        const double half_Fdt_wall = 0.5 * GILBM_dt * Force[0];
#else
        const double half_Fdt_wall = 0.0;
#endif
        WallCERegularize(f_arr, mx_stream, my_stream, mz_stream,
                         rho_stream, rho_wall,
                         du_dk, dv_dk, dw_dk,
                         zeta_y_val, zeta_z_val,
                         omega_global, dt_global,
                         half_Fdt_wall);
    }
#endif

    // ── STEP 1.5: Macroscopic (mass correction) ── ↓
```

**條件編譯保護**:
- `DISABLE_WALL_MOMENTUM_CORRECTION`: 可全域停用（用於 A/B testing）
- `USE_GUO_FORCING`: 若未使用 Guo 外力方案，`half_Fdt_wall = 0`（退化為純 CE 重建）

#### C3: 碰撞外力壁面禁止

**檔案**: `gilbm/evolution_gilbm/1.algorithm1.h`  
**位置**: 每個 kernel 的 collision 呼叫前  
**修改數**: 2 處（Kernel 1 + Kernel 2）

```c
// 修改前:
    gilbm_collision_GTS(f_out, f_arr, rho_local, u_local, v_local, w_local,
                        GILBM_s_visc_global, GILBM_dt, Force[0]);
//                                                     ├────────┘
//                                                     壁面也注入外力 → 矛盾

// 修改後:
    const double Force_collision = is_wall ? 0.0 : Force[0];
    gilbm_collision_GTS(f_out, f_arr, rho_local, u_local, v_local, w_local,
                        GILBM_s_visc_global, GILBM_dt, Force_collision);
//                                                     ├────────────────┘
//                                                     壁面外力 = 0
```

**為什麼壁面不能有碰撞外力？**

壁面的 f 已經由 `WallCERegularize` 完全重建為 CE 分佈，動量被強制歸零。若碰撞中再注入外力 `Force[0]`，collision operator 的 Guo forcing term 會在壁面 f 中加入 `ΔF_q ∝ Force[0]`，下一步 streaming 時壁面向內部節點輸出含外力的分佈 → 壁面發出虛假的驅動力。設 `Force_collision = 0` 消除此矛盾。

---

## 4. 三項修正的耦合關係

```
           ┌───────────────────────────────────────────────────────┐
           │  壁面 BC 的完整 pipeline (每個壁面節點, 每步)             │
           │                                                       │
           │  u_interior ──→ FD(2nd) ──→ du/dk               修改B │
           │                               │                       │
           │                     C_alpha = ... × du/dk             │
           │                               │                       │
           │                 C_alpha *= -(τ·Δt)              修改A │
           │                               │                       │
           │  ┌─ q-loop: per-direction CE BC (選定方向)  ──────────┐ │
           │  │  f_q = W_q·ρ_wall·(1 + C_q)                      │ │
           │  └───────────────────────────────────────────────────┘ │
           │                               │                       │
           │  ┌─ WallCERegularize (全 19 方向覆寫)  ──────────────┐ │
           │  │  f_arr[q] = W_q·ρ_wall·(1+C_q),  q=0..18  修改C1 │ │
           │  │  my_stream = -½FΔt                          修改C2 │ │
           │  └───────────────────────────────────────────────────┘ │
           │                               │                       │
           │  STEP 1.5: v = (my_stream + ½FΔt)/ρ = 0    ← no-slip │
           │                               │                       │
           │  collision(Force_collision = 0)              修改C3   │
           │       │                                               │
           │       └──→ f_out ──→ streaming ──→ u_interior (下一步) │
           └───────────────────────────────────────────────────────┘
```

### 4.1 為什麼必須三項同時修正

| 缺少哪項 | 後果 | 原因 |
|----------|------|------|
| 只改 A，不改 B | **發散** | CE 係數放大 ~35 倍，6th-order FD 增益 G>1 |
| 改 A+B，不改 C | **v_wall ≠ 0** | Guo 半力使壁面 `v = (my_stream + ½FΔt)/ρ ≠ 0` |
| 改 A+B+C1+C2，不改 C3 | **壁面外力泄漏** | collision 中 Guo forcing 再注入體積力 |
| **A+B+C 全改** | **正確** | 係數正確 + 穩定 + no-slip 精確 + 無外力泄漏 |

### 4.2 回饋增益分析

```
回饋增益 G = τ·Δt × 3 × max_FD_coeff × ζ_z(wall)

修改前 (A舊+B舊): G_old = (τ-0.5)·Δt × 3 × 6.0 × ζ_z ≈ 0.16  ✅
只改 A (A新+B舊): G_new =      τ·Δt × 3 × 6.0 × ζ_z ≈ 4.75  ❌ (G/G_old ≈ 35×)
改 A+B (A新+B新): G     =      τ·Δt × 3 × 2.0 × ζ_z ≈ 1.59  ✅ (Channel 實測穩定)
```

---

## 5. 量化驗證結果 — 修改前後效果

### 5.1 單網格效果 (NZ=65)

| 指標 | 修改前 | 修改後 | 改善 |
|------|--------|--------|------|
| CE 係數 | (τ-0.5)·Δt | τ·Δt | — |
| 壁面 FD | 6th-order | 2nd-order | — |
| Guo 壁面修正 | 無 | WallCERegularize + Force_collision=0 | — |
| L_inf (穩態) | 6.538e-04 | 6.040e-06 | **108×** |
| 空間收斂階 p | 不可量測 | **≈ 2.0** | — |
| 壁面 v | ≠ 0 (Guo residual) | exact 0 | — |

### 5.2 空間收斂驗證 — 均勻網格 R=1.0

4 組網格 (NZ=33/65/129/257), CFL=1.0, CHANNEL_UNIFORM_TEST=1:

| NZ | L_inf | L2_norm | p(L_inf) |
|----|-------|---------|----------|
| 33 | 2.389e-05 | 1.675e-05 | — |
| 65 | 6.040e-06 | 4.228e-06 | 1.98 |
| 129 | 1.627e-06 | 1.114e-06 | 1.89 |
| 257 | 4.505e-07 | 3.027e-07 | 1.85 |

最小二乘擬合: **p(L_inf) = 1.91, p(L2) = 1.93, R² > 0.9997**

### 5.3 空間收斂驗證 — 非均勻網格 R=0.3 (tanh-stretched)

4 組網格 (NZ=33/65/129/257), CFL=0.3, tanh stretch a=0.9:

| NZ | L_inf | L2_norm | p(L_inf) |
|----|-------|---------|----------|
| 33 | 4.198e-05 | 2.908e-05 | — |
| 65 | 9.435e-06 | 6.758e-06 | 2.15 |
| 129 | 2.257e-06 | 1.644e-06 | 2.06 |
| 257 | 5.527e-07 | 4.054e-07 | 2.03 |

最小二乘擬合: **p(L_inf) = 2.08, p(L2) = 2.05**

### 5.4 結論

均勻與非均勻網格下均確認 **O(h²) 空間收斂**。
收斂圖: `convergence_a_NZ_vs_Linf.pdf` ~ `convergence_d_logh_vs_logL2.pdf`

---

## 6. Edit2/Edit3 移植狀態

### 6.1 全部修改點 — 完整對照表

| # | 修改 | 檔案 | 位置 | Edit2 | Edit3 |
|---|------|------|------|-------|-------|
| A | CE 係數 `τ·Δt` | `boundary_conditions.h` | line 109 | ✅ | ✅ |
| A' | Host CE 係數 | `diagnostic_gilbm.h` | line 295 | ✅ | ✅ |
| B1 | K1 底壁 FD 2nd | `1.algorithm1.h` | line 193 | ✅ | ✅ |
| B2 | K1 頂壁 FD 2nd | `1.algorithm1.h` | line 211 | ✅ | ✅ |
| B3 | K2 底壁 FD 2nd | `1.algorithm1.h` | line 499 | ✅ | ✅ |
| B4 | K2 頂壁 FD 2nd | `1.algorithm1.h` | line 517 | ✅ | ✅ |
| C1 | `WallCERegularize` 函式 | `boundary_conditions.h` | line 125-144 | ✅ | ✅ |
| C2a | K1 呼叫 WallCERegularize | `1.algorithm1.h` | line 371-388 | ✅ | ✅ |
| C2b | K2 呼叫 WallCERegularize | `1.algorithm1.h` | line 723-740 | ✅ | ✅ |
| C3a | K1 Force_collision | `1.algorithm1.h` | line 412-414 | ✅ | ✅ |
| C3b | K2 Force_collision | `1.algorithm1.h` | line 763-765 | ✅ | ✅ |

### 6.2 移植腳本

**第一次移植 (修改 A+B)**：`port_ce_bc_fixes.sh`
- CE 係數: `sed` 替換 `omega_global - 0.5` → `omega_global`
- FD 降階: `sed` 替換 `360.0*u3 - ...` → `4.0*u3 - u4`

**第二次移植 (修改 C)**：`port_wall_guo_fixes.sh`
- C1: `sed -i '/#endif/i\...'` 在 `boundary_conditions.h` 插入 `WallCERegularize` 函式
- C2: `awk` 在兩個 `STEP 1.5` 錨點前插入 wall regularization block
- C3: `awk` + `sed` 在 collision 呼叫前插入 `Force_collision` 宣告並替換參數

### 6.3 驗證 grep (已全部通過)

```bash
# CE 係數 — 無殘留舊版
grep -rn "omega_global - 0.5" gilbm/ | grep -v "//" | grep -v ".bak"
# → (無輸出) ✅

# FD — 無殘留 6th-order active code
grep -n "360.0" gilbm/evolution_gilbm/1.algorithm1.h | grep -v "//"
# → (無輸出) ✅

# C1 — WallCERegularize 函式存在
grep -c "void WallCERegularize" gilbm/boundary_conditions.h
# → 1 ✅

# C2 — WallCERegularize 呼叫 (2 kernel)
grep -c "WallCERegularize(" gilbm/evolution_gilbm/1.algorithm1.h
# → 2 ✅

# C3 — Force_collision 宣告與使用
grep -c "const double Force_collision" gilbm/evolution_gilbm/1.algorithm1.h
# → 2 ✅
grep -c "GILBM_dt, Force_collision)" gilbm/evolution_gilbm/1.algorithm1.h
# → 2 ✅

# 舊 Force[0] 在 collision 中已消除
grep -c "GILBM_dt, Force\[0\])" gilbm/evolution_gilbm/1.algorithm1.h
# → 0 ✅
```

### 6.4 備份

```
# 第一次移植備份
gilbm/boundary_conditions.h.bak.20260510
gilbm/diagnostic_gilbm.h.bak.20260510
gilbm/evolution_gilbm/1.algorithm1.h.bak.20260510

# 第二次移植備份 (C1+C2+C3)
gilbm/boundary_conditions.h.bak_guo
gilbm/evolution_gilbm/1.algorithm1.h.bak_guo
```

### 6.5 待完成

- [ ] `--build-only` 編譯通過
- [ ] 短跑 100-500 步確認 Ma_max 穩定 < 0.1
- [ ] 確認 solver 啟動輸出中的 ζ_z(wall) 值，計算實際 G
- [ ] 確認壁面 v_local 輸出為精確零

---

## 7. Edit2/Edit3 vs Channel 差異 (Code Reviewer 須知)

### 7.1 已消除的差異（修改 C 之前存在，現已移植）

| 差異點 | 修改前 | 修改後 |
|--------|--------|--------|
| 壁面 BC 架構 | per-direction CE BC only | **+ WallCERegularize 全覆寫** |
| Guo 壁面處理 | 無修正 (v_wall ≠ 0) | **my_stream = -½FΔt** |
| 碰撞外力 | `Force[0]` 無條件 | **`is_wall ? 0 : Force[0]`** |

### 7.2 仍存在的非功能性差異（不影響計算結果）

| 差異 | 數量 | 說明 |
|------|------|------|
| FD 註解仍寫 "6th-order" | 8 處 | 程式碼正確，僅註解未更新 |
| CE 係數 line 107-108 註解描述舊公式 | 2 處 | 不影響執行 |
| Kernel 2 多載 u5-u8 未用變數 | 2 處 | Channel 已清理為 2 點，Edit2/Edit3 仍載 6 點 |
| Channel 多了 rho_wall 外推說明註解 | 4 處 | 純文件差異 |

---

## 8. 風險矩陣與緩解方案

| 風險 | 嚴重度 | 可能性 | 徵兆 | 緩解 |
|------|--------|--------|------|------|
| FD 回饋增益 G>1 → 發散 | 高 | 中 (Re10595 ζ_z 大) | Ma_max 飆升或 NaN | 見 §8.1 備選 |
| WallCERegularize 覆寫 mass correction | 無 | 已排除 | — | C2 在 STEP 1.5 之前執行 |
| `DISABLE_WALL_MOMENTUM_CORRECTION` 意外定義 | 中 | 低 | L_inf 停在 ~6.5e-4 | grep 搜尋此 macro |
| Edit2/Edit3 kernel 數量不符 | 高 | 已排除 | — | diff 驗證 Edit2=Edit3，僅 2 kernel |

### 8.1 若 Re10595 不穩定的備選方案 (依優先序)

| 優先 | 方案 | 修改方式 | 精度影響 | 穩定性 |
|------|------|---------|---------|--------|
| 1 | Under-relaxation | `C_alpha *= relax;` (0.5~0.9) | p 略降 | 可調至穩定 |
| 2 | 1st-order FD | `du_dk = u3;` | p 降至 O(h) | G 降為原來的 1/2 |
| 3 | 回退 CE 係數 | 恢復 `-(omega_global - 0.5)` | 放棄 108× 改善 | 恢復原穩定性 |

---

## 附錄 A: 修改後完整函式 — `ChapmanEnskogBC`

```c
// boundary_conditions.h line 74-112
__device__ double ChapmanEnskogBC(
    int alpha, double rho_wall,
    double du_dk, double dv_dk, double dw_dk,
    double zeta_y_val, double zeta_z_val,
    double omega_global, double dt_global
) {
    double ex = GILBM_e[alpha][0];
    double ey = GILBM_e[alpha][1];
    double ez = GILBM_e[alpha][2];
    double C_alpha = 0.0;

    // 6 項 CE tensor 展開 (α=x,y,z × β=y,z)
    C_alpha += (3.0*ex*ey) * du_dk * zeta_y_val +
               (3.0*ex*ez) * du_dk * zeta_z_val;
    C_alpha += (3.0*ey*ey - 1.0) * dv_dk * zeta_y_val +
               (3.0*ey*ez) * dv_dk * zeta_z_val;
    C_alpha += (3.0*ez*ey) * dw_dk * zeta_y_val +
               (3.0*ez*ez - 1.0) * dw_dk * zeta_z_val;

    C_alpha *= -(omega_global) * dt_global;      // ← 修改 A: τ·Δt (was (τ-0.5)·Δt)
    return GILBM_W[alpha] * rho_wall * (1.0 + C_alpha);
}
```

## 附錄 B: 修改後完整函式 — `WallCERegularize`

```c
// boundary_conditions.h line 125-144
__device__ __forceinline__ void WallCERegularize(
    double f_arr[19],
    double &mx_stream, double &my_stream, double &mz_stream,
    double &rho_stream, double rho_wall,
    double du_dk, double dv_dk, double dw_dk,
    double zeta_y_val, double zeta_z_val,
    double omega_global, double dt_global,
    double half_Fdt                              // ← 修改 C: Guo 半力
) {
    for (int q = 0; q < 19; q++)
        f_arr[q] = ChapmanEnskogBC(q, rho_wall, du_dk, dv_dk, dw_dk,
                                    zeta_y_val, zeta_z_val, omega_global, dt_global);
    rho_stream = rho_wall;
    mx_stream = 0.0;
    my_stream = -half_Fdt;                       // ← v_wall = (-½FΔt + ½FΔt)/ρ = 0
    mz_stream = 0.0;
}
```

## 附錄 C: 壁面節點完整資料流

```
時步 n → n+1, 壁面節點 (k=3 or NZ6-4):

1. FD 讀取前一步速度 (修改 B):
   du/dk = (4·u[k±1] - u[k±2]) / 2              ← 2nd-order

2. q-loop (q=0..18):
   if NeedsBoundaryCondition(q):
     f_streamed = ChapmanEnskogBC(q, ...)         ← 修改 A: τ·Δt
   else:
     f_streamed = interpolation(...)
   f_arr[q] = f_streamed
   rho/mx/my/mz += f_streamed × e_q

3. WallCERegularize (修改 C1+C2):
   f_arr[q] = W_q·ρ_wall·(1+C_q)  for all q     ← 全覆寫
   mx = 0, my = -½FΔt, mz = 0                    ← 動量預修正

4. STEP 1.5 Macroscopic:
   rho += rho_modify
   v = (my + ½FΔt)/ρ = (-½FΔt + ½FΔt)/ρ = 0    ← exact no-slip

5. Collision (修改 C3):
   Force_collision = 0                             ← 壁面無外力
   f_out = collision(f_arr, u=0, v=0, w=0, Force=0)

6. Write:
   f_post[q] = f_out[q]
   u_out = 0, v_out = 0, w_out = 0
```
