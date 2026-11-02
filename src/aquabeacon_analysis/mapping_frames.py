"""Rigid conversion of local ENU frames through their WGS84 origins."""

import numpy as np

from .gps_parent import geodetic_to_enu


def enu_basis(origin_llh):
    lat, lon = np.radians(origin_llh[:2])
    return np.array(
        [
            [-np.sin(lon), np.cos(lon), 0],
            [-np.sin(lat) * np.cos(lon), -np.sin(lat) * np.sin(lon), np.cos(lat)],
            [np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)],
        ]
    )


def enu_transform(source_origin, target_origin):
    """Return R,t so p_target = R @ p_source + t, without fitting map points."""
    rotation = enu_basis(target_origin) @ enu_basis(source_origin).T
    shift = geodetic_to_enu(*[np.array([v]) for v in source_origin], *target_origin)[0]
    return rotation, shift
