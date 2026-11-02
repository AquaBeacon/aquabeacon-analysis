import numpy as np
import pytest

from aquabeacon_analysis.stationary_gnss import sample_gnss, variance_budget


def test_gnss_alignment_does_not_bridge_bad_epochs_or_extrapolate():
    t = np.array([0.0, 1.0, 2.0, 5.0, 6.0])
    p = np.column_stack([t, 2 * t, -t])
    q = np.array([-0.1, 0, 0.5, 1, 1.5, 2, 3, 5, 5.5, 6, 6.1])
    s = sample_gnss(t, p, [True, False, True, True, True], q)
    expected = np.array([False, True, False, False, False, True, False, True, True, True, False])
    np.testing.assert_array_equal(s["available"], expected)
    np.testing.assert_allclose(s["linear"][expected], np.column_stack([q, 2 * q, -q])[expected])
    assert np.isnan(s["linear"][~expected]).all()
    # Exact good epochs remain usable adjacent to a quality-invalid neighbour.
    assert s["left_index"][1] == s["right_index"][1] == 0
    assert s["left_index"][5] == s["right_index"][5] == 2


def test_nearest_gnss_is_uninterpolated_and_uses_native_clock():
    s = sample_gnss([0, 1, 3], [[0, 0, 0], [2, 0, 0], [8, 0, 0]], [True] * 3, [0.25, 0.75, 2.0])
    np.testing.assert_allclose(s["linear"][:2, 0], [0.5, 1.5])
    np.testing.assert_allclose(s["nearest"][:2, 0], [0, 2])
    assert not s["nearest_available"][2]
    assert not s["available"][2]
    with pytest.raises(ValueError):
        sample_gnss([0, 0], np.zeros((2, 3)), [True, True], [0])


def test_absolute_variance_can_cancel_even_with_varying_gnss():
    g = np.array([[-1, 0, 0], [0, 0, 0], [1, 0, 0]], float)
    s = -g + [5, 9, -3]
    b = variance_budget(g, s)
    assert b["absolute_variance_3d_m2"] == pytest.approx(0)
    assert b["gnss_variance_3d_m2"] == pytest.approx(2 / 3)
    assert b["twice_covariance_3d_m2"] == pytest.approx(-4 / 3)
    assert b["closure_max_abs_m2"] < 1e-12
    shifted = variance_budget(g + [100, 200, 300], s)
    assert shifted["absolute_variance_3d_m2"] == pytest.approx(0)
