import math

import numpy as np

from aquabeacon_analysis.attitude_light_angles import (
    interpolate_attitude,
    stabilized_angles,
)

MOUNT = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]])
BASE = np.array([math.cos(math.pi / 4), 0, -math.sin(math.pi / 4), 0])
FORWARD = np.array([-1, 0, 0])


def test_level_attitude_preserves_right_positive_and_up_positive():
    azimuth, elevation = stabilized_angles(
        math.radians(20), math.radians(10), BASE, MOUNT, FORWARD,
    )
    assert math.isclose(azimuth, math.radians(20))
    assert math.isclose(elevation, math.radians(10))


def test_yaw_rotation_changes_local_heading_but_not_elevation():
    q = np.array([
        math.cos(math.pi / 8) * BASE[0],
        -math.sin(math.pi / 8) * BASE[2],
        math.cos(math.pi / 8) * BASE[2],
        math.sin(math.pi / 8) * BASE[0],
    ])
    azimuth, elevation = stabilized_angles(0, 0, q, MOUNT, FORWARD)
    assert math.isclose(azimuth, -math.pi / 4)
    assert math.isclose(elevation, 0)


def test_interpolation_refuses_outside_or_long_gap():
    q = np.array([[1, 0, 0, 0], [0, 0, 0, 1]])
    times = np.array([0.0, 1.0])
    assert interpolate_attitude(times, q, -0.01) is None
    assert interpolate_attitude(times, q, 0.5) is None
    mid = interpolate_attitude(times, q, 0.5, max_gap_s=2)
    assert math.isclose(np.linalg.norm(mid), 1)
