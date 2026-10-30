"""Auditable interval accounting and pixel-frame conversions for the pool report."""
from __future__ import annotations

import numpy as np


def state_intervals(times, states, end_s, max_gap_s=None):
    """Partition timestamp cells, without merging across a missing source-time gap."""
    t = np.asarray(times, float)
    values = list(states)
    if len(t) != len(values) or not len(t) or not np.isfinite(t).all():
        raise ValueError('Matching, nonempty finite samples required')
    if np.any(np.diff(t) <= 0) or end_s <= t[-1]:
        raise ValueError('Increasing samples and a later final boundary required')
    result, start = [], 0
    for i in range(1, len(t)+1):
        gap = i < len(t) and max_gap_s is not None and t[i]-t[i-1] > max_gap_s
        if i == len(t) or values[i] != values[start] or gap:
            stop = end_s if i == len(t) else t[i]
            if gap:
                stop = min(stop, t[i-1] + max_gap_s)
            result.append({'start_s': float(t[start]), 'end_s': float(stop),
                'duration_s': float(stop-t[start]), 'state': values[start], 'samples': i-start})
            start = i
    return result


def position_state(row):
    """Mutually exclusive flag combinations, not causal labels inferred from imagery."""
    if row['magnetic_position_available'] in (True, 'True'):
        return 'available'
    missing = [name for name, key in [('optical', 'optical_available'),
        ('acoustic', 'range_formula_available'), ('attitude', 'magnetic_attitude_available')]
        if row[key] not in (True, 'True')]
    return '+'.join(missing) or 'unexplained'


def sample_cell_vertices(times, values, end_s):
    """Repeat cell edges so shaded timelines retain isolated states and the last cell."""
    t = np.asarray(times, float)
    v = np.asarray(values, bool)
    if len(t) != len(v) or not len(t) or end_s <= t[-1] or np.any(np.diff(t) <= 0):
        raise ValueError('Increasing timestamps, matching states and a final boundary required')
    return np.repeat(np.r_[t, end_s], 2)[1:-1], np.repeat(v, 2)


def clockwise_display_to_sensor(pixels, sensor_height):
    """Undo a 90-degree clockwise display rotation: (xd,yd) -> (yd,H-1-xd)."""
    p = np.asarray(pixels, float)
    if p.shape[-1] != 2 or sensor_height <= 0:
        raise ValueError('2D pixels and positive sensor height required')
    return np.stack([p[..., 1], sensor_height-1-p[..., 0]], axis=-1)


def sensor_rays_to_clockwise_display(rays):
    """Keep rays in the same displayed optical basis for an angular sensitivity comparison."""
    p = np.asarray(rays, float)
    if p.shape[-1] != 3:
        raise ValueError('3D rays required')
    return np.stack([-p[..., 1], p[..., 0], p[..., 2]], axis=-1)
