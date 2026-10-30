import csv
import json

import pytest

from scripts.extract_p_main_tof import extract, utc_us


def test_p_main_utc_anchor_and_c_main_overlap(tmp_path):
    raw = tmp_path / "raw"
    processed = tmp_path / "processed"
    results = tmp_path / "results"
    raw.mkdir()
    source = raw / "P_MAIN_HEALTH_RANGE_00024.csv"
    with source.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=[
            "mono_us", "utc_anchor_mono_us", "utc_anchor_epoch_us",
            "utc_valid_until_us", "tof_us", "range_state",
        ])
        writer.writeheader()
        for i, tof in enumerate([0, 1, 1, 1, 1, 1, 0]):
            writer.writerow({"mono_us": i * 1_000_000,
                             "utc_anchor_mono_us": i * 1_000_000,
                             "utc_anchor_epoch_us": 1_790_000_000_000_000 + i * 1_000_000,
                             "utc_valid_until_us": i * 1_000_000 + 2_000_000,
                             "tof_us": tof, "range_state": 3 if tof else 1})
    c_manifest = results / "20260922_cmain_022" / "preprocessing" / "manifest.json"
    c_manifest.parent.mkdir(parents=True)
    c_manifest.write_text(json.dumps({
        "experiment_id": "20260922_cmain_022",
        "window_start_utc_us": 1_790_000_002_000_000,
        "window_end_utc_us_exclusive": 1_790_000_005_000_000,
    }), encoding="utf-8")
    assert utc_us({"mono_us": "12", "utc_anchor_mono_us": "10",
                   "utc_anchor_epoch_us": "100", "utc_valid_until_us": "20"}) == 102
    manifest_path, = extract(raw, processed, results, tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["outputs"][0]["rows"] == 5
    assert manifest["outputs"][0]["valid_tof_rows"] == 5
    assert manifest["c_main_overlaps"][0]["valid_tof_rows"] == 3
    overlap = processed / "20260922_cmain_022" / "tof_window" / "p_main" / source.name
    with overlap.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["window_time_s"] for row in rows] == ["0.000000", "1.000000", "2.000000"]


def test_registered_extraction_uses_only_selected_source_and_experiment(tmp_path):
    raw = tmp_path / "data/raw/20261027_pool_001/P_MAIN"
    raw.mkdir(parents=True)
    for suffix in ("00022", "00023"):
        with (raw / f"P_MAIN_HEALTH_RANGE_{suffix}.csv").open(
            "w", newline="", encoding="utf-8"
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=[
                "mono_us", "utc_anchor_mono_us", "utc_anchor_epoch_us",
                "utc_valid_until_us", "tof_us", "range_state",
            ])
            writer.writeheader()
            for i in range(7):
                writer.writerow({"mono_us": i * 1_000_000, "utc_anchor_mono_us": 0,
                                 "utc_anchor_epoch_us": 1_790_000_000_000_000,
                                 "utc_valid_until_us": 10_000_000,
                                 "tof_us": 1 if 1 <= i <= 5 else 0,
                                 "range_state": 3})
    results = tmp_path / "results"
    identifier = "20261027_pool_001"
    selected = results / identifier / "preprocessing/manifest.json"
    selected.parent.mkdir(parents=True)
    selected.write_text(json.dumps({"experiment_id": identifier,
                                    "window_start_utc_us": 1_790_000_002_000_000,
                                    "window_end_utc_us_exclusive": 1_790_000_005_000_000}),
                        encoding="utf-8")
    other = results / "20260922_cmain_022/preprocessing/manifest.json"
    other.parent.mkdir(parents=True)
    other.write_text(selected.read_text(encoding="utf-8"), encoding="utf-8")
    originals = {path: path.read_bytes() for path in (selected, other)}
    manifests = extract(raw, tmp_path / "data/processed", results, tmp_path,
                        experiment_id=identifier, source_suffix="00022", c_main_manifest=selected)
    assert manifests == [results / identifier / "p_main_preprocessing/manifest.json"]
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["experiment_id"] == identifier
    assert manifest["outputs"][0]["rows"] == 3
    assert manifest["c_main_overlaps"] == []
    assert {path: path.read_bytes() for path in (selected, other)} == originals
    files = list((tmp_path / "data/processed").rglob("*.csv"))
    assert len(files) == 1
    assert files[0].name == "P_MAIN_HEALTH_RANGE_00022.csv"
    assert files[0].is_relative_to(tmp_path / "data/processed" / identifier)


def test_registered_extraction_requires_actual_parent_source_number(tmp_path):
    with pytest.raises(ValueError, match="p_main_suffix"):
        extract(tmp_path, tmp_path / "processed", tmp_path / "results", tmp_path,
                experiment_id="20261027_pool_001")
    assert list(tmp_path.iterdir()) == []
