import numpy as np

from scripts.fuse_relative_position import (
    detect_immersion_reference,
    heldout_optical_check,
    interpolate_series,
    optical_observations,
    pressure_referenced_observations,
    solve_axis,
)


def test_gap_aware_interpolation():
    source_times = np.array([0.0, 1.0, 4.0])
    values = np.array([0.0, 2.0, 8.0])
    result = interpolate_series(source_times, values, np.array([0.5, 2.0, 4.5]), 1.5)
    assert np.isclose(result[0], 1.0)
    assert np.isnan(result[1])
    assert np.isnan(result[2])


def test_smoother_handles_missing_and_outlying_optical_position():
    times = np.arange(0.0, 10.5, 0.5)
    truth = 0.1 * times**2
    observed = truth.copy()
    observed[6] = np.nan
    observed[10] += 8.0
    acceleration = np.full(len(times), 0.2)
    fused = solve_axis(times, observed, acceleration)
    assert abs(fused[6] - truth[6]) < 0.4
    assert abs(fused[10] - truth[10]) < 2.0
    assert np.isfinite(fused).all()


def test_immersion_reference_uses_pre_rise_pressure():
    rows = [{"experiment_time_s": str(t), "pressure_mbar": str(
        1027 + (0.1 if t % 2 else -0.1) + max(0, t - 80) * 3),
        "sync_utc_us": str(1000000 * t)} for t in range(150)]
    reference = detect_immersion_reference(rows)
    assert 82 <= reference["onset_experiment_time_s"] <= 84
    assert np.isclose(reference["surface_pressure_mbar"], 1027, atol=0.1)


def test_optical_position_uses_new_local_light_angles():
    rows = [{"window_time_s": "1", "range_qc": "reported_valid", "range_m": "2"}]
    angles = [{"window_time_s": "1", "angle_status": "conditional_mount_and_fov",
               "local_azimuth_rad": str(np.pi / 2), "gravity_elevation_rad": "0"}]
    _, positions, valid = optical_observations(rows, angles)
    assert valid.tolist() == [True]
    assert np.allclose(positions[0], [0, 2, 0], atol=1e-7)


def test_pressure_depth_takes_vertical_priority_when_tof_conflicts():
    times = np.arange(0.0, 10.5, 0.5)
    optical_up = np.zeros(len(times))
    pressure_up = np.full(len(times), -2.0)
    result = solve_axis(times, optical_up, np.zeros(len(times)),
                        pressure_target=pressure_up, pressure_sigma=0.03)
    assert np.median(abs(result - pressure_up)) < 0.03
    assert np.median(abs(result - optical_up)) > 1.9


def test_display_observation_and_fused_position_share_parent_camera_frame():
    optical = np.array([[1.0, -0.5, 0.4], [np.nan, np.nan, np.nan]])
    result = pressure_referenced_observations(optical, np.array([2.2, 2.3]), 0.03)
    assert np.allclose(result[0], [1.0, -0.5, -2.17])
    assert np.isnan(result[1, :2]).all()
    assert np.isclose(result[1, 2], -2.27)


def test_heldout_check_reports_disagreement_not_accuracy():
    times = np.arange(0.0, 15.0, 0.5)
    position = np.column_stack([times * 0.1, times * 0, times * 0])
    valid = np.ones(len(times), dtype=bool)
    acceleration = np.zeros_like(position)
    depth = np.full(len(times), 0.03)
    result = heldout_optical_check(times, position, valid, acceleration, depth, 0.03)
    assert result["heldout_optical_points"] == 6
    assert result["fused_mean_disagreement_m"] < 0.1
    assert "not ground-truth" in result["interpretation"]
