"""Evaluate reviewed pool detections and prepare bounded old/new mapping inputs."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
import cv2
import numpy as np
from evaluate_ngt_position import build_observations
from evaluate_position_fallback import ARGUMENTS, dump, finite, output_rows, scatter, write_csv
from improve_nanyo_light import ROOT, read, relative
from improve_ngt_light import CONFIG, folder
from position_fallback_ngt import load_ngt, normalize_observations, numbers

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.camera_geometry import resize_intrinsics_pixel_centers, rotate_intrinsics
from aquabeacon_analysis.nanyo_followup import bracket_values
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.position_fallback import run_fallback


def load_recovered(eid, optical_path):
    data = load_ngt(eid)
    source = ROOT/f"results/{eid}/position_evaluation/rotation_corrected_20261031/manifest.json"
    meta = json.loads(source.read_text())
    config, frame = meta["config"], meta["frame_calibration"]
    config = {**config, "optical_to_camera_imu_rotation": frame["R_camera_imu_from_display_optical"]}
    k, d = np.array(frame["parent_proxy_matrix_display"]), np.array(frame["parent_proxy_distortion_display"])
    body = np.array(meta["R_body_from_camera_imu"])
    acq = "20261028_ngt_000"
    paths = {"optical": optical_path,
             "ranges": ROOT/f"data/processed/{acq}/fusion/range_constraints.csv",
             "attitude": ROOT/f"data/processed/{acq}/parent_magnetic_attitude/attitude.csv",
             "gnss": ROOT/f"results/{acq}/gnss_selection/selected_positions.csv",
             "pressure": ROOT/f"data/processed/{acq}/fusion/child_depth.csv",
             "telemetry": ROOT/f"data/interim/{acq}/cameras/parent_telemetry.csv"}
    tables = {key: read(path) for key, path in paths.items()}
    sync_path = ROOT/f"results/{acq}/synchronization/manifest.json"
    sync = json.loads(sync_path.read_text())["cameras"]["parent"]
    times = np.array([r["parent_video_s"] for r in data["records"]])
    rows = build_observations(times, **tables, cfg=config, sync=sync, k=k, d=d, body_cam=body)
    updated = normalize_observations(rows, times, [r["grid_sample"] for r in data["records"]],
                                     [r["event_sample"] for r in data["records"]], tables["attitude"], config, k, d, body)
    data.update(updated)
    for record in data["records"]:
        record["optical_source"] = relative(optical_path)
    data["input_paths"] += [optical_path, Path(__file__)]
    return data


def prepare_mapping(eid, cfg, out, data, results):
    source = ROOT/f"data/interim/{eid}/child_only_vslam/okinawa_native120"
    export_path = source/"export_manifest.json"
    export = json.loads(export_path.read_text())
    if export["resize"] != [1280, 720] or export["native_fps"] != "120000/1001":
        raise ValueError("Review native child image geometry before reuse")
    frames_path = source/"frames.csv"
    frames = read(frames_path)
    sync_path = ROOT/"results/20261028_ngt_000/synchronization/manifest.json"
    sync = json.loads(sync_path.read_text())["cameras"]
    ft = sync["child"]["utc_at_video_zero_s"]+sync["child"]["time_scale"]*numbers(frames, ["source_video_s"])[:, 0]
    pt = (ft-sync["parent"]["utc_at_video_zero_s"])/sync["parent"]["time_scale"]
    lo, hi = data["metadata"]["parent_video_interval_s"]
    requested = np.arange(lo+.05, hi, 1/cfg["mapping_hz"][eid])
    indices = np.array([int(np.argmin(abs(pt-t))) for t in requested])
    if len(indices) > cfg["maximum_images"] or len(set(indices)) != len(indices) or np.max(abs(pt[indices]-requested)) > .01:
        raise ValueError("Mapping selection must be bounded, unique and have native timestamps")
    work = ROOT/f"data/interim/{eid}/optical_followup/{cfg['run']}/inputs"
    images = work/"images"; images.mkdir(parents=True, exist_ok=False)
    target_times = ft[indices]
    priors = {label: bracket_values(data["times"], results[label]["selected"]+data["parent_origin"],
                                    target_times, cfg["mapping_prior_max_gap_s"]) for label in results}
    rows = []
    for j, index in enumerate(indices):
        row = frames[index]
        name = row["synthetic_ns"]+".jpg"
        path = source/"mav0/cam0/data"/name
        if sha256(path) != row["image_sha256"]:
            raise ValueError("Saved native child image changed")
        shutil.copy2(path, images/name)
        record = {"image": name, "image_sha256": row["image_sha256"], "utc_s": ft[index],
                  "parent_video_s": pt[index], "source_video_s": row["source_video_s"],
                  "source_frame": row["source_frame"], "source_image": relative(path)}
        for label in priors:
            record[label+"_available"] = bool(np.isfinite(priors[label][j]).all())
            record.update({f"{label}_{axis}_m": value for axis, value in zip("enu", priors[label][j])})
        rows.append(record)
    # Verify automatic display rotation against the raw MP4 at three selected frames.
    video = ROOT/export["camera_source"].replace("\\", "/")
    cap = cv2.VideoCapture(str(video), cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 2])
    checks = []
    try:
        if not cap.isOpened() or int(cap.get(cv2.CAP_PROP_ORIENTATION_META)) != 180:
            raise ValueError("Child display rotation is not the audited 180 degrees")
        for index in sorted({0, len(rows)//2, len(rows)-1}):
            row = rows[index]; cap.set(cv2.CAP_PROP_POS_FRAMES, int(row["source_frame"]))
            ok, frame = cap.read()
            if not ok or abs(cap.get(cv2.CAP_PROP_POS_MSEC)/1000-float(row["source_video_s"])) > .001:
                raise ValueError("Saved child image time cannot be reproduced")
            frame = cv2.resize(frame, (1280, 720), interpolation=cv2.INTER_AREA)
            saved = cv2.imread(str(images/row["image"]))
            direct = float(np.mean(abs(frame.astype(float)-saved)))
            opposite = float(np.mean(abs(cv2.rotate(frame, cv2.ROTATE_180).astype(float)-saved)))
            if direct >= opposite/2:
                raise ValueError("Saved child image rotation not verified")
            checks.append({"image": row["image"], "direct_mean_absolute_difference": direct, "opposite_rotation_difference": opposite})
    finally:
        cap.release()
    k, d, provenance = load_camera_intrinsics(ROOT, "C_MAIN_CAM", (1920, 1080), "main")
    k = resize_intrinsics_pixel_centers(k, (1920, 1080), (1280, 720))
    k, d, _, size = rotate_intrinsics(k, d, (1280, 720), 2)
    k_colmap = k.copy(); k_colmap[:2, 2] += .5
    write_csv(out/"mapping_frame_priors.csv", rows)
    dump(out/"mapping_input.json", {"source_work": relative(work), "selected_images": len(rows),
          "parent_interval_s": [rows[0]["parent_video_s"], rows[-1]["parent_video_s"]],
          "intrinsics": {"camera_matrix_px": k_colmap.tolist(), "opencv_matrix_px": k.tolist(),
                         "distortion": d.tolist(), "size": list(size), "provenance": provenance},
          "orientation_checks": checks, "coordinate_frame": "magnetic ENU relative plus GNSS ENU with assumed zero declination",
          "centers": "Coincident camera/light/acoustic/GNSS centers, unsurveyed; same pool baseline convention",
          "missing_policy": "finite exact or two finite immediate neighbors <=0.25 seconds; no extrapolation",
          "selection": "Uniform full-trial sampling predeclared before mapping; original images and native timestamps retained"})
    return [export_path, frames_path, sync_path, Path(provenance["source"]), ROOT/"calibration/camera_defaults.json",
            ROOT/"src/aquabeacon_analysis/camera_geometry.py", ROOT/"src/aquabeacon_analysis/nanyo_followup.py"]


def analyze(eid, cfg):
    out = folder(eid, cfg)
    if (out/"analysis_manifest.json").exists() or (out/"old_positions.csv").exists():
        raise FileExistsError("Preserve previous position and mapping inputs")
    review = json.loads((out/"identity_review_manifest.json").read_text())
    optical_path = out/"reviewed_candidates.csv"
    if sha256(optical_path) != review["output_sha256"]:
        raise ValueError("Reviewed optical inputs changed")
    config_path = ROOT/cfg["position_config"]
    geometry = json.loads(config_path.read_text())["geometry"]
    datasets = {"old": load_ngt(eid), "new": load_recovered(eid, optical_path)}
    np.testing.assert_array_equal(datasets["old"]["times"], datasets["new"]["times"])
    results, counts, metrics = {}, [], []
    inputs = [CONFIG, config_path, Path(__file__), out/"identity_review_manifest.json", optical_path,
              ROOT/"src/aquabeacon_analysis/position_fallback.py", ROOT/"scripts/evaluate_position_fallback.py"]
    for label, data in datasets.items():
        grid = np.array([r["grid_sample"] for r in data["records"]])
        events = np.array([r["event_sample"] for r in data["records"]])
        results[label] = run_fallback(*(data[k] for k in ARGUMENTS), config=geometry, surface_seed_mask=events)
        write_csv(out/f"{label}_positions.csv", output_rows(data, results[label]))
        row = {"experiment_id": eid, "version": label, "grid_samples": int(grid.sum()),
               "optical_samples": int(sum(r["optical_available"] for r, g in zip(data["records"], grid) if g))}
        for method in ("joint", "optical_imu_depth", "acoustic_imu_depth", "selected"):
            row[method+"_samples"] = int(finite(results[label][method])[grid].sum())
        counts.append(row); inputs += data["input_paths"]
    data = datasets["new"]
    for window in data["metadata"]["static_windows"]:
        inside = grid & (data["times"] >= window["start_s"]) & (data["times"] < window["end_s"])
        for method in ("joint", "optical_imu_depth", "acoustic_imu_depth", "selected"):
            paired = inside & finite(results["old"][method]) & finite(results["new"][method])
            for sampling, mask in (("all_available", inside), ("paired_same_times", paired)):
                for label in results:
                    metrics.append({"experiment_id": eid, "window": window["id"], "condition": window["condition"],
                                    "method": method, "sampling": sampling, "version": label,
                                    **scatter(results[label][method][mask])})
    write_csv(out/"availability.csv", counts)
    if metrics:
        write_csv(out/"paired_static_rms.csv", metrics)
    inputs += prepare_mapping(eid, cfg, out, data, results)
    dump(out/"analysis_manifest.json", {"experiment_id": eid, "run": cfg["run"], "metadata": data["metadata"],
          "inputs_sha256": {relative(p): sha256(p) for p in set(inputs)}, "pid": os.getpid(),
          "created_utc": datetime.now(UTC).isoformat(), "accuracy_validated": False,
          "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()})
    print(eid, "positions and mapping inputs prepared", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__); parser.add_argument("--experiment", required=True, action="append")
    args = parser.parse_args(); cfg = json.loads(CONFIG.read_text())
    if set(args.experiment)-set(cfg["experiments"]):
        parser.error("Explicit pool trials required")
    cv2.setNumThreads(1)
    print("pool position PID", os.getpid(), datetime.now(UTC).isoformat(), flush=True)
    for eid in dict.fromkeys(args.experiment):
        analyze(eid, cfg)
