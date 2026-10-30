"""Separate magnetometer alignment and gravity/camera tilt in a down-positive frame."""
import numpy as np
from scipy.spatial.transform import Rotation, Slerp


def sample_rotations(times, wxyz, targets, max_gap_s=.1):
    """SLERP only finite unit-like samples inside short brackets; no extrapolation."""
    times, q, targets = map(np.asarray, (times, wxyz, targets))
    good = (np.isfinite(times) & np.isfinite(q).all(axis=1)
            & (np.abs(np.linalg.norm(q, axis=1) - 1) < .05))
    t, unique = np.unique(times[good], return_index=True)
    q = q[good][unique]
    out = np.full((len(targets), 4), np.nan)
    if len(t) < 2:
        return out
    hi = np.clip(np.searchsorted(t, targets, side='right'), 1, len(t)-1)
    ok = (np.isfinite(targets) & (targets >= t[0]) & (targets <= t[-1])
          & (t[hi]-t[hi-1] <= max_gap_s))
    if ok.any():
        out[ok] = Slerp(t, Rotation.from_quat(q[:, [1, 2, 3, 0]]))(
            targets[ok]).as_quat()[:, [3, 0, 1, 2]]
    return out


def gravity_tilt(specific_force_body):
    """Instantaneous accelerometer tilt; dynamic acceleration is not removed."""
    a = np.asarray(specific_force_body)
    good = np.isfinite(a).all(axis=1) & (np.linalg.norm(a, axis=1) > 1e-6)
    out = np.full((len(a), 4), np.nan)
    roll = np.arctan2(-a[good, 1], -a[good, 2])
    pitch = np.arctan2(a[good, 0], np.hypot(a[good, 1], a[good, 2]))
    if good.any():
        out[good] = Rotation.from_euler('xyz', np.column_stack(
            [roll, pitch, np.zeros(len(roll))])).as_quat()[:, [3, 0, 1, 2]]
    return out


def absolute_from_separate_mag(body_to_world_down_wxyz, magnetic_sensor_uT,
                               body_from_mag):
    """Rotate world horizontal axes to magnetic NED without altering tilt.

    The input world must have +z down. Separate sensor axes are transformed first.
    No accuracy gate, field calibration fit, or declination correction is applied.
    """
    matrix = np.asarray(body_from_mag)
    if (matrix.shape != (3, 3) or not np.isfinite(matrix).all()
            or not np.allclose(matrix.T @ matrix, np.eye(3), atol=1e-8)
            or not np.isclose(np.linalg.det(matrix), 1)):
        raise ValueError('Magnetic mounting must be a proper rotation')
    q, mag = map(np.asarray, (body_to_world_down_wxyz, magnetic_sensor_uT))
    out = np.full_like(q, np.nan, dtype=float)
    heading = np.full(len(q), np.nan)
    rp = np.full((len(q), 2), np.nan)
    good = (np.isfinite(q).all(axis=1) & (np.abs(np.linalg.norm(q, axis=1)-1) < .05)
            & np.isfinite(mag).all(axis=1))
    ids = np.flatnonzero(good)
    if not len(ids):
        return out, heading, rp
    rotation = Rotation.from_quat(q[ids][:, [1, 2, 3, 0]])
    field = rotation.apply(mag[ids] @ matrix.T)
    forward = rotation.as_matrix()[:, :, 0]
    observable = ((np.linalg.norm(field[:, :2], axis=1) > 1)
                  & (np.linalg.norm(forward[:, :2], axis=1) > .1))
    north_angle = np.arctan2(field[:, 1], field[:, 0])
    fixed = Rotation.from_euler('z', -north_angle[:, None]) * rotation
    ids = ids[observable]
    out[ids] = fixed[observable].as_quat()[:, [3, 0, 1, 2]]
    r = fixed[observable].as_matrix()
    heading[ids] = np.degrees(np.arctan2(r[:, 1, 0], r[:, 0, 0])) % 360
    rp[ids] = np.column_stack([np.arctan2(r[:, 2, 1], r[:, 2, 2]),
                              np.arcsin(np.clip(-r[:, 2, 0], -1, 1))])
    return out, heading, rp
