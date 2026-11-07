"""Bounded same-image position-prior ablation and temporal match diagnosis."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
import cv2
import numpy as np
from evaluate_position_fallback import dump, write_csv
from improve_nanyo_light import ROOT, read, relative
from map_pool_optical_followup import placement

from aquabeacon_analysis.mapping_diagnostics import (
    compare_priors,
    temporal_connectivity,
    zero_cut_intervals,
)
from aquabeacon_analysis.ngt_binary import sha256

CONFIG = ROOT/"config/20261103_field_review_000/pool_mapping_diagnosis.json"


def paths(cfg):
    eid, run = cfg["experiment_id"], cfg["run"]
    return (ROOT/f"results/{eid}/mapping_diagnosis/{run}",
            ROOT/f"data/interim/{eid}/mapping_diagnosis/{run}",
            ROOT/f"results/{eid}/optical_followup/{cfg['source_run']}",
            ROOT/f"data/interim/{eid}/optical_followup/{cfg['source_run']}")


def readonly_database(path):
    return sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)


def distribution(values):
    a = np.asarray(values, float); a = a[np.isfinite(a)]
    return {"count": len(a), **{name: float(np.percentile(a, q)) if len(a) else None
            for name, q in (("min", 0), ("p10", 10), ("median", 50), ("p90", 90), ("max", 100))}}


def audit(cfg, pc, out, database, images, rows, info):
    """Read all tested pairs; do not rematch or tune thresholds after results."""
    cv2.setNumThreads(1)
    k, d = np.array(info["intrinsics"]["camera_matrix_px"]), info["intrinsics"]["distortion"]
    camera = pc.Camera(model="OPENCV", width=1280, height=720,
                       params=[k[0, 0], k[1, 1], k[0, 2], k[1, 2], *d[:4]])
    names = {r["image"]: i for i, r in enumerate(rows)}
    pairs, frames = [], []
    with readonly_database(database) as db:
        ids = {int(i): names[n] for i, n in db.execute("SELECT image_id,name FROM images")}
        keypoints = {int(i): np.frombuffer(b, np.float32).reshape(n, c)[:, :2].astype(float)
                     for i, n, c, b in db.execute("SELECT image_id,rows,cols,data FROM keypoints")}
        raw = {int(i): int(n) for i, n in db.execute("SELECT pair_id,rows FROM matches")}
        for pid, n, c, blob, config, eb in db.execute(
                "SELECT pair_id,rows,cols,data,config,E FROM two_view_geometries ORDER BY pair_id"):
            aid, bid = divmod(pid, 2147483647); ai, bi = ids[aid], ids[bid]
            matches = np.frombuffer(blob, np.uint32).reshape(n, c) if n else np.empty((0, 2), np.uint32)
            flow = np.linalg.norm(keypoints[bid][matches[:, 1]]-keypoints[aid][matches[:, 0]], axis=1)
            angle, forward, ok = None, None, False
            # Essential-matrix pose is a conditional two-view estimate, not measured ground truth.
            if config == int(pc.TwoViewGeometryConfiguration.CALIBRATED) and n >= 15 and eb:
                geom = pc.TwoViewGeometry(config=config, E=np.frombuffer(eb, np.float64).reshape(3, 3),
                                         inlier_matches=matches)
                ok = pc.estimate_two_view_geometry_pose(camera, keypoints[aid], camera, keypoints[bid], geom)
                if ok and np.isfinite(geom.tri_angle) and geom.tri_angle >= 0:
                    angle = float(np.degrees(geom.tri_angle))
                    t = np.asarray(geom.cam2_from_cam1.translation)
                    forward = float(abs(t[2])/np.linalg.norm(t)) if np.linalg.norm(t) else None
            pairs.append({"pair_id": pid, "index_a": ai, "index_b": bi,
                          "time_a_s": float(rows[ai]["parent_video_s"]),
                          "time_b_s": float(rows[bi]["parent_video_s"]),
                          "frame_gap": bi-ai, "raw_matches": raw[pid], "inliers": n, "config": config,
                          "median_displacement_px": float(np.median(flow)) if n else None,
                          "pose_estimated": bool(ok), "tri_angle_deg": angle, "forward_fraction": forward,
                          "initial_pair_indicator": bool(n >= cfg["initial_pair_inliers"] and angle is not None
                              and angle >= cfg["initial_triangle_angle_deg"] and forward is not None and forward <= .95)})
    times = np.array([float(r["parent_video_s"]) for r in rows])
    graphs = {}
    for label, predicate in (("verified", lambda p: p["inliers"] >= cfg["minimum_pair_inliers"]),
                             ("initial", lambda p: p["initial_pair_indicator"])):
        graphs[label] = temporal_connectivity(len(rows), [(p["index_a"], p["index_b"]) for p in pairs if predicate(p)])
    for image_id, i in sorted(ids.items(), key=lambda item: item[1]):
        image = cv2.imread(str(images/rows[i]["image"]), cv2.IMREAD_GRAYSCALE)
        if image is None or image.shape != (720, 1280):
            raise ValueError("Unexpected child image geometry")
        wall = image[36:684, 640:1280]
        linked = [p for p in pairs if p["index_a"] == i or p["index_b"] == i]
        record = {"index": i, "image": rows[i]["image"], "parent_video_s": times[i],
                  "keypoints": len(keypoints[image_id]), "wall_std_gray": float(wall.std()),
                  "wall_laplacian_variance": float(cv2.Laplacian(wall, cv2.CV_64F).var()),
                  "best_pair_inliers": max((p["inliers"] for p in linked), default=0),
                  "old_available": rows[i]["old_available"], "new_available": rows[i]["new_available"]}
        for label, graph in graphs.items():
            record[label+"_degree"] = int(graph["degree"][i])
            record[label+"_component"] = int(graph["labels"][i])
            record[label+"_crossing_after"] = int(graph["crossing"][i]) if i+1 < len(rows) else None
        for label in ("old", "new"):
            xyz = np.array([[float(r[f"{label}_{a}_m"]) for a in "enu"] for r in rows])
            step = np.linalg.norm(xyz[i]-xyz[i-1]) if i > 0 else np.nan
            record[label+"_step_m"] = float(step) if np.isfinite(step) else None
        frames.append(record)
    components = []
    for label, graph in graphs.items():
        for group in sorted(set(graph["labels"])):
            ix = np.flatnonzero(graph["labels"] == group)
            components.append({"graph": label, "component": int(group), "images": len(ix),
                               "start_s": float(times[ix[0]]), "end_s": float(times[ix[-1]]),
                               "old_priors": sum(rows[i]["old_available"] == "True" for i in ix),
                               "new_priors": sum(rows[i]["new_available"] == "True" for i in ix)})
    write_csv(out/"image_diagnostics.csv", frames); write_csv(out/"pair_diagnostics.csv", pairs)
    write_csv(out/"components.csv", components)
    gaps = zero_cut_intervals(graphs["verified"]["crossing"], times)
    if gaps:
        write_csv(out/"unbridged_intervals.csv", gaps)
    old, new = [np.array([[float(r[f"{v}_{a}_m"]) for a in "enu"] for r in rows]) for v in ("old", "new")]
    return {"images": len(rows), "pairs_tested": len(pairs), "prior_comparison": compare_priors(old, new),
            "keypoints": distribution([r["keypoints"] for r in frames]),
            "raw_matches": distribution([p["raw_matches"] for p in pairs]),
            "verified_pairs": sum(p["inliers"] >= 15 for p in pairs),
            "config_counts": {str(c): sum(p["config"] == c for p in pairs) for c in sorted({p["config"] for p in pairs})},
            "config_names": {str(int(value)): name for name, value in pc.TwoViewGeometryConfiguration.__members__.items()},
            "calibrated_angles_deg": distribution([p["tri_angle_deg"] for p in pairs if p["tri_angle_deg"] is not None]),
            "initial_pair_indicators": sum(p["initial_pair_indicator"] for p in pairs),
            "verified_component_sizes": graphs["verified"]["component_sizes"],
            "initial_component_sizes": graphs["initial"]["component_sizes"],
            "isolated_images": int(sum(graphs["verified"]["degree"] == 0)), "unbridged_intervals": gaps,
            "definitions": {"tri_angle_deg": "COLMAP essential-matrix pose triangulation angle; calibrated pairs only; conditional estimate",
                            "initial_pair_indicator": ">=30 stored inliers, estimated tri_angle>=1.5 deg and forward_fraction<=0.95; not full mapper acceptance",
                            "unbridged_interval": "No verified tested image pair crosses each temporal cut; includes skip-pair bridges; not online tracking loss",
                            "wall_laplacian_variance": "Image sharpness proxy influenced by texture, noise and blur; not an independently calibrated focus measurement"}}


def mapper_options(pc, cfg, rows, use_priors):
    options = pc.IncrementalPipelineOptions(num_threads=2, random_seed=cfg["random_seed"],
        image_names=[r["image"] for r in rows], max_num_models=5, min_model_size=5, init_num_trials=80,
        ba_refine_focal_length=False, ba_refine_principal_point=False, ba_refine_extra_params=False,
        ba_local_max_num_iterations=20, ba_global_max_num_iterations=40,
        ba_min_num_residuals_for_cpu_multi_threading=1, max_runtime_seconds=cfg["mapper_seconds"],
        use_prior_position=use_priors, use_robust_loss_on_prior_position=use_priors)
    options.mapper.num_threads = 2; options.mapper.random_seed = cfg["random_seed"]
    options.triangulation.random_seed = cfg["random_seed"]
    options.mapper.init_min_num_inliers = cfg["initial_pair_inliers"]
    options.mapper.init_min_tri_angle = cfg["initial_triangle_angle_deg"]
    options.mapper.abs_pose_min_num_inliers = 20
    options.mapper.abs_pose_refine_focal_length = False; options.mapper.abs_pose_refine_extra_params = False
    return options


def reconstruct(cfg, pc, out, work, database, images, rows):
    lookup = {r["image"]: r for r in rows}; index = {r["image"]: i for i, r in enumerate(rows)}
    modes = []
    for mode in cfg["modes"]:
        label = "new" if mode == "new" else "old"
        target = work/(mode+".db"); shutil.copy2(database, target)
        prior_count = 0
        with pc.Database.open(target) as db:
            db.clear_pose_priors()
            if mode != "visual_only":
                for im in db.read_all_images():
                    row = lookup[im.name]
                    if row[label+"_available"] != "True":
                        continue
                    prior = pc.PosePrior(); prior.corr_data_id = im.data_id
                    prior.position = [float(row[f"{label}_{a}_m"]) for a in "enu"]
                    prior.position_covariance = np.diag(cfg["position_prior_sigma_m"])**2
                    prior.coordinate_system = pc.PosePriorCoordinateSystem.CARTESIAN
                    db.write_pose_prior(prior); prior_count += 1
        options = mapper_options(pc, cfg, rows, mode != "visual_only")
        token = pc.CancellationToken(); timer = threading.Timer(cfg["mapper_seconds"]+20, token.cancel)
        timer.daemon = True; timer.start(); start = time.monotonic()
        print("BEGIN MODE", mode, datetime.now(UTC).isoformat(), flush=True)
        try:
            models = pc.incremental_mapping(target, images, work/mode, options=options, cancellation_token=token)
        finally:
            timer.cancel()
        summary = {"mode": mode, "prior_input": None if mode == "visual_only" else label,
                   "prior_count": prior_count, "elapsed_s": time.monotonic()-start,
                   "deadline_cancelled": bool(token.is_cancelled), "options": str(options), "models": []}
        registered, model_rows, point_rows = set(), [], []
        for mid, model in models.items():
            ims = sorted(model.images.values(), key=lambda im: im.name)
            centers = np.array([im.projection_center() for im in ims])
            targets = np.array([[float(lookup[im.name][f"{label}_{a}_m"]) for a in "enu"] for im in ims])
            fit = placement(centers, targets, cfg["experiment_id"], model.num_points3D(), .25)
            point_angles, track_lengths = [], []
            for pid, point in model.points3D.items():
                elements = point.track.elements
                track_ims = [model.images[e.image_id] for e in elements]
                cs = [im.projection_center() for im in track_ims]
                angles = [np.degrees(pc.calculate_triangulation_angle(cs[a], cs[b], point.xyz))
                          for a in range(len(cs)) for b in range(a+1, len(cs))]
                angle = float(max(angles)) if angles else None
                track_lengths.append(len(elements))
                if angle is not None:
                    point_angles.append(angle)
                ts = [float(lookup[im.name]["parent_video_s"]) for im in track_ims]
                point_rows.append({"mode": mode, "model": int(mid), "point_id": int(pid),
                                   "observations": len(elements), "max_tri_angle_deg": angle,
                                   "first_time_s": min(ts), "last_time_s": max(ts), "duration_s": max(ts)-min(ts),
                                   "error_px": float(point.error)})
            for im in ims:
                registered.add(im.name); row = lookup[im.name]; c = im.projection_center()
                model_rows.append({"mode": mode, "model": int(mid), "image": im.name,
                                   "index": index[im.name], "parent_video_s": float(row["parent_video_s"]),
                                   "observed_points3d": im.num_points3D, "x_local": c[0], "y_local": c[1], "z_local": c[2]})
            name = f"{mode}_model_{mid}_local.ply"; model.export_PLY(out/name)
            summary["models"].append({"model": int(mid), "registered": len(ims), "points": model.num_points3D(),
                                      "reprojection_error_px": model.compute_mean_reprojection_error(),
                                      "track_length": distribution(track_lengths), "point_max_tri_angle_deg": distribution(point_angles),
                                      "start_s": min(float(lookup[im.name]["parent_video_s"]) for im in ims),
                                      "end_s": max(float(lookup[im.name]["parent_video_s"]) for im in ims),
                                      "position_fit_diagnostic": fit, "ply": name, "unit": "local_units",
                                      "metric_map_adopted": False})
        summary["registered_unique"] = len(registered)
        if model_rows:
            write_csv(out/f"{mode}_registered_images.csv", model_rows)
        if point_rows:
            write_csv(out/f"{mode}_points.csv", point_rows)
        dump(out/(mode+"_summary.json"), summary)
        modes.append(summary)
        print("END MODE", mode, "registered", len(registered), "points", sum(m["points"] for m in summary["models"]), flush=True)
    return modes


def worker(cfg):
    sys.path.insert(0, str(ROOT/cfg["runtime"]))
    import pycolmap as pc
    out, work, source, source_work = paths(cfg)
    work.mkdir(parents=True, exist_ok=False)
    prior_path = source/"mapping_frame_priors.csv"; rows = read(prior_path)
    if len(rows) != 300 or len(rows) > cfg["maximum_images"]:
        raise ValueError("Explicit original 300-frame wall subset required")
    info = json.loads((source/"mapping_input.json").read_text())
    images = ROOT/info["source_work"]/"images"
    for row in rows:
        if sha256(images/row["image"]) != row["image_sha256"]:
            raise ValueError("Source image changed")
    inputs = [CONFIG, Path(__file__), ROOT/"src/aquabeacon_analysis/mapping_diagnostics.py",
              ROOT/"scripts/map_pool_optical_followup.py", ROOT/"src/aquabeacon_analysis/restart_mapping.py",
              source/"mapping.json", source/"mapping_input.json", prior_path, source_work/"features.db",
              ROOT/"config/20261103_field_review_000/pool_mapping_dense.json"]
    hashes = {relative(p): sha256(p) for p in inputs}
    database = work/"features.db"; shutil.copy2(source_work/"features.db", database)
    start = time.monotonic()
    diagnosis = audit(cfg, pc, out, database, images, rows, info)
    diagnosis["audit_seconds"] = time.monotonic()-start
    dump(out/"image_audit.json", diagnosis)
    modes = reconstruct(cfg, pc, out, work, database, images, rows)
    for path, digest in hashes.items():
        if sha256(ROOT/path) != digest:
            raise ValueError("Input was modified during diagnosis: "+path)
    dump(out/"manifest.json", {"experiment_id": cfg["experiment_id"], "run": cfg["run"],
          "created_utc": datetime.now(UTC).isoformat(), "pid": os.getpid(), "inputs_sha256": hashes,
          "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
          "source_image_hashes": {r["source_image"]: r["image_sha256"] for r in rows},
          "pycolmap_version": pc.__version__, "cuda_available": pc.has_cuda, "scope": cfg["scope"],
          "modes": modes, "accuracy_validated": False})


def supervise(cfg):
    out, work, _, _ = paths(cfg)
    if out.exists() or work.exists():
        raise FileExistsError("Preserve all existing and incomplete diagnostic runs")
    out.mkdir(parents=True)
    command = [sys.executable, str(Path(__file__).resolve()), "--experiment", cfg["experiment_id"], "--phase", "worker"]
    record = {"purpose": "same-image pool position-prior ablation", "command": command, "task_owned": True,
              "timeout_s": cfg["worker_timeout_s"]}
    with (out/"run.log").open("x", encoding="utf-8") as log:
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
        record.update(pid=proc.pid, started_utc=datetime.now(UTC).isoformat(), status="running")
        dump(out/"process.json", record); print("worker PID", proc.pid, record["started_utc"], flush=True)
        try:
            code = proc.wait(timeout=cfg["worker_timeout_s"])
            record["status"] = "completed" if code == 0 else "failed"
        except subprocess.TimeoutExpired:
            record["status"] = "timeout"
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=5)
            record.update(returncode=proc.returncode, ended_utc=datetime.now(UTC).isoformat())
            dump(out/"process.json", record)
    print(record["status"], flush=True)
    if proc.returncode != 0:
        raise RuntimeError("Diagnosis worker failed; preserved run.log")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--phase", choices=("run", "worker", "report", "verify"), required=True)
    args = parser.parse_args(); cfg = json.loads(CONFIG.read_text())
    if args.experiment != cfg["experiment_id"]:
        parser.error("Explicit configured pool 004 required")
    print("diagnosis PID", os.getpid(), datetime.now(UTC).isoformat(), args.phase, flush=True)
    if args.phase == "run":
        supervise(cfg)
    elif args.phase == "worker":
        worker(cfg)
    else:
        from write_pool_mapping_diagnosis import report, verify
        (report if args.phase == "report" else verify)(cfg)
