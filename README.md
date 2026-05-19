# Edit5_Rebuild — Periodic Hill Re10595 (GILBM D3Q19 MRT)

Generalized Interpolation LBM solver for turbulent flow over periodic hills
at Re_tau = 10595 (effective Re_eff ~ 5600 during development).

## Solver Configuration

| Parameter | Value |
|-----------|-------|
| Lattice | D3Q19 MRT |
| Grid | NX=65, NY=129, NZ=65 (I=129, J=65) |
| MPI ranks | jp=8 (streamwise decomposition) |
| dt_global | 7.408e-3 |
| tau / omega | 0.5011 |
| nu | 2.679e-6 |
| GAMMA (grid stretching) | 2.0 |
| CFL | 0.5 |
| Forcing | SIMPLE-PROP controller, NDTFRC=50 |
| Hermite order | FORCE_HERMITE_ORDER=1 |
| Collision | Precomputed K-matrix: `K = M^{-1} S M` |

## Key Optimizations

1. **MRT Nonequilibrium Projection** — K-matrix precomputed on host, stored in
   `__constant__` memory. Kernel does table-lookup + multiply-add only.
2. **Shared Eta Interpolation Weights** — 7-point Lagrange weights depend only
   on `sign(e_x)`, precomputed as 2 sets in `GILBM_L_eta_shared[2][7]`.
3. **Per-volume Mass Correction** — Global scalar correction
   `delta_rho = (N - sum(rho)) / N` using correct MPI-decomposed cell count
   `jp*(NYD6-7)`. Critical for stability; incorrect cell count causes divergence.

## Experiment Log

### 2026-05-19: Per-k J-weighted Mass Correction (FAILED)

**Motivation:** Persistent O(1e-8) density oscillations in |rho_max - rho_min|
correlated with forcing ramp-up. Hypothesis: Jacobian-unweighted
non-conservative semi-Lagrangian streaming causes systematic mass redistribution
across wall-normal (zeta) levels where J varies 4.4x.

**Approach:** Replace global scalar correction with per-k J-weighted correction:
```
delta_rho(k) = (sum_J(k) - sum_Jrho(k)) / sum_J(k)  +  global_offset
```
where `global_offset` ensures sum(rho) = N. Implemented `ReduceJrhoPerK_Kernel`
with shared-memory reduction (one block per k-level, NZ6-6 blocks).

**Files modified (all reverted):**
- `evolution.h` — added `ReduceJrhoPerK_Kernel`
- `1.algorithm1.h` — `rho_modify[0]` -> `rho_modify[k-3]`
- `main.cu` — per-k correction blocks (mid-step + NDTFRC), J_2D_d upload
- `memory.h` — expanded rho_modify allocation, added Jrho/J per-k buffers

**Results:**

| Metric | Original scalar | Per-k J-weighted |
|--------|----------------|-----------------|
| \|avg(rho) - 1\| | 5-8e-09 (stable) | 1-4e-06 (500x worse) |
| Drift direction | None (bounded) | Monotonically increasing |
| Steady state | O(1e-9) | Oscillating ~3e-6 |

**Root cause of failure:**
1. **Correction-streaming timing mismatch** — delta_rho computed before
   streaming but applied during streaming. The interpolation error epsilon from
   the current streaming step is unknown at computation time, so
   `sum(rho)_after = N + epsilon`. With scalar correction epsilon ~ 5e-9;
   with per-k epsilon ~ 3e-6 because of sharper density gradients.
2. **Over-constraining** — Enforcing J-weighted avg(rho)_k = 1 at each k-level
   fights the physical wall-normal pressure gradient. The global offset must
   continuously compensate, creating systematic positive drift.
3. **Larger interpolation error** — Per-k correction creates density
   discontinuities between k-levels. The 7-point Lagrange interpolation in
   zeta direction amplifies these into larger streaming mass errors.

**Additional test: per-k smoothing then switch back to scalar**
Ran per-k for ~5 FTT to "smooth" density field, then switched to scalar
correction. Result: |avg(rho)-1| stuck at ~3e-6, did NOT recover to original
5e-9 level. The f[q] distribution functions adapted to per-k correction
structure and produce larger interpolation errors even under scalar correction.

**Conclusion:** Mass correction must only enforce global sum(rho) = N.
Per-k, per-(j,k), and J-weighted variants are counterproductive.
The O(1e-8) density oscillation is an inherent feature of semi-Lagrangian
GILBM interpolation, not addressable by mass correction schemes.
All changes reverted to commit `55b573c`.

## Pipeline

Regrid workflow: `phase1_generategrid/` (grid) + `phase2_generatecheckpoint/`
(interpolation). Driven through `./run.sh` — see `PIPELINE_GUIDE.md`.

Origin checkpoint: `oldcheckpoint_Re5600_step_12932001` (257x129x129 grid).

## Documentation

- `CLAUDE.md` — Agent instructions and efficiency rules
- `PIPELINE_GUIDE.md` — Regrid pipeline workflow
- `THEORY.md` — GILBM theory notes
- `GRID_CONSISTENCY.md` — Grid validation requirements
