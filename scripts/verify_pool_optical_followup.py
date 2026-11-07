"""Verify pool recovery preservation, numerical comparisons, mapping and report provenance."""
from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime

import numpy as np
from evaluate_position_fallback import dump, scatter
from improve_nanyo_light import ROOT, read, relative
from improve_ngt_light import CONFIG, REVIEW, folder
from PIL import Image
from position_fallback_ngt import numbers
from write_pool_optical_followup import DENSE_CONFIG, FRAME_REVIEW

from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.pool_optical import reviewed_pool_rows


def main():
    parser = argparse.ArgumentParser(__doc__); parser.add_argument("--experiment", action="append", required=True)
    args = parser.parse_args(); cfg = json.loads(CONFIG.read_text())
    if set(args.experiment) != set(cfg["experiments"]):
        parser.error("Verify all three pool trial IDs explicitly")
    root_out = folder("20261028_ngt_000", cfg)
    snapshots = json.loads((root_out/"source_snapshot_index.json").read_text())
    hashes, archived, checked = {}, [], []

    def check(path, expected):
        key = relative(path)
        if key not in hashes:
            hashes[key] = sha256(path)
        if hashes[key] == expected:
            return
        snapshot = snapshots.get(key)
        if not snapshot or snapshot["sha256"] != expected or sha256(ROOT/snapshot["snapshot"]) != expected:
            raise ValueError("Changed input: "+key)
        if key not in archived:
            archived.append(key)

    identity = json.loads(REVIEW.read_text(encoding="utf-8"))
    frame_review = json.loads(FRAME_REVIEW.read_text(encoding="utf-8"))
    source_rows = read(ROOT/cfg["baseline"])
    reports = [root_out/"report.md"]
    mappings = []
    for eid in cfg["experiments"]:
        out = folder(eid, cfg); reports.append(out/"report.md"); mappings.append((eid, out, cfg))
        for name in ("detection_manifest.json", "analysis_manifest.json"):
            meta = json.loads((out/name).read_text())
            for path, expected in meta["inputs_sha256"].items():
                check(ROOT/path, expected)
        detect = json.loads((out/"detection_manifest.json").read_text())
        if not detect["same_frame_control_exact"]:
            raise ValueError("Same-frame old detector was not reproduced")
        source = ROOT/cfg["source_video"]
        if source.stat().st_size != detect["source_stat"]["size"] or source.stat().st_mtime_ns != detect["source_stat"]["mtime_ns"]:
            raise ValueError("Raw video changed")
        records = read(out/"automatic_candidates.csv")
        reviewed = read(out/"reviewed_candidates.csv")
        review = identity["experiments"][eid]
        check(out/"automatic_candidates.csv", review["automatic_candidates_sha256"])
        expected_rows = reviewed_pool_rows(records, review["accepted_rows"], review["rejected_rows"])
        original = [r for r in source_rows if detect["interval_s"][0] <= float(r["frame_pts_s"]) < detect["interval_s"][1]]
        if len(original) != len(reviewed) or len(records) != len(reviewed):
            raise ValueError("Observation denominator changed")
        for i, (old, new, expected) in enumerate(zip(original, reviewed, expected_rows, strict=True)):
            if any(new[k] != str(v) for k, v in expected.items()):
                raise ValueError("Review application mismatch")
            if i not in review["accepted_rows"] and any(new[k] != v for k, v in old.items()):
                raise ValueError("Old measurement/exclusion altered")
            if new["frame_index"] != old["frame_index"] or new["frame_pts_s"] != old["frame_pts_s"]:
                raise ValueError("Frame timestamps altered")
            if abs(float(new["decoded_pts_s"])-float(old["frame_pts_s"])) > .001:
                raise ValueError("Decoded frame mismatch")
        for case in json.loads((out/"parent_frame_cases.json").read_text()):
            check(out/case["image"], case["image_sha256"])
            with Image.open(out/case["image"]) as im:
                if im.size != (720, 1280):
                    raise ValueError("Parent full-frame geometry changed")
        for case in frame_review["experiments"][eid]:
            check(out/case["image"], case["image_sha256"])
        series = {v: read(out/f"{v}_positions.csv") for v in ("old", "new")}
        times = numbers(series["old"], ["utc_s"])[:, 0]
        np.testing.assert_array_equal(times, numbers(series["new"], ["utc_s"])[:, 0])
        old_joint = numbers(series["old"], [f"joint_{a}_m" for a in ("east", "north", "up")])
        new_joint = numbers(series["new"], [f"joint_{a}_m" for a in ("east", "north", "up")])
        good = np.isfinite(old_joint).all(1)
        np.testing.assert_array_equal(old_joint[good], new_joint[good])
        availability = read(out/"availability.csv")
        grid = np.array([r["grid_sample"] == "True" for r in series["old"]])
        for metric in availability:
            rows = series[metric["version"]]
            if int(metric["grid_samples"]) != int(grid.sum()):
                raise ValueError("Grid denominator differs")
            for method in ("joint", "optical_imu_depth", "acoustic_imu_depth", "selected"):
                valid = np.isfinite(numbers(rows, [f"{method}_{a}_m" for a in ("east", "north", "up")])).all(1)
                if int(metric[method+"_samples"]) != int((grid & valid).sum()):
                    raise ValueError("Availability differs")
        if (out/"paired_static_rms.csv").exists():
            for metric in read(out/"paired_static_rms.csv"):
                arrays = {v: numbers(rows, [f"{metric['method']}_{a}_m" for a in ("east", "north", "up")]) for v, rows in series.items()}
                mask = grid.copy()
                if metric["sampling"] == "paired_same_times":
                    mask &= np.isfinite(arrays["old"]).all(1) & np.isfinite(arrays["new"]).all(1)
                measured = scatter(arrays[metric["version"]][mask])
                if measured["samples"] != int(metric["samples"]):
                    raise ValueError("Static sample count differs")
                np.testing.assert_allclose(measured["rms_3d_m"], float(metric["rms_3d_m"]), atol=1e-12)
        checked.append({"experiment_id": eid, "frames": len(original), "retained_additions": len(review["accepted_rows"]),
                        "reviewed_proposals": len(review["accepted_rows"])+len(review["rejected_rows"])})
    dense_cfg = json.loads(DENSE_CONFIG.read_text()); dense_out = folder("20261028_ngt_004", dense_cfg)
    mappings.append(("20261028_ngt_004", dense_out, dense_cfg)); reports.append(dense_out/"report.md")
    for eid, out, run_cfg in mappings:
        for name in ("analysis_manifest.json", "mapping.json"):
            for path, expected in json.loads((out/name).read_text())["inputs_sha256"].items():
                check(ROOT/path, expected)
        mapping = json.loads((out/"mapping.json").read_text()); process = json.loads((out/"mapping_process.json").read_text())
        if mapping["status"] != "completed" or process["status"] != "completed" or process["returncode"] != 0:
            raise ValueError("Mapping worker failed or incomplete")
        priors = read(out/"mapping_frame_priors.csv")
        if len(priors) > run_cfg["maximum_images"]:
            raise ValueError("Mapping image budget exceeded")
        source = ROOT/json.loads((out/"mapping_input.json").read_text())["source_work"]/"images"
        for row in priors:
            check(source/row["image"], row["image_sha256"])
            check(ROOT/row["source_image"], row["image_sha256"])
        for mode in mapping["modes"]:
            names = set()
            if mode["images"] != len(priors):
                raise ValueError("Mapping image denominator differs")
            for m in mode["models"]:
                poses = read(out/f"{mode['version']}_model_{m['model']}_cameras.csv")
                names.update(r["image"] for r in poses)
                if len(poses) != m["registered_images"] or not (out/m["ply"]).exists():
                    raise ValueError("Model output incomplete")
                p = m["placement"]
                if p["accepted"] and (eid.endswith("002") or p["correspondences"] < 6 or p["scale"] <= 0
                                      or p["navigation_span_m"] < .25 or p["fit_median_m"] > 5 or m["points"] < 50):
                    raise ValueError("Unsupported metric map")
                if m["unit"] != ("m_provisional" if p["accepted"] else "local_units") or m["accuracy_validated"]:
                    raise ValueError("Unsupported units/accuracy claim")
            if len(names) != mode["registered_unique"]:
                raise ValueError("Registered count not deduplicated")
    for path, expected in json.loads((root_out/"report_manifest.json").read_text())["inputs_sha256"].items():
        check(ROOT/path, expected)
    for report in reports:
        for link in re.findall(r"\]\(([^)]+)\)", report.read_text(encoding="utf-8")):
            if not link.startswith(("http:", "https:", "#")) and not (report.parent/link).is_file():
                raise ValueError("Missing report link: "+link)
    dump(root_out/"verification.json", {"status": "passed", "created_utc": datetime.now(UTC).isoformat(),
          "experiments": checked, "mapping_attempts": len(mappings), "exact_execution_snapshots": archived,
          "checks": ["raw video stat and exact old same-frame control", "old values and veto preservation", "explicit complete addition review",
                     "parent full-frame geometry and image hashes", "matching position grid and unchanged old joint positions",
                     "recomputed availability and paired RMS", "mapping units, support and worker exit codes", "report input hashes and links"],
          "accuracy_validated": False})
    print("PASS", len(checked), "pool trials;", sum(r["retained_additions"] for r in checked), "retained additions;", len(mappings), "mapping attempts")


if __name__ == "__main__":
    main()
