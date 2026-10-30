"""Check replay frame, clock, gap and source-track semantics independently."""
import struct

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.ngt_replay_v2 import (
    compose_cad_quaternions,
    enu_quaternions,
    observation_times,
    read_image_names,
    read_points,
    sample_attitudes,
    utc_to_video,
)


def matrix(q):
    return Rotation.from_quat(np.asarray(q)[[1, 2, 3, 0]]).as_matrix()


def test_north_facing_level_frd_has_forward_north_right_east_down_negative_up():
    q = enu_quaternions([[1, 0, 0, 0]])[0]
    np.testing.assert_allclose(matrix(q), [[0, 1, 0], [1, 0, 0], [0, 0, -1]], atol=1e-14)


def test_east_facing_and_cad_rotation_composition():
    ned = Rotation.from_euler("z", 90, degrees=True).as_quat()[[3, 0, 1, 2]]
    enu = enu_quaternions([ned])
    np.testing.assert_allclose(matrix(enu[0])[:, 0], [1, 0, 0], atol=1e-14)
    cad = compose_cad_quaternions(enu, np.diag([1, -1, -1]))[0]
    np.testing.assert_allclose(matrix(cad), matrix(enu[0]) @ np.diag([1, -1, -1]))
    with pytest.raises(ValueError, match="proper rotation"):
        compose_cad_quaternions(enu, np.diag([1, 1, -1]))


def test_clock_affine_mapping_keeps_scale_and_offset():
    np.testing.assert_allclose(utc_to_video([100, 102, 104], 100, 2), [0, 1, 2])
    with pytest.raises(ValueError):
        utc_to_video([0], 100, 0)


def test_slerp_sign_equivalence_and_halfway_rotation():
    q = Rotation.from_euler("z", [[0], [90]], degrees=True).as_quat()[:, [3, 0, 1, 2]]
    q[1] *= -1
    result, _, method = sample_attitudes([0, .1], q, [True, True], [.05])
    assert method == ["slerp"]
    np.testing.assert_allclose(matrix(result[0])[:, 0], [2**-.5, 2**-.5, 0], atol=1e-14)


def test_missing_intervals_invalid_samples_and_no_extrapolation():
    q = np.array([[1., 0, 0, 0]] * 4)
    result, _, methods = sample_attitudes([0, .1, 1, 1.1], q, [True] * 4,
                                         [-.01, .5, 1.11], nearest_tolerance_s=.055)
    assert np.isnan(result).all()
    assert methods == ["missing"] * 3
    result, _, methods = sample_attitudes([0, .1, .2, .3], q, [True, False, True, True],
                                         [.1], nearest_tolerance_s=.055)
    assert methods == ["missing"]
    assert np.isnan(result).all()


def test_nearest_is_explicit_and_bounded_independently_of_slerp_gap():
    q = np.array([[1., 0, 0, 0]] * 2)
    result, delta, methods = sample_attitudes([0, .12], q, [True, True], [.04, .06],
                                              nearest_tolerance_s=.055)
    assert methods == ["nearest", "missing"]
    assert delta[0] == -.04
    np.testing.assert_array_equal(result[0], q[0])


def test_reveal_is_second_distinct_track_observation_not_distance_or_last():
    first, second, last, count = observation_times([9, 1, 3, 3],
        {9: "c", 1: "a", 3: "b"}, {"a": 10., "b": 11., "c": 20.})
    assert (first, second, last, count) == (10, 11, 20, 3)
    with pytest.raises(ValueError, match="two distinct"):
        observation_times([1, 1], {1: "a"}, {"a": 10})
    with pytest.raises(KeyError):
        observation_times([1, 2], {1: "a"}, {"a": 10})


def test_binary_decoding_retains_original_xyz_rgb_id_and_track(tmp_path):
    images = tmp_path / "images.bin"
    with images.open("wb") as stream:
        stream.write(struct.pack("<Q", 2))
        for index, name in [(7, "a.png"), (8, "b.png")]:
            stream.write(struct.pack("<i7di", index, 1, 0, 0, 0, 0, 0, 0, 3))
            stream.write(name.encode() + b"\0")
            stream.write(struct.pack("<Qddq", 1, 12.5, 9., 55))
    assert read_image_names(images) == {7: "a.png", 8: "b.png"}
    points = tmp_path / "points3D.bin"
    with points.open("wb") as stream:
        stream.write(struct.pack("<Q", 1))
        stream.write(struct.pack("<Q3d3BdQ", 55, 1.2345678912345, -2., 3., 0, 123, 255, .2, 2))
        stream.write(struct.pack("<4i", 7, 0, 8, 1))
    pid, xyz, rgb, track = next(read_points(points))
    assert pid == 55
    assert xyz == (1.2345678912345, -2., 3.)
    assert rgb == (0, 123, 255)
    np.testing.assert_array_equal(track, [7, 8])
