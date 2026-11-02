import numpy as np

from aquabeacon_analysis.gps_parent import geodetic_to_enu
from aquabeacon_analysis.mapping_frames import enu_transform


def test_enu_conversion_agrees_with_independent_geodetic_coordinates():
    a = [32.96, 132.50, 32.0]
    b = [32.965, 132.505, 41.0]
    lat, lon, height = (
        np.array([32.961, 32.963]),
        np.array([132.502, 132.509]),
        np.array([15.0, 23.0]),
    )
    source = geodetic_to_enu(lat, lon, height, *a)
    expected = geodetic_to_enu(lat, lon, height, *b)
    r, t = enu_transform(a, b)
    np.testing.assert_allclose(source @ r.T + t, expected, atol=1e-8)
    ri, ti = enu_transform(b, a)
    np.testing.assert_allclose((source @ r.T + t) @ ri.T + ti, source, atol=1e-8)
    np.testing.assert_allclose(np.linalg.det(r), 1, atol=1e-12)
