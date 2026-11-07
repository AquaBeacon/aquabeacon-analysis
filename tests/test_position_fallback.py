"""Geometry and causal-state regression tests for depth fallbacks."""

import numpy as np
import pytest

from aquabeacon_analysis.position_fallback import run_fallback


def scene(times=None):
    """Changing rotated offsets; all sensors see the same known child origin."""
    times = np.arange(7.0) if times is None else np.asarray(times, dtype=float)
    n = len(times)
    origin = np.tile([3.0, 4.0, -2.0], (n, 1))
    camera = np.tile([0.1, -0.2, 0.3], (n, 1))
    transmitter = np.tile([-0.2, 0.1, -0.1], (n, 1))
    # These are ENU offsets after the independently supplied IMU rotations.
    angles = np.arange(n) * 0.19
    light = np.c_[0.2 * np.cos(angles), 0.2 * np.sin(angles), 0.1 * np.cos(angles)]
    acoustic = np.c_[0.1 * np.sin(angles), 0.1 * np.cos(angles), 0.2 * np.sin(angles)]
    pressure = np.c_[0.03 * np.cos(angles), 0.04 * np.sin(angles), 0.1 * np.sin(angles)]
    surface_offset = 0.6
    reference = np.linspace(0.05, 0.15, n)
    depths = reference + surface_offset - origin[:, 2] - pressure[:, 2]
    rays = origin + light - camera
    rays *= 2  # Normalization is part of the public API.
    ranges = np.linalg.norm(origin + acoustic - transmitter, axis=1)
    joints = origin.copy()
    joints[3:] = np.nan
    kwargs = {
        "times": times, "joint_positions": joints, "rays": rays, "cameras": camera,
        "transmitters": transmitter, "light_offsets": light, "acoustic_offsets": acoustic,
        "pressure_offsets": pressure, "ranges": ranges, "depths": depths,
        "surface_reference_z": reference,
    }
    return kwargs, origin


def test_optical_geometry_with_current_rotated_offsets_and_missing_range():
    kwargs, expected = scene()
    kwargs["ranges"][3:] = np.nan
    result = run_fallback(**kwargs)
    np.testing.assert_allclose(result["optical_imu_depth"][3:], expected[3:], atol=1e-12)
    np.testing.assert_allclose(result["selected"], expected, atol=1e-12)
    np.testing.assert_allclose(result["surface_offset_m"][3:], 0.6, atol=1e-12)
    assert list(result["method"][3:]) == ["optical_imu_depth"] * 4
    assert result["surface_samples"][2] == 2  # Current joint has not yet seeded.
    assert result["optical_rejection_reason"][2] == "surface_unavailable"


def test_acoustic_geometry_nonzero_offsets_and_independent_held_bearing():
    kwargs, expected = scene()
    # Keep acoustic XY offsets constant to make the held world bearing exact.
    kwargs["acoustic_offsets"][:, :2] = [0.1, 0.2]
    kwargs["ranges"] = np.linalg.norm(
        expected + kwargs["acoustic_offsets"] - kwargs["transmitters"], axis=1,
    )
    kwargs["rays"][3:] = np.nan
    result = run_fallback(**kwargs)
    np.testing.assert_allclose(result["acoustic_imu_depth"][3:6], expected[3:6], atol=1e-12)
    assert list(result["method"][3:6]) == ["acoustic_imu_depth"] * 3
    assert result["bearing_anchor_time_s"][5] == 2
    assert result["bearing_age_s"][5] == 3  # Exact expiry boundary is included.
    assert result["acoustic_rejection_reason"][6] == "bearing_expired"
    assert result["method"][6] == "none"  # Acoustic output cannot refresh itself.
    assert set(result["acoustic_bearing_model"]) == {"held_world_optical_bearing"}


def test_no_prior_and_no_future_leakage():
    kwargs, _ = scene()
    kwargs["joint_positions"][:3] = np.nan
    kwargs["joint_positions"][5] = [3, 4, -2]
    result = run_fallback(**kwargs)
    assert np.isnan(result["selected"][:5]).all()
    assert np.isnan(result["surface_anchor_time_s"][:6]).all()
    assert result["surface_anchor_time_s"][6] == 5
    assert result["method"][5] == "joint"
    assert result["method"][6] == "none"  # Insufficient prior surface samples.


def test_surface_window_relative_to_latest_seed_and_age_boundary():
    kwargs, _ = scene([0, 1, 2, 10, 62, 62.1, 80])
    result = run_fallback(**kwargs)
    assert result["surface_samples"][4] == 3
    assert result["surface_age_s"][4] == 60
    assert result["method"][4] == "optical_imu_depth"
    assert result["optical_rejection_reason"][5] == "surface_unavailable"
    kwargs["joint_positions"][5] = [3, 4, -2]
    result = run_fallback(**kwargs)
    assert result["surface_samples"][6] == 1  # New seed drops obsolete seed burst.
    assert result["surface_anchor_time_s"][6] == 62.1


def test_surface_uses_median_and_only_previous_joints():
    kwargs, _ = scene()
    kwargs["joint_positions"][0, 2] += 50
    result = run_fallback(**kwargs)
    assert result["surface_offset_m"][3] == pytest.approx(0.6)
    assert result["surface_anchor_time_s"][3] == 2


@pytest.mark.parametrize(("ray", "reason"), [
    ([1, 0, 0], "near_horizontal_ray"),
    ([1, 0, 0.001], "near_horizontal_ray"),
    ([0, 0, 1], "backward_ray"),
    ([0, 0, 0], "invalid_ray"),
    ([np.nan, 0, 1], "invalid_optical_geometry"),
])
def test_invalid_optical_geometry(ray, reason):
    kwargs, _ = scene()
    kwargs["rays"][3] = ray
    result = run_fallback(**kwargs)
    assert np.isnan(result["optical_imu_depth"][3]).all()
    assert result["optical_rejection_reason"][3] == reason


def test_impossible_sphere_and_distance_limit():
    kwargs, _ = scene()
    kwargs["ranges"][3] = 0.1
    result = run_fallback(**kwargs, config={"max_optical_distance_m": 1})
    assert result["acoustic_rejection_reason"][3] == "impossible_sphere_depth"
    assert result["optical_rejection_reason"][3] == "optical_distance_exceeded"
    assert result["method"][3] == "none"


def test_joints_preserved_with_missing_depth_and_geometry():
    kwargs, expected = scene()
    kwargs["joint_positions"][:] = expected
    for key in ("depths", "rays", "pressure_offsets", "ranges"):
        kwargs[key][:] = np.nan
    result = run_fallback(**kwargs)
    np.testing.assert_array_equal(result["selected"], expected)
    assert set(result["method"]) == {"joint"}
    assert set(result["optical_rejection_reason"]) == {"invalid_depth"}


def test_optical_refreshes_bearing_but_current_bearing_is_not_used():
    kwargs, _ = scene()
    # Remove acoustic anchor geometry only on all previous joint rows.
    kwargs["transmitters"][:3] = np.nan
    kwargs["rays"][4:] = np.nan
    result = run_fallback(**kwargs)
    assert result["method"][3] == "optical_imu_depth"
    assert result["acoustic_rejection_reason"][3] == "bearing_unavailable"
    assert result["bearing_anchor_time_s"][4] == 3
    assert result["method"][4] == "acoustic_imu_depth"


@pytest.mark.parametrize("times", [[0, 0, 1], [0, 2, 1], [0, np.nan, 2]])
def test_invalid_time(times):
    kwargs, _ = scene(times)
    with pytest.raises(ValueError, match="strictly increasing"):
        run_fallback(**kwargs)


def test_input_shape_and_config_validation():
    kwargs, _ = scene()
    kwargs["rays"] = np.zeros((7, 2))
    with pytest.raises(ValueError, match="rays must have shape"):
        run_fallback(**kwargs)
    kwargs, _ = scene()
    for config in ({"unknown": 0}, {"min_surface_samples": 0},
                   {"surface_window_s": -1}, {"max_bearing_age_s": np.nan}):
        with pytest.raises(ValueError):
            run_fallback(**kwargs, config=config)


def test_empty_input_supported():
    kwargs, _ = scene([])
    result = run_fallback(**kwargs)
    assert result["selected"].shape == (0, 3)


def test_surface_seed_mask_counts_only_independent_events():
    kwargs, expected = scene()
    kwargs["joint_positions"][:6] = expected[:6]
    mask = np.array([True, False, True, False, True, False, False])
    result = run_fallback(**kwargs, surface_seed_mask=mask)
    np.testing.assert_array_equal(result["surface_samples"], [0, 1, 1, 2, 2, 3, 3])
    assert result["optical_rejection_reason"][4] == "surface_unavailable"
    assert result["method"][6] == "optical_imu_depth"
    assert result["surface_anchor_time_s"][6] == 4
    assert result["bearing_anchor_time_s"][6] == 5
    with pytest.raises(ValueError, match="boolean array"):
        run_fallback(**kwargs, surface_seed_mask=np.ones(7))
