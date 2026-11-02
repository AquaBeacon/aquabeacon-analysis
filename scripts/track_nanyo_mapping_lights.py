"""Track reviewed beacon identities throughout mapping trials; never fill failed tracks."""

import argparse
import json
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from analyze_nanyo import read, write

from aquabeacon_analysis.beacon_tracker import BeaconTracker
from aquabeacon_analysis.ngt_binary import sha256


def reuse_static(eid):
    """Keep already reviewed 60-second tracks on their exact original frame clocks."""
    folder = Path(f"results/{eid}/mapping_light_tracking")
    path = folder / "camera_relative_candidates.csv"
    rows = read(path)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    meta_trial = json.loads(Path(f"experiments/{eid}/experiment.json").read_text(encoding="utf-8"))
    trial_lo, trial_hi = meta_trial["utc_interval_s"]
    review_path = Path(f"config/{meta_trial['parent_acquisition']}/mapping_light_review.json")
    exclusions = (
        json.loads(review_path.read_text(encoding="utf-8"))
        .get("excluded_intervals_s", {})
        .get(eid, [])
    )
    for row in rows:
        offset = float(row["utc_s"]) - trial_lo
        if any(a["start"] <= offset < a["end"] for a in exclusions):
            row.update(
                optical_candidate="False",
                water_like_heuristic="False",
                x_px="nan",
                y_px="nan",
                track_status="rejected_identity_review",
            )
    originals = []
    for source in sorted(
        Path(f"results/{eid}/light_tracking").glob("*/camera_relative_candidates.csv")
    ):
        previous = read(source)
        if not previous:
            continue
        meta = json.loads((source.parent / "manifest.json").read_text(encoding="utf-8"))
        lo = datetime.fromisoformat(meta["window"]["start"]).timestamp()
        rows = [r for r in rows if not lo <= float(r["utc_s"]) < lo + 60]
        rows.extend(
            {**{k: r[k] for k in rows[0]}, "segment_time_s": float(r["utc_s"]) - trial_lo}
            for r in previous
            if trial_lo <= float(r["utc_s"]) < trial_hi
        )
        originals.extend([source, source.parent / "manifest.json"])
    rows.sort(key=lambda r: float(r["utc_s"]))
    rows = [r for r in rows if trial_lo <= float(r["utc_s"]) < trial_hi]
    write(path, rows)
    manifest.update(
        rows=len(rows),
        accepted=sum(r["optical_candidate"] == "True" for r in rows),
        status_counts=dict(Counter(r["track_status"] for r in rows)),
        static_window_reuse="Previously image-reviewed static-window tracks replace those 60-second windows, retaining exact original video UTC; no interpolation or resmoothing",
        static_track_inputs_sha256={str(p): sha256(p) for p in originals},
    )
    manifest["inputs_sha256"][str(Path(__file__))] = sha256(Path(__file__))
    manifest["inputs_sha256"][str(review_path)] = sha256(review_path)
    manifest["reviewed_exclusions_before_validated_static_reuse"] = exclusions
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(eid, "with static reuse", manifest["accepted"], "/", len(rows), flush=True)


def run(eid):
    meta = json.loads(Path(f"experiments/{eid}/experiment.json").read_text(encoding="utf-8"))
    acq = meta["parent_acquisition"]
    lo, hi = meta["utc_interval_s"]
    review_path = Path(f"config/{acq}/mapping_light_review.json")
    review = json.loads(review_path.read_text(encoding="utf-8"))["trials"][eid]
    folder = Path(f"results/{eid}/mapping_light_tracking")
    seeds = [dict(a) for a in review]
    # Reuse previously reviewed stationary identities, independently of GNSS/RMSE.
    static_path = Path("config/20261101_nanyo_000/static_light_review.json")
    windows_path = Path("config/20261101_nanyo_000/stationary_spectra.json")
    static = json.loads(static_path.read_text(encoding="utf-8"))["windows"]
    for spec in json.loads(windows_path.read_text(encoding="utf-8"))["windows"]:
        if spec["experiment_id"] != eid:
            continue
        start = datetime.fromisoformat(spec["start"]).timestamp()
        anchors = static.get(
            spec["id"],
            [[0, *{"day1_144110": [606, 308], "day1_150300": [540, 400]}.get(spec["id"], [0, 0])]],
        )
        seeds.extend(
            {"utc_s": start + a[0], "seed_px": a[1:], "review_source": spec["id"]} for a in anchors
        )
    seeds.sort(key=lambda a: a["utc_s"])
    sync_path = Path(f"results/{acq}/synchronization/manifest.json")
    clocks = sorted(
        (
            c
            for k, c in json.loads(sync_path.read_text(encoding="utf-8"))["cameras"].items()
            if k.startswith("parent")
        ),
        key=lambda c: c["utc_at_video_zero_s"],
    )
    rows, crops, events = [], [], []
    seed_index, next_utc, tracker = 0, lo, None
    for ci, clock in enumerate(clocks):
        start = max(lo, clock["utc_at_video_zero_s"])
        end = min(hi, clocks[ci + 1]["utc_at_video_zero_s"] if ci + 1 < len(clocks) else hi)
        if end <= start:
            continue
        source = Path(clock["source_video"]["path"]).with_suffix(".LRF")
        cap = cv2.VideoCapture(str(source))
        cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 0)
        cap.set(
            cv2.CAP_PROP_POS_MSEC,
            (start - clock["utc_at_video_zero_s"]) / clock["time_scale"] * 1000,
        )
        next_utc = max(next_utc, start)
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            sec = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
            utc = clock["utc_at_video_zero_s"] + clock["time_scale"] * sec
            if utc >= end:
                break
            if utc < next_utc - 1e-5:
                continue
            next_utc += 0.1
            # Use a seed only on its reviewed frame (native rounding <= 50 ms).
            while seed_index < len(seeds) and seeds[seed_index]["utc_s"] < utc - 0.075:
                seed_index += 1
            anchor = None
            if seed_index < len(seeds) and abs(seeds[seed_index]["utc_s"] - utc) <= 0.075:
                anchor = seeds[seed_index]
                tracker = (
                    BeaconTracker(anchor["seed_px"]) if anchor["seed_px"] is not None else None
                )
                events.append({**anchor, "actual_utc_s": utc})
                seed_index += 1
            det = (
                tracker.update(frame)
                if tracker is not None
                else {
                    "candidate": False,
                    "x_px": np.nan,
                    "y_px": np.nan,
                    "track_status": "no_reviewed_visible_light",
                    "template_ncc": np.nan,
                }
            )
            rows.append(
                {
                    "utc_s": utc,
                    "segment_time_s": utc - lo,
                    "source_video": source.with_suffix(".MP4").name,
                    "actual_source_s": sec,
                    "x_px": det["x_px"],
                    "y_px": det["y_px"],
                    "optical_candidate": det["candidate"],
                    "water_like_heuristic": det["candidate"],
                    "track_status": det["track_status"],
                    "template_ncc": det["template_ncc"],
                }
            )
            if det["candidate"] and (len(rows) % 100 == 1 or anchor is not None):
                preview = frame.copy()
                center = (float(det["x_px"]), float(det["y_px"]))
                cv2.circle(preview, tuple(map(round, center)), 12, (0, 0, 255), 1)
                patch = cv2.getRectSubPix(preview, (200, 160), center)
                cv2.putText(
                    patch,
                    f"{utc - lo:.1f}s",
                    (4, 16),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.45,
                    (0, 0, 255),
                    1,
                )
                crops.append(patch)
        cap.release()
    write(folder / "camera_relative_candidates.csv", rows)
    for page, offset in enumerate(range(0, len(crops), 40)):
        frames = crops[offset : offset + 40]
        while len(frames) % 8:
            frames.append(np.zeros((160, 200, 3), np.uint8))
        cv2.imwrite(
            str(folder / f"tracked_crops_{page:02d}.jpg"),
            np.vstack([np.hstack(frames[i : i + 8]) for i in range(0, len(frames), 8)]),
        )
    manifest = {
        "experiment_id": eid,
        "sample_hz": 10,
        "rows": len(rows),
        "accepted": sum(r["optical_candidate"] for r in rows),
        "status_counts": dict(Counter(r["track_status"] for r in rows)),
        "reviewed_initializations": events,
        "coordinate_smoothing": False,
        "failed_track_policy": "NaN; no coordinate interpolation or automatic global reacquisition",
        "manual_review_scope": "Identity at initialization frames and sampled accepted crops; not pixel ground truth",
        "inputs_sha256": {
            str(p): sha256(p)
            for p in [
                review_path,
                static_path,
                windows_path,
                sync_path,
                Path(__file__),
                Path("src/aquabeacon_analysis/beacon_tracker.py"),
                Path("src/aquabeacon_analysis/beacon_halo.py"),
            ]
        },
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(eid, manifest["accepted"], "/", len(rows), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", action="append", required=True)
    parser.add_argument("--reuse-static-only", action="store_true")
    args = parser.parse_args()
    for eid in args.experiment:
        if not args.reuse_static_only:
            run(eid)
        reuse_static(eid)
