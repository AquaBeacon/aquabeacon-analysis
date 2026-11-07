import numpy as np
import pytest

from aquabeacon_analysis.mapping_diagnostics import (
    compare_priors,
    temporal_connectivity,
    zero_cut_intervals,
)


def test_prior_identity_distinguishes_missing_from_coordinate_changes():
    a = np.array([[0., 0., 0.], [np.nan]*3, [1., 2., 3.]])
    assert compare_priors(a, a.copy())["identical"]
    b = a.copy(); b[2, 0] += .01
    assert compare_priors(a, b)["changed_coordinates"] == 1
    b[1] = 0
    assert compare_priors(a, b)["availability_changes"] == 1


def test_skip_pair_bridges_missing_adjacent_match():
    graph = temporal_connectivity(5, [(0, 2), (2, 3), (2, 0)])
    assert graph["crossing"].tolist() == [1, 1, 1, 0]
    assert graph["component_sizes"] == [3, 1, 1]
    assert graph["degree"].tolist() == [1, 0, 2, 1, 0]
    assert zero_cut_intervals(graph["crossing"], [10, 11, 12, 13, 14]) == [
        {"first_cut": 3, "last_cut": 3, "start_s": 13., "end_s": 14., "duration_s": 1.}]


def test_gap_at_start_middle_and_end():
    gaps = zero_cut_intervals([0, 0, 2, 0, 3, 0], np.arange(7)*.1)
    assert [r["first_cut"] for r in gaps] == [0, 3, 5]
    assert [r["last_cut"] for r in gaps] == [1, 3, 5]


def test_invalid_geometry_inputs_fail():
    with pytest.raises(ValueError):
        temporal_connectivity(2, [(0, 2)])
    with pytest.raises(ValueError):
        zero_cut_intervals([0], [1., 1.])
    with pytest.raises(ValueError):
        compare_priors(np.zeros((2, 2)), np.zeros((2, 2)))
