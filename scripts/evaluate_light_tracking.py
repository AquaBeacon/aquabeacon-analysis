"""Compare candidate and EKF tracks with independent manually labeled frames."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path


def nearest(rows: list[dict[str, str]], second: float) -> dict[str, str]:
    return min(rows, key=lambda row: abs(float(row["video_time_s"]) - second))


def main() -> None:
    labels = json.loads(Path("experiments/light_validation_20260922.json").read_text())
    all_rows = []
    for experiment_id, points in labels.items():
        if experiment_id == "method":
            continue
        directory = Path("results") / experiment_id / "light_detection"
        with (directory / "parent_light.csv").open(encoding="utf-8", newline="") as stream:
            raw = list(csv.DictReader(stream))
        with (directory / "tracked_light.csv").open(encoding="utf-8", newline="") as stream:
            tracked = list(csv.DictReader(stream))
        rows = []
        for point in points:
            for method, observations in (("candidate", raw), ("ekf", tracked)):
                row = nearest(observations, point["video_time_s"])
                has_position = bool(row["x_px"])
                error = (
                    math.hypot(float(row["x_px"]) - point["x_px"],
                               float(row["y_px"]) - point["y_px"])
                    if has_position and point["visible"] else None
                )
                rows.append({
                    "video_time_s": point["video_time_s"],
                    "visible": point["visible"],
                    "method": method,
                    "status": row["status"],
                    "error_px": round(error, 1) if error is not None else "",
                    "correct_within_30_px": bool(error is not None and error <= 30),
                    "false_positive_offscreen": bool(has_position and not point["visible"]),
                })
        all_rows.extend(rows)
        output = directory / "tracking_validation.csv"
        with output.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        for method in ("candidate", "ekf"):
            selected = [row for row in rows if row["method"] == method]
            print(
                experiment_id, method,
                "correct", sum(row["correct_within_30_px"] for row in selected),
                "of", sum(row["visible"] for row in selected),
                "offscreen false positives", sum(row["false_positive_offscreen"] for row in selected),
            )
        manifest = json.loads((directory / "tracking_manifest.json").read_text(encoding="utf-8"))
        candidate_rows = [row for row in rows if row["method"] == "candidate"]
        ekf_rows = [row for row in rows if row["method"] == "ekf"]
        visible = sum(row["visible"] for row in ekf_rows)
        hidden = len(ekf_rows) - visible
        report = (
            f"# EKF light tracking: {experiment_id}\n\n"
            f"- Samples: {manifest['samples']} at approximately 2 Hz.\n"
            f"- Status counts: {manifest['status_counts']}.\n"
            f"- Independent manual checks: {visible} visible, {hidden} outside frame. "
            "Coordinates are approximate within about 5-10 px.\n"
            f"- Within 30 px of manual check: candidate "
            f"{sum(row['correct_within_30_px'] for row in candidate_rows)}/{visible}, "
            f"EKF {sum(row['correct_within_30_px'] for row in ekf_rows)}/{visible}.\n"
            f"- False coordinates on checked out-of-frame samples: candidate "
            f"{sum(row['false_positive_offscreen'] for row in candidate_rows)}/{hidden}, "
            f"EKF {sum(row['false_positive_offscreen'] for row in ekf_rows)}/{hidden}.\n"
            "- Visual review: `tracking_review.jpg`; individual checks: "
            "`tracking_validation.csv`.\n"
            "- This is agreement with a small manual sample, not ground-truth accuracy. "
            "Unreviewed `observed` and `reacquired` rows can still contain errors.\n"
            "- Camera-relative angles remain blank without underwater in-case calibration.\n"
        )
        (directory / "tracking_report.md").write_text(report, encoding="utf-8")
    for method in ("candidate", "ekf"):
        selected = [row for row in all_rows if row["method"] == method]
        print(
            "TOTAL", method,
            "correct", sum(row["correct_within_30_px"] for row in selected),
            "of", sum(row["visible"] for row in selected),
            "offscreen false positives", sum(row["false_positive_offscreen"] for row in selected),
        )


if __name__ == "__main__":
    main()
