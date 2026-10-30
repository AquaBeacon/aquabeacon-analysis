import numpy as np

from aquabeacon_analysis.ngt_visualization import contiguous_segments, surface_display


def test_missing_or_unrecorded_time_splits_lines_without_losing_valid_endpoints():
    xyz = [[0, 0, 0], [1, 0, 0], [np.nan]*3, [3, 0, 0], [4, 0, 0]]
    assert contiguous_segments(xyz, [0, .2, .4, .6, 2.0]) == [[0, 1], [3], [4]]


def test_display_preserves_horizontal_offset_range_and_raw_heights():
    parent = np.array([[10., 20., 7.], [11., 21., 8.], [12., 22., 9.]])
    child = np.array([[10.3, 20.4, 6.], [np.nan]*3, [12.3, 22.4, 8.]])
    before = parent.copy()
    p, c, origin = surface_display(parent, child, [1., np.nan, 1.])
    np.testing.assert_allclose(parent, before)
    np.testing.assert_allclose(origin, [10, 20, 7])
    np.testing.assert_allclose(p[:, 2], 0)
    np.testing.assert_allclose(c[[0, 2]]-p[[0, 2]], [[.3, .4, -1.]]*2)
    assert np.isnan(c[1]).all()
