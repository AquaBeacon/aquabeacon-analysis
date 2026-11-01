"""Water occupancy and ranging are independent required conditions."""
from aquabeacon_analysis.interval_selection import intersect_activity


def interval(a, b):
    return {'start_utc_s': a, 'end_utc_s': b}


def test_both_conditions_and_restart_runs():
    result = intersect_activity([interval(20, 100)],
                                [interval(0, 10), interval(15, 50), interval(70, 120)])
    assert [(r['start_utc_s'], r['end_utc_s']) for r in result] == [(20, 50), (70, 100)]
    assert result[0]['parent_ranging_start_utc_s'] == 15
    assert result[1]['immersion_start_utc_s'] == 20


def test_air_only_and_touching_intervals_not_selected():
    assert intersect_activity([interval(20, 40)], [interval(0, 20), interval(40, 80)]) == []
