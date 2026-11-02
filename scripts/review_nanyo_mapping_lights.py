"""Make coarse, timestamped parent-image sheets for full-trial tracking initialization."""

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from aquabeacon_analysis.beacon_halo import detect_halo
from aquabeacon_analysis.ngt_binary import sha256


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True, action="append")
    args = parser.parse_args()
    for eid in args.experiment:
        meta = json.loads(Path(f"experiments/{eid}/experiment.json").read_text(encoding="utf-8"))
        acq = meta["parent_acquisition"]
        syncpath = Path(f"results/{acq}/synchronization/manifest.json")
        clocks = json.loads(syncpath.read_text(encoding="utf-8"))["cameras"]
        lo, hi = meta["utc_interval_s"]
        out = Path(f"results/{eid}/mapping_light_tracking")
        out.mkdir(parents=True, exist_ok=True)
        frames, records = [], []
        offsets = sorted({*np.arange(5, hi - lo, 60), hi - lo - 5})
        for offset in offsets:
            utc = lo + offset
            _key, clock = max(
                [
                    (k, c)
                    for k, c in clocks.items()
                    if k.startswith("parent") and c["utc_at_video_zero_s"] <= utc
                ],
                key=lambda x: x[1]["utc_at_video_zero_s"],
            )
            source = Path(clock["source_video"]["path"]).with_suffix(".LRF")
            sec = (utc - clock["utc_at_video_zero_s"]) / clock["time_scale"]
            cap = cv2.VideoCapture(str(source))
            cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 0)
            cap.set(cv2.CAP_PROP_POS_MSEC, sec * 1000)
            ok, frame = cap.read()
            actual = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
            cap.release()
            if not ok:
                continue
            candidate = detect_halo(frame)
            preview = frame.copy()
            for x in range(200, 1280, 200):
                cv2.line(preview, (x, 0), (x, 720), (90, 90, 90), 1)
                cv2.putText(
                    preview, str(x), (x + 3, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1
                )
            for y in range(200, 720, 200):
                cv2.line(preview, (0, y), (1280, y), (90, 90, 90), 1)
                cv2.putText(
                    preview, str(y), (3, y - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1
                )
            if candidate["candidate"]:
                cv2.drawMarker(
                    preview,
                    (round(candidate["x_px"]), round(candidate["y_px"])),
                    (0, 255, 255),
                    cv2.MARKER_CROSS,
                    30,
                    2,
                )
            cv2.putText(
                preview,
                f"{eid[-3:]} {offset:.2f}s | proposal {candidate['x_px']:.0f},{candidate['y_px']:.0f}",
                (5, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 0, 255),
                2,
            )
            index = len(records)
            cv2.imwrite(str(out / f"review_{index:03d}.jpg"), frame)
            frames.append(cv2.resize(preview, (640, 360)))
            records.append(
                {
                    "index": index,
                    "offset_s": float(offset),
                    "utc_s": clock["utc_at_video_zero_s"] + clock["time_scale"] * actual,
                    "source": str(source),
                    "source_s": actual,
                    "proposal": candidate,
                    "reviewed": False,
                }
            )
        for start in range(0, len(frames), 8):
            page = frames[start : start + 8]
            while len(page) < 8:
                page.append(np.zeros_like(frames[0]))
            cv2.imwrite(
                str(out / f"review_page_{start // 8:02d}.jpg"),
                np.vstack([np.hstack(page[:4]), np.hstack(page[4:])]),
            )
        (out / "review_candidates.json").write_text(
            json.dumps(
                {
                    "experiment_id": eid,
                    "records": records,
                    "sync_sha256": sha256(syncpath),
                    "script_sha256": sha256(Path(__file__)),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(eid, len(records), flush=True)


if __name__ == "__main__":
    main()
