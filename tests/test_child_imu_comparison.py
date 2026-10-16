import numpy as np

from scripts.compare_child_imu_dead_reckoning import (
    GRAVITY_M_S2,
    integrate_child_from_start,
    keep_strictly_increasing,
)


def test_child_dead_reckoning_static_specific_force():
    times = np.linspace(0, 2, 201)
    acceleration = np.tile([0, 0, GRAVITY_M_S2], (len(times), 1))
    quaternions = np.tile([0, 0, 0, 1], (len(times), 1))
    _, velocity, position = integrate_child_from_start(
        times, acceleration, quaternions, 0.5
    )
    assert np.max(np.abs(velocity)) < 1e-9
    assert np.max(np.abs(position)) < 1e-9


def test_child_dead_reckoning_constant_acceleration_from_common_start():
    times = np.linspace(0, 2, 201)
    acceleration = np.tile([1, 0, GRAVITY_M_S2], (len(times), 1))
    quaternions = np.tile([0, 0, 0, 1], (len(times), 1))
    integration_times, velocity, position = integrate_child_from_start(
        times, acceleration, quaternions, 0.5
    )
    assert np.isclose(integration_times[0], 0.5)
    assert np.isclose(velocity[-1, 0], 1.5, atol=1e-6)
    assert np.isclose(position[-1, 0], 1.125, atol=1e-6)


def test_nonmonotonic_source_row_excluded():
    times = np.array([0.0, 0.01, 0.02, 0.015, 0.03])
    assert keep_strictly_increasing(times).tolist() == [True, True, True, False, True]
