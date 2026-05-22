# Periodic Hill Turbulence — Wall Friction Pipeline

## Project Parameters (Edit6_5600DNS)

| Parameter | Value | Source |
|-----------|-------|--------|
| Re | 5600 | `variables.h` |
| Uref | 0.015 | `variables.h` |
| niu | Uref/Re = 2.678571e-06 | `variables.h` |
| NX (span) | 257 | `variables.h` |
| NY (stream) | 513 | `variables.h` |
| NZ (normal) | 257 | `variables.h` |
| LX | 4.5 | `variables.h` |
| LY | 9.0 | `variables.h` |
| LZ | 3.036 | `variables.h` |
| VTK format | **BINARY** (double) | solver output |
| STRETCH_A | 0.97 | `variables.h` |
| ALPHA | 0.5 | `variables.h` |

## Project Convention
- x = span (i), y = stream (j), z = wall-normal (k)
- Curvilinear: eta = span, xi = stream, zeta = wall-normal
- Bottom wall: k=0 (zeta=0), Top wall: k=Nz-1

## VTK Variable Convention

Solver outputs VTK in **ERCOFTAC naming** convention:
- `U_mean` = streamwise velocity / Uref (code v)
- `V_mean` = wall-normal velocity / Uref (code w)
- `W_mean` = spanwise (Level 1, not always present)

Step 1 (`1.phase1_transvtk.py`) applies the cyclic rename:
- `U_mean` → `V_mean` (stream, project convention)
- `V_mean` → `W_mean` (normal, project convention)
- `W_mean` → `U_mean` (span, project convention)

Steps 3/4 read `V_mean` and `W_mean` from the renamed VTK.

## Input Preparation

Place in `Input/`:
1. **VTK file**: One `*.vtk` from `result/` (time-averaged velocity field)
2. **Mesh DAT**: One `*.dat` from `J_Frohlich/` (2D corner grid, I=NY J=NZ)
3. **variables.h**: Auto-linked (symlink to `../../variables.h`)
4. **Metadata**: Run `python Input/generate_metadata.py` after placing the VTK
5. **(Optional)** `Ustar_Force_record.dat`: For time-averaged Force in Phase 4

## Tau Convention

Single convention throughout all stages:

    tau_wall = niu * du_t/dn     (lattice stress, rho = 1)
    u_tau    = sqrt(tau_wall / rho)
    z+       = u_tau * d_n / niu

Step 4 restores lattice units by multiplying VTK velocity by Uref.

## Output File Numbering

| # | File | Description |
|---|------|-------------|
| 1 | `1.*_v2.vtk` | VTK with renamed variables |
| 2 | `2.j*_k*_g*_a*.dat` | 2D mesh in h-units |
| 3 | `3.*_uxi_*.vtk` | VTK + u_xi, u_zeta |
| 4 | `4.*_inverseJacobian_*.dat` | Full metric terms |
| 5-6 | `5/6.*_utan_*.dat` | Bottom/top 7-layer u_tangent slabs |
| 7-8 | `7/8.*_tauwall.dat` | Bottom/top tau_wall (signed + abs) |
| 9 | `9.*_tauwall_global.dat` | Area-weighted global tau |
| 10-11 | `10/11.*_delta*.dat/.txt` | Grid spacing delta_y, delta_z |
| 13 | `13.*_Deltay_Deltaz.vtk` | Delta VTK |
| 14-17 | `14-17.*_zplus_*.dat/.txt` | z+ summary + per-wall data |
| 18 | `18.*_zplus_*.pdf/.png` | z+ plots (1D→2D average) |
| 19-26 | `19-26.*` | Span-averaged utan, tau, z+ + plots |
| 27-32 | `27-32.*` | 2nd-order span-averaged + plots |
| 34 | `34.*_total_Fvics.dat` | Total viscous drag |
| 35 | `35.*_Fbody_volume.dat` | Body force × volume |
| 36 | `36.*_force_balance.dat` | Force balance verification |
| 37-42 | `37-42.*_utau_*.txt` | u_tau 1D profiles |

## Mesh Spacing Extrema (Output 14)

### Global wall-unit extrema location
- delta_y_plus_max/min, delta_z_plus_max/min use u_tau_global (constant)
- Location is the same as the mesh spacing extrema (constant scaling factor)

### Local wall-unit extrema (bottom wall, u_tau = u_tau_local)
- Formula: delta_*_plus_local(i,j) = u_tau_local(i,j) * delta_*(j, k=0) / niu
- u_tau_local varies along the wall surface (i, j), so the extremum
  of the product is a 3D search over the bottom wall
- Report 3D location: (i, j, k=0) with physical coords (x, y, z)
- delta_y at bottom wall: central-difference with periodic wrap
- delta_z at bottom wall: first cell height |z(j,1) - z(j,0)|
