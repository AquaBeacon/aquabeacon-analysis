"""Apply an explicitly hypothetical pinhole model to existing light tracks."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import subprocess
from pathlib import Path

import cv2

from .light_detection import Intrinsics, camera_angles


def assumed_intrinsics(width: int, height: int, horizontal_fov_deg: float) -> Intrinsics:
    """Derive square-pixel pinhole intrinsics from a hypothetical horizontal FOV."""
    if width <= 0 or height <= 0 or not 0 < horizontal_fov_deg < 180:
        raise ValueError("positive image dimensions and 0 < horizontal FOV < 180 required")
    focal_px = (width / 2) / math.tan(math.radians(horizontal_fov_deg / 2))
    return Intrinsics(focal_px, focal_px, width / 2, height / 2)


def provisional_angle_rows(
    source_rows: list[dict[str, str]], intrinsics: Intrinsics
) -> list[dict[str, object]]:
    """Keep untracked and prediction-only angles empty; preserve source status."""
    output = []
    for row in source_rows:
        status = row["status"]
        x, y = row["x_px"], row["y_px"]
        valid = status in {"anchor", "observed", "reacquired"} and bool(x and y)
        azimuth, elevation = (
            camera_angles(float(x), float(y), intrinsics) if valid else (None, None)
        )
        output.append({
            "video_time_s": row["video_time_s"],
            "source_frame_index": row["source_frame_index"],
            "tracking_status": status,
            "x_px": x,
            "y_px": y,
            "position_sigma_px": row["position_sigma_px"],
            "azimuth_rad": round(azimuth, 8) if azimuth is not None else "",
            "elevation_rad": round(elevation, 8) if elevation is not None else "",
            "azimuth_deg": round(math.degrees(azimuth), 4) if azimuth is not None else "",
            "elevation_deg": round(math.degrees(elevation), 4) if elevation is not None else "",
            "angle_status": "hypothetical_uncalibrated" if valid else "unavailable",
        })
    return output


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_provisional_angles(
    directory: Path, assumption_path: Path
) -> tuple[Path, Path]:
    """Write a separate per-experiment CSV and its input/assumption manifest."""
    assumption = json.loads(assumption_path.read_text(encoding="utf-8"))
    if assumption["status"] != "hypothetical_not_measured":
        raise ValueError("provisional angle assumptions must be marked hypothetical")
    tracking_csv = directory / "tracked_light.csv"
    tracking_manifest_path = directory / "tracking_manifest.json"
    tracking_manifest = json.loads(tracking_manifest_path.read_text(encoding="utf-8"))
    video = Path(tracking_manifest["video"])
    capture = cv2.VideoCapture(str(video))
    try:
        if not capture.isOpened():
            raise ValueError(f"cannot open source video: {video}")
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        capture.release()
    if [width, height] != assumption["image_size_px"]:
        raise ValueError(f"video size {width}x{height} does not match provisional assumption")
    intrinsics = assumed_intrinsics(width, height, assumption["assumed_horizontal_fov_deg"])
    with tracking_csv.open(encoding="utf-8", newline="") as stream:
        rows = provisional_angle_rows(list(csv.DictReader(stream)), intrinsics)
    if not rows:
        raise ValueError(f"empty track: {tracking_csv}")
    output_csv = directory / "provisional_light_angles.csv"
    with output_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    manifest_path = directory / "provisional_angle_manifest.json"
    manifest = {
        "experiment_id": directory.parent.name,
        "angle_status": "hypothetical_uncalibrated",
        "camera_frame": "P_MAIN_CAM; right-positive azimuth, up-positive elevation",
        "image_size_px": [width, height],
        "intrinsics_px": vars(intrinsics),
        "assumption_path": str(assumption_path),
        "assumption_sha256": _sha256(assumption_path),
        "tracking_csv": str(tracking_csv),
        "tracking_csv_sha256": _sha256(tracking_csv),
        "tracking_manifest": str(tracking_manifest_path),
        "tracking_manifest_sha256": _sha256(tracking_manifest_path),
        "source_video": str(video),
        "source_video_sha256": tracking_manifest["video_sha256"],
        "sensor_config_sha256": tracking_manifest["sensor_config_sha256"],
        "camera_setup_sha256": tracking_manifest["camera_setup_sha256"],
        "valid_angles": sum(row["angle_status"] == "hypothetical_uncalibrated" for row in rows),
        "samples": len(rows),
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "limitations": "FOV is assumed; underwater housing refraction and distortion are not calibrated",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return output_csv, manifest_path
