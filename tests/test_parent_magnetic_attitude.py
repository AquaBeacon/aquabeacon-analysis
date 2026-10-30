import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.parent_magnetic_attitude import (
    absolute_from_separate_mag,
    gravity_tilt,
    sample_rotations,
)


def test_separate_mount_recovers_ned_and_preserves_tilt():
    true = Rotation.from_euler('xyz', [[.2, -.4, 0], [-.3, .5, np.pi/2], [0, 0, np.pi]])
    mount = Rotation.from_matrix(np.diag([1., -1., -1.]))
    mag = (true * mount).inv().apply([[30, 0, 40]]*3)
    world = Rotation.from_euler('z', 2.4) * true
    q, heading, _ = absolute_from_separate_mag(
        world.as_quat()[:, [3, 0, 1, 2]], mag, mount.as_matrix())
    np.testing.assert_allclose(Rotation.from_quat(q[:, [1, 2, 3, 0]]).as_matrix(),
                               true.as_matrix(), atol=1e-12)
    np.testing.assert_allclose((heading-[0, 90, 180]+180) % 360-180, 0, atol=1e-12)


def test_gravity_fallback_and_missing():
    true = Rotation.from_euler('xyz', [[.3, -.2, 1], [-.4, .6, 2]])
    acceleration = true.inv().apply([[0, 0, -9.81]]*2)
    q = gravity_tilt(acceleration)
    np.testing.assert_allclose(Rotation.from_quat(q[:, [1, 2, 3, 0]]).as_euler('xyz')[:, :2],
                               [[.3, -.2], [-.4, .6]], atol=1e-12)
    assert np.isnan(gravity_tilt([[0, 0, 0], [np.nan, 0, 0]])).all()
    q, heading, _ = absolute_from_separate_mag([[1, 0, 0, 0]], [[0, 0, 40]], np.eye(3))
    assert np.isnan(q).all() and np.isnan(heading).all()
    with pytest.raises(ValueError):
        absolute_from_separate_mag([[1, 0, 0, 0]], [[30, 0, 40]], -np.eye(3))


def test_slerp_gap_and_no_extrapolation():
    q = Rotation.from_euler('z', [[0], [.1], [.2]]).as_quat()[:, [3, 0, 1, 2]]
    out = sample_rotations([0, .05, 1], q, [-.1, .025, .5, 1.1])
    assert np.isnan(out[[0, 2, 3]]).all()
    np.testing.assert_allclose(Rotation.from_quat(out[1, [1, 2, 3, 0]]).as_rotvec(),
                               [0, 0, .05], atol=1e-12)
