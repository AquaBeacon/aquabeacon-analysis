"""Compare color-independent beacon recovery on explicitly selected Nanyo trials."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("OPENCV_FFMPEG_THREADS", "2")
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
import cv2
import numpy as np

from aquabeacon_analysis.beacon_detection import BeaconDetectionConfig, detect_beacon
from aquabeacon_analysis.beacon_tracker import BeaconTracker
from aquabeacon_analysis.ngt_binary import sha256

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/20261103_field_review_000/optical_detection.json"


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, list(dict.fromkeys(k for r in rows for k in r)))
        writer.writeheader()
        writer.writerows(rows)


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def relative(path):
    return str(path.resolve().relative_to(ROOT)).replace("\\", "/")


def source_path(clock):
    """Resolve archived checkout paths only through their explicit raw-relative path."""
    text = clock["source_video"]["path"].replace("\\", "/")
    if "/data/raw/" not in text:
        raise ValueError("Expected an explicitly registered raw camera source")
    path = (ROOT / "data/raw" / text.split("/data/raw/", 1)[1]).with_suffix(".LRF").resolve()
    path.relative_to((ROOT / "data/raw").resolve())
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def processed_sources(eid, acq):
    """Map each original video to its trimmed/concatenated MP4 clock, explicitly."""
    segmentation = ROOT / f"results/{eid}/segmentation/manifest.json"
    meta = json.loads(segmentation.read_text(encoding="utf-8"))
    info = next(v for v in meta["videos"] if v["role"] == "parent")
    path = ROOT / f"data/processed/{eid}/videos/parent.mp4"
    inputs = [segmentation]
    if "pieces" in info:
        sidecar = path.with_suffix(".clock.json")
        info = json.loads(sidecar.read_text(encoding="utf-8"))
        inputs.append(sidecar)
        pieces = info["pieces"]
        if not info.get("edit_list_correction"):
            raise ValueError("Processed day2 MP4 requires verified edit-list timing")
        sources = {Path(p["source"].replace("\\", "/")).name:
                   (path, p["source_start_s"]-p["joined_start_s"]) for p in pieces}
    else:
        sidecar = ROOT / f"data/processed/{acq}/videos/parent_joined.clock.json"
        pieces = json.loads(sidecar.read_text(encoding="utf-8"))
        inputs.append(sidecar)
        sources = {Path(p["source"].replace("\\", "/")).name:
                   (path, info["source_start_s"]-p["joined_start_s"]) for p in pieces}
    return sources, inputs


def merge_detection(original, detection, *, excluded=False):
    """Preserve previous accepted measurements and never fill a review veto."""
    baseline = original["optical_candidate"] == "True"
    added = not baseline and bool(detection["candidate"]) and not excluded
    row = dict(original)
    row.update(baseline_candidate=baseline, recovered_candidate=added,
               recovery_method=detection.get("method", ""),
               recovery_track_status=detection.get("track_status", ""),
               recovery_core_ncc=detection.get("core_template_ncc", np.nan))
    if added:
        row.update(x_px=detection["x_px"], y_px=detection["y_px"],
                   optical_candidate="True", water_like_heuristic="True",
                   track_status="recovered_" + detection["track_status"],
                   template_ncc=detection["template_ncc"])
    elif not baseline:
        row.update(x_px="nan", y_px="nan", optical_candidate="False",
                   water_like_heuristic="False")
    return row


def track(eid, run, cfg, deadline, max_frames=None, cancel=None):
    baseline_dir = ROOT / f"results/{eid}/mapping_light_tracking"
    baseline_path = baseline_dir / "camera_relative_candidates.csv"
    manifest_path = baseline_dir / "manifest.json"
    original_meta = json.loads(manifest_path.read_text(encoding="utf-8"))
    registry_path = ROOT / f"experiments/{eid}/experiment.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    acq = registry["parent_acquisition"]
    sync_path = ROOT / f"results/{acq}/synchronization/manifest.json"
    sync = json.loads(sync_path.read_text(encoding="utf-8"))
    clocks = {Path(c["source_video"]["path"].replace("\\", "/")).name: c
              for key, c in sync["cameras"].items() if key.startswith("parent")}
    review_path = ROOT / f"config/{acq}/mapping_light_review.json"
    inputs = [baseline_path, manifest_path, registry_path, sync_path, review_path, CONFIG,
              Path(__file__), ROOT / "src/aquabeacon_analysis/beacon_tracker.py",
              ROOT / "src/aquabeacon_analysis/beacon_halo.py",
              ROOT / "src/aquabeacon_analysis/beacon_detection.py"]
    hashes = {relative(p): sha256(p) for p in inputs}
    processed, extra_inputs = processed_sources(eid, acq)
    proxy = ROOT / f"data/interim/{eid}/optical_detection/parent_30hz.mp4"
    proxy_meta = json.loads(proxy.with_suffix(".json").read_text(encoding="utf-8"))
    if proxy_meta["status"] != "completed" or proxy_meta["sha256"] != sha256(proxy):
        raise ValueError("Detection proxy is incomplete or its hash changed")
    extra_inputs.append(proxy.with_suffix(".json"))
    processed = {name: (proxy, offset) for name, (_, offset) in processed.items()}
    inputs.extend(extra_inputs)
    hashes.update({relative(p): sha256(p) for p in extra_inputs})
    rows = read(baseline_path)
    if max_frames is not None:
        rows = rows[:max_frames]
    seeds = sorted(original_meta["reviewed_initializations"], key=lambda a: a["utc_s"])
    for anchor in seeds:
        name = anchor.get("review_source", "")
        if name.endswith(".jpg"):
            path = baseline_dir / name
            if not path.is_file() or sha256(path) != anchor["review_image_sha256"]:
                raise ValueError("Reviewed initialization image is missing or changed")
            inputs.append(path)
            hashes[relative(path)] = sha256(path)
    out = ROOT / f"results/{eid}/optical_detection/{run}"
    out.mkdir(parents=True, exist_ok=True)
    if (out / "manifest.json").exists():
        raise FileExistsError(f"Use a new run name; preserving {out}")
    cap, current_source, tracker, control, seed_index = None, None, None, None, 0
    result, reviews, preview_records, video_sources, seed_events = [], [], [], {}, []
    max_delta, last_review = 0.0, -np.inf
    try:
        for i, row in enumerate(rows):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("Cancelled after another trial failed")
            if time.monotonic() > deadline:
                raise TimeoutError("Explicit optical evaluation runtime limit exceeded")
            source = row["source_video"]
            target = float(row["actual_source_s"])
            utc = float(row["utc_s"])
            if source != current_source:
                if cap is not None:
                    cap.release()
                path, source_offset = processed[source]
                stat = path.stat()
                video_sources[relative(path)] = {"size_bytes": stat.st_size,
                                                "mtime_ns": stat.st_mtime_ns,
                                                "read_only": True}
                cap = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG,
                                       [cv2.CAP_PROP_N_THREADS, 2])
                if not cap.isOpened():
                    raise RuntimeError(f"Cannot open {path}")
                cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 0)
                cap.set(cv2.CAP_PROP_POS_MSEC, max(0, target-source_offset-0.02)*1000)
                frame_period = 1/cap.get(cv2.CAP_PROP_FPS)
                current_source = source
                tracker = None
                control = None
            while True:
                if not cap.grab():
                    raise RuntimeError(f"Unexpected video EOF at {target}")
                video_pts = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
                actual = video_pts + source_offset
                if actual >= target - frame_period/2:
                    ok, frame = cap.retrieve()
                    if not ok:
                        raise RuntimeError("Frame retrieval failed")
                    break
            if abs(actual-target) > 0.02:
                raise ValueError(f"Frame clock mismatch: {actual} vs {target}")
            if frame.shape[:2] != (720, 1280):
                raise ValueError("Detection proxy must be encoded 1280x720")
            max_delta = max(max_delta, abs(actual-target))
            measured_utc = utc + clocks[source]["time_scale"]*(actual-target)
            while seed_index < len(seeds) and seeds[seed_index]["utc_s"] <= measured_utc:
                anchor = seeds[seed_index]
                name = anchor.get("review_source", "")
                snapshot = cv2.imread(str(baseline_dir/name)) if name.endswith(".jpg") else None
                if (snapshot is None and anchor["seed_px"] is not None
                        and measured_utc-anchor["utc_s"] > .075):
                    seed_events.append({"utc_s": anchor["utc_s"], "review_source": name,
                                        "action": "skip_stale_coordinate_only_seed"})
                    seed_index += 1
                    continue
                tracker = (BeaconTracker(anchor["seed_px"], detector=detect_beacon, template_size=41,
                                         max_missing_frames=cfg["max_missing_frames"],
                                         allow_flow_core=True)
                           if anchor["seed_px"] is not None else None)
                control = BeaconTracker(anchor["seed_px"]) if anchor["seed_px"] is not None else None
                if snapshot is not None and tracker is not None:
                    tracker.update(snapshot)
                    control.update(snapshot)
                seed_events.append({"utc_s": anchor["utc_s"], "review_source": name,
                                    "action": "initialize_review_image" if snapshot is not None
                                              else "initialize_current_frame_or_clear",
                                    "first_following_frame_utc_s": measured_utc})
                seed_index += 1
            detection = (tracker.update(frame) if tracker is not None else
                         {"candidate": False, "track_status": "no_reviewed_visible_light"})
            control_detection = control.update(frame) if control is not None else {"candidate": False}
            merged = merge_detection(row, detection,
                                     excluded=row["track_status"] == "rejected_identity_review")
            merged["decoded_source_s"] = actual
            merged["reference_utc_s"] = row["utc_s"]
            merged["decoded_utc_s"] = measured_utc
            merged["decoded_video_pts_s"] = video_pts
            veto = row["track_status"] == "rejected_identity_review"
            merged["control_candidate"] = bool(control_detection["candidate"]) and not veto
            merged["new_algorithm_candidate"] = bool(detection["candidate"]) and not veto
            if merged["recovered_candidate"]:
                merged["utc_s"] = measured_utc
                merged["segment_time_s"] = measured_utc-registry["utc_interval_s"][0]
                merged["actual_source_s"] = actual
            merged["source_frame_index"] = int(cap.get(cv2.CAP_PROP_POS_FRAMES))-1
            result.append(merged)
            if merged["recovered_candidate"] and utc-last_review >= cfg["review_stride_s"]:
                point = tuple(float(merged[c]) for c in ("x_px", "y_px"))
                preview = frame.copy()
                cv2.circle(preview, tuple(map(round, point)), 9, (0, 0, 255), 1)
                crop = cv2.getRectSubPix(preview, (200, 160), point)
                label = f"{i}: {float(row['segment_time_s']):.1f}s"
                cv2.putText(crop, label, (3, 15), cv2.FONT_HERSHEY_SIMPLEX,
                            .4, (255, 255, 255), 1)
                reviews.append(crop)
                preview_records.append({"row_index": i, "utc_s": utc, "source_s": actual,
                                        "x_px": point[0], "y_px": point[1],
                                        "page": (len(reviews)-1)//24,
                                        "cell": (len(reviews)-1)%24})
                last_review = utc
            if (i+1) % 1000 == 0:
                print(eid, i+1, "/", len(rows), "added",
                      sum(r["recovered_candidate"] for r in result), flush=True)
    finally:
        if cap is not None:
            cap.release()
    for page, start in enumerate(range(0, len(reviews), 24)):
        tiles = reviews[start:start+24]
        while len(tiles) % 6:
            tiles.append(np.zeros_like(tiles[0]))
        cv2.imwrite(str(out / f"recovered_review_{page:02}.jpg"),
                    np.vstack([np.hstack(tiles[i:i+6]) for i in range(0, len(tiles), 6)]))
    for path in inputs:
        if hashes[relative(path)] != sha256(path):
            raise RuntimeError(f"Input changed during processing: {path}")
    write(out / "camera_relative_candidates.csv", result)
    dump(out / "review_samples.json", preview_records)
    baseline = sum(r["baseline_candidate"] for r in result)
    added = sum(r["recovered_candidate"] for r in result)
    meta = {"experiment_id": eid, "run": run, "rows": len(result),
            "baseline_accepted": baseline, "accepted": baseline+added,
            "recovered": added, "status_counts": dict(Counter(r["track_status"] for r in result)),
            "control_same_mp4_accepted": sum(r["control_candidate"] for r in result),
            "new_same_mp4_accepted": sum(r["new_algorithm_candidate"] for r in result),
            "configuration": cfg, "detector_config": asdict(BeaconDetectionConfig()),
            "seed_events": seed_events,
            "seed_policy": "Initialize on the hashed reviewed image at its true UTC, then track forward; coordinate-only static seeds expire after 75 ms",
            "coordinate_basis": cfg["pixel_basis"],
            "baseline_valid_policy": "Preserve original coordinates, flags and UTC exactly",
            "maximum_frame_time_delta_s": max_delta, "complete_trial": max_frames is None,
            "identity_validation": "reviewed seeds and sampled crops, not every frame ground truth",
            "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                                   text=True).strip(),
            "inputs_sha256": hashes, "videos": video_sources,
            "created_utc": datetime.now(UTC).isoformat()}
    dump(out / "manifest.json", meta)
    print(eid, "DONE", baseline, "->", baseline+added, "/", len(result), flush=True)
    return meta


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True, action="append")
    parser.add_argument("--run", default="multiscale_v2")
    parser.add_argument("--max-frames", type=int)
    args = parser.parse_args()
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    if set(args.experiment) - set(cfg["experiments"]):
        parser.error("Explicit Nanyo trial IDs from optical_detection.json are required")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", args.run):
        parser.error("Run must be a simple directory name")
    if args.max_frames is not None and args.max_frames <= 0:
        parser.error("--max-frames must be positive")
    cv2.setNumThreads(2)
    out = ROOT / f"results/20261103_field_review_000/optical_detection/{args.run}"
    out.mkdir(parents=True, exist_ok=True)
    process = {"pid": os.getpid(), "started_utc": datetime.now(UTC).isoformat(),
               "purpose": "Nanyo beacon detection comparison", "task_owned": True,
               "command": sys.argv, "status": "running", "workers": cfg["max_workers"],
               "decoder_threads_per_worker": 2, "opencv_threads": 1}
    dump(out / "process.json", process)
    print(json.dumps(process), flush=True)
    try:
        deadline = time.monotonic() + cfg["max_runtime_s"]
        cancel = threading.Event()
        with ThreadPoolExecutor(max_workers=cfg["max_workers"]) as pool:
            jobs = [pool.submit(track, eid, args.run, cfg, deadline, args.max_frames, cancel)
                    for eid in dict.fromkeys(args.experiment)]
            try:
                for future in as_completed(jobs):
                    future.result()
            except BaseException:
                cancel.set()
                for future in jobs:
                    future.cancel()
                raise
        process["status"] = "completed"
    except BaseException as exc:
        process.update(status="failed", error=str(exc))
        raise
    finally:
        process["ended_utc"] = datetime.now(UTC).isoformat()
        dump(out / "process.json", process)


if __name__ == "__main__":
    main()
