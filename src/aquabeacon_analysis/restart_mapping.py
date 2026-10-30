"""Time-window mapping and explicit, diagnosable position registration."""
from __future__ import annotations

import numpy as np


def prior_anchor_indices(times, available, period_s=.2):
    """At most one finite prior per source-rate time cell, nearest its center.

    Densifying video must not multiply the information weight of interpolated fixes.
    No missing values are made available by this selection.
    """
    t, valid = np.asarray(times, float), np.asarray(available, bool)
    if t.shape != valid.shape or period_s <= 0:
        raise ValueError('Matching arrays and positive period required')
    selected = {}
    for index in np.flatnonzero(valid & np.isfinite(t)):
        cell = int(np.floor(t[index] / period_s + .5))
        distance = abs(t[index] - cell * period_s)
        if cell not in selected or distance < selected[cell][0]:
            selected[cell] = (distance, int(index))
    return np.array(sorted(v[1] for v in selected.values()), dtype=int)


def temporal_windows(times, width_s=20., stride_s=15.):
    """Cover every input frame, including the tail, with independently restartable windows."""
    t = np.asarray(times, float)
    if not len(t) or not np.isfinite(t).all() or np.any(np.diff(t) <= 0):
        raise ValueError('Finite strictly increasing frame times required')
    if not 0 < stride_s <= width_s:
        raise ValueError('Require 0 < stride <= width')
    windows = []
    for start in np.arange(t[0], t[-1] + 1e-9, stride_s):
        ix = np.flatnonzero((t >= start - 1e-9) & (t < start + width_s))
        if len(ix):
            windows.append(ix)
        if start + width_s > t[-1]:
            break
    return windows


def position_registration(source, target, min_ratio=1e-3, huber_delta_m=1.):
    """Similarity fit for a local model; reject collinear orientation/scale ambiguity.

    Fit residuals are internal consistency, never independent accuracy. No points
    are silently discarded; the caller declares all finite position correspondences.
    """
    a, b = np.asarray(source, float), np.asarray(target, float)
    if a.shape != b.shape or a.ndim != 2 or a.shape[1] != 3:
        raise ValueError('Matching Nx3 arrays required')
    result = {'correspondences': len(a), 'accepted': False}
    if len(a) < 6 or not np.isfinite(a).all() or not np.isfinite(b).all():
        return {**result, 'reason': 'fewer than six finite position correspondences'}
    ac, bc = a - a.mean(axis=0), b - b.mean(axis=0)
    sa, sb = np.linalg.svd(ac, compute_uv=False), np.linalg.svd(bc, compute_uv=False)
    ra, rb = sa[1] / max(sa[0], 1e-15), sb[1] / max(sb[0], 1e-15)
    result.update(source_second_first_ratio=float(ra), target_second_first_ratio=float(rb))
    if min(ra, rb) < min_ratio or min(sa[0], sb[0]) < 1e-6:
        return {**result, 'reason': 'position geometry insufficient for unique orientation/scale'}
    if huber_delta_m <= 0:
        raise ValueError('Positive Huber length required')
    weights = np.ones(len(a))
    converged = False
    for iteration in range(50):
        weights_used = weights.copy()
        w = weights / weights.sum()
        ma, mb = np.sum(a * w[:, None], axis=0), np.sum(b * w[:, None], axis=0)
        ac, bc = a - ma, b - mb
        u, singular, vt = np.linalg.svd((bc * w[:, None]).T @ ac)
        correction = np.eye(3)
        correction[2, 2] = np.linalg.det(u @ vt)
        rotation = u @ correction @ vt
        scale = float(np.sum(singular * np.diag(correction)) / np.sum(w * np.sum(ac**2, axis=1)))
        translation = mb - scale * rotation @ ma
        fitted = scale * a @ rotation.T + translation
        residual = np.linalg.norm(fitted - b, axis=1)
        new_weights = np.minimum(1., huber_delta_m / np.maximum(residual, 1e-12))
        if np.max(np.abs(new_weights - weights)) < 1e-7:
            converged = True
            break
        weights = new_weights
    sa = np.linalg.svd(ac * np.sqrt(w[:, None]), compute_uv=False)
    sb = np.linalg.svd(bc * np.sqrt(w[:, None]), compute_uv=False)
    weighted_ratios = [float(s[1] / max(s[0], 1e-15)) for s in [sa, sb]]
    result.update(fit_method='Huber IRLS similarity', huber_delta_m=huber_delta_m,
        weights=weights_used.tolist(), iterations=iteration+1, converged=converged,
        weighted_second_first_ratios=weighted_ratios)
    if min(weighted_ratios) < min_ratio:
        return {**result, 'reason': 'weighted position geometry insufficient'}
    return {**result, 'accepted': True, 'scale': scale, 'rotation': rotation.tolist(),
            'translation': translation.tolist(), 'fit_rms_m': float(np.sqrt(np.mean(residual**2))),
            'fit_median_m': float(np.median(residual)), 'fit_max_m': float(residual.max()),
            'reason': 'conditional fit to supplied positions; not surveyed ground truth'}
