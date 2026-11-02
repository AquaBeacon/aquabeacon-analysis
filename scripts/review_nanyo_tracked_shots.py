"""Re-extract every image used for stationary RMSE and create identity review sheets."""

import argparse
import csv
import json
from pathlib import Path

import cv2
import numpy as np

from aquabeacon_analysis.ngt_binary import sha256


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261101_nanyo_000"])
    args = parser.parse_args()
    config = json.loads(Path(f"config/{args.experiment}/stationary_spectra.json").read_text())
    for window in config["windows"]:
        folder = Path(f"results/{window['experiment_id']}/light_tracking/{window['id']}")
        manifest = json.loads((folder / "manifest.json").read_text())
        with (folder / "positions.csv").open() as stream:
            rows = [r for r in csv.DictReader(stream) if r["position_available"] == "True"]
        cap = cv2.VideoCapture(manifest["source_video"])
        cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 0)
        patches, time_errors = [], []
        for row in rows:
            cap.set(cv2.CAP_PROP_POS_MSEC, float(row["source_video_s"]) * 1000)
            ok, frame = cap.read()
            if not ok:
                raise ValueError("A source image used by a position cannot be decoded")
            error = abs(cap.get(cv2.CAP_PROP_POS_MSEC) / 1000 - float(row["source_video_s"]))
            if error >= 0.04:
                raise ValueError("Re-extracted image differs by more than one source frame")
            time_errors.append(error)
            patch = cv2.getRectSubPix(frame, (160, 120), (float(row["x_px"]), float(row["y_px"])))
            cv2.circle(patch, (80, 60), 12, (0, 0, 255), 1)
            cv2.putText(
                patch,
                f"{float(row['segment_time_s']):.2f}s / {row['shot_seq']}",
                (3, 15),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.35,
                (0, 0, 255),
                1,
            )
            patches.append(patch)
        cap.release()
        while len(patches) % 10:
            patches.append(np.zeros_like(patches[0]))
        cv2.imwrite(
            str(folder / "matched_shot_crops.jpg"),
            np.vstack([np.hstack(patches[i : i + 10]) for i in range(0, len(patches), 10)]),
        )
        (folder / "matched_shot_review.json").write_text(
            json.dumps(
                {
                    "window": window["id"],
                    "reviewed_image_count": len(rows),
                    "max_source_time_difference_s": max(time_errors),
                    "inputs_sha256": {
                        str(folder / "positions.csv"): sha256(folder / "positions.csv"),
                        str(Path(__file__)): sha256(Path(__file__)),
                    },
                    "automated_target_identity_certification": False,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(window["id"], len(rows), flush=True)


if __name__ == "__main__":
    main()
