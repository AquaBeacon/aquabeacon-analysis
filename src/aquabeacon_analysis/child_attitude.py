"""Reusable FRD attitude in a gravity-aligned local frame, with explicit yaw limits."""
import numpy as np
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.child_axis_calibration import body_quaternions
from aquabeacon_analysis.magnetic_heading import magnetic_candidates


def magnetic_ned_quaternions(roll_pitch_rad, magnetic_heading_deg):
    """FRD to magnetic NED, wxyz; heading clockwise from magnetic north.

    This uses uncorrected magnetic observations, not a validated true-north frame.
    Missing angles remain missing; no interpolation or temporal filtering.
    """
    rp = np.asarray(roll_pitch_rad, dtype=float)
    heading = np.asarray(magnetic_heading_deg, dtype=float)
    angles = np.column_stack([rp, np.radians(heading)])
    valid = np.isfinite(angles).all(axis=1)
    output = np.full((len(angles), 4), np.nan)
    if valid.any():
        output[valid] = Rotation.from_euler('xyz', angles[valid]).as_quat()[:, [3, 0, 1, 2]]
    return output


def estimate_attitude(sensor_wxyz, magnetic_uT, body_from_sensor):
    """Return local-down quaternions, ZYX angles and unvalidated magnetic headings.

    local = diag(1,-1,-1) @ game_world. Local x is arbitrary, never north.
    Euler order is roll,pitch,yaw in radians; singular roll/yaw remain NaN.
    No magnetic correction is applied to the exported quaternion.
    """
    body = body_quaternions(sensor_wxyz, body_from_sensor)
    valid = np.isfinite(body).all(axis=1)
    local = np.full_like(body, np.nan)
    angles = np.full((len(body), 3), np.nan)
    heading = np.full(len(body), np.nan)
    observable = np.zeros(len(body), dtype=bool)
    if valid.any():
        rot = (Rotation.from_matrix(np.diag([1., -1., -1.]))
               * Rotation.from_quat(body[valid][:, [1, 2, 3, 0]]))
        local[valid] = rot.as_quat()[:, [3, 0, 1, 2]]
        r = rot.as_matrix()
        a = np.column_stack([np.arctan2(r[:, 2, 1], r[:, 2, 2]),
                             np.arcsin(np.clip(-r[:, 2, 0], -1, 1)),
                             np.arctan2(r[:, 1, 0], r[:, 0, 0])])
        a[np.hypot(r[:, 0, 0], r[:, 1, 0]) < 1e-6, 0::2] = np.nan
        angles[valid] = a
    mag = np.asarray(magnetic_uT, dtype=float)
    ok = valid & np.isfinite(mag).all(axis=1)
    if ok.any():
        heading[ok], _, observable[ok] = magnetic_candidates(
            np.asarray(sensor_wxyz)[ok], mag[ok], body_from_sensor)
    return local, angles, heading, observable
