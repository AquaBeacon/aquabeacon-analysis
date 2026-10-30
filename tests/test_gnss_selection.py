import numpy as np
import pytest

from aquabeacon_analysis.gnss_selection import paired_epochs, quality_rank, select_positions


def sample(t=0, carrier=1, quality=2, valid=True):
    return {'utc_s': t, 'parent_video_s': t, 'east_m': t, 'north_m': 0, 'up_m': 0,
            'latitude_deg': 34, 'longitude_deg': 135, 'height_ellipsoid_m': 10,
            'fix_valid': str(valid), 'utc_valid': 'True', 'fix_type': 3,
            'carrier_solution': carrier, 'differential': 'True', 'quality': quality}


@pytest.mark.parametrize('carrier,quality,expected', [
    (2, 2, 'RTK'), (1, 1, 'PPK'), (2, 1, 'PPK'), (1, 2, 'PPK'),
    (1, 4, 'RTK'), (0, 5, 'RTK'),
])
def test_quality_priority(carrier, quality, expected):
    assert select_positions([sample(carrier=carrier)], [sample(quality=quality)])[0][
        'selected_source'] == expected


def test_invalid_fixed_never_wins_and_no_solution_is_nan():
    assert select_positions([sample(carrier=2, valid=False)], [sample()])[0][
        'selected_source'] == 'PPK'
    row = select_positions([sample(valid=False)], [sample(quality=0)])[0]
    assert row['selected_source'] == 'None' and np.isnan(row['east_m'])
    assert quality_rank(sample(carrier=3), 'RTK') == 0
    bad = sample(); bad['east_m'] = np.nan
    assert quality_rank(bad, 'RTK') == 0


def test_epoch_union_preserves_missing_and_ppk_only():
    rows = select_positions([sample(0), sample(1), sample(3)],
                            [sample(.001), sample(2), sample(3.03)])
    assert len(rows) == 5
    assert rows[0]['match_delta_s'] == .001
    assert rows[1]['ppk_status'] == 'Missing'
    assert rows[2]['rtk_status'] == 'Missing'
    assert rows[2]['selected_utc_s'] == 2
    assert rows[1]['source_switch']
    assert rows[1]['switch_step_horizontal_m'] == pytest.approx(.999)


def test_unsorted_and_duplicate_times_rejected():
    for times in [[2, 1], [1, 1]]:
        with pytest.raises(ValueError):
            list(paired_epochs([sample(t) for t in times], []))
