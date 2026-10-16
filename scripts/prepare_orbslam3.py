"""Export one processed C_MAIN experiment to ORB-SLAM3's EuRoC input layout."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from itertools import pairwise
from pathlib import Path


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def image_plan(
    camera_rows: list[dict[str, str]], first_camera_frame: int, video_frame_count: int
) -> list[tuple[int, int]]:
    """Return (decoded video index, UTC nanoseconds); reject uncertain mapping."""
    if not camera_rows or video_frame_count <= 0:
        raise ValueError("camera telemetry and video frames are required")
    pairs = []
    last_index = -1
    last_time = -1
    for row in camera_rows:
        index = int(row["camera_frame"]) - first_camera_frame
        if index < 0 or index >= video_frame_count:
            continue
        timestamp_ns = int(row["refined_sync_utc_us"]) * 1000
        if index <= last_index or timestamp_ns <= last_time:
            raise ValueError("camera frame indices and refined UTC must increase strictly")
        pairs.append((index, timestamp_ns))
        last_index, last_time = index, timestamp_ns
    if not pairs:
        raise ValueError("first camera frame has no overlap with refined telemetry")
    return pairs


def imu_records(rows: list[dict[str, str]], time_offset_ns: int = 0
                ) -> list[tuple[int, float, float, float, float, float, float]]:
    records = []
    for row in rows:
        timestamp_ns = int(row["sync_utc_us"]) * 1000 + time_offset_ns
        values = tuple(float(row[name]) for name in ("gx", "gy", "gz", "ax", "ay", "az"))
        records.append((timestamp_ns, *values))
    records.sort(key=lambda record: record[0])
    if any(b[0] <= a[0] for a, b in pairwise(records)):
        raise ValueError("IMU UTC contains duplicate timestamps")
    return records


def export(
    processed: Path, destination: Path, first_camera_frame: int, max_frames: int | None = None,
    alignment_note: str | None = None, imu_time_offset_s: float = 0.0,
) -> Path:
    if max_frames is None and not alignment_note:
        raise ValueError("full export requires --alignment-note with frame matching evidence")
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("install the vision extra: pip install -e .[vision]") from exc

    camera = next((processed / "camera_telemetry").glob("C_MAIN*_refined.csv"))
    imu = next((processed / "c_main").glob("C_MAIN_IMU_100Hz*.csv"))
    pressure = next((processed / "c_main").glob("C_MAIN_HEALTH_RANGE*.csv"))
    video = next((processed / "camera_video").glob("C_MAIN*proxy.mp4"))
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise ValueError(f"cannot decode video: {video}")
    count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    plan = image_plan(read_rows(camera), first_camera_frame, count)
    if max_frames is not None:
        if max_frames <= 0:
            raise ValueError("max_frames must be positive")
        plan = plan[:max_frames]
    imu_rows = read_rows(imu)
    imu_reversals = sum(
        int(b["sync_utc_us"]) <= int(a["sync_utc_us"])
        for a, b in pairwise(imu_rows)
    )
    imu_data = imu_records(imu_rows, round(imu_time_offset_s * 1_000_000_000))
    if not imu_data or imu_data[0][0] >= plan[0][1]:
        raise ValueError("IMU must begin before the camera")
    planned_images = len(plan)
    plan = [item for item in plan if item[1] <= imu_data[-1][0]]
    if not plan:
        raise ValueError("no camera frames are covered by IMU")

    image_dir = destination / "mav0" / "cam0" / "data"
    image_dir.mkdir(parents=True, exist_ok=True)
    actual = []
    next_index = 0
    for index, timestamp_ns in plan:
        while next_index < index:
            if not capture.grab():
                raise ValueError(f"could not skip to video frame {index}")
            next_index += 1
        ok, image = capture.read()
        next_index += 1
        if not ok or not cv2.imwrite(str(image_dir / f"{timestamp_ns}.png"),
                                      cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)):
            raise ValueError(f"could not export video frame {index}")
        actual.append((index, timestamp_ns))
    capture.release()
    with (destination / "timestamps.txt").open("w", encoding="utf-8", newline="\n") as stream:
        for _, timestamp_ns in actual:
            stream.write(f"{timestamp_ns}\n")
    imu_dir = destination / "mav0" / "imu0"
    imu_dir.mkdir(parents=True, exist_ok=True)
    with (imu_dir / "data.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["#timestamp [ns]", "w_RS_S_x [rad s^-1]", "w_RS_S_y [rad s^-1]",
                         "w_RS_S_z [rad s^-1]", "a_RS_S_x [m s^-2]", "a_RS_S_y [m s^-2]",
                         "a_RS_S_z [m s^-2]"])
        writer.writerows(imu_data)
    depth_dir = destination / "mav0" / "pressure0"
    depth_dir.mkdir(parents=True, exist_ok=True)
    with (depth_dir / "data.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["utc_ns", "pressure_mbar", "relative_depth_m"])
        rows = [row for row in read_rows(pressure) if row["pressure_mbar"]]
        if not rows:
            raise ValueError("no pressure observations")
        origin = float(rows[0]["pressure_mbar"])
        for row in rows:
            p = float(row["pressure_mbar"])
            writer.writerow([int(row["sync_utc_us"]) * 1000, p,
                             (p - origin) * 100 / (1025 * 9.80665)])
    repository = Path(__file__).resolve().parents[1]
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository,
                            capture_output=True, text=True, check=False)
    manifest = {
        "experiment_id": destination.name,
        "camera_source": str(camera),
        "imu_source": str(imu),
        "pressure_source": str(pressure),
        "video_source": str(video),
        "input_sha256": {str(path): sha256(path) for path in (camera, imu, pressure, video)},
        "conditions_sha256": {
            str(path.relative_to(repository)): sha256(path)
            for folder in ("config", "calibration")
            for path in (repository / folder).glob("*") if path.is_file()
        },
        "git_commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "first_video_frame_camera_index": first_camera_frame,
        "frame_alignment_note": alignment_note,
        "test_only": max_frames is not None,
        "exported_images": len(actual),
        "camera_frames_dropped_for_imu_coverage": planned_images - len(actual),
        "imu_rows": len(imu_data),
        "imu_utc_reversals_sorted": imu_reversals,
        "imu_time_offset_applied_s": imu_time_offset_s,
        "time_unit": "UTC nanoseconds",
        "pressure_depth": "relative to first pressure reading; rho=1025 kg/m3",
        "pressure_in_orbslam3": False,
        "calibration_required_before_slam": True,
    }
    path = destination / "export_manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_id")
    parser.add_argument("--first-camera-frame", type=int, required=True,
                        help="camera_frame corresponding to decoded processed video frame zero")
    parser.add_argument("--processed-root", type=Path, default=Path("data/processed"))
    parser.add_argument("--output-root", type=Path, default=Path("data/interim/orbslam3"))
    parser.add_argument("--max-frames", type=int, help="small export for interface checks")
    parser.add_argument("--alignment-note", help="how processed video frame zero was matched to camera_frame")
    parser.add_argument("--imu-time-offset-s", type=float, default=0.0,
                        help="residual body IMU clock shift applied before export, in seconds")
    args = parser.parse_args()
    print(export(args.processed_root / args.experiment_id / "tof_window",
                 args.output_root / args.experiment_id, args.first_camera_frame,
                 args.max_frames, args.alignment_note, args.imu_time_offset_s))


if __name__ == "__main__":
    main()
