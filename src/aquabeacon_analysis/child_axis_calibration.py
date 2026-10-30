"""Geometric child FRD axes; independent of magnetic heading and camera clocks."""
from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation


def fit_frd(poses, max_error_deg=5.0):
    """Fit level/nose-up/right-up/tail-up specific-force poses, without bias correction."""
    means, quality = [], []
    for acceleration, gyro, times in poses:
        a, g, t = map(np.asarray, (acceleration, gyro, times))
        if (len(a) < 20 or len(g) < 20 or len(t) < 20
                or not all(np.isfinite(x).all() for x in (a, g, t))
                or np.any(np.diff(t) <= 0) or np.max(np.diff(t)) > .025):
            raise ValueError("Missing, nonfinite or discontinuous pose samples")
        mean = a.mean(0)
        if (not 9.3 <= np.linalg.norm(mean) <= 10.3
                or np.linalg.norm(g.mean(0)) > .03
                or a.std(0).max() > .04 or g.std(0).max() > .003):
            raise ValueError("Pose fails static/gravity gate")
        means.append(mean)
        quality.append({"acceleration_samples": len(a), "gyro_samples": len(g),
                        "mean_acceleration_m_s2": mean.tolist(),
                        "acceleration_std_m_s2": a.std(0).tolist(),
                        "gyro_std_rad_s": g.std(0).tolist(),
                        "max_sample_gap_s": float(np.diff(t).max())})
    if len(means) != 4:
        raise ValueError("Exactly four labeled poses required")
    down = -means[0] / np.linalg.norm(means[0])
    forward = means[1] - np.dot(means[1], down) * down
    if np.linalg.norm(forward) < 1:
        raise ValueError("Unobservable forward direction")
    forward /= np.linalg.norm(forward)
    matrix = np.array([forward, np.cross(down, forward), down])
    expected = np.array([[0, 0, -1], [1, 0, 0], [0, 1, 0], [-1, 0, 0]])
    directions = np.array(means) / np.linalg.norm(means, axis=1)[:, None]
    errors = np.degrees(np.arccos(np.clip(np.sum((directions @ matrix.T) * expected,
                                              axis=1), -1, 1)))
    if np.max(errors) > max_error_deg:
        raise ValueError(f"Pose direction gate failed: {errors.tolist()}")
    for row, error in zip(quality, errors, strict=True):
        row["direction_residual_deg"] = float(error)
    return matrix, quality


def body_quaternions(sensor_to_world_wxyz, body_from_sensor):
    """R_world_body = R_world_sensor @ R_body_sensor.T; invalid samples remain NaN."""
    matrix = np.asarray(body_from_sensor, dtype=float)
    if (matrix.shape != (3, 3) or not np.isfinite(matrix).all()
            or not np.allclose(matrix @ matrix.T, np.eye(3), atol=1e-6)
            or not np.isclose(np.linalg.det(matrix), 1, atol=1e-6)):
        raise ValueError("Expected a proper sensor-to-body rotation")
    q = np.asarray(sensor_to_world_wxyz, dtype=float)
    norm = np.linalg.norm(q, axis=1)
    valid = np.isfinite(q).all(axis=1) & (np.abs(norm - 1) < .05)
    out = np.full_like(q, np.nan)
    if not valid.any():
        return out
    sensor = Rotation.from_quat((q[valid] / norm[valid, None])[:, [1, 2, 3, 0]])
    out[valid] = (sensor * Rotation.from_matrix(matrix).inv()).as_quat()[:, [3, 0, 1, 2]]
    return out
