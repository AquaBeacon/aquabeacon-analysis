"""Pool adapter invariants independent of the private experiment files."""
import numpy as np
import pytest

from scripts.position_fallback_ngt import (
    ROOT,
    RUN,
    combined_timeline,
    load_ngt,
    normalize_observations,
    numbers,
    read_csv,
)


def test_event_union_preserves_invalid_events_without_grid_duplication():
    t, grid, event = combined_timeline([0, .2, .4], [.1, .2, .2, .3])
    np.testing.assert_allclose(t, [0, .1, .2, .3, .4])
    assert grid.tolist() == [True, False, True, False, True]
    assert event.tolist() == [False, True, True, True, False]
    with pytest.raises(ValueError):
        combined_timeline([0], [np.nan])


def test_optical_ray_survives_range_outage_and_missing_imu_masks_geometry():
    rows = []
    for time, imu_ok in [(0., True), (.1, False), (.2, True)]:
        rows.append({"utc_s": 1000 + time, "parent_video_s": time,
                     "light_x_px": 0., "light_y_px": 0., "optical_available": True,
                     "range_formula_available": False, "range_m": np.nan,
                     "pressure_depth_m": 1., "parent_east_m": 4., "parent_north_m": 5.,
                     "parent_up_m": 6., "child_east_declination0_m": np.nan,
                     "child_north_declination0_m": np.nan, "child_up_approx_m": np.nan,
                     "relative_magnetic_east_m": np.nan,
                     "relative_magnetic_north_m": np.nan, "relative_down_m": np.nan})
    attitude = [{"parent_video_time_s": time, "magnetic_ned_qw": 1.,
                 "magnetic_ned_qx": 0., "magnetic_ned_qy": 0., "magnetic_ned_qz": 0.,
                 "absolute_attitude_available": str(valid), "clock_valid": "True"}
                for time, valid in [(0., True), (.1, False), (.2, True)]]
    cfg = {"attitude_max_bracket_s": .25, "optical_to_camera_imu_rotation": np.eye(3)}
    result = normalize_observations(rows, np.array([0., .1, .2]), [True]*3,
                                    [False, True, False], attitude, cfg, np.eye(3),
                                    np.zeros(5), np.eye(3))
    np.testing.assert_allclose(result["rays"][[0, 2]], [[0, 0, -1], [0, 0, -1]])
    assert np.isnan(result["rays"][1]).all()
    assert np.isnan(result["ranges"]).all()
    assert np.isnan(result["joint_positions"]).all()
    np.testing.assert_allclose(result["parent_origin"], [[4, 5, 6]] * 3)
    np.testing.assert_allclose(result["camera_rays"], [[0, 0, 1]] * 3)
    for key in ("cameras", "transmitters", "light_offsets", "acoustic_offsets", "pressure_offsets"):
        assert np.isfinite(result[key][[0, 2]]).all()
        assert np.isnan(result[key][1]).all()
        np.testing.assert_allclose(result[key][[0, 2]], 0)
    assert result["records"][1]["event_sample"]
    assert not result["records"][1]["range_available"]


def test_unrelated_experiment_cannot_inherit_pool_calibration():
    with pytest.raises(ValueError, match="Explicit corrected pool trial"):
        load_ngt("20261031_nanyo_001")


def test_private_corrected_grid_matches_relative_baseline_when_available():
    eid = "20261028_ngt_002"
    source = ROOT / f"results/{eid}/position_evaluation/{RUN}/positions_5hz.csv"
    if not source.exists():
        pytest.skip("Private corrected pool inputs are not present")
    result = load_ngt(eid)
    baseline = numbers(read_csv(source), ["relative_magnetic_east_m",
                                         "relative_magnetic_north_m", "relative_down_m"])
    baseline[:, 2] *= -1
    grid = [r["grid_sample"] for r in result["records"]]
    np.testing.assert_allclose(result["joint_positions"][grid], baseline,
                               atol=1e-10, equal_nan=True)
