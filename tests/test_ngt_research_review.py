import numpy as np

from aquabeacon_analysis.ngt_research_review import (
    clockwise_display_to_sensor,
    position_state,
    sample_cell_vertices,
    sensor_rays_to_clockwise_display,
    state_intervals,
)


def test_intervals_keep_failed_cells_and_tail():
    runs = state_intervals([10, 10.2, 10.4, 10.6], [True, False, False, True], 10.8)
    assert [r['samples'] for r in runs] == [1, 2, 1]
    np.testing.assert_allclose([r['duration_s'] for r in runs], [.2, .4, .2])
    gap = state_intervals([0, .2, 2], ['ok']*3, 2.2, .25)
    assert len(gap) == 2 and gap[0]['end_s'] == .45


def test_all_failure_combinations_remain_distinct():
    row = {'magnetic_position_available':'False', 'optical_available':'False',
        'range_formula_available':'False', 'magnetic_attitude_available':'True'}
    assert position_state(row) == 'optical+acoustic'
    row['optical_available'] = 'True'
    assert position_state(row) == 'acoustic'


def test_timeline_includes_isolated_and_final_success_cells():
    x, valid = sample_cell_vertices([0, 1, 2], [True, False, True], 3)
    np.testing.assert_array_equal(x, [0, 1, 1, 2, 2, 3])
    np.testing.assert_array_equal(valid, [True, True, False, False, True, True])


def test_clockwise_conversion_matches_image_index_rotation():
    image = np.arange(3*5).reshape(3, 5)
    display = np.rot90(image, k=-1)
    for yd in range(display.shape[0]):
        for xd in range(display.shape[1]):
            xs, ys = clockwise_display_to_sensor([xd, yd], 3).astype(int)
            assert image[ys, xs] == display[yd, xd]
    np.testing.assert_array_equal(sensor_rays_to_clockwise_display([[1, 2, 3]]), [[-2, 1, 3]])
