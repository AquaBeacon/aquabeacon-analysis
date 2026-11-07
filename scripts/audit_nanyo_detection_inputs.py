"""Compare saved LRF review patches against timestamped processed-MP4 proxies."""
import argparse
import json
import os
from datetime import UTC, datetime

import cv2
import numpy as np
from improve_nanyo_light import CONFIG, ROOT, dump, processed_sources, relative, write

from aquabeacon_analysis.beacon_detection import detect_beacon
from aquabeacon_analysis.beacon_halo import detect_halo
from aquabeacon_analysis.ngt_binary import sha256


def audit(eid):
    registry = json.loads((ROOT/f"experiments/{eid}/experiment.json").read_text(encoding="utf-8"))
    acq = registry["parent_acquisition"]
    review = ROOT/f"config/{acq}/mapping_light_review.json"
    anchors = json.loads(review.read_text(encoding="utf-8"))["trials"][eid]
    folder = ROOT/f"results/{eid}/mapping_light_tracking"
    records_path = folder/"review_candidates.json"
    records = json.loads(records_path.read_text(encoding="utf-8"))["records"]
    by_image = {f"review_{r['index']:03}.jpg": r for r in records}
    sources, inputs = processed_sources(eid, acq)
    proxy = ROOT/f"data/interim/{eid}/optical_detection/parent_30hz.mp4"
    out = ROOT/f"results/{eid}/optical_detection/input_audit"
    out.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(proxy), cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 2])
    rows, patches = [], []
    inputs += [review, records_path, proxy.with_suffix(".json")]
    try:
        for anchor in anchors:
            name = anchor.get("review_source", "")
            if anchor["seed_px"] is None or name not in by_image:
                continue
            ref_path = folder/name
            ref = cv2.imread(str(ref_path))
            inputs.append(ref_path)
            record = by_image[name]
            source_name = record["source"].replace("\\", "/").split("/")[-1].replace(".LRF", ".MP4")
            _, offset = sources[source_name]
            target = record["source_s"]-offset
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0, target-.1)*1000)
            period = 1/cap.get(cv2.CAP_PROP_FPS)
            while True:
                if not cap.grab():
                    raise RuntimeError(f"Audit frame unavailable: {eid} {name}")
                pts = cap.get(cv2.CAP_PROP_POS_MSEC)/1000
                if pts >= target-period/2:
                    ok, frame = cap.retrieve()
                    if not ok or abs(pts-target) > .02:
                        raise ValueError("Audit frame must be within 20 ms")
                    break
            xy = tuple(map(float, anchor["seed_px"]))
            template = cv2.getRectSubPix(cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY), (101, 101), xy)
            search = cv2.getRectSubPix(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (161, 161), xy)
            _, ncc, _, location = cv2.minMaxLoc(cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED))
            shift = np.array(location)-30
            row = {"experiment_id": eid, "image": name, "requested_source_s": record["source_s"],
                   "decoded_source_s": pts+offset, "clock_difference_s": pts-target,
                   "image_ncc": ncc, "shift_x_px": int(shift[0]), "shift_y_px": int(shift[1])}
            for label, fn in (("old", detect_halo), ("new", detect_beacon)):
                detection = fn(ref, xy, 20)
                point = np.array([detection["x_px"], detection["y_px"]])
                row[label+"_accepted_on_review"] = bool(detection["candidate"] and np.linalg.norm(point-xy)<20)
            rows.append(row)
            patch = np.hstack([cv2.getRectSubPix(ref, (160, 160), xy),
                               cv2.getRectSubPix(frame, (160, 160), xy)])
            cv2.putText(patch, f"{name}: {ncc:.3f} dx={shift[0]} dy={shift[1]}", (2, 15),
                        cv2.FONT_HERSHEY_SIMPLEX, .4, (255, 255, 255), 1)
            patches.append(patch)
    finally:
        cap.release()
    write(out/"image_clock_audit.csv", rows)
    for page, start in enumerate(range(0, len(patches), 12)):
        tiles = patches[start:start+12]
        while len(tiles)%3:
            tiles.append(np.zeros_like(tiles[0]))
        cv2.imwrite(str(out/f"comparison_{page:02}.jpg"),
                    np.vstack([np.hstack(tiles[i:i+3]) for i in range(0, len(tiles), 3)]))
    dump(out/"manifest.json", {"experiment_id": eid, "rows": len(rows),
                               "interpretation": "Local appearance comparison, not independent geometric calibration",
                               "inputs_sha256": {relative(p): sha256(p) for p in inputs}})
    print(eid, "review", len(rows), "old/new",
          sum(r["old_accepted_on_review"] for r in rows), sum(r["new_accepted_on_review"] for r in rows),
          "min NCC", min(r["image_ncc"] for r in rows),
          "max shift", max(np.hypot(r["shift_x_px"], r["shift_y_px"]) for r in rows), flush=True)
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True, action="append")
    args = parser.parse_args()
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    if set(args.experiment)-set(cfg["experiments"]):
        parser.error("Explicit Nanyo trial IDs required")
    cv2.setNumThreads(1)
    print("input audit PID", os.getpid(), datetime.now(UTC).isoformat(), flush=True)
    result = [r for eid in dict.fromkeys(args.experiment) for r in audit(eid)]
    write(ROOT/"results/20261103_field_review_000/optical_detection/input_audit.csv", result)
