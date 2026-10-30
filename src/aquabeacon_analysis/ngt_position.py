"""Auditable range/ray positions and availability without gap extrapolation."""
from __future__ import annotations

import cv2
import numpy as np


def calibrated_rays(pixels, matrix, distortion):
    """Unit rays in optical right/down/forward coordinates, preserving NaNs."""
    xy = np.asarray(pixels, dtype=float)
    out = np.full((len(xy), 3), np.nan)
    good = np.isfinite(xy).all(axis=1)
    if good.any():
        criteria = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 50, 1e-12)
        if hasattr(cv2, "undistortPointsIter"):
            p = cv2.undistortPointsIter(
                xy[good, None, :], np.asarray(matrix, float), np.asarray(distortion, float),
                None, None, criteria,
            )[:, 0, :]
        else:  # OpenCV 5 exposes the iteration criterion in the primary overload.
            p = cv2.undistortPoints(
                xy[good, None, :], np.asarray(matrix, float), np.asarray(distortion, float),
                criteria=criteria,
            )[:, 0, :]
        r = np.column_stack([p, np.ones(len(p))])
        out[good] = r/np.linalg.norm(r, axis=1)[:, None]
    return out


def nearest_indices(times, targets, max_delta):
    """Nearest event, including invalid events. Never jump across one to a valid event."""
    t, q = np.asarray(times), np.asarray(targets)
    finite = np.flatnonzero(np.isfinite(t))
    if not len(finite):
        return np.full(len(q), -1), np.full(len(q), np.nan)
    order = finite[np.argsort(t[finite], kind="stable")]
    t = t[order]
    hi = np.clip(np.searchsorted(t, q), 0, len(t)-1)
    lo = np.maximum(hi-1, 0)
    ix = np.where(abs(t[lo]-q) <= abs(t[hi]-q), lo, hi)
    delta = t[ix]-q
    good = np.isfinite(q) & (abs(delta) <= max_delta)
    return np.where(good, order[ix], -1), np.where(good, delta, np.nan)


def bracket_interpolate(times, values, targets, max_gap):
    """Interpolate finite adjacent ORIGINAL rows only, without bridging missing rows."""
    t, v, q = map(np.asarray, (times, values, targets))
    v = v.reshape(len(t), -1)
    order = np.argsort(t, kind="stable")
    t, v = t[order], v[order]
    t, unique = np.unique(t, return_index=True)
    v = v[unique]
    out = np.full((len(q), v.shape[1]), np.nan)
    if not len(t):
        return out
    hi = np.clip(np.searchsorted(t, q), 0, len(t)-1)
    exact = np.isclose(t[hi], q, atol=1e-7, rtol=0)
    out[exact] = v[hi[exact]]
    lo = np.maximum(hi-1, 0)
    good = (~exact & (q > t[0]) & (q < t[-1]) & (t[hi]-t[lo] <= max_gap)
            & np.isfinite(v[lo]).all(axis=1) & np.isfinite(v[hi]).all(axis=1))
    w = (q[good]-t[lo[good]])/(t[hi[good]]-t[lo[good]])
    out[good] = (1-w[:, None])*v[lo[good]]+w[:, None]*v[hi[good]]
    return out


def centered_scatter(xyz):
    a = np.asarray(xyz, float)
    a = a[np.isfinite(a).all(axis=1)]
    if not len(a):
        return {"count": 0}
    mean = a.mean(axis=0)
    residual = a-mean
    distance = np.linalg.norm(residual, axis=1)
    return {"count": len(a), "mean_m": mean.tolist(),
            "std_population_m": np.std(a, axis=0, ddof=0).tolist(),
            "centered_rms_3d_m": float(np.sqrt(np.mean(distance**2))),
            "centered_rms_horizontal_m": float(np.sqrt(np.mean(np.sum(residual[:, :2]**2, axis=1)))),
            "radius_p95_m": float(np.percentile(distance, 95)),
            "maximum_radius_m": float(distance.max())}


def missing_intervals(times, available, interval_end, step):
    """Return exact missing cells [sample time, next sample time), including boundaries."""
    t, good = np.asarray(times), np.asarray(available, bool)
    intervals = []
    start = None
    for i, ok in enumerate(good):
        if not ok and start is None:
            start = t[i]
        if start is not None and (ok or i == len(good)-1):
            end = t[i] if ok else min(interval_end, t[i]+step)
            intervals.append({"start_s": float(start), "end_s": float(end),
                              "duration_s": float(end-start)})
            start = None
    return intervals


def trajectory_metrics(times, xyz, max_gap_s=1.5):
    """No length across outages; endpoint displacement does not imply traveled distance."""
    t, a = np.asarray(times), np.asarray(xyz)
    good = np.isfinite(a).all(axis=1)
    valid = np.flatnonzero(good)
    if not len(valid):
        return {"count": 0}
    pairs = good[1:] & good[:-1] & (np.diff(t) <= max_gap_s)
    distances = np.linalg.norm(np.diff(a, axis=0), axis=1)
    return {"count": int(good.sum()), "connected_step_count": int(pairs.sum()),
            "observed_polyline_length_m": float(np.sum(distances[pairs])),
            "first_to_last_displacement_m": float(np.linalg.norm(a[valid[-1]]-a[valid[0]])),
            "first_time_s": float(t[valid[0]]), "last_time_s": float(t[valid[-1]]),
            "maximum_connected_step_m": float(np.max(distances[pairs])) if pairs.any() else None}
