from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
from pathlib import Path

os.environ.setdefault("OPENCV_FFMPEG_THREADS", "2")
import cv2
import numpy as np

from aquabeacon_analysis.ngt_optical import detect_candidate

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/raw/ngtHighSchoolExperiment1/parent/DJI/DJI_20261028140300_0024_D.LRF"
OUT = ROOT / "data/processed/20261028_ngt_000/optical"
RESULT = ROOT / "results/20261028_ngt_000/optical"


def main():
    parser = argparse.ArgumentParser(description="NGT parent video light candidates; uncalibrated")
    parser.add_argument("--experiment", required=True, choices=["20261028_ngt_000"])
    parser.parse_args()
    cv2.setNumThreads(2)
    OUT.mkdir(parents=True, exist_ok=True)
    RESULT.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(SOURCE))
    if not cap.isOpened():
        raise RuntimeError("Cannot open source")
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.set(cv2.CAP_PROP_POS_MSEC, 1050_000)
    rows, previews = [], []
    next_time = 1050.0
    review_times = [
        1050,
        1080,
        1110,
        1140,
        1170,
        1200,
        1230,
        1260,
        1300,
        1350,
        1400,
        1450,
        1500,
        1539,
    ]
    try:
        while next_time < 1540:
            ok, frame = cap.read()
            if not ok:
                break
            frame_index = int(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1
            pts = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
            if pts + 1e-7 < next_time:
                continue
            det = detect_candidate(frame)
            rows.append(
                {
                    "requested_s": round(next_time, 3),
                    "frame_index": frame_index,
                    "frame_pts_s": pts,
                    "x_px": det["x_px"],
                    "y_px": det["y_px"],
                    "confidence": det["confidence"],
                    "status": det["status"],
                    "reason": det["reason"],
                }
            )
            if review_times and next_time >= review_times[0]:
                if det["bbox"]:
                    x, y, w, h = det["bbox"]
                    cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 0, 255), 3)
                cv2.putText(
                    frame,
                    f"{pts:.2f}s {det['status']}",
                    (15, 45),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1,
                    (0, 0, 255),
                    2,
                )
                previews.append(cv2.resize(frame, (640, 360)))
                review_times.pop(0)
            next_time += 0.2
    finally:
        cap.release()
    with (OUT / "light_candidates.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "requested_s",
                "frame_index",
                "frame_pts_s",
                "x_px",
                "y_px",
                "confidence",
                "status",
                "reason",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    if previews:
        if len(previews) % 2:
            previews.append(np.zeros_like(previews[0]))
        cv2.imwrite(
            str(RESULT / "candidate_review.jpg"),
            np.vstack([np.hstack(previews[i : i + 2]) for i in range(0, len(previews), 2)]),
        )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    manifest = {
        "source": str(SOURCE.relative_to(ROOT)),
        "interval_s": [1050, 1540],
        "end_exclusive": True,
        "sample_hz": 5,
        "frame_rate": fps,
        "calibration": None,
        "camera_bearing": "uncalibrated",
        "method": "largest connected component with all BGR channels >=245, >=20 pixels; edge components excluded",
        "confidence": "heuristic brightness component score, not probability or verified beacon identity",
        "source_read_only": True,
        "git_commit": sha,
        "rows": len(rows),
        "status_counts": {
            s: sum(r["status"] == s for r in rows) for s in ["candidate", "missing", "excluded"]
        },
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
