# Grid Consistency

This project has two grid paths that must stay numerically identical for the
NEW grid used by checkpoint interpolation and by the solver runtime.

## Paths

1. Solver runtime path:
   - `main.cu` derives `J_Frohlich/adaptive_<ref>_I<NY>_J<NZ>_g<GAMMA>_a<ALPHA>.dat`.
   - If the file is missing or stale, `main.cu` runs `python3 J_Frohlich/grid_zeta_tool.py --auto`.
   - The solver reads that `.dat` through `ReadExternalGrid_YZ()`.

2. Phase 1/2 checkpoint path:
   - `phase1_generategrid/grid_zeta_tool.py` generates Phase 1 grids using the same generator and `grid_params.py`.
   - `phase2_generatecheckpoint/interp_checkpoint.py` reads `phase1_generategrid/oldgrid_*.dat` and `phase1_generategrid/newgrid_*.dat`.
   - Phase 2 compares the NEW Phase 1 grid against the exact solver runtime grid before writing a rebuilt checkpoint.

Before this fix, `phase1_generategrid/` contained only cached `.dat` files and no generator script. Those legacy files have no parameter fingerprint, so their Poisson iteration count cannot be proven from the file itself.

## Current Parameters

| Parameter | Solver runtime path | Phase 1 path | Match |
| --- | --- | --- | --- |
| Shared config | `grid_params.py` | `grid_params.py` | yes |
| Reference grid | `J_Frohlich/3.fine grid.dat` | `J_Frohlich/3.fine grid.dat` | yes |
| I / streamwise nodes | `NY=257` | `NY=257` | yes |
| J / wall-normal nodes | `NZ=129` | `NZ=129` | yes |
| NEW gamma | `GAMMA=3.7` | from `variables.h` (`3.7`) | yes |
| OLD gamma | not used by solver runtime | `PHASE1_OLD_GAMMA=2.0` | intentionally different |
| Alpha | `ALPHA=0.5` | from `variables.h` (`0.5`) | yes |
| Domain length | `LY=9.0`, `LZ=3.036`, `H_HILL=1.0` | same from `variables.h` | yes |
| Hill shape | Mellen-Frohlich-Rodi piecewise polynomial, mirrored, `scale=54/28` | same function | yes |
| Vertical redistribution | physical-z Vinokur tanh | same function | yes |
| Poisson max iterations | `100000` | `100000` | yes |
| Poisson tolerance | `1e-12` | `1e-12` | yes |
| Poisson relaxation omega | `1.0` | `1.0` | yes |
| Poisson print interval | `2000` | `2000` | yes |
| Convergence policy | require converged by default | require converged by default | yes |
| P/Q controls | reverse-computed Steger-Sorenson control functions | same function | yes |
| P/Q interpolation | SciPy bicubic if available, else NumPy bilinear; actual backend is fingerprinted | same | yes if same environment, otherwise caught |
| Boundary resampling | SciPy cubic if available, else NumPy linear; actual backend is fingerprinted | same | yes if same environment, otherwise caught |
| Poisson BCs | fixed resampled bottom/top/left/right boundaries, TFI initial guess | same | yes |
| Solver ghost BCs | streamwise periodic ghosts, wall-normal linear extrapolated ghosts | runtime load only | N/A |

## Implemented Solution

The implemented solution is Option A with a fingerprint guard:

- `grid_params.py` is the single source of truth for Poisson and grid-shape parameters.
- `J_Frohlich/grid_zeta_tool.py` imports Poisson defaults from `grid_params.py`.
- `phase1_generategrid/grid_zeta_tool.py` imports the same config and calls the same `J_Frohlich.grid_zeta_tool.generate_adaptive_grid()` function.
- Generated `.dat` files get Tecplot-safe comment headers:
  - `# GRID_PARAMS_SHA256=...`
  - `# GRID_PARAMS_JSON=...`
- Coordinate rows are unchanged, and legacy `.dat` files without these comments are still readable.
- `phase2_generatecheckpoint/interp_checkpoint.py` still performs exact coordinate comparison, and now also propagates grid fingerprints into checkpoint metadata.
- `main.cu` checks `interp_solver_grid_match` and grid parameter fingerprints before calling `LoadBinaryCheckpoint()`.
- `main.cu` also treats `grid_params.py` as a grid-generation dependency, so changing shared parameters marks solver grids stale.

## Safe Update Checklist

1. Edit grid-generation defaults only in `grid_params.py`.
2. If changing `GAMMA`, `ALPHA`, `NY`, `NZ`, `GRID_DAT_REF`, or domain lengths, edit `variables.h`.
3. Regenerate the solver runtime grid with:
   `python3 J_Frohlich/grid_zeta_tool.py --auto`
4. Regenerate Phase 1 grids with:
   `python3 phase1_generategrid/grid_zeta_tool.py`
5. Rebuild the checkpoint through Phase 2. Leave `--allow-solver-grid-mismatch` off for production.
6. Start the solver. If Phase 1 and runtime grids differ in coordinates or fingerprint, startup aborts before checkpoint data is loaded.

Do not manually copy or rename a Phase 1 grid into `J_Frohlich/` as a substitute for regeneration. The runtime filename, coordinate comparison, and fingerprint must all agree.
