import pytest

from aquabeacon_analysis.interval_selection import group_events, map_anchor, valid_anchors


def test_union_grouping_and_padding_does_not_drop_acoustic_only_event():
    result = group_events([10, 11, 12, 20, 100, 101, float('nan')], 30, 5)
    assert [(r['start_utc_s'], r['end_utc_s']) for r in result] == [(5, 25), (95, 106)]
    assert group_events([]) == []


def test_anchor_expiry_and_clock_reset_are_not_extrapolated():
    anchors = valid_anchors([
        {'utc_anchor_mono_us': 100, 'utc_anchor_epoch_us': 1793420000000000,
         'utc_valid_until_us': 200},
        {'utc_anchor_mono_us': 300, 'utc_anchor_epoch_us': 1793420000000200,
         'utc_valid_until_us': 400}])
    assert map_anchor(99, anchors) is None
    assert map_anchor(200, anchors) is None
    assert map_anchor(250, anchors) is None
    assert map_anchor(100, anchors) == pytest.approx(1793420000)
