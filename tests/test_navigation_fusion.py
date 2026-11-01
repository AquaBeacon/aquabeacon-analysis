import numpy as np

from aquabeacon_analysis.navigation_fusion import event_grid, hampel_positions, inertial_filter


def test_constant_acceleration_dropout_and_large_outlier():
    t = np.arange(0, 12, 0.05)
    a = np.tile([0.2, 0, 0], (len(t), 1))
    truth = np.column_stack([0.1 * t * t, 0 * t, 0 * t])
    p = np.full_like(truth, np.nan)
    p[::20] = truth[::20]
    p[(t > 3) & (t < 7)] = np.nan
    p[160] = truth[160] + [100, 100, 100]
    v = np.full_like(p, np.nan)
    v[0] = 0
    out = inertial_filter(
        t, a, p, np.ones_like(p) * 0.05, velocity=v, velocity_sigma=np.ones_like(p) * 0.05
    )
    assert np.linalg.norm(out["state"][139, :, 0] - truth[139]) < 0.02
    assert not out["position_used"][160].any()
    assert np.linalg.norm(out["state"][-1, :, 0] - truth[-1]) < 0.05


def test_unobserved_drift_is_not_exported_forever():
    t = np.arange(0, 10, 0.05)
    a = np.zeros((len(t), 3))
    p = np.full_like(a, np.nan)
    p[0] = 0
    out = inertial_filter(t, a, p, np.ones_like(p), max_anchor_age=3)
    assert out["available"][50] and not out["available"][100]
    assert out["position_sigma"][100, 0] > out["position_sigma"][20, 0]
    a[100] = np.nan
    out = inertial_filter(t, a, p, np.ones_like(p))
    assert not np.isfinite(out["state"][101:]).any()


def test_event_has_one_update_and_hampel_preserves_motion():
    t = np.arange(0, 4, 0.05)
    grid, indices = event_grid(t, [0.006, 1.012, 2.01], np.ones((3, 3)))
    assert np.isfinite(grid[:, 0]).sum() == 3 and len(set(indices)) == 3
    line = np.column_stack([np.arange(40) * 0.3, np.zeros(40), np.ones(40)])
    line[20, 2] = 50
    flagged = hampel_positions(line)
    assert flagged.sum() == 1 and flagged[20, 2]


def test_reacquisition_starts_a_new_segment_after_expired_anchor():
    t = np.arange(0, 10, 0.05)
    a = np.zeros((len(t), 3))
    p = np.full_like(a, np.nan)
    p[0] = 0
    p[160] = [20, 5, -4]
    result = inertial_filter(t, a, p, np.ones_like(p) * 0.2, max_anchor_age=3)
    assert not result["available"][150]
    assert result["available"][160]
    np.testing.assert_allclose(result["state"][160, :, 0], p[160])
    assert result["segment"][160] != result["segment"][0]
