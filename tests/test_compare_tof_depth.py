import pytest

from scripts.compare_tof_depth import (
    find_parent_source,
    match_shots,
    medwin_sound_speed,
    pressure_depth_m,
)


def test_medwin_temperature_salinity_and_depth_terms():
    base = medwin_sound_speed(30, 35, 0)
    assert base == pytest.approx(1545.53, abs=0.01)
    assert medwin_sound_speed(30, 36, 0) - base == pytest.approx(1.04)
    assert medwin_sound_speed(30, 35, 10) - base == pytest.approx(0.16)


def test_pressure_depth_uses_absolute_pressure_offset():
    assert pressure_depth_m(1013.25, 1013.25, 1025) == 0
    assert pressure_depth_m(1113.25, 1013.25, 1025) == pytest.approx(0.994845, abs=1e-6)


def test_match_shots_requires_shot_and_nearby_utc():
    child = [{"shot_seq": "7", "sync_utc_us": "1000000"}]
    parent = [
        {"shot_seq": "7", "sync_utc_us": "5000000"},
        {"shot_seq": "8", "sync_utc_us": "1000000"},
        {"shot_seq": "7", "sync_utc_us": "900000"},
    ]
    matched = match_shots(child, parent)
    assert matched["7"] is parent[2]


def test_parent_source_is_selected_by_matching_shots_not_file_number(tmp_path):
    import csv

    root = tmp_path / "processed"
    child = [{"shot_seq": str(i), "sync_utc_us": str(i * 1_000_000)}
             for i in range(100, 105)]
    for name, start in [("022", 10), ("024", 100)]:
        source = (root / f"20260922_pmain_{name}" / "tof_window" / "p_main"
                  / f"P_MAIN_HEALTH_RANGE_00{name}.csv")
        source.parent.mkdir(parents=True)
        with source.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["shot_seq", "sync_utc_us"])
            writer.writeheader()
            writer.writerows({"shot_seq": str(i), "sync_utc_us": str(i * 1_000_000)}
                             for i in range(start, start + 5))
    selected, matches = find_parent_source(child, root)
    assert selected.name == "P_MAIN_HEALTH_RANGE_00024.csv"
    assert len(matches) == 5
