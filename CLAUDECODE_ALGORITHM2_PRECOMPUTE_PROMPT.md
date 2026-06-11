# Claude Code Task: GILBM Algorithm2 Departure Coordinate Precompute

## Project Scope

Please work in this project:

```text
/home/s8313697/5.Re10595/Edit6_5600DNS
```

Read the existing GILBM implementation before editing, especially:

```text
gilbm/precompute.h
gilbm/interpolation_gilbm.h
gilbm/evolution_gilbm/0.shared_code.h
gilbm/evolution_gilbm/1.algorithm1.h
main.cu
memory.h
variables.h
```

The core implementation must land in:

```text
gilbm/precompute2.h
gilbm/evolution_gilbm/2.algorithm2.h
```

These two files are the only places where the new Algorithm2 mathematics and kernel logic should be implemented.

Other files may be changed only for minimal integration wiring, such as adding includes, feature flags, allocation/copy/free of the new coordinate table, dispatching to Algorithm2 behind a disabled-by-default flag, and adding test build targets. Do not use integration edits to change existing Algorithm1 behavior.

Do not delete, rewrite, or simplify the existing Algorithm1 implementation. Algorithm1 is the reference implementation and must remain available for numerical equivalence validation.

## Objective

Create a new GILBM Algorithm2 path that precomputes only the final departure coordinates for the y-z plane, then uses those coordinates inside the streaming kernel.

The intended architecture is a shared coordinate-table interface for both GILBM and ITBLBM:

```text
GILBM:
  uses the current GILBM Jacobian / contravariant velocity / RK2 method
  to generate final (r,s) or (t_xi,t_zeta)

ITBLBM:
  uses the ITBLBM isoparametric / inverse-mapping method
  to generate final (r,s) or (t_xi,t_zeta)

common streaming kernel concept:
  consume the same coordinate-table layout
  compute 7-point Lagrange weights on the fly
  interpolate f using the same separable tensor-product logic
```

Therefore the difference between GILBM and ITBLBM should be isolated to how the coordinate table is generated. The downstream interpolation kernel should be able to consume the same coordinate-only memory format.

The current Algorithm1 computes the GILBM departure point inside the fused CUDA kernel for every `i,j,k,q`. However, the y-z departure coordinate depends only on:

- grid metric / Jacobian data
- `j,k`
- y-z direction class
- `dt_global`

It does not depend on:

- x-index `i`
- distribution function `f`
- macroscopic fields after initialization, as long as the grid and `dt_global` are unchanged

Therefore Algorithm2 should precompute the final coordinate once and reuse it in the kernel.

## Non-Negotiable Requirements

1. Keep Algorithm1 intact as the reference path.
2. Do not import ITBLBM's physical-space Newton inverse mapping into Algorithm2.
3. Algorithm2 must use the same mathematical path as current GILBM Algorithm1.
4. Precompute only final departure coordinates, not weights.
5. GILBM precompute must output one final coordinate pair for every `(j,k,yz_class)`; it is not sufficient to precompute only Jacobian or metric terms.
6. Do not precompute 7-point Lagrange weights.
7. Do not precompute 49 tensor weights.
8. Do not store a separate coordinate table for all 19 D3Q19 directions.
9. Use only the 9 unique y-z direction classes.
10. Add strict numerical equivalence validation against Algorithm1.
11. Add unit tests. A validation-only runtime diagnostic is not enough.
12. Do not modify the current Algorithm1 calculation kernel except for strictly necessary declarations or includes. Any change to existing Algorithm1 numerical logic is out of scope.
13. Keep the Algorithm2 coordinate-table memory format compatible with a future ITBLBM coordinate-only output path.

## Direction Compression

D3Q19 has only 9 unique y-z direction classes:

```text
0: ( 0,  0)
1: (+1,  0)
2: (-1,  0)
3: ( 0, +1)
4: ( 0, -1)
5: (+1, +1)
6: (-1, +1)
7: (+1, -1)
8: (-1, -1)
```

Algorithm2 must map each D3Q19 `q` to one of these classes. Directions with the same `(e_y,e_z)` must share the same precomputed y-z departure coordinate, regardless of `e_x`.

The coordinate table should be conceptually:

```text
NYD6 * NZ6 * 9 * 2
```

where the two stored values are the final computational coordinates used for the 7-point Lagrange interpolation.

This table is the required precompute output. For GILBM, `precompute2.h` must compute this table for every local y-z cell and every y-z direction class. It must not stop at Jacobian, metric, or contravariant velocity precomputation.

Example coordinate-only struct:

```cpp
struct GILBM_DepartureCoord2 {
    double t_xi;
    double t_zeta;
    unsigned char flags;
};
```

The exact struct name may differ, but the storage must stay coordinate-only. Do not store `L_xi[7]`, `L_zeta[7]`, or `L_xi * L_zeta`.

This same coordinate-only layout is also the target format for ITBLBM if ITBLBM is later routed through the same optimized interpolation kernel. Current ITBLBM code may store more data, but this task should not copy that larger memory format into GILBM.

## Mathematical Equivalence Requirement

Algorithm2 precompute must reproduce the same final coordinates that Algorithm1 computes inside the kernel.

This means the precompute implementation must follow Algorithm1's current GILBM path:

1. Same RK2 departure-point logic as `gilbm_rk2_displacement()`.
2. Same use of metric / inverse Jacobian data.
3. Same `dt_global`.
4. Same base stencil convention.
5. Same clamp rules.
6. Same treatment of zeta ghost / folding behavior.
7. Same interpretation of `t_xi` and `t_zeta`.

Do not replace this with ITBLBM's physical-space departure:

```cpp
yd = y(j,k) - ey * dt;
zd = z(j,k) - ez * dt;
```

That is a different mathematical path and is not acceptable for this Algorithm2 task.

The intended abstraction is:

```text
coordinate generator differs:
  GILBM generator  -> GILBM-equivalent (r,s)
  ITBLBM generator -> ITBLBM-equivalent (r,s)

coordinate consumer is shared:
  read (r,s)
  build L_xi[7], L_zeta[7]
  use separable interpolation
```

## Kernel Behavior

In `gilbm/evolution_gilbm/2.algorithm2.h`, create a new Algorithm2 kernel path that:

1. Keeps q=0 behavior identical to Algorithm1.
2. Keeps x-only directions q=1,2 compatible with the existing x interpolation path.
3. For y-z moving directions:
   - map `q` to the 9-class y-z direction ID
   - load only `t_xi,t_zeta` from the precomputed table
   - compute `L_xi[7]` and `L_zeta[7]` inside the kernel
   - perform the same interpolation as Algorithm1
4. Keep boundary-condition decisions and collision behavior equivalent to Algorithm1.
5. Keep MPI buffer / interior split compatibility in mind, but do not perform a large unrelated rewrite.

## Required Validation

Add strict validation that directly compares Algorithm1 and Algorithm2 departure coordinates.

This validation must compare the coordinates themselves, not only final macroscopic fields.

At minimum, implement a debug/diagnostic validation path that reports:

```text
max abs error for t_xi
max abs error for t_zeta
RMS error for t_xi
RMS error for t_zeta
location of max error: j,k,yz_class
MPI global max and RMS, if MPI is active
```

The validation must be repeated in more than one place:

1. Immediately after precompute on host.
2. After upload/download round-trip, if the table is transferred to GPU.
3. Before first Algorithm2 launch in debug mode.
4. Optionally at a configurable interval, for example `GILBM_ALGO2_VALIDATE_INTERVAL`.

If the host and device results are not bitwise identical, document why. Acceptable reasons may include:

- host/device floating-point evaluation order
- FMA behavior
- different inlining or contraction

However, any non-roundoff-level discrepancy must be treated as a failure.

Use explicit tolerances. Suggested starting points:

```text
coordinate max abs tolerance: 1.0e-12 for double
coordinate RMS tolerance:     1.0e-13 for double
```

If these are too strict due to host/device differences, justify the revised threshold in comments and diagnostics.

## Secondary End-to-End Validation

In addition to direct coordinate comparison, add an optional short-run equivalence check:

1. Start from the same initialized distribution.
2. Run one step with Algorithm1.
3. Run one step with Algorithm2.
4. Compare:
   - `f_post`
   - `rho`
   - `ux`
   - `uy`
   - `uz`
5. Report max and RMS differences.

This is secondary validation. It does not replace direct coordinate validation.

## Required Unit Tests

Add unit tests for the new Algorithm2 precompute logic. Do not rely only on full simulation output.

If the repository already has a test framework, use it. If it does not, add a minimal test target or standalone test executable with clear build/run instructions.

At minimum, add tests for:

1. **Y-Z direction class mapping**
   - Every D3Q19 `q` maps to the expected 9-class y-z direction.
   - Directions sharing the same `(e_y,e_z)` share the same class.

2. **Coordinate table indexing**
   - Index calculation for `(class,j,k)` is correct.
   - No accidental 19-direction storage is used.

3. **Lagrange coordinate interpretation**
   - Stored `t_xi,t_zeta` produce the same `L_xi[7]`, `L_zeta[7]` as Algorithm1 would compute from its immediate coordinate path.
   - The test must not validate precomputed weights, because weights must not be stored.

4. **Identity-grid or simple-grid equivalence**
   - On a simple synthetic metric/grid where the expected departure is analytically obvious, Algorithm2 coordinates match Algorithm1-derived coordinates.

5. **Boundary and clamp behavior**
   - Test edge `j,k` locations where stencil base or clamping matters.
   - Confirm Algorithm2 uses the same valid stencil range as Algorithm1.

6. **No-weight-storage invariant**
   - Add a compile-time or runtime check that the Algorithm2 table stores only coordinate data plus minimal flags.
   - The test should fail if `L_xi[7]`, `L_zeta[7]`, or 49 tensor weights are added to the table.

The unit tests must be runnable independently of a full production simulation.

## Integration Guidance

Keep integration minimal and explicit. The main implementation restriction is:

```text
core new logic:
  gilbm/precompute2.h
  gilbm/evolution_gilbm/2.algorithm2.h

allowed wiring-only edits:
  variables.h
  memory.h
  main.cu
  evolution.h
  gilbm/evolution_gilbm/0.shared_code.h
  build/test files, if needed
```

Allowed wiring-only edits include:

1. Add feature flags.
2. Add includes for `precompute2.h` and `2.algorithm2.h`.
3. Allocate, free, upload, and download the Algorithm2 coordinate table.
4. Call Algorithm2 precompute and validation routines.
5. Dispatch to Algorithm2 only when the Algorithm2 flag is enabled.
6. Add unit test source files and build rules.

Not allowed in wiring files:

1. Change Algorithm1's RK2 departure-point math.
2. Change Algorithm1 interpolation math.
3. Change collision, boundary, MPI, or statistics behavior for the default path.
4. Refactor unrelated code.
5. Make Algorithm2 the default path before equivalence validation passes.

Expected integration points may include:

```text
variables.h
memory.h
main.cu
evolution.h
gilbm/evolution_gilbm/0.shared_code.h
```

Use feature flags such as:

```cpp
#define USE_GILBM_ALGORITHM2 0
#define GILBM_ALGO2_VALIDATE 0
```

or an equivalent existing configuration style.

Algorithm1 must remain the default path unless explicitly enabled.

## Expected Performance Tradeoff

The desired tradeoff is:

```text
additional memory:
  NYD6 * NZ6 * 9 * 2 coordinate scalars

removed repeated kernel work:
  RK2 departure-point calculation repeated for every x-index i and every time step

kept in kernel:
  7-point Lagrange weight generation:
    9 * 7 * 2 scalar weight calculations per y-z cell pattern
```

Do not move the `9 * 7 * 2` Lagrange weights into global memory. That would increase memory traffic and is not the intended optimization.

The interpolation should exploit separability:

```text
full tensor weight view:
  9 * 49 coefficients

separable generation view:
  9 * (7 + 7) coefficients
```

This optimization reduces coefficient generation and avoids storing 49 tensor weights per direction. It does not eliminate the need to sample the 7x7 interpolation stencil from `f`.

## Deliverables

Please provide:

1. `gilbm/precompute2.h`
2. `gilbm/evolution_gilbm/2.algorithm2.h`
3. Minimal integration patches needed to compile and run Algorithm2 behind a flag
4. Direct coordinate equivalence validation
5. Secondary one-step field equivalence validation
6. Unit tests for the new precompute and indexing logic
7. Clear instructions for how to build and run the tests
8. A short summary of memory cost and expected kernel work reduction
9. A short note explaining how the coordinate-only table can also be used by ITBLBM if ITBLBM later changes its precompute output to the same `(r,s)` format

Algorithm2 is acceptable only if the direct departure-coordinate validation proves numerical equivalence to Algorithm1 within strict double-precision tolerances.
/home/s8313697/5.Re10595/Edit6_5600DNS/gilbm/evolution_gilbm/2.algorithm2.h 
/home/s8313697/5.Re10595/Edit6_5600DNS/gilbm/precompute2.h only can change 
