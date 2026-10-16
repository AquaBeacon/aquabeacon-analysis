from aquabeacon_analysis.tof_preprocess import tof_window


def test_tof_window_ignores_startup_blips_and_keeps_internal_gaps():
    states = [0, 1, 0, 1, 0, 1, 1, 1, 1, 1, 0, 0, 1, 0]
    rows = [
        {"sync_utc_us": str(i * 1_000_000), "tof_us": str(value),
         "range_state": "3" if value else "1"}
        for i, value in enumerate(states)
    ]
    assert tof_window(rows) == (5_000_000, 13_000_000)


def test_tof_window_requires_deactivation():
    rows = [{"sync_utc_us": str(i), "tof_us": "1", "range_state": "3"}
            for i in range(5)]
    try:
        tof_window(rows)
    except ValueError as exc:
        assert "never becomes inactive" in str(exc)
    else:
        raise AssertionError("expected ValueError")
