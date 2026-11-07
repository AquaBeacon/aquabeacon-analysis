"""Bounded pool SfM with identical child features and old/new fallback position priors."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
import numpy as np
from evaluate_position_fallback import dump, write_csv
from improve_nanyo_light import ROOT, read, relative
from improve_ngt_light import CONFIG, folder

from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.restart_mapping import position_registration


def placement(centers, targets, eid, points, min_motion):
    good = np.isfinite(targets).all(1)
    fit = position_registration(centers[good], targets[good], min_ratio=1e-3, huber_delta_m=1.)
    fit["navigation_span_m"] = float(np.linalg.norm(np.ptp(targets[good], axis=0))) if good.any() else 0.
    if eid.endswith("002"):
        fit.update(accepted=False, reason="declared static trial: position scatter is not a metric scale baseline")
    elif fit["navigation_span_m"] < min_motion:
        fit.update(accepted=False, reason="insufficient navigation motion")
    elif fit.get("scale", 0) <= 0 and fit["accepted"]:
        fit.update(accepted=False, reason="non-positive scale")
    elif fit.get("fit_median_m", float("inf")) > 5 and fit["accepted"]:
        fit.update(accepted=False, reason="position disagreement exceeds 5 m")
    elif points < 50:
        fit.update(accepted=False, reason="fewer than fifty points")
    fit["accuracy_validated"] = False
    fit["orientation_source"] = "position correspondences, no independent child attitude constraint"
    return fit


def worker(eid, cfg):
    sys.path.insert(0, str(ROOT/cfg["runtime"]))
    import pycolmap as pc
    out = folder(eid, cfg)
    work = ROOT/f"data/interim/{eid}/optical_followup/{cfg['run']}"
    database = work/"features.db"
    if database.exists() or (out/"mapping.json").exists():
        raise FileExistsError("Preserve completed/incomplete mapping")
    prior_path = out/"mapping_frame_priors.csv"
    rows = read(prior_path); lookup = {r["image"]: r for r in rows}
    info = json.loads((out/"mapping_input.json").read_text())
    images = ROOT/info["source_work"]/"images"
    if len(rows) > cfg["maximum_images"]:
        raise ValueError("Image budget exceeded")
    for row in rows:
        if sha256(images/row["image"]) != row["image_sha256"]:
            raise ValueError("Child image changed")
    k = np.array(info["intrinsics"]["camera_matrix_px"]); d = info["intrinsics"]["distortion"]
    params = [k[0, 0], k[1, 1], k[0, 2], k[1, 2], *d[:4]]
    reader = pc.ImageReaderOptions(camera_model="OPENCV", camera_params=",".join(map(str, params)))
    if "feature_mask_xyxy" in cfg:
        from PIL import Image, ImageDraw
        masks = work/"feature_masks"; masks.mkdir()
        mask = Image.new("L", tuple(info["intrinsics"]["size"]), 0)
        x0, y0, x1, y1 = cfg["feature_mask_xyxy"]
        ImageDraw.Draw(mask).rectangle((x0, y0, x1-1, y1-1), fill=255)
        for row in rows:
            mask.save(masks/(row["image"]+".png"))
        reader.mask_path = str(masks)
    extraction = pc.FeatureExtractionOptions(); extraction.num_threads = 2
    extraction.max_image_size = cfg.get("feature_max_image_size", 960)
    extraction.sift.max_num_features = 2048; extraction.sift.first_octave = 0
    if "feature_peak_threshold" in cfg:
        extraction.sift.peak_threshold = cfg["feature_peak_threshold"]
    start = time.monotonic()
    pc.extract_features(database, images, camera_mode=pc.CameraMode.SINGLE, reader_options=reader,
                        extraction_options=extraction, device=pc.Device.cpu)
    matching = pc.FeatureMatchingOptions(); matching.num_threads = 2
    pairing = pc.SequentialPairingOptions(); pairing.overlap = 10; pairing.quadratic_overlap = False
    geometry = pc.TwoViewGeometryOptions(); geometry.ransac.random_seed = 20261104
    pc.match_sequential(database, matching_options=matching, pairing_options=pairing,
                        verification_options=geometry, device=pc.Device.cpu)
    feature_seconds = time.monotonic()-start
    modes = []
    for label in ("old", "new"):
        target = work/f"{label}.db"; shutil.copy2(database, target)
        prior_count = 0
        with pc.Database.open(target) as db:
            db.clear_pose_priors()
            cameras = db.read_all_cameras()
            if len(cameras) != 1:
                raise ValueError("Expected a single fixed camera")
            np.testing.assert_allclose(cameras[0].params, params, atol=1e-10, rtol=0)
            for im in db.read_all_images():
                row = lookup[im.name]
                if row[label+"_available"] != "True":
                    continue
                prior = pc.PosePrior(); prior.corr_data_id = im.data_id
                prior.position = [float(row[f"{label}_{a}_m"]) for a in "enu"]
                prior.position_covariance = np.diag(cfg["position_prior_sigma_m"])**2
                prior.coordinate_system = pc.PosePriorCoordinateSystem.CARTESIAN
                db.write_pose_prior(prior); prior_count += 1
        options = pc.IncrementalPipelineOptions(num_threads=2, random_seed=20261104,
            image_names=list(lookup), max_num_models=5, min_model_size=5, init_num_trials=80,
            ba_refine_focal_length=False, ba_refine_principal_point=False, ba_refine_extra_params=False,
            ba_local_max_num_iterations=20, ba_global_max_num_iterations=40,
            ba_min_num_residuals_for_cpu_multi_threading=1, max_runtime_seconds=cfg["mapper_seconds"],
            use_prior_position=prior_count >= 3, use_robust_loss_on_prior_position=prior_count >= 3)
        options.mapper.num_threads = 2; options.mapper.init_min_num_inliers = 30
        options.mapper.init_min_tri_angle = 1.5; options.mapper.abs_pose_min_num_inliers = 20
        options.mapper.abs_pose_refine_focal_length = False; options.mapper.abs_pose_refine_extra_params = False
        token = pc.CancellationToken(); timer = threading.Timer(cfg["mapper_seconds"]+20, token.cancel)
        timer.daemon = True; timer.start(); start = time.monotonic()
        try:
            models = pc.incremental_mapping(target, images, work/label, options=options, cancellation_token=token)
        finally:
            timer.cancel()
        mode = {"version": label, "images": len(rows), "position_priors": prior_count, "models": [],
                "elapsed_s": time.monotonic()-start, "options": str(options),
                "cancelled_at_deadline": bool(token.is_cancelled)}
        registered = set()
        for mid, model in models.items():
            ims = sorted(model.images.values(), key=lambda im: im.name)
            registered.update(im.name for im in ims)
            centers = np.array([im.projection_center() for im in ims])
            targets = np.array([[float(lookup[im.name][f"{label}_{a}_m"]) for a in "enu"] for im in ims])
            fit = placement(centers, targets, eid, model.num_points3D(), cfg["mapping_min_motion_m"])
            if fit["accepted"]:
                before = model.compute_mean_reprojection_error()
                model.transform(pc.Sim3d(fit["scale"], pc.Rotation3d(np.array(fit["rotation"])), np.array(fit["translation"])))
                if abs(before-model.compute_mean_reprojection_error()) > 1e-6:
                    raise ValueError("Similarity must preserve reprojection")
                dest = work/f"{label}_metric/{mid}"; dest.mkdir(parents=True); model.write(dest)
            unit = "m_provisional" if fit["accepted"] else "local_units"
            name = f"{label}_model_{mid}"
            model.export_PLY(out/f"{name}_{unit}.ply")
            poses = []
            for im in ims:
                center = im.projection_center(); row = lookup[im.name]
                poses.append({"image": im.name, "utc_s": row["utc_s"], "unit": unit,
                              "x": center[0], "y": center[1], "z": center[2],
                              "prior_available": row[label+"_available"]})
            write_csv(out/f"{name}_cameras.csv", poses)
            mode["models"].append({"model": int(mid), "registered_images": len(ims), "points": model.num_points3D(),
                "mean_reprojection_error_px": model.compute_mean_reprojection_error(), "placement": fit,
                "unit": unit, "ply": f"{name}_{unit}.ply", "accuracy_validated": False, "static_scene_validated": False})
        mode["registered_unique"] = len(registered); modes.append(mode)
        print(eid, label, "priors", prior_count, "registered", len(registered), flush=True)
    inputs = [CONFIG, prior_path, out/"mapping_input.json", database, Path(__file__),
              ROOT/"src/aquabeacon_analysis/restart_mapping.py"]
    dump(out/"mapping.json", {"experiment_id": eid, "run": cfg["run"], "status": "completed", "modes": modes,
          "feature_matching_seconds": feature_seconds, "feature_extraction_options": str(extraction),
          "matching_options": str(matching), "pairing_options": str(pairing),
          "pycolmap_version": pc.__version__, "cuda_available": pc.has_cuda,
          "input_images_same_between_versions": True, "accuracy_validated": False,
          "inputs_sha256": {relative(p): sha256(p) for p in inputs}, "created_utc": datetime.now(UTC).isoformat()})


def supervise(eids, cfg):
    for eid in eids:
        out = folder(eid, cfg)
        work = ROOT/f"data/interim/{eid}/optical_followup/{cfg['run']}"
        if (work/"features.db").exists() or any((out/name).exists() for name in ("mapping.json", "mapping.log", "mapping_process.json")):
            raise FileExistsError("Preserve mapping results, partial runs and logs")
    processes = []
    for eid in eids:
        out = folder(eid, cfg)
        command = [sys.executable, str(Path(__file__).resolve()), "--experiment", eid, "--worker", "--config", str(CONFIG)]
        record = {"experiment_id": eid, "command": command, "task_owned": True,
                  "purpose": "pool paired fallback-constrained mapping", "timeout_s": cfg["worker_timeout_s"]}
        with (out/"mapping.log").open("x", encoding="utf-8") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
            record.update(pid=process.pid, started_utc=datetime.now(UTC).isoformat(), status="running")
            dump(out/"mapping_process.json", record)
            print(eid, "worker PID", process.pid, record["started_utc"], flush=True)
            try:
                code = process.wait(timeout=cfg["worker_timeout_s"])
                record["status"] = "completed" if code == 0 else "failed"
            except subprocess.TimeoutExpired:
                record["status"] = "timeout"
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=5)
                record.update(returncode=process.returncode, ended_utc=datetime.now(UTC).isoformat())
                dump(out/"mapping_process.json", record)
            print(eid, record["status"], flush=True)
        processes.append(record)
    out = folder("20261028_ngt_000", cfg); out.mkdir(parents=True, exist_ok=True)
    dump(out/"mapping_processes.json", processes)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__); parser.add_argument("--experiment", required=True, action="append")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args(); CONFIG = args.config.resolve(); cfg = json.loads(CONFIG.read_text())
    if set(args.experiment)-set(cfg["experiments"]) or (args.worker and len(args.experiment) != 1):
        parser.error("Explicit pool trials and one trial per worker required")
    print("pool mapping PID", os.getpid(), datetime.now(UTC).isoformat(), flush=True)
    (worker(args.experiment[0], cfg) if args.worker else supervise(list(dict.fromkeys(args.experiment)), cfg))
