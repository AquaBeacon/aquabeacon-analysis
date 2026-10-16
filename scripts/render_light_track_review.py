"""Render 10-second EKF contact sheets for visual review."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import cv2
import numpy as np

COLORS = {
    "anchor": (0, 255, 0),
    "observed": (0, 0, 255),
    "reacquired": (255, 0, 255),
    "predicted": (0, 255, 255),
}


def main() -> None:
    for directory in sorted(Path("results").glob("*/light_detection")):
        track = directory / "tracked_light.csv"
        manifest = directory / "tracking_manifest.json"
        if not track.exists() or not manifest.exists():
            continue
        video = Path(json.loads(manifest.read_text(encoding="utf-8"))["video"])
        with track.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        capture = cv2.VideoCapture(str(video))
        try:
            canvas = np.zeros((720, 1280, 3), dtype=np.uint8)
            for slot, second in enumerate(range(0, 160, 10)):
                row = min(rows, key=lambda item: abs(float(item["video_time_s"]) - second))
                if abs(float(row["video_time_s"]) - second) > 0.3:
                    continue
                capture.set(cv2.CAP_PROP_POS_FRAMES, int(row["source_frame_index"]))
                ok, frame = capture.read()
                if not ok:
                    continue
                if row["x_px"]:
                    point = round(float(row["x_px"])), round(float(row["y_px"]))
                    cv2.circle(frame, point, 35, COLORS[row["status"]], 5)
                tile = cv2.resize(frame, (320, 180))
                cv2.putText(tile, f"{second} {row['status']}", (5, 23),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)
                top, left = divmod(slot, 4)
                canvas[top * 180:(top + 1) * 180, left * 320:(left + 1) * 320] = tile
            output = directory / "tracking_review.jpg"
            cv2.imwrite(str(output), canvas)
            print(output)
        finally:
            capture.release()


if __name__ == "__main__":
    main()
