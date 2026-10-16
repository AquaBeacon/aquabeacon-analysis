"""Convert provisional P_MAIN light rays to gravity-frame bearings using fused attitude."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import subprocess
from bisect import bisect_right
from pathlib import Path

import cv2
import numpy as np


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def match_first_frame(source: Path, cut: Path, requested_offset_s: float) -> tuple[int, float]:
    """Locate cut frame zero in the source; stream copy can start before the request."""
    original = cv2.VideoCapture(str(source))
    trimmed = cv2.VideoCapture(str(cut))
    try:
        ok, frame = trimmed.read()
        if not ok or not original.isOpened():
            raise ValueError("cannot read source or cut video")
        target = cv2.resize(frame, (160, 90)).astype(np.int16)
        fps = original.get(cv2.CAP_PROP_FPS)
        if fps <= 0:
            raise ValueError("source frame rate unavailable")
        first = max(0, math.floor((requested_offset_s - 3) * fps))
        last = math.ceil((requested_offset_s + 0.2) * fps)
        original.set(cv2.CAP_PROP_POS_FRAMES, first)
        best = (float("inf"), -1)
        for index in range(first, last + 1):
            ok, candidate = original.read()
            if not ok:
                break
            reduced = cv2.resize(candidate, (160, 90)).astype(np.int16)
            error = float(np.mean(np.abs(reduced - target)))
            if error < best[0]:
                best = error, index
        if best[0] > 1:
            raise ValueError(f"first frame could not be matched: mean pixel difference {best[0]}")
        return best[1], fps
    finally:
        original.release()
        trimmed.release()


def _normalized(q: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(q)
    if not np.isfinite(norm) or norm < 0.5:
        raise ValueError("invalid attitude quaternion")
    return q / norm


def interpolate_attitude(
    times_s: np.ndarray, quaternions: np.ndarray, target_s: float,
    max_gap_s: float = 0.15,
) -> np.ndarray | None:
    """Shortest-arc SLERP; refuse missing intervals and extrapolation."""
    if target_s < times_s[0] or target_s > times_s[-1]:
        return None
    hi = bisect_right(times_s, target_s)
    if hi == len(times_s):
        return _normalized(quaternions[-1])
    if hi == 0:
        return None
    lo = hi - 1
    gap = times_s[hi] - times_s[lo]
    if gap <= 0 or gap > max_gap_s:
        return None
    fraction = (target_s - times_s[lo]) / gap
    a, b = _normalized(quaternions[lo]), _normalized(quaternions[hi])
    dot = float(np.dot(a, b))
    if dot < 0:
        b, dot = -b, -dot
    dot = min(dot, 1.0)
    if dot > 0.9995:
        return _normalized(a + fraction * (b - a))
    theta = math.acos(dot)
    result = (math.sin((1 - fraction) * theta) * a + math.sin(fraction * theta) * b)
    return result / math.sin(theta)


def rotate(q: np.ndarray, vector: np.ndarray) -> np.ndarray:
    """Rotate a sensor-frame vector into the gravity-aligned local frame."""
    xyz = q[1:]
    uv = np.cross(xyz, vector)
    return vector + 2 * (q[0] * uv + np.cross(xyz, uv))


def stabilized_angles(
    azimuth_rad: float, elevation_rad: float, attitude: np.ndarray,
    optical_to_imu: np.ndarray, initial_forward_horizontal: np.ndarray,
) -> tuple[float, float]:
    """Return initial-heading-relative azimuth and gravity-referenced elevation."""
    nx = math.tan(azimuth_rad)
    optical = np.array([nx, -math.tan(elevation_rad) * math.hypot(1, nx), 1.0])
    optical /= np.linalg.norm(optical)
    world = rotate(attitude, optical_to_imu @ optical)
    right = np.cross(initial_forward_horizontal, np.array([0.0, 0.0, 1.0]))
    azimuth = math.atan2(np.dot(world, right), np.dot(world, initial_forward_horizontal))
    elevation = math.asin(float(np.clip(world[2], -1, 1)))
    return azimuth, elevation


def _attitude_data(path: Path) -> tuple[np.ndarray, np.ndarray]:
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    times = np.array([float(row["window_time_s"]) for row in rows])
    quaternions = np.array([
        [float(row[key]) for key in ("qw", "qx", "qy", "qz")] for row in rows
    ])
    if not len(rows) or np.any(np.diff(times) <= 0):
        raise ValueError("parent fused attitude times must increase")
    return times, quaternions


def export_attitude_angles(experiment_id: str, root: Path) -> tuple[Path, Path]:
    result = root / "results" / experiment_id
    directory = result / "light_detection"
    provisional_csv = directory / "provisional_light_angles.csv"
    attitude_csv = result / "localization" / "parent_fused_attitude.csv"
    preprocessing_path = result / "preprocessing" / "manifest.json"
    mount_path = root / "calibration" / "parent_camera_imu_provisional.json"
    intrinsic_path = root / "calibration" / "parent_camera_provisional.json"
    preprocessing = json.loads(preprocessing_path.read_text(encoding="utf-8"))
    mount = json.loads(mount_path.read_text(encoding="utf-8"))
    if mount["status"] != "hypothetical_not_measured":
        raise ValueError("mounting rotation must be explicitly marked provisional")
    matrix = np.asarray(mount["optical_to_imu_rotation_matrix"], dtype=float)
    if matrix.shape != (3, 3) or not np.allclose(matrix.T @ matrix, np.eye(3), atol=1e-8):
        raise ValueError("optical-to-IMU rotation must be orthonormal")
    if not math.isclose(float(np.linalg.det(matrix)), 1, abs_tol=1e-8):
        raise ValueError("optical-to-IMU rotation must be proper")
    video = next(
        v for v in preprocessing["video_outputs"]
        if "P_MAIN" in v["source"] and "proxy" in v["source"]
    )
    source, cut = root / video["source"], root / video["output"]
    source_index, fps = match_first_frame(source, cut, video["source_offset_s"])
    video_start_window_s = source_index / fps - video["source_offset_s"]
    times, quaternions = _attitude_data(attitude_csv)
    reference = rotate(_normalized(quaternions[0]), matrix @ np.array([0.0, 0.0, 1.0]))
    forward = np.array([reference[0], reference[1], 0.0])
    if np.linalg.norm(forward) < 0.1:
        raise ValueError("initial optical axis is too close to vertical for heading reference")
    forward /= np.linalg.norm(forward)
    with provisional_csv.open(encoding="utf-8", newline="") as stream:
        source_rows = list(csv.DictReader(stream))
    output_rows = []
    for row in source_rows:
        time_s = video_start_window_s + float(row["video_time_s"])
        output = {
            "video_time_s": row["video_time_s"],
            "window_time_s": round(time_s, 6),
            "source_frame_index": row["source_frame_index"],
            "tracking_status": row["tracking_status"],
            "x_px": row["x_px"], "y_px": row["y_px"],
            "camera_azimuth_rad": row["azimuth_rad"],
            "camera_elevation_rad": row["elevation_rad"],
            "local_azimuth_rad": "", "gravity_elevation_rad": "",
            "local_azimuth_deg": "", "gravity_elevation_deg": "",
            "angle_status": "no_light_angle",
        }
        if row["angle_status"] == "hypothetical_uncalibrated":
            q = interpolate_attitude(times, quaternions, time_s)
            if q is None:
                output["angle_status"] = "no_parent_attitude"
            else:
                azimuth, elevation = stabilized_angles(
                    float(row["azimuth_rad"]), float(row["elevation_rad"]),
                    q, matrix, forward,
                )
                output.update({
                    "local_azimuth_rad": round(azimuth, 8),
                    "gravity_elevation_rad": round(elevation, 8),
                    "local_azimuth_deg": round(math.degrees(azimuth), 4),
                    "gravity_elevation_deg": round(math.degrees(elevation), 4),
                    "angle_status": "conditional_mount_and_fov",
                })
        output_rows.append(output)
    if not output_rows:
        raise ValueError("no provisional light angle rows")
    output_path = directory / "attitude_corrected_light_angles.csv"
    with output_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(output_rows[0]))
        writer.writeheader()
        writer.writerows(output_rows)
    manifest_path = directory / "attitude_angle_manifest.json"
    manifest = {
        "experiment_id": experiment_id,
        "frame": "parent local gravity frame, +Z up; horizontal azimuth relative to first optical heading",
        "status": "conditional_mount_and_fov; no absolute compass heading",
        "video_start_window_s": video_start_window_s,
        "first_frame_source_index": source_index,
        "first_frame_source_fps": fps,
        "time_alignment": (
            "cut frame 0 matched exactly to interim source; source t=0 is assumed "
            "to coincide with first refined camera telemetry row"
        ),
        "angle_counts": {status: sum(r["angle_status"] == status for r in output_rows)
                         for status in ("conditional_mount_and_fov", "no_parent_attitude",
                                        "no_light_angle")},
        "input_sha256": {str(p.relative_to(root)): _sha256(p) for p in
                         (provisional_csv, attitude_csv, preprocessing_path,
                          mount_path, intrinsic_path, source, cut)},
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "limitations": [
            "90-degree horizontal FOV is hypothetical; underwater case not calibrated",
            "optical-to-IMU rotation is hypothetical, not measured",
            "horizontal heading is relative to the first fused attitude, not ENU or north",
            "source-video/telemetry origin correspondence has not been independently calibrated",
            "fused quaternion agreement is not ground-truth angular accuracy",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return output_path, manifest_path
