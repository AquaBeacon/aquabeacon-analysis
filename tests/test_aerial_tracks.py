import numpy as np

from aquabeacon_analysis.aerial_tracks import broken_track, enu_to_geodetic, mercator
from aquabeacon_analysis.gps_parent import geodetic_to_enu


def test_enu_inverse_matches_wgs84_input():
    origin = [34.67077, 135.1432, 50.198]
    llh = np.array([origin, [34.671, 135.1436, 63], [34.6704, 135.142, 30]])
    enu = geodetic_to_enu(*llh.T, *origin)
    np.testing.assert_allclose(enu_to_geodetic(enu, origin), llh, atol=1e-7, rtol=0)
    xy = mercator(llh[:, 0], llh[:, 1])
    assert (xy[1] > xy[0]).all()


def test_gap_reset_and_invalid_never_connect():
    track = broken_track([0, 1, 7, 8, 9], np.arange(10).reshape(5, 2),
                         [True, False, True, True, True], [False]*4+[True])
    assert len(track) == 7
    assert np.isnan(track[[1, 2, 5]]).all()
    np.testing.assert_equal(track[[0, 3, 4, 6]], [[0, 1], [4, 5], [6, 7], [8, 9]])
