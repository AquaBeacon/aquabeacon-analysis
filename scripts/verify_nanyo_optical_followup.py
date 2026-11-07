"""Verify follow-up provenance, paired statistics, image evidence and map units."""
from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime

import numpy as np
from analyze_nanyo_optical_followup import CONFIG
from evaluate_position_fallback import dump, scatter
from improve_nanyo_light import ROOT, read, relative
from PIL import Image

from aquabeacon_analysis.ngt_binary import sha256


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", action="append", required=True)
    args = parser.parse_args()
    cfg = json.loads(CONFIG.read_text())
    if set(args.experiment)-set(cfg["experiments"]):
        parser.error("Explicit configured trials required")
    root_out = ROOT/f"results/20261103_field_review_000/optical_followup/{cfg['run']}"
    snapshot_index = root_out/"source_snapshot_index.json"
    snapshots = json.loads(snapshot_index.read_text()) if snapshot_index.exists() else {}
    hashes, archived, verified = {}, [], []

    def check(source, expected):
        if source not in hashes:
            hashes[source] = sha256(ROOT/source)
        if hashes[source] == expected:
            return
        snapshot = snapshots.get(source)
        if not snapshot or snapshot["sha256"] != expected or sha256(ROOT/snapshot["snapshot"]) != expected:
            raise ValueError(f"Changed input without an exact execution snapshot: {source}")
        if source not in archived:
            archived.append(source)

    reports = [root_out/"report.md"]
    for eid in dict.fromkeys(args.experiment):
        out = ROOT/f"results/{eid}/optical_followup/{cfg['run']}"
        reports.append(out/"report.md")
        for filename in ("manifest.json", "mapping.json"):
            meta = json.loads((out/filename).read_text())
            for source, expected in meta["inputs_sha256"].items():
                check(source, expected)
        mapping = json.loads((out/"mapping.json").read_text())
        process = json.loads((out/"mapping_process.json").read_text())
        if mapping["status"] != "completed" or process["returncode"] != 0:
            raise ValueError("Mapping process incomplete or failed")
        priors = read(out/"mapping_frame_priors.csv")
        if len(priors) > cfg["maximum_images"]:
            raise ValueError("Image budget exceeded")
        source_images = ROOT/json.loads((out/"mapping_input.json").read_text())["source_work"]/"images"
        for row in priors:
            check(relative(source_images/row["image"]), row["image_sha256"])
        metric_models = 0
        for mode in mapping["modes"]:
            names = set()
            if mode["images"] != len(priors):
                raise ValueError("Old/new mapping image denominator differs")
            for model in mode["models"]:
                poses = read(out/f"{mode['version']}_model_{model['model']}_cameras.csv")
                names.update(r["image"] for r in poses)
                if len(poses) != model["registered_images"] or not (out/model["ply"]).is_file():
                    raise ValueError("Model camera count or point cloud missing")
                p = model["placement"]
                if p["accepted"]:
                    metric_models += 1
                    if not (p["anchors"] >= 6 and p["scale_m_per_unit"] > 0 and model["points"] >= 50
                            and p["navigation_span_m"] >= .25 and p["fit_median_m"] <= 5
                            and p["orientation_median_deg"] <= 25 and p["orientation_p95_deg"] <= 60):
                        raise ValueError("Unsupported metric model")
                if model["unit"] != ("m_provisional" if p["accepted"] else "local_units"):
                    raise ValueError("Model units disagree with placement decision")
                if model["accuracy_validated"] or model["static_scene_validated"]:
                    raise ValueError("Unsupported ground-truth/static-scene claim")
            if len(names) != mode["registered_unique"]:
                raise ValueError("Registered frames must be deduplicated across models")
        cases = json.loads((out/"parent_frame_cases.json").read_text())
        for case in cases:
            check(relative(out/case["image"]), case["image_sha256"])
            with Image.open(out/case["image"]) as image:
                if image.size != (1280, 720):
                    raise ValueError("Parent evidence must retain the full 1280x720 frame")
        old, new = read(out/"old_positions.csv"), read(out/"new_positions.csv")
        times = np.array([float(r["utc_s"]) for r in old])
        np.testing.assert_array_equal(times, [float(r["utc_s"]) for r in new])
        grid = np.array([r["grid_sample"] == "True" for r in old])
        meta = json.loads((out/"manifest.json").read_text())
        windows = {w["id"]: w for w in meta["metadata"]["static_windows"]}
        arrays = {(label, method): np.array([[float(r[f"{method}_{a}_m"]) for a in ("east", "north", "up")]
                                            for r in rows])
                  for label, rows in (("old", old), ("new", new))
                  for method in ("joint", "optical_imu_depth", "selected")}
        for metric in read(out/"paired_static_rms.csv"):
            window = windows[metric["window"]]
            inside = grid & (times >= window["start_s"]) & (times < window["end_s"])
            if metric["sampling"] == "paired_same_times":
                for label in ("old", "new"):
                    inside &= np.isfinite(arrays[label, metric["method"]]).all(1)
            result = scatter(arrays[metric["version"], metric["method"]][inside])
            if result["samples"] != int(metric["samples"]):
                raise ValueError("Static RMS sample count mismatch")
            if result["rms_3d_m"] is not None:
                np.testing.assert_allclose(result["rms_3d_m"], float(metric["rms_3d_m"]), atol=1e-12)
        verified.append({"experiment_id": eid, "parent_frames": len(cases), "images": len(priors),
                         "old_and_new_metric_models": metric_models})
    report_meta = json.loads((root_out/"report_manifest.json").read_text())
    for source, expected in report_meta["inputs_sha256"].items():
        check(source, expected)
    for path in reports:
        for link in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if not link.startswith(("http:", "https:", "#")) and not (path.parent/link.split("#")[0]).is_file():
                raise ValueError(f"Missing report target: {path}: {link}")
    dump(root_out/"verification.json", {"status": "passed", "created_utc": datetime.now(UTC).isoformat(),
                                        "experiments": verified, "execution_snapshot_used": archived,
                                        "checks": ["input hashes or exact execution snapshots", "full parent-frame dimensions",
                                                   "same paired position times and recomputed RMS", "same mapping image sets",
                                                   "positive scale and navigation/attitude gates", "deduplicated frame counts",
                                                   "mapping worker exit codes", "report links"],
                                        "accuracy_validated": False})
    print("PASS", len(verified), "trials;", sum(r["parent_frames"] for r in verified), "parent frames")


if __name__ == "__main__":
    main()
