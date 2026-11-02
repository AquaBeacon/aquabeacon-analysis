"""GNSS timing and covariance checks for matched stationary position comparisons."""

import numpy as np


def sample_gnss(times, positions, valid, query, max_gap_s=1.5, nearest_limit_s=0.51):
    """Sample valid native epochs; retain brackets and never bridge missing epochs.

    Linear interpolation is only a clock alignment, not a new GNSS observation.
    Exact epochs do not depend on the validity of their neighbours.
    """
    times = np.asarray(times, float)
    positions = np.asarray(positions, float)
    valid = np.asarray(valid, bool)
    query = np.asarray(query, float)
    if (
        len(times) == 0
        or positions.shape != (len(times), 3)
        or valid.shape != times.shape
        or not np.isfinite(times).all()
        or not np.isfinite(query).all()
        or np.any(np.diff(times) <= 0)
        or max_gap_s <= 0
        or nearest_limit_s < 0
    ):
        raise ValueError("Expected ordered unique GNSS epochs and finite query times")
    valid = valid & np.isfinite(positions).all(axis=1)
    right = np.searchsorted(times, query, side="left")
    left = np.maximum(0, right - 1)
    right = np.minimum(right, len(times) - 1)
    exact = times[right] == query
    left[exact] = right[exact]
    span = times[right] - times[left]
    fraction = np.divide(query - times[left], span, out=np.zeros(len(query)), where=span > 0)
    inside = (query >= times[0]) & (query <= times[-1])
    available = inside & (span <= max_gap_s) & valid[left] & valid[right]
    linear = positions[left] * (1 - fraction[:, None]) + positions[right] * fraction[:, None]
    linear[~available] = np.nan
    nearest = np.where(abs(query - times[left]) <= abs(query - times[right]), left, right)
    nearest_available = inside & valid[nearest] & (abs(query - times[nearest]) <= nearest_limit_s)
    nearest_xyz = positions[nearest].copy()
    nearest_xyz[~nearest_available] = np.nan
    return {
        "linear": linear,
        "available": available,
        "left_index": left,
        "right_index": right,
        "fraction": fraction,
        "nearest": nearest_xyz,
        "nearest_index": nearest,
        "nearest_available": nearest_available,
    }


def variance_budget(gnss, sensor_offset):
    """Squared repeatability of a sum, retaining its signed covariance term."""
    gnss, sensor_offset = np.asarray(gnss, float), np.asarray(sensor_offset, float)
    if (
        gnss.shape != sensor_offset.shape
        or gnss.ndim != 2
        or gnss.shape[1] != 3
        or not len(gnss)
        or not np.isfinite(gnss).all()
        or not np.isfinite(sensor_offset).all()
    ):
        raise ValueError("Expected matched finite N by 3 arrays")
    dg, ds = gnss - gnss.mean(0), sensor_offset - sensor_offset.mean(0)
    vg = np.mean(dg**2, axis=0)
    vs = np.mean(ds**2, axis=0)
    cross = 2 * np.mean(dg * ds, axis=0)
    total = np.mean((dg + ds) ** 2, axis=0)
    return {
        "gnss_axis_variance_m2": vg.tolist(),
        "sensor_offset_axis_variance_m2": vs.tolist(),
        "twice_axis_covariance_m2": cross.tolist(),
        "absolute_axis_variance_m2": total.tolist(),
        "gnss_variance_3d_m2": float(vg.sum()),
        "sensor_offset_variance_3d_m2": float(vs.sum()),
        "twice_covariance_3d_m2": float(cross.sum()),
        "absolute_variance_3d_m2": float(total.sum()),
        "closure_max_abs_m2": float(np.max(abs(total - vg - vs - cross))),
    }
