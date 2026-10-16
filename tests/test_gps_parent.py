from __future__ import annotations

import csv

import numpy as np

from aquabeacon_analysis.gps_parent import (
    classify_gps,
    geodetic_to_enu,
    interpolate_gps_segments,
    produce_parent_gps_position,
)


def test_wgs84_enu_has_expected_axes() -> None:
    lat = np.array([26.0, 26.00001, 26.0])
    lon = np.array([127.0, 127.0, 127.00001])
    alt = np.array([10.0, 10.0, 10.0])
    enu = geodetic_to_enu(lat, lon, alt, 26.0, 127.0, 10.0)
    np.testing.assert_allclose(enu[0], [0, 0, 0], atol=1e-8)
    assert enu[1, 1] > 1 and abs(enu[1, 0]) < 1e-5
    assert enu[2, 0] > 0.9 and abs(enu[2, 1]) < 1e-5


def test_isolated_gps_jump_is_retained_and_not_interpolated() -> None:
    times = np.arange(5, dtype=float)
    enu = np.column_stack([[0, 0.1, 0.2, 3.6, 3.7], np.zeros(5), np.zeros(5)])
    segment, status, speed, jump = classify_gps(
        times, enu, np.full(5, 3), np.full(5, 10)
    )
    np.testing.assert_array_equal(segment, [0, 0, 0, 1, 1])
    assert status[3] == "segment_start_jump"
    assert jump[3] and speed[3] > 3
    xy, frame_segment = interpolate_gps_segments(
        times, enu, segment, np.array([1.5, 2.5, 3.5])
    )
    np.testing.assert_allclose(xy[0], [0.15, 0])
    assert np.isnan(xy[1]).all() and frame_segment[1] == -1
    np.testing.assert_allclose(xy[2], [3.65, 0])


def test_invalid_fix_breaks_interpolation() -> None:
    times = np.arange(4, dtype=float)
    enu = np.zeros((4, 3))
    segment, _, _, _ = classify_gps(
        times, enu, np.array([3, 3, 0, 3]), np.full(4, 10)
    )
    np.testing.assert_array_equal(segment, [0, 0, -1, 1])


def test_gps_position_outputs_keep_source_and_camera_time(tmp_path) -> None:
    source = tmp_path / "G00001.CSV"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["window_time_s", "sync_utc_us", "flags", "fix", "satellites",
                         "lat_e7", "lon_e7", "altitude_mm"])
        for i in range(5):
            writer.writerow([i, 1_000_000 * i, 15, 3, 10, 260000000,
                             1270000000 + i, 10000])
    out = tmp_path / "output"
    meta = produce_parent_gps_position(
        source, out, np.array([0.5, 1.5, 2.5]),
        np.array([500000, 1500000, 2500000]), np.zeros((3, 2))
    )
    assert meta["valid_fixes"] == 5
    with (out / "parent_gps_position.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert rows[0]["sync_utc_us"] == "500000"
    assert rows[0]["status"] == "gps_interpolated"
    assert rows[0]["up_m"] == "nan"
