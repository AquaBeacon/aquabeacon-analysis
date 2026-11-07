"""Descriptive field-review calculations without ground-truth assumptions."""
from __future__ import annotations

import numpy as np


def centered_scatter(points):
    """Population RMS about the observed mean, never an accuracy estimate."""
    p = np.asarray(points, dtype=float)
    if p.ndim != 2 or p.shape[1] != 3 or len(p) < 2 or not np.isfinite(p).all():
        raise ValueError("At least two finite 3D points are required")
    mean = p.mean(axis=0)
    residual = p - mean
    variance = (residual**2).mean(axis=0)
    return {"samples": len(p), "mean_m": mean.tolist(),
            "axis_rms_m": np.sqrt(variance).tolist(),
            "horizontal_rms_m": float(np.sqrt(variance[:2].sum())),
            "rms_3d_m": float(np.sqrt(variance.sum()))}


def directional_scatter(points):
    """Orthogonal decomposition about the mean line of sight.

    Radial/tangential describe position variation, not identified sensor errors.
    """
    p = np.asarray(points, dtype=float)
    stats = centered_scatter(p)
    mean = np.asarray(stats["mean_m"])
    distance = float(np.linalg.norm(mean))
    if distance < 1e-9:
        raise ValueError("The mean direction is undefined at zero distance")
    u = mean / distance
    residual = p - mean
    radial = residual @ u
    tangent = residual - radial[:, None] * u
    rv = float(np.mean(radial**2))
    tv = float(np.mean(np.sum(tangent**2, axis=1)))
    return {**stats, "mean_vector_length_m": distance,
            "radial_rms_m": float(np.sqrt(rv)), "tangential_rms_m": float(np.sqrt(tv)),
            "tangential_variance_fraction": tv / (tv + rv) if tv + rv else 0.0,
            "equivalent_tangent_angle_deg": float(np.rad2deg(np.sqrt(tv) / distance)),
            "variance_closure_m2": abs(stats["rms_3d_m"]**2 - rv - tv)}


def nearest_indices(sorted_times, targets):
    """Return deterministic nearest neighbors; ties choose the earlier sample."""
    t = np.asarray(sorted_times, dtype=float)
    q = np.asarray(targets, dtype=float)
    if len(t) == 0 or not np.isfinite(t).all() or np.any(np.diff(t) <= 0):
        raise ValueError("Times must be finite, nonempty and strictly increasing")
    right = np.clip(np.searchsorted(t, q), 0, len(t) - 1)
    left = np.clip(right - 1, 0, len(t) - 1)
    return np.where(abs(q - t[left]) <= abs(q - t[right]), left, right)


def valid_bracket_support(times, valid, targets, max_gap_s=0.15):
    """Preserve missing values; no support across invalid or distant neighbors."""
    t = np.asarray(times, dtype=float)
    q = np.asarray(targets, dtype=float)
    good = np.asarray(valid, dtype=bool)
    nearest_indices(t, q)  # Validate the clock ordering.
    if len(t) < 2 or len(good) != len(t):
        raise ValueError("Need at least two samples and matching validity flags")
    right = np.searchsorted(t, q)
    j = np.clip(right, 1, len(t) - 1)
    result = ((q >= t[0]) & (q <= t[-1]) & good[j - 1] & good[j]
              & (t[j] - t[j - 1] <= max_gap_s))
    nearest = nearest_indices(t, q)
    exact = np.isclose(q, t[nearest], rtol=0, atol=1e-7)
    return np.where(exact, good[nearest], result)


def observation_coverage(times, start_s, end_s):
    """Include unobserved leading/trailing duration in the coverage audit."""
    t = np.asarray(times, dtype=float)
    if len(t) == 0:
        return {"observations": 0, "maximum_gap_with_edges_s": end_s - start_s}
    if np.any(np.diff(t) <= 0) or t[0] < start_s or t[-1] >= end_s:
        raise ValueError("Unique ordered observations must be within [start, end)")
    return {"observations": len(t), "leading_gap_s": float(t[0] - start_s),
            "trailing_gap_s": float(end_s - t[-1]),
            "maximum_gap_with_edges_s": float(np.diff(np.r_[start_s, t, end_s]).max())}
