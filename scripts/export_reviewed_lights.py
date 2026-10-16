"""Export fixed, visually checked keyframe coordinates from experiment labels."""

from __future__ import annotations

import csv
import json
from pathlib import Path


def main() -> None:
    review = json.loads(Path("experiments/light_review_20260922.json").read_text(encoding="utf-8"))
    for experiment_id, anchors in review.items():
        if experiment_id == "method":
            continue
        directory = Path("results") / experiment_id / "light_detection"
        confirmed = [
            {"video_time_s": point["video_time_s"],
             "source_frame_index": point["source_frame_index"],
             "status": "visually_confirmed",
             "x_px": point["x_px"], "y_px": point["y_px"],
             "white_area_work_px": point["white_area_work_px"]}
            for point in anchors
        ]
        output = directory / "reviewed_light.csv"
        with output.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=confirmed[0].keys())
            writer.writeheader()
            writer.writerows(confirmed)
        print(output)


if __name__ == "__main__":
    main()
