"""Verifier checks for provenance caching, link resolution, and numeric summaries."""
import csv
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify_field_dead_reckoning.py"
SPEC = importlib.util.spec_from_file_location("verify_field_dead_reckoning", SCRIPT)
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def test_hash_cache_reads_repeated_input_once(tmp_path):
    path = tmp_path / "source.csv"
    path.write_text("observed", encoding="utf-8")
    hashes = verifier.HashCache()
    expected = hashes.sha(path)
    assert hashes.sha(path) == expected
    assert len(hashes.cache) == 1
    assert expected == hashlib.sha256(b"observed").hexdigest()


def test_stats_rejects_wrong_rms_and_accepts_signed_vertical_values():
    record = {"count": 2, "rms_m": 5, "median_m": 0, "p95_m": 4.5, "max_m": 5}
    verifier.check_stats([-5, 5], record)
    with pytest.raises(AssertionError):
        verifier.check_stats([-5, 5], {**record, "rms_m": 0})
    verifier.check_stats([], {"count": 0, "rms_m": None, "median_m": None,
                              "p95_m": None, "max_m": None})


def test_html_links_check_images_and_defer_verification_file(tmp_path):
    report = tmp_path / "report.html"
    verification = tmp_path / "verification.json"
    image = tmp_path / "figure space.svg"
    image.write_text("<svg/>", encoding="utf-8")
    report.write_text('<img src="figure%20space.svg"><a href="verification.json">check</a>'
                      '<a href="https://example.org">external</a>', encoding="utf-8")
    verifier.check_links(report, verification)
    image.unlink()
    with pytest.raises(AssertionError, match="Missing local link"):
        verifier.check_links(report, verification)


def test_editable_plot_requires_matching_master_and_all_formats(tmp_path):
    for suffix in (".png", ".svg", ".pdf", ".figure.pickle"):
        (tmp_path / ("figure" + suffix)).write_bytes(b"test artifact")
    hashes = verifier.HashCache()
    plot = {"master": "figure.figure.pickle",
            "master_sha256": hashes.sha(tmp_path / "figure.figure.pickle")}
    (tmp_path / "figure.plot.json").write_text(json.dumps(plot), encoding="utf-8")
    verifier.check_plot(tmp_path, "figure", hashes)
    plot["master_sha256"] = "bad"
    (tmp_path / "figure.plot.json").write_text(json.dumps(plot), encoding="utf-8")
    with pytest.raises(AssertionError):
        verifier.check_plot(tmp_path, "figure", hashes)
    (tmp_path / "figure.pdf").unlink()
    with pytest.raises(AssertionError, match="Missing plot artifact"):
        verifier.check_plot(tmp_path, "figure", hashes)


def test_numeric_horizon_keys_accept_json_float_strings():
    assert verifier.numeric_key({"5.0": {"count": 12}}, 5) == {"count": 12}


def test_pooled_rms_weights_windows_and_rejects_trial_average(tmp_path):
    metrics = ("horizontal_gap_m", "vertical_gap_m", "horizontal_magnitude_gap_m",
               "dr_horizontal_displacement_m", "current_horizontal_displacement_m")
    cfg = {"run": "test", "window_horizons_s": [5]}
    summaries = [{"experiment_id": "one", "environment": "sea"},
                 {"experiment_id": "two", "environment": "sea"}]
    for eid, values in (("one", [0, 0]), ("two", [3])):
        directory = tmp_path / "results" / eid / "child_dead_reckoning" / "test"
        directory.mkdir(parents=True)
        with (directory / "windows.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["horizon_s", *metrics])
            writer.writeheader()
            writer.writerows({"horizon_s": 5, **dict.fromkeys(metrics, v)} for v in values)
    out = tmp_path / "aggregate"
    out.mkdir()
    columns = ["environment", "horizon_s", "windows", "experiments",
               *[m + "_rms_m" for m in metrics]]

    def write_pooled(sea_rms):
        with (out / "pooled_summary.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerow({"environment": "pool", "horizon_s": 5, "windows": 0,
                             "experiments": 0, **dict.fromkeys(columns[4:], "")})
            writer.writerow({"environment": "sea", "horizon_s": 5, "windows": 3,
                             "experiments": 2, **dict.fromkeys(columns[4:], sea_rms)})

    write_pooled(3**.5)
    verifier.check_pooled(out, cfg, summaries, tmp_path)
    write_pooled(1.5)
    with pytest.raises(AssertionError):
        verifier.check_pooled(out, cfg, summaries, tmp_path)
