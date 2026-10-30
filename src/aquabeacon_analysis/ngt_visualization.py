"""Presentation coordinates with explicit missing samples and reversible origins."""
from __future__ import annotations

import numpy as np


def contiguous_segments(points, times, max_gap_s=.3):
    """Return original indices; never draw a line through missing observations."""
    xyz, t = np.asarray(points, float), np.asarray(times, float)
    good = np.isfinite(xyz).all(axis=1) & np.isfinite(t)
    segments, current = [], []
    for i, ok in enumerate(good):
        if current and (not ok or t[i]-t[current[-1]] > max_gap_s or t[i] <= t[current[-1]]):
            segments.append(current)
            current = []
        if ok:
            current.append(i)
    if current:
        segments.append(current)
    return segments


def surface_display(parent_enu, child_enu, relative_down):
    """Keep GNSS horizontal coordinates, put parent at z=0 and child at -relative down.

    This is a presentation frame, not a correction to GNSS height or raw positions.
    A single translation centers x/y on the first available parent position.
    """
    parent, child = np.asarray(parent_enu, float).copy(), np.asarray(child_enu, float).copy()
    first = np.flatnonzero(np.isfinite(parent).all(axis=1))[0]
    origin = parent[first].copy()
    parent[:, :2] -= origin[:2]
    child[:, :2] -= origin[:2]
    valid_parent = np.isfinite(parent).all(axis=1)
    valid_child = np.isfinite(child).all(axis=1) & np.isfinite(relative_down)
    parent[:, 2] = 0
    child[:, 2] = -np.asarray(relative_down)
    parent[~valid_parent] = np.nan
    child[~valid_child] = np.nan
    return parent, child, origin
