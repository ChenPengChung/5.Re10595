#!/usr/bin/env python3
"""Checkpoint visual/numeric audit for regrid restart initial states.

Reads a D3Q19 checkpoint folder, computes macro fields, writes boundary/slice
legacy VTK files for ParaView, and emits JSON/TXT continuity diagnostics.

Typical use:
  python restart_tools/checkpoint_visual_audit.py ^
    --checkpoint-dir restart/checkpoint/step_00000001 ^
    --grid-dat "J_Frohlich/adaptive_3.fine grid_I257_J129_a0.5.dat"
"""
import argparse
import json
import math
import os
import sys
import time

import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from interp_checkpoint import (  # noqa: E402
    BFR, E, LX, LY,
    GridConfig,
    auto_detect_from_metadata,
    build_grid_xyz,
    compute_feq_q,
    compute_inverse_metric_2d,
    compute_inverse_metric_2d_fornberg,
    compute_velocity_gradient_3d,
    fill_ghost,
    parse_metadata,
    read_rank_bin,
    stitch_y,
)


def _as_float(meta, key, default):
    try:
        return float(meta.get(key, default))
    except (TypeError, ValueError):
        return default


def resolve_grid_path(cli_grid, meta, checkpoint_dir):
    candidates = []
    if cli_grid:
        candidates.append(cli_grid)
    for key in ('interp_new_grid', 'interp_old_grid', 'grid_dat'):
        val = meta.get(key)
        if val:
            candidates.append(val)
    for c in candidates:
        if os.path.isabs(c) and os.path.isfile(c):
            return c
        for base in (os.getcwd(), checkpoint_dir, os.path.dirname(checkpoint_dir)):
            p = os.path.abspath(os.path.join(base, c))
            if os.path.isfile(p):
                return p
    raise FileNotFoundError(
        'Grid .dat not found. Pass --grid-dat or provide interp_new_grid in metadata.dat')


def build_cfg(checkpoint_dir, grid_dat, gamma=None, alpha=None):
    meta_path = os.path.join(checkpoint_dir, 'metadata.dat')
    meta = parse_metadata(meta_path)
    detected = auto_detect_from_metadata(meta_path)
    if detected is None:
        raise ValueError('metadata.dat lacks grid_dims/mpi_rank_count; cannot infer dimensions')
    grid_dat = resolve_grid_path(grid_dat, meta, checkpoint_dir)
    gamma = gamma if gamma is not None else _as_float(meta, 'interp_new_gamma', 0.0)
    alpha = alpha if alpha is not None else _as_float(meta, 'ALPHA', 0.5)
    cfg = GridConfig(detected['NX'], detected['NY'], detected['NZ'], detected['jp'],
                     gamma, alpha, grid_dat)
    return cfg, meta


def read_checkpoint_macros(checkpoint_dir, cfg, want_fneq_ratio=True):
    rho_sum = np.zeros((cfg.NY6, cfg.NZ6, cfg.NX6), dtype=np.float64)
    momx = np.zeros_like(rho_sum)
    momy = np.zeros_like(rho_sum)
    momz = np.zeros_like(rho_sum)
    min_f = float('inf')
    max_f = -float('inf')
    nonfinite_f = 0
    negative_f = 0

    for q in range(19):
        per_rank = []
        for r in range(cfg.JP):
            path = os.path.join(checkpoint_dir, 'f{:02d}_{}.bin'.format(q, r))
            per_rank.append(read_rank_bin(path, cfg))
        f = stitch_y(per_rank, cfg)
        nonfinite_f += int(np.size(f) - np.count_nonzero(np.isfinite(f)))
        negative_f += int(np.count_nonzero(f <= 0.0))
        min_f = min(min_f, float(np.nanmin(f)))
        max_f = max(max_f, float(np.nanmax(f)))
        rho_sum += f
        if E[q, 0] != 0:
            momx += E[q, 0] * f
        if E[q, 1] != 0:
            momy += E[q, 1] * f
        if E[q, 2] != 0:
            momz += E[q, 2] * f

    rho_safe = np.where(rho_sum > 1e-30, rho_sum, 1.0)
    ux = momx / rho_safe
    uy = momy / rho_safe
    uz = momz / rho_safe

    rho_file = None
    rho_file_diff = None
    try:
        rho_file = stitch_y([
            read_rank_bin(os.path.join(checkpoint_dir, 'rho_{}.bin'.format(r)), cfg)
            for r in range(cfg.JP)
        ], cfg)
        rho_file_diff = float(np.nanmax(np.abs(rho_file - rho_sum)))
    except (IOError, OSError, ValueError):
        pass

    max_fneq_ratio = None
    if want_fneq_ratio:
        max_fneq_ratio = 0.0
        for q in range(19):
            per_rank = []
            for r in range(cfg.JP):
                path = os.path.join(checkpoint_dir, 'f{:02d}_{}.bin'.format(q, r))
                per_rank.append(read_rank_bin(path, cfg))
            f = stitch_y(per_rank, cfg)
            feq = compute_feq_q(rho_sum, ux, uy, uz, q)
            ratio = np.abs(f - feq) / np.maximum(np.abs(feq), 1e-30)
            max_fneq_ratio = max(max_fneq_ratio, float(np.nanmax(ratio)))

    return {
        'rho': rho_sum,
        'rho_file': rho_file,
        'rho_file_diff': rho_file_diff,
        'ux': ux,
        'uy': uy,
        'uz': uz,
        'min_f': min_f,
        'max_f': max_f,
        'nonfinite_f': nonfinite_f,
        'negative_f': negative_f,
        'max_fneq_ratio': max_fneq_ratio,
    }


def interior(cfg, arr):
    return arr[BFR:BFR+cfg.NY, BFR:BFR+cfg.NZ, BFR:BFR+cfg.NX]


def max_abs(a):
    return float(np.nanmax(np.abs(a)))


def seam_stats(a):
    return {
        'i_periodic_max_abs': max_abs(a[:, :, 0] - a[:, :, -1]),
        'j_periodic_max_abs': max_abs(a[0, :, :] - a[-1, :, :]),
    }


def classify_metrics(m):
    checks = {}
    checks['finite_f'] = 'PASS' if m['nonfinite_f_count'] == 0 else 'FAIL'
    checks['positive_f'] = 'PASS' if m['min_f'] > 0.0 else 'FAIL'
    checks['positive_rho'] = 'PASS' if m['rho_min'] > 0.0 else 'FAIL'
    checks['wall_no_slip'] = 'PASS' if m['wall_speed_max'] < 1e-6 else 'WARN'
    checks['periodic_i_velocity'] = 'PASS' if m['velocity_i_periodic_jump_max'] < 1e-6 else 'WARN'
    checks['periodic_j_velocity'] = 'PASS' if m['velocity_j_periodic_jump_max'] < 1e-6 else 'WARN'
    checks['rho_file_consistency'] = 'PASS'
    if m['rho_file_minus_sumf_max'] is None:
        checks['rho_file_consistency'] = 'WARN'
    elif m['rho_file_minus_sumf_max'] > 1e-2:
        checks['rho_file_consistency'] = 'FAIL'
    elif m['rho_file_minus_sumf_max'] > 1e-6:
        checks['rho_file_consistency'] = 'WARN'
    if m['fneq_over_feq_max'] is not None and m['fneq_over_feq_max'] > 0.5:
        checks['fneq_ratio'] = 'WARN'
    else:
        checks['fneq_ratio'] = 'PASS'
    checks['z_monotone'] = 'PASS' if m['grid_min_dz_dk'] > 0.0 else 'FAIL'
    overall = 'PASS'
    if any(v == 'FAIL' for v in checks.values()):
        overall = 'FAIL'
    elif any(v == 'WARN' for v in checks.values()):
        overall = 'WARN'
    return overall, checks


def compute_audit_metrics(cfg, fields, metric_order=6):
    rho = interior(cfg, fields['rho'])
    ux = interior(cfg, fields['ux'])
    uy = interior(cfg, fields['uy'])
    uz = interior(cfg, fields['uz'])
    speed = np.sqrt(ux*ux + uy*uy + uz*uz)

    x, y2d, z2d = build_grid_xyz(cfg)
    y = y2d[BFR:BFR+cfg.NY, BFR:BFR+cfg.NZ]
    z = z2d[BFR:BFR+cfg.NY, BFR:BFR+cfg.NZ]

    vel_i_jump = np.sqrt((ux[:, :, 0] - ux[:, :, -1])**2
                         + (uy[:, :, 0] - uy[:, :, -1])**2
                         + (uz[:, :, 0] - uz[:, :, -1])**2)
    vel_j_jump = np.sqrt((ux[0, :, :] - ux[-1, :, :])**2
                         + (uy[0, :, :] - uy[-1, :, :])**2
                         + (uz[0, :, :] - uz[-1, :, :])**2)

    wall_speed = max(float(np.nanmax(speed[:, 0, :])),
                     float(np.nanmax(speed[:, -1, :])))

    rho_g = fields['rho'].copy()
    ux_g = fields['ux'].copy()
    uy_g = fields['uy'].copy()
    uz_g = fields['uz'].copy()
    for arr in (rho_g, ux_g, uy_g, uz_g):
        fill_ghost(arr, cfg)
    if metric_order == 6:
        dj_dy, dj_dz, dk_dy, dk_dz = compute_inverse_metric_2d_fornberg(y2d, z2d)
    else:
        dj_dy, dj_dz, dk_dy, dk_dz = compute_inverse_metric_2d(y2d, z2d)
    dx = LX / (cfg.NX - 1)
    dudx, dudy, dudz = compute_velocity_gradient_3d(ux_g, dx, dj_dy, dj_dz, dk_dy, dk_dz, cfg)
    dvdx, dvdy, dvdz = compute_velocity_gradient_3d(uy_g, dx, dj_dy, dj_dz, dk_dy, dk_dz, cfg)
    dwdx, dwdy, dwdz = compute_velocity_gradient_3d(uz_g, dx, dj_dy, dj_dz, dk_dy, dk_dz, cfg)
    div_u = dudx + dvdy + dwdz
    max_grad = max(max_abs(a) for a in (dudx, dudy, dudz, dvdx, dvdy, dvdz, dwdx, dwdy, dwdz))

    rho_seam = seam_stats(rho)
    metrics = {
        'shape_NX_NY_NZ_jp': [cfg.NX, cfg.NY, cfg.NZ, cfg.JP],
        'rho_min': float(np.nanmin(rho)),
        'rho_max': float(np.nanmax(rho)),
        'rho_mean': float(np.nanmean(rho)),
        'rho_std': float(np.nanstd(rho)),
        'speed_max': float(np.nanmax(speed)),
        'wall_speed_max': wall_speed,
        'rho_i_periodic_jump_max': rho_seam['i_periodic_max_abs'],
        'rho_j_periodic_jump_max': rho_seam['j_periodic_max_abs'],
        'velocity_i_periodic_jump_max': float(np.nanmax(vel_i_jump)),
        'velocity_j_periodic_jump_max': float(np.nanmax(vel_j_jump)),
        'max_div_u': max_abs(div_u),
        'max_grad_u': max_grad,
        'div_over_grad': max_abs(div_u) / max(max_grad, 1e-30),
        'grid_min_dz_dk': float(np.nanmin(np.diff(z, axis=1))),
        'grid_j_seam_z_jump_max': max_abs(z[0, :] - z[-1, :]),
        'grid_j_period_span_error_max': max_abs((y[-1, :] - y[0, :]) - LY),
        'min_f': fields['min_f'],
        'max_f': fields['max_f'],
        'nonfinite_f_count': fields['nonfinite_f'],
        'nonpositive_f_count': fields['negative_f'],
        'rho_file_minus_sumf_max': fields['rho_file_diff'],
        'fneq_over_feq_max': fields['max_fneq_ratio'],
    }
    overall, checks = classify_metrics(metrics)
    metrics['overall'] = overall
    metrics['checks'] = checks
    return metrics


def write_polydata(path, points, dims, scalars, vectors=None):
    n0, n1 = dims
    npts = len(points)
    quads = max(n0 - 1, 0) * max(n1 - 1, 0)
    with open(path, 'w', newline='\n') as f:
        f.write('# vtk DataFile Version 3.0\n')
        f.write('checkpoint audit surface\n')
        f.write('ASCII\n')
        f.write('DATASET POLYDATA\n')
        f.write('POINTS {} float\n'.format(npts))
        for p in points:
            f.write('{:.9e} {:.9e} {:.9e}\n'.format(p[0], p[1], p[2]))
        f.write('POLYGONS {} {}\n'.format(quads, quads * 5))
        for a in range(n0 - 1):
            for b in range(n1 - 1):
                p0 = a * n1 + b
                f.write('4 {} {} {} {}\n'.format(p0, p0 + 1, p0 + n1 + 1, p0 + n1))
        f.write('POINT_DATA {}\n'.format(npts))
        for name, values in scalars.items():
            vals = np.asarray(values).reshape(-1)
            f.write('SCALARS {} float 1\n'.format(name))
            f.write('LOOKUP_TABLE default\n')
            for v in vals:
                f.write('{:.9e}\n'.format(float(v)))
        if vectors:
            for name, values in vectors.items():
                vals = np.asarray(values).reshape((-1, 3))
                f.write('VECTORS {} float\n'.format(name))
                for v in vals:
                    f.write('{:.9e} {:.9e} {:.9e}\n'.format(float(v[0]), float(v[1]), float(v[2])))


def surface_points(cfg, x, y, z, mode, index):
    pts = []
    if mode == 'k':
        k = index
        for j in range(cfg.NY):
            for i in range(cfg.NX):
                pts.append((x[i], y[j, k], z[j, k]))
        return pts, (cfg.NY, cfg.NX)
    if mode == 'j':
        j = index
        for k in range(cfg.NZ):
            for i in range(cfg.NX):
                pts.append((x[i], y[j, k], z[j, k]))
        return pts, (cfg.NZ, cfg.NX)
    if mode == 'i':
        i = index
        for j in range(cfg.NY):
            for k in range(cfg.NZ):
                pts.append((x[i], y[j, k], z[j, k]))
        return pts, (cfg.NY, cfg.NZ)
    raise ValueError(mode)


def slice_data(cfg, rho, ux, uy, uz, mode, index):
    speed = np.sqrt(ux*ux + uy*uy + uz*uz)
    vel = np.stack([ux, uy, uz], axis=-1)
    if mode == 'k':
        return {
            'rho': rho[:, index, :],
            'speed': speed[:, index, :],
            'ux': ux[:, index, :],
            'uy': uy[:, index, :],
            'uz': uz[:, index, :],
        }, {'velocity': vel[:, index, :, :]}
    if mode == 'j':
        return {
            'rho': rho[index, :, :],
            'speed': speed[index, :, :],
            'ux': ux[index, :, :],
            'uy': uy[index, :, :],
            'uz': uz[index, :, :],
        }, {'velocity': vel[index, :, :, :]}
    if mode == 'i':
        return {
            'rho': rho[:, :, index],
            'speed': speed[:, :, index],
            'ux': ux[:, :, index],
            'uy': uy[:, :, index],
            'uz': uz[:, :, index],
        }, {'velocity': vel[:, :, index, :]}
    raise ValueError(mode)


def write_vtk_suite(out_dir, cfg, fields):
    os.makedirs(out_dir, exist_ok=True)
    x_full, y2d, z2d = build_grid_xyz(cfg)
    x = x_full[BFR:BFR+cfg.NX]
    y = y2d[BFR:BFR+cfg.NY, BFR:BFR+cfg.NZ]
    z = z2d[BFR:BFR+cfg.NY, BFR:BFR+cfg.NZ]
    rho = interior(cfg, fields['rho'])
    ux = interior(cfg, fields['ux'])
    uy = interior(cfg, fields['uy'])
    uz = interior(cfg, fields['uz'])

    surfaces = [
        ('boundary_bottom', 'k', 0),
        ('boundary_top', 'k', cfg.NZ - 1),
        ('periodic_j0', 'j', 0),
        ('periodic_jL', 'j', cfg.NY - 1),
        ('periodic_i0', 'i', 0),
        ('periodic_iL', 'i', cfg.NX - 1),
        ('slice_k_mid', 'k', cfg.NZ // 2),
        ('slice_i_mid', 'i', cfg.NX // 2),
    ]
    written = []
    for name, mode, idx in surfaces:
        pts, dims = surface_points(cfg, x, y, z, mode, idx)
        scalars, vectors = slice_data(cfg, rho, ux, uy, uz, mode, idx)
        path = os.path.join(out_dir, name + '.vtk')
        write_polydata(path, pts, dims, scalars, vectors)
        written.append(path)

    # Periodic jump surfaces: written on the first side of each seam.
    vel = np.stack([ux, uy, uz], axis=-1)
    pts, dims = surface_points(cfg, x, y, z, 'j', 0)
    jump_v = vel[0, :, :, :] - vel[-1, :, :, :]
    scalars = {
        'jump_rho': rho[0, :, :] - rho[-1, :, :],
        'jump_velocity_norm': np.sqrt(np.sum(jump_v * jump_v, axis=-1)),
    }
    path = os.path.join(out_dir, 'periodic_j_jump.vtk')
    write_polydata(path, pts, dims, scalars, {'jump_velocity': jump_v})
    written.append(path)

    pts, dims = surface_points(cfg, x, y, z, 'i', 0)
    jump_v = vel[:, :, 0, :] - vel[:, :, -1, :]
    scalars = {
        'jump_rho': rho[:, :, 0] - rho[:, :, -1],
        'jump_velocity_norm': np.sqrt(np.sum(jump_v * jump_v, axis=-1)),
    }
    path = os.path.join(out_dir, 'periodic_i_jump.vtk')
    write_polydata(path, pts, dims, scalars, {'jump_velocity': jump_v})
    written.append(path)
    return written


def write_text_report(path, metrics, vtk_files):
    with open(path, 'w', newline='\n') as f:
        f.write('Checkpoint Visual Audit\n')
        f.write('overall: {}\n\n'.format(metrics['overall']))
        f.write('Checks:\n')
        for k in sorted(metrics['checks']):
            f.write('  {:28s} {}\n'.format(k, metrics['checks'][k]))
        f.write('\nMetrics:\n')
        for k in sorted(metrics):
            if k in ('checks',):
                continue
            f.write('  {:32s} {}\n'.format(k, metrics[k]))
        f.write('\nVTK files:\n')
        for p in vtk_files:
            f.write('  {}\n'.format(p))


def main():
    ap = argparse.ArgumentParser(description='Audit restart checkpoint continuity and write VTK surfaces.')
    ap.add_argument('--checkpoint-dir', required=True,
                    help='Checkpoint folder containing metadata.dat, f00_*.bin..f18_*.bin, rho_*.bin')
    ap.add_argument('--grid-dat', default=None,
                    help='Tecplot grid .dat. If omitted, uses metadata interp_new_grid when available.')
    ap.add_argument('--out-dir', default=None,
                    help='Output audit directory. Default: <checkpoint-dir>/audit_vtk')
    ap.add_argument('--metric-order', type=int, choices=[2, 6], default=6,
                    help='Metric order for divergence diagnostic; default 6.')
    ap.add_argument('--skip-fneq-ratio', action='store_true',
                    help='Skip second f-file pass for max |fneq/feq|.')
    args = ap.parse_args()

    checkpoint_dir = os.path.abspath(args.checkpoint_dir)
    out_dir = os.path.abspath(args.out_dir or os.path.join(checkpoint_dir, 'audit_vtk'))

    t0 = time.time()
    cfg, meta = build_cfg(checkpoint_dir, args.grid_dat)
    print('checkpoint: {}'.format(checkpoint_dir))
    print('grid:       {}'.format(cfg.GRID_DAT))
    print('dims:       NX={} NY={} NZ={} jp={}'.format(cfg.NX, cfg.NY, cfg.NZ, cfg.JP))

    fields = read_checkpoint_macros(
        checkpoint_dir, cfg, want_fneq_ratio=not args.skip_fneq_ratio)
    metrics = compute_audit_metrics(cfg, fields, metric_order=args.metric_order)
    vtk_files = write_vtk_suite(out_dir, cfg, fields)

    metrics['checkpoint_dir'] = checkpoint_dir
    metrics['grid_dat'] = os.path.abspath(cfg.GRID_DAT)
    metrics['metadata_interp_macro_mode'] = meta.get('interp_macro_mode')
    metrics['metadata_interp_fneq_mode'] = meta.get('interp_fneq_mode')
    metrics['metadata_interp_metric_order'] = meta.get('interp_metric_order')
    metrics['audit_seconds'] = time.time() - t0

    json_path = os.path.join(out_dir, 'audit_metrics.json')
    txt_path = os.path.join(out_dir, 'audit_report.txt')
    with open(json_path, 'w', newline='\n') as f:
        json.dump(metrics, f, indent=2, sort_keys=True)
    write_text_report(txt_path, metrics, vtk_files)

    print('overall: {}'.format(metrics['overall']))
    print('report:  {}'.format(txt_path))
    print('json:    {}'.format(json_path))
    print('vtk dir: {}'.format(out_dir))


if __name__ == '__main__':
    main()
