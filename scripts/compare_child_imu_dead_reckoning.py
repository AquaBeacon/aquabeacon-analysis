"""Compare child-body IMU dead reckoning with acoustic/light displacement."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.imu_localization import integrate_acceleration

GRAVITY_M_S2 = 9.80665


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def keep_strictly_increasing(times_s: np.ndarray) -> np.ndarray:
    """Retain input order and mark every non-increasing timestamp for exclusion."""
    previous_max = np.r_[-np.inf, np.maximum.accumulate(times_s[:-1])]
    return times_s > previous_max


def integrate_child_from_start(
    times_s: np.ndarray, acceleration_m_s2: np.ndarray,
    quaternions_xyzw: np.ndarray, start_s: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Rotate measured specific force to child world, subtract gravity, integrate from zero velocity."""
    if not (times_s[0] <= start_s < times_s[-1]):
        raise ValueError("start outside child IMU coverage")
    world_acceleration = Rotation.from_quat(quaternions_xyzw).apply(acceleration_m_s2)
    linear = world_acceleration - np.array([0.0, 0.0, GRAVITY_M_S2])
    first = np.searchsorted(times_s, start_s, side="right")
    integration_times = np.r_[start_s, times_s[first:]]
    start_acceleration = np.array([
        np.interp(start_s, times_s, linear[:, axis]) for axis in range(3)
    ])
    integration_acceleration = np.vstack([start_acceleration, linear[first:]])
    velocity, position = integrate_acceleration(integration_times, integration_acceleration)
    return integration_times, velocity, position


def compare(experiment: str, root: Path) -> dict[str, object]:
    result = root / "results" / experiment
    acoustic_path = result / "relative_position" / "parent_child_position.csv"
    acoustic_manifest = result / "relative_position" / "manifest.json"
    imu_path = next((root / "data" / "processed" / experiment / "tof_window" / "c_main").glob("C_MAIN_IMU_100Hz_*.csv"))
    imu_manifest = result / "localization" / "imu_manifest.json"
    acoustic = read_rows(acoustic_path)
    observed = [row for row in acoustic if row["relative_gravity_x_m"] and row["range_qc"] == "reported_valid"]
    if len(observed) < 2:
        raise ValueError(f"too few acoustic observations: {experiment}")
    start_s = float(observed[0]["window_time_s"])
    baseline = np.array([float(observed[0][k]) for k in (
        "relative_gravity_x_m", "relative_gravity_y_m", "relative_gravity_z_up_m")])
    imu = read_rows(imu_path)
    raw_times = np.array([float(row["window_time_s"]) for row in imu])
    keep = keep_strictly_increasing(raw_times)
    imu = [row for row, valid in zip(imu, keep, strict=True) if valid]
    times = raw_times[keep]
    acceleration = np.array([[float(row[k]) for k in ("ax", "ay", "az")] for row in imu])
    quaternions = np.array([[float(row[k]) for k in ("qx", "qy", "qz", "qw")] for row in imu])
    integration_times, velocity, position = integrate_child_from_start(
        times, acceleration, quaternions, start_s
    )
    output = []
    for row in acoustic:
        if not row["relative_gravity_x_m"]:
            continue
        time_s = float(row["window_time_s"])
        if not start_s <= time_s <= integration_times[-1]:
            continue
        acoustic_xyz = np.array([float(row[k]) for k in (
            "relative_gravity_x_m", "relative_gravity_y_m", "relative_gravity_z_up_m")])
        acoustic_delta = acoustic_xyz - baseline
        imu_delta = np.array([
            np.interp(time_s, integration_times, position[:, axis]) for axis in range(3)
        ])
        imu_velocity = np.array([
            np.interp(time_s, integration_times, velocity[:, axis]) for axis in range(3)
        ])
        output.append({
            "experiment_id": experiment, "window_time_s": row["window_time_s"],
            "elapsed_s": f"{time_s - start_s:.6f}", "video_time_s": row["video_time_s"],
            "acoustic_status": row["range_qc"], "acoustic_range_m": row["range_m"],
            "acoustic_dx_m": f"{acoustic_delta[0]:.6f}",
            "acoustic_dy_m": f"{acoustic_delta[1]:.6f}",
            "acoustic_dz_up_m": f"{acoustic_delta[2]:.6f}",
            "acoustic_displacement_m": f"{np.linalg.norm(acoustic_delta):.6f}",
            "imu_dx_m": f"{imu_delta[0]:.6f}",
            "imu_dy_m": f"{imu_delta[1]:.6f}",
            "imu_dz_up_m": f"{imu_delta[2]:.6f}",
            "imu_displacement_m": f"{np.linalg.norm(imu_delta):.6f}",
            "imu_speed_m_s": f"{np.linalg.norm(imu_velocity):.6f}",
            "displacement_magnitude_gap_m": f"{abs(np.linalg.norm(imu_delta) - np.linalg.norm(acoustic_delta)):.6f}",
        })
    if not output:
        raise ValueError(f"no common observations: {experiment}")
    directory = result / "relative_position"
    output_path = directory / "child_imu_comparison.csv"
    with output_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(output[0]))
        writer.writeheader()
        writer.writerows(output)
    last_normal = next(row for row in reversed(output) if row["acoustic_status"] == "reported_valid")
    manifest = {
        "experiment_id": experiment,
        "method": "child C_MAIN body quaternion rotates specific force to its world frame; subtract 9.80665 m/s^2 in +z; trapezoidal double integration",
        "initial_window_time_s": start_s,
        "initial_position_m": [0, 0, 0], "initial_velocity_m_s": [0, 0, 0],
        "accelerometer_bias_correction": "none",
        "attitude_source": "C_MAIN body IMU quaternion, not fused child camera attitude",
        "imu_world_frame": "child gravity-aligned frame; horizontal yaw relative to parent frame uncalibrated",
        "acoustic_frame": "parent gravity frame conditional on optical-to-IMU identity",
        "comparison_metric": "displacement magnitudes since first common valid acoustic observation; magnitude gap is not position error",
        "parent_motion_assumption": "parent fixed; no GPS antenna-to-body translation applied",
        "acoustic_qc": "short (<0.2m) and spike (>5m) ToF rows retained and marked; first anchor is reported_valid",
        "imu_rows_input": len(raw_times), "imu_rows_used": len(times),
        "nonincreasing_imu_rows_excluded": int((~keep).sum()),
        "common_rows": len(output),
        "last_normal_time_s": float(last_normal["window_time_s"]),
        "last_normal_acoustic_displacement_m": float(last_normal["acoustic_displacement_m"]),
        "last_normal_imu_displacement_m": float(last_normal["imu_displacement_m"]),
        "input_sha256": {str(path.relative_to(root)): sha256(path) for path in (
            acoustic_path, acoustic_manifest, imu_path, imu_manifest
        )},
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
    }
    (directory / "child_imu_comparison_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="append", dest="sessions")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    for experiment in args.sessions or (
        "20260922_cmain_022", "20260922_cmain_023", "20260922_cmain_024"
    ):
        manifest = compare(experiment, root)
        print(experiment, manifest["common_rows"], manifest["last_normal_imu_displacement_m"])


if __name__ == "__main__":
    main()
