from __future__ import annotations

import csv

from aquabeacon_analysis.time_sync import ClockFit, _session_bounds, fit_spresense_clock


def test_spresense_clock_fit_recovers_linear_mapping() -> None:
    rows = [
        {
            "received_mono_us": str(mono),
            "utc_s": str((1_790_000_000_000_000 + mono) // 1_000_000),
            "nav_usec": str((1_790_000_000_000_000 + mono) % 1_000_000),
        }
        for mono in (1_000_000, 2_000_000, 3_000_000, 4_000_000)
    ]
    fit = fit_spresense_clock(rows)
    assert fit.slope == 1.0
    assert fit.intercept_us == 1_790_000_000_000_000
    assert fit.residual_rms_us == 0.0


def test_clock_fit_inverse_round_trip() -> None:
    fit = ClockFit(intercept_us=1_000_000.0, slope=1.000002, residual_rms_us=5.0, points=4)
    mono_us = 4_567_890
    assert abs(fit.mono_us(fit.utc_us(mono_us)) - mono_us) <= 1


def test_session_bounds_ignore_invalid_zero_utc(tmp_path) -> None:
    path = tmp_path / "imu.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["utc_us"])
        writer.writeheader()
        writer.writerows([{"utc_us": 0}, {"utc_us": 100}, {"utc_us": 250}])
    assert _session_bounds(path) == (100, 250)
