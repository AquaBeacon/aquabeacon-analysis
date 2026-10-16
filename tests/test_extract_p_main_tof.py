import csv
import json

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
