"""Independently verify field inertial comparison outputs and recorded provenance."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/20261103_field_review_000/child_dead_reckoning.json"
AXES = ("east", "north", "up")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


class HashCache:
    """Read each immutable input/output file at most once in this verification."""

    def __init__(self):
        self.cache = {}

    def sha(self, path):
        path = path.resolve()
        if path not in self.cache:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            self.cache[path] = digest.hexdigest()
        return self.cache[path]


def close(actual, expected):
    np.testing.assert_allclose(actual, expected, rtol=1e-9, atol=1e-7, equal_nan=True)


def vector(rows, prefix, unit="m"):
    return np.array([[float(r[f"{prefix}_{axis}_{unit}"]) for axis in AXES] for r in rows])


def flag(rows, name):
    values = [r[name].lower() for r in rows]
    assert all(v in ("true", "false") for v in values), f"Invalid boolean {name}"
    return np.array([v == "true" for v in values])


def check_stats(values, recorded):
    values = np.asarray(values)
    assert np.isfinite(values).all()
    assert recorded["count"] == len(values)
    if not len(values):
        assert all(recorded[k] is None for k in ("rms_m", "median_m", "p95_m", "max_m"))
    else:
        close(recorded["rms_m"], np.sqrt(np.mean(values**2)))
        close(recorded["median_m"], np.median(values))
        close(recorded["p95_m"], np.percentile(values, 95))
        close(recorded["max_m"], np.max(values))


def numeric_key(mapping, horizon):
    return next(value for key, value in mapping.items() if float(key) == float(horizon))


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in ("src", "href") and value:
                self.links.append(value)


def check_links(path, verification_path):
    parser = Links()
    parser.feed(path.read_text(encoding="utf-8"))
    for value in parser.links:
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        target = (path.parent / unquote(parsed.path)).resolve()
        if target == verification_path.resolve():
            continue  # Written only after every other check is finished.
        assert target.is_file(), f"Missing local link in {path}: {value}"


def check_plot(out, stem, hashes):
    for suffix in (".png", ".svg", ".pdf", ".figure.pickle", ".plot.json"):
        path = out / (stem + suffix)
        assert path.is_file() and path.stat().st_size > 0, f"Missing plot artifact: {path}"
    plot = read_json(out / (stem + ".plot.json"))
    assert plot["master"] == stem + ".figure.pickle"
    assert plot["master_sha256"] == hashes.sha(out / plot["master"])


def check_pooled(out, cfg, summaries, root):
    """Recompute aggregate RMS directly from window rows, weighted per window."""
    pooled = read_csv(out / "pooled_summary.csv")
    groups = {}
    for summary in summaries:
        path = root / "results" / summary["experiment_id"] / "child_dead_reckoning" / cfg["run"]
        for row in read_csv(path / "windows.csv"):
            key = (summary["environment"], float(row["horizon_s"]))
            groups.setdefault(key, []).append((summary["experiment_id"], row))
    expected_keys = {(e, float(h)) for e in ("pool", "sea") for h in cfg["window_horizons_s"]}
    keys = [(r["environment"], float(r["horizon_s"])) for r in pooled]
    assert len(keys) == len(set(keys)) and set(keys) == expected_keys
    for row, key in zip(pooled, keys, strict=True):
        windows = groups.get(key, [])
        assert int(row["windows"]) == len(windows)
        assert int(row["experiments"]) == len({eid for eid, _ in windows})
        for metric in ("horizontal_gap_m", "vertical_gap_m", "horizontal_magnitude_gap_m",
                       "dr_horizontal_displacement_m", "current_horizontal_displacement_m"):
            column = metric + "_rms_m"
            if not windows:
                assert row[column] == ""
            else:
                values = np.array([float(window[metric]) for _, window in windows])
                assert np.isfinite(values).all()
                close(float(row[column]), np.sqrt(np.mean(values**2)))


def check_aggregate_manifest(out, cfg, hashes, root):
    path = out / "manifest.json"
    assert path.is_file(), "Aggregate manifest missing"
    manifest = read_json(path)
    assert manifest["run"] == cfg["run"]
    assert manifest["experiment_id"] == cfg["aggregate_experiment"]
    assert "verification.json" not in manifest["outputs_sha256"], "Verification is a later output"
    for relative, expected in manifest["inputs_sha256"].items():
        source = (root / relative).resolve()
        matches = source.is_file() and hashes.sha(source) == expected
        if not matches:
            snapshot = manifest.get("input_snapshots", {}).get(relative)
            assert snapshot, f"Changed aggregate input without snapshot: {relative}"
            source = (out / snapshot).resolve()
            assert source.is_relative_to(out.resolve())
            assert hashes.sha(source) == expected, f"Changed aggregate snapshot: {relative}"
    for relative, expected in manifest["outputs_sha256"].items():
        output = (out / relative).resolve()
        assert output.is_relative_to(out.resolve())
        assert hashes.sha(output) == expected, f"Changed aggregate output: {relative}"


def verify_trial(root, eid, cfg, hashes, verification_path):
    out = root / "results" / eid / "child_dead_reckoning" / cfg["run"]
    summary, manifest = (read_json(out / f"{name}.json") for name in ("summary", "manifest"))
    assert summary["experiment_id"] == manifest["experiment_id"] == eid
    assert manifest["run"] == cfg["run"] and manifest["config"] == cfg
    input_paths = set()
    for relative, expected in manifest["inputs_sha256"].items():
        path = (root / relative).resolve()
        assert not path.is_relative_to(out.resolve()), "Input and output trees overlap"
        current_matches = path.is_file() and hashes.sha(path) == expected
        if not current_matches:
            snapshot = manifest.get("input_snapshots", {}).get(relative)
            assert snapshot, f"Changed input without recorded snapshot: {relative}"
            snapshot_path = (out / snapshot).resolve()
            assert snapshot_path.is_relative_to(out.resolve()), "Snapshot escapes output tree"
            assert hashes.sha(snapshot_path) == expected, f"Changed input and snapshot: {relative}"
        input_paths.add(path)
    assert input_paths, "No recorded input provenance"
    for relative, expected in manifest["outputs_sha256"].items():
        path = (out / relative).resolve()
        assert path.is_relative_to(out.resolve()) and path not in input_paths
        assert hashes.sha(path) == expected, f"Changed output: {relative}"
    assert {"timeseries.csv", "windows.csv", "summary.json"} <= set(manifest["outputs_sha256"])
    rows, windows = read_csv(out / "timeseries.csv"), read_csv(out / "windows.csv")
    times = np.array([float(r["utc_s"]) for r in rows])
    assert len(times) > 1 and np.isfinite(times).all() and np.all(np.diff(times) > 0)
    grid, available, paired = (flag(rows, k) for k in ("grid_sample", "current_available", "paired"))
    current, anchored = vector(rows, "current"), vector(rows, "dr_depth")
    raw, velocity = vector(rows, "inertial_position"), vector(rows, "inertial_velocity", "m_s")
    acceleration = vector(rows, "acceleration", "m_s2")
    segments = np.array([int(r["segment"]) for r in rows])
    depth = np.array([float(r["depth_m"]) for r in rows])
    assert np.isfinite(current[available]).all(), "Available current has NaNs"
    assert np.all(~available | grid), "Current positions occur outside observed grid"
    np.testing.assert_array_equal(paired, available & np.isfinite(anchored).all(axis=1))
    assert summary["grid_samples"] == int(grid.sum())
    assert summary["current_available_samples"] == int(available.sum())
    assert summary["common_samples"] == int(paired.sum())
    close(summary["current_available_fraction"], available[grid].mean())
    assert summary["dr_depth_available_samples"] == int(
        (grid & np.isfinite(acceleration).all(axis=1) & np.isfinite(depth)).sum())
    close(summary["duration_s"], times[-1] - times[0])
    difference = anchored - current
    close(vector(rows, "difference"), difference)
    check_stats(np.linalg.norm(difference[paired, :2], axis=1),
                summary["full_segment_horizontal_gap"])
    check_stats(difference[paired, 2], summary["full_segment_vertical_gap"])
    for info in summary["segments"]:
        idx = np.flatnonzero(segments == info["segment"])
        assert len(idx) and np.all(np.diff(idx) == 1), "Noncontiguous segment"
        assert np.all(np.diff(times[idx]) <= cfg["integration_max_gap_s"])
        close([info["start_utc_s"], info["end_utc_s"]], times[idx[[0, -1]]])
        candidates = idx[available[idx] & np.isfinite(depth[idx])]
        if not len(candidates):
            assert info["anchor_utc_s"] is None and info["paired_samples"] == 0
            assert np.isnan(anchored[idx]).all()
            continue
        anchor = candidates[0]
        assert info["anchor_utc_s"] == times[anchor]
        np.testing.assert_array_equal(anchored[anchor], current[anchor])
        chosen = idx[idx >= anchor]
        expected = raw[chosen] - raw[anchor] - velocity[anchor] * (times[chosen]-times[anchor])[:, None]
        expected[:, 2] = depth[anchor] - depth[chosen]
        close(anchored[chosen], expected + current[anchor])
        close(vector(rows, "dr_displacement")[chosen], expected)
        assert info["paired_samples"] == int(paired[idx].sum())
    for window in windows:
        start, end = (int(np.searchsorted(times, float(window[k]))) for k in ("start_s", "end_s"))
        assert 0 <= start < end < len(times)
        assert times[start] == float(window["start_s"]) and times[end] == float(window["end_s"])
        assert abs(times[end] - times[start] - float(window["horizon_s"])) <= .026
        assert available[start] and available[end] and np.isfinite(depth[[start, end]]).all()
        assert segments[start] >= 0 and np.all(segments[start:end+1] == segments[start])
        assert int(window["segment"]) == segments[start]
        assert np.isfinite(raw[start:end+1]).all() and np.isfinite(velocity[start:end+1]).all()
        assert np.all(np.diff(times[start:end+1]) <= cfg["integration_max_gap_s"])
        dr = raw[end] - raw[start] - velocity[start] * (times[end] - times[start])
        dr[2] = depth[start] - depth[end]
        cur = current[end] - current[start]
        for axis, a, b in zip(AXES, dr, cur, strict=True):
            close(float(window[f"dr_{axis}_m"]), a)
            close(float(window[f"current_{axis}_m"]), b)
        expected = {"horizontal_gap_m": np.linalg.norm((dr-cur)[:2]),
                    "vertical_gap_m": (dr-cur)[2], "position_gap_m": np.linalg.norm(dr-cur),
                    "dr_horizontal_displacement_m": np.linalg.norm(dr[:2]),
                    "current_horizontal_displacement_m": np.linalg.norm(cur[:2]),
                    "horizontal_magnitude_gap_m": abs(np.linalg.norm(dr[:2])-np.linalg.norm(cur[:2]))}
        for key, value in expected.items():
            close(float(window[key]), value)
    for horizon in cfg["window_horizons_s"]:
        selected = [w for w in windows if float(w["horizon_s"]) == horizon]
        metrics = numeric_key(summary["window_metrics"], horizon)
        for metric, recorded in metrics.items():
            check_stats([float(w[metric]) for w in selected], recorded)
        attempts = numeric_key(summary["window_attempts"], horizon)
        rejections = numeric_key(summary["window_rejections"], horizon)
        assert attempts == len(selected) + sum(rejections.values())
        assert all(isinstance(v, int) and v >= 0 for v in rejections.values())
    assert all(float(w["horizon_s"]) in cfg["window_horizons_s"] for w in windows)
    check_plot(out, "comparison", hashes)
    check_links(out / "report.html", verification_path)
    return {"experiment_id": eid, "status": "pass", "timeseries_rows": len(rows),
            "windows": len(windows), "checks": ["provenance_and_output_hashes", "availability_counts",
            "anchors_and_displacements", "all_window_equations_and_segments", "summary_statistics",
            "editable_plot_artifacts", "html_local_links"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", action="append", required=True)
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args()
    cfg = read_json(args.config)
    if len(set(args.experiment)) != len(args.experiment) or not set(args.experiment) <= set(cfg["experiments"]):
        parser.error("Explicit unique supported experiment IDs required")
    aggregate = ROOT / "results" / cfg["aggregate_experiment"] / "child_dead_reckoning" / cfg["run"]
    verification = aggregate / "verification.json"
    hashes, checks = HashCache(), []
    for eid in args.experiment:
        try:
            checks.append(verify_trial(ROOT, eid, cfg, hashes, verification))
        except (AssertionError, OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
            checks.append({"experiment_id": eid, "status": "fail", "error": f"{type(exc).__name__}: {exc}"})
    try:
        check_plot(aggregate, "horizon_comparison", hashes)
        check_links(aggregate / "report.html", verification)
        summaries = read_json(aggregate / "summary.json")
        assert {s["experiment_id"] for s in summaries} == set(args.experiment)
        for s in summaries:
            path = ROOT / "results" / s["experiment_id"] / "child_dead_reckoning" / cfg["run"] / "summary.json"
            assert s == read_json(path), "Aggregate summary differs from per-experiment summary"
        check_pooled(aggregate, cfg, summaries, ROOT)
        check_aggregate_manifest(aggregate, cfg, hashes, ROOT)
        checks.append({"aggregate": True, "status": "pass",
                       "checks": ["editable_plot_artifacts", "html_local_links", "trial_summaries",
                                  "pooled_window_counts_and_rms", "aggregate_provenance_and_hashes"]})
    except (AssertionError, OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        checks.append({"aggregate": True, "status": "fail", "error": f"{type(exc).__name__}: {exc}"})
    passed = all(c["status"] == "pass" for c in checks)
    result = {"created_utc": datetime.now(UTC).isoformat(), "status": "pass" if passed else "fail",
              "experiments": args.experiment, "checks": checks, "hashed_files": len(hashes.cache)}
    aggregate.mkdir(parents=True, exist_ok=True)
    verification.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                            encoding="utf-8")
    assert verification.is_file()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
