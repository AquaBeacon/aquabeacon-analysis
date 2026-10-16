"""Anchor-assisted EKF tracking of the child lamp in parent camera frames."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import subprocess
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .light_detection import Candidate, Intrinsics, camera_angles, lamp_candidates


@dataclass(frozen=True)
class TrackingConfig:
    sample_fps: float = 2.0
    work_width: int = 640
    acceleration_noise_px_s2: float = 65.0
    log_area_noise_sqrt_s: float = 0.65
    gate_chi2: float = 9.21
    min_candidate_score: float = 9.5
    max_prediction_s: float = 1.5
    max_prediction_sigma_px: float = 45.0
    max_after_last_anchor_s: float = 5.0


class LightEKF:
    """State is (x, y, vx, vy, log white-area) in work-image pixels.

    The area observation h[2] = exp(log_area) makes the measurement model
    nonlinear; its Jacobian is evaluated at each update.
    """

    def __init__(self, x: float, y: float, area: float, config: TrackingConfig):
        self.state = np.array([x, y, 0.0, 0.0, math.log(max(area, 1.0))])
        self.covariance = np.diag([16.0, 16.0, 2500.0, 2500.0, 0.7])
        self.config = config

    def predict(self, dt: float) -> None:
        if dt < 0:
            raise ValueError("timestamps must increase")
        transition = np.eye(5)
        transition[0, 2] = transition[1, 3] = dt
        q = np.zeros((5, 5))
        accel_variance = self.config.acceleration_noise_px_s2**2
        for position, velocity in ((0, 2), (1, 3)):
            q[position, position] = accel_variance * dt**4 / 4
            q[position, velocity] = q[velocity, position] = accel_variance * dt**3 / 2
            q[velocity, velocity] = accel_variance * dt**2
        q[4, 4] = self.config.log_area_noise_sqrt_s**2 * dt
        self.state = transition @ self.state
        self.covariance = transition @ self.covariance @ transition.T + q

    def position_mahalanobis(self, x: float, y: float, sigma_px: float) -> float:
        residual = np.array([x, y]) - self.state[:2]
        innovation_covariance = self.covariance[:2, :2] + np.eye(2) * sigma_px**2
        return float(residual @ np.linalg.solve(innovation_covariance, residual))

    def update(self, candidate: Candidate, sigma_px: float) -> None:
        area_predicted = math.exp(float(np.clip(self.state[4], -10, 12)))
        observation = np.array([candidate.x, candidate.y, float(candidate.area)])
        predicted = np.array([self.state[0], self.state[1], area_predicted])
        jacobian = np.zeros((3, 5))
        jacobian[0, 0] = jacobian[1, 1] = 1.0
        jacobian[2, 4] = area_predicted
        area_sigma = max(8.0, 0.8 * max(candidate.area, area_predicted))
        noise = np.diag([sigma_px**2, sigma_px**2, area_sigma**2])
        innovation_covariance = jacobian @ self.covariance @ jacobian.T + noise
        gain = self.covariance @ jacobian.T @ np.linalg.inv(innovation_covariance)
        self.state += gain @ (observation - predicted)
        identity = np.eye(5)
        corrected = identity - gain @ jacobian
        self.covariance = corrected @ self.covariance @ corrected.T + gain @ noise @ gain.T

    @property
    def position_sigma_px(self) -> float:
        return math.sqrt(float(max(self.covariance[0, 0], self.covariance[1, 1])))


def measurement_sigma(candidate: Candidate) -> float:
    return float(np.clip(12.0 - 0.55 * (candidate.score - 9.5), 5.0, 14.0))


def associate(
    filter_: LightEKF, candidates: list[Candidate], config: TrackingConfig,
    guide: tuple[float, float, float] | None = None,
) -> tuple[Candidate | None, float | None]:
    """Choose the best gated emitter using innovation and visual evidence."""
    admissible = []
    for candidate in candidates:
        if candidate.score < config.min_candidate_score:
            continue
        if candidate.water_fraction < 0.35 and not (
            candidate.orange_distance is not None and candidate.orange_distance < 25
        ):
            continue
        if guide is not None and math.hypot(candidate.x - guide[0], candidate.y - guide[1]) > guide[2]:
            continue
        distance = filter_.position_mahalanobis(
            candidate.x, candidate.y, measurement_sigma(candidate)
        )
        if distance <= config.gate_chi2:
            visual_bonus = 0.12 * max(0.0, candidate.score - config.min_candidate_score)
            admissible.append((distance - visual_bonus, distance, candidate))
    if not admissible:
        return None, None
    _, distance, candidate = min(admissible, key=lambda item: item[0])
    return candidate, distance


def strong_candidate(
    candidates: list[Candidate], guide: tuple[float, float, float] | None
) -> Candidate | None:
    """Permit a new track only for distinctive light evidence near checked anchors."""
    for candidate in candidates:
        if guide is not None and math.hypot(
            candidate.x - guide[0], candidate.y - guide[1]
        ) > max(200.0, 2.0 * guide[2]):
            continue
        compact_emitter = (
            candidate.score >= 13.0
            and candidate.area >= 20
            and candidate.water_fraction >= 0.6
            and candidate.orange_distance is not None
            and candidate.orange_distance <= 80
        )
        saturated_emitter = (
            candidate.score >= 15.0
            and candidate.area >= 250
            and candidate.water_fraction >= 0.5
        )
        isolated_water_emitter = (
            guide is not None and candidate.score >= 12.5
            and candidate.area >= 35 and candidate.water_fraction >= 0.85
        )
        large_saturated_emitter = (
            guide is not None and candidate.score >= 15.5 and candidate.area >= 1000
        )
        if (compact_emitter or saturated_emitter or isolated_water_emitter
                or large_saturated_emitter):
            return candidate
    return None


def anchor_guide(
    frame_index: int, source_fps: float, anchor_frames: list[int],
    anchor_points: dict[int, tuple[float, float, float]],
) -> tuple[float, float, float] | None:
    next_index = bisect_left(anchor_frames, frame_index)
    if not 0 < next_index < len(anchor_frames):
        return None
    before, after = anchor_frames[next_index - 1], anchor_frames[next_index]
    if (after - before) / source_fps > 31:
        return None
    start = anchor_points[before]
    end = anchor_points[after]
    alpha = (frame_index - before) / (after - before)
    radius = max(60.0, 25.0 + 0.35 * math.hypot(end[0] - start[0], end[1] - start[1]))
    return (start[0] + alpha * (end[0] - start[0]),
            start[1] + alpha * (end[1] - start[1]), radius)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _anchors(path: Path, work_width: int, video_width: int, work_height: int,
             video_height: int) -> dict[int, tuple[float, float, float]]:
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise ValueError(f"no reviewed light points: {path}")
    anchors = {
        int(row["source_frame_index"]): (
            float(row["x_px"]) * work_width / video_width,
            float(row["y_px"]) * work_height / video_height,
            float(row["white_area_work_px"]),
        )
        for row in rows
        if row["status"] == "visually_confirmed"
    }
    if not anchors:
        raise ValueError(f"no visually confirmed anchors: {path}")
    return anchors


def track_video(
    video: Path,
    reviewed_csv: Path,
    output_csv: Path,
    config: TrackingConfig | None = None,
    intrinsics: Intrinsics | None = None,
) -> dict:
    """Track at regular frame indices and preserve observation provenance."""
    if config is None:
        config = TrackingConfig()
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise ValueError(f"cannot open video: {video}")
    try:
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        source_fps = capture.get(cv2.CAP_PROP_FPS)
        if source_fps <= 0 or config.sample_fps <= 0:
            raise ValueError("frame rates must be positive")
        work_height = round(height * config.work_width / width)
        stride = max(1, round(source_fps / config.sample_fps))
        anchor_points = _anchors(
            reviewed_csv, config.work_width, width, work_height, height
        )
        anchor_frames = sorted(anchor_points)
        filter_: LightEKF | None = None
        previous_time_s: float | None = None
        last_observed_s: float | None = None
        rows = []
        frame_index = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if frame_index % stride:
                frame_index += 1
                continue
            time_s = frame_index / source_fps
            if frame_index in anchor_points:
                ax, ay, area = anchor_points[frame_index]
                filter_ = LightEKF(ax, ay, area, config)
                last_observed_s = time_s
                status = "anchor"
                selected = None
                innovation = None
            else:
                after_last_anchor_s = (frame_index - anchor_frames[-1]) / source_fps
                if after_last_anchor_s > config.max_after_last_anchor_s:
                    filter_ = None
                    status = "untracked"
                    selected = None
                    innovation = None
                else:
                    resized = cv2.resize(
                        frame, (config.work_width, work_height), interpolation=cv2.INTER_AREA
                    )
                    candidates = lamp_candidates(resized)
                    guide = anchor_guide(
                        frame_index, source_fps, anchor_frames, anchor_points
                    )
                    strong = strong_candidate(candidates, guide)
                    if filter_ is not None:
                        filter_.predict(time_s - previous_time_s)
                        if time_s - last_observed_s > config.max_prediction_s:
                            filter_ = None
                    if filter_ is None:
                        selected, innovation = None, None
                        if frame_index < anchor_frames[0] or strong is None:
                            status = "untracked"
                        else:
                            selected = strong
                            filter_ = LightEKF(strong.x, strong.y, strong.area, config)
                            last_observed_s = time_s
                            status = "reacquired"
                    else:
                        selected, innovation = associate(filter_, candidates, config, guide)
                        if (strong is not None and (selected is None or
                                strong.score >= selected.score + 2.0)):
                            nearby = selected is not None and math.hypot(
                                strong.x - selected.x, strong.y - selected.y
                            ) <= 25
                            selected = strong
                            if nearby:
                                innovation = filter_.position_mahalanobis(
                                    strong.x, strong.y, measurement_sigma(strong)
                                )
                                filter_.update(strong, measurement_sigma(strong))
                                status = "observed"
                            else:
                                innovation = None
                                filter_ = LightEKF(strong.x, strong.y, strong.area, config)
                                status = "reacquired"
                            last_observed_s = time_s
                        elif selected is None:
                            status = "predicted"
                        else:
                            filter_.update(selected, measurement_sigma(selected))
                            last_observed_s = time_s
                            status = "observed"
            if (status == "predicted" and filter_ is not None
                    and filter_.position_sigma_px * width / config.work_width
                    > config.max_prediction_sigma_px):
                filter_ = None
                status = "untracked"
            if filter_ is not None and status != "untracked":
                x = float(filter_.state[0]) * width / config.work_width
                y = float(filter_.state[1]) * height / work_height
                sigma = filter_.position_sigma_px * width / config.work_width
                angles = (camera_angles(x, y, intrinsics)
                          if intrinsics and status != "predicted" else (None, None))
            else:
                x = y = sigma = None
                angles = (None, None)
            rows.append({
                "video_time_s": round(time_s, 6),
                "source_frame_index": frame_index,
                "status": status,
                "x_px": round(x, 2) if x is not None else "",
                "y_px": round(y, 2) if y is not None else "",
                "position_sigma_px": round(sigma, 2) if sigma is not None else "",
                "azimuth_rad": round(angles[0], 8) if angles[0] is not None else "",
                "elevation_rad": round(angles[1], 8) if angles[1] is not None else "",
                "measurement_x_px": round(selected.x * width / config.work_width, 2)
                if selected else "",
                "measurement_y_px": round(selected.y * height / work_height, 2)
                if selected else "",
                "measurement_score": round(selected.score, 3) if selected else "",
                "innovation_chi2": round(innovation, 3) if innovation is not None else "",
            })
            previous_time_s = time_s
            frame_index += 1
    finally:
        capture.release()
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    return {
        "video": str(video),
        "video_sha256": _sha256(video),
        "reviewed_csv": str(reviewed_csv),
        "reviewed_csv_sha256": _sha256(reviewed_csv),
        "output_csv": str(output_csv),
        "config": vars(config),
        "intrinsics": vars(intrinsics) if intrinsics else None,
        "samples": len(rows),
        "status_counts": {name: sum(row["status"] == name for row in rows)
                          for name in ("anchor", "observed", "reacquired",
                                       "predicted", "untracked")},
        "angle_status": "pinhole_uncorrected" if intrinsics else "missing_calibration",
        "time_status": "video_relative; stream-copy start may precede requested UTC window",
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    }


def track_all(processed_root: Path, results_root: Path,
              sessions: list[str] | None = None,
              intrinsics: Intrinsics | None = None) -> list[Path]:
    manifests = []
    for session in sorted(processed_root.glob("*")):
        if not session.is_dir() or sessions and session.name not in sessions:
            continue
        videos = sorted((session / "tof_window" / "camera_video").glob("P_MAIN*_proxy.mp4"))
        for video in videos:
            directory = results_root / session.name / "light_detection"
            reviewed = directory / "reviewed_light.csv"
            if not reviewed.exists():
                raise FileNotFoundError(f"visual anchors required: {reviewed}")
            output = directory / "tracked_light.csv"
            manifest = track_video(video, reviewed, output, intrinsics=intrinsics)
            manifest["experiment_id"] = session.name
            setup = Path("experiments") / f"{session.name[:8]}_camera_setup.json"
            manifest["camera_setup_sha256"] = _sha256(setup) if setup.exists() else None
            calibration = Path("calibration/parent_camera.yaml")
            manifest["calibration_sha256"] = _sha256(calibration) if calibration.exists() else None
            config_path = Path("config/sensors.yaml")
            manifest["sensor_config_sha256"] = _sha256(config_path)
            path = directory / "tracking_manifest.json"
            path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            manifests.append(path)
    return manifests
