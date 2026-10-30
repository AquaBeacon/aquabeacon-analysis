"""GNSS binary validity and scatter definitions."""
import struct

import numpy as np
import pytest

from aquabeacon_analysis.ngt_gnss import centered_scatter, crc24q, decode_nav_pvt, ubx_messages


def test_crc24q_check_vector():
    assert crc24q(b"123456789") == 0xCDE703


def test_ubx_checksum_and_truncation():
    body = bytes([1, 7, 2, 0, 12, 15])
    a = b = 0
    for value in body:
        a = (a + value) & 255
        b = (b + a) & 255
    frame = b"\xb5\x62" + body + bytes([a, b])
    assert list(ubx_messages(frame)) == [(1, 7, bytes([12, 15]))]
    with pytest.raises(ValueError, match="checksum"):
        list(ubx_messages(frame[:-1] + bytes([b ^ 1])))
    with pytest.raises(ValueError, match="Truncated"):
        list(ubx_messages(frame[:-1]))


def test_pvt_units_quality_and_time():
    payload = bytearray(92)
    struct.pack_into("<H5B", payload, 4, 2026, 10, 28, 5, 10, 0)
    payload[11] = 3
    payload[20] = 3
    payload[21] = 0x83
    struct.pack_into("<4i", payload, 24, 1350000000, 340000000, 50000, 20000)
    result = decode_nav_pvt(payload)
    assert result["latitude_deg"] == pytest.approx(34)
    assert result["longitude_deg"] == pytest.approx(135)
    assert result["height_ellipsoid_m"] == 50
    assert result["carrier_solution"] == 2
    assert result["utc_valid"] and result["fix_valid"] and result["differential"]
    assert result["utc_s"] == 1793164200


def test_scatter_uses_window_mean_not_origin():
    stats = centered_scatter(np.array([[9, 20, 27], [11, 20, 33]]))
    assert stats["mean_enu_m"] == [10, 20, 30]
    assert stats["rms_horizontal_m"] == 1
    assert stats["rms_3d_m"] == pytest.approx(np.sqrt(10))
    with pytest.raises(ValueError):
        centered_scatter(np.array([[np.nan, 0, 0]]))
