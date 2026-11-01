"""Explicit frame and lever-arm geometry for provisional Nanyo localization."""

import numpy as np
from scipy.spatial.transform import Rotation

ENU_FROM_NED = np.array([[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, -1.0]])


def true_enu_from_magnetic_enu(declination_deg):
    """East-positive declination: magnetic north is east of true north."""
    return Rotation.from_euler("z", -declination_deg, degrees=True).as_matrix()


def ray_sphere_position(
    antenna,
    parent_rotation,
    child_rotation,
    ray_optical,
    body_from_optical,
    distance,
    parent_camera,
    parent_acoustic,
    parent_antenna,
    child_light,
    child_acoustic,
    child_camera,
):
    """Intersect beacon ray with acoustic sphere with all nominal lever arms.

    Rotations map body FRD to true ENU. Vectors are column coordinates, all meters.
    Return child body-origin, child optical-center estimate, optical path length,
    and acoustic residual. Negative/imaginary intersections remain NaN.
    """
    pg = np.asarray(antenna)
    rp, rc = np.asarray(parent_rotation), np.asarray(child_rotation)
    camera = pg + rp @ (np.asarray(parent_camera) - parent_antenna)
    transmitter = pg + rp @ (np.asarray(parent_acoustic) - parent_antenna)
    direction = rp @ np.asarray(body_from_optical) @ ray_optical
    direction /= np.linalg.norm(direction)
    b = camera + rc @ (np.asarray(child_acoustic) - child_light) - transmitter
    dot = b @ direction
    discriminant = dot * dot + distance * distance - b @ b
    if not np.isfinite(discriminant) or distance <= 0 or discriminant < 0:
        return np.full(3, np.nan), np.full(3, np.nan), np.nan, np.nan
    length = -dot + np.sqrt(discriminant)
    if length <= 0:
        return np.full(3, np.nan), np.full(3, np.nan), np.nan, np.nan
    origin = camera + length * direction - rc @ child_light
    child_cam = origin + rc @ child_camera
    residual = np.linalg.norm(origin + rc @ child_acoustic - transmitter) - distance
    return origin, child_cam, length, residual


def robust_rotation_mean(matrices, cutoff_deg=5):
    """Recordable trimmed rotation mean; rejects only fit outliers, not records."""
    rotations = Rotation.from_matrix(matrices)
    mean = rotations.mean()
    residual = np.degrees((mean.inv() * rotations).magnitude())
    keep = residual < cutoff_deg
    if keep.any():
        mean = rotations[keep].mean()
    return mean.as_matrix(), np.degrees((mean.inv() * rotations).magnitude()), keep
