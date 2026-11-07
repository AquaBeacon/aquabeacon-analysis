import numpy as np
import pytest

from aquabeacon_analysis.multipath import burst_offsets_s, envelope_events, max_bins


def test_three_transmitted_bursts_are_not_reflections():
    np.testing.assert_allclose(burst_offsets_s(200000, 1, 4600, 8200), [0, 0.004628, 0.012856])
    np.testing.assert_equal(burst_offsets_s(200000, 0, 0, 0), [0])


def test_event_grouping_retains_isolated_late_transients():
    t = np.arange(60000) * 0.00005
    y = np.full(len(t), 24.0)
    y[0:3] = 100
    y[300:303] = 80
    y[20000] = 90
    a = envelope_events(t, y)
    assert a["baseline_adc"] == 24
    assert a["threshold_adc"] == 44
    assert len(a["events"]) == 3
    assert a["events"][-1]["peak_s"] == 1
    assert a["events"][-1]["above_threshold_samples"] == 1
    assert len(envelope_events(t, y, threshold_adc=80)["events"]) == 0


def test_bins_do_not_hide_gaps_or_reduce_peaks():
    x, y = max_bins([0, 0.0005, 0.002], [10, 90, 20])
    np.testing.assert_allclose(x, [0.0005, 0.0015, 0.0025])
    np.testing.assert_allclose(y, [90, np.nan, 20], equal_nan=True)


def test_reversed_times_rejected():
    with pytest.raises(ValueError):
        envelope_events([1, 0], [10, 20])
