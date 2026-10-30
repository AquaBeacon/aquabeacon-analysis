import numpy as np
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.child_vslam import (
    frame_interval,
    rotation_angle_difference,
    sample_rotations,
    spans,
)


def test_native_half_open_interval():
    np.testing.assert_array_equal(frame_interval(1, 2, 120, 1000), np.arange(120, 240))


def test_rotation_invariant_to_both_frames_and_sign():
    r = Rotation.from_euler('xyz', np.arange(60).reshape(20, 3), degrees=True)
    camera = Rotation.from_euler('z', 67, degrees=True) * r * Rotation.from_euler(
        'y', 25, degrees=True)
    q = camera.as_quat()
    q[::2] *= -1
    _, _, difference = rotation_angle_difference(q, r.as_quat(), np.ones(20, bool), 3)
    np.testing.assert_allclose(difference[3:], 0, atol=1e-12)


def test_no_pair_crosses_tracking_gap():
    q = Rotation.identity(10).as_quat()
    ok = np.ones(10, bool)
    ok[4] = False
    _, _, difference = rotation_angle_difference(q, q, ok, 3)
    assert np.isnan(difference[4:8]).all()
    assert difference[3] == 0 and difference[8] == 0


def test_slerp_never_bridges_outage_or_extrapolates():
    q = Rotation.from_euler('z', [[0], [10], [90]], degrees=True).as_quat()
    sampled = sample_rotations([0, .05, 1], q, [-1, .025, .5, 2])
    assert np.isnan(sampled[[0, 2, 3]]).all()
    np.testing.assert_allclose(Rotation.from_quat(sampled[1]).magnitude(), np.deg2rad(5))


def test_spans_include_all_states():
    assert spans([1, 1, 2, 3, 3]) == [
        {'start_frame': 0, 'end_frame_exclusive': 2, 'state': 1},
        {'start_frame': 2, 'end_frame_exclusive': 3, 'state': 2},
        {'start_frame': 3, 'end_frame_exclusive': 5, 'state': 3}]
