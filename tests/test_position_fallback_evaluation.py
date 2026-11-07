"""Statistical and sampling contracts without loading experiment data."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from evaluate_position_fallback import (
    METHODS,
    angular_rms,
    evaluate_window,
    longest_grid_gap,
    scatter,
)


def evaluation_data(times, event_flags, grid_flags):
    n = len(times)
    data = {
        "times": np.asarray(times, dtype=float),
        "records": [{"event_sample": bool(e), "grid_sample": bool(g)}
                    for e, g in zip(event_flags, grid_flags)],
        "metadata": {"experiment_id": "synthetic", "grid_hz": 10},
        "ranges": np.full(n, 5.0),
        "rays": np.tile([0.0, 0.0, 1.0], (n, 1)),
    }
    result = {m: np.zeros((n, 3)) for m in METHODS}
    return data, result


def window(start=0, end=60):
    return {"id": "static", "condition": "synthetic_static", "start_s": start, "end_s": end}


def metric(rows, sampling, method):
    return next(r for r in rows if r["sampling"] == sampling and r["method"] == method)


def test_scatter_population_rms_translation_invariance_and_partial_missing():
    points = np.array([[-1, -2, -3], [1, 2, 3], [np.nan, 100, 100]])
    actual = scatter(points)
    assert actual["samples"] == 2
    assert actual["rms_east_m"] == pytest.approx(1)
    assert actual["rms_north_m"] == pytest.approx(2)
    assert actual["rms_up_m"] == pytest.approx(3)
    assert actual["horizontal_rms_m"] == pytest.approx(np.sqrt(5))
    assert actual["rms_3d_m"] == pytest.approx(np.sqrt(14))
    shifted = scatter(points + [100, -50, 20])
    assert shifted == actual


@pytest.mark.parametrize("points", [[], [[1, 2, 3]], [[np.nan, 0, 0]],
                                    [[1, 2, 3], [0, np.inf, 0]]])
def test_insufficient_points_have_null_rms(points):
    result = scatter(points)
    assert result["samples"] < 2
    assert all(value is None for key, value in result.items() if key != "samples")


def test_two_valid_zero_points_have_real_zero_rms():
    result = scatter(np.zeros((2, 3)))
    assert result["samples"] == 2
    assert result["rms_3d_m"] == 0


def test_angular_rms_known_angle_normalizes_rays_and_ignores_missing():
    angle = np.deg2rad(12)
    rays = np.array([[np.sin(angle), 0, np.cos(angle)],
                     [-2 * np.sin(angle), 0, 2 * np.cos(angle)],
                     [np.nan, 0, 1]])
    assert angular_rms(rays) == pytest.approx(12)
    assert angular_rms(rays[2:]) is None
    assert angular_rms(rays[:1]) is None
    assert angular_rms(np.array([[1.0, 0, 0], [-1.0, 0, 0]])) is None


def test_event_rms_does_not_reweight_repeated_grid_matches_and_end_is_excluded():
    data, result = evaluation_data(
        [0, 0.1, 0.2, 0.3, 1, 60],
        [True, False, False, False, True, True],
        [True, True, True, True, True, True],
    )
    for method in METHODS:
        result[method][:, 0] = [0, 0, 0, 0, 10, 1000]
    rows, sensors, _, inside, _ = evaluate_window(data, result, window())
    events = metric(rows, "all_events", "joint")
    grid = metric(rows, "all_grid", "joint")
    assert events["recorded_samples"] == events["samples"] == 2
    assert events["rms_3d_m"] == pytest.approx(5)
    assert grid["samples"] == 5
    assert grid["rms_3d_m"] == pytest.approx(4)
    assert list(inside) == [True, True, True, True, True, False]
    acoustic = next(r for r in sensors if r["sensor"] == "acoustic_range")
    assert acoustic["samples"] == 2


def test_common_events_is_exact_three_way_finite_intersection():
    data, result = evaluation_data(np.arange(7), [True] * 6 + [False], [True] * 7)
    result["joint"][0, 0] = np.nan
    result["acoustic_imu_depth"][1, 1] = np.inf
    result["optical_imu_depth"][2, 2] = np.nan
    # Selected availability must not change the definition of three-way common.
    result["selected"][3] = np.nan
    for method in METHODS[:3]:
        result[method][3:6, 0] = [1, 2, 3]
    rows, _, _, _, common = evaluate_window(data, result, window())
    np.testing.assert_array_equal(common, [False, False, False, True, True, True, False])
    for method in METHODS[:3]:
        actual = metric(rows, "common_events", method)
        assert actual["recorded_samples"] == actual["samples"] == 3
        assert actual["rms_3d_m"] == pytest.approx(np.sqrt(2 / 3))
    assert metric(rows, "common_events", "selected")["samples"] == 2


def test_leading_fourteen_seconds_remain_in_expected_600_and_longest_gap():
    times = np.arange(140, 600) / 10
    data, result = evaluation_data(times, np.zeros(len(times), bool), np.ones(len(times), bool))
    _, sensors, coverage, _, _ = evaluate_window(data, result, window())
    acoustic = next(r for r in sensors if r["sensor"] == "acoustic_range")
    assert acoustic["samples"] == 0
    assert acoustic["centered_rms"] is None
    assert coverage["expected_grid_samples"] == 600
    for method in METHODS:
        assert coverage[f"{method}_samples"] == 460
        assert coverage[f"{method}_percent"] == pytest.approx(460 / 600 * 100)
        assert coverage[f"{method}_longest_gap_s"] == pytest.approx(14)


def test_no_available_points_is_missing_statistic_and_full_window_gap():
    data, result = evaluation_data([20, 30], [True, True], [True, True])
    for value in result.values():
        value[:] = np.nan
    rows, _, coverage, _, common = evaluate_window(data, result, window())
    assert not common.any()
    for method in METHODS:
        assert metric(rows, "all_events", method)["rms_3d_m"] is None
        assert coverage[f"{method}_samples"] == 0
        assert coverage[f"{method}_longest_gap_s"] == 60
    assert longest_grid_gap(np.array([]), np.array([], bool), 0, 60, 10) == 60


def test_epoch_grid_floating_point_drift_does_not_invent_missing_slots():
    start = 1793500000.0
    times = np.arange(start, start + 60, 0.1)
    assert len(times) == 600
    assert longest_grid_gap(times, np.ones(len(times), bool), start, start + 60, 10) == 0
