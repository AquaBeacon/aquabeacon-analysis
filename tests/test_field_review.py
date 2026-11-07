"""Invariant tests for field-review metrics and missing-data handling."""
import numpy as np
import pytest

from aquabeacon_analysis.field_review import (
    centered_scatter,
    directional_scatter,
    observation_coverage,
    valid_bracket_support,
)


def test_centering_is_translation_invariant_not_truth_error():
    p = np.array([[0, 0, -10], [2, 0, -10], [1, 3, -10]])
    assert centered_scatter(p)["rms_3d_m"] == pytest.approx(np.sqrt(8 / 3))
    assert centered_scatter(p + 1000)["rms_3d_m"] == pytest.approx(np.sqrt(8 / 3))


def test_radial_tangential_variances_close_under_rotation():
    p = np.array([[1., 0, -10], [-1, 0, -10], [0, 0, -9], [0, 0, -11]])
    a = directional_scatter(p)
    assert a["radial_rms_m"] == pytest.approx(np.sqrt(0.5))
    assert a["tangential_rms_m"] == pytest.approx(np.sqrt(0.5))
    q = np.array([[0, 1, 0], [0, 0, 1], [1, 0, 0]])
    b = directional_scatter(p @ q)
    assert a["rms_3d_m"] == pytest.approx(b["rms_3d_m"])
    assert a["equivalent_tangent_angle_deg"] == pytest.approx(b["equivalent_tangent_angle_deg"])
    assert a["variance_closure_m2"] < 1e-14


def test_navigation_support_does_not_cross_missing_or_extrapolate():
    times = [0., 0.05, 0.1, 1.0, 1.05]
    result = valid_bracket_support(times, [True, True, False, True, True],
                                  [-0.01, 0.025, 0.05, 0.075, 0.5, 1.025, 1.1])
    assert result.tolist() == [False, True, True, False, False, True, False]


def test_coverage_retains_delayed_ranging_and_rejects_duplicate_shots():
    assert observation_coverage([14, 15, 59], 0, 60)["leading_gap_s"] == 14
    assert observation_coverage([14, 15, 59], 0, 60)["maximum_gap_with_edges_s"] == 44
    with pytest.raises(ValueError):
        observation_coverage([14, 14, 59], 0, 60)
