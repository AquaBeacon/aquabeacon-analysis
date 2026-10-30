"""Tilt-compensated magnetic heading candidates and independently checked bias fits."""
import numpy as np
from scipy.spatial.transform import Rotation


def magnetic_candidates(sensor_q_wxyz, magnetic_uT, body_from_sensor):
    """Clockwise heading from local magnetic north; no declination or validity claim."""
    q = np.asarray(sensor_q_wxyz)
    rotation = Rotation.from_quat(q[:, [1, 2, 3, 0]])
    world_magnetic = rotation.apply(magnetic_uT)
    forward = rotation.apply(np.broadcast_to(np.asarray(body_from_sensor)[0],
                                              (len(q), 3)))
    heading = np.degrees(np.arctan2(world_magnetic[:, 1], world_magnetic[:, 0])
                         - np.arctan2(forward[:, 1], forward[:, 0])) % 360
    observable = ((np.linalg.norm(forward[:, :2], axis=1) > .1)
                  & (np.linalg.norm(world_magnetic[:, :2], axis=1) > 1))
    heading[~observable] = np.nan
    return heading, world_magnetic, observable


def fit_constant_bias(sensor_q_wxyz, magnetic_uT):
    """Fit m_sensor = bias + R_sensor_world * fixed_field; caller must hold out poses."""
    q = np.asarray(sensor_q_wxyz)
    rotations = Rotation.from_quat(q[:, [1, 2, 3, 0]]).as_matrix()
    design = np.concatenate([rotations, np.broadcast_to(np.eye(3), rotations.shape)],
                            axis=2).reshape(-1, 6)
    target = np.einsum("nij,nj->ni", rotations, magnetic_uT).reshape(-1)
    value, _, rank, singular = np.linalg.lstsq(design, target, rcond=None)
    if rank < 6:
        raise ValueError("Insufficient orientations for constant bias")
    return value[:3], value[3:], float(singular[0] / singular[-1])
