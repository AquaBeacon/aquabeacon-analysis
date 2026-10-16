"""Checks for native-rate frame timestamps derived from camera telemetry."""

from itertools import pairwise

import pytest

from scripts.prepare_child_orbslam3_120fps import frame_plan


def test_frame_plan_uses_all_intermediate_120fps_frames() -> None:
    rows = [
        {"camera_frame": "100", "refined_sync_utc_us": "1000000"},
        {"camera_frame": "101", "refined_sync_utc_us": "1033367"},
        {"camera_frame": "102", "refined_sync_utc_us": "1066734"},
    ]
    result = frame_plan(rows, 100, 9)
    assert len(result) == 9
    assert [index for index, _ in result] == list(range(9))
    assert result[0][1] == 1_000_000_000
    assert result[4][1] == 1_033_367_000
    assert result[8][1] == 1_066_734_000
    assert all(b[1] > a[1] for a, b in pairwise(result))


def test_frame_plan_rejects_reversed_utc() -> None:
    rows = [
        {"camera_frame": "100", "refined_sync_utc_us": "1000000"},
        {"camera_frame": "101", "refined_sync_utc_us": "999999"},
    ]
    with pytest.raises(ValueError, match="nonmonotonic"):
        frame_plan(rows, 100, 9)
