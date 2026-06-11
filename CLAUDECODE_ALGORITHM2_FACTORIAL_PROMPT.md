# Claude Code Task: GILBM/ITBLBM Departure-Coordinate Factorial Study (Algorithm2)

> Finalized spec (2026-06-11). Supersedes `CLAUDECODE_ALGORITHM2_PRECOMPUTE_PROMPT.md`
> (kept for reference). Folds in: the 3-way verification findings (me + codex +
> 8-dimension workflow), the dual storage-mode design, and the 2×2 factorial.

---

## 0. Goal

Build ONE unified, isolated testbed that precomputes the semi-Lagrangian **departure
coordinates** once and consumes them in the streaming kernel, and use it to run a clean
**2×2 factorial** experiment:

```
                STORE_MODE = COORDS              STORE_MODE = WEIGHTS
              (read 2 doubles, regen in-kernel) (read 7+7 weights, pure MAC)
GEN = GILBM    GILBM-B                           GILBM-A
(RK2)
GEN = ITB      ITB-B                             ITB-A   (≈ current Edit9 ITBLBM)
(Newton)
```

Two orthogonal factors:
- **GEN_MODE** = which departure-point generator: `GILBM_RK2` (current GILBM math) or
  `ITB_NEWTON` (ITBLBM physical-space inverse map).
- **STORE_MODE** = what the table stores: `COORDS` (final `(t_xi,t_zeta)`, regenerate
  Lagrange weights in-kernel) or `WEIGHTS` (pre-baked `wr[7]`,`ws[7]` + folded stencil,
  pure multiply-accumulate).

**The whole point of the factorial:** hold the *consumer kernel* identical across all four
cells so that (a) `GILBM vs ITB` isolates the departure-math difference and (b) `A vs B`
isolates the store-vs-recompute performance difference — apples-to-apples, no confound.

**The two axes are DIFFERENT IN KIND (critical):**
- **STORE (A vs B) is the EFFICIENCY axis.** It decides what the per-step kernel reads and
  computes (pure MAC vs read-coords + regen weights), so it is what moves `Iter_ms`.
- **GEN (GILBM vs ITB) is NOT an efficiency axis — it is the ACCURACY/physics axis.** The
  generator runs ONCE at initialization to fill the table; once the table exists, the
  per-step consumer kernel reads the SAME bytes/cell and gathers f from the SAME geometry-
  fixed stencil (`bj=j-3`, `bk=bk_precomp[k]`) regardless of which generator produced the
  values — the departure value only changes the *weights*, never the memory layout or the
  ops. Therefore, **at a fixed STORE, GILBM and ITB have identical per-step runtime by
  construction** (`GILBM-A ≈ ITB-A`, `GILBM-B ≈ ITB-B`). What GEN actually changes is the
  departure-coordinate VALUES → flow accuracy (RK2 computational-space approx vs Newton
  physical-space inverse map), plus a one-time precompute cost. Efficiency therefore has
  only TWO levels (A, B); the GEN axis is judged by accuracy (departure values, and
  ultimately flow statistics vs Krank DNS), not by `Iter_ms`.

`Algorithm1` (current GILBM in-kernel RK2 recompute) is the **equivalence reference and
timing baseline** and MUST stay intact and runnable.

### Empirical anchors (same grid NX321 NY641 NZ321, jp=32, 32×H200; verified from live logs)
- GILBM Algorithm1 (recompute geometry in-kernel): **Iter_ms ≈ 5.16 ms**, ~17,900 MLUPS
  (560/GPU); live kernel breakdown: **Interior S1 = 3.47 ms (67%)**, Buffer 1.63 ms,
  MPI 2.04 ms (41% hidden), periodicSW 0.03 ms.
- Edit9 ITBLBM (reads ~152 B/cell folded-weight table, pure MAC): **Iter_ms ≈ 3.50 ms**,
  ~26,400 MLUPS (825/GPU). => the table-consumer kernel is the FASTER one; "table read is
  read-bound / evicts f" is empirically false. The ~1.66 ms GILBM penalty is dominated by
  the in-kernel RK2 geometry recompute that Algorithm2 removes.

Realistic Algorithm2 target: between 3.50 and 5.16 ms. GILBM-A should approach the ~3.50 ms
floor; GILBM-B sits slightly above (retains in-kernel weight regen + ghost). Confirm by
benchmark, NOT by op-counts (project Mandatory Efficiency Rule).

---

## 1. HARD SAFETY (non-negotiable)

- A live Slurm job (`Edit6_5600DNS`, jobid 88688) is RUNNING in
  `/home/s8313697/5.Re10595/Edit6_5600DNS`.
- ALL build + test + benchmark happens in an **isolated git worktree** (provided:
  `/home/s8313697/5.Re10595/Edit6_algo2_factorial`, branch `Edit6_algo2_factorial`).
- NEVER rebuild `a.out`, touch `restart/`, overwrite checkpoints, or launch benchmarks in
  the live directory. Source-only reading of the live dir is fine.
- Use `./run job-guard scancel` only; never bare `scancel`. (You will not need it.)

---

## 2. Shared architecture — ONE skeleton, not four kernels

All four cells share:

### 2a. One table struct (per-rank, indexed `[yz_class][j][k]`)
9 y-z projection classes × `NYD6 × NZ6` per rank. Storage class is **`__device__` global
memory** (e.g. `cudaMalloc`, like `xi_y_d`) — the table is ~1.27 MiB/rank (COORDS) to
~12 MiB/rank (WEIGHTS), both **far over the 64 KB `__constant__` limit**, so do NOT use
`__constant__`/`cudaMemcpyToSymbol` (the existing `GILBM_L_eta_shared` / `GILBM_MRT_K`
constant-memory precedents do NOT apply here — they are tiny). Declare the device pointer in
`gilbm/evolution_gilbm/0.shared_code.h`; alloc/free in `memory.h`.

```cpp
// STORE_MODE = COORDS  (B): 2 doubles + flags
struct GILBM_Depart_Coords { double t_xi; double t_zeta; unsigned char flags; };
// STORE_MODE = WEIGHTS (A): pre-baked separable weights + folded stencil
struct GILBM_Depart_Weights {
    int    j0;            // xi stencil base (= j-3, recomputable; stored for parity w/ ITB)
    int    k_idx[7];      // zeta stencil indices, ghost ALREADY folded
    double wr[7];         // L_xi  (xi  Lagrange weights)
    double ws[7];         // L_zeta (zeta Lagrange weights, ghost folded into k_idx)
    unsigned char flags;
};
```

### 2b. One consumer kernel (parametrized by STORE_MODE)
The inner per-direction interpolation is the SAME f-gather for both modes; only the weight
source differs:
```cpp
#if STORE_MODE == COORDS
    // read (t_xi,t_zeta) -> lagrange_7point_coeffs (DEVICE helper) -> L_xi,L_zeta
    //   -> gilbm_ghost_zone_extrapolate(interp2, bk) -> zeta collapse
#else // WEIGHTS
    // read wr[7],ws[7],k_idx[7] -> pure MAC over f-stencil (no regen, no in-kernel ghost)
#endif
```
The x-direction (eta) weights stay as today: `GILBM_L_eta_shared[2][7]` in `__constant__`
(uniform x, tiny, already shared by `sign(e_x)`).

### 2c. Two generators (precompute side), same output format
- `GEN = GILBM_RK2`: reproduce Algorithm1's RK2 math EXACTLY (§4). Emits `(t_xi,t_zeta)`;
  for WEIGHTS mode, additionally fold to `wr/ws/k_idx`.
- `GEN = ITB_NEWTON`: port Edit9's physical-space Newton inverse map
  (`Edit9_ITBISLBM5600/itblbm/isoparametric_precompute.h`) into the SAME table format and
  the SAME coordinate contract (§5). Emits the same struct.

### 2d. Two compile flags
```cpp
#define USE_GILBM_ALGORITHM2   0   // 0 = Algorithm1 (default), 1 = Algorithm2 path
#define GILBM_ALGO2_GEN        GILBM_RK2   // GILBM_RK2 | ITB_NEWTON
#define GILBM_ALGO2_STORE      COORDS      // COORDS | WEIGHTS
#define GILBM_ALGO2_VALIDATE   0           // 1 = run coordinate/field equivalence checks
```
`GEN × STORE` = the 4 cells. Algorithm1 stays default. Algorithm2 never becomes default
until equivalence passes.

### Core files (new): `gilbm/precompute2.h` (generators + table build + validation) and
`gilbm/evolution_gilbm/2.algorithm2.h` (consumer kernel + dispatch). Keep `Algorithm1`
math untouched.

---

## 3. Implementation ORDER (stage-gated — do NOT open all 4 at once)

1. **Stage 0 — worktree + baseline.** In the worktree, build Algorithm1 unchanged; confirm
   it compiles and (if a checkpoint is available) reproduces ~5.16 ms. Establish the
   coordinate-extraction debug path (§7a) for Algorithm1.
2. **Stage 1 — GILBM-B** (`GEN=GILBM_RK2`, `STORE=COORDS`). Implement the table, the GILBM
   generator, the COORDS consumer. **GATE: direct `(t_xi,t_zeta)` equivalence vs Algorithm1
   ≤ 1e-12 (max abs), ≤ 1e-13 (RMS), per rank + MPI-global.** Must be GREEN before Stage 2.
3. **Stage 2 — GILBM-A** (`STORE=WEIGHTS`). Same GILBM generator, fold to weights + pure-MAC
   consumer branch. GATE: GILBM-A fields ≡ GILBM-B ≡ Algorithm1 (one-step field check).
   Now benchmark GILBM-A vs GILBM-B → answers the store-vs-recompute question.
4. **Stage 3 — ITB-B then ITB-A** (`GEN=ITB_NEWTON`). Port the Newton generator into the
   shared format (§5). GATE: ITB-A fields ≡ Edit9 ITBLBM (cross-check); ITB-A ≡ ITB-B fields.
5. **Stage 4 — factorial benchmark** (§8) + 2×2 analysis.

---

## 3.5 Per-Round Verification Gate (MANDATORY — applies to EVERY implementation round)

After completing **each** implementation round — every Stage in §3, AND every substantive
sub-increment within a Stage (e.g. "generator done", "consumer COORDS branch done",
"WEIGHTS fold done", "ITB port done") — you MUST run BOTH of the following BEFORE moving on.
This is non-negotiable and is in addition to the runtime coordinate-equivalence gate (§7b).

1. **Codex verification pass (independent review).** Hand the round's diff + changed files to
   Codex (read-only). Codex must confirm: (a) the implemented departure math matches the §4
   12-step recipe and the §5 coordinate contract; (b) the `t_zeta` no-`[0,6]`-clamp asymmetry
   (§4) is preserved; (c) NO Algorithm1 numerical regression and NO change to Algorithm1's
   kernel math; (d) storage class / call-site ordering / dispatch coverage / `__device__`
   (not `__constant__`) are correct; (e) no live-job / safety violation. Resolve every
   Codex **blocker/major** before proceeding.

2. **Workflow mathematical-implementation-equivalence verification.** Launch a multi-agent
   Workflow that **adversarially verifies the round's implementation is mathematically
   equivalent to Algorithm1** — independent of the runtime numeric gate. It must check, with
   cited file:line evidence: the RK2 departure chain (steps 1-11), every clamp, the
   `bk_precomp` centering, the ghost-fold, the `t_zeta` asymmetry, the 19→9 class mapping, the
   COORDS↔WEIGHTS weight identity, and (for ITB) the coordinate-contract conversion. It must
   reach an explicit verdict (EQUIVALENT / NOT) and list any divergence as a gap.

**Both must pass (or all findings be resolved) before the round is "done" and the next round
begins.** Record both verdicts (codex + workflow) in the round's notes / commit message. A
round whose codex or workflow check is unresolved is NOT complete, regardless of whether the
code compiles or the runtime tolerance happens to pass.

---

## 4. Mathematics — exact GILBM_RK2 equivalence (the 12-step recipe)

The GILBM generator MUST reproduce Algorithm1's departure coordinates. Replicate, in order
(file refs = `gilbm/evolution_gilbm/1.algorithm1.h`):

1. `e_txi_0 = ey*xi_y[j,k] + ez*xi_z[j,k]`; `e_tzeta_0 = ey*zeta_y[j,k] + ez*zeta_z[j,k]`  (L23-24)
2. `j_half = j - 0.5*dt*e_txi_0`; `k_half = k - 0.5*dt*e_tzeta_0`  (L25-26)
3. Clamp `j_half ∈ [0, NYD6-1]`, `k_half ∈ [3, NZ6-4]`  (L27-30)
4. `sj_rk = clamp(floor(j_half)-3, 0, NYD6-7)`; `tj_rk = j_half - sj_rk`  (L31-34)
5. `sk_rk = clamp(floor(k_half)-3, 0, NZ6-7)`; `tk_rk = k_half - sk_rk`  (L37-40)
6. Midpoint Lagrange weights via the **device** `lagrange_7point_coeffs` (same loop order)  (L36,L42)
7. Interpolate `e_txi_half`, `e_tzeta_half` over the 7×7 metric stencil (same accumulation order)  (L43-56)
8. `d_xi = dt*e_txi_half`; `delta_zeta = dt*e_tzeta_half`  (L57-58)
9. `bj = j-3`; `bk = bk_precomp_d[k]` (from `PrecomputeGILBM_StencilBaseK`, `precompute.h:236`)
10. `t_xi = (j-bj) - d_xi = 3 - d_xi`, **clamp to `[0,6]`**  (L310-311)
11. `up_k = k - delta_zeta`, **clamp to `[3, NZ6-4]`**; `t_zeta = up_k - bk`  (L316-319)
12. (consumer, runtime) regenerate `L_xi`,`L_zeta`; call `gilbm_ghost_zone_extrapolate(interp2,bk)`; zeta collapse  (L313,L321,L371)

**CRITICAL ASYMMETRY:** `t_xi` IS clamped to `[0,6]` (step 10) but `t_zeta` is NOT — it is
`up_k` (clamped to `[3,NZ6-4]`) minus `bk`, with no `[0,6]` clamp (step 11). Store the RAW
`up_k - bk` for zeta; do NOT apply a `[0,6]` clamp to it. Storing the wrong (clamped) zeta
silently corrupts near-wall stencils.

**Bit-exactness:** the device `lagrange_7point_coeffs` (`interpolation_gilbm.h:117`,
hardcoded denominators, division-free) differs from the host `lagrange_7point_coeffs_host`
(`precompute.h:15`, naive product/division) by up to ~7 ULP (~8.9e-16). Therefore:
- For **diff = 0** bit-exact equivalence to Algorithm1, run the GILBM generator as a
  **one-time DEVICE precompute kernel** that calls the SAME device helpers and the SAME
  loop order (steps 1-11). Host precompute is allowed only for diagnostics and is bounded by
  the 1e-12 tolerance, NOT bit-zero. State this explicitly; do not describe host precompute
  as bit-exact.
- `(t_xi,t_zeta)` + in-kernel device Lagrange (COORDS) preserves Algorithm1's exact in-kernel
  weight path → easiest route to diff=0. WEIGHTS mode bakes the weights at precompute → must
  be device-precomputed to stay bit-exact (host-folded weights inherit the ~7 ULP gap).
- Tolerance bars: coordinate max-abs ≤ 1e-12, RMS ≤ 1e-13. Any discrepancy > ~1e-14 is a
  BUG, not roundoff — it must not hide behind the tolerance. Report expected magnitude.

**Direction scope:** only `q=3..18` (16 dirs, 8 moving classes) use the generator.
`q=0` is a self-read; `q=1,2` (ey=ez=0) take the eta-only 1D path. Exclude them from the
table (class 0 is inert). Wall-BC directions skip streaming (Chapman-Enskog) — see §7.

**WENO7:** pin `USE_WENO7 = 0` for this study (zeta collapse = linear Lagrange-7, fully
precomputable). If `USE_WENO7=1`, the nonlinear collapse is data-dependent and MUST stay
runtime — out of scope here; assert/`#error` if someone enables it with Algorithm2.

---

## 5. Coordinate Contract (resolves the GILBM↔ITB "shared format" question)

The current Edit9 ITBLBM does NOT store `(r,s)` — it stores **folded weights** `wr[7]/ws[7]` +
explicit `k_idx[7]` + `j0` (`isoparametric_coeff.h:13`), ghost folded at precompute, with a
**centered** local coordinate (`shape7` around node 0, r,s ~[-1,1]). GILBM uses `t_xi,t_zeta
∈ [0,6]` with `lagrange_7point_coeffs` and ghost handled IN-KERNEL. These are different
conventions. For a single shared consumer, DEFINE one contract:

1. **Coordinate convention:** the canonical stored coordinate is GILBM's `[0,6]` stencil-local
   `(t_xi,t_zeta)`. The ITB generator must convert its centered `(r,s)` to this convention
   (`t = r + 3 + stencil_base_offset`) when emitting COORDS.
2. **Stencil base:** xi base `bj=j-3`; zeta base `bk=bk_precomp_d[k]`. Both generators emit
   coordinates relative to these bases (ITB's absolute `k_idx`/`j0` must be re-expressed).
3. **Ghost handling location:** in COORDS mode, ghost is handled IN-KERNEL
   (`gilbm_ghost_zone_extrapolate`) for BOTH generators. In WEIGHTS mode, ghost is folded at
   precompute for BOTH (ITB-style). A given STORE_MODE picks ONE ghost location for all cells.

If full unification of ITB into this contract proves too invasive in the time budget,
**ITB stays in WEIGHTS mode only** (its native format) and the GILBM↔ITB comparison is run
at `STORE=WEIGHTS` (both pure-MAC, same consumer) — still a clean generator comparison.
Document this as the fallback; do NOT claim drop-in ITB COORDS support unless §5.1-5.3 are
actually implemented and validated.

---

## 6. Wiring / integration

Core logic: `gilbm/precompute2.h` + `gilbm/evolution_gilbm/2.algorithm2.h`.
**Allowed (and required) wiring edits** — this supersedes any "only two files" restriction:
- `variables.h`: the four flags (§2d), `#error` guards (WENO7, flag ranges).
- `gilbm/evolution_gilbm/0.shared_code.h`: `__device__` table pointer declaration(s).
- `memory.h`: `cudaMalloc`/`cudaFree` of the table.
- `main.cu`: call the Algorithm2 precompute + validation. **Call-site ordering (hard):**
  AFTER the metric MPI ghost-exchange (~`main.cu:691-712`), AFTER `ComputeGlobalTimeStep`
  + `MPI_Allreduce` that sets `dt_global` (~`main.cu:740-742`), AFTER
  `PrecomputeGILBM_StencilBaseK` (bk); natural anchor = right after the eta-weight precompute
  block (~`main.cu:783-805`). Placing it before `MPI_Allreduce` gives a stale-dt table.
- `evolution.h`: dispatch. **`Launch_CollisionStreaming` has multiple launch sites**
  (Buffer boundary + Interior; ~`evolution.h:789/851/863`). With `USE_SMEM_INTERIOR=0`, the
  Buffer-path variants are active — ALL active launches must branch on `USE_GILBM_ALGORITHM2`.
  Scope the smem-twin to Algorithm1 only (do not port smem in this pass).

Not allowed: changing Algorithm1's RK2/interpolation/collision/BC/MPI/stats math; refactoring
unrelated code; making Algorithm2 default before equivalence passes.

---

## 7. Validation

### 7a. Reference-coordinate extraction (REQUIRED — Algorithm1 never stores coords)
Algorithm1 consumes `t_xi/t_zeta` inline and never stores them. Add a **debug dump path**:
a small instrumented variant (or a `#if GILBM_ALGO2_VALIDATE` branch in Algorithm1, behind
the flag, that writes `t_xi/t_zeta` per `(class,j,k)` to a scratch device array). This is the
ground truth the Algorithm2 table is compared against. Without it the "direct coordinate"
gate cannot be evaluated.

### 7b. Direct coordinate equivalence (PRIMARY GATE)
Compare Algorithm2 table vs Algorithm1 dumped coords: max-abs + RMS of `t_xi`,`t_zeta`,
location of max error `(j,k,class)`, MPI-global max+RMS. Tolerances per §4. Run at: (i) right
after precompute on host, (ii) after H2D upload round-trip, (iii) before first Algorithm2
launch. Mask wall-BC directions (they skip RK2). Compare coordinates, not just fields —
fields can average away coordinate errors.

### 7c. Secondary one-step field equivalence
From the same initialized `f`, run one step Algorithm1 vs Algorithm2; compare `f_post`, `rho`,
`ux`, `uy`, `uz` (max + RMS). Secondary, not a replacement for 7b.

### 7d. Factorial cross-checks
- **Equivalence (gate to ~0):** GILBM-A ≡ GILBM-B ≡ Algorithm1 (field); ITB-A ≡ ITB-B
  (field); ITB-A ≡ Edit9 ITBLBM (sanity).
- **ACCURACY axis = GEN (GILBM vs ITB) — this is the real purpose of the GEN factor, NOT
  efficiency.** Report the GILBM-vs-ITB departure-coordinate difference (expected NONZERO —
  quantify, do NOT gate to zero), and, once a cell runs long enough, compare flow statistics
  (Cf, Cp, mean-velocity profiles, Reynolds stresses) of GILBM vs ITB against the Krank et
  al. (2018) DNS benchmark data to judge which departure approximation is more accurate.
  This comparison is by physics/accuracy, never by `Iter_ms`.

---

## 8. Benchmark protocol (REQUIRED for any speedup claim — project Mandatory Efficiency Rule)

- **Isolated dir only** (the worktree), never the live job dir.
- A/B/cell comparison: identical initial checkpoint, identical grid (NX321/NY641/NZ321),
  jp=32, 32×H200, same partition, same execution-mode flags (`USE_SMEM_INTERIOR=0`,
  `USE_WENO7=0`, `FORCE_HERMITE_ORDER=2`, `USE_MRT`, `TIMING_DETAIL=1`); change ONLY the
  Algorithm2 flags between arms.
- Metrics from existing `timing.h` → `timing_log.dat` (every `TIMING_INTERVAL=1000`):
  **Iter_ms** (`last_iter_ms`), **Interior kernel ms** (`last_step1_ms`), **Buffer ms**,
  **MPI ms**, **periodicSW ms** (`last_psw_ms`), **MLUPS total + /GPU**, and table bytes/rank.
- Discard the first interval (warm-up); average ≥3 consecutive intervals per arm; report
  min/median Iter_ms; run arms back-to-back on the same allocation.
- **Equivalence-first:** timing is meaningless until 7b passes. Do not report a speedup for a
  not-yet-equivalent kernel.
- Pass criterion: Algorithm2 is a win only if (a) equivalence passes AND (b) Interior ms and
  Iter_ms drop at jp=32 with no MLUPS/GPU regression. Neutral/negative → stays behind the
  disabled-by-default flag, reported honestly. NEVER claim a speedup from op-counts alone.
- **GEN-independence consistency check (MANDATORY):** at a fixed STORE, the generator does
  not touch the per-step kernel, so `GILBM-A` and `ITB-A` MUST land at the same `Iter_ms`/
  `Interior ms` (within run-to-run noise), and likewise `GILBM-B` ≈ `ITB-B`. Efficiency has
  only TWO distinct levels (A, B). If `GILBM-A` and `ITB-A` (or `GILBM-B`/`ITB-B`) measure
  meaningfully DIFFERENT, that is a RED FLAG that the consumer kernel is not actually shared
  (an accidental confound) — investigate before trusting any number. Report the timing table
  as "STORE level (A/B) × GEN-independence confirmation", not as four independent speedups.
- Note: the "9×49 tensor → 9×(7+7) separable" framing is NOT a new win — Algorithm1 AND
  ITBLBM are already separable. The real removed cost is the in-kernel RK2 metric recompute.

---

## 9. Unit tests (runnable without a full sim)

1. y-z class mapping: every `q` → expected 9-class id; same `(ey,ez)` → same class.
2. Table indexing `(class,j,k)`; assert no 19-direction storage.
3. COORDS→weights: stored `(t_xi,t_zeta)` regenerate the same `L_xi[7]`,`L_zeta[7]` the
   in-kernel path produces (test the coordinate→weight map, NOT stored weights in COORDS mode).
4. Identity/simple-grid: on an analytic metric, Algorithm2 coords match Algorithm1-derived.
5. Boundary/clamp: edge `(j,k)` where stencil base/clamp matters; confirm the `t_zeta`
   no-clamp asymmetry (§4) is preserved; same valid stencil range as Algorithm1.
6. Storage invariant: COORDS table holds only `(t_xi,t_zeta,flags)` (compile/runtime check);
   WEIGHTS table holds `wr/ws/k_idx/j0/flags`. Fails if the wrong fields appear for the mode.

---

## 10. Deliverables

1. `gilbm/precompute2.h`, `gilbm/evolution_gilbm/2.algorithm2.h`.
2. Minimal wiring patches (§6) behind disabled-by-default flags.
3. The reference-coordinate extraction path (§7a).
4. Direct coordinate + one-step field equivalence (§7b/7c) + factorial cross-checks (§7d).
5. Unit tests + build/run instructions.
6. The factorial benchmark harness + results table (§8): Algorithm1 + 4 cells, Iter_ms /
   Interior ms / MLUPS-per-GPU / table bytes, with the 2×2 read (storage effect × generator effect).
7. A short note on the coordinate contract (§5) and which fallback (if any) was taken for ITB.
8. **A per-round verification log (§3.5):** for every implementation round, the Codex verdict
   and the Workflow mathematical-equivalence verdict, with any findings and their resolution.
   No round is accepted without both recorded as passed/resolved.

Algorithm2 (any cell) is acceptable only if its direct departure-coordinate validation
proves numerical equivalence to Algorithm1 within the stated tolerances. Performance numbers
are reported only after equivalence passes, only from the isolated benchmark.
