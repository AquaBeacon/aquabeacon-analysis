"""Verify saved NGT position/wall artifacts without modifying acquisition data."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("OMP_NUM_THREADS", "2")
import matplotlib
import numpy as np

matplotlib.use("Agg")
from aquabeacon_analysis.editable_plots import PlotDocument
from aquabeacon_analysis.ngt_binary import sha256

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results/20261028_ngt_000/position_evaluation"
WALL = ROOT / "results/20261028_ngt_004/wall_mapping"
WORK = ROOT / "data/interim/20261028_ngt_004/wall_mapping"


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261028_ngt_000"])
    parser.parse_args()
    record = {"pid": os.getpid(), "started_utc": datetime.now(UTC).isoformat(),
              "owner": "position_evaluation", "purpose": __doc__}
    print(json.dumps(record), flush=True)
    checked_hashes, positions, mapping = {}, [], []

    def check_hash(path, expected):
        key = str(path.relative_to(ROOT))
        if key not in checked_hashes:
            checked_hashes[key] = sha256(path)
        actual = checked_hashes[key]
        assert actual == expected, f"Changed input: {key}"

    ranges = read_csv(ROOT / "data/processed/20261028_ngt_000/fusion/range_constraints.csv")
    folders = [OUT, WALL]
    for suffix in ("002", "003", "004"):
        folder = ROOT / f"results/20261028_ngt_{suffix}/position_evaluation"
        folders.append(folder)
        manifest, metrics = [read_json(folder / f"{name}.json") for name in ("manifest", "metrics")]
        for item in manifest["inputs"]:
            check_hash(ROOT / item["path"], item["sha256"])
        rows = read_csv(folder / "positions_5hz.csv")
        shots = read_csv(folder / "positions_at_acoustic_events.csv")
        lo, hi = metrics["interval_s"]
        original = [r for r in ranges if lo <= float(r["parent_video_s"]) < hi]
        assert [r["shot_seq"] for r in original] == [r["shot_seq"] for r in shots]
        assert len(rows) == int((hi-lo)*5)
        assert np.allclose([float(r["parent_video_s"]) for r in rows], np.arange(lo, hi, .2), rtol=0, atol=1e-7)
        for key, count in metrics["grid_counts"].items():
            assert sum(r[key] == "True" for r in rows) == count
        assert np.isclose(sum(g["duration_s"] for g in metrics["missing_intervals"]),
                          (len(rows)-metrics["grid_counts"]["magnetic_position_available"])/5, atol=1e-6)
        maximum_norm_error = 0.
        for r in rows + shots:
            available = r["magnetic_position_available"] == "True"
            xyz = np.array([float(r[k]) for k in
                            ["relative_magnetic_north_m", "relative_magnetic_east_m", "relative_down_m"]])
            assert bool(np.isfinite(xyz).all()) == available
            if available:
                assert r["range_formula_available"] == r["optical_available"] == r["magnetic_attitude_available"] == "True"
                error = abs(np.linalg.norm(xyz)-float(r["range_m"]))
                maximum_norm_error = max(maximum_norm_error, error)
                assert error < 1e-10
                assert abs(float(r["range_time_delta_s"])) <= .6000001
                assert abs(float(r["optical_time_delta_s"])) <= .1200001
            if any(a <= float(r["parent_video_s"]) < b for a, b in [(1397.5, 1402.5), (1497.5, 1502.5)]):
                assert r["optical_available"] == "False" and r["optical_reason"]
        for raw, row in zip(original, shots, strict=True):
            assert raw["range_valid"] == row["range_formula_available"]
            if raw["range_valid"] == "True":
                assert float(raw["corrected_range_m"]) == float(row["range_m"])
        assert metrics["ground_truth_rmse_m"] is None
        assert sha256(folder / "generate.py") == sha256(ROOT / "scripts/evaluate_ngt_position.py")
        positions.append({"experiment": suffix, "grid_rows": len(rows), "all_events_retained": len(shots),
                          "norm_error_max_m": maximum_norm_error, "input_hashes_match": True})

    for suffix in ("", "five_hz"):
        folder, work = WALL / suffix, WORK / suffix
        manifest = read_json(folder / "manifest.json")
        check_hash(ROOT / manifest["source"], manifest["source_sha256"])
        check_hash(ROOT / manifest["calibration"], manifest["calibration_sha256"])
        input_path = ROOT / manifest.get("position_input_snapshot",
            "results/20261028_ngt_004/position_evaluation/positions_5hz.csv")
        check_hash(input_path, manifest["position_csv_sha256"])
        frames, priors = [read_csv(work / p) for p in ("frames.csv", "frame_priors.csv")]
        assert len(frames) == manifest["images"] == len(priors)
        assert sum(r["prior_available"] == "True" for r in priors) == manifest["prior_count"]
        assert not manifest["nominal_25m_used_as_scale"]
        for mode in ("visual_only", "aquabeacon_prior"):
            registration = read_csv(folder / f"{mode}_registration.csv")
            assert len(registration) == len(frames)
            expected = next(r for r in read_json(WALL / "summary.json")
                            if r["sample_hz"] == manifest["sample_hz"] and r["mode"] == mode)
            assert sum(r["registered_nondegenerate"] == "True" for r in registration) == expected["registered_nondegenerate_unique"]
            assert expected["known_dimension_error_m"] is None
            mapping.append(expected)
        for phase in ("prepare", "features", "visual-only", "prior"):
            process = read_json(folder / f"{phase}.process.json")
            assert process["status"] == "completed" and process["ended_utc"]

    graph_count = 0
    for folder in folders:
        for meta_path in folder.glob("*.plot.json"):
            meta = read_json(meta_path)
            master = folder / meta["master"]
            check_hash(master, meta["master_sha256"])
            assert (folder / meta["image"]).stat().st_size > 1000
            assert (folder / meta["image"]).with_suffix(".svg").stat().st_size > 1000
            graph_count += 1

    editor = OUT / "editor_roundtrip"
    editor.mkdir(exist_ok=True)
    base = ROOT / "results/20261028_ngt_002/position_evaluation/positions.figure.pickle"
    shutil.copy2(base, editor / base.name)
    master = editor / base.name
    doc = PlotDocument(master)
    before = doc.figure.axes[0].lines[0].get_xydata().copy()
    doc.update({"figure": {"width": 9, "height": 4.5, "font_size": 11},
                "axes": {"0": {"xmin": -.8, "xmax": .8, "ymin": -.6, "ymax": .6,
                    "xlabel": "Edited magnetic east [m]", "grid": True,
                    "artists": {"0": {"color": "#2244aa", "marker": "o", "markersize": 5}}}}})
    doc.save()
    restored = PlotDocument(master)
    np.testing.assert_allclose(restored.figure.axes[0].lines[0].get_xydata(), before, equal_nan=True)
    np.testing.assert_allclose(restored.figure.get_size_inches(), [9, 4.5])
    np.testing.assert_allclose(restored.figure.axes[0].get_xlim(), [-.8, .8])
    np.testing.assert_allclose(restored.figure.axes[0].get_ylim(), [-.6, .6])
    assert sha256(master) == sha256(base)
    for extension in ("png", "svg", "pdf"):
        assert (editor / f"positions.edited.{extension}").stat().st_size > 1000

    broken_links = []
    for folder in folders:
        report = folder / "report.md"
        for target in re.findall(r"\]\(([^)]+)\)", report.read_text(encoding="utf-8")):
            if "://" not in target and not (folder / target).exists() and target != "verification.json":
                broken_links.append(str(folder / target))
    assert not broken_links, broken_links
    plan = ROOT / "data/raw/ngtHighSchoolExperiment1/AquaBeacon_pool_experiment_checklist.docx"
    record.update(status="passed", ended_utc=datetime.now(UTC).isoformat(),
        git_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        verifier_sha256=sha256(Path(__file__)), input_hashes=checked_hashes, positions=positions, mapping=mapping,
        plan={"path": str(plan.relative_to(ROOT)), "sha256": sha256(plan)}, editable_graphs=graph_count,
        editor_roundtrip="Dimensions, limits, style, PNG/SVG/PDF and original data preserved; backend exercised without a browser server",
        manual_review={"reviewer": "Codex", "source_samples": "98 parent frames at 5s, 19 child frames at 15s, parent axis action frames",
            "figures": "9 position plots, summary plot, 2 wall plots and wall feature overlays visually inspected",
            "observations": "Occlusion exclusions at 1400/1500s; wall features visible in ROI; plots readable and missing data retained",
            "plan_coverage": "Static spread, full-duration availability, outages, observed trajectories and wall shape assessed; independent RMSE/dimension error unavailable"},
        raw_write_policy="Analysis code only reads raw inputs; child LRF hash equals the preparation manifest; no claim of a whole-raw-tree before/after hash audit",
        tests_log="pytest.log", lint_log="ruff.log", process_cleanup_evidence="process_cleanup.json")
    (OUT / "verification.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "passed", "position_experiments": len(positions),
                      "mapping_attempts": len(mapping), "editable_graphs": graph_count}), flush=True)


if __name__ == "__main__":
    main()
