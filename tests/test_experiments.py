import csv
import hashlib
import json
from pathlib import Path

import pytest

from aquabeacon_analysis import time_sync
from aquabeacon_analysis.__main__ import main
from aquabeacon_analysis.experiments import (
    describe_experiment,
    experiment_context,
    initialize_experiment,
    load_experiment,
    repository_path,
    select_experiment_directories,
)


def test_initialize_preserves_raw_and_existing_experiments(tmp_path):
    source = tmp_path / "data/raw/existing.csv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original sensor data\n")
    first = initialize_experiment(tmp_path, "20261027_pool_001", "22")
    second = initialize_experiment(tmp_path, "20261027_pool_002", "22")
    assert first.metadata["c_main_suffix"] == "00022"
    assert first.metadata["calibration_paths"] == []
    assert first.stage_path("processed") != second.stage_path("processed")
    assert list(source.parent.iterdir()) == [source]
    assert source.read_bytes() == b"original sensor data\n"
    assert not first.raw_root.exists()
    assert first.stage_path("results").is_dir()
    before = first.metadata_path.read_bytes()
    with pytest.raises(FileExistsError):
        initialize_experiment(tmp_path, first.experiment_id)
    assert first.metadata_path.read_bytes() == before


@pytest.mark.parametrize("identifier", ["../other", "20261301_pool_001", "20261027_Pool_001",
                                         "20261027_pool/other", "20261027_pool_001."])
def test_invalid_ids_do_not_create_anything(tmp_path, identifier):
    with pytest.raises(ValueError):
        initialize_experiment(tmp_path, identifier)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("reference", ["../outside", "/absolute", "C:/outside", "data\\raw"])
def test_registry_paths_cannot_escape_repository(tmp_path, reference):
    with pytest.raises(ValueError):
        repository_path(tmp_path, reference)


def test_initializer_refuses_to_adopt_existing_outputs(tmp_path):
    identifier = "20261027_pool_001"
    output = tmp_path / "results" / identifier / "old.txt"
    output.parent.mkdir(parents=True)
    output.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        initialize_experiment(tmp_path, identifier)
    assert not (tmp_path / "experiments").exists()
    assert output.read_text(encoding="utf-8") == "keep"


def test_selector_skips_tool_workspaces_and_parent_only_runs(tmp_path):
    identifier = "20260922_cmain_023"
    for name, subfolder in [(identifier, "tof_window/c_main"),
                            ("orbslam3_full", identifier),
                            ("20260922_pmain_023", "tof_window/p_main"),
                            ("_inspection", "tof_window/c_main")]:
        (tmp_path / name / subfolder).mkdir(parents=True)
    assert select_experiment_directories(tmp_path, None, "tof_window/c_main") == [tmp_path / identifier]
    with pytest.raises(FileNotFoundError):
        select_experiment_directories(tmp_path, [identifier, "20261027_pool_001"],
                                      "tof_window/c_main")


def test_selector_requires_full_id_for_reused_log_numbers(tmp_path):
    identifiers = ["20260922_cmain_023", "20261027_pool_023"]
    for identifier in identifiers:
        (tmp_path / identifier / "c_main").mkdir(parents=True)
    with pytest.raises(ValueError, match="ambiguous"):
        select_experiment_directories(tmp_path, ["023"], "c_main")
    assert select_experiment_directories(tmp_path, [identifiers[1]], "c_main") == [
        tmp_path / identifiers[1]]


def test_registered_missing_sources_are_reported_without_scanning_raw(tmp_path):
    experiment = initialize_experiment(tmp_path, "20261027_pool_001")
    details = describe_experiment(experiment)
    assert not details["paths"]["raw"]["exists"]
    assert all(not entry["exists"] for entry in details["reference_status"]["raw_sources"])


def test_context_tracks_explicit_references_and_rejects_missing_calibration(tmp_path):
    experiment = initialize_experiment(tmp_path, "20261027_pool_001")
    reference = tmp_path / "calibration/20261027_pool_001/camera.json"
    reference.parent.mkdir(parents=True)
    reference.write_text('{"fx": 123}', encoding="utf-8")
    metadata = {**experiment.metadata, "calibration_paths": [reference.relative_to(tmp_path).as_posix()]}
    experiment.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    registered = load_experiment(tmp_path, experiment.experiment_id)
    context = experiment_context(registered, "detect-lights")
    assert context["registered_references_sha256"]["calibration_paths"] == {
        "calibration/20261027_pool_001/camera.json": hashlib.sha256(reference.read_bytes()).hexdigest()}
    assert context["metadata_snapshot"]["experiment_id"] == experiment.experiment_id
    reference.unlink()
    with pytest.raises(FileNotFoundError, match="registered reference"):
        experiment_context(registered, "detect-lights")


@pytest.mark.parametrize("command", ["sync-raw", "analyze-sync", "preprocess-tof", "localize-imu",
                                     "detect-lights", "track-lights"])
def test_analysis_cli_requires_an_explicit_scope(tmp_path, monkeypatch, command):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as error:
        main([command])
    assert error.value.code == 2
    assert list(tmp_path.iterdir()) == []


def test_registered_experiment_cannot_override_roots_or_select_other_sessions(tmp_path, monkeypatch):
    experiment = initialize_experiment(tmp_path, "20261027_pool_001", "22")
    monkeypatch.chdir(tmp_path)
    for extras in (["--results-root", "results/other"], ["--session", "023"]):
        with pytest.raises(SystemExit) as error:
            main(["sync-raw", "--experiment", experiment.experiment_id, *extras])
        assert error.value.code == 2
    assert list(experiment.stage_path("results").iterdir()) == []


def test_cli_refuses_output_inside_raw_before_processing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as error:
        main(["preprocess-tof", "--all-experiments", "--processed-root", "data/raw/output"])
    assert error.value.code == 2
    assert not (tmp_path / "data").exists()


def _write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_sync_cli_uses_registered_id_and_source_number_without_touching_other_run(tmp_path, monkeypatch):
    experiment = initialize_experiment(tmp_path, "20261027_pool_001", "22")
    other = initialize_experiment(tmp_path, "20261027_pool_002", "23")
    sentinel = other.stage_path("results") / "old.txt"
    sentinel.write_text("keep", encoding="utf-8")
    start = 1_792_972_800_000_000
    for number in ("00022", "00023"):
        _write_csv(experiment.raw_root / "C_MAIN" / f"C_MAIN_IMU_100Hz_{number}.csv",
                   ["utc_us"], [{"utc_us": start}, {"utc_us": start + 1_000_000}])
    _write_csv(experiment.raw_root / "Spresense/G00001.CSV",
               ["received_mono_us", "utc_s", "nav_usec"],
               [{"received_mono_us": mono, "utc_s": (start + mono) // 1_000_000,
                 "nav_usec": 0} for mono in (0, 1_000_000)])
    _write_csv(experiment.raw_root / "Spresense/I00001.CSV", ["received_mono_us"],
               [{"received_mono_us": mono} for mono in (0, 1_000_000)])
    original_files = {path: path.read_bytes() for path in experiment.raw_root.rglob("*")
                      if path.is_file()}
    monkeypatch.setattr(time_sync, "resolve_ffmpeg", lambda explicit: Path("unused-ffmpeg"))
    monkeypatch.setattr(time_sync, "_camera_outputs", lambda *args: ([], []))
    monkeypatch.chdir(tmp_path)
    assert main(["sync-raw", "--experiment", experiment.experiment_id, "--skip-video"]) == 0
    report = experiment.stage_path("results") / "synchronization"
    manifest = json.loads((report / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["experiment_id"] == experiment.experiment_id
    assert manifest["c_main_suffix"] == "00022"
    assert (report / "experiment_context_sync_raw.json").is_file()
    assert (experiment.stage_path("interim") / "c_main/C_MAIN_IMU_100Hz_00022.csv").is_file()
    assert not (experiment.stage_path("interim") / "c_main/C_MAIN_IMU_100Hz_00023.csv").exists()
    assert list(other.stage_path("results").iterdir()) == [sentinel]
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert {path: path.read_bytes() for path in experiment.raw_root.rglob("*")
            if path.is_file()} == original_files


def test_sync_does_not_silently_process_a_subset_of_requested_logs(tmp_path):
    _write_csv(tmp_path / "C_MAIN_IMU_100Hz_00022.csv", ["utc_us"], [{"utc_us": 1_790_000_000_000_000}])
    with pytest.raises(FileNotFoundError, match="00023"):
        time_sync.discover_sessions(tmp_path, ["22", "23"])
