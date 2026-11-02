"""Gap-aware exploratory spectra; no causal attribution or ground-truth error."""

import numpy as np
from scipy import signal


def finite_runs(mask):
    """Half-open contiguous valid intervals, without joining missing samples."""
    edges = np.diff(np.r_[False, np.asarray(mask, dtype=bool), False].astype(int))
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1), strict=True))


def clean_series(times, values):
    t, y = np.asarray(times, float), np.asarray(values, float)
    good = np.isfinite(t)
    t, ids = np.unique(t[good], return_index=True)
    y = y[good][ids]
    if len(t) < 2:
        raise ValueError("At least two distinct timestamps required")
    return t, y


def regularize(times, values, grid, max_gap_s):
    """Linear jitter correction only, preserving invalid records and long gaps."""
    t, y = clean_series(times, values)
    grid = np.asarray(grid, float)
    hi = np.clip(np.searchsorted(t, grid, side="right"), 1, len(t) - 1)
    lo = hi - 1
    good = (
        (grid >= t[0])
        & (grid <= t[-1])
        & (t[hi] - t[lo] <= max_gap_s)
        & np.isfinite(y[lo])
        & np.isfinite(y[hi])
    )
    out = np.full(len(grid), np.nan)
    out[good] = y[lo[good]] + (grid[good] - t[lo[good]]) / (t[hi[good]] - t[lo[good]]) * (
        y[hi[good]] - y[lo[good]]
    )
    # Exact observations remain available even next to an outage.
    nearest = np.clip(np.searchsorted(t, grid), 0, len(t) - 1)
    exact = np.abs(t[nearest] - grid) < 1e-7
    out[exact] = y[nearest[exact]]
    return out


def spectrum(times, values, maximum_hz=0.45, minimum_run_s=8):
    """Hann FFT on longest gap-free run plus Lomb-Scargle on observed samples.

    RMSE uses all finite original values, no detrending. Spectra remove a linear
    trend, record its slope, and never synthesize observations across outages.
    """
    t, y = clean_series(times, values)
    cadence = float(np.median(np.diff(t)))
    fs = 1 / cadence
    valid = np.isfinite(y)
    x, v = t[valid] - t[0], y[valid]
    if len(v) < 4:
        return {"status": "insufficient_samples", "n": len(v)}, {}
    slope, intercept = np.polyfit(x, v, 1)
    residual = v - (slope * x + intercept)
    high = min(maximum_hz, 0.45 * fs)
    span = float(x[-1] - x[0])
    low = 2 / span if span > 0 else high
    info = {
        "status": "ok",
        "n": len(v),
        "records": len(t),
        "cadence_s": cadence,
        "sampling_hz": fs,
        "nyquist_hz": 0.5 * fs,
        "span_s": span,
        "rmse_about_mean": float(np.std(v)),
        "linear_slope_per_s": float(slope),
        "detrended_rmse": float(np.sqrt(np.mean(residual**2))),
        "maximum_observed_gap_s": float(np.diff(x).max()),
        "search_band_hz": [low, high],
    }
    arrays = {}
    if high > low and np.std(residual) > 1e-12:
        freq = np.linspace(low, high, max(100, int((high - low) * span * 8)))
        power = signal.lombscargle(
            x, residual, 2 * np.pi * freq, normalize=True, floating_mean=True
        )
        peak = int(np.argmax(power))
        info.update(
            lomb_peak_hz=float(freq[peak]),
            lomb_period_s=float(1 / freq[peak]),
            lomb_peak_normalized=float(power[peak]),
            independent_resolution_hz=1 / span,
        )
        arrays.update(lomb_frequency_hz=freq, lomb_normalized_power=power)
    grid = t[0] + np.arange(int((t[-1] - t[0]) / cadence) + 1) * cadence
    uniform = regularize(t, y, grid, 1.5 * cadence)
    runs = finite_runs(np.isfinite(uniform))
    a, b = max(runs, key=lambda ab: ab[1] - ab[0]) if runs else (0, 0)
    info.update(
        regular_grid_n=len(grid),
        grid_valid_n=int(np.isfinite(uniform).sum()),
        interpolation_max_gap_s=1.5 * cadence,
        fft_run_start_offset_s=float(grid[a] - t[0]) if b > a else None,
        fft_samples=int(b - a),
        fft_duration_s=(b - a) * cadence,
    )
    if b - a >= 8 and (b - a) * cadence >= minimum_run_s:
        f, p = signal.periodogram(
            uniform[a:b], fs=fs, window="hann", detrend="linear", scaling="density"
        )
        arrays.update(fft_frequency_hz=f, fft_psd=p)
        native_ids = np.flatnonzero((f >= 2 / ((b - a) * cadence)) & (f <= 0.45 * fs))
        if len(native_ids) and p[native_ids].max() > 0:
            native_peak = native_ids[np.argmax(p[native_ids])]
            info["fft_native_peak_hz"] = float(f[native_peak])
        sel = np.flatnonzero((f >= 2 / ((b - a) * cadence)) & (f <= high))
        if len(sel) and p[sel].max() > 0:
            peak = sel[np.argmax(p[sel])]
            info.update(
                fft_peak_hz=float(f[peak]),
                fft_period_s=float(1 / f[peak]),
                fft_resolution_hz=fs / (b - a),
            )
        # Orthogonal Fourier decomposition of this run after linear detrending.
        z = signal.detrend(uniform[a:b], type="linear")
        ft = np.fft.rfft(z)
        power = np.abs(ft) ** 2 / len(z) ** 2
        power[1 : len(power) - (1 if len(z) % 2 == 0 else 0)] *= 2
        bands = {
            "below_0_05": (0, 0.05),
            "0_05_to_0_45": (0.05, 0.45),
            "above_0_45": (0.45, np.inf),
        }
        info["fft_run_band_rms"] = {
            name: float(np.sqrt(np.sum(power[(f >= lo) & (f < hi)])))
            for name, (lo, hi) in bands.items()
        }
        info["fft_run_parseval_variance"] = float(power.sum())
        info["fft_run_detrended_variance"] = float(np.mean(z * z))
        info["fft_run_fraction_above_0_45_hz"] = (
            float(power[f >= 0.45].sum() / power.sum()) if power.sum() > 0 else 0.0
        )
    else:
        info["fft_status"] = "No contiguous run long enough; Lomb uses observed samples only"
    return info, arrays


def common_grid_series(times, values, grid, fs=1):
    """Anti-alias higher-rate series before reduction; never fill a long gap."""
    t, y = clean_series(times, values)
    dt = float(np.median(np.diff(t)))
    native_fs = 1 / dt
    if native_fs > fs * 1.5:
        native_grid = t[0] + np.arange(int((t[-1] - t[0]) / dt) + 1) * dt
        native = regularize(t, y, native_grid, 1.5 * dt)
        filtered = np.full_like(native, np.nan)
        sos = signal.butter(4, 0.4 * fs, fs=native_fs, output="sos")
        for a, b in finite_runs(np.isfinite(native)):
            if b - a > max(20, int(6 * native_fs)):
                filtered[a:b] = signal.sosfiltfilt(sos, native[a:b])
        return regularize(native_grid, filtered, grid, 1.5 * dt)
    return regularize(t, y, grid, 1.5 * dt)


def paired_spectra(x, y, fs=1, window_s=16):
    """Welch cross-spectrum on complete windows only; >=3 averages required.

    Overlapping windows are not independent. Coherence is descriptive, without
    a significance threshold or inferred causal/independent noise fraction.
    """
    x, y = np.asarray(x, float), np.asarray(y, float)
    if x.shape != y.shape:
        raise ValueError("Paired arrays must have matching shapes")
    n = round(window_s * fs)
    hop = n // 2
    window = signal.get_window("hann", n)
    xx, yy, xy = [], [], []
    for a, b in finite_runs(np.isfinite(x) & np.isfinite(y)):
        for start in range(a, b - n + 1, hop):
            fx = np.fft.rfft(signal.detrend(x[start : start + n]) * window)
            fy = np.fft.rfft(signal.detrend(y[start : start + n]) * window)
            xx.append(np.abs(fx) ** 2)
            yy.append(np.abs(fy) ** 2)
            xy.append(fx.conj() * fy)
    info = {
        "welch_windows": len(xx),
        "window_s": window_s,
        "overlap_fraction": 0.5,
        "paired_grid_samples": int((np.isfinite(x) & np.isfinite(y)).sum()),
        "status": "ok" if len(xx) >= 3 else "insufficient_complete_windows",
    }
    if len(xx) < 3:
        return info, {}
    pxx, pyy, pxy = (np.mean(a, axis=0) for a in [xx, yy, xy])
    coherence = np.divide(
        np.abs(pxy) ** 2, pxx * pyy, out=np.full_like(pxx, np.nan), where=pxx * pyy > 1e-24
    )
    return info, {
        "frequency_hz": np.fft.rfftfreq(n, 1 / fs),
        "coherence": coherence,
        "phase_y_minus_x_rad": np.angle(pxy),
        "pxx": pxx,
        "pyy": pyy,
    }


def variance_identity(fixed, corrected):
    """Exact signed covariance accounting, not independent source separation."""
    a, b = np.asarray(fixed, float), np.asarray(corrected, float)
    good = np.isfinite(a).all(1) & np.isfinite(b).all(1)
    a, b = a[good], b[good]
    a, b = a - a.mean(0), b - b.mean(0)
    d = b - a
    return {
        "n": len(a),
        "fixed_variance_m2": float(np.mean(np.sum(a * a, axis=1))),
        "correction_variance_m2": float(np.mean(np.sum(d * d, axis=1))),
        "twice_covariance_m2": float(2 * np.mean(np.sum(a * d, axis=1))),
        "corrected_variance_m2": float(np.mean(np.sum(b * b, axis=1))),
    }


def trend_periodic_decomposition(times, xyz, frequency_hz):
    """Orthogonal finite-sample accounting: trend, selected harmonic, remainder.

    Frequency must be chosen from attitude, not optimized on position. This is
    an exploratory fit, not a physical wave/noise separation or held-out score.
    """
    t, y = np.asarray(times, float), np.asarray(xyz, float)
    if len(t) < 6 or not np.isfinite(y).all() or frequency_hz <= 0:
        raise ValueError("Finite observations and a positive frequency required")
    t = t - t.mean()
    y = y - y.mean(0)
    drift_basis = t / np.linalg.norm(t)
    drift = drift_basis[:, None] * (drift_basis @ y)[None, :]
    periodic_basis = np.column_stack(
        [np.sin(2 * np.pi * frequency_hz * t), np.cos(2 * np.pi * frequency_hz * t)]
    )
    periodic_basis -= periodic_basis.mean(0)
    periodic_basis -= drift_basis[:, None] * (drift_basis @ periodic_basis)[None, :]
    q, _ = np.linalg.qr(periodic_basis)
    periodic = q @ (q.T @ y)
    remainder = y - drift - periodic
    parts = {"trend": drift, "attitude_frequency": periodic, "remainder": remainder}
    variances = {k: float(np.mean(np.sum(v * v, axis=1))) for k, v in parts.items()}
    total = float(np.mean(np.sum(y * y, axis=1)))
    return {
        "n": len(t),
        "frequency_hz": float(frequency_hz),
        "total_variance_m2": total,
        "variance_m2": variances,
        "rms_m": {k: float(np.sqrt(v)) for k, v in variances.items()},
        "closure_error_m2": float(sum(variances.values()) - total),
        "interpretation": "Orthogonal descriptive fit; remainder includes noise and unmodeled real motion",
    }
