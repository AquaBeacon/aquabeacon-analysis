"""Child-only visual SLAM evaluation without metric-position ground truth."""
from __future__ import annotations

from itertools import pairwise

import numpy as np
from scipy.spatial.transform import Rotation, Slerp


def frame_interval(start_s, end_s, fps, frame_count):
    """Select native frames in a half-open source-video interval."""
    if not 0 <= start_s < end_s or fps <= 0:
        raise ValueError('Invalid frame interval')
    return np.arange(max(0, int(np.ceil(start_s * fps - 1e-6))),
                     min(frame_count, int(np.ceil(end_s * fps - 1e-6))))


def sample_rotations(times, quaternions_xyzw, query, max_gap_s=.1):
    """SLERP valid brackets only; NaNs and long gaps remain missing."""
    times, q, query = map(np.asarray, (times, quaternions_xyzw, query))
    out = np.full((len(query), 4), np.nan)
    valid = np.isfinite(times) & np.isfinite(q).all(axis=1)
    valid &= np.linalg.norm(q, axis=1) > .5
    times, q = times[valid], q[valid]
    times, indices = np.unique(times, return_index=True)
    q = q[indices]
    if len(times) < 2:
        return out
    idx = np.clip(np.searchsorted(times, query), 1, len(times)-1)
    mask = ((query >= times[0]) & (query <= times[-1]) &
            (times[idx] - times[idx-1] <= max_gap_s))
    if mask.any():
        out[mask] = Slerp(times, Rotation.from_quat(q))(query[mask]).as_quat()
    return out


def rotation_angle_difference(q_visual, q_reference, frame_ok, lag):
    """Difference of relative-rotation MAGNITUDES, not full SO(3) error.

    This scalar diagnostic is invariant to a fixed unknown camera/body rotation
    and world-frame gauge. Pairs crossing any tracking or reference gap are invalid.
    """
    q_visual, q_reference = map(np.asarray, (q_visual, q_reference))
    n = len(q_visual)
    if lag < 1 or lag >= n:
        return np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    good = (np.asarray(frame_ok, dtype=bool) & np.isfinite(q_visual).all(axis=1)
            & np.isfinite(q_reference).all(axis=1))
    bad_prefix = np.r_[0, np.cumsum(~good)]
    starts = np.arange(n-lag)
    starts = starts[(bad_prefix[starts+lag+1] - bad_prefix[starts]) == 0]
    va, ra = np.full(n, np.nan), np.full(n, np.nan)
    if len(starts):
        ends = starts + lag
        for q, output in ((q_visual, va), (q_reference, ra)):
            delta = Rotation.from_quat(q[starts]).inv() * Rotation.from_quat(q[ends])
            output[ends] = np.rad2deg(delta.magnitude())
    return va, ra, va - ra


def statistics(values):
    values = np.asarray(values)
    values = values[np.isfinite(values)]
    return {'samples': len(values),
            'rmse_deg': float(np.sqrt(np.mean(values**2))) if len(values) else None,
            'median_abs_deg': float(np.median(np.abs(values))) if len(values) else None,
            'p95_abs_deg': float(np.percentile(np.abs(values), 95)) if len(values) else None}


def spans(values):
    """Run-length encode states, retaining every input frame."""
    values = np.asarray(values)
    if not len(values):
        return []
    breaks = np.r_[0, np.flatnonzero(values[1:] != values[:-1])+1, len(values)]
    return [{'start_frame': int(a), 'end_frame_exclusive': int(b), 'state': int(values[a])}
            for a, b in pairwise(breaks)]
