#!/usr/bin/env python3
"""
mass_monitor.py — SKIP_ALL_MASSCORR 測試專用質量漂移監控
=========================================================
讀取 checkrho.dat，產出：
  1. mass_drift.png / .pdf  — 漂移趨勢圖 (含閾值線 + 外推)
  2. stdout 摘要報告         — 可被 watcher / cron 擷取

用法:
  python3 live/mass_monitor.py                  # 從 PROJECT_ROOT 執行
  python3 live/mass_monitor.py --check-only     # 只印摘要不出圖
  python3 live/mass_monitor.py --alert=1e-3     # 自訂警報閾值 (預設 1e-2)
"""

import sys, os, time
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "result"))
os.chdir(PROJECT_DIR)

CHECKRHO = "checkrho.dat"
OUT_DIR  = os.path.join(PROJECT_DIR, "live")
PNG_OUT  = os.path.join(OUT_DIR, "mass_drift.png")
PDF_OUT  = os.path.join(OUT_DIR, "mass_drift.pdf")

THRESHOLDS = [
    (1e-4, "1e-4 (minor)",   "gold"),
    (1e-3, "1e-3 (caution)", "orange"),
    (1e-2, "1e-2 (WARNING)", "red"),
    (5e-2, "5e-2 (DANGER)",  "darkred"),
]

def load_checkrho():
    if not os.path.isfile(CHECKRHO):
        print(f"[mass_monitor] ERROR: {CHECKRHO} 不存在"); sys.exit(1)
    data = np.loadtxt(CHECKRHO, comments="#")
    if data.ndim == 1:
        data = data.reshape(1, -1)
    if len(data) < 2:
        print("[mass_monitor] 資料不足 (< 2 rows)"); sys.exit(0)
    return data

def analyze(data, alert_threshold=1e-2):
    steps = data[:, 0].astype(int)
    ftt   = data[:, 1]
    drift = data[:, 4]
    corr  = data[:, 5]
    skip  = data[:, 6].astype(int)

    n = len(steps)
    abs_drift = np.abs(drift)
    current_drift = drift[-1]
    current_ftt   = ftt[-1]
    max_abs_drift = np.max(abs_drift)

    corr_all_zero = np.all(np.abs(corr) < 1e-30)
    skip_all_one  = np.all(skip == 1)

    half = max(n // 2, 1)
    p = np.polyfit(ftt[half:], drift[half:], 1) if n > 10 else [0, 0]
    rate_per_ftt = p[0]

    extrap = {}
    for thr, label, _ in THRESHOLDS:
        if abs(rate_per_ftt) > 0:
            ftt_remain = (thr - abs(current_drift)) / abs(rate_per_ftt)
            if ftt_remain > 0:
                extrap[thr] = current_ftt + ftt_remain
            else:
                extrap[thr] = -1  # already exceeded or reversing
        else:
            extrap[thr] = float("inf")

    alert = abs(current_drift) >= alert_threshold

    return {
        "n": n, "steps": steps, "ftt": ftt, "drift": drift,
        "current_drift": current_drift, "current_ftt": current_ftt,
        "max_abs_drift": max_abs_drift, "rate_per_ftt": rate_per_ftt,
        "fit_poly": p, "extrap": extrap, "alert": alert,
        "alert_threshold": alert_threshold,
        "corr_all_zero": corr_all_zero, "skip_all_one": skip_all_one,
    }

def print_report(r):
    ts = time.strftime("%F %T")
    print(f"{'='*60}")
    print(f" Mass Drift Monitor @ {ts}")
    print(f"{'='*60}")
    print(f"  步數:    {int(r['steps'][0])} ~ {int(r['steps'][-1])}  ({r['n']} records)")
    print(f"  FTT:     {r['ftt'][0]:.4f} ~ {r['current_ftt']:.4f}")
    print(f"  目前 drift:   {r['current_drift']:+.6e}")
    print(f"  最大|drift|:  {r['max_abs_drift']:.6e}")
    print(f"  漂移速率:     {r['rate_per_ftt']:+.4e} / FTT")
    print()
    print(f"  --- 外推預估 (線性) ---")
    for thr, label, _ in THRESHOLDS:
        eta = r["extrap"].get(thr)
        if eta is None or eta == float("inf"):
            est = "永不到達 (rate≈0)"
        elif eta < 0:
            est = "已超過 或 方向反轉"
        else:
            remain = eta - r["current_ftt"]
            est = f"FTT ≈ {eta:.1f}  (還需 {remain:.1f} FTT)"
        print(f"    |drift| = {label}: {est}")
    print()
    print(f"  --- 驗證 ---")
    print(f"    Col6 全為 0: {'✓' if r['corr_all_zero'] else '✗ 異常！有非零修正值'}")
    print(f"    Col7 全為 1: {'✓' if r['skip_all_one'] else '✗ 異常！部分步未停用修正'}")
    if r["alert"]:
        print(f"\n  *** ALERT: |drift| = {abs(r['current_drift']):.3e} >= 閾值 {r['alert_threshold']:.0e} ***")
    else:
        print(f"\n  狀態: 正常 (|drift| < {r['alert_threshold']:.0e})")
    print(f"{'='*60}")

def plot(r):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import ScalarFormatter
    from plot_style import apply_style
    apply_style()

    ftt = r["ftt"]
    drift = r["drift"]
    abs_drift = np.abs(drift)
    p = r["fit_poly"]

    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True,
                             gridspec_kw={"height_ratios": [3, 2, 2]})

    LG_SIZE = 9

    # --- Panel 1: drift vs FTT (y-axis auto-scaled to data) ---
    ax1 = axes[0]
    ax1.plot(ftt, drift, "-", color="purple", linewidth=1.2, label=r"$\langle\rho\rangle - 1$")
    ftt_ext = min(ftt[-1] * 1.5, ftt[-1] + 5)
    ftt_fit = np.linspace(ftt[0], ftt_ext, 200)
    ax1.plot(ftt_fit, np.polyval(p, ftt_fit), "--", color="gray", alpha=0.8,
             label=f"linear fit (rate={p[0]:+.2e}/FTT)")
    data_absmax = max(abs(drift.min()), abs(drift.max()), 1e-15)
    fit_at_ext = abs(np.polyval(p, ftt_ext))
    ylim_val = max(data_absmax, fit_at_ext) * 1.4
    ax1.set_ylim(-ylim_val, ylim_val)
    for thr, label, color in THRESHOLDS:
        if thr <= ylim_val * 1.2:
            ax1.axhline(y=-thr, color=color, linestyle="--", alpha=0.85, linewidth=1.0)
            ax1.axhline(y=+thr, color=color, linestyle="--", alpha=0.85, linewidth=1.0, label=f"$\\pm${label}")
    ax1.set_ylabel(r"$\langle\rho\rangle - 1$")
    ax1.legend(fontsize=LG_SIZE, ncol=2, frameon=False, loc="best")
    ax1.grid(True, alpha=0.3)
    ax1.yaxis.set_major_formatter(ScalarFormatter(useMathText=True))
    ax1.ticklabel_format(axis="y", style="sci", scilimits=(-3, -3))

    # --- Panel 2: |drift| log scale ---
    ax2 = axes[1]
    ax2.semilogy(ftt, abs_drift + 1e-30, "-", color="darkviolet", linewidth=1.2,
                 label=r"$|\langle\rho\rangle - 1|$")
    for thr, label, color in THRESHOLDS:
        ax2.axhline(y=thr, color=color, linestyle="--", alpha=0.85, linewidth=1.0, label=f"{label}")
    ax2.set_ylabel(r"$|\langle\rho\rangle - 1|$  (log)")
    ax2.legend(fontsize=LG_SIZE, ncol=2, frameon=False, loc="best")
    ax2.grid(True, alpha=0.3, which="both")
    ax2.set_ylim(bottom=max(1e-14, abs_drift[abs_drift > 0].min() * 0.1) if np.any(abs_drift > 0) else 1e-14)

    # --- Panel 3: drift rate (Savitzky-Golay smoothed) ---
    ax3 = axes[2]
    if len(ftt) > 3:
        dftt = np.diff(ftt)
        dftt[dftt == 0] = 1e-30
        ddrift = np.diff(drift)
        rate = ddrift / dftt
        window = min(21, len(rate) // 3)
        if window > 3 and window % 2 == 0:
            window += 1
        if window >= 3:
            from scipy.signal import savgol_filter
            try:
                rate_smooth = savgol_filter(rate, window, 2)
            except Exception:
                rate_smooth = np.convolve(rate, np.ones(window)/window, mode="same")
        else:
            rate_smooth = rate
        ftt_mid = 0.5 * (ftt[:-1] + ftt[1:])
        ax3.plot(ftt_mid, rate_smooth, "-", color="crimson", linewidth=1.2,
                 label=r"$\mathrm{d}(\mathrm{drift})/\mathrm{d}(\mathrm{FTT})$")
        ax3.axhline(y=0, color="black", linewidth=0.5)
    ax3.set_ylabel("drift rate  [/FTT]")
    ax3.set_xlabel("FTT (Flow-Through Time)")
    ax3.legend(fontsize=LG_SIZE, frameon=False, loc="best")
    ax3.grid(True, alpha=0.3)
    ax3.yaxis.set_major_formatter(ScalarFormatter(useMathText=True))
    ax3.ticklabel_format(axis="y", style="sci", scilimits=(-3, -3))

    fig.tight_layout()
    fig.savefig(PNG_OUT, dpi=150, bbox_inches="tight")
    fig.savefig(PDF_OUT, bbox_inches="tight")
    plt.close(fig)
    print(f"  [plot] {PNG_OUT}")
    print(f"  [plot] {PDF_OUT}")

def main():
    check_only = "--check-only" in sys.argv
    alert_thr = 1e-2
    for arg in sys.argv[1:]:
        if arg.startswith("--alert="):
            alert_thr = float(arg.split("=", 1)[1])

    data = load_checkrho()
    r = analyze(data, alert_threshold=alert_thr)
    print_report(r)

    if not check_only:
        try:
            plot(r)
        except Exception as e:
            print(f"  [plot] ERROR: {e}")

    sys.exit(1 if r["alert"] else 0)

if __name__ == "__main__":
    main()
