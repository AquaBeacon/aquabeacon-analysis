import csv
import json

import pytest

from scripts.apply_orbslam3_pressure_depth import apply, pressure_at


def test_pressure_interpolation_and_gap_rejection() -> None:
    values = [(1_000_000_000, 0.0), (1_500_000_000, 2.0)]
    assert pressure_at(values, 1_250_000_000) == 1.0
    assert pressure_at(values, 1_500_000_000) == 2.0
    assert pressure_at(values, 900_000_000) is None
    assert pressure_at(values, 1_600_000_000) is None
    assert pressure_at([(0, 0.0), (2_000_000_000, 2.0)], 1_000_000_000) is None


def test_pressure_correction_keeps_missing_frame_and_original_z(tmp_path) -> None:
    dataset = tmp_path / "experiment"
    pressure_dir = dataset / "mav0/pressure0"
    pressure_dir.mkdir(parents=True)
    (pressure_dir / "data.csv").write_text(
        "utc_ns,pressure_mbar,relative_depth_m\n"
        "1000000000,1000,0\n2000000000,1100,2\n", encoding="utf-8")
    (dataset / "timestamps.txt").write_text(
        "1000000000\n1500000000\n2000000000\n", encoding="utf-8")
    (dataset / "export_manifest.json").write_text(
        json.dumps({"test_only": True}), encoding="utf-8")
    trajectory = tmp_path / "CameraTrajectory.txt"
    trajectory.write_text(
        "1000000000 1 2 3 0 0 0 1\n1500000000 4 5 6 0 0 0 1\n",
        encoding="utf-8")
    settings = tmp_path / "settings.yaml"
    settings.write_text("provisional", encoding="utf-8")
    result = apply(dataset, trajectory, settings, tmp_path / "output")
    with (tmp_path / "output/pressure_corrected_trajectory.csv").open(
            encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert result["missing_or_unlocalized_frames"] == 1
    assert rows[1]["slam_z_m"] == "6.0"
    assert rows[1]["corrected_z_m"] == "2.0"


def test_pressure_correction_rejects_uninitialized_slam(tmp_path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    trajectory = run / "CameraTrajectory.txt"
    trajectory.write_text("", encoding="utf-8")
    (run / "run_manifest.json").write_text(
        json.dumps({"usable_vi_trajectory": False}), encoding="utf-8")
    with pytest.raises(ValueError, match="usable inertial"):
        apply(tmp_path / "dataset", trajectory, tmp_path / "settings", tmp_path / "output")
