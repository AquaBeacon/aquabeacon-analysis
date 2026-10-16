import math

import numpy as np
from scipy.spatial.transform import Rotation

from scripts.estimate_parent_child_positions import (
    interpolate_attitude,
    interpolate_gps,
    nearest_range,
    unit_ray,
)


def test_range_bearing_to_optical_coordinates():
    ray = unit_ray(math.atan(1), 0)
    assert np.allclose(ray, [2 ** -0.5, 0, 2 ** -0.5])
    assert np.allclose(unit_ray(0, math.atan(1)), [0, -2 ** -0.5, 2 ** -0.5])


def test_tof_selection_respects_validity_and_time():
    rows = [
        {"window_time_s": "1", "tof_valid": "1", "range_corrected_m": "2"},
        {"window_time_s": "1.1", "tof_valid": "0", "range_corrected_m": "9"},
        {"window_time_s": "2", "tof_valid": "1", "range_corrected_m": "3"},
    ]
    assert nearest_range(rows, 1.12)["range_corrected_m"] == "2"
    assert nearest_range(rows, 1.8)["range_corrected_m"] == "3"
    assert nearest_range(rows, 3) is None


def test_attitude_and_gps_do_not_cross_gaps():
    attitudes = [
        {"window_time_s": str(t), "qx": "0", "qy": "0", "qz": "0", "qw": "1"}
        for t in (0, 0.1, 1.0)
    ]
    assert isinstance(interpolate_attitude(attitudes, 0.05), Rotation)
    assert interpolate_attitude(attitudes, 0.5) is None
    gps = [
        {"window_time_s": str(t), "east_m": str(t), "north_m": "0", "status": "gps_interpolated", "segment_id": segment}
        for t, segment in ((0, "0"), (1, "0"), (2, "1"))
    ]
    assert interpolate_gps(gps, 0.5) == (0.5, 0.0)
    assert interpolate_gps(gps, 1.5) is None
