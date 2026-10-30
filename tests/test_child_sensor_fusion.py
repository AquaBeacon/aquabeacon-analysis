import numpy as np
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.child_sensor_fusion import (
    fit_vector_rotation,
    inertial_pressure_filter,
    pressure_scale,
)


def test_rotation_and_depth_scale():
    rng = np.random.default_rng(41)
    source = rng.normal(size=(200, 3))
    expected = Rotation.from_euler('xyz', [.2, -.7, 1.])
    actual, quality = fit_vector_rotation(source, expected.apply(source))
    assert np.allclose(actual, expected.as_matrix())
    assert quality['rms_rad_s'] < 1e-10
    t = np.arange(100.)
    result = pressure_scale(t*.01, t*.03+.4, t)
    assert result['accepted']
    assert np.isclose(result['scale_m_per_slam_unit'], 3.)
    assert not pressure_scale(t*.01, -t*.03, t)['accepted']


def test_depth_updates_do_not_claim_horizontal_observability():
    t = np.arange(0, 20, .05)
    a = np.tile([.1, 0., .1], (len(t), 1))
    depth = np.full(len(t), np.nan)
    depth[::20] = 0
    raw, fused, _, segments, _ = inertial_pressure_filter(t, a, depth)
    assert raw[-1, 2] > 19
    assert abs(fused[-1, 2]) < .05
    assert np.allclose(raw[:, 0], fused[:, 0])
    assert np.unique(segments).tolist() == [0]


def test_missing_inertial_data_resets_without_bridging():
    t = np.arange(0, 2, .05)
    a = np.ones((len(t), 3))
    a[10] = np.nan
    _, fused, _, segment, _ = inertial_pressure_filter(t, a, np.full(len(t), np.nan))
    assert np.isnan(fused[10]).all()
    assert segment[10] == -1 and segment[11] == 1
    assert np.allclose(fused[11], 0)


def test_visual_velocity_update_constrains_horizontal_drift():
    t = np.arange(0, 20, .05)
    a = np.tile([.1, 0., 0.], (len(t), 1))
    obs = np.full(len(t), np.nan)
    visual = np.full_like(a, np.nan)
    visual[::10] = 0.
    raw, fused, _, _, accepted = inertial_pressure_filter(t, a, obs, visual_velocity=visual)
    assert accepted.sum() == 40
    assert raw[-1, 0] > 19
    assert abs(fused[-1, 0]) < .5


def test_flat_depth_does_not_resolve_monocular_scale():
    t = np.arange(50.)
    result = pressure_scale(t*.01, np.full(50, .3), t)
    assert not result['accepted']
