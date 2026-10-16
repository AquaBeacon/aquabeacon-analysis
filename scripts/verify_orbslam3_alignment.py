"""Verify processed video frames against the untrimmed interim camera proxy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np


def frame(capture: cv2.VideoCapture, index: int, rotation: str = "0") -> np.ndarray:
    capture.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, image = capture.read()
    if not ok:
        raise ValueError(f"cannot decode frame {index}")
    turns = {"90": cv2.ROTATE_90_CLOCKWISE, "180": cv2.ROTATE_180,
             "270": cv2.ROTATE_90_COUNTERCLOCKWISE}
    if rotation in turns:
        image = cv2.rotate(image, turns[rotation])
    return cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (160, 90))


def verify(experiment_id: str, source_offset: int, first_source_camera_frame: int,
           rotation: str, root: Path) -> dict:
    name = "C_MAIN*proxy.mp4"
    source_path = next((root / "data/interim" / experiment_id / "camera_video").glob(name))
    output_path = next((root / "data/processed" / experiment_id / "tof_window/camera_video").glob(name))
    source = cv2.VideoCapture(str(source_path))
    output = cv2.VideoCapture(str(output_path))
    count = int(output.get(cv2.CAP_PROP_FRAME_COUNT))
    indices = sorted({0, count // 4, count // 2, 3 * count // 4, count - 1})
    matches = []
    for index in indices:
        a = frame(source, source_offset + index, rotation)
        b = frame(output, index)
        delta = float(np.mean(np.abs(a.astype(np.int16) - b.astype(np.int16))))
        matches.append({"processed_index": index, "source_index": source_offset + index,
                        "mean_absolute_pixel_difference": delta})
    source.release()
    output.release()
    return {"experiment_id": experiment_id, "source_video": str(source_path),
            "processed_video": str(output_path), "source_offset_frames": source_offset,
            "source_first_camera_frame": first_source_camera_frame,
            "first_processed_camera_frame": first_source_camera_frame + source_offset,
            "rotation_for_comparison_degrees": int(rotation), "anchors": matches,
            "passed": all(item["mean_absolute_pixel_difference"] == 0 for item in matches),
            "note": "source first camera_frame inferred from stream-copy GOP start; verify independently"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_id")
    parser.add_argument("--source-offset", type=int, required=True)
    parser.add_argument("--source-first-camera-frame", type=int, required=True)
    parser.add_argument("--rotation", choices=("0", "90", "180", "270"), default="0")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = verify(args.experiment_id, args.source_offset,
                    args.source_first_camera_frame, args.rotation, root)
    destination = root / "results" / args.experiment_id / "vislam_depth"
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "frame_alignment.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(path)
    if not result["passed"]:
        raise SystemExit("frame alignment anchors did not match")


if __name__ == "__main__":
    main()
