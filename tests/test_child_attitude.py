import numpy as np
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.child_attitude import estimate_attitude, magnetic_ned_quaternions


def test_magnetic_ned_compass_and_tilt():
    headings = np.array([0, 90, 180, 270, 359.9])
    rp = np.tile([.2, -.3], (len(headings), 1))
    q = magnetic_ned_quaternions(rp, headings)
    rotations = Rotation.from_quat(q[:, [1, 2, 3, 0]])
    forward = rotations.apply(np.tile([1, 0, 0], (len(q), 1)))
    np.testing.assert_allclose(np.degrees(np.arctan2(forward[:, 1], forward[:, 0])) % 360,
                               headings, atol=1e-12)
    np.testing.assert_allclose(rotations.as_euler('xyz')[:, :2], rp, atol=1e-12)
    assert np.isnan(magnetic_ned_quaternions([[0, 0]], [np.nan])).all()


def test_absolute_attitude_independent_of_game_yaw_and_sensor_mount():
    truth = Rotation.from_euler('xyz', [[.2, -.3, 1.1], [-.4, .5, -2.]])
    mount = Rotation.from_euler('xyz', [.4, .6, -.2])
    game_from_ned = (Rotation.from_euler('z', 1.7)
                     * Rotation.from_matrix(np.diag([1., -1., -1.])))
    game_from_sensor = game_from_ned * truth * mount
    mag_sensor = (truth * mount).inv().apply([[30, 0, 40]] * 2)
    _, angles, heading, observable = estimate_attitude(
        game_from_sensor.as_quat()[:, [3, 0, 1, 2]], mag_sensor, mount.as_matrix())
    q = magnetic_ned_quaternions(angles[:, :2], heading)
    assert observable.all()
    np.testing.assert_allclose(Rotation.from_quat(q[:, [1, 2, 3, 0]]).as_matrix(),
                               truth.as_matrix(), atol=1e-12)


def test_known_frd_angles_and_magnetic_north():
    expected = np.array([[.2, -.3, .8], [-.1, .4, -1.]])
    local = Rotation.from_euler('xyz', expected)
    flip = Rotation.from_matrix(np.diag([1., -1., -1.]))
    sensor = flip * local
    q = sensor.as_quat()[:, [3, 0, 1, 2]]
    magnetic = sensor.inv().apply([[30., 0., -40.]] * 2)
    actual_q, angles, heading, observable = estimate_attitude(q, magnetic, np.eye(3))
    np.testing.assert_allclose(angles, expected, atol=1e-12)
    np.testing.assert_allclose(heading, np.degrees(expected[:, 2]) % 360)
    np.testing.assert_allclose(Rotation.from_quat(actual_q[:, [1, 2, 3, 0]]).as_matrix(),
                               local.as_matrix(), atol=1e-12)
    assert observable.all()


def test_invalid_samples_and_vertical_forward():
    sensor = (Rotation.from_matrix(np.diag([1., -1., -1.]))
              * Rotation.from_euler('y', np.pi / 2))
    q = sensor.as_quat()[[3, 0, 1, 2]]
    local, angles, heading, observable = estimate_attitude(
        [[0, 0, 0, 0], q], [[0, 0, 0], [30, 0, 40]], np.eye(3))
    assert np.isnan(local[0]).all()
    assert np.isnan(angles[0]).all()
    assert np.isnan(angles[1, [0, 2]]).all()
    assert np.isnan(heading).all()
    assert not observable.any()
