import math

import pytest

from aquabeacon_analysis.provisional_angles import assumed_intrinsics, provisional_angle_rows


def test_hypothetical_ninety_degree_fov_and_center():
    intrinsics = assumed_intrinsics(1280, 720, 90)
    assert math.isclose(intrinsics.fx, 640)
    assert intrinsics.fy == intrinsics.fx
    assert (intrinsics.cx, intrinsics.cy) == (640, 360)


def test_angle_status_and_missing_rows_are_preserved():
    rows = [
        {"video_time_s": "0", "source_frame_index": "0", "status": "anchor",
         "x_px": "1280", "y_px": "360", "position_sigma_px": "8"},
        {"video_time_s": "0.5", "source_frame_index": "15", "status": "predicted",
         "x_px": "900", "y_px": "300", "position_sigma_px": "20"},
        {"video_time_s": "1", "source_frame_index": "30", "status": "untracked",
         "x_px": "", "y_px": "", "position_sigma_px": ""},
    ]
    output = provisional_angle_rows(rows, assumed_intrinsics(1280, 720, 90))
    assert math.isclose(float(output[0]["azimuth_deg"]), 45)
    assert output[0]["elevation_deg"] == 0
    assert output[0]["angle_status"] == "hypothetical_uncalibrated"
    assert output[1]["azimuth_rad"] == ""
    assert output[2]["elevation_rad"] == ""


def test_invalid_hypothetical_fov_rejected():
    with pytest.raises(ValueError):
        assumed_intrinsics(1280, 720, 180)
