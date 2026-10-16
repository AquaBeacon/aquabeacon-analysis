from __future__ import annotations

import numpy as np

from aquabeacon_analysis.sync_analysis import estimate_time_offset


def test_estimate_time_offset_recovers_camera_timestamp_shift() -> None:
    times = np.arange(0.0, 120.0, 0.01)
    signal = (
        np.sin(times * 1.7)
        + 0.4 * np.sin(times * 4.3)
        + np.exp(-((times - 50.0) / 0.6) ** 2) * 3
    )
    camera_timestamp_error_s = 0.42
    result = estimate_time_offset(
        times,
        signal,
        times + camera_timestamp_error_s,
        signal,
        max_lag_s=1.0,
        sample_rate_hz=50.0,
    )
    assert abs(result[0] + camera_timestamp_error_s) < 0.03
    assert result[1] > 0.98
