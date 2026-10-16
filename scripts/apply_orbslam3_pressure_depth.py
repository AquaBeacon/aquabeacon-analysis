"""Replace VI-SLAM vertical positions with synchronized relative pressure depth."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from bisect import bisect_right
from decimal import Decimal
from itertools import pairwise
from pathlib import Path


def pressure_at(pressure: list[tuple[int, float]], timestamp_ns: int,
                max_gap_ns: int = 1_000_000_000) -> float | None:
    times = [row[0] for row in pressure]
    right = bisect_right(times, timestamp_ns)
    if right == 0 or right == len(times):
        if right and times[-1] == timestamp_ns:
            return pressure[-1][1]
        return None
    t0, d0 = pressure[right - 1]
    t1, d1 = pressure[right]
    if t1 - t0 > max_gap_ns:
        return None
    fraction = (timestamp_ns - t0) / (t1 - t0)
    return d0 + fraction * (d1 - d0)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply(dataset: Path, trajectory: Path, settings: Path, output: Path) -> dict:
    pressure_path = dataset / "mav0/pressure0/data.csv"
    timestamps_path = dataset / "timestamps.txt"
    run_manifest_path = trajectory.parent / "run_manifest.json"
    run_manifest = (json.loads(run_manifest_path.read_text(encoding="utf-8"))
                    if run_manifest_path.exists() else {})
    if run_manifest and not run_manifest.get("usable_vi_trajectory", False):
        raise ValueError("run manifest does not verify a usable inertial VI trajectory")
    with pressure_path.open(encoding="utf-8", newline="") as stream:
        pressure = sorted((int(row["utc_ns"]), float(row["relative_depth_m"]))
                          for row in csv.DictReader(stream))
    if len(pressure) < 2 or any(b[0] <= a[0] for a, b in pairwise(pressure)):
        raise ValueError("pressure timestamps must be strictly increasing")
    all_times = timestamps_path.read_text(encoding="utf-8").splitlines()
    start = int(run_manifest.get("start_image", -1))
    end = int(run_manifest.get("end_image_exclusive", -1))
    expected = {int(line) for line in (all_times[start:end] if start >= 0 else all_times)}
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for line in trajectory.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) != 8:
            raise ValueError("expected EuRoC trajectory timestamp, xyz, quaternion")
        stamp = int(Decimal(fields[0]))
        depth = pressure_at(pressure, stamp)
        if depth is None:
            continue
        rows.append((stamp, *(float(value) for value in fields[1:]), depth))
    if not rows:
        raise ValueError("no trajectory poses overlap valid pressure intervals")
    anchor_z = rows[0][3]
    anchor_depth = rows[0][8]
    destination = output / "pressure_corrected_trajectory.csv"
    with destination.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["utc_ns", "slam_x_m", "slam_y_m", "slam_z_m", "pressure_depth_m",
                         "corrected_x_m", "corrected_y_m", "corrected_z_m",
                         "qx", "qy", "qz", "qw"])
        for stamp, x, y, z, qx, qy, qz, qw, depth in rows:
            writer.writerow([stamp, x, y, z, depth, x, y,
                             anchor_z - (depth - anchor_depth), qx, qy, qz, qw])
    root = Path(__file__).resolve().parents[1]
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                            capture_output=True, text=True, check=False)
    result = {
        "experiment_id": dataset.name,
        "test_only": bool(run_manifest.get("test_only") or
                          json.loads((dataset / "export_manifest.json").read_text(encoding="utf-8"))
                          ["test_only"]),
        "git_commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "inputs_sha256": {str(path): sha256(path) for path in
                          (trajectory, pressure_path, timestamps_path, settings)},
        "input_frames": len(expected),
        "slam_poses": len(trajectory.read_text(encoding="utf-8").splitlines()),
        "pressure_corrected_poses": len(rows),
        "missing_or_unlocalized_frames": len(expected - {row[0] for row in rows}),
        "method": "vertical replacement with linearly interpolated relative pressure depth",
        "vertical_reference": "first overlapping SLAM pose; pressure relative to first log reading",
        "pressure_max_interpolation_gap_s": 1.0,
        "assumptions": ["SLAM world z is up after IMU initialization",
                        "pressure sensor and camera vertical lever arm is zero"],
        "absolute_accuracy_claimed": False,
    }
    (output / "pressure_correction_manifest.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_id")
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path, default=Path("data/interim/orbslam3"))
    parser.add_argument("--run-name", help="ORB-SLAM3 output subdirectory for a segment pilot")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    dataset = root / args.dataset_root / args.experiment_id
    output = root / "results" / args.experiment_id / "vislam_depth"
    run = output / (args.run_name or ("pilot_orbslam3_raw" if args.pilot else "orbslam3_raw"))
    trajectory = run / "CameraTrajectory.txt"
    correction_name = (f"{args.run_name}_pressure_depth" if args.run_name else
                       ("pilot_pressure_depth" if args.pilot else "pressure_depth"))
    result = apply(dataset, trajectory, args.settings, output / correction_name)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
