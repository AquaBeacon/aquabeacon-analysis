"""Independently verify saved fallback diagnostics, metrics and editable figures."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from aquabeacon_analysis.editable_plots import PlotDocument

ROOT = Path(__file__).resolve().parents[1]
REVIEW = "20261103_field_review_000"
METHODS = ("joint", "acoustic_imu_depth", "optical_imu_depth", "selected")
AXES = ("east", "north", "up")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def values(rows, fields):
    return np.array([[float(row.get(k) or "nan") for k in fields] for row in rows])


def flag(rows, name):
    return np.array([r[name] == "True" for r in rows])


def finite(x):
    return np.isfinite(x).all(axis=1)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def compare(actual, expected):
    if expected is None:
        assert actual in (None, ""), (actual, expected)
    elif isinstance(expected, str):
        assert actual == expected, (actual, expected)
    else:
        np.testing.assert_allclose(float(actual), expected, rtol=1e-9, atol=1e-10)


def rms(points):
    p = points[finite(points)]
    result = {"samples": len(p)}
    keys = ("rms_3d_m", "horizontal_rms_m", "rms_east_m", "rms_north_m", "rms_up_m")
    if len(p) < 2:
        return {**result, **dict.fromkeys(keys)}
    variance = np.sum((p - np.mean(p, axis=0)) ** 2, axis=0) / len(p)
    return {**result, **dict(zip(keys, np.sqrt([variance.sum(), variance[:2].sum(), *variance])))}


def angular_rms(rays):
    rays = rays[finite(rays)]
    if len(rays) < 2:
        return None
    unit = rays / np.linalg.norm(rays, axis=1)[:, None]
    center = unit.mean(axis=0)
    if np.linalg.norm(center) < 1e-10:
        return None
    center /= np.linalg.norm(center)
    angle = np.arctan2(np.linalg.norm(np.cross(unit, center), axis=1), unit @ center)
    return float(np.rad2deg(np.sqrt(np.mean(angle ** 2))))


def verify_hashes(manifest):
    for name, expected in manifest["inputs_sha256"].items():
        path = ROOT / name.replace("\\", "/")
        assert path.is_file(), str(path)
        assert sha(path) == expected, f"Input changed: {path}"


def verify_links(folder):
    for suffix in ("md", "html"):
        path = folder / f"report.{suffix}"
        text = path.read_text(encoding="utf-8")
        links = re.findall(r"\]\(([^)]+)\)", text) if suffix == "md" else re.findall(r'(?:href|src)=[\"\x27]([^\"\x27]+)', text)
        for link in links:
            parsed = urlsplit(html.unescape(link))
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = (folder / unquote(parsed.path)).resolve()
            assert target.exists(), f"Broken {path}: {link}"


def verify_trial(eid, aggregate):
    folder = ROOT / f"results/{eid}/position_fallback"
    manifest = read_json(folder / "manifest.json")
    verify_hashes(manifest)
    rows = read_csv(folder / "positions.csv")
    times = values(rows, ["utc_s"])[:, 0]
    assert np.isfinite(times).all() and np.all(np.diff(times) > 0), eid
    grid, event = flag(rows, "grid_sample"), flag(rows, "event_sample")
    assert len(np.unique(times[event])) == event.sum()
    assert read_csv(ROOT / f"data/processed/{eid}/position_fallback/positions.csv") == [r for r, ok in zip(rows, grid) if ok]
    assert read_csv(folder / "positions_at_acoustic_events.csv") == [r for r, ok in zip(rows, event) if ok]
    positions = {m: values(rows, [f"{m}_{a}_m" for a in AXES]) for m in METHODS}
    valid = {m: finite(p) for m, p in positions.items()}
    summary = manifest["availability"]
    compare(summary["grid_samples"], int(grid.sum()))
    compare(summary["recorded_events"], int(event.sum()))
    for m in METHODS:
        assert np.array_equal(valid[m], flag(rows, f"{m}_available"))
        assert np.isnan(positions[m][~valid[m]]).all(), (eid, m, "partial invalid position")
        compare(summary[f"{m}_samples"], int((grid & valid[m]).sum()))
        compare(summary[f"{m}_percent"], 100 * (grid & valid[m]).sum() / grid.sum())
    aggregate_summary = next(r for r in aggregate["availability"] if r["experiment_id"] == eid)
    for key, value in summary.items():
        compare(aggregate_summary[key], value)
    compare(summary["added_samples"], int((grid & valid["selected"]).sum() - (grid & valid["joint"]).sum()))
    np.testing.assert_array_equal(positions["selected"][valid["joint"]], positions["joint"][valid["joint"]])
    chosen = np.array([r["position_method"] for r in rows])
    expected = np.full(len(rows), "none", dtype="U32")
    for m in ("acoustic_imu_depth", "optical_imu_depth", "joint"):
        expected[valid[m]] = m
    assert np.array_equal(chosen, expected), (eid, "priority")
    for m in METHODS[:3]:
        np.testing.assert_array_equal(positions["selected"][chosen == m], positions[m][chosen == m])
    for m in METHODS[1:3]:
        compare(summary[f"selected_{m}_samples"], int((grid & (chosen == m)).sum()))
    assert dict(Counter(chosen[grid])) == manifest["method_counts_grid"]
    for m in ("optical_imu_depth", "acoustic_imu_depth"):
        mask = valid[m]
        anchor, age, seeds = values(rows, ["surface_anchor_time_s", "surface_age_s", "surface_samples"]).T
        assert np.all(seeds[mask] >= 3)
        assert np.all(anchor[mask] < times[mask]) and np.all((age[mask] >= 0) & (age[mask] <= 60))
        np.testing.assert_allclose(age[mask], times[mask] - anchor[mask], atol=1e-7)
    anchor, age = values(rows, ["bearing_anchor_time_s", "bearing_age_s"]).T
    acoustic = valid["acoustic_imu_depth"]
    assert np.all(anchor[acoustic] < times[acoustic]) and np.all((age[acoustic] >= 0) & (age[acoustic] <= 3))
    np.testing.assert_allclose(age[acoustic], times[acoustic] - anchor[acoustic], atol=1e-7)
    # Anchor must identify an earlier independent optical/joint observation;
    # an acoustic-only result can never refresh the held bearing.
    independent = valid["joint"] | valid["optical_imu_depth"]
    for i in np.flatnonzero(np.isfinite(anchor)):
        j = np.searchsorted(times, anchor[i])
        assert j < i and times[j] == anchor[i] and independent[j], (eid, i, "self-refresh")
        assert not independent[j + 1:i].any(), (eid, i, "not latest independent bearing")
    metric_paths = sorted((folder / "static").glob("*/metrics.json"))
    assert {p.parent.name for p in metric_paths} == {w["id"] for w in manifest["metadata"]["static_windows"]}
    static_rows = []
    for metric_path in metric_paths:
        static_rows.extend(read_json(metric_path)["positions"])
        verify_window(metric_path, rows, aggregate, manifest["metadata"]["grid_hz"])
    assert static_rows == manifest["static_metrics"]
    verify_links(folder)
    return {"experiment_id": eid, "rows": len(rows), "grid_rows": int(grid.sum()), "events": int(event.sum())}


def verify_window(path, source, aggregate, hz):
    document = read_json(path)
    window = document["window"]
    lo, hi = window["start_s"], window["end_s"]
    rows = read_csv(path.parent / "samples.csv")
    expected = [r for r in source if lo <= float(r["utc_s"]) < hi]
    assert [{k: v for k, v in r.items() if k != "common_event"} for r in rows] == expected
    time = values(rows, ["utc_s"])[:, 0]
    grid, event = flag(rows, "grid_sample"), flag(rows, "event_sample")
    positions = {m: values(rows, [f"{m}_{a}_m" for a in AXES]) for m in METHODS}
    common = event & np.logical_and.reduce([finite(positions[m]) for m in METHODS[:3]])
    assert np.array_equal(common, flag(rows, "common_event"))
    for sampling, mask in (("all_events", event), ("common_events", common), ("all_grid", grid)):
        for method in METHODS:
            metric = next(r for r in document["positions"] if r["sampling"] == sampling and r["method"] == method)
            compare(metric["recorded_samples"], int(mask.sum()))
            for key, value in rms(positions[method][mask]).items():
                compare(metric[key], value)
            agg = next(r for r in aggregate["rms"] if r["window"] == window["id"] and r["sampling"] == sampling and r["method"] == method)
            for key, value in metric.items():
                compare(agg[key], value)
    for metric in document["sensors"]:
        if metric["sensor"] == "acoustic_range":
            sample = values(rows, ["range_m"])[:, 0][event]
            sample = sample[np.isfinite(sample)]
            value = float(np.std(sample, ddof=0)) if len(sample) >= 2 else None
            count = len(sample)
        else:
            prefix = "camera_ray" if metric["sensor"] == "optical_camera_bearing" else "world_ray"
            sample = values(rows, [f"{prefix}_{a}" for a in "xyz"])[grid]
            value, count = angular_rms(sample), int(finite(sample).sum())
        compare(metric["samples"], count)
        compare(metric["centered_rms"], value)
        agg = next(r for r in aggregate["sensors"] if r["window"] == window["id"] and r["sensor"] == metric["sensor"])
        for key, value in metric.items():
            compare(agg[key], value)
    coverage = document["coverage"]
    expected_count = round((hi - lo) * hz)
    compare(coverage["expected_grid_samples"], expected_count)
    for method in METHODS:
        valid = grid & finite(positions[method])
        compare(coverage[f"{method}_samples"], int(valid.sum()))
        compare(coverage[f"{method}_percent"], 100 * valid.sum() / expected_count)
        grid_times = time[grid]
        if len(grid_times):
            first_slot = int(np.floor((grid_times[0] - lo) * hz + 1e-5))
            occupied = set(first_slot + np.rint((time[valid] - grid_times[0]) * hz).astype(int))
        else:
            occupied = set()
        longest = run = 0
        for slot in range(expected_count):
            run = 0 if slot in occupied else run + 1
            longest = max(longest, run)
        compare(coverage[f"{method}_longest_gap_s"], longest / hz)
    agg = next(r for r in aggregate["coverage"] if r["window"] == window["id"])
    for key, value in coverage.items():
        compare(agg[key], value)


def verify_figures(folder):
    tested = []
    for master in folder.glob("*.figure.pickle"):
        stem = master.name.removesuffix(".figure.pickle")
        for suffix in ("png", "svg", "pdf", "plot.json", "figure.pickle"):
            assert (folder / f"{stem}.{suffix}").stat().st_size > 0
        with TemporaryDirectory(prefix="fallback_plot_verify_") as temporary:
            target = Path(temporary)
            for source in folder.glob(f"{stem}.*"):
                shutil.copy2(source, target / source.name)
            document = PlotDocument(target / master.name)
            bars = [p.get_height() for ax in document.figure.axes for p in ax.patches]
            xmin, xmax = document.figure.axes[0].get_xlim()
            document.update({"figure": {"width": 10.25, "height": 6.25},
                             "axes": {"0": {"xlabel": "verification x", "ylabel": "verification y",
                                             "xmin": xmin - .1, "xmax": xmax + .1}}})
            document.save()
            restored = PlotDocument(target / master.name)
            np.testing.assert_allclose(restored.figure.get_size_inches(), [10.25, 6.25])
            assert restored.figure.axes[0].get_xlabel() == "verification x"
            assert restored.figure.axes[0].get_ylabel() == "verification y"
            np.testing.assert_allclose(restored.figure.axes[0].get_xlim(), [xmin - .1, xmax + .1])
            np.testing.assert_allclose([p.get_height() for ax in restored.figure.axes for p in ax.patches], bars, equal_nan=True)
            for suffix in ("style.json", "edited.png", "edited.svg", "edited.pdf"):
                assert (target / f"{stem}.{suffix}").stat().st_size > 0
            plt.close(document.figure)
            plt.close(restored.figure)
        tested.append(stem)
    assert set(tested) == {"availability", "stationary_rms"}
    return tested


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=[REVIEW])
    args = parser.parse_args()
    folder = ROOT / f"results/{args.experiment}/position_fallback"
    result = {"status": "running", "created_utc": datetime.now(UTC).isoformat(), "verifier_sha256": sha(Path(__file__)), "checks": {}}
    try:
        manifest = read_json(folder / "manifest.json")
        verify_hashes(manifest)
        process = read_json(folder / "process.json")
        assert process["status"] == "completed" and process.get("ended_utc")
        if os.name == "nt":
            query = subprocess.run(["powershell", "-NoProfile", "-Command",
                                    (f"try {{ $p = Get-Process -Id {int(process['pid'])} -ErrorAction Stop; $p.StartTime.ToUniversalTime().ToString('o') }} "
                                    "catch [Microsoft.PowerShell.Commands.ProcessCommandException] { "
                                    "if ($_.FullyQualifiedErrorId -notlike 'NoProcessFoundForGivenId,*') { throw } }; exit 0")],
                                   capture_output=True, text=True, timeout=15, check=False)
            assert query.returncode == 0
            if query.stdout.strip():
                current_start = datetime.fromisoformat(query.stdout.strip())
                recorded_start = datetime.fromisoformat(process["started_utc"])
                assert abs((current_start - recorded_start).total_seconds()) > 3, "Generation PID still alive"
        aggregate = {"availability": read_csv(folder / "availability.csv"), "rms": read_csv(folder / "stationary_rms.csv"),
                     "sensors": read_csv(folder / "sensor_rms.csv"), "coverage": read_csv(folder / "stationary_coverage.csv")}
        result["checks"]["trials"] = [verify_trial(eid, aggregate) for eid in manifest["experiments"]]
        result["checks"]["figures"] = verify_figures(folder)
        verify_links(folder)
        result["checks"]["input_hashes_process_exit_links"] = "pass"
        result["status"] = "passed"
    except BaseException as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        plt.close("all")
        (folder / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
