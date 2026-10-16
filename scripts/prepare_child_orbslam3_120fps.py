"""Export every child source-video frame, using 30 Hz telemetry as UTC anchors."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from itertools import pairwise
from pathlib import Path

import cv2
import numpy as np


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def frame_plan(rows: list[dict[str, str]], first_proxy_camera_frame: int,
               source_frame_count: int) -> list[tuple[int, int]]:
    anchors = [(4 * (int(row["camera_frame"]) - first_proxy_camera_frame),
                int(row["refined_sync_utc_us"]) * 1000) for row in rows]
    anchors = [(index, utc_ns) for index, utc_ns in anchors
               if 0 <= index < source_frame_count]
    if len(anchors) < 2 or any(b[0] <= a[0] or b[1] <= a[1]
                               for a, b in pairwise(anchors)):
        raise ValueError("insufficient or nonmonotonic camera UTC anchors")
    indices = np.arange(anchors[0][0], anchors[-1][0] + 1, dtype=np.int64)
    anchor_indices = np.array([item[0] for item in anchors], dtype=np.int64)
    # Interpolate the small UTC offset rather than epoch-sized values in float64.
    origin = anchors[0][1]
    offsets = np.array([item[1] - origin for item in anchors], dtype=np.int64)
    stamps = np.rint(np.interp(indices, anchor_indices, offsets)).astype(np.int64) + origin
    if np.any(np.diff(stamps) <= 0):
        raise ValueError("interpolated frame times are not strictly increasing")
    return list(zip(indices.tolist(), stamps.tolist()))


def export(root: Path, experiment_id: str, output_root: Path,
           max_frames: int | None = None, color_jpeg: bool = False) -> Path:
    processed = root / "data/processed" / experiment_id / "tof_window"
    proxy = next((processed / "camera_video").glob("C_MAIN*proxy.mp4"))
    source = next((processed / "camera_video").glob("C_MAIN*video.mp4"))
    telemetry = next((processed / "camera_telemetry").glob("C_MAIN*_refined.csv"))
    previous = root / "data/interim/orbslam3_full" / experiment_id / "export_manifest.json"
    old = json.loads(previous.read_text(encoding="utf-8"))
    first_proxy = int(old["first_video_frame_camera_index"])
    with telemetry.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise ValueError(f"cannot decode {source}")
    source_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    source_fps = capture.get(cv2.CAP_PROP_FPS)
    if abs(source_fps - 120000 / 1001) > 0.01:
        raise ValueError(f"expected 120000/1001 fps, got {source_fps}")
    plan = frame_plan(rows, first_proxy, source_count)
    if max_frames is not None:
        if max_frames <= 0:
            raise ValueError("max_frames must be positive")
        plan = plan[:max_frames]
    destination = output_root / experiment_id
    images = destination / "mav0/cam0/data"
    images.mkdir(parents=True, exist_ok=True)
    capture.set(cv2.CAP_PROP_POS_FRAMES, plan[0][0])
    with (destination / "timestamps.txt").open("w", encoding="utf-8", newline="\n") as timestamps:
        for expected, utc_ns in plan:
            actual = int(capture.get(cv2.CAP_PROP_POS_FRAMES))
            if actual != expected:
                raise ValueError(f"video frame position {actual} != {expected}")
            ok, frame = capture.read()
            if not ok:
                raise ValueError(f"could not decode frame {expected}")
            resized = cv2.resize(frame, (1280, 720), interpolation=cv2.INTER_AREA)
            image = resized if color_jpeg else cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
            extension = ".jpg" if color_jpeg else ".png"
            options = ([cv2.IMWRITE_JPEG_QUALITY, 95] if color_jpeg else
                       [cv2.IMWRITE_PNG_COMPRESSION, 3])
            if not cv2.imwrite(str(images / f"{utc_ns}{extension}"), image, options):
                raise ValueError(f"could not save frame {expected}")
            timestamps.write(f"{utc_ns}\n")
            if (expected - plan[0][0]) % 2000 == 0:
                print(f"{experiment_id}: {expected - plan[0][0] + 1}/{len(plan)}", flush=True)
    capture.release()
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                            text=True, capture_output=True, check=False)
    calibration = root / "calibration/parent_camera_underwater_20260924_provisional.json"
    manifest = {
        "experiment_id": experiment_id,
        "video_source": str(source), "video_sha256": sha256(source),
        "proxy_source": str(proxy), "proxy_sha256": sha256(proxy),
        "telemetry_source": str(telemetry), "telemetry_sha256": sha256(telemetry),
        "calibration_source": str(calibration), "calibration_sha256": sha256(calibration),
        "git_commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "source_fps": source_fps, "source_frame_count": source_count,
        "exported_images": len(plan), "first_source_frame": plan[0][0],
        "last_source_frame": plan[-1][0], "first_proxy_camera_frame": first_proxy,
        "source_frames_without_utc_before": plan[0][0],
        "source_frames_without_utc_after": source_count - 1 - plan[-1][0],
        "image_size_px": [1280, 720],
        "image_format": "BGR color JPEG quality 95" if color_jpeg else "grayscale PNG",
        "image_extension": ".jpg" if color_jpeg else ".png",
        "color_channels": 3 if color_jpeg else 1,
        "frame_time_method": "source frame 4*i corresponds to proxy frame i; linear UTC interpolation between 30 Hz refined telemetry anchors",
        "alignment_evidence": str(root / "results" / experiment_id / "vislam_depth/frame_alignment_120fps.json"),
        "frame_alignment_note": "Fourfold 120/30 frame mapping verified at sampled visual anchors; see alignment_evidence",
        "test_only": max_frames is not None,
    }
    path = destination / "export_manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_id")
    parser.add_argument("--output-root", type=Path,
                        help="default: color or grayscale 120 fps interim root")
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--color-jpeg", action="store_true",
                        help="keep native video color using 1280x720 JPEG images")
    args = parser.parse_args()
    output_root = args.output_root or Path("data/interim/orbslam3_color_120fps" if args.color_jpeg
                                          else "data/interim/orbslam3_120fps")
    print(export(Path(__file__).resolve().parents[1], args.experiment_id,
                 output_root, args.max_frames, args.color_jpeg))


if __name__ == "__main__":
    main()
