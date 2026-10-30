import numpy as np

from aquabeacon_analysis.ngt_fusion import (
    cv_filter,
    fuse_local_attitude,
    gap_interpolate,
    pressure_depth,
    unobservable_child_position,
)


def test_interpolation_does_not_bridge_gap_or_extrapolate():
    out = gap_interpolate(np.array([0., .01, 2., 2.01]), np.arange(4.),
                          np.array([-.1, .005, 1., 2.005, 3.]))
    assert np.isnan(out[[0, 2, 4]]).all()
    np.testing.assert_allclose(out[[1, 3]], [.5, 2.5])


def test_attitude_rotated_gyro_prediction_and_gap_reset():
    t = np.array([0., .01, .02, 1.])
    q = np.tile([1., 0, 0, 0], (4, 1))
    gyro = np.tile([1., 0, 0], (4, 1))
    rotation = np.array([[0., -1, 0], [1., 0, 0], [0., 0, 1]])
    fused, used, reset = fuse_local_attitude(t, q, t, gyro, rotation)
    assert fused[1, 2] > 0
    assert abs(fused[1, 1]) < 1e-9
    assert used[1] and reset[-1]
    np.testing.assert_allclose(fused[-1], q[-1])
    np.testing.assert_allclose(np.linalg.norm(fused, axis=1), 1)


def test_filter_long_loss_produces_nan_then_reinitializes():
    t = np.arange(10.)
    observations = np.arange(10.)[:, None]
    valid = np.array([True, True, False, False, False, False, False, False, True, True])
    output, covariance, accepted, _, reset = cv_filter(
        t, observations, np.ones((10, 1)), valid,
        np.zeros(10, bool), np.zeros(10, bool))
    assert np.isnan(output[5:8]).all()
    assert reset[8] and accepted[8]
    assert output[8, 0] == 8
    assert np.nanmin(covariance) >= 0


def test_pressure_units_and_unobservable_xy_are_not_fabricated():
    np.testing.assert_allclose(pressure_depth(np.array([1031.2, 1031.2 + 997 * 9.80665 / 100]), 1031.2), [0, 1])
    assert np.isnan(unobservable_child_position(3)).all()


def test_quiet_imu_alone_preserves_constant_velocity_without_zero_rate():
    times = np.arange(6.)
    observations = times[:, None]
    valid = np.array([True, True, True, False, False, False])
    sigma = np.full((6, 1), .01)
    quiet = np.ones(6, bool)
    unconstrained, covariance, *_ = cv_filter(
        times, observations, sigma, valid, quiet, np.zeros(6, bool), innovation_gate=1e12)
    constrained, *_ = cv_filter(
        times, observations, sigma, valid, quiet, quiet, innovation_gate=1e12)
    assert unconstrained[-1, 0] > 4.9
    assert constrained[-1, 0] < unconstrained[-1, 0] - 1
    assert np.isfinite(covariance).all()
    assert (covariance >= 0).all()
