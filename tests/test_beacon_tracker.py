import cv2
import numpy as np

from aquabeacon_analysis.beacon_tracker import BeaconTracker


def frame_at(x, y, present=True):
    yy, xx = np.mgrid[:720, :1280]
    a = np.zeros((720, 1280, 3), np.float32) + [70, 45, 15]
    if present:
        glow = np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * 14**2))
        a += glow[..., None] * [180, 205, 170]
    out = np.clip(a, 0, 255).astype(np.uint8)
    cv2.line(out, (900, 700), (800, 30), (255, 255, 255), 8)
    return out


def test_tracks_image_motion_without_sticking_to_tether():
    tracker = BeaconTracker([330, 310])
    errors = []
    for i in range(12):
        xy = [330 + 3 * i, 310 + 10 * np.sin(i / 2)]
        row = tracker.update(frame_at(*xy))
        assert row["candidate"]
        errors.append(np.linalg.norm(np.array([row["x_px"], row["y_px"]]) - xy))
    assert max(errors) < 3
    assert row["track_status"] == "tracked_template"


def test_occlusion_does_not_emit_predicted_positions():
    tracker = BeaconTracker([330, 310])
    assert tracker.update(frame_at(330, 310))["candidate"]
    for _ in range(12):
        row = tracker.update(frame_at(330, 310, False))
        assert not row["candidate"]
        assert np.isnan(row["x_px"])


def test_failed_initialization_waits_for_reviewed_seed():
    tracker = BeaconTracker([330, 310])
    assert not tracker.update(frame_at(330, 310, False))["candidate"]
    for _ in range(12):
        assert not tracker.update(frame_at(330, 310))["candidate"]
    row = tracker.update(frame_at(330, 310), seed=[330, 310])
    assert row["candidate"]
    assert np.linalg.norm(np.array([row["x_px"], row["y_px"]]) - [330, 310]) < 3
