"""WGS84 ENU inversion and Web Mercator display, with explicit track gaps."""
import numpy as np

from .gps_parent import WGS84_A_M, WGS84_E2


def enu_to_geodetic(enu, origin):
    """Invert the repository's WGS84 ENU transform; return lat, lon, height."""
    phi, lam = np.radians(origin[:2])
    radius = WGS84_A_M / np.sqrt(1 - WGS84_E2 * np.sin(phi)**2)
    xyz0 = np.array([(radius + origin[2])*np.cos(phi)*np.cos(lam),
                     (radius + origin[2])*np.cos(phi)*np.sin(lam),
                     (radius*(1-WGS84_E2) + origin[2])*np.sin(phi)])
    rotation = np.array([[-np.sin(lam), np.cos(lam), 0],
                         [-np.sin(phi)*np.cos(lam), -np.sin(phi)*np.sin(lam), np.cos(phi)],
                         [np.cos(phi)*np.cos(lam), np.cos(phi)*np.sin(lam), np.sin(phi)]])
    x, y, z = (np.asarray(enu) @ rotation + xyz0).T
    p = np.hypot(x, y)
    lat = np.arctan2(z, p*(1-WGS84_E2))
    for _ in range(8):
        n = WGS84_A_M / np.sqrt(1-WGS84_E2*np.sin(lat)**2)
        lat = np.arctan2(z + WGS84_E2*n*np.sin(lat), p)
    height = p/np.cos(lat) - WGS84_A_M/np.sqrt(1-WGS84_E2*np.sin(lat)**2)
    return np.column_stack([np.degrees(lat), np.degrees(np.arctan2(y, x)), height])


def mercator(latitude, longitude):
    """EPSG:3857 display coordinates (not ground-distance metres)."""
    return np.column_stack([WGS84_A_M*np.radians(longitude),
                            WGS84_A_M*np.arcsinh(np.tan(np.radians(latitude)))])


def broken_track(times, xy, valid, reset=None, max_gap_s=3):
    """Keep samples; insert NaN before a reset/gap and mask invalid samples."""
    output = []
    for i, point in enumerate(xy):
        if i and (times[i]-times[i-1] > max_gap_s or
                  (reset is not None and reset[i])):
            output.append([np.nan, np.nan])
        output.append(point if valid[i] else [np.nan, np.nan])
    return np.asarray(output)
