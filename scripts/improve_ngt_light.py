"""Pool multiscale proposals on exact LRF frames, followed by exhaustive addition review."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
import cv2
import numpy as np
from evaluate_position_fallback import dump, write_csv
from improve_nanyo_light import ROOT, read, relative
from PIL import Image, ImageDraw

from aquabeacon_analysis.beacon_detection import BeaconDetectionConfig, detect_luminous_core
from aquabeacon_analysis.nanyo_followup import longest_runs
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_optical import detect_candidate
from aquabeacon_analysis.pool_optical import merge_pool_detection, reviewed_pool_rows

CONFIG = ROOT/"config/20261103_field_review_000/pool_optical_followup.json"
REVIEW = ROOT/"config/20261103_field_review_000/pool_optical_identity_review.json"


def folder(eid, cfg):
    return ROOT/f"results/{eid}/optical_followup/{cfg['run']}"


def contact_sheets(out, cases):
    for first in range(0, len(cases), 16):
        canvas = Image.new("RGB", (4*400, 4*430), "white")
        draw = ImageDraw.Draw(canvas)
        for j, case in enumerate(cases[first:first+16]):
            image = Image.open(out/case["image"]).convert("RGB")
            full = image.copy(); full.thumbnail((225, 400))
            x, y = j % 4*400, j//4*430
            canvas.paste(full, (x, y+28))
            draw.text((x+4, y+4), f"row {case['row_index']} / {case['parent_video_s']:.3f}s", fill="black")
            px, py = case["candidate_xy"]
            if np.isfinite([px, py]).all():
                crop = image.crop((px-40, py-40, px+40, py+40)).resize((168, 168))
                canvas.paste(crop, (x+228, y+60))
                draw.ellipse((x+228+80, y+60+80, x+228+88, y+60+88), outline="red", width=2)
                ratio = full.width/image.width
                cx, cy = x+px*ratio, y+28+py*ratio
                draw.ellipse((cx-7, cy-7, cx+7, cy+7), outline="red", width=2)
            draw.text((x+228, y+242), case["category"], fill="black")
        canvas.save(out/f"review_{first//16:02}.jpg", quality=94)


def detect(eid, cfg):
    out = folder(eid, cfg); out.mkdir(parents=True, exist_ok=False)
    process = {"pid": os.getpid(), "started_utc": datetime.now(UTC).isoformat(), "task_owned": True,
               "purpose": "pool light recovery and exact parent frame evidence", "status": "running"}
    dump(out/"process.json", process)
    start = time.monotonic()
    registry = ROOT/f"experiments/{eid}/experiment.json"
    lo, hi = json.loads(registry.read_text())["parent_video_interval_s"]
    original = [r for r in read(ROOT/cfg["baseline"]) if lo <= float(r["frame_pts_s"]) < hi]
    baseline_cfg = ROOT/"config/20261028_ngt/position_evaluation.json"
    vetoes = json.loads(baseline_cfg.read_text())["manual_exclusions"]
    detector_cfg = BeaconDetectionConfig(**cfg["detector"])
    missing = [r["status"] == "missing" for r in original]
    special = {(a+b-1)//2 for a, b in longest_runs(missing)[:3]}
    special.update({0, len(original)//2, len(original)-1})
    source = ROOT/cfg["source_video"]
    cap = cv2.VideoCapture(str(source), cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 2])
    rows, cases = [], []
    try:
        if not cap.isOpened() or int(cap.get(cv2.CAP_PROP_ORIENTATION_META)) != 90:
            raise ValueError("Pool parent LRF orientation/source mismatch")
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(original[0]["frame_index"]))
        for i, original_row in enumerate(original):
            if time.monotonic()-start > cfg["max_detection_seconds"]:
                raise TimeoutError("Pool detection deadline")
            wanted = int(original_row["frame_index"])
            while int(cap.get(cv2.CAP_PROP_POS_FRAMES)) <= wanted:
                if not cap.grab():
                    raise ValueError("Cannot decode baseline frame")
            ok, frame = cap.retrieve()
            actual = cap.get(cv2.CAP_PROP_POS_MSEC)/1000
            if not ok or frame.shape[:2] != (1280, 720) or abs(actual-float(original_row["frame_pts_s"])) > .001:
                raise ValueError("Exact baseline frame and displayed geometry required")
            control = detect_candidate(frame)
            if control["status"] != original_row["status"]:
                raise ValueError("Legacy detector does not reproduce on the identical frame")
            if original_row["status"] == "candidate":
                np.testing.assert_allclose([control[k] for k in ("x_px", "y_px")],
                                           [float(original_row[k]) for k in ("x_px", "y_px")], atol=1e-8)
            proposal = detect_luminous_core(frame, config=detector_cfg)
            veto = any(v["interval_s"][0] <= actual < v["interval_s"][1] for v in vetoes)
            row = merge_pool_detection(original_row, proposal, identity_excluded=veto)
            row.update(decoded_pts_s=actual, control_status=control["status"])
            rows.append(row)
            if row["proposed_addition"] or i in special or (veto and i % 5 == 0):
                category = "proposed" if row["proposed_addition"] else "veto" if veto else "baseline" if row["baseline_candidate"] else "missing"
                name = f"parent_{i:05}.jpg"
                cv2.imwrite(str(out/name), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
                cases.append({"row_index": i, "parent_video_s": actual, "segment_time_s": actual-lo,
                              "category": category, "image": name, "image_sha256": sha256(out/name),
                              "candidate_xy": [proposal["x_px"], proposal["y_px"]] if proposal["candidate"] else [None, None],
                              "baseline_status": original_row["status"], "baseline_reason": original_row["reason"],
                              "proposed_addition": row["proposed_addition"]})
        write_csv(out/"automatic_candidates.csv", rows)
        dump(out/"parent_frame_cases.json", cases)
        contact_sheets(out, [c for c in cases if c["proposed_addition"]])
        inputs = [CONFIG, registry, ROOT/cfg["baseline"], baseline_cfg, Path(__file__),
                  ROOT/"src/aquabeacon_analysis/pool_optical.py", ROOT/"src/aquabeacon_analysis/beacon_detection.py",
                  ROOT/"src/aquabeacon_analysis/ngt_optical.py", ROOT/"calibration/20261031_ngt_camera_frames.json"]
        dump(out/"detection_manifest.json", {"experiment_id": eid, "run": cfg["run"], "interval_s": [lo, hi],
              "detector": asdict(detector_cfg), "source_stat": {"size": source.stat().st_size, "mtime_ns": source.stat().st_mtime_ns},
              "rows": len(rows), "baseline_status_counts": dict(Counter(r["status"] for r in rows)),
              "new_algorithm_candidates": sum(r["new_algorithm_candidate"] for r in rows),
              "proposed_additions": sum(r["proposed_addition"] for r in rows), "same_frame_control_exact": True,
              "automatic_candidates_sha256": sha256(out/"automatic_candidates.csv"),
              "inputs_sha256": {relative(p): sha256(p) for p in inputs},
              "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "scope": cfg["detection_policy"]})
        process["status"] = "completed"
        print(eid, len(rows), "frames", sum(r["proposed_addition"] for r in rows), "proposed additions", flush=True)
    finally:
        cap.release()
        process.update(ended_utc=datetime.now(UTC).isoformat(), elapsed_s=time.monotonic()-start)
        if process["status"] == "running":
            process["status"] = "failed"
        dump(out/"process.json", process)


def apply(eid, cfg):
    out = folder(eid, cfg)
    target = out/"reviewed_candidates.csv"
    if target.exists():
        raise FileExistsError("Preserve the completed reviewed run")
    review = json.loads(REVIEW.read_text(encoding="utf-8"))["experiments"][eid]
    source = out/"automatic_candidates.csv"
    if sha256(source) != review["automatic_candidates_sha256"]:
        raise ValueError("Review belongs to another input")
    cases = json.loads((out/"parent_frame_cases.json").read_text())
    for case in cases:
        if sha256(out/case["image"]) != case["image_sha256"]:
            raise ValueError("Review image changed")
    rows = reviewed_pool_rows(read(source), review["accepted_rows"], review["rejected_rows"])
    write_csv(target, rows)
    dump(out/"identity_review_manifest.json", {"review_sha256": sha256(REVIEW), "input_sha256": sha256(source),
          "output_sha256": sha256(target), "accepted": len(review["accepted_rows"]), "rejected": len(review["rejected_rows"]),
          "all_proposed_additions_visually_reviewed": True, "scope": "Selected proposals only; not annotated full-frame recall"})
    print(eid, "review applied", len(review["accepted_rows"]), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", action="append", required=True)
    parser.add_argument("--phase", choices=["detect", "apply"], required=True)
    args = parser.parse_args(); config = json.loads(CONFIG.read_text())
    if set(args.experiment)-set(config["experiments"]):
        parser.error("Explicit pool trials 002/003/004 required")
    eids = list(dict.fromkeys(args.experiment))
    if args.phase == "detect" and any(folder(e, config).exists() for e in eids):
        parser.error("Preserve previous runs; choose a new configured run")
    cv2.setNumThreads(1)
    print("pool detection PID", os.getpid(), datetime.now(UTC).isoformat(), flush=True)
    for eid in eids:
        (detect if args.phase == "detect" else apply)(eid, config)
