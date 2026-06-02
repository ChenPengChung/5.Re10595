# ITB-LBM Streaming Implementation Plan

This document defines the first implementation pass for the ITB-ISLBM
streaming path. The goal is to replace only the interpolation/streaming
part of the current GILBM kernel while keeping collision, MPI, wall BC,
statistics, checkpointing, and the current `dt_global` path unchanged.

## 1. Scope

First pass:

- Add a compile-time switch:
  - `USE_ITBLBM_STREAMING=0`: current GILBM RK2/contravariant streaming.
  - `USE_ITBLBM_STREAMING=1`: ITB precomputed physical-space streaming.
- Precompute ITB interpolation coefficients on host after the y-z coordinate
  ghost exchange.
- Upload coefficient tables to GPU.
- In the fused kernel, replace only the non-wall interpolation path.

Not in first pass:

- Do not remove metric/Jacobian computation yet.
- Do not replace `ComputeGlobalTimeStep()` yet.
- Do not replace `NeedsBoundaryCondition()` or `ChapmanEnskogBC()` yet.
- Do not use mirror symmetry to skip Newton solves until diagnostics prove it
  is safe for the periodic-hill grid.

## 2. File Layout

Planned files:

```text
itblbm/
  ITB_IMPLEMENTATION_PLAN.md
  isoparametric_coeff.h
  isoparametric_precompute.h
  isoparametric_streaming.h
```

Planned responsibilities:

- `isoparametric_coeff.h`
  - Compile-time constants.
  - D3Q19 to y-z projection class mapping.
  - Coefficient structs.
  - Diagnostic structs.
- `isoparametric_precompute.h`
  - Host-side shape functions.
  - Host-side Newton-Raphson inverse mapping.
  - Host-side ghost-consistent geometry getter.
  - Coefficient generation and diagnostics.
- `isoparametric_streaming.h`
  - Device-side coefficient lookup.
  - Device-side ITB streaming helper.
  - Runtime interpolation using folded coefficients.

## 3. Direction Compression

The y-z ITB coefficients depend only on `(e_y,e_z)`, not on `e_x`.
Therefore the coefficient table must be stored by y-z projection class, not
by all 19 lattice directions.

Projection classes:

```text
id 0: ( 0,  0)
id 1: (+1,  0)
id 2: (-1,  0)
id 3: ( 0, +1)
id 4: ( 0, -1)
id 5: (+1, +1)
id 6: (-1, +1)
id 7: (+1, -1)
id 8: (-1, -1)
```

D3Q19 mapping:

```cpp
ITB_YZ_ID[19] = {
    0, 0, 0,
    1, 2, 3, 4,
    1, 1, 2, 2,
    3, 3, 4, 4,
    5, 6, 7, 8
};
```

This compression is exact.

The `id=0` class is the center y-z position. It can be treated as a direct
Kronecker delta and does not need Newton.

## 4. Recommended First-Pass Interpolation Order

Use a hybrid path:

```text
x direction:   7-point Lagrange on uniform x grid
y-z direction: 3x3 quadratic isoparametric element
```

Rationale:

- x is uniform, so 7-point Lagrange weights are cheap and globally shared.
- y-z is curved, so ITB handles physical-space departure with local inverse
  mapping.
- Runtime read count is much lower than 7x7x7 while keeping x-direction
  accuracy conservative.

Approximate reads per grid point for D3Q19:

```text
q=0:          1
q=1,2:        2 * 7      = 14
ex=0 y-z:     8 * 9      = 72
ex!=0 y-z:    8 * 7 * 9  = 504
total:        about 591 reads
```

Current 7-point GILBM interpolation is about 3151 reads per grid point before
cache effects.

## 5. Coefficient Structs

Use separated 1D weights rather than storing full tensor-product weights.

First-pass y-z coefficient:

```cpp
struct ITB_YZCoeff {
    int j0;              // first j row in the 3-row stencil
    int k_idx[3];        // actual k rows after ghost folding
    double wr[3];        // shape weights in j/local-r direction
    double ws[3];        // folded shape weights in k/local-s direction
    unsigned char flags; // diagnostic/classification bits
};
```

Table shape:

```text
ITB_YZCoeff itb_yz_coeff[9 * NYD6 * NZ6]
```

The runtime y-z interpolation is:

```text
sum_sj sum_sk wr[sj] * ws[sk] * f[j0+sj][k_idx[sk]]
```

x-direction weights:

```cpp
__constant__ double ITB_WX[2][7]; // 0: ex=+1, 1: ex=-1
```

For backward streaming:

```text
t_x(ex) = 3 - ex * dt_global / dx
```

where `dx = LX / (NX6 - 7)`.

Runtime x stencil:

```text
i0 = i - 3
x rows = i0, i0+1, ..., i0+6
```

Do not clamp the x stencil in the ITB helper. The current solver already
maintains x/spanwise periodic ghost zones through the existing periodic path.
Clamping here would silently change the periodic streaming behavior.

## 6. Ghost-Consistent Geometry

The ITB Newton solve must use the same virtual geometry that runtime
interpolation uses for `f`.

Do not directly trust the existing k-ghost coordinate values for ITB Newton.
`ReadExternalGrid_YZ()` currently fills coordinate ghost rows by repeated
linear extrapolation, while the streaming path uses `GHOST_EXTRAP_ORDER`.

For ITB, define a host geometry getter:

```text
geom_eff(arr, j, k)
```

Rules for `GHOST_EXTRAP_ORDER=2`:

```text
bottom k=2:
  F2 = 3*F3 - 3*F4 + F5

top k=NZ6-3:
  FN3 = 3*FN4 - 3*FN5 + FN6
```

Rules for `GHOST_EXTRAP_ORDER=3`:

```text
bottom k=2:
  F2 = 4*F3 - 6*F4 + 4*F5 - F6

top k=NZ6-3:
  FN3 = 4*FN4 - 6*FN5 + 4*FN6 - FN7
```

Use `geom_eff(y_2d_h, gj, gk)` and `geom_eff(z_h, gj, gk)` for every
isoparametric element node in the Newton solve.

## 7. Ghost Folding For Runtime

Fold k-ghost extrapolation into the stored k weights so the runtime path has
no wall-adjacent ghost branch.

Interior stencil:

```text
k_idx = {k-1, k, k+1}
ws    = {w0,  w1, w2}
```

Bottom wall-adjacent stencil:

Original centered stencil uses `{2,3,4}`. With quadratic extrapolation:

```text
F2 = 3*F3 - 3*F4 + F5

w0*F2 + w1*F3 + w2*F4
= (3*w0 + w1)*F3 + (-3*w0 + w2)*F4 + w0*F5
```

Store:

```text
k_idx = {3, 4, 5}
ws    = {3*w0 + w1, -3*w0 + w2, w0}
```

Top wall-adjacent stencil:

Original centered stencil uses `{NZ6-5,NZ6-4,NZ6-3}`. With quadratic
extrapolation:

```text
FN3 = 3*FN4 - 3*FN5 + FN6
```

Store rows:

```text
k_idx = {NZ6-6, NZ6-5, NZ6-4}
```

and fold the weights consistently:

```text
if original weights are:
  w0*F(NZ6-5) + w1*F(NZ6-4) + w2*F(NZ6-3)

then:
  ws for {NZ6-6,NZ6-5,NZ6-4}
  = {w2, w0 - 3*w2, w1 + 3*w2}
```

If `GHOST_EXTRAP_ORDER=3`, use the cubic formula and a 4-row folded stencil.
For the first pass, keep `GHOST_EXTRAP_ORDER=2` unless explicitly changed.

## 8. Newton-Raphson Inverse Mapping

For each moving y-z class `(ey,ez)` and each `(j,k)`:

```text
yd = y[j,k] - ey * dt_global
zd = z[j,k] - ez * dt_global
```

Use a centered 3x3 element:

```text
j nodes: j-1, j, j+1
k nodes: k-1, k, k+1
```

k ghost nodes must be provided through `geom_eff()`.

Quadratic 1D shape functions on `[-1,0,+1]`:

```text
L0(r) = 0.5*r*(r-1)
L1(r) = 1-r*r
L2(r) = 0.5*r*(r+1)
```

Derivatives:

```text
dL0/dr = r - 0.5
dL1/dr = -2*r
dL2/dr = r + 0.5
```

Mapping:

```text
Y(r,s) = sum_a sum_b L_a(r) L_b(s) y_ab
Z(r,s) = sum_a sum_b L_a(r) L_b(s) z_ab
```

Residual:

```text
R = [Y(r,s)-yd, Z(r,s)-zd]
```

Local Jacobian:

```text
J = [dY/dr dY/ds
     dZ/dr dZ/ds]
```

Newton update:

```text
[r,s] <- [r,s] - inv(J) * R
```

Safeguards:

- Maximum iterations: 12.
- Convergence tolerance:
  - update norm: `abs(dr)+abs(ds) < 1e-12`
  - residual norm: `abs(Ry)+abs(Rz) < 1e-11`
- Minimum determinant: `abs(detJ) > 1e-14`.
- If Newton step is too large, damp it:
  - If `abs(dr)+abs(ds) > 1.0`, multiply `(dr,ds)` by `0.5`.
  - Repeat damping up to 4 times if residual grows.
- Clamp only for the next initial guess if necessary; do not silently accept a
  clamped unconverged solution.

Initial guess:

```text
r0 = 0
s0 = 0
```

Optional improvement:

- Use the local affine inverse at the center as the initial guess.
- Use mirror-opposite solution as an initial guess only after direct Newton
  for the source direction is available.

Failure handling:

- Mark coefficient `flags |= ITB_COEFF_NEWTON_FAILED`.
- Fall back to direct current-node Kronecker y-z weight for that coefficient
  in the first debug build, or abort if strict mode is enabled.
- Always print diagnostics. Do not silently continue in production mode if
  failures exist.

## 9. Mirror-Symmetry Diagnostics

Mirror symmetry is not assumed to be exact for the periodic-hill grid.

For each opposite pair:

```text
(+1,  0) <-> (-1,  0)
( 0, +1) <-> ( 0, -1)
(+1, +1) <-> (-1, -1)
(-1, +1) <-> (+1, -1)
```

Compute both directions directly by Newton. Then compare direct opposite
weights with mirrored source weights.

For a 3x3 tensor-product stencil:

```text
mirror(w_src)[a,b] = w_src[2-a, 2-b]
```

Because ITB stores separated folded weights, compute the diagnostic on the
unfolded raw 3x3 weights before k-ghost folding, then separately report folded
weight mismatch at wall-adjacent rows.

Metrics:

```text
max_abs_raw_mirror_err
rms_raw_mirror_err
max_abs_folded_mirror_err
rms_folded_mirror_err
count_raw_err_gt_1e-12
count_raw_err_gt_1e-10
count_raw_err_gt_1e-8
worst pair, j, k, weight index
```

Warnings:

- Print warning if `max_abs_raw_mirror_err > 1e-12`.
- Print stronger warning if `max_abs_raw_mirror_err > 1e-10`.
- Print fatal recommendation if `max_abs_raw_mirror_err > 1e-8`:
  mirror compression must not be used.

Warning text should be explicit:

```text
[ITB][WARN] Periodic-hill y-z grid is not mirror-symmetric enough for
            coefficient mirroring. Direct Newton coefficients will be used.
            max_raw_mirror_err=...
```

The first pass must always use direct Newton coefficients. Mirror diagnostics
are for information only.

## 10. Core Diagnostics

After precompute, print a rank-local and MPI-global summary:

```text
[ITB] coefficient table:
  yz classes                 = 9
  active moving classes       = 8
  coeff count per rank        = 9*NYD6*NZ6
  interpolation order         = x7_yz3x3
  ghost extrapolation order   = GHOST_EXTRAP_ORDER
```

Newton diagnostics:

```text
newton_total
newton_converged
newton_failed
newton_max_iter_used
newton_avg_iter
max_residual
max_update
min_abs_detJ
count_abs_r_gt_1
count_abs_s_gt_1
count_abs_r_or_s_gt_1p05
worst_residual class,j,k,r,s
worst_detJ class,j,k,detJ
```

Weight diagnostics:

```text
max_abs_sumw_minus_1_raw
max_abs_sumw_minus_1_folded
max_abs_weight_raw
max_abs_weight_folded
count_negative_weight_raw
count_large_weight_abs_gt_2
```

Warning thresholds:

```text
newton_failed > 0:
  fatal in strict mode; warning in debug fallback mode

max_residual > 1e-10:
  warning

max_abs_sumw_minus_1_folded > 1e-12:
  warning

count_abs_r_or_s_gt_1p05 > 0:
  warning; departure may be outside centered element

min_abs_detJ < 1e-12:
  warning
```

## 11. Integration Points

Main setup order:

1. `ReadExternalGrid_YZ(y_2d_h, z_h, myid)`
2. current metric computation and coordinate MPI ghost exchange
3. `ComputeGlobalTimeStep(...)`
4. ITB coefficient precompute using final `dt_global`
5. upload ITB coefficients and x weights
6. initialize distributions as before

Fused kernel changes:

- Add `const ITB_YZCoeff *itb_yz_coeff_d` argument behind
  `USE_ITBLBM_STREAMING`.
- In the non-wall interpolation branch:

```cpp
#if USE_ITBLBM_STREAMING
    f_streamed = itb_stream_q(q, i, j, k, f_post_read, itb_yz_coeff_d);
#else
    current GILBM RK2 interpolation path
#endif
```

Do not change the wall BC path in the first pass.

## 12. First Test Plan

Build tests:

- `USE_ITBLBM_STREAMING=0`: must remain bitwise-equivalent or numerically
  equivalent to current build.
- `USE_ITBLBM_STREAMING=1`: must compile without changing non-ITB paths.

Small-run diagnostics:

- Run a small grid or one-rank case first.
- Check coefficient diagnostics before any time stepping.
- Run 10 to 100 steps.
- Check:
  - `rho_min/rho_max`
  - NaN monitor
  - mass drift
  - wall BC path still triggered as expected
  - short-step difference versus GILBM baseline

Promotion criteria:

- zero Newton failures
- no `|r|` or `|s|` beyond `1.05`
- `max_abs_sumw_minus_1_folded <= 1e-12`
- no NaN in short run
- mass drift not worse than baseline by more than the chosen tolerance
