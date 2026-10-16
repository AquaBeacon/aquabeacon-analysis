from __future__ import annotations

from pathlib import Path

import pytest

from scripts.prepare_orbslam3 import export, image_plan, imu_records


def test_image_plan_uses_explicit_video_origin_and_refined_utc() -> None:
    rows = [
        {"camera_frame": "101", "refined_sync_utc_us": "1000001"},
        {"camera_frame": "102", "refined_sync_utc_us": "1033334"},
    ]
    assert image_plan(rows, 100, 3) == [(1, 1000001000), (2, 1033334000)]


def test_image_plan_rejects_duplicate_camera_frame() -> None:
    rows = [
        {"camera_frame": "101", "refined_sync_utc_us": "1000001"},
        {"camera_frame": "101", "refined_sync_utc_us": "1033334"},
    ]
    with pytest.raises(ValueError, match="strictly"):
        image_plan(rows, 100, 3)


def test_imu_order_matches_euroc_columns() -> None:
    rows = [{"sync_utc_us": "123", "gx": "1", "gy": "2", "gz": "3",
             "ax": "4", "ay": "5", "az": "6"}]
    assert imu_records(rows) == [(123000, 1, 2, 3, 4, 5, 6)]
    assert imu_records(rows, -25_000) == [(98000, 1, 2, 3, 4, 5, 6)]


def test_imu_reversal_is_sorted_without_dropping_measurements() -> None:
    base = {"gx": "1", "gy": "2", "gz": "3", "ax": "4", "ay": "5", "az": "6"}
    rows = [{**base, "sync_utc_us": "200"}, {**base, "sync_utc_us": "100"}]
    assert [record[0] for record in imu_records(rows)] == [100000, 200000]


def test_full_export_requires_frame_alignment_evidence(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="alignment-note"):
        export(tmp_path, tmp_path / "out", 100)
