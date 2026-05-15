#!/usr/bin/env python3
"""
Density Stability Index — quantify the transition from noisy transient
to clean steady-state oscillation in checkrho.dat.

Three complementary metrics, all computed in sliding windows:

1. Spectral Purity Index (SPI):
     SPI = P_peak / P_total   (FFT-based)
     0 = broadband noise (all frequencies equal)  →  1 = pure sine wave

2. Waveform Regularity (R²):
     Fit a sine wave A·sin(2πf·t + φ) + C to each window.
     R² → 1 means the signal is perfectly sinusoidal.

3. Amplitude Stability (AS):
     AS = 1 - CV(envelope) = 1 - std(|peaks|) / mean(|peaks|)
     1 = constant amplitude  →  0 = wildly varying amplitude

Composite:
   DSI = (SPI × R² × AS)^(1/3)   ∈ [0, 1]
   0 = noisy/unstable  →  1 = clean steady sinusoidal oscillation
"""
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.signal import hilbert
from scipy.optimize import curve_fit
import os

CHECKRHO = os.path.join(os.path.dirname(__file__), '..', 'checkrho.dat')


def load_checkrho(path):
    ftts, drho = [], []
    with open(path) as f:
        for line in f:
            p = line.split()
            if len(p) >= 6:
                ftts.append(float(p[1]))
                drho.append(float(p[4]))
    return np.array(ftts), np.array(drho)


def spectral_purity(x):
    if len(x) < 8:
        return np.nan
    x = x - x.mean()
    ps = np.abs(np.fft.rfft(x))**2
    ps[0] = 0
    total = ps.sum()
    return ps.max() / total if total > 0 else 0.0


def sine_r2(t, x):
    if len(t) < 8:
        return np.nan
    x0 = x - x.mean()
    ps = np.abs(np.fft.rfft(x0))**2
    ps[0] = 0
    if ps.sum() == 0:
        return 0.0
    dt = np.mean(np.diff(t))
    freqs = np.fft.rfftfreq(len(x0), d=dt)
    f_dom = freqs[np.argmax(ps)]
    if f_dom == 0:
        return 0.0

    def model(tt, A, phi, C):
        return A * np.sin(2 * np.pi * f_dom * tt + phi) + C
    try:
        popt, _ = curve_fit(model, t, x,
                            p0=[np.std(x), 0, np.mean(x)],
                            maxfev=2000)
        ss_res = np.sum((x - model(t, *popt))**2)
        ss_tot = np.sum((x - x.mean())**2)
        return max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0
    except Exception:
        return np.nan


def amplitude_stability(x):
    if len(x) < 16:
        return np.nan
    x0 = x - x.mean()
    env = np.abs(hilbert(x0))
    m = env.mean()
    if m == 0:
        return 0.0
    cv = env.std() / m
    return max(0.0, 1.0 - cv)


def rolling(ftt, drho, func, win_ftt=0.25, step_ftt=0.02):
    centres, values = [], []
    t0 = ftt[0]
    while t0 + win_ftt <= ftt[-1]:
        mask = (ftt >= t0) & (ftt < t0 + win_ftt)
        idx = np.where(mask)[0]
        if len(idx) >= 16:
            needs_t = (func.__code__.co_varnames[:2] == ('t', 'x'))
            val = func(ftt[idx], drho[idx]) if needs_t else func(drho[idx])
            centres.append(t0 + win_ftt / 2)
            values.append(val)
        t0 += step_ftt
    return np.array(centres), np.array(values)


def main():
    ftt, drho = load_checkrho(CHECKRHO)
    print(f'Loaded {len(ftt)} samples, FTT {ftt[0]:.4f} ~ {ftt[-1]:.4f}')

    WIN = 0.25
    STEP = 0.02

    print('Computing SPI...')
    c_spi, v_spi = rolling(ftt, drho, spectral_purity, WIN, STEP)

    print('Computing R² (sine fit)...')
    c_r2, v_r2 = rolling(ftt, drho, sine_r2, WIN, STEP)

    print('Computing Amplitude Stability...')
    c_as, v_as = rolling(ftt, drho, amplitude_stability, WIN, STEP)

    v_spi_c = np.clip(v_spi, 0, 1)
    v_r2_c  = np.clip(v_r2, 0, 1)
    v_as_c  = np.clip(v_as, 0, 1)
    composite = (v_spi_c * v_r2_c * v_as_c) ** (1.0 / 3.0)

    fig, axes = plt.subplots(5, 1, figsize=(14, 14), sharex=True,
                             gridspec_kw={'height_ratios': [1.2, 1, 1, 1, 1.2]})

    ax = axes[0]
    ax.plot(ftt, drho, color='steelblue', lw=0.3, alpha=0.7)
    ax.set_ylabel(r'$\Delta\rho^+$', fontsize=12)
    ax.set_title('Density Oscillation: Transient to Steady-State Transition',
                 fontsize=14, fontweight='bold')
    ax.ticklabel_format(axis='y', style='sci', scilimits=(-10, -10))
    ax.axhline(0, color='gray', lw=0.5, ls='--')

    ax = axes[1]
    ax.plot(c_spi, v_spi, color='#e74c3c', lw=1.5)
    ax.fill_between(c_spi, 0, v_spi, color='#e74c3c', alpha=0.15)
    ax.set_ylabel('SPI', fontsize=12)
    ax.set_ylim(0, 1)
    ax.axhline(0.5, color='gray', lw=0.5, ls=':')

    ax = axes[2]
    ax.plot(c_r2, v_r2, color='#2ecc71', lw=1.5)
    ax.fill_between(c_r2, 0, v_r2, color='#2ecc71', alpha=0.15)
    ax.set_ylabel(r'$R^2_{\sin}$', fontsize=12)
    ax.set_ylim(0, 1)
    ax.axhline(0.5, color='gray', lw=0.5, ls=':')

    ax = axes[3]
    ax.plot(c_as, v_as, color='#3498db', lw=1.5)
    ax.fill_between(c_as, 0, v_as, color='#3498db', alpha=0.15)
    ax.set_ylabel('AS', fontsize=12)
    ax.set_ylim(0, 1)
    ax.axhline(0.5, color='gray', lw=0.5, ls=':')

    ax = axes[4]
    ax.plot(c_spi, composite, color='#8e44ad', lw=2.5)
    ax.fill_between(c_spi, 0, composite, color='#8e44ad', alpha=0.15)
    ax.set_ylabel('DSI', fontsize=12)
    ax.set_ylim(0, 1)
    ax.set_xlabel('FTT (Flow-Through Time)', fontsize=12)
    ax.axhline(0.5, color='gray', lw=0.5, ls=':')

    for a in axes:
        a.axvspan(0, 0.5, color='red', alpha=0.04)
        a.axvline(0.5, color='red', lw=0.8, ls='--', alpha=0.4)

    plt.tight_layout()
    out_png = os.path.join(os.path.dirname(__file__), 'density_stability_index.png')
    out_pdf = out_png.replace('.png', '.pdf')
    plt.savefig(out_png, dpi=150, bbox_inches='tight')
    plt.savefig(out_pdf, bbox_inches='tight')
    print(f'\nSaved: {out_png}')
    print(f'Saved: {out_pdf}')

    print('\n' + '=' * 70)
    print('  Density Stability Index  Summary by FTT phase')
    print('=' * 70)
    for lo, hi, label in [(0, 0.5, 'Transient  (0-0.5)'),
                          (0.5, 1.5, 'Settling   (0.5-1.5)'),
                          (1.5, 3.0, 'Steady-1   (1.5-3.0)'),
                          (3.0, 99,  'Steady-2   (3.0-end)')]:
        mask = (c_spi >= lo) & (c_spi < hi)
        if mask.sum() == 0:
            continue
        print(f'\n  {label}:')
        print(f'    SPI  = {v_spi[mask].mean():.3f} +/- {v_spi[mask].std():.3f}')
        print(f'    R2   = {np.nanmean(v_r2[mask]):.3f} +/- {np.nanstd(v_r2[mask]):.3f}')
        print(f'    AS   = {v_as[mask].mean():.3f} +/- {v_as[mask].std():.3f}')
        print(f'    DSI  = {composite[mask].mean():.3f} +/- {composite[mask].std():.3f}')


if __name__ == '__main__':
    main()
