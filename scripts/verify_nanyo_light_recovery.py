"""Verify saved Nanyo recovery observations, position grids, review evidence and links."""
from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from evaluate_nanyo_light_recovery import verify_observations
from improve_nanyo_light import CONFIG, ROOT, dump, read, relative

from aquabeacon_analysis.ngt_binary import sha256


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", action="append", required=True)
    parser.add_argument("--run", default="multiscale_v2")
    args = parser.parse_args()
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    if set(args.experiment)-set(cfg["experiments"]):
        parser.error("Explicit registered Nanyo trials required")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", args.run):
        parser.error("Invalid run name")
    hashes, checked = {}, []

    def digest(path):
        path = Path(path)
        key = relative(path)
        if key not in hashes:
            hashes[key] = sha256(path)
        return hashes[key]

    reports = [ROOT/f"results/20261103_field_review_000/optical_detection/{args.run}/report.md",
               ROOT/"results/20261103_field_review_000/optical_detection/input_audit/report.md"]
    for eid in dict.fromkeys(args.experiment):
        out = ROOT/f"results/{eid}/optical_detection/{args.run}"
        for name in ("manifest.json", "identity_review_manifest.json", "evaluation.json"):
            path = out/name
            meta = json.loads(path.read_text(encoding="utf-8"))
            digest(path)
            for source, expected in meta["inputs_sha256"].items():
                if digest(ROOT/source) != expected:
                    raise ValueError(f"Changed input/evidence: {source}")
        review = json.loads((out/"identity_review_manifest.json").read_text(encoding="utf-8"))
        path = out/"reviewed_camera_relative_candidates.csv"
        if digest(path) != review["output_sha256"]:
            raise ValueError("Reviewed output hash mismatch")
        if digest(ROOT/"scripts/review_nanyo_light_recovery.py") != review["implementation_sha256"]:
            raise ValueError("Review implementation changed")
        baseline = ROOT/f"results/{eid}/mapping_light_tracking/camera_relative_candidates.csv"
        recovered = verify_observations(read(baseline), read(path))
        positions = read(out/"positions_10hz.csv")
        metrics = json.loads((out/"evaluation.json").read_text(encoding="utf-8"))["summary"]
        if len(positions) != metrics["position_grid_samples"] or recovered != metrics["recovered"]:
            raise ValueError("Evaluation denominator/count mismatch")
        if not np.all(np.diff([float(r["utc_s"]) for r in positions]) > 0):
            raise ValueError("Position grid clock is not strictly increasing")
        for method in ("joint", "optical_imu_depth", "selected"):
            available = np.array([r[method+"_available"] == "True" for r in positions])
            coordinates = [[float(r[f"{method}_{axis}_m"]) for axis in ("east", "north", "up")]
                           for r in positions]
            if not np.array_equal(available, np.isfinite(coordinates).all(axis=1)):
                raise ValueError(f"Availability differs from finite coordinates: {method}")
            if available.sum() != metrics[f"new_{method}"]:
                raise ValueError(f"Position summary changed: {method}")
        proxy_meta = json.loads((ROOT/f"data/interim/{eid}/optical_detection/parent_30hz.json").read_text())
        source_stat = (ROOT/proxy_meta["source"]).stat()
        if (source_stat.st_size, source_stat.st_mtime_ns) != (
                proxy_meta["source_size"], proxy_meta["source_mtime_ns"]):
            raise ValueError("Source MP4 size/time changed")
        if proxy_meta["status"] != "completed" or proxy_meta["returncode"] != 0:
            raise ValueError("GPU proxy process did not finish successfully")
        for name in ("positions.csv", "positions_10hz.csv", "positions_at_acoustic_events.csv",
                     "stationary_rms.csv"):
            digest(out/name)
        reports.extend([out/"report.md", out.parent/"input_audit/report.md"])
        checked.append({"experiment_id": eid, "retained_additions": recovered,
                        "review_vetoes": review["review_rejections"], "position_grid": len(positions)})
    for path in reports:
        for link in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if not link.startswith(("https:", "http:", "#")) and not (path.parent/link.split("#")[0]).is_file():
                raise ValueError(f"Broken report link: {path}: {link}")
    out = ROOT/f"results/20261103_field_review_000/optical_detection/{args.run}"
    dump(out/"verification.json", {"status": "passed", "created_utc": datetime.now(UTC).isoformat(),
                                   "experiments": checked, "source_and_artifact_sha256": hashes,
                                   "verifier_sha256": sha256(Path(__file__)),
                                   "checks": ["old valid measurements unchanged", "review vetoes preserved",
                                              "actual image times and missing values", "position grids and flags",
                                              "source MP4 size/mtime unchanged", "GPU jobs completed",
                                              "report links exist", "input and review hashes match"],
                                   "accuracy_validated": False})
    print(f"PASS: {len(checked)} trials; {sum(r['retained_additions'] for r in checked)} retained additions")


if __name__ == "__main__":
    main()
