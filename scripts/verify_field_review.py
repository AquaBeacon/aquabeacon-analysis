"""Independently check the field review, its sources and portable figure exports."""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import tempfile
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import numpy as np

from aquabeacon_analysis.editable_plots import PlotDocument
from aquabeacon_analysis.experiments import load_experiment
from aquabeacon_analysis.ngt_binary import sha256

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def rms(data, columns):
    x = np.array([[float(r[c]) for c in columns] for r in data])
    assert np.isfinite(x).all() and len(x) > 0
    centered = x - x.mean(axis=0)
    return float(np.sqrt(np.sum(centered**2) / len(x)))


def close(a, b):
    np.testing.assert_allclose(float(a), float(b), atol=1e-10, rtol=1e-10)


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets = []

    def handle_starttag(self, tag, attrs):
        self.targets.extend(v for k, v in attrs if k in ("href", "src") and v)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261103_field_review_000"])
    args = parser.parse_args()
    exp = load_experiment(ROOT, args.experiment)
    out = exp.stage_path("results") / "research_review"
    checks = {}
    relative_count = absolute_count = 0
    for row in rows(out / "stationary_summary.csv"):
        base = ROOT / "results" / row["experiment_id"]
        matched = rows(base / "stationary_spectra" / row["window"] / "matched_positions.csv")
        assert len(matched) == int(row["samples"])
        assert len({r["shot_seq"] for r in matched}) == len(matched)
        for prefix, key in [("corrected", "relative_rms_m"), ("fixed_parent", "fixed_parent_rms_m")]:
            close(rms(matched, [f"{prefix}_{axis}_m" for axis in ("east", "north", "up")]), row[key])
        close(float(row["radial_rms_m"])**2 + float(row["tangential_rms_m"])**2,
              float(row["relative_rms_m"])**2)
        relative_count += len(matched)
        absolute = rows(base / "stationary_gnss_comparison" / row["window"] / "position_samples.csv")
        for solution in ("ppk_ar", "ppk_float"):
            subset = [r for r in absolute if r["solution"] == solution and r["attitude"] == "corrected"
                      and r["common_available"] == "True"]
            assert {r["shot_seq"] for r in subset} == {r["shot_seq"] for r in matched}
            close(rms(subset, [f"child_absolute_{axis}_m" for axis in ("east", "north", "up")]),
                  row[f"absolute_{solution}_rms_m"])
            absolute_count += len(subset)
    checks["stationary"] = {"windows": 6, "unique_relative_shots": relative_count,
                            "absolute_samples_two_solutions": absolute_count, "independent_rms": "passed"}
    pool = rows(ROOT / "results/20261028_ngt_002/position_evaluation/rotation_corrected_20261031/positions_at_acoustic_events.csv")
    pool = [r for r in pool if r["magnetic_position_available"] == "True"]
    close(rms(pool, ["relative_magnetic_east_m", "relative_magnetic_north_m", "relative_down_m"]),
          load(out / "stationary_details.json")["ngt_002"]["scatter"]["rms_3d_m"])
    checks["pool_unique_events"] = len(pool)
    sim = rows(out / "simulation_comparison.csv")
    assert len(sim) == 56
    for row in sim:
        assert np.isfinite(float(row["mc_centered_rms_m"]))
        close(float(row["observed_centered_rms_m"]) / float(row["mc_centered_rms_m"]),
              row["observed_to_sim_scatter_ratio"])
        if row["case"] == "base_noise":
            assert abs(float(row["mc_centered_rms_m"]) / float(row["linearized_rms_m"]) - 1) < .015
    checks["simulation"] = {"cases": len(sim), "basic_mc_linear_tolerance": .015,
                            "scope": "Conditional model; no field ground truth"}
    captures = envelope_rows = loss = 0
    for row in rows(out / "acoustic_capture_quality.csv"):
        folder = ROOT / f'results/{row["experiment_id"]}/acoustic_audit_20261103'
        meta = load(folder / "manifest.json")
        assert len(rows(folder / "captures.csv")) == int(row["recorded_captures_in_csv"])
        for name, digest in meta["inputs_sha256"].items():
            assert sha256(ROOT / name) == digest, name
        captures += int(row["recorded_captures_in_csv"])
        envelope_rows += int(row["envelope_rows"])
        loss += int(row["recorded_lost_samples"])
    checks["acoustic_capture_audit"] = {"recorded_captures": captures, "samples": envelope_rows,
                                       "acquisition_lost_samples": loss, "input_hashes": "passed"}
    hashed = 0
    manifest = load(out / "manifest.json")
    for name, entry in manifest["inputs"].items():
        assert sha256(ROOT / name) == entry["sha256"], name
        hashed += 1
    for name, digest in manifest["outputs"].items():
        assert sha256(out / name) == digest, name
    report_manifest = load(out / "report_manifest.json")
    for name, digest in report_manifest["inputs_sha256"].items():
        assert sha256(ROOT / name) == digest, name
        hashed += 1
    for name, digest in report_manifest["outputs_sha256"].items():
        assert sha256(out / name) == digest, name
    checks["hashed_source_references"] = hashed
    browser = load(out / "browser_verification.json")
    assert browser["passed"] and browser["browser_exited"]
    for entry in browser["reports"]:
        assert sha256(out / entry["file"]) == entry["sha256"], entry["file"]
    for entry in browser["viewers"]:
        assert sha256(ROOT / entry["file"]) == entry["sha256"], entry["file"]
    checks["browser_checked_current_artifacts"] = True
    folders, model_summary = [], []
    for suffix, run in [("004", "field_review_20261103_2hz"), ("005", "field_review_20261103_5hz")]:
        folder = ROOT / f"results/20261101_nanyo_{suffix}/mapping_refinement/{run}"
        folders.append(folder)
        meta, review = load(folder / "manifest.json"), load(folder / "review_manifest.json")
        assert sha256(folder / "generate.py") == meta["code_sha256"]
        assert sha256(folder / "manifest.json") == review["source_manifest_sha256"]
        assert sha256(folder / "execution.log") == review["execution_log_sha256"]
        for name, digest in review["inputs_sha256"].items():
            assert sha256(ROOT / name) == digest, name
        status = rows(folder / "frame_status.csv")
        assert len(status) == meta["sampled_frames"]
        registered = {r["image"] for r in status if r["registered"] == "True"}
        assert len(registered) == meta["registered_frames"]
        model_frames = set()
        for model in review["models"]:
            poses = rows(folder / f'model_{model["model"]}_poses.csv')
            assert len(poses) == model["images"]
            model_frames.update(r["image"] for r in poses)
            assert np.isfinite([[float(r[c]) for c in ("utc_s", "x_local", "y_local", "z_local")] for r in poses]).all()
            binary = (folder / f'model_{model["model"]}_LOCAL_UNITS.ply').read_bytes()
            header, content = re.split(rb"end_header\r?\n", binary, maxsplit=1)
            count = int(re.search(rb"element vertex (\d+)", header)[1])
            point = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("rgb", "u1", 3)])
            vertices = np.frombuffer(content, dtype=point)
            assert count == len(vertices) == model["points"]
            assert all(np.isfinite(vertices[c]).all() for c in ("x", "y", "z"))
            assert model["metric_accepted"] is False and model["accuracy_validated"] is False
            model_summary.append({"experiment": meta["experiment_id"], "model": model["model"],
                                  "images": len(poses), "points": count, "metric_accepted": False})
        assert model_frames == registered
    checks["mapping_models"] = model_summary
    exported = []
    with tempfile.TemporaryDirectory(prefix="field_review_figure_") as temp:
        for name in ("observed_simulation_scatter", "directional_scatter", "navigation_availability"):
            for suffix in ("png", "svg", "figure.pickle", "plot.json"):
                assert (out / f"{name}.{suffix}").stat().st_size > 0
            copied = Path(temp) / f"{name}.figure.pickle"
            shutil.copy2(out / copied.name, copied)
            shutil.copy2(out / f"{name}.plot.json", Path(temp) / f"{name}.plot.json")
            doc = PlotDocument(copied)
            original_heights = [p.get_height() for p in doc.figure.axes[0].patches]
            doc.update({"figure": {"width": 9.5, "height": 4.5},
                        "axes": {"0": {"ylabel": "検証用RMS", "ylim": [0, 110]}}})
            doc.save()
            restored = PlotDocument(copied)
            assert restored.figure.axes[0].get_ylabel() == "検証用RMS"
            np.testing.assert_allclose(restored.figure.get_size_inches(), [9.5, 4.5])
            np.testing.assert_equal(original_heights, [p.get_height() for p in restored.figure.axes[0].patches])
            for suffix in ("png", "svg", "pdf"):
                assert (Path(temp) / f"{name}.edited.{suffix}").stat().st_size > 0
            exported.append(name)
    checks["actual_2d_figure_edit_save_reload_export"] = exported
    # The final contents are written after checking links; create the destination first.
    destination = out / "verification.json"
    if not destination.exists():
        destination.write_text("{}\n", encoding="utf-8")
    missing, link_count = [], 0
    for folder in [out, *folders]:
        for path in [*folder.glob("*.md"), *folder.glob("*.html")]:
            content = path.read_text(encoding="utf-8")
            assert "{{" not in content or path.name == "viewer.html", path
            if path.suffix == ".html":
                links = Links()
                links.feed(content)
                targets = links.targets
            else:
                targets = re.findall(r"!?\[[^\]]*\]\(([^\n)]+)\)", content)
            for link in targets:
                target = urlsplit(link.strip("<>"))
                if target.scheme or not target.path:
                    continue
                link_count += 1
                if not (path.parent / unquote(target.path)).exists():
                    missing.append({"document": str(path.relative_to(ROOT)), "target": link})
    checks["new_report_links"] = {"checked": link_count, "missing": missing}
    result = {"checked_utc": datetime.now(UTC).isoformat(), "checks": checks,
              "passed": not missing, "verifier_sha256": sha256(Path(__file__))}
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if missing:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
