"""Regression cases for colored/dim lamps, image coordinates and recovery safety."""
import cv2
import numpy as np
import pytest

from aquabeacon_analysis.beacon_detection import detect_beacon, detect_luminous_core
from aquabeacon_analysis.beacon_tracker import BeaconTracker
from scripts.improve_nanyo_light import merge_detection


def lamp_frame(color=(150, 160, 210), center=(350, 300), shape=(720, 1280), sigma=5):
    yy, xx = np.mgrid[:shape[0], :shape[1]]
    image = np.empty((*shape, 3), np.float32)
    image[:] = [90, 65, 25]
    glow = np.exp(-((xx-center[0])**2+(yy-center[1])**2)/(2*sigma**2))
    image += glow[..., None]*color
    return np.clip(image, 0, 255).astype(np.uint8)


@pytest.mark.parametrize("color", [(110, 110, 210), (130, 100, 100), (20, 20, 20)])
def test_unsaturated_magenta_white_and_dim_lamps(color):
    frame = lamp_frame(color=color)
    det = detect_luminous_core(frame, [350, 300], 20)
    assert det["candidate"]
    assert np.hypot(det["x_px"]-350, det["y_px"]-300) < 2


@pytest.mark.parametrize("shape,center", [((360, 640), (230, 160)),
                                         ((1280, 720), (310, 740)),
                                         ((1080, 1920), (905, 480))])
def test_input_pixel_basis_and_portrait_frames(shape, center):
    result = detect_luminous_core(lamp_frame(shape=shape, center=center), center, 30)
    assert result["candidate"]
    assert np.hypot(result["x_px"]-center[0], result["y_px"]-center[1]) < 2


def test_bright_line_uniform_background_and_outside_roi_rejected():
    frame = np.full((720, 1280, 3), [90, 65, 25], np.uint8)
    assert not detect_luminous_core(frame, [350, 300], 25)["candidate"]
    cv2.line(frame, (350, 0), (350, 719), (255, 255, 255), 4)
    assert not detect_luminous_core(frame, [350, 300], 25)["candidate"]
    assert not detect_luminous_core(lamp_frame(), [800, 500], 25)["candidate"]
    assert not detect_luminous_core(lamp_frame(), [-100, -100], 20)["candidate"]


def test_core_centroid_not_clipped_to_roi_boundary():
    result = detect_luminous_core(lamp_frame(sigma=8), [365, 300], 20)
    assert result["candidate"]
    assert np.hypot(result["x_px"]-350, result["y_px"]-300) < 2


@pytest.mark.parametrize("template_size", [41, 81])
def test_color_recovery_tracks_motion_and_emits_nan_during_occlusion(template_size):
    tracker = BeaconTracker([350, 300], detector=detect_beacon, template_size=template_size)
    for i in range(12):
        point = (350+3*i, 300)
        result = tracker.update(lamp_frame(center=point))
        assert result["candidate"]
        assert np.hypot(result["x_px"]-point[0], result["y_px"]-point[1]) < 2
    for _ in range(12):
        result = tracker.update(lamp_frame(color=(0, 0, 0)))
        assert not result["candidate"]
        assert np.isnan(result["x_px"])


def test_failed_seed_does_not_self_initialize_on_later_light():
    tracker = BeaconTracker([350, 300], detector=detect_beacon)
    assert not tracker.update(lamp_frame(color=(0, 0, 0)))["candidate"]
    assert not tracker.update(lamp_frame())["candidate"]
    assert tracker.update(lamp_frame(), seed=[350, 300])["candidate"]


def test_baseline_measurements_and_vetoes_are_preserved():
    original = {"optical_candidate": "True", "water_like_heuristic": "True",
                "utc_s": "123.456", "x_px": "0.0", "y_px": "300.55",
                "track_status": "tracked_template", "template_ncc": "0.94"}
    recovered = {"candidate": True, "x_px": 12, "y_px": 14,
                 "track_status": "tracked_template", "template_ncc": 0.85}
    result = merge_detection(original, recovered)
    assert all(result[k] == value for k, value in original.items())
    missing = {**original, "optical_candidate": "False", "x_px": "nan", "y_px": "nan"}
    result = merge_detection(missing, recovered, excluded=True)
    assert result["optical_candidate"] == "False"
    assert result["x_px"] == "nan"
    result = merge_detection(missing, recovered)
    assert result["recovered_candidate"]
    assert result["x_px"] == 12
    assert result["utc_s"] == original["utc_s"]


def test_out_of_image_roi_fails_without_legacy_empty_patch_crash():
    assert not detect_beacon(lamp_frame(), [-100, -100], 20)["candidate"]
    assert not detect_beacon(lamp_frame(), [1280, 720], 20)["candidate"]
    with pytest.raises(ValueError, match="finite"):
        detect_beacon(lamp_frame(), [np.nan, 30], 20)


def test_bright_magenta_core_wins_over_adjacent_cyan_tether():
    frame = lamp_frame(color=(140, 120, 230))
    cv2.line(frame, (360, 313), (390, 380), (200, 170, 30), 3)
    result = detect_beacon(frame, [350, 308], 20)
    assert result["candidate"]
    assert np.hypot(result["x_px"]-350, result["y_px"]-300) < 3


def test_long_loss_requires_reviewed_reinitialization():
    tracker = BeaconTracker([350, 300], detector=detect_beacon,
                            template_size=41, max_missing_frames=10)
    assert tracker.update(lamp_frame())["candidate"]
    for _ in range(10):
        assert not tracker.update(lamp_frame(color=(0, 0, 0)))["candidate"]
    assert not tracker.update(lamp_frame())["candidate"]
    assert tracker.update(lamp_frame(), seed=[350, 300])["candidate"]


def test_changing_halo_keeps_measured_core_with_consistent_flow():
    yy, xx = np.mgrid[:180, :320]
    core = np.exp(-((xx-150)**2+(yy-90)**2)/(2*3**2))
    tracker = BeaconTracker([150, 90], detector=detect_beacon,
                            template_size=41, allow_flow_core=True, max_missing_frames=10)
    modes = []
    for i in range(40):
        halo = np.exp(-((xx-150-25*np.sin(i/10))**2+(yy-90)**2)/(2*14**2))
        frame = np.clip(30+core[..., None]*[150, 140, 190]
                        + halo[..., None]*[120, 80, 80], 0, 255).astype(np.uint8)
        row = tracker.update(frame)
        modes.append(row["track_status"])
        if row["candidate"]:
            assert np.hypot(row["x_px"]-150, row["y_px"]-90) < 4
    assert "tracked_flow_core" in modes
    blank = np.full((180, 320, 3), 30, np.uint8)
    row = tracker.update(blank)
    assert not row["candidate"]
    assert np.isnan(row["x_px"])


def test_seed_image_prevents_applying_old_pixel_to_later_moving_frame():
    seed_image = lamp_frame(center=(350, 300))
    later = lamp_frame(center=(380, 300))
    wrong = BeaconTracker([350, 300], detector=detect_beacon, template_size=41)
    assert not wrong.update(later)["candidate"]
    correct = BeaconTracker([350, 300], detector=detect_beacon, template_size=41,
                            allow_flow_core=True, max_missing_frames=10)
    assert correct.update(seed_image)["candidate"]
    result = correct.update(later)
    assert result["candidate"]
    assert np.hypot(result["x_px"]-380, result["y_px"]-300) < 2
