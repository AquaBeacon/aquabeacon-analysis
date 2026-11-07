"""Descriptive envelope events; these metrics do not identify acoustic propagation paths."""

import numpy as np


def burst_offsets_s(frequency_hz, pattern, gap1_us, gap2_us):
    """Mirror acquisition.h integer PIO spacing, referenced to the first received burst."""
    if not pattern:
        return np.array([0.0])
    spacing = [
        (5 * 4000000 + int(g) * frequency_hz * 4 + 3000000) // (4 * frequency_hz) / 1e6
        for g in (gap1_us, gap2_us)
    ]
    return np.array([0.0, spacing[0], sum(spacing)])


def envelope_events(time_s, adc, threshold_adc=20.0, merge_gap_s=0.0005):
    """Group above-threshold samples, retaining time gaps and the unsmoothed maximum.

    Baseline/MAD are estimated from the last second, which may itself contain signals.
    No claim that this interval is independent noise. Threshold is max(floor, 6 robust SD).
    Events separated by <= merge_gap_s are merged, a descriptive counting convention.
    """
    t, y = np.asarray(time_s), np.asarray(adc)
    if len(t) != len(y) or len(t) < 2 or not np.all(np.diff(t) > 0):
        raise ValueError("Matching arrays and strictly increasing times required")
    if not np.isfinite(y).all() or not np.isfinite(t).all():
        raise ValueError("Nonfinite samples must be handled explicitly")
    tail = y[t >= t[-1] - 1.0]
    baseline = float(np.median(tail))
    sigma = float(1.4826 * np.median(np.abs(tail - baseline)))
    threshold = baseline + max(threshold_adc, 6 * sigma)
    active = np.flatnonzero(y > threshold)
    groups = np.split(active, np.flatnonzero(np.diff(t[active]) > merge_gap_s + 1e-12) + 1)
    events = []
    for group in groups:
        if not len(group):
            continue
        peak = group[np.argmax(y[group])]
        events.append(
            {
                "start_s": float(t[group[0]]),
                "end_s": float(t[group[-1]]),
                "peak_s": float(t[peak]),
                "peak_adc": float(y[peak]),
                "peak_excess_adc": float(y[peak] - baseline),
                "above_threshold_samples": len(group),
            }
        )
    return {
        "baseline_adc": baseline,
        "tail_robust_sigma_adc": sigma,
        "threshold_adc": threshold,
        "events": events,
        "active_fraction": float(len(active) / len(y)),
    }


def max_bins(time_s, adc, width_s=0.001):
    """Peak-preserving bins for display only; empty bins stay NaN."""
    t, y = np.asarray(time_s), np.asarray(adc)
    bins = np.floor(t / width_s).astype(int)
    if np.any(bins < 0):
        raise ValueError("Display bins require nonnegative times")
    out = np.full(bins.max() + 1, -np.inf)
    np.maximum.at(out, bins, y)
    out[out == -np.inf] = np.nan
    return (np.arange(len(out)) + 0.5) * width_s, out
