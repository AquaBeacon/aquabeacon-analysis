"""Gap-bounded inertial integration and displacement comparisons, without fitting."""

from __future__ import annotations

import numpy as np


def _times(values):
    times = np.asarray(values, dtype=float)
    if times.ndim != 1 or not np.all(np.isfinite(times)) or np.any(np.diff(times) <= 0):
        raise ValueError("times must be a finite, strictly increasing one-dimensional array")
    return times


def _positive(value, name):
    if not np.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


def integrate_segments(times_s, acceleration_enu, max_gap_s=.100001):
    """Integrate gravity-free ENU acceleration; each valid segment starts at rest.

    Nonfinite acceleration rows remain NaN, with segment -1. A following valid
    row starts a new segment, as does a time gap exceeding ``max_gap_s``.
    The mean endpoint acceleration is held constant over each integration step.
    Input timestamps are validated globally and are never sorted or repaired.
    """
    times = _times(times_s)
    acceleration = np.asarray(acceleration_enu, dtype=float)
    if acceleration.shape != (len(times), 3):
        raise ValueError("acceleration_enu must have shape (N, 3)")
    _positive(max_gap_s, "max_gap_s")
    position = np.full(acceleration.shape, np.nan)
    velocity = np.full(acceleration.shape, np.nan)
    segment = np.full(len(times), -1, dtype=int)
    valid = np.all(np.isfinite(acceleration), axis=1)
    segment_id = -1
    for i in range(len(times)):
        if not valid[i]:
            continue
        if i == 0 or not valid[i - 1] or times[i] - times[i - 1] > max_gap_s:
            segment_id += 1
            position[i] = velocity[i] = 0
        else:
            dt = times[i] - times[i - 1]
            mean_a = .5 * (acceleration[i - 1] + acceleration[i])
            position[i] = position[i - 1] + velocity[i - 1] * dt + .5 * mean_a * dt**2
            velocity[i] = velocity[i - 1] + mean_a * dt
        segment[i] = segment_id
    return {"position_m": position, "velocity_m_s": velocity, "segment": segment}


def bounded_sample(times, values, query, max_gap_s):
    """Linearly sample finite adjacent observations within a bounded time gap.

    Exact observed times are preserved even beside a gap or invalid neighbor.
    For vector values, every endpoint component must be finite. Missing samples,
    nonfinite queries and extrapolation produce NaN. Output shape is the query
    shape followed by the trailing value dimension (if present).
    """
    times = _times(times)
    values = np.asarray(values, dtype=float)
    if values.ndim not in (1, 2) or values.shape[0] != len(times):
        raise ValueError("values must have shape (N,) or (N, D)")
    _positive(max_gap_s, "max_gap_s")
    query = np.asarray(query, dtype=float)
    flat_query = query.ravel()
    output = np.full((flat_query.size,) + values.shape[1:], np.nan)
    if not len(times):
        return output.reshape(query.shape + values.shape[1:])
    right = np.searchsorted(times, flat_query)
    finite_rows = np.isfinite(values)
    if values.ndim == 2:
        finite_rows = np.all(finite_rows, axis=1)
    for i, (q, r) in enumerate(zip(flat_query, right, strict=True)):
        if not np.isfinite(q):
            continue
        if r < len(times) and times[r] == q:
            if finite_rows[r]:
                output[i] = values[r]
        elif 0 < r < len(times):
            left = r - 1
            gap = times[r] - times[left]
            if gap <= max_gap_s and finite_rows[left] and finite_rows[r]:
                fraction = (q - times[left]) / gap
                output[i] = values[left] + fraction * (values[r] - values[left])
    return output.reshape(query.shape + values.shape[1:])


def window_comparisons(
    times, dr_position, dr_velocity, segment, depth_m,
    current_position_enu, current_valid, horizons_s=(5, 10, 30, 60), stride_s=5,
):
    """Compare zero-initial-velocity DR+depth and observed current displacement.

    All arrays share one clock. Starts require valid current position, DR and
    depth and are spaced by ``stride_s`` from the preceding attempted start.
    Endpoints use the nearest existing grid timestamp within 0.026 s; current
    positions are never interpolated. A window must stay within one nonnegative
    DR segment. Interior current/depth gaps are allowed because only their
    observed endpoints define displacement. ``vertical_gap_m`` is signed DR up
    minus current up; the horizontal and three-dimensional gaps are norms.

    Returns rows, attempt counts and rejection-reason counts per numeric horizon.
    Every attempt produces either one row or exactly one rejection.
    """
    times = _times(times)
    n = len(times)
    dr_position = np.asarray(dr_position, dtype=float)
    dr_velocity = np.asarray(dr_velocity, dtype=float)
    current_position = np.asarray(current_position_enu, dtype=float)
    for array in (dr_position, dr_velocity, current_position):
        if array.shape != (n, 3):
            raise ValueError("position and velocity arrays must have shape (N, 3)")
    segment = np.asarray(segment)
    depth = np.asarray(depth_m, dtype=float)
    current_valid = np.asarray(current_valid, dtype=bool)
    if any(array.shape != (n,) for array in (segment, depth, current_valid)):
        raise ValueError("segment, depth_m and current_valid must have shape (N,)")
    _positive(stride_s, "stride_s")
    horizons = tuple(float(h) for h in horizons_s)
    for horizon in horizons:
        _positive(horizon, "horizon")
    if len(set(horizons)) != len(horizons):
        raise ValueError("horizons_s must contain unique horizons")
    dr_valid = np.all(np.isfinite(dr_position), axis=1)
    dr_valid &= np.all(np.isfinite(dr_velocity), axis=1) & (segment >= 0)
    current_valid = current_valid & np.all(np.isfinite(current_position), axis=1)
    starts = np.flatnonzero(dr_valid & current_valid & np.isfinite(depth))
    chosen_starts = []
    for start in starts:
        if not chosen_starts or times[start] - times[chosen_starts[-1]] >= stride_s - 1e-9:
            chosen_starts.append(start)
    rows = []
    attempts = dict.fromkeys(horizons, 0)
    rejections = {h: {} for h in horizons}
    for horizon in horizons:
        for start in chosen_starts:
            attempts[horizon] += 1
            target = times[start] + horizon
            right = int(np.searchsorted(times, target))
            candidates = [i for i in (right - 1, right) if start < i < n]
            end = min(candidates, key=lambda i: abs(times[i] - target)) if candidates else None
            reason = None
            if end is None or abs(times[end] - target) > .026:
                reason = "endpoint_unavailable"
            elif not current_valid[end]:
                reason = "current_endpoint_invalid"
            elif not np.isfinite(depth[end]):
                reason = "depth_endpoint_invalid"
            elif not np.all(dr_valid[start:end + 1]) or not np.all(
                segment[start:end + 1] == segment[start]
            ):
                reason = "dr_segment_discontinuity"
            if reason is not None:
                rejections[horizon][reason] = rejections[horizon].get(reason, 0) + 1
                continue
            duration = times[end] - times[start]
            dr_delta = dr_position[end] - dr_position[start] - dr_velocity[start] * duration
            dr_delta[2] = depth[start] - depth[end]
            current_delta = current_position[end] - current_position[start]
            gap = dr_delta - current_delta
            row = {
                "start_s": float(times[start]), "end_s": float(times[end]),
                "horizon_s": horizon, "segment": int(segment[start]),
                "horizontal_gap_m": float(np.linalg.norm(gap[:2])),
                "vertical_gap_m": float(gap[2]),
                "position_gap_m": float(np.linalg.norm(gap)),
                "dr_horizontal_displacement_m": float(np.linalg.norm(dr_delta[:2])),
                "current_horizontal_displacement_m": float(np.linalg.norm(current_delta[:2])),
            }
            for axis, dr_value, current_value in zip(
                ("east", "north", "up"), dr_delta, current_delta, strict=True
            ):
                row[f"dr_{axis}_m"] = float(dr_value)
                row[f"current_{axis}_m"] = float(current_value)
            rows.append(row)
    return {"rows": rows, "attempts": attempts, "rejections": rejections}
