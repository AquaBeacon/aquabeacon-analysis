"""Derive camera-frame angular rates from the child fused attitude for ORB-SLAM3."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np


def angular_velocity_from_quaternions(q: np.ndarray, times_ns: np.ndarray) -> np.ndarray:
    """Return wxyz camera-frame angular velocity between consecutive fused poses."""
    if len(q) < 2 or np.any(np.diff(times_ns) <= 0):
        raise ValueError("fused attitude timestamps must increase strictly")
    previous = q[:-1].copy()
    previous[:, 1:] *= -1
    aw, ax, ay, az = previous.T
    bw, bx, by, bz = q[1:].T
    relative = np.column_stack((
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    ))
    relative[relative[:, 0] < 0] *= -1
    vector = relative[:, 1:]
    magnitude = np.linalg.norm(vector, axis=1)
    angle = 2 * np.arctan2(magnitude, relative[:, 0])
    factor = np.divide(angle, magnitude, out=np.full_like(angle, 2.0),
                       where=magnitude > 1e-10)
    return vector * (factor / (np.diff(times_ns) * 1e-9))[:, None]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare(experiment_id: str, root: Path, dataset_root: Path, output_root: Path) -> Path:
    dataset = dataset_root / experiment_id
    source_imu = dataset / "mav0/imu0/data.csv"
    fused_path = root / "results" / experiment_id / "localization/child_fused_attitude.csv"
    localization_manifest = root / "results" / experiment_id / "localization/imu_manifest.json"
    with fused_path.open(encoding="utf-8", newline="") as stream:
        fused = list(csv.DictReader(stream))
    with source_imu.open(encoding="utf-8", newline="") as stream:
        imu = list(csv.DictReader(stream))
    times = np.array([int(row["sync_utc_us"]) * 1000 for row in fused], dtype=np.int64)
    quaternions = np.array([[float(row[key]) for key in ("qw", "qx", "qy", "qz")]
                            for row in fused])
    if np.max(np.abs(np.linalg.norm(quaternions, axis=1) - 1)) > 1e-4:
        raise ValueError("fused attitude quaternions are not normalized")
    gyro_interval = angular_velocity_from_quaternions(quaternions, times)
    centers = times[:-1] + np.diff(times) // 2
    imu_times = np.array([int(row["#timestamp [ns]"]) for row in imu], dtype=np.int64)
    origin = times[0]
    gyro = np.column_stack([
        np.interp((imu_times - origin) * 1e-9, (centers - origin) * 1e-9,
                  gyro_interval[:, axis]) for axis in range(3)
    ])
    manifest = json.loads(localization_manifest.read_text(encoding="utf-8"))
    body_to_camera = np.array(manifest["child_body_to_camera_rotation_matrix"])
    if not np.allclose(body_to_camera.T @ body_to_camera, np.eye(3), atol=1e-3):
        raise ValueError("body to camera rotation is not orthogonal")
    acceleration_body = np.array([[float(row[key]) for key in
                                   ("a_RS_S_x [m s^-2]", "a_RS_S_y [m s^-2]",
                                    "a_RS_S_z [m s^-2]")] for row in imu])
    acceleration_camera = acceleration_body @ body_to_camera.T
    raw_gyro_body = np.array([[float(row[key]) for key in
                               ("w_RS_S_x [rad s^-1]", "w_RS_S_y [rad s^-1]",
                                "w_RS_S_z [rad s^-1]")] for row in imu])
    raw_gyro_camera = raw_gyro_body @ body_to_camera.T
    destination = output_root / experiment_id
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "data.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["#timestamp [ns]", "w_RS_S_x [rad s^-1]", "w_RS_S_y [rad s^-1]",
                         "w_RS_S_z [rad s^-1]", "a_RS_S_x [m s^-2]", "a_RS_S_y [m s^-2]",
                         "a_RS_S_z [m s^-2]"])
        for timestamp, angular, acceleration in zip(imu_times, gyro, acceleration_camera):
            writer.writerow([int(timestamp), *angular, *acceleration])
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                            capture_output=True, text=True, check=False)
    report = {
        "experiment_id": experiment_id,
        "method": "fused child attitude finite differences, linearly interpolated to original IMU times",
        "coordinate_frame": "DJI camera quaternion axes; optical mapping provisional",
        "absolute_attitude_used_as_prior": False,
        "fused_attitude_source": str(fused_path),
        "source_imu": str(source_imu),
        "body_to_camera_rotation_source": str(localization_manifest),
        "input_sha256": {str(item): sha256(item) for item in
                         (fused_path, source_imu, localization_manifest)},
        "output_sha256": sha256(path),
        "git_commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "imu_rows": len(imu_times),
        "fused_attitude_rows": len(times),
        "gyro_extrapolated_rows": int(np.count_nonzero(
            (imu_times < centers[0]) | (imu_times > centers[-1]))),
        "raw_vs_fused_gyro_correlation": float(np.corrcoef(
            raw_gyro_camera.ravel(), gyro.ravel())[0, 1]),
        "raw_vs_fused_gyro_rms_rad_s": float(np.sqrt(np.mean(
            (raw_gyro_camera - gyro) ** 2))),
        "limitation": "ORB-SLAM3 receives derived angular rates, not absolute fused quaternions",
    }
    (destination / "manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_id")
    parser.add_argument("--dataset-root", type=Path, default=Path("data/interim/orbslam3_full"))
    parser.add_argument("--output-root", type=Path, default=Path("data/interim/orbslam3_fused"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    print(prepare(args.experiment_id, root, args.dataset_root, args.output_root))


if __name__ == "__main__":
    main()
