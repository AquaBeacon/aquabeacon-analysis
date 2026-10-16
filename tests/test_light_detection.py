import math

import cv2
import numpy as np

from aquabeacon_analysis.light_detection import Intrinsics, camera_angles, lamp_candidates


def test_center_ray_has_zero_angles():
    azimuth, elevation = camera_angles(640, 360, Intrinsics(800, 800, 640, 360))
    assert azimuth == 0
    assert elevation == 0


def test_right_and_up_angles():
    azimuth, elevation = camera_angles(1040, 160, Intrinsics(800, 800, 640, 360))
    assert math.isclose(azimuth, math.atan(0.5))
    assert math.isclose(elevation, math.atan2(0.25, math.hypot(1, 0.5)))


def test_orange_housing_and_white_light_are_detected():
    frame = np.full((360, 640, 3), (100, 60, 20), dtype=np.uint8)
    cv2.rectangle(frame, (300, 175), (330, 205), (20, 90, 240), -1)
    cv2.circle(frame, (280, 180), 7, (255, 255, 255), -1)
    candidates = lamp_candidates(frame)
    assert candidates
    assert math.hypot(candidates[0].x - 280, candidates[0].y - 180) < 3


def test_saturated_nearby_lamp_core_is_retained():
    frame = np.full((360, 640, 3), (100, 70, 30), dtype=np.uint8)
    cv2.circle(frame, (280, 180), 24, (255, 255, 255), -1)
    candidates = lamp_candidates(frame)
    assert any(
        math.hypot(item.x - 280, item.y - 180) < 3 and item.area > 1000
        for item in candidates
    )


def test_brightest_core_centers_asymmetric_bloom():
    frame = np.full((360, 640, 3), (95, 65, 30), dtype=np.uint8)
    cv2.ellipse(frame, (260, 180), (30, 18), 0, 0, 360, (220, 220, 220), -1)
    cv2.circle(frame, (280, 180), 7, (255, 255, 255), -1)
    candidates = lamp_candidates(frame)
    assert math.hypot(candidates[0].x - 280, candidates[0].y - 180) < 3
