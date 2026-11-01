import numpy as np
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.nanyo_position import (
    ENU_FROM_NED,
    ray_sphere_position,
    true_enu_from_magnetic_enu,
)


def test_declination_west_rotates_magnetic_north_west_in_true_enu():
    r = true_enu_from_magnetic_enu(-8)
    assert np.allclose(r @ np.array([0, 1, 0]), [-np.sin(np.radians(8)), np.cos(np.radians(8)), 0])
    assert np.allclose(
        r, ENU_FROM_NED @ Rotation.from_euler("z", -8, degrees=True).as_matrix() @ ENU_FROM_NED.T
    )


def test_full_lever_geometry_recovers_known_child_origin():
    rp = Rotation.from_euler("xyz", [0.3, -0.2, 0.7]).as_matrix()
    rc = Rotation.from_euler("xyz", [-0.6, 0.4, -1]).as_matrix()
    optical = Rotation.from_euler("xyz", [0.8, 0.1, 0.4]).as_matrix()
    pg = np.array([10.0, 20.0, 5.0])
    truth = np.array([11.0, 22.0, -4.0])
    pa = np.array([0.1, -0.2, -0.1])
    pc = np.array([-0.1, 0.1, 0.3])
    pt = np.array([0.1, 0.1, 0.4])
    cl = np.array([0.1, 0.2, -0.1])
    ct = np.array([-0.1, -0.2, -0.3])
    cc = np.array([0.2, -0.1, -0.2])
    global_pc = pg + rp @ (pc - pa)
    ray = optical.T @ rp.T @ (truth + rc @ cl - global_pc)
    ray /= np.linalg.norm(ray)
    distance = np.linalg.norm(truth + rc @ ct - (pg + rp @ (pt - pa)))
    result, cam, length, residual = ray_sphere_position(
        pg, rp, rc, ray, optical, distance, pc, pt, pa, cl, ct, cc
    )
    assert np.allclose(result, truth)
    assert np.allclose(cam, truth + rc @ cc)
    assert length > 0 and abs(residual) < 1e-12


def test_impossible_ray_sphere_stays_missing():
    z = np.zeros(3)
    eye = np.eye(3)
    result, *_ = ray_sphere_position(z, eye, eye, [0, 0, 1], eye, 1, z, [3, 0, 0], z, z, z, z)
    assert np.isnan(result).all()
