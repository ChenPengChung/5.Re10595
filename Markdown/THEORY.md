# THEORY.md — LBE 二階壁面 Chapman–Enskog BC 推導鏈

> 本文件固化本專案壁面 BC 的理論基礎，避免未來維護者重複踩同一個坑。
> 主要參考：Imamura et al., *J. Comput. Phys.* **202** (2005) 645–663。
> Repo 對應實作：`gilbm/boundary_conditions.h` + `gilbm/evolution_gilbm/`。

---

## 0. 一句話結論

**程式裡演化的 `f` 就是變換後分布 $\bar f$；壁面 BC 直接套 Imamura Eq. (26)，係數是 $-\omega\Delta t$（無 $-1/2$），其中 `omega_global = τ`（Imamura 約定 = 無因次化鬆弛時間）。**

任何改回 NS-level 係數的修改都會和本專案演化的 $\bar f$ 不一致。

---

## 1. 完整推導鏈：從 BGK 走到 Imamura Eq. (26)

### Step 1 — 連續 BGK
$$\partial_t f + \mathbf{c}\cdot\nabla f = -\frac{1}{\lambda}(f - f^{eq})\tag{1}$$
$f$：原始物理分布。$\lambda$：連續鬆弛時間。

### Step 2 — 沿特徵線梯形積分（二階時間離散）
$$f(\mathbf{x}+\mathbf{c}\Delta t, t+\Delta t) - f(\mathbf{x}, t) = -\frac{\Delta t}{2\lambda}\Big[(f-f^{eq})\big|_t + (f-f^{eq})\big|_{t+\Delta t}\Big]\tag{2}$$
這是**隱式**的——右側 $t+\Delta t$ 項使 $f^{new}$ 依賴自己。直接寫 code 會需要解非線性方程。

### Step 3 — He–Chen–Doolen 變換（**理論核心**）
$$\boxed{\;\bar f \;\equiv\; f + \frac{\Delta t}{2\lambda}(f - f^{eq})\;}\tag{3}$$

代回 (2)：
$$\bar f(\mathbf{x}+\mathbf{c}\Delta t, t+\Delta t) - \bar f(\mathbf{x}, t) = -\frac{\Delta t}{\tau}\big[\bar f(\mathbf{x},t) - f^{eq}(\mathbf{x},t)\big]\tag{4}$$

其中
$$\tau \equiv \lambda + \frac{\Delta t}{2}\quad\Longleftrightarrow\quad \lambda = \tau - \frac{\Delta t}{2}\tag{5}$$

(4) 在 $\bar f$ 上**顯式且二階精度**。

### Step 4 — 文獻「省略上標」慣例
1998 後所有 LBE 論文（包含 Imamura 2005）把 $\bar f$ 直接寫成 $f$。本專案程式碼也遵循此慣例——`f`、`f_post`、`f_arr` 都是 $\bar f$。

於是 (4) 變成
$$f(\mathbf{x}+\mathbf{c}\Delta t, t+\Delta t) - f(\mathbf{x},t) = -\frac{1}{\omega}(f - f^{eq}),\quad \omega \equiv \tau/\Delta t\tag{6}$$

這正是 **Imamura Eq. (1)**。$\omega$ 是 Imamura 定義的「relaxation time」（無因次化鬆弛時間，等於 $\tau/\Delta t$；若以 lattice units $\Delta t = 1$ 看，數值等於 $\tau$）。

### Step 5 — 對 (6) 做 Chapman–Enskog 展開

Taylor 展開 (6) 得 Imamura Eq. (22)：
$$\partial_t f + c_\alpha\partial_\alpha f + \frac{\Delta t}{2}(\partial_t + c_\alpha\partial_\alpha)^2 f + O(\Delta t^2) = -\frac{1}{\omega\Delta t}(f - f^{eq})\tag{7}$$

多重尺度展開 $f = f^{(0)} + \varepsilon f^{(1)} + \cdots$，逐階解：
- $O(\varepsilon^0)$：$f^{(0)} = f^{eq}$
- $O(\varepsilon^1)$：
$$f^{(1)} = -\omega\Delta t \cdot W_i\rho\Big[\frac{3 U_{i,\alpha} U_{i,\beta}}{c^2} - \delta_{\alpha\beta}\Big]\frac{\partial u_\alpha}{\partial x_\beta}\tag{8}$$
其中 $U_{i,\alpha} = c_{i,\alpha} - u_\alpha$。

這個 $f^{(1)}$ **就是 $\bar f^{(1)}$**——但 Imamura 沿用「省略上標」慣例，直接寫 $f^{(1)}$。

### Step 6 — 牆面 BC：Imamura Eq. (26)

牆面 no-slip ($u = 0$)：$f^{eq}(\rho_w, 0) = W_i\rho_w$，$U_{i,\alpha} = c_{i,\alpha}$。代回 (8)：

$$\boxed{\;f_i\big|_{bc} = W_i\rho_w\Big[1 - \omega\Delta t\Big(\frac{3 c_{i,\alpha} c_{i,\beta}}{c^2} - \delta_{\alpha\beta}\Big)\frac{\partial u_\alpha}{\partial x_\beta}\Big] + O(\delta^2)\;}\tag{9}$$

**這個 $f_i|_{bc}$ 就是 $\bar f_i|_{bc}$**，可以直接寫進 LBE 演化迴圈，**不需要再做變換**。

對應 channel + no-slip 場景的展開（$x$ 均勻 → $\partial_\eta = 0$；no-slip → $\partial u/\partial\xi = 0$）：
$$\frac{\partial u_\alpha}{\partial x_\beta}\bigg|_\text{wall} = \frac{\partial u_\alpha}{\partial \zeta}\,\zeta_{x_\beta} = \frac{du_\alpha}{dk}\,\zeta_{x_\beta},\quad \beta\in\{y,z\}$$

只剩 6 項（α∈{x,y,z}, β∈{y,z}），即 `boundary_conditions.h` 中 `ChapmanEnskogBC` 的展開。

---

## 2. omega 的雙身份（程式碼層次）

本專案有**兩個**互為倒數的 `__constant__`，雖然名字像但代表不同物理量：

| Constant | 數值 | 角色 | 對應程式碼 |
|----------|------|------|----------|
| `GILBM_omega_global` | $\tau = 3\nu/\Delta t + 1/2$ | Imamura 的 $\omega$（壁面 BC 用）| `-(omega_global)*dt` |
| `GILBM_s_visc_global` | $1/\tau = \omega_\text{rate}$ | 碰撞 rate | `(1 - s_visc)*(m - m_eq)` |

設定來源（`main.cu` L496–567）：
```c
omega_global   = (3.0 * niu / dt_global) + 0.5;   // = τ
double s_visc_val = 1.0 / omega_global;            // = 1/τ
cudaMemcpyToSymbol(GILBM_omega_global,  &omega_global, sizeof(double));
cudaMemcpyToSymbol(GILBM_s_visc_global, &s_visc_val,   sizeof(double));
```

對應 Imamura 的 viscosity 關係（論文 Eq. 10）：
$$\nu = \frac{1}{6}(2\omega - 1)c^2\Delta t = c_s^2\big(\omega - \tfrac{1}{2}\big)\Delta t\quad\Leftrightarrow\quad \omega = \tau$$

**兩個 constant 不能混用**：
- Collision rate 寫成 `(1 - omega_global)` 會錯（要的是 $1 - 1/\tau$，但寫的是 $1-\tau$）；
- 壁面 CE 寫成 `-(s_visc_global)*dt` 也會錯（要的是 $-\tau\Delta t$，但寫的是 $-\Delta t/\tau$）。

---

## 3. 為什麼不需要再做一次 transformation

可能的誤解：
> 「Imamura 沒提 transformation，所以我應該先按物理 $f$ 寫公式，再套變換 (3) 把它變成 $\bar f$ 才丟進 LBE。」

**錯**。因為：
1. Imamura Eq. (1) 本身已經是後變換形式（顯式 LBE）；
2. 從 Eq. (1) 派生的所有公式（Eq. 22 Taylor 展開、Eq. 26 牆面 BC）都是 $\bar f$ 的式子；
3. **Eq. (26) 直接給出 $\bar f|_{bc}$**，可立即代入演化迴圈。

如果硬要走「兩步路」做交叉驗證：
- (a) 用連續 BGK $\lambda$ 推出物理 $f^{(1)}$，係數為 $-\lambda\Delta t = -(\tau - \Delta t/2)\Delta t$；
- (b) 對其套變換 (3)：$\bar f^{(1)} = f^{(1)}\cdot(1 + \Delta t/(2\lambda)) = f^{(1)}\cdot\tau/\lambda$；
- (c) 結果：$-(\tau-\tfrac{\Delta t}{2})\Delta t \cdot \tfrac{\tau}{\tau-\Delta t/2} = -\tau\Delta t$ → **同 Imamura Eq. (26)**。

兩條路殊途同歸。**走 Imamura 直接路徑**是一步、走「物理 f → 再變換」是兩步——後者多了個容易出錯的中間量，無實質好處。

---

## 4. 正確係數：直接使用 `-omega*dt`

本專案重建的是 LBE 演化中的 transformed distribution $\bar f$。壁面 CE 與
checkpoint CE 重建都應直接使用 Imamura Eq. (26) 的 lattice-level 係數：

$$-\omega\Delta t,\qquad \omega = 3\nu/\Delta t + 1/2$$

其中 checkpoint 重建使用新網格的 `dt_global_new`，因此：

$$-\omega_\text{new}\Delta t_\text{new}
= -(3\nu/\Delta t_\text{new} + 1/2)\Delta t_\text{new}$$

這和 `main.cu` 中 runtime 計算 `omega_global` 的方式一致。

---

## 5. CE BC 策略對比（v1 / v2 / v3）

`WallCERegularize` 演進歷史（標頭 L117–126）：

| 版本 | 壁面 $f$ 處理 | 質量 | 動量 | 剪應力 | 結果 |
|------|--------------|------|------|-------|------|
| **v1** | 僅覆寫巨觀 $u=v=w=0$，14 個非 BC 方向保留 streaming | ✓ | ✗（假動量）| △ | 不穩定 |
| **v2** | 全 19 方向 $f = f^{eq}(\rho,0)$（純平衡態）| ✓ | ✓ | ✗ | 穩定但 L_inf ≈ 6.5e-4 卡死 |
| **v3** | 全 19 方向 $f = W_q\rho_w(1+C_q)$（CE 重建）| ✓ | ✓ | ✓ | **本版** |

v3 的 CE identity 保證守恆：
$$\sum_q W_q C_q = 0,\quad \sum_q W_q c_{q,\alpha} C_q = 0$$
（推導：把 (8) 對 $i$ 加總，利用 $\sum W_i = 1$ 與 $\sum W_i c_i = 0$；以及 Hermite 正交性。）

### v3 與其他策略的等價性

| 策略 | 描述 | 等價？ |
|------|------|--------|
| 「全 BC + lattice CE，不再變換」（**現行 v3**）| Eq. (26) 直接寫 $\bar f$ | 基準 |
| 「全 BC + NS CE，再變換」 | 先寫物理 $f$（係數 $-\lambda\Delta t$），再套變換 (3) | **數學等價於 v3**（§3 (a)(b)(c)）|
| 「部分 BC + 部分變換」 | 只對 BC 方向做 CE + 變換，其他保留 streaming | **質量不守恆 → 發散** |
| 「全 BC + 邊界 1st-order LBE」 | CE 完 + 變換完 + 用 1st-order collision | **邊界層降階 → 1st-order**（誤差透過 7-point Lagrange 傳到 bulk）|

---

## 6. 對應程式碼位置

```
gilbm/boundary_conditions.h
├── NeedsBoundaryCondition()   — 判斷 streaming 出發點是否在壁外
├── ChapmanEnskogBC()          — Eq. (26) 實作；C_alpha *= -(omega_global) * dt_global
└── WallCERegularize()         — v3：對全 19 方向覆寫 CE 重建值

gilbm/evolution_gilbm/0.shared_code.h
├── GILBM_omega_global  (= τ;      壁面 BC 用)
└── GILBM_s_visc_global (= 1/τ;    碰撞 rate)

gilbm/evolution_gilbm/0.collision.h
└── gilbm_*_collision_GTS()    — (1 - s_visc) 為 collision rate factor

gilbm/evolution_gilbm/1.algorithm1.h
├── algorithm1_step1_GTS()         — 主 fused kernel
└── algorithm1_step1_GTS_smem()    — smem cooperative load 版

main.cu
└── omega_global = 3*niu/dt_global + 0.5  (確認 = τ)
```

---

## 7. 維護備忘錄

1. **ChapmanEnskogBC 的係數固定為 `-(omega_global)*dt_global`**，不得改成 NS-level 應力係數。
2. **omega_global 與 s_visc_global 不可混用**（互為倒數，但物理意義不同）。
3. **`diagnostic_gilbm.h` 內若有 host-side CE 重算（驗證用），必須與 production 一致**：使用 `-(omega_global)*dt_global`。
4. **若擴充到非 channel 場景**：
   - 非 Poiseuille body-force（Hill, Couette）：`rho_wall` 的 0 階外推可能不足，需升階；
   - 高 Re (≥ 700)：壁面 du/dk 的 2 階 FD 不足，需升 4–6 階（標頭 L30–39 已記錄）。
5. **時間精度測試**：方案 B 融合會讓壁面 du/dk 滯後一步，這是 1 階時間誤差，在嚴格瞬態驗證時要還原 4×19 read 寫法。

---

## 8. 參考文獻

1. He X., Chen S., Doolen G. D. (1998) *A novel thermal model for the lattice Boltzmann method in incompressible limit.* J. Comput. Phys. **146**, 282–300. — He–Chen–Doolen 變換 (Eq. 3)。
2. Imamura T., Suzuki K., Nakamura T., Yoshida M. (2005) *Acceleration of steady-state lattice Boltzmann simulations on non-uniform mesh using local time step method.* J. Comput. Phys. **202**, 645–663. — 本專案直接依據；Eq. (1) LBE、Eq. (10) viscosity、Eq. (26) 壁面 CE BC。
3. Guo Z., Zheng C., Shi B. (2002) *Discrete lattice effects on the forcing term in the lattice Boltzmann method.* Phys. Rev. E **65**, 046308. — body force 二階離散與半力修正。
4. Lallemand P., Luo L.-S. (2000) *Theory of the lattice Boltzmann method.* Phys. Rev. E **61**, 6546. — MRT collision 與 NS-level CE。
5. Krüger T. et al. (2017) *The Lattice Boltzmann Method.* Springer. — 教科書統整，§3–4 講變換省略上標慣例。
6. BX-Jin et al. (2025) *Direct Numerical Simulations of Turbulent Channel and Duct Flows Using Interpolation-Based Lattice Boltzmann Method.* Flow Turbul. Combust. **115**, 1445–1471. — per-NZ CFL 對照表（CLAUDE.md `channel_testing` 流程引用）。
