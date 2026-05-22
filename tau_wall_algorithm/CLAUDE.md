# Periodic Hill Turbulence — Friction Velocity Pipeline

## Project Convention
- x = span (i), y = stream (j), z = wall-normal (k)
- Curvilinear: eta = span, xi = stream, zeta = wall-normal
- Bottom wall: k=0 (zeta=0), Top wall: k=Nz-1

## Output File 14 (zplus_summary) Enhancement

### Mesh spacing extrema with spatial location
For delta_y_max | delta_y_min | delta_z_max | delta_z_min:
- Report the (y, z) plane location where the extremum occurs
- These are 2D quantities (mesh is 2D in y-z plane)
- Location data is read from file 11 (delta_extrema.txt)

### Global wall-unit extrema location
- delta_y_plus_max/min, delta_z_plus_max/min use u_tau_global (constant)
- Location is the same as the mesh spacing extrema (constant scaling factor)

### Local wall-unit extrema (bottom wall, u_tau = u_tau_local)
- Formula: delta_*_plus_local(i,j) = u_tau_local(i,j) * delta_*(j, k=0) / niu
- u_tau_local varies along the wall surface (i, j), so the extremum
  of the product is a 3D search over the bottom wall
- Report 3D location: (i, j, k=0) with physical coords (x, y, z)
  corresponding to (eta, xi, zeta=0_bottom)
- delta_y at bottom wall: central-difference with periodic wrap
  delta_y(j,0) = ( |y(j+1,0)-y(j,0)| + |y(j,0)-y(j-1,0)| ) / 2
- delta_z at bottom wall: first cell height
  delta_z(j,0) = |z(j,1) - z(j,0)|
