import numpy as np
import pytest

from scripts.prepare_orbslam3_fused_imu import angular_velocity_from_quaternions


def test_fused_quaternion_derivative_uses_body_axes_and_seconds() -> None:
    times_ns = np.array([0, 1_000_000_000, 2_000_000_000])
    angle_rad = np.array([0.0, 0.2, 0.4])
    quaternion = np.column_stack((np.cos(angle_rad / 2), np.zeros(3),
                                  np.zeros(3), np.sin(angle_rad / 2)))
    quaternion[1] *= -1  # Equivalent orientation must not create a full rotation.
    np.testing.assert_allclose(
        angular_velocity_from_quaternions(quaternion, times_ns),
        [[0.0, 0.0, 0.2], [0.0, 0.0, 0.2]], atol=1e-12)


def test_fused_quaternion_derivative_rejects_nonmonotonic_time() -> None:
    quaternion = np.tile([1.0, 0.0, 0.0, 0.0], (2, 1))
    with pytest.raises(ValueError, match="increase strictly"):
        angular_velocity_from_quaternions(quaternion, np.array([1, 1]))
