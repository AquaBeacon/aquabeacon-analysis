"""Small, testable checks for paired optical evaluations and provisional map placement."""
from __future__ import annotations

import numpy as np
from scipy.optimize import least_squares


def longest_runs(mask):
    edges = np.diff(np.r_[False, np.asarray(mask, bool), False].astype(int))
    return sorted(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1), strict=True),
                  key=lambda pair: pair[1]-pair[0], reverse=True)


def bracket_values(times, values, query, max_gap=.15):
    """Finite exact hits or two-sided short brackets only; never extrapolate gaps."""
    times, values, query = np.asarray(times), np.asarray(values), np.asarray(query)
    output = np.full((len(query), values.shape[1]), np.nan)
    for i, t in enumerate(query):
        j = int(np.searchsorted(times, t))
        if j < len(times) and abs(times[j]-t) < 1e-7:
            if np.isfinite(values[j]).all():
                output[i] = values[j]
        elif (0 < j < len(times) and times[j]-times[j-1] <= max_gap
              and np.isfinite(values[j-1:j+1]).all()):
            weight = (t-times[j-1])/(times[j]-times[j-1])
            output[i] = values[j-1]*(1-weight)+values[j]*weight
    return output


def positive_position_fit(rotated, targets, min_anchors=6):
    """Diagnose unconstrained scale first; a negative scale is never forced positive."""
    good = np.isfinite(rotated).all(1) & np.isfinite(targets).all(1)
    x, y = np.asarray(rotated)[good], np.asarray(targets)[good]
    result = {"anchors": len(x), "accepted": False, "accuracy_validated": False}
    if len(x) < min_anchors:
        return {**result, "reason": "insufficient synchronized position anchors"}
    span = float(np.linalg.norm(np.ptp(y, axis=0)))
    result["navigation_span_m"] = span
    if span < .25 or np.linalg.norm(np.ptp(x, axis=0)) < 1e-7:
        return {**result, "reason": "insufficient motion to constrain scale"}
    fit = least_squares(lambda v: ((v[0]*x+v[1:]-y)/[1, 1, 3]).ravel(),
                        np.r_[1., np.median(y-x, axis=0)], loss="soft_l1")
    residual = np.linalg.norm(fit.x[0]*x+fit.x[1:]-y, axis=1)
    result.update(scale_m_per_unit=float(fit.x[0]), translation_enu_m=fit.x[1:].tolist(),
                  fit_median_m=float(np.median(residual)), fit_p95_m=float(np.percentile(residual, 95)))
    if fit.x[0] <= 0:
        return {**result, "reason": "non-positive scale; no reflection or forced positive fit"}
    if np.median(residual) > 5:
        return {**result, "reason": "median position disagreement exceeds 5 m"}
    return {**result, "accepted": True, "reason": "provisional position-supported metric fragment"}
