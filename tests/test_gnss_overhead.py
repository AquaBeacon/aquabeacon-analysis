import numpy as np
import pytest

from aquabeacon_analysis.gnss_overhead import horizontal_metrics, nearest_pairs


def test_rms_bias_scatter_decomposition():
    metrics = horizontal_metrics([[2, 1], [4, 1]])
    assert metrics['horizontal_rms_m'] == pytest.approx(np.sqrt(11))
    assert metrics['horizontal_scatter_m'] == pytest.approx(1)
    assert metrics['mean_east_north_m'] == [3, 1]


def test_invalid_nearest_is_not_replaced_and_gaps_not_bridged():
    pairs = nearest_pairs([0, 1, 2, 5], [.01, 1.01, 2.4, 5.01],
                          [True]*4, [True, False, True, True])
    assert pairs.tolist() == [[0, 0], [3, 3]]


def test_pair_is_unique_and_tie_earlier():
    assert nearest_pairs([1, 1.1], [.9, 1.2], [True]*2, [True]*2, .25).tolist() == [[0, 0], [1, 1]]
    assert nearest_pairs([], [], [], []).shape == (0, 2)
    with pytest.raises(ValueError):
        nearest_pairs([1, 1], [1], [True]*2, [True])


def test_no_implicit_nonfinite_filter():
    with pytest.raises(ValueError):
        horizontal_metrics([[np.nan, 1]])
    assert horizontal_metrics([])['count'] == 0
