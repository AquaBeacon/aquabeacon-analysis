"""Detect the child lamp in parent camera footage.

Image coordinates refer to encoded (unrotated) video pixels, with origin at top left.
Angles are camera-relative and require measured camera intrinsics.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass(frozen=True)
class Intrinsics:
    fx: float
    fy: float
    cx: float
    cy: float


@dataclass(frozen=True)
class Candidate:
    x: float
    y: float
    area: int
    contrast: float
    orange_distance: float | None
    water_fraction: float
    score: float


def camera_angles(x: float, y: float, intrinsics: Intrinsics) -> tuple[float, float]:
    """Return right-positive azimuth and up-positive elevation in radians.

    This is the undistorted pinhole model. Supply coordinates corrected for lens
    distortion when using a camera with significant distortion.
    """
    if intrinsics.fx <= 0 or intrinsics.fy <= 0:
        raise ValueError("focal lengths must be positive")
    nx = (x - intrinsics.cx) / intrinsics.fx
    ny = (intrinsics.cy - y) / intrinsics.fy
    return math.atan(nx), math.atan2(ny, math.hypot(1.0, nx))


def lamp_candidates(frame: np.ndarray) -> list[Candidate]:
    """Find compact white emitters with local contrast and nearby orange housing."""
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("expected BGR image")
    b, g, r = cv2.split(frame)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    local = cv2.GaussianBlur(gray, (0, 0), 35)
    halo = (cv2.GaussianBlur(gray, (0, 0), 8).astype(np.float32)
            - cv2.GaussianBlur(gray, (0, 0), 35).astype(np.float32))
    # Keep the saturated core: a small local-contrast mask erases large nearby lamps.
    white = (b >= 205) & (g >= 205) & (r >= 205)
    count, labels, stats, centers = cv2.connectedComponentsWithStats(white.astype(np.uint8))

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    orange = (
        (hsv[:, :, 0] <= 23)
        & (hsv[:, :, 1] >= 95)
        & (r.astype(np.float32) > 1.20 * g)
        & (r.astype(np.float32) > 1.35 * b)
    ).astype(np.uint8)
    orange = cv2.morphologyEx(orange, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n_orange, _, orange_stats, orange_centers = cv2.connectedComponentsWithStats(orange)
    housing = [orange_centers[i] for i in range(1, n_orange)
               if 12 <= orange_stats[i, cv2.CC_STAT_AREA] <= 5000]

    output = []
    height, width = gray.shape
    water = ((b.astype(np.float32) > 1.08 * r) |
             (g.astype(np.float32) > 1.10 * r))
    for i in range(1, count):
        left, top, w, h, area = stats[i]
        if area < 2 or area > 3500 or max(w, h) > 100:
            continue
        x, y = centers[i]
        patch = labels[top:top + h, left:left + w] == i
        if area >= 100:
            gray_patch = gray[top:top + h, left:left + w]
            peak = int(gray_patch[patch].max())
            core = patch & (gray_patch >= max(230, peak - 20))
            if int(core.sum()) >= 10:
                core_y, core_x = np.where(core)
                x = float(left + core_x.mean())
                y = float(top + core_y.mean())
        contrast = float(np.max((gray[top:top + h, left:left + w].astype(np.int16)
                                 - local[top:top + h, left:left + w].astype(np.int16))[patch]))
        if contrast < 35:
            continue
        compactness = area / (w * h)
        distance = min((float(np.hypot(x - ox, y - oy)) for ox, oy in housing), default=None)
        x0, x1 = max(0, round(x) - 30), min(width, round(x) + 31)
        y0, y1 = max(0, round(y) - 30), min(height, round(y) + 31)
        water_fraction = float(water[y0:y1, x0:x1].mean())
        # A lamp is a compact emitter. Orange housing is corroborating evidence,
        # but it may be invisible when the child is far away.
        score = (0.018 * contrast + 1.5 * math.log1p(area) + compactness
                 + 3.0 * water_fraction
                 + 0.035 * max(0.0, float(halo[round(y), round(x)]))
                 + (2.5 * math.exp(-distance / 30) if distance is not None else 0))
        if x < 2 or x > width - 3 or y < 2 or y > height - 3:
            score -= 2
        output.append(Candidate(float(x), float(y), int(area), contrast, distance,
                                water_fraction, score))
    return sorted(output, key=lambda item: item.score, reverse=True)


def choose_candidate(candidates: list[Candidate], previous: Candidate | None) -> Candidate | None:
    """Associate a light to the previous sample; reject ambiguous weak proposals."""
    if not candidates:
        return None
    ranked = []
    for candidate in candidates:
        continuity = 0.0
        if previous is not None:
            distance = math.hypot(candidate.x - previous.x, candidate.y - previous.y)
            continuity = 0.8 * math.exp(-distance / 45)
        ranked.append((candidate.score + continuity, candidate))
    ranked.sort(key=lambda item: item[0], reverse=True)
    score, selected = ranked[0]
    if score < 8.5:
        return None
    if previous is None and selected.orange_distance is None and score < 8.0:
        return None
    return selected


def _video_size(path: Path) -> tuple[int, int]:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise ValueError(f"cannot open video: {path}")
        return int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        capture.release()


def analyze_video(
    video: Path,
    output_csv: Path,
    *,
    fps: float = 2.0,
    work_width: int = 640,
    intrinsics: Intrinsics | None = None,
) -> dict:
    """Analyze proxy footage; preserve undetected samples as explicit missing rows."""
    if fps <= 0 or work_width <= 0:
        raise ValueError("fps and work_width must be positive")
    width, height = _video_size(video)
    work_height = round(height * work_width / width)
    capture = cv2.VideoCapture(str(video))
    try:
        source_fps = capture.get(cv2.CAP_PROP_FPS)
        if not source_fps:
            raise ValueError("video frame rate is unavailable")
        stride = max(1, round(source_fps / fps))
        rows = []
        previous = None
        frame_index = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if frame_index % stride:
                frame_index += 1
                continue
            resized = cv2.resize(frame, (work_width, work_height), interpolation=cv2.INTER_AREA)
            candidates = lamp_candidates(resized)
            selected = choose_candidate(candidates, previous)
            if selected is not None:
                previous = selected
                x = selected.x * width / work_width
                y = selected.y * height / work_height
                azimuth, elevation = camera_angles(x, y, intrinsics) if intrinsics else (None, None)
            else:
                x = y = azimuth = elevation = None
                previous = None
            rows.append({
                "video_time_s": round(frame_index / source_fps, 6),
                "source_frame_index": frame_index,
                "status": "candidate_unreviewed" if selected else "not_detected",
                "x_px": round(x, 2) if x is not None else "",
                "y_px": round(y, 2) if y is not None else "",
                "azimuth_rad": round(azimuth, 8) if azimuth is not None else "",
                "elevation_rad": round(elevation, 8) if elevation is not None else "",
                "white_area_work_px": selected.area if selected else "",
                "local_contrast": round(selected.contrast, 2) if selected else "",
                "water_fraction": round(selected.water_fraction, 3) if selected else "",
                "orange_distance_work_px": round(selected.orange_distance, 2)
                if selected and selected.orange_distance is not None else "",
                "candidate_score": round(selected.score, 3) if selected else "",
            })
            frame_index += 1
    finally:
        capture.release()
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ["video_time_s"])
        writer.writeheader()
        writer.writerows(rows)
    return {
        "video": str(video),
        "video_sha256": _sha256(video),
        "video_width": width,
        "video_height": height,
        "source_fps": source_fps,
        "sample_stride_frames": stride,
        "samples": len(rows),
        "candidates": sum(row["status"] == "candidate_unreviewed" for row in rows),
        "intrinsics": vars(intrinsics) if intrinsics else None,
        "angle_status": "pinhole_uncorrected" if intrinsics else "missing_calibration",
        "time_status": "video_relative; stream-copy start may precede requested UTC window",
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def analyze_all(processed_root: Path, results_root: Path, intrinsics: Intrinsics | None = None,
                sessions: list[str] | None = None) -> list[Path]:
    manifests = []
    for session in sorted(processed_root.glob("*")):
        if not session.is_dir() or sessions and session.name not in sessions:
            continue
        videos = sorted((session / "tof_window" / "camera_video").glob("P_MAIN*_proxy.mp4"))
        for video in videos:
            directory = results_root / session.name / "light_detection"
            output = directory / "parent_light.csv"
            manifest = analyze_video(video, output, intrinsics=intrinsics)
            manifest["experiment_id"] = session.name
            manifest["output_csv"] = str(output)
            manifest["config_sha256"] = _sha256(Path("config/sensors.yaml"))
            calibration = Path("calibration/parent_camera.yaml")
            manifest["calibration_sha256"] = _sha256(calibration) if calibration.exists() else None
            setup = Path("experiments") / f"{session.name[:8]}_camera_setup.json"
            manifest["camera_setup"] = str(setup) if setup.exists() else None
            manifest["camera_setup_sha256"] = _sha256(setup) if setup.exists() else None
            manifest_path = directory / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            manifests.append(manifest_path)
    return manifests
