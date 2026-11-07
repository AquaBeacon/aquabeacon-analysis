"""Analytic and missing-data checks for independent inertial displacement."""

import numpy as np
import pytest

from aquabeacon_analysis.dead_reckoning_comparison import (
    bounded_sample,
    integrate_segments,
    window_comparisons,
)


def test_constant_acceleration_matches_analytic_solution():
    times = np.arange(201) * .05
    acceleration = np.tile([2., -1., .5], (len(times), 1))
    result = integrate_segments(times, acceleration)
    np.testing.assert_allclose(result["position_m"], .5 * times[:, None]**2 * acceleration)
    np.testing.assert_allclose(result["velocity_m_s"], times[:, None] * acceleration)
    assert np.all(result["segment"] == 0)


def test_gravity_free_stationary_acceleration_stays_stationary():
    result = integrate_segments(np.arange(100) * .05, np.zeros((100, 3)))
    np.testing.assert_array_equal(result["position_m"], 0)
    np.testing.assert_array_equal(result["velocity_m_s"], 0)


def test_gap_and_nan_restart_at_zero_without_bridge():
    times = [0, .05, .1, .15, .5, .55]
    acceleration = np.ones((6, 3))
    acceleration[2] = np.nan
    result = integrate_segments(times, acceleration)
    np.testing.assert_array_equal(result["segment"], [0, 0, -1, 1, 2, 2])
    assert np.isnan(result["position_m"][2]).all()
    np.testing.assert_array_equal(result["position_m"][[0, 3, 4]], 0)
    np.testing.assert_allclose(result["position_m"][5], .5 * .05**2)
    np.testing.assert_allclose(result["velocity_m_s"][5], .05)


@pytest.mark.parametrize("times", [[0, 0], [1, 0], [0, np.nan], [0, np.inf]])
def test_time_validation_rejects_instead_of_sorting(times):
    with pytest.raises(ValueError, match="strictly increasing"):
        integrate_segments(times, np.zeros((2, 3)))
    with pytest.raises(ValueError, match="strictly increasing"):
        bounded_sample(times, [0, 1], [0], 1)


def test_bounded_sample_preserves_exact_gap_edges_and_rejects_bad_brackets():
    times = [0, 1, 2, 3, 10]
    sampled = bounded_sample(
        times, [0, 2, np.nan, 6, 20], [-1, 0, .5, 1, 1.5, 2, 2.5, 3, 6, 10, 11], 1
    )
    np.testing.assert_allclose(
        sampled, [np.nan, 0, 1, 2, np.nan, np.nan, np.nan, 6, np.nan, 20, np.nan],
        equal_nan=True,
    )


def test_bounded_sample_vector_and_scalar_query_shapes():
    values = [[0, 2], [2, 4], [4, np.nan]]
    np.testing.assert_allclose(bounded_sample([0, 1, 2], values, .5, 1), [1, 3])
    assert bounded_sample([0, 1, 2], values, [1.5, 2], 1).shape == (2, 2)
    assert np.isnan(bounded_sample([0, 1, 2], values, [1.5, 2], 1)).all()
    assert bounded_sample([], [], [0, 1], 1).shape == (2,)


def _window_inputs():
    times = np.arange(401) * .05
    dr = integrate_segments(times, np.tile([2., 0., 3.], (len(times), 1)))
    depth = 5 + .1 * times
    # At horizon 5, a new zero-velocity inertial window travels 25 m east.
    # Current position is translated, with no effect on displacement comparison.
    current = np.column_stack((100 + 5 * times, 200 + times * 0, -depth))
    return times, dr, depth, current, np.ones(len(times), dtype=bool)


def test_window_restarts_velocity_and_uses_depth_up_without_pose_fitting():
    times, dr, depth, current, valid = _window_inputs()
    result = window_comparisons(
        times, dr["position_m"], dr["velocity_m_s"], dr["segment"], depth, current, valid,
        horizons_s=(5,),
    )
    assert result["attempts"] == {5: 5}
    assert result["rejections"] == {5: {"endpoint_unavailable": 1}}
    assert len(result["rows"]) == 4
    for row in result["rows"]:
        assert row["dr_east_m"] == pytest.approx(25)
        assert row["dr_up_m"] == pytest.approx(-.5)
        assert row["horizontal_gap_m"] == pytest.approx(0, abs=1e-10)
        assert row["position_gap_m"] == pytest.approx(0, abs=1e-10)


def test_windows_reject_segment_crossing_and_missing_current_endpoint():
    times, dr, depth, current, valid = _window_inputs()
    segments = dr["segment"].copy()
    segments[40:60] = 1
    valid[200] = False
    result = window_comparisons(
        times, dr["position_m"], dr["velocity_m_s"], segments, depth, current, valid,
        horizons_s=(5,),
    )
    assert result["rejections"][5]["dr_segment_discontinuity"] == 1
    assert result["rejections"][5]["current_endpoint_invalid"] == 1
    assert all(row["start_s"] != 0 for row in result["rows"])
    assert result["attempts"][5] == len(result["rows"]) + sum(result["rejections"][5].values())


def test_window_endpoint_nearest_grid_does_not_interpolate_current():
    times, dr, depth, current, valid = _window_inputs()
    result = window_comparisons(
        times, dr["position_m"], dr["velocity_m_s"], dr["segment"], depth, current, valid,
        horizons_s=(5.02,),
    )
    assert result["rows"][0]["end_s"] == pytest.approx(5)
    assert result["rows"][0]["current_east_m"] == pytest.approx(25)


def test_windows_require_finite_depth_endpoints():
    times, dr, depth, current, valid = _window_inputs()
    depth[100] = np.nan
    result = window_comparisons(
        times, dr["position_m"], dr["velocity_m_s"], dr["segment"], depth, current, valid,
        horizons_s=(5,),
    )
    assert result["rejections"][5]["depth_endpoint_invalid"] == 1
