"""Fuse provisional light bearings with corrected acoustic ranges per experiment."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import subprocess
from pathlib import Path

import cv2
import numpy as np
from scipy.spatial.transform import Rotation, Slerp


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unit_ray(azimuth_rad: float, elevation_rad: float) -> np.ndarray:
    """Optical x right, y down, z forward; elevation is up positive."""
    x = math.tan(azimuth_rad)
    y = -math.tan(elevation_rad) * math.hypot(1.0, x)
    ray = np.array([x, y, 1.0])
    return ray / np.linalg.norm(ray)


def match_video_origin(source: Path, trimmed: Path, requested_offset_s: float) -> tuple[float, float]:
    """Find the source timestamp of the first stream-copied video frame."""
    cut = cv2.VideoCapture(str(trimmed))
    original = cv2.VideoCapture(str(source))
    try:
        ok, frame = cut.read()
        if not ok:
            raise ValueError(f"cannot read cut video: {trimmed}")
        target = cv2.resize(frame, (160, 90)).astype(np.int16)
        fps = original.get(cv2.CAP_PROP_FPS)
        if fps <= 0:
            raise ValueError(f"invalid source FPS: {source}")
        # ffmpeg stream-copy seeks to a preceding keyframe, normally within 2 s.
        first = max(0, math.floor((requested_offset_s - 3) * fps))
        last = math.ceil((requested_offset_s + 0.2) * fps)
        original.set(cv2.CAP_PROP_POS_FRAMES, first)
        best = (float("inf"), 0.0)
        for index in range(first, last + 1):
            ok, candidate = original.read()
            if not ok:
                break
            difference = float(np.mean(np.abs(cv2.resize(candidate, (160, 90)).astype(np.int16) - target)))
            if difference < best[0]:
                best = difference, index / fps
        if best[0] > 1.0:
            raise ValueError(f"cut/source first frame mismatch: {best[0]:.3f}: {trimmed}")
        return best[1], best[0]
    finally:
        cut.release()
        original.release()


def interpolate_attitude(rows: list[dict[str, str]], time_s: float) -> Rotation | None:
    times = np.array([float(row["window_time_s"]) for row in rows])
    index = int(np.searchsorted(times, time_s))
    if index == 0 or index == len(times):
        return None
    lo, hi = rows[index - 1], rows[index]
    if float(hi["window_time_s"]) - float(lo["window_time_s"]) > 0.15:
        return None
    quats = np.array([[float(row[k]) for k in ("qx", "qy", "qz", "qw")] for row in (lo, hi)])
    return Slerp(times[index - 1:index + 1], Rotation.from_quat(quats))([time_s])[0]


def nearest_range(rows: list[dict[str, str]], time_s: float, tolerance_s: float = 0.75) -> dict[str, str] | None:
    valid = [row for row in rows if row["tof_valid"] == "1" and math.isfinite(float(row["range_corrected_m"]))]
    if not valid:
        return None
    best = min(valid, key=lambda row: abs(float(row["window_time_s"]) - time_s))
    return best if abs(float(best["window_time_s"]) - time_s) <= tolerance_s else None


def interpolate_gps(rows: list[dict[str, str]], time_s: float) -> tuple[float, float] | None:
    times = np.array([float(row["window_time_s"]) for row in rows])
    index = int(np.searchsorted(times, time_s))
    if index == 0 or index == len(times):
        return None
    lo, hi = rows[index - 1], rows[index]
    if lo["status"] != "gps_interpolated" or hi["status"] != "gps_interpolated" or lo["segment_id"] != hi["segment_id"]:
        return None
    fraction = (time_s - times[index - 1]) / (times[index] - times[index - 1])
    return tuple(float(lo[k]) + fraction * (float(hi[k]) - float(lo[k])) for k in ("east_m", "north_m"))


def estimate(experiment: str, root: Path) -> dict[str, object]:
    result = root / "results" / experiment
    light = result / "light_detection" / "attitude_corrected_light_angles.csv"
    range_csv = result / "tof_depth_comparison" / "paired_observations.csv"
    attitude_csv = result / "localization" / "parent_fused_attitude.csv"
    gps_csv = result / "localization" / "parent_gps_position.csv"
    preprocessing = result / "preprocessing" / "manifest.json"
    assumption = root / "calibration" / "parent_camera_provisional.json"
    inputs = [light, result / "light_detection" / "attitude_angle_manifest.json",
              range_csv, attitude_csv, gps_csv, preprocessing, assumption,
              root / "calibration" / "parent_camera_imu_provisional.json"]
    manifest = json.loads(preprocessing.read_text(encoding="utf-8"))
    video = next(v for v in manifest["video_outputs"] if "P_MAIN" in v["source"] and "proxy" in v["source"])
    source = root / video["source"]
    cut = root / video["output"]
    source_start_s, match_error = match_video_origin(source, cut, video["source_offset_s"])
    inputs.append(source)
    inputs.append(cut)
    # Source video t=0 is the first refined camera telemetry row. The requested
    # source offset is measured from that row, even when stream copy starts early.
    video_start_window_s = source_start_s - video["source_offset_s"]
    angles = read_rows(light)
    ranges = read_rows(range_csv)
    gps = read_rows(gps_csv)
    output_rows = []
    counts = {"angle_available": 0, "range_matched": 0, "attitude_available": 0, "gps_available": 0}
    for angle in angles:
        video_time_s = float(angle["video_time_s"])
        time_s = video_start_window_s + video_time_s
        if abs(time_s - float(angle["window_time_s"])) > 0.02:
            raise ValueError("light angle and video window clocks disagree")
        record: dict[str, object] = {
            "experiment_id": experiment, "video_time_s": f"{video_time_s:.6f}",
            "window_time_s": f"{time_s:.6f}",
            "sync_utc_us": round(manifest["window_start_utc_us"] + time_s * 1e6),
            "light_status": angle["tracking_status"], "angle_status": angle["angle_status"],
            "x_px": angle["x_px"], "y_px": angle["y_px"],
            "azimuth_rad": angle["camera_azimuth_rad"],
            "elevation_rad": angle["camera_elevation_rad"],
            "local_azimuth_rad": angle["local_azimuth_rad"],
            "gravity_elevation_rad": angle["gravity_elevation_rad"],
            "range_time_delta_s": "", "range_m": "", "tof_us": "", "range_qc": "",
            "relative_camera_x_right_m": "", "relative_camera_y_down_m": "", "relative_camera_z_forward_m": "",
            "relative_gravity_x_m": "", "relative_gravity_y_m": "", "relative_gravity_z_up_m": "",
            "parent_gps_east_m": "", "parent_gps_north_m": "",
            "child_east_yaw0_m": "", "child_north_yaw0_m": "",
            "position_status": "no_light_angle",
        }
        if angle["angle_status"] != "conditional_mount_and_fov":
            output_rows.append(record)
            continue
        counts["angle_available"] += 1
        range_row = nearest_range(ranges, time_s)
        if range_row is None:
            record["position_status"] = "no_nearby_valid_tof"
            output_rows.append(record)
            continue
        counts["range_matched"] += 1
        distance = float(range_row["range_corrected_m"])
        vector = distance * unit_ray(float(angle["camera_azimuth_rad"]),
                                     float(angle["camera_elevation_rad"]))
        record.update({"range_time_delta_s": f"{float(range_row['window_time_s']) - time_s:.6f}",
                       "range_m": f"{distance:.6f}", "tof_us": range_row["tof_us"],
                       "range_qc": "inspect_short_or_spike" if distance < 0.2 or distance > 5.0 else "reported_valid",
                       "relative_camera_x_right_m": f"{vector[0]:.6f}",
                       "relative_camera_y_down_m": f"{vector[1]:.6f}",
                       "relative_camera_z_forward_m": f"{vector[2]:.6f}"})
        record["position_status"] = "camera_relative_only"
        counts["attitude_available"] += 1
        az = float(angle["local_azimuth_rad"])
        el = float(angle["gravity_elevation_rad"])
        gravity = distance * np.array([math.cos(el) * math.cos(az),
                                       math.cos(el) * math.sin(az), math.sin(el)])
        record.update({"relative_gravity_x_m": f"{gravity[0]:.6f}",
                       "relative_gravity_y_m": f"{gravity[1]:.6f}",
                       "relative_gravity_z_up_m": f"{gravity[2]:.6f}"})
        record["position_status"] = "fixed_parent_gravity_frame_conditional"
        parent_xy = interpolate_gps(gps, time_s)
        if parent_xy is not None:
            counts["gps_available"] += 1
            record.update({"parent_gps_east_m": f"{parent_xy[0]:.6f}",
                           "parent_gps_north_m": f"{parent_xy[1]:.6f}",
                           "child_east_yaw0_m": f"{parent_xy[0] + gravity[0]:.6f}",
                           "child_north_yaw0_m": f"{parent_xy[1] + gravity[1]:.6f}"})
            record["position_status"] = "gps_anchored_yaw0_conditional"
        output_rows.append(record)
    directory = result / "relative_position"
    directory.mkdir(parents=True, exist_ok=True)
    output_csv = directory / "parent_child_position.csv"
    with output_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(output_rows[0]))
        writer.writeheader()
        writer.writerows(output_rows)
    metadata = {
        "experiment_id": experiment, "rows": len(output_rows), "counts": counts,
        "video_start_window_s": video_start_window_s,
        "first_frame_source_time_s": source_start_s, "first_frame_match_mean_abs_pixel": match_error,
        "range_max_abs_time_delta_s": 0.75,
        "frame_camera": "+x image right, +y image down, +z optical forward; origin at P_MAIN optical center",
        "frame_gravity": "first optical heading in parent local gravity frame; +z up; hypothetical camera-to-IMU mount and FOV",
        "frame_gps": "ENU relative to SONY first GPS fix; parent is SONY antenna, not calibrated body origin",
        "yaw0_assumption": "gravity frame x/y are treated as east/north only for the illustrated child_east_yaw0/north_yaw0 columns; true yaw is unknown",
        "vertical_status": "relative_gravity_z_up is conditional; parent absolute up unavailable; no child absolute height",
        "range_status": "one-way ToF and salinity 35 ppt assumed; no transducer delay or camera-light lever-arm correction",
        "range_qc_rule": "valid ToF is retained; <0.2 m or >5 m is flagged inspect_short_or_spike, not discarded",
        "angle_status": "attitude_corrected_light_angles.csv; 90-degree horizontal FOV and camera-to-IMU mount hypothetical",
        "input_sha256": {str(path.relative_to(root)): sha256(path) for path in inputs},
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
    }
    (directory / "manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="append", dest="sessions")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sessions = args.sessions or ["20260922_cmain_022", "20260922_cmain_023", "20260922_cmain_024"]
    for session in sessions:
        print(json.dumps(estimate(session, root)["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
