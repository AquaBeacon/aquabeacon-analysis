"""Bounded child-only SfM trial from transferred, clocked MP4 clips.

Writes a separate run; never promotes arbitrary units to metric coordinates.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
import cv2
import numpy as np

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.experiments import load_experiment
from aquabeacon_analysis.ngt_binary import sha256


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
                    encoding="utf-8")


def write_csv(path, rows, fields=None):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or list(dict.fromkeys(k for r in rows for k in r)))
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def clip_utc(clock, clip_s):
    """Map an actual decoded presentation time using the containing clip piece."""
    pieces = [p for p in clock["pieces"]
              if p["joined_start_s"] <= clip_s < p["joined_end_s"]]
    if len(pieces) != 1:
        raise ValueError("Decoded frame outside a uniquely clocked piece")
    piece = pieces[0]
    source_s = piece["source_start_s"] + clip_s - piece["joined_start_s"]
    bounds = piece["validated_source_interval_s"]
    return (piece["utc_at_source_zero_s"] + source_s * piece["time_scale"],
            source_s, not bounds[0] <= source_s <= bounds[1])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--experiment", required=True,
                   choices=["20261101_nanyo_004", "20261101_nanyo_005"])
    p.add_argument("--run", required=True)
    p.add_argument("--start", required=True, type=float)
    p.add_argument("--stop", required=True, type=float)
    p.add_argument("--hz", required=True, type=float)
    p.add_argument("--runtime", type=Path)
    p.add_argument("--mapper-seconds", type=int, default=120)
    a = p.parse_args()
    if not a.run.replace("_", "").isalnum():
        p.error("run must be alphanumeric with underscores")
    if not (0 <= a.start < a.stop and 0 < a.hz <= 10
            and (a.stop - a.start) * a.hz <= 500 and 1 <= a.mapper_seconds <= 300):
        p.error("Require <=500 frames, <=10 Hz and <=300 mapper seconds")
    root = Path(__file__).resolve().parents[1]
    exp = load_experiment(root, a.experiment)
    if a.runtime:
        sys.path.insert(0, str(a.runtime.resolve()))
    import pycolmap as pc

    cv2.setNumThreads(2)
    video = exp.stage_path("processed") / "videos/child.mp4"
    clock_path = video.with_suffix(".clock.json")
    clock = json.loads(clock_path.read_text(encoding="utf-8"))
    out = exp.stage_path("results") / "mapping_refinement" / a.run
    work = exp.stage_path("interim") / "mapping_refinement" / a.run
    if out.exists() or work.exists():
        raise FileExistsError("Choose a new run; existing results are preserved")
    images = work / "images"
    images.mkdir(parents=True)
    out.mkdir(parents=True)
    manifest = {
        "experiment_id": a.experiment, "run": a.run,
        "started_utc": datetime.now(UTC).isoformat(), "pid": os.getpid(),
        "method": "child-only COLMAP; separate exploratory MP4 trial",
        "selection": "existing navigation coverage and visual review; not a held-out test",
        "clip_interval_s": [a.start, a.stop], "sampling_hz": a.hz,
        "source": str(video.relative_to(root)), "source_bytes": video.stat().st_size,
        "source_sha256": sha256(video), "clock_sha256": sha256(clock_path),
        "code_sha256": sha256(Path(__file__)),
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                                           text=True).strip(),
        "pycolmap_version": pc.__version__, "threads": 2,
        "random_seed": 20261103, "mapper_soft_limit_s": a.mapper_seconds,
        "scale": "arbitrary monocular units", "accuracy_validated": False,
        "static_scene_validated": False, "navigation_prior_used": False,
        "comparison_caveat": "Sampling, MP4 versus LRF and intrinsics differ; not an isolated rate test",
    }
    dump(out / "manifest.json", manifest)
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened() or cap.get(cv2.CAP_PROP_ORIENTATION_META) != 0:
        raise ValueError("This trial requires a readable zero-rotation MP4")
    manifest["source_fps"] = cap.get(cv2.CAP_PROP_FPS)
    manifest["source_size_px"] = [int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                                  int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))]
    if manifest["source_size_px"] != [1920, 1080]:
        raise ValueError("Review calibration for this image size")
    k, dist, provenance = load_camera_intrinsics(root, "C_MAIN_CAM", (960, 540), "main")
    manifest["intrinsics"] = provenance
    clahe = cv2.createCLAHE(2.0, (8, 8))
    frames = []
    last_pts = -1.0
    try:
        for i, requested in enumerate(np.arange(a.start, a.stop, 1 / a.hz)):
            cap.set(cv2.CAP_PROP_POS_MSEC, requested * 1000)
            ok, frame = cap.read()
            pts = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
            if not ok or pts <= last_pts or abs(pts - requested) > 0.04:
                raise ValueError(f"Frame timing/read failure at {requested}: {pts}")
            last_pts = pts
            utc, source_s, extrapolated = clip_utc(clock, pts)
            frame = cv2.resize(frame, (960, 540), interpolation=cv2.INTER_AREA)
            lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
            lab[:, :, 0] = clahe.apply(lab[:, :, 0])
            name = f"{i:06d}.jpg"
            cv2.imwrite(str(images / name), cv2.cvtColor(lab, cv2.COLOR_LAB2BGR))
            frames.append({"image": name, "requested_clip_s": requested, "clip_pts_s": pts,
                           "source_video_s": source_s, "utc_s": utc,
                           "clock_extrapolated": extrapolated,
                           "sha256": sha256(images / name)})
    finally:
        cap.release()
    write_csv(out / "frames.csv", frames)
    reader = pc.ImageReaderOptions()
    reader.camera_model = "OPENCV"
    reader.camera_params = ",".join(map(str, [k[0, 0], k[1, 1], k[0, 2], k[1, 2], *dist[:4]]))
    extraction = pc.FeatureExtractionOptions()
    extraction.num_threads = 2
    extraction.sift.max_num_features = 2048
    db = work / "features.db"
    pc.extract_features(db, images, camera_mode=pc.CameraMode.SINGLE, reader_options=reader,
                        extraction_options=extraction, device=pc.Device.cpu)
    matching = pc.FeatureMatchingOptions()
    matching.num_threads = 2
    pairing = pc.SequentialPairingOptions()
    pairing.overlap = 10
    pairing.quadratic_overlap = False
    pc.match_sequential(db, matching_options=matching, pairing_options=pairing, device=pc.Device.cpu)
    opts = pc.IncrementalPipelineOptions()
    opts.num_threads = 2
    opts.ba_refine_focal_length = False
    opts.ba_refine_extra_params = False
    opts.ba_refine_principal_point = False
    opts.mapper.abs_pose_refine_focal_length = False
    opts.mapper.abs_pose_refine_extra_params = False
    opts.max_runtime_seconds = a.mapper_seconds
    opts.max_num_models = 10
    opts.init_num_trials = 100
    opts.random_seed = 20261103
    models = pc.incremental_mapping(db, images, work / "models", options=opts)
    nav = read_csv(exp.stage_path("results") / "mapping_navigation/child_navigation.csv")
    nt = np.array([float(r["utc_s"]) for r in nav])
    good = np.array([r["position_available"] == "True"
                     and float(r["anchor_age_s"]) <= 1 for r in nav])
    lookup = {r["image"]: r for r in frames}
    registered = set()
    summaries = []
    for mid, model in models.items():
        model.export_PLY(out / f"model_{mid}_LOCAL_UNITS.ply")
        poses = []
        for im in model.images.values():
            registered.add(im.name)
            t = lookup[im.name]["utc_s"]
            idx = np.searchsorted(nt, t)
            j = min(max(idx, 1), len(nt)-1)
            anchor = bool(good[j-1] and good[j] and nt[j]-nt[j-1] <= 0.15)
            center = im.projection_center()
            row = {"image": im.name, "utc_s": t, "clip_pts_s": lookup[im.name]["clip_pts_s"],
                   "x_local": center[0], "y_local": center[1], "z_local": center[2],
                   "recent_navigation_bracket": anchor}
            poses.append(row)
        poses.sort(key=lambda r: r["utc_s"])
        write_csv(out / f"model_{mid}_poses.csv", poses)
        summaries.append({"model": int(mid), "registered_images": len(poses),
                          "points": model.num_points3D(),
                          "mean_reprojection_error_px": model.compute_mean_reprojection_error(),
                          "recent_navigation_brackets": sum(r["recent_navigation_bracket"] for r in poses),
                          "utc_interval_s": [poses[0]["utc_s"], poses[-1]["utc_s"]]})
    for row in frames:
        row["registered"] = row["image"] in registered
    write_csv(out / "frame_status.csv", frames)
    old_folder = exp.stage_path("results") / "visual_mapping"
    old_times = {r["image"]: float(r["sync_utc_s"])
                 for r in read_csv(old_folder / "frame_times.csv")}
    lo, _, _ = clip_utc(clock, a.start)
    hi, _, _ = clip_utc(clock, a.stop)
    old_frames = [r for r in read_csv(old_folder / "frame_status.csv")
                  if lo <= old_times[r["image"]] < hi]
    manifest.update(models=summaries, sampled_frames=len(frames), registered_frames=len(registered),
                    old_same_interval_frames=len(old_frames),
                    old_same_interval_registered=sum(r["registered"] == "True" for r in old_frames),
                    finished_utc=datetime.now(UTC).isoformat(), status="completed")
    dump(out / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
