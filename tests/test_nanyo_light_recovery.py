"""Reject changes to old measurements, missing values, review vetoes and clocks."""
import sys
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
from evaluate_nanyo_light_recovery import verify_observations
from position_fallback_nanyo import mixed_camera_rays
from review_nanyo_light_recovery import apply_vetoes

from aquabeacon_analysis.ngt_position import calibrated_rays


def observations():
    old = [
        {"utc_s": "100.0", "actual_source_s": "10.0", "x_px": "0.0", "y_px": "123.0",
         "optical_candidate": "True", "track_status": "tracked_template"},
        {"utc_s": "100.1", "actual_source_s": "10.1", "x_px": "nan", "y_px": "nan",
         "optical_candidate": "False", "track_status": "missing_tracked_template"},
        {"utc_s": "100.2", "actual_source_s": "10.2", "x_px": "nan", "y_px": "nan",
         "optical_candidate": "False", "track_status": "rejected_identity_review"},
    ]
    new = [{**r, "recovered_candidate": "False", "decoded_source_s": r["actual_source_s"],
            "decoded_utc_s": r["utc_s"]} for r in old]
    new[1].update(utc_s="100.11", actual_source_s="10.11", decoded_source_s="10.11",
                  decoded_utc_s="100.11", x_px="120.1", y_px="80.1", optical_candidate="True",
                  recovered_candidate="True", track_status="recovered_tracked_template")
    return old, new


def test_exact_old_values_and_actual_time_of_addition():
    old, new = observations()
    original = deepcopy(old)
    assert verify_observations(old, new) == 1
    assert old == original


@pytest.mark.parametrize("row,key,value", [
    (0, "x_px", "0"), (0, "utc_s", "100.001"),
    (1, "decoded_source_s", "10.13"), (1, "decoded_utc_s", "100.1"),
    (1, "x_px", "nan"), (1, "y_px", "720"),
    (2, "optical_candidate", "True"), (2, "x_px", "2.0"),
])
def test_invalid_preservation_or_frame_contract_rejected(row, key, value):
    old, new = observations()
    new[row][key] = value
    with pytest.raises(ValueError):
        verify_observations(old, new)


def test_mixed_video_calibration_keeps_lrf_rays_and_maps_mp4_pixel_centers():
    pixels = np.array([[0., 120.], [640., 360.], [np.nan, np.nan]])
    proxy_k = np.array([[600., 0, 640], [0, 600, 360], [0, 0, 1.]])
    main_k = np.array([[1100., 0, 970], [0, 1100, 540], [0, 0, 1.]])
    distortion = np.zeros(5)
    result = mixed_camera_rays(pixels, [False, True, False], proxy_k, distortion,
                               main_k, distortion)
    np.testing.assert_array_equal(result[0], calibrated_rays(pixels[:1], proxy_k, distortion)[0])
    native = (pixels[1:2]+.5)*1.5-.5
    np.testing.assert_allclose(result[1], calibrated_rays(native, main_k, distortion)[0])
    assert np.isnan(result[2]).all()
    with pytest.raises(ValueError, match="calibration"):
        mixed_camera_rays(pixels, [False, True, False], proxy_k, distortion)


def test_review_exclusion_only_removes_new_measurements_and_keeps_automatic_input():
    old, automatic = observations()
    saved = deepcopy(automatic)
    reviewed = apply_vetoes(old, automatic, [
        {"first_row": 0, "last_row": 2, "reason": "Identity lost at a reflective net"}])
    assert automatic == saved
    assert all(reviewed[0][key] == value for key, value in old[0].items())
    assert reviewed[1]["identity_review_rejected"]
    assert reviewed[1]["automatic_recovered_candidate"]
    assert reviewed[1]["recovered_candidate"] == "False"
    assert reviewed[1]["utc_s"] == old[1]["utc_s"]
    assert verify_observations(old, reviewed) == 0
    assert not reviewed[2]["identity_review_rejected"]


@pytest.mark.parametrize("first,last", [(-1, 1), (1, 3), (2, 1)])
def test_invalid_review_interval_is_rejected(first, last):
    old, automatic = observations()
    with pytest.raises(ValueError, match="range"):
        apply_vetoes(old, automatic, [{"first_row": first, "last_row": last, "reason": "Review"}])
