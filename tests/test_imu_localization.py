from __future__ import annotations

import numpy as np

from aquabeacon_analysis.imu_localization import (
    align_body_attitude_to_camera,
    fit_mounting_with_time_refinement,
    fit_sony_to_camera,
    fuse_camera_and_gyro,
    fuse_child_attitudes,
    integrate_acceleration,
    quaternion_multiply,
    rotate_vectors,
    rotation_quaternion,
)


def test_quaternion_rotation_and_product() -> None:
    q = np.array([np.sqrt(0.5), 0, 0, np.sqrt(0.5)])
    np.testing.assert_allclose(rotate_vectors(q, np.array([1, 0, 0])), [0, 1, 0], atol=1e-12)
    np.testing.assert_allclose(quaternion_multiply(q, q), [0, 0, 0, 1], atol=1e-12)


def test_rotation_quaternion_negative_trace() -> None:
    matrix = np.diag([1, -1, -1])
    q = rotation_quaternion(matrix)
    np.testing.assert_allclose(rotate_vectors(q, np.eye(3)), matrix.T, atol=1e-12)


def test_integration_stationary_and_constant_acceleration() -> None:
    times = np.linspace(0, 10, 101)
    stationary = np.zeros((len(times), 3))
    velocity, position = integrate_acceleration(times, stationary)
    np.testing.assert_array_equal(velocity, stationary)
    np.testing.assert_array_equal(position, stationary)
    acceleration = stationary.copy()
    acceleration[:, 0] = 2
    velocity, position = integrate_acceleration(times, acceleration)
    np.testing.assert_allclose(velocity[:, 0], 2 * times, atol=1e-12)
    np.testing.assert_allclose(position[:, 0], times**2, atol=1e-12)


def test_gyro_axis_alignment() -> None:
    times = np.arange(0, 20, 1 / 30)
    angle = 0.6 * np.sin(0.9 * times)
    camera_q = np.column_stack([np.cos(angle / 2), np.zeros(len(times)),
                                np.zeros(len(times)), np.sin(angle / 2)])
    sony_time = np.arange(0, 20, 1 / 300)
    sony_gyro = np.column_stack([np.zeros(len(sony_time)),
                                 0.54 * np.cos(0.9 * sony_time),
                                 np.zeros(len(sony_time))])
    # A single-axis motion cannot constrain the full 3D mounting rotation.
    with np.testing.assert_raises(ValueError):
        fit_sony_to_camera(camera_q, times, sony_time, sony_gyro)


def test_gyro_alignment_recovers_three_axis_mounting() -> None:
    times = np.arange(0, 20, 1 / 30)
    dt = times[1] - times[0]
    camera_rate = np.column_stack([
        0.3 * np.sin(0.7 * times),
        0.4 * np.cos(0.9 * times),
        0.25 * np.sin(1.3 * times + 0.2),
    ])
    q = np.zeros((len(times), 4))
    q[0, 0] = 1
    for index in range(1, len(times)):
        rate = (camera_rate[index - 1] + camera_rate[index]) / 2
        angle = np.linalg.norm(rate) * dt
        step = np.r_[np.cos(angle / 2), rate / np.linalg.norm(rate) * np.sin(angle / 2)]
        q[index] = quaternion_multiply(q[index - 1], step)
    true_rotation = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]])
    sony_time = times.copy()
    sony_rate = camera_rate @ true_rotation
    fitted, quality = fit_sony_to_camera(q, times, sony_time, sony_rate)
    np.testing.assert_allclose(fitted, true_rotation, atol=0.002)
    assert quality["gyro_correlation"] > 0.99
    refined, offset, metrics = fit_mounting_with_time_refinement(
        q, times, sony_time + 1 / 30, sony_rate
    )
    np.testing.assert_allclose(refined, true_rotation, atol=0.005)
    assert abs(offset + 1 / 30) < 1 / 120 + 1e-9
    assert metrics["gyro_rms_rad_s"] < metrics["before_refinement_gyro_rms_rad_s"]


def test_fusion_uses_gyro_motion_and_limits_drift() -> None:
    times = np.linspace(0, 10, 301)
    yaw = 0.2 * times
    camera = np.column_stack([np.cos(yaw / 2), np.zeros(len(times)),
                              np.zeros(len(times)), np.sin(yaw / 2)])
    imu_times = np.linspace(-0.1, 10.1, 3061)
    biased_gyro = np.tile([0, 0, 0.22], (len(imu_times), 1))
    fused, gyro_only = fuse_camera_and_gyro(
        camera, times, imu_times, biased_gyro, np.eye(3)
    )
    camera_dot_fused = np.abs(np.sum(camera * fused, axis=1))
    camera_dot_gyro = np.abs(np.sum(camera * gyro_only, axis=1))
    assert np.rad2deg(2 * np.arccos(camera_dot_fused[-1])) < 1
    assert np.rad2deg(2 * np.arccos(camera_dot_gyro[-1])) > 10


def test_child_motion_frame_alignment() -> None:
    t = np.linspace(0, 2, 10)
    camera = np.column_stack([np.cos(t / 2), np.zeros(len(t)),
                              np.zeros(len(t)), np.sin(t / 2)])
    camera_from_body = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]])
    q_cb = rotation_quaternion(camera_from_body)
    body = quaternion_multiply(camera, np.broadcast_to(q_cb, camera.shape))
    aligned = align_body_attitude_to_camera(body, camera, camera_from_body)
    np.testing.assert_allclose(aligned, camera, atol=1e-12)


def test_child_fusion_preserves_gaps_and_rejects_large_disagreement() -> None:
    camera = np.tile([1.0, 0, 0, 0], (4, 1))
    body = camera.copy()
    body[1] = [np.cos(np.deg2rad(4)), 0, 0, np.sin(np.deg2rad(4))]
    body[2] = [np.cos(np.deg2rad(15)), 0, 0, np.sin(np.deg2rad(15))]
    body[3] = np.nan
    fused, angle, weight, status = fuse_child_attitudes(camera, body)
    assert status.tolist() == ["fused_equal", "body_downweighted",
                               "camera_only_disagreement", "camera_only_gap"]
    assert weight[0] == 0.5
    assert 0 < weight[1] < 0.5
    assert weight[2] == weight[3] == 0
    np.testing.assert_allclose(fused[2:], camera[2:])
    assert np.isnan(angle[3])
