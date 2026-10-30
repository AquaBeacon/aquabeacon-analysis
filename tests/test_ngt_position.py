import numpy as np

from aquabeacon_analysis.ngt_position import (
    bracket_interpolate,
    calibrated_rays,
    centered_scatter,
    missing_intervals,
    nearest_indices,
    trajectory_metrics,
)


def test_ray_center_and_inverse_distortion():
    import cv2
    k = np.array([[670., 0, 640], [0, 670, 360], [0, 0, 1]])
    d = np.array([.36, .35, -.006, .002, 0])
    truth = np.array([[0., 0, 1], [.5, -.3, 1]])
    pixels, _ = cv2.projectPoints(truth, np.zeros(3), np.zeros(3), k, d)
    got = calibrated_rays(pixels[:, 0], k, d)
    np.testing.assert_allclose(got, truth/np.linalg.norm(truth, axis=1)[:, None], atol=1e-8)


def test_nearest_keeps_invalid_middle_event_and_rejects_old():
    ix, dt = nearest_indices([1., 2., 3.], [1.95, 4.], .6)
    np.testing.assert_array_equal(ix, [1, -1])
    assert abs(dt[0]-.05) < 1e-10


def test_no_interpolation_across_missing_rows_or_long_gap():
    got = bracket_interpolate([0., 1., 2., 5.], [[0.], [np.nan], [2.], [5.]],
                              [-1., .5, 1.5, 2., 3., 5.], 1.5)[:, 0]
    assert np.isnan(got[[0, 1, 2, 4]]).all()
    np.testing.assert_array_equal(got[[3, 5]], [2, 5])


def test_scatter_is_translation_invariant_and_not_true_error():
    points = np.array([[0., 0, 0], [2, 0, 0]])
    assert centered_scatter(points+1000)["centered_rms_3d_m"] == 1


def test_missing_boundary_cells_and_no_outage_polyline():
    gaps = missing_intervals(np.arange(5), [False, True, False, False, False], 5, 1)
    assert gaps == [{"start_s": 0., "end_s": 1., "duration_s": 1.},
                    {"start_s": 2., "end_s": 5., "duration_s": 3.}]
    m = trajectory_metrics([0, 1, 2, 3], [[0, 0], [1, 0], [np.nan, np.nan], [100, 0]])
    assert m["observed_polyline_length_m"] == 1
    assert m["first_to_last_displacement_m"] == 100
