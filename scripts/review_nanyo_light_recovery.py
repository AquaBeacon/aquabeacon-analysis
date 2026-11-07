"""Prepare track-boundary images and apply explicit, provenance-checked identity vetoes."""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("OPENCV_FFMPEG_THREADS", "2")
import cv2
import numpy as np
from improve_nanyo_light import CONFIG, ROOT, dump, read, relative, write

from aquabeacon_analysis.ngt_binary import sha256


def apply_vetoes(original, automatic, ranges):
    """Only remove new observations; never change a previously accepted measurement."""
    if len(original) != len(automatic):
        raise ValueError("Review rows do not match baseline")
    reasons = {}
    for item in ranges:
        first, last = item["first_row"], item["last_row"]
        if not (0 <= first <= last < len(original)) or not item["reason"]:
            raise ValueError("Invalid explicit review range")
        for index in range(first, last+1):
            reasons[index] = item["reason"]
    result = []
    for index, (old, new) in enumerate(zip(original, automatic, strict=True)):
        row = dict(new)
        added = new["recovered_candidate"] == "True"
        rejected = added and index in reasons
        if added and old["optical_candidate"] == "True":
            raise ValueError("Recovered flag overlaps an old accepted point")
        row.update(automatic_recovered_candidate=added, identity_review_rejected=rejected,
                   identity_review_reason=reasons.get(index, "") if rejected else "")
        if rejected:
            row.update(old)
            row.update(x_px="nan", y_px="nan", optical_candidate="False",
                       water_like_heuristic="False", recovered_candidate="False",
                       track_status="rejected_recovery_identity_review")
        result.append(row)
    return result


def prepare(eid, out, rows):
    ids = [i for i, r in enumerate(rows) if r["recovered_candidate"] == "True"]
    groups = []
    for i in ids:
        if not groups or i-groups[-1][-1] > 10:
            groups.append([i])
        else:
            groups[-1].append(i)
    selected = sorted({i for group in groups for i in (group[0], group[-1])})
    proxy = ROOT / f"data/interim/{eid}/optical_detection/parent_30hz.mp4"
    cap = cv2.VideoCapture(str(proxy), cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 2])
    cv2.setNumThreads(1)
    tiles, records = [], []
    deadline = time.monotonic()+300
    try:
        for index in selected:
            if time.monotonic() > deadline:
                raise TimeoutError(f"Boundary audit exceeded 300 seconds: {eid}")
            row = rows[index]
            target = float(row["decoded_video_pts_s"])
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0, target-.2)*1000)
            ok = False
            for _ in range(100):
                if not cap.grab():
                    break
                if cap.get(cv2.CAP_PROP_POS_MSEC)/1000 >= target-.001:
                    ok, frame = cap.retrieve()
                    break
            if not ok or abs(cap.get(cv2.CAP_PROP_POS_MSEC)/1000-target) > .001:
                raise ValueError(f"Boundary review frame mismatch: {eid}, {index}")
            point = (float(row["x_px"]), float(row["y_px"]))
            cv2.circle(frame, tuple(round(v) for v in point), 8, (0, 0, 255), 1)
            crop = cv2.getRectSubPix(frame, (200, 160), point)
            cv2.putText(crop, f"{index}: {float(row['segment_time_s']):.1f}s", (3, 15),
                        cv2.FONT_HERSHEY_SIMPLEX, .4, (255, 255, 255), 1, cv2.LINE_AA)
            records.append({"row_index": index, "page": len(tiles)//24, "cell": len(tiles)%24,
                            "decoded_video_pts_s": float(row["decoded_video_pts_s"])})
            tiles.append(crop)
    finally:
        cap.release()
    for page, first in enumerate(range(0, len(tiles), 24)):
        cells = tiles[first:first+24]
        sheet = np.zeros((160*((len(cells)+5)//6), 1200, 3), dtype=np.uint8)
        for cell, tile in enumerate(cells):
            y, x = 160*(cell//6), 200*(cell%6)
            sheet[y:y+160, x:x+200] = tile
        cv2.imwrite(str(out/f"boundary_review_{page:02d}.jpg"), sheet)
    dump(out/"boundary_review_samples.json", records)
    print(eid, "boundary images", len(records), flush=True)


def apply(eid, out, rows, review_config):
    notes_path = out/"manual_review.json"
    policy = json.loads(review_config.read_text(encoding="utf-8"))
    if policy["run"] != out.name:
        raise ValueError("Review configuration is for a different run")
    notes = policy["reviews"][eid]
    automatic_path = out/"camera_relative_candidates.csv"
    if notes["experiment_id"] != eid or notes["automatic_csv_sha256"] != sha256(automatic_path):
        raise ValueError("Manual review does not identify these automatic observations")
    dump(notes_path, notes)
    baseline = ROOT/f"results/{eid}/mapping_light_tracking/camera_relative_candidates.csv"
    reviewed = apply_vetoes(read(baseline), rows, notes["identity_rejections"])
    path = out/"reviewed_camera_relative_candidates.csv"
    write(path, reviewed)
    inputs = [automatic_path, baseline, notes_path, review_config, out/"review_samples.json",
              out/"boundary_review_samples.json", *[out/p for p in notes["review_images"]]]
    dump(out/"identity_review_manifest.json", {
        "experiment_id": eid, "created_utc": datetime.now(UTC).isoformat(),
        "automatic_additions": sum(r["recovered_candidate"] == "True" for r in rows),
        "review_rejections": sum(r["identity_review_rejected"] for r in reviewed),
        "retained_additions": sum(str(r["recovered_candidate"]) == "True" for r in reviewed),
        "output_sha256": sha256(path),
        "inputs_sha256": {relative(p): sha256(p) for p in inputs},
        "implementation_sha256": sha256(Path(__file__)),
        "limitation": "Sampled visual review with explicit exclusions, not all-frame ground truth"})
    print(eid, "review vetoes", sum(r["identity_review_rejected"] for r in reviewed), flush=True)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", action="append", required=True)
    parser.add_argument("--run", default="multiscale_v2")
    parser.add_argument("--phase", choices=("prepare", "apply"), required=True)
    parser.add_argument("--review-config", type=Path,
                        default=ROOT/"config/20261103_field_review_000/optical_detection_identity_review.json")
    args = parser.parse_args()
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    if set(args.experiment)-set(cfg["experiments"]):
        parser.error("Explicit registered Nanyo trials required")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", args.run):
        parser.error("Invalid run name")
    print("review PID", os.getpid(), datetime.now(UTC).isoformat(), flush=True)
    for eid in dict.fromkeys(args.experiment):
        out = ROOT/f"results/{eid}/optical_detection/{args.run}"
        rows = read(out/"camera_relative_candidates.csv")
        if args.phase == "prepare":
            prepare(eid, out, rows)
        else:
            apply(eid, out, rows, args.review_config)


if __name__ == "__main__":
    main()
