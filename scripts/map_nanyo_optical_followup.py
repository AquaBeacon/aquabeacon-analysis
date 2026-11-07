"""Bounded, sequential old/new fallback-prior COLMAP trials on identical saved images."""
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
from analyze_nanyo_optical_followup import CONFIG
from evaluate_position_fallback import dump, write_csv
from improve_nanyo_light import ROOT, read, relative

from aquabeacon_analysis.nanyo_followup import positive_position_fit
from aquabeacon_analysis.nanyo_position import robust_rotation_mean
from aquabeacon_analysis.ngt_binary import sha256


def preview(model, path, unit):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from aquabeacon_analysis.editable_plots import save_editable_figure
    points = list(model.points3D.values())
    if not points:
        return
    points = points[::max(1, len(points)//15000)]
    xyz = np.array([p.xyz for p in points])
    rgb = np.array([p.color for p in points])/255
    centers = np.array([im.projection_center() for im in sorted(model.images.values(), key=lambda im: im.name)])
    fig = plt.figure(figsize=(7, 5))
    ax = fig.add_subplot(projection="3d")
    ax.scatter(*xyz.T, c=rgb, s=2, alpha=.7)
    ax.plot(*centers.T, color="#d65f00", lw=1, label="Camera path")
    ax.set(xlabel=f"X [{unit}]", ylabel=f"Y [{unit}]", zlabel=f"Z [{unit}]")
    span = np.ptp(xyz, axis=0)
    ax.set_box_aspect(np.maximum(span, max(span.max()*.1, .001)))
    ax.legend()
    save_editable_figure(fig, path)
    plt.close(fig)


def worker(eid, cfg):
    sys.path.insert(0, str(ROOT/cfg["runtime"]))
    import pycolmap as pc
    out = ROOT/f"results/{eid}/optical_followup/{cfg['run']}"
    work = ROOT/f"data/interim/{eid}/optical_followup/{cfg['run']}"
    if work.exists() or (out/"mapping.json").exists():
        raise FileExistsError("Preserving completed/incomplete mapping run; choose a new configured run")
    work.mkdir(parents=True)
    prior_path = out/"mapping_frame_priors.csv"
    rows = read(prior_path)
    lookup = {r["image"]: r for r in rows}
    info = json.loads((out/"mapping_input.json").read_text())
    source = ROOT/info["source_work"]
    images = source/"images"
    for row in rows:
        if sha256(images/row["image"]) != row["image_sha256"]:
            raise ValueError("Source child image changed")
    summaries = []
    for label in ("old", "new"):
        database = work/f"{label}.db"
        shutil.copy2(source/"features.db", database)
        prior_count = 0
        with pc.Database.open(database) as db:
            db.clear_pose_priors()
            cameras = db.read_all_cameras()
            intrinsic = info["intrinsics"]
            k, d = np.array(intrinsic["camera_matrix_px"]), intrinsic["distortion"]
            expected = [k[0, 0], k[1, 1], k[0, 2], k[1, 2], *d[:4]]
            if len(cameras) != 1 or cameras[0].model_name != "OPENCV":
                raise ValueError("Review source camera model before reuse")
            np.testing.assert_allclose(cameras[0].params, expected, rtol=0, atol=1e-10)
            for im in db.read_all_images():
                row = lookup.get(im.name)
                if row is None or row[label+"_available"] != "True":
                    continue
                prior = pc.PosePrior()
                prior.corr_data_id = im.data_id
                prior.position = [float(row[f"{label}_{axis}_m"]) for axis in "enu"]
                prior.position_covariance = np.diag(cfg["position_prior_sigma_m"])**2
                prior.coordinate_system = pc.PosePriorCoordinateSystem.CARTESIAN
                db.write_pose_prior(prior)
                prior_count += 1
        opts = pc.IncrementalPipelineOptions(
            num_threads=2, random_seed=20261104, image_names=list(lookup), max_num_models=5,
            min_model_size=5, init_num_trials=80, ba_refine_focal_length=False,
            ba_refine_extra_params=False, ba_refine_principal_point=False,
            ba_local_max_num_iterations=20, ba_global_max_num_iterations=40,
            max_runtime_seconds=cfg["mapper_seconds"],
            use_prior_position=prior_count >= 3, use_robust_loss_on_prior_position=prior_count >= 3,
            ba_min_num_residuals_for_cpu_multi_threading=1)
        opts.mapper.num_threads = 2
        opts.mapper.init_min_num_inliers = 40
        opts.mapper.init_min_tri_angle = 3.
        opts.mapper.abs_pose_refine_focal_length = False
        opts.mapper.abs_pose_refine_extra_params = False
        opts.mapper.abs_pose_min_num_inliers = 20
        token = pc.CancellationToken()
        timer = threading.Timer(cfg["mapper_seconds"]+20, token.cancel)
        timer.daemon = True
        timer.start()
        start = time.monotonic()
        try:
            models = pc.incremental_mapping(database, images, work/label, options=opts,
                                            cancellation_token=token)
        finally:
            timer.cancel()
        mode = {"version": label, "images": len(rows), "position_priors": prior_count,
                "prior_used": prior_count >= 3, "elapsed_s": time.monotonic()-start,
                "cancelled_at_deadline": bool(token.is_cancelled), "models": [],
                "options": str(opts), "registered_unique": 0}
        registered = set()
        for mid, model in models.items():
            ims = sorted(model.images.values(), key=lambda im: im.name)
            registered.update(im.name for im in ims)
            centers = np.array([im.projection_center() for im in ims])
            world_cameras = np.array([[[float(lookup[im.name][f"r{a}{b}"]) for b in range(3)]
                                       for a in range(3)] for im in ims])
            orientations = world_cameras @ np.array([im.cam_from_world().rotation.matrix() for im in ims])
            valid = np.isfinite(orientations).all((1, 2))
            metric = {"accepted": False, "reason": "insufficient synchronized attitude"}
            if valid.sum() >= 3:
                rotation, errors, _ = robust_rotation_mean(orientations[valid], 15)
                targets = np.array([[float(lookup[im.name][f"{label}_{axis}_m"]) for axis in "enu"] for im in ims])
                metric = positive_position_fit(centers @ rotation.T, targets)
                metric.update(orientation_median_deg=float(np.median(errors)),
                              orientation_p95_deg=float(np.percentile(errors, 95)))
                if np.median(errors) > 25 or np.percentile(errors, 95) > 60:
                    metric.update(accepted=False, reason="visual and IMU orientations inconsistent")
                if model.num_points3D() < 50:
                    metric.update(accepted=False, reason="fewer than 50 reconstructed points")
                if metric["accepted"]:
                    before = model.compute_mean_reprojection_error()
                    model.transform(pc.Sim3d(metric["scale_m_per_unit"], pc.Rotation3d(rotation),
                                             np.array(metric["translation_enu_m"])))
                    if abs(model.compute_mean_reprojection_error()-before) > 1e-6:
                        raise ValueError("Similarity changed reprojection error")
            name = f"{label}_model_{mid}"
            unit = "m_provisional" if metric["accepted"] else "local_units"
            model.export_PLY(out/f"{name}_{unit}.ply")
            poses = []
            for im in sorted(model.images.values(), key=lambda im: im.name):
                row = lookup[im.name]
                center = im.projection_center()
                poses.append({"image": im.name, "utc_s": row["utc_s"], "unit": unit,
                              "x": center[0], "y": center[1], "z": center[2],
                              "prior_available": row[label+"_available"]})
            write_csv(out/f"{name}_cameras.csv", poses)
            if metric["accepted"]:
                dest = work/f"{label}_metric/{mid}"
                dest.mkdir(parents=True)
                model.write(dest)
            item = {"model": int(mid), "registered_images": len(ims), "points": model.num_points3D(),
                    "mean_reprojection_error_px": model.compute_mean_reprojection_error(),
                    "placement": metric, "unit": unit, "ply": f"{name}_{unit}.ply",
                    "static_scene_validated": False, "accuracy_validated": False}
            mode["models"].append(item)
        mode["registered_unique"] = len(registered)
        summaries.append(mode)
        dump(out/"mapping.json", {"experiment_id": eid, "run": cfg["run"], "status": "running",
                                  "modes": summaries})
        if models:
            best = max(models, key=lambda key: models[key].num_reg_images())
            item = next(r for r in mode["models"] if r["model"] == best)
            preview(models[best], out/f"{label}_largest_model.png", item["unit"])
        print(eid, label, "priors", prior_count, "registered", len(registered), flush=True)
    inputs = [CONFIG, prior_path, out/"mapping_input.json", source/"features.db", Path(__file__),
              ROOT/"src/aquabeacon_analysis/nanyo_followup.py"]
    dump(out/"mapping.json", {"experiment_id": eid, "run": cfg["run"], "status": "completed",
                              "modes": summaries, "pycolmap_version": pc.__version__, "cuda_available": pc.has_cuda,
                              "input_images_same_between_versions": True,
                              "source_features_reused_read_only": True,
                              "inputs_sha256": {relative(p): sha256(p) for p in inputs},
                              "accuracy_validated": False, "created_utc": datetime.now(UTC).isoformat()})


def supervise(experiments, cfg):
    # Check every target before opening any log or launching a worker.
    for eid in experiments:
        out = ROOT/f"results/{eid}/optical_followup/{cfg['run']}"
        work = ROOT/f"data/interim/{eid}/optical_followup/{cfg['run']}"
        if work.exists() or any((out/name).exists() for name in
                                ("mapping.json", "mapping_process.json", "mapping.log")):
            raise FileExistsError("Preserving completed/incomplete mapping run and logs; choose a new configured run")
    process_records = []
    for eid in experiments:
        out = ROOT/f"results/{eid}/optical_followup/{cfg['run']}"
        command = [sys.executable, str(Path(__file__).resolve()), "--experiment", eid, "--worker"]
        record = {"experiment_id": eid, "command": command, "task_owned": True,
                  "purpose": "paired COLMAP fallback-prior reconstruction", "timeout_s": cfg["worker_timeout_s"]}
        with (out/"mapping.log").open("x", encoding="utf-8") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
            record.update(pid=process.pid, started_utc=datetime.now(UTC).isoformat(), status="running")
            dump(out/"mapping_process.json", record)
            print(eid, "mapper PID", process.pid, record["started_utc"], flush=True)
            try:
                code = process.wait(timeout=cfg["worker_timeout_s"])
                record.update(returncode=code, status="completed" if code == 0 else "failed")
            except subprocess.TimeoutExpired:
                record.update(status="timeout")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                record.update(ended_utc=datetime.now(UTC).isoformat(), returncode=process.returncode)
                dump(out/"mapping_process.json", record)
            print(eid, record["status"], flush=True)
        process_records.append(record)
    summary = ROOT/f"results/20261103_field_review_000/optical_followup/{cfg['run']}"
    summary.mkdir(parents=True, exist_ok=True)
    dump(summary/"mapping_processes.json", process_records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True, action="append")
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    config = json.loads(CONFIG.read_text())
    if set(args.experiment)-set(config["experiments"]) or (args.worker and len(args.experiment) != 1):
        parser.error("Explicit configured trials required; one trial per worker")
    print("mapping manager/worker PID", os.getpid(), datetime.now(UTC).isoformat(), flush=True)
    if args.worker:
        worker(args.experiment[0], config)
    else:
        supervise(list(dict.fromkeys(args.experiment)), config)
