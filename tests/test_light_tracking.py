import math

from aquabeacon_analysis.light_detection import Candidate
from aquabeacon_analysis.light_tracking import (
    LightEKF,
    TrackingConfig,
    anchor_guide,
    associate,
    measurement_sigma,
    strong_candidate,
)


def candidate(x: float, y: float, score: float = 12.0) -> Candidate:
    return Candidate(x, y, 25, 70.0, 15.0, 0.9, score)


def test_ekf_tracks_motion_through_noise():
    config = TrackingConfig()
    filter_ = LightEKF(100, 90, 25, config)
    for step in range(1, 21):
        filter_.predict(0.5)
        measurement = candidate(100 + 4 * step + (-2 if step % 2 else 2), 90 + step)
        filter_.update(measurement, measurement_sigma(measurement))
    assert abs(filter_.state[0] - 180) < 6
    assert abs(filter_.state[1] - 110) < 4
    assert math.isfinite(filter_.state[4])
    assert filter_.position_sigma_px < 10


def test_ekf_rejects_bright_reflection_outside_gate():
    config = TrackingConfig()
    filter_ = LightEKF(100, 90, 25, config)
    filter_.predict(0.5)
    selected, distance = associate(
        filter_, [candidate(102, 91, 11), candidate(400, 300, 20)], config
    )
    assert selected is not None
    assert (selected.x, selected.y) == (102, 91)
    assert distance is not None and distance < config.gate_chi2


def test_ekf_rejects_rock_colored_candidate_near_prediction():
    config = TrackingConfig()
    filter_ = LightEKF(100, 90, 25, config)
    filter_.predict(0.5)
    rock = Candidate(101, 90, 25, 70.0, None, 0.1, 20.0)
    selected, distance = associate(filter_, [rock], config)
    assert selected is None
    assert distance is None


def test_strong_light_reacquisition_requires_visual_evidence():
    true_light = Candidate(300, 120, 350, 80.0, 150.0, 0.9, 17.0)
    rock = Candidate(305, 130, 100, 80.0, 10.0, 0.1, 18.0)
    assert strong_candidate([rock, true_light], (290, 120, 70)) == true_light
    assert strong_candidate([rock], (290, 120, 70)) is None


def test_guide_allows_nominal_thirty_second_anchor_gap():
    anchors = {3600: (380, 120, 20), 4500: (300, 115, 20)}
    guide = anchor_guide(3900, 29.97002997, sorted(anchors), anchors)
    assert guide is not None
    assert abs(guide[0] - (380 - 80 / 3)) < 0.01
