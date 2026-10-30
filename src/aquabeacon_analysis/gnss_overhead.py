"""Plan-view GNSS comparisons; references are observations, never surveyed truth."""
from __future__ import annotations

import numpy as np


def horizontal_metrics(xy):
    """Population RMS and mean-centred scatter, with no implicit outlier removal."""
    xy = np.asarray(xy, dtype=float).reshape(-1, 2)
    if not np.isfinite(xy).all():
        raise ValueError('Pass explicitly selected finite observations')
    if not len(xy):
        return {'count': 0, 'mean_east_north_m': None, 'horizontal_rms_m': None,
                'horizontal_scatter_m': None}
    mean = xy.mean(axis=0)
    return {'count': len(xy), 'mean_east_north_m': mean.tolist(),
            'horizontal_rms_m': float(np.sqrt(np.mean(np.sum(xy**2, axis=1)))),
            'horizontal_scatter_m': float(np.sqrt(np.mean(np.sum((xy-mean)**2, axis=1))))}


def nearest_pairs(left_times, right_times, left_valid, right_valid, max_gap_s=.02):
    """Nearest one-to-one pairs within tolerance, never bridge/interpolate a gap.

    Invalid samples remain in the time search, so matching cannot jump over an
    invalid nearest epoch to select a farther valid solution. Ties choose earlier.
    """
    left = np.asarray(left_times, float)
    right = np.asarray(right_times, float)
    if max_gap_s < 0 or not np.isfinite(left).all() or not np.isfinite(right).all():
        raise ValueError('Finite times and nonnegative tolerance required')
    if np.any(np.diff(left) <= 0) or np.any(np.diff(right) <= 0):
        raise ValueError('Times must be strictly increasing')
    used, pairs = set(), []
    for i, time in enumerate(left):
        if not left_valid[i] or not len(right):
            continue
        at = np.searchsorted(right, time)
        candidates = [j for j in [at-1, at] if 0 <= j < len(right)]
        j = min(candidates, key=lambda j: (abs(right[j]-time), j))
        if right_valid[j] and j not in used and abs(right[j]-time) <= max_gap_s:
            pairs.append((i, j))
            used.add(j)
    return np.asarray(pairs, dtype=int).reshape(-1, 2)
