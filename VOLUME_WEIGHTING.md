# VOLUME_WEIGHTING.md — 體積加權、Shoelace 面積與 Jacobian 風險

> 本文件固化本專案 mass correction 的體積加權定義，避免未來把
> finite-volume 幾何面積與 nodal Jacobian metric 混用。
>
> Repo 對應實作：
> - `evolution.h:MassCorrectionCellVolume`
> - `evolution.h:InitializeMassCorrectionWeights`
> - `evolution.h:ComputeVolumeWeightedRhoAverageRoot`
> - `gilbm/metric_terms.h:ComputeMetricTerms_Full`

---

## 0. 一句話結論

本專案的體積加權分母應由**實體 control-volume 幾何體積**定義：

$$
V_\Omega = \sum_{\text{cell}} \Delta x_{\eta}\, A_{yz,\text{cell}}
$$

目前實作使用 Shoelace formula 由外部曲線網格的四個角點直接算每個
$y$-$z$ cell 面積，再乘上 spanwise cell 長度 $\Delta x_{\eta}$。這保證分母等於**離散物理空間網格實際包住的體積**。

若改用目前的 nodal Jacobian：

$$
J_{2D} = y_\xi z_\zeta - y_\zeta z_\xi
$$

直接拼出體積加權分母，則不保證總和等於離散物理空間體積。Jacobian 公式在連續微分幾何上正確，但目前 `J_2D_h` 是用差分得到的點上 metric；它不是 finite-volume cell 面積。

---

## 1. 座標與幾何設定

本專案的方向定義：

| code index | 物理方向 | 說明 |
|------------|----------|------|
| `i` / $x$ / $\eta$ | spanwise | 均勻一維網格 `x_h[i]` |
| `j` / $y$ / $\xi$ | streamwise | 外部 Fröhlich 曲線網格 |
| `k` / $z$ / $\zeta$ | wall-normal | 外部 Fröhlich 曲線網格 |

外部網格提供二維節點：

$$
\mathbf r_{jk} =
\begin{bmatrix}
y(j,k) \\
z(j,k)
\end{bmatrix}
$$

三維體積由 spanwise 均勻方向拉伸而成。因此每個三維 cell 體積可寫成：

$$
\Delta V_{i,j,k} = \Delta x_i \, A_{j,k}^{yz}
$$

其中 $A_{j,k}^{yz}$ 是 $y$-$z$ 平面中 cell 四邊形面積。

---

## 2. Shoelace 的 per-cell 面積推導

對 cell $(j,k)$，四個角點依序取：

$$
\mathbf r_0 = (y_{j,k}, z_{j,k}),\quad
\mathbf r_1 = (y_{j+1,k}, z_{j+1,k}),\quad
\mathbf r_2 = (y_{j+1,k+1}, z_{j+1,k+1}),\quad
\mathbf r_3 = (y_{j,k+1}, z_{j,k+1})
$$

Shoelace formula 給出離散四邊形面積：

$$
A_{j,k}^{yz}
= \frac{1}{2}\left|
y_0 z_1 - z_0 y_1
+ y_1 z_2 - z_1 y_2
+ y_2 z_3 - z_2 y_3
+ y_3 z_0 - z_3 y_0
\right|
$$

三維 cell 體積為：

$$
\boxed{
\Delta V_{i,j,k}
= |x_{i+1} - x_i|\, A_{j,k}^{yz}
}
$$

這正是目前 `MassCorrectionCellVolume()` 的實作：

```cpp
const double area_yz = 0.5 * fabs(
      y0 * z1 - z0 * y1
    + y1 * z2 - z1 * y2
    + y2 * z3 - z2 * y3
    + y3 * z0 - z3 * y0);

return fabs(dx) * area_yz;
```

### Shoelace 的守恆意義

Shoelace 面積是對**目前離散四邊形網格**的精確幾何面積。把所有 cell 面積加總時，內部邊界的 oriented edge contribution 會互相抵消，最後只剩外邊界包住的總面積。因此：

$$
\sum_{j,k} A_{j,k}^{yz}
= A_{\text{discrete physical } yz}
$$

再乘上 spanwise 方向長度：

$$
\boxed{
\sum_{i,j,k} \Delta V_{i,j,k}
= L_x \, A_{\text{discrete physical } yz}
}
$$

也就是說，Shoelace 保證體積加權的分母對應到這份外部 Periodic Hill 離散網格實際包住的物理空間體積。

---

## 3. 從 per-cell 體積到 node control-volume 權重

mass correction 是在 node 上修正 density，因此需要把 cell 體積分配成 node 權重。對 interior node $(i,j,k)$，control-volume 權重定義為所有相鄰 cell 體積的八分之一：

$$
\boxed{
w_{i,j,k}
= \frac{1}{8}
\sum_{\alpha \in \{i-1,i\}}
\sum_{\beta \in \{j-1,j\}}
\sum_{\gamma \in \{k-1,k\}}
\Delta V_{\alpha,\beta,\gamma}
}
$$

這是 hexahedral finite-volume 的標準 node-centered volume 分配。對完整內部點，一個 node 周圍有 8 個 cell；對壁面點，wall-normal 方向少一側 cell；spanwise 方向是週期的，`i=3` 會包到最後一個有效 cell。

目前實作對每個有效 node 建立：

```cpp
weight += 0.125 * vol;
rho_cv_weight_h[idx] = weight;
```

有效 reduction domain 與 mass correction 實際修正的 node domain 一致：

$$
i \in [3, NX6-4),\quad
j \in [3, NYD6-4),\quad
k \in [3, NZ6-3)
$$

全域體積加權平均密度為：

$$
\boxed{
\langle \rho \rangle_V
=
\frac{\sum_{i,j,k} \rho_{i,j,k}\, w_{i,j,k}}
     {\sum_{i,j,k} w_{i,j,k}}
}
$$

mass correction 使用：

$$
\rho_{\text{modify}} = 1 - \langle \rho \rangle_V
$$

並把同一個 additive correction 加回有效 node 的 `rho_stream` 與 `f_arr[0]`。

---

## 4. Jacobian 的微分幾何推導

在連續 mapping 中，$y$-$z$ 平面由計算座標 $(\xi,\zeta)$ 映到物理座標：

$$
\mathbf r(\xi,\zeta) =
\begin{bmatrix}
y(\xi,\zeta) \\
z(\xi,\zeta)
\end{bmatrix}
$$

兩個切向量為：

$$
\mathbf r_\xi =
\begin{bmatrix}
y_\xi \\
z_\xi
\end{bmatrix},
\qquad
\mathbf r_\zeta =
\begin{bmatrix}
y_\zeta \\
z_\zeta
\end{bmatrix}
$$

把二維曲面嵌入三維，可視為：

$$
\tilde{\mathbf r}_\xi = (0, y_\xi, z_\xi),
\qquad
\tilde{\mathbf r}_\zeta = (0, y_\zeta, z_\zeta)
$$

其面積元素為：

$$
dA_{yz}
=
\left|
\tilde{\mathbf r}_\xi \times \tilde{\mathbf r}_\zeta
\right| d\xi d\zeta
=
\left|
y_\xi z_\zeta - y_\zeta z_\xi
\right| d\xi d\zeta
$$

因此連續 cell 面積為：

$$
A_{j,k}^{yz}
=
\int_{\xi_j}^{\xi_{j+1}}
\int_{\zeta_k}^{\zeta_{k+1}}
\left|J_{2D}(\xi,\zeta)\right|
d\zeta d\xi
$$

其中：

$$
\boxed{
J_{2D}
=
y_\xi z_\zeta - y_\zeta z_\xi
}
$$

三維體積則是：

$$
\Delta V_{i,j,k}
=
\Delta x_i
\int_{\xi_j}^{\xi_{j+1}}
\int_{\zeta_k}^{\zeta_{k+1}}
\left|J_{2D}(\xi,\zeta)\right|
d\zeta d\xi
$$

所以在連續數學上，Jacobian 不是錯的；它正是曲線座標面積元素。

---

## 5. 為什麼目前不直接用 `J_2D_h` 當體積權重

目前 `gilbm/metric_terms.h:ComputeMetricTerms_Full` 會用高階差分計算：

$$
y_\xi,\quad y_\zeta,\quad z_\xi,\quad z_\zeta
$$

再得到：

$$
J_{2D,h}[j,k] = y_\xi[j,k] z_\zeta[j,k] - y_\zeta[j,k] z_\xi[j,k]
$$

這個 `J_2D_h[j,k]` 是**node 上的微分 metric**，主要服務於：

- inverse Jacobian；
- contravariant velocity；
- 物理導數與 vorticity/gradient 轉換；
- GILBM semi-Lagrangian departure 計算。

它不是 cell volume，也不是 finite-volume 幾何面積。若直接用：

$$
\Delta V_{i,j,k}^{J}
\approx
\Delta x_i \, J_{2D,h}[j,k]
$$

或用簡單 node average：

$$
\Delta V_{i,j,k}^{J}
\approx
\Delta x_i\,
\frac{J_{j,k}+J_{j+1,k}+J_{j+1,k+1}+J_{j,k+1}}{4}
$$

就額外引入了「由 nodal metric 積分成 cell area」的 quadrature 誤差。

### Jacobian 分母不保證等於物理空間體積的原因

體積加權分母應該是：

$$
\sum_{i,j,k} w_{i,j,k}
$$

且它必須等於有效 domain 的物理體積。Shoelace 的分母是 cell 幾何體積加總，因此與離散物理網格一致。

但 Jacobian 版本若由 nodal `J_2D_h` 組成：

$$
V_\Omega^J
=
\sum_{\text{cell}} \Delta x_i
\mathcal Q_{\text{cell}}(J_{2D,h})
$$

其中 $\mathcal Q_{\text{cell}}$ 是某個 quadrature，例如 corner average。這個總和不保證等於：

$$
L_x \, A_{\text{discrete physical } yz}
$$

主要風險是：

1. **`J_2D_h` 是差分導數，不是幾何面積。**  
   導數 stencil 會受到非正交曲線、網格拉伸、ghost zone、邊界偏斜差分影響。局部 metric 可以很準，但它的全域加總不自動滿足 finite-volume 幾何守恆。

2. **node metric 到 cell integral 需要一致 quadrature。**  
   連續公式要求 $\int J\,d\xi d\zeta$，不是單點 `J`。若 quadrature 與 metric discretization 不構成 conservative metric identity，cell 面積會有殘差。

3. **內部誤差不會像 Shoelace edge term 那樣精確抵消。**  
   Shoelace 對 polygon 面積的加總有明確的邊界抵消結構；nodal Jacobian 的差分誤差沒有同樣的 telescoping guarantee。

4. **分母偏差會直接污染 mass correction。**  
   density correction 使用
   $$
   \langle \rho \rangle_V = \frac{\sum \rho w}{\sum w}
   $$
   若 $\sum w$ 不等於實體 domain 體積，則即使 $\rho$ 場本身合理，計算出的體積平均也會帶有幾何 normalization error。這會讓 `rho_modify = 1 - <rho>_V` 修正到錯的基準。

因此，Jacobian 公式本身沒有錯；風險在於把目前的 nodal `J_2D_h` 直接當成 finite-volume 體積分母。

---

## 6. 什麼情況下 Jacobian 也可以用

Jacobian 可以作為體積權重，但必須使用一致的 cell integral：

$$
\Delta V_{i,j,k}
=
\Delta x_i
\int_{\text{cell}} |J_{2D}|\,d\xi d\zeta
$$

可行條件包括：

1. 有解析 mapping $y(\xi,\zeta), z(\xi,\zeta)$，並用足夠高階 quadrature 積分；
2. 使用 conservative metric discretization，使離散 metric identity 與 cell 面積加總一致；
3. 對每個 cell 先得到一致的 $\Delta V$，再用同樣的 $1/8$ 分配回 node。

若只是把目前 `J_2D_h[j,k]` 乘上 $\Delta x$ 或做簡單 corner average，則不滿足上述條件。

---

## 7. 維護規則

1. **mass correction 的 volume denominator 以 Shoelace cell volume 為基準。**
2. **不要把 `J_2D_h` 直接替換成 `rho_cv_weight_h` 的來源。**
3. 若未來要改成 Jacobian 積分，必須先建立 per-cell $\Delta V$，並驗證：
   $$
   \left|
   \frac{\sum_{\text{cell}} \Delta V - L_x A_{\text{discrete physical } yz}}
        {L_x A_{\text{discrete physical } yz}}
   \right|
   $$
   足夠小，且跨 MPI rank、週期邊界、wall-normal 邊界都一致。
4. `J_2D_h` 仍應保留作為 GILBM metric / inverse Jacobian 用途；它與 mass correction 的 conservative volume weight 是不同層級的量。

