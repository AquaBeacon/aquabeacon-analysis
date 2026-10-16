"""Measure child-only ORB-SLAM3 frame coverage against the exported camera timeline."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from bisect import bisect_left
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def match_pose_indices(frame_ns: list[int], pose_ns: list[int], tolerance_ns: int = 2_000_000
                       ) -> set[int]:
    """Associate floating point EuRoC output times to integer nanosecond inputs."""
    matches = set()
    for timestamp in pose_ns:
        index = bisect_left(frame_ns, timestamp)
        candidates = [item for item in (index - 1, index) if 0 <= item < len(frame_ns)]
        if not candidates:
            continue
        nearest = min(candidates, key=lambda item: abs(frame_ns[item] - timestamp))
        if abs(frame_ns[nearest] - timestamp) <= tolerance_ns:
            matches.add(nearest)
    return matches


def pose_lookup(frame_ns: list[int], pose_rows: list[list[str]],
                tolerance_ns: int = 2_000_000) -> dict[int, list[str]]:
    """Choose the nearest input frame for each saved pose."""
    lookup = {}
    for row in pose_rows:
        timestamp = round(float(row[0]))
        index = bisect_left(frame_ns, timestamp)
        candidates = [item for item in (index - 1, index) if 0 <= item < len(frame_ns)]
        if candidates:
            nearest = min(candidates, key=lambda item: abs(frame_ns[item] - timestamp))
            if abs(frame_ns[nearest] - timestamp) <= tolerance_ns:
                lookup[nearest] = row[1:]
    return lookup


def gaps(matched: set[int], count: int) -> list[dict[str, int]]:
    missing = sorted(set(range(count)) - matched)
    spans = []
    for index in missing:
        if spans and spans[-1]["end_index_exclusive"] == index:
            spans[-1]["end_index_exclusive"] = index + 1
        else:
            spans.append({"start_index": index, "end_index_exclusive": index + 1})
    return spans


def summarize(dataset: Path, run: Path) -> dict:
    manifest_path = run / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    timestamps_path = dataset / "timestamps.txt"
    all_times = [int(line) for line in timestamps_path.read_text(encoding="utf-8").splitlines()]
    start = max(0, int(manifest["start_image"]))
    end = int(manifest["end_image_exclusive"])
    if end < 0:
        end = len(all_times)
    frame_ns = all_times[start:end]
    trajectory = run / "CameraTrajectory.txt"
    if trajectory.exists():
        pose_rows = [line.split() for line in trajectory.read_text(encoding="utf-8").splitlines()
                     if line.strip()]
    else:
        pose_rows = []
    lookup = pose_lookup(frame_ns, pose_rows)
    matched = set(lookup)
    log = (run / "run.log").read_text(encoding="utf-8", errors="replace")
    map_counts = re.findall(r"There are (\d+) maps? in the atlas", log)
    output_csv = run / "camera_trajectory_status.csv"
    with output_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("utc_ns", "pose_status", "x_slam", "y_slam", "z_slam",
                         "qx", "qy", "qz", "qw"))
        for index, timestamp in enumerate(frame_ns):
            pose = lookup.get(index)
            writer.writerow((timestamp, "saved" if pose else "not_saved", *(pose or [""] * 7)))
    spans = gaps(matched, len(frame_ns))
    result = {
        "experiment_id": manifest["experiment_id"],
        "sensor_mode": manifest["sensor_mode"],
        "run": str(run),
        "settings_sha256": manifest["settings_sha256"],
        "source_export_manifest_sha256": sha256(dataset / "export_manifest.json"),
        "camera_timestamps_sha256": sha256(timestamps_path),
        "trajectory_sha256": sha256(trajectory) if trajectory.exists() else None,
        "git_commit": manifest["git_commit"],
        "exit_code": manifest.get("exit_code"),
        "start_image_index_in_export": start,
        "end_image_index_exclusive_in_export": end,
        "input_frames": len(frame_ns),
        "trajectory_rows": len(pose_rows),
        "matched_frames": len(matched),
        "duplicate_pose_frame_rows": len(pose_rows) - len(matched),
        "duplicate_pose_policy": "last saved pose for a repeated frame; raw trajectory retained",
        "pose_export_scope": "ORB-SLAM3 EuRoC export includes only its largest Atlas map",
        "coverage_fraction": len(matched) / len(frame_ns),
        "final_atlas_map_count": int(map_counts[-1]) if map_counts else None,
        "tracking_failure_log_lines": log.count("Fail to track local map!"),
        "continuous_full_sequence": len(matched) == len(frame_ns) and
        bool(map_counts) and int(map_counts[-1]) == 1,
        "missing_frame_spans": spans,
        "missing_span_interpretation": "no pose in exported largest map; not exact tracking loss",
        "longest_missing_span_frames": max((s["end_index_exclusive"] - s["start_index"]
                                             for s in spans), default=0),
        "monocular_scale_known": False,
        "usable_for_metric_position": False,
        "absolute_accuracy_evaluated": False,
        "trajectory_status_csv": str(output_csv),
    }
    (run / "coverage_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_id")
    parser.add_argument("run_name")
    parser.add_argument("--dataset-root", type=Path, default=Path("data/interim/orbslam3_full"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = summarize(root / args.dataset_root / args.experiment_id,
                       root / "results" / args.experiment_id / "vislam_depth" / args.run_name)
    print(json.dumps({key: result[key] for key in ("input_frames", "trajectory_rows",
                                                "matched_frames", "coverage_fraction",
                                                "longest_missing_span_frames")}, indent=2))


if __name__ == "__main__":
    main()
