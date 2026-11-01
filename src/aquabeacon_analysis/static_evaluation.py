"""Repeatability about a fitted constant position, not ground-truth accuracy."""

import numpy as np


def static_metrics(xyz):
    xyz = np.asarray(xyz, dtype=float)
    if xyz.ndim != 2 or xyz.shape[1] != 3 or not len(xyz):
        raise ValueError("Expected nonempty N by 3 positions")
    if not np.isfinite(xyz).all():
        raise ValueError("Missing positions must be explicitly selected by the caller")
    mean = xyz.mean(axis=0)
    residual = xyz - mean
    axis = np.sqrt(np.mean(residual**2, axis=0))
    radius = np.linalg.norm(residual, axis=1)
    return {
        "n": len(xyz),
        "mean_m": mean.tolist(),
        "axis_rmse_m": axis.tolist(),
        "horizontal_rmse_m": float(np.linalg.norm(axis[:2])),
        "rmse_3d_m": float(np.linalg.norm(axis)),
        "radius_p95_m": float(np.percentile(radius, 95)),
        "radius_max_m": float(radius.max()),
    }
