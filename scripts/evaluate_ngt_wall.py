"""Bounded COLMAP wall reconstruction for the explicitly selected simultaneous-move run."""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENCV_FFMPEG_THREADS", "2")
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "data/interim/20261028_ngt_004/wall_mapping"
OUT = ROOT / "results/20261028_ngt_004/wall_mapping"
sys.path.insert(0, str(WORK / "runtime"))
import pycolmap as colmap

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_position import bracket_interpolate

SAMPLE_HZ = 2


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def prepare():
    images = WORK / "images"
    images.mkdir(parents=True, exist_ok=True)
    source_frames = WORK / "source_frames"
    source_frames.mkdir(exist_ok=True)
    sync_path = ROOT / "results/20261028_ngt_000/synchronization/manifest.json"
    sync = json.loads(sync_path.read_text())["cameras"]
    video = Path(sync["child"]["video"]).with_suffix(".LRF")
    cap = cv2.VideoCapture(str(video))
    clahe = cv2.createCLAHE(clipLimit=2., tileGridSize=(8, 8))
    frames = []
    try:
        for index, pt in enumerate(np.arange(1415., 1540., 1/SAMPLE_HZ)):
            ct = (sync["parent"]["utc_at_video_zero_s"] + sync["parent"]["time_scale"]*pt
                  - sync["child"]["utc_at_video_zero_s"])/sync["child"]["time_scale"]
            cap.set(cv2.CAP_PROP_POS_MSEC, ct*1000)
            ok, frame = cap.read()
            if not ok:
                raise RuntimeError(f"Frame unavailable at child {ct}")
            actual = cap.get(cv2.CAP_PROP_POS_MSEC)/1000
            actual_pt = (sync["child"]["utc_at_video_zero_s"]+sync["child"]["time_scale"]*actual
                         - sync["parent"]["utc_at_video_zero_s"])/sync["parent"]["time_scale"]
            name = f"wall_{index:05}.jpg"
            cv2.imwrite(str(source_frames / name), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
            lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
            lab[:, :, 0] = clahe.apply(lab[:, :, 0])
            enhanced = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
            cv2.imwrite(str(images / name), enhanced, [cv2.IMWRITE_JPEG_QUALITY, 95])
            frames.append({"image_name": name, "parent_video_s": actual_pt,
                           "child_video_s": actual, "requested_child_s": ct,
                           "source_frame_index": int(cap.get(cv2.CAP_PROP_POS_FRAMES))-1})
    finally:
        cap.release()
    mask = np.zeros(frame.shape[:2], np.uint8)
    mask[36:684, 832:1260] = 255
    cv2.imwrite(str(WORK / "wall_mask.png"), mask)
    write_csv(WORK / "frames.csv", frames)
    # Positions remain missing across optical/ranging outages. Pressure provides vertical prior.
    positions_path = ROOT / "results/20261028_ngt_004/position_evaluation/positions_5hz.csv"
    positions = read_csv(positions_path)
    t = np.array([float(r["parent_video_s"]) for r in positions])
    xyz = np.array([[float(r["child_east_declination0_m"]),
                     float(r["child_north_declination0_m"]), -float(r["pressure_depth_m"])]
                    for r in positions])
    for i, row in enumerate(positions):
        if row["magnetic_position_available"] != "True":
            xyz[i] = np.nan
    priors = bracket_interpolate(t, xyz, [r["parent_video_s"] for r in frames], .25)
    for row, p in zip(frames, priors, strict=True):
        row.update(prior_available=bool(np.isfinite(p).all()),
                   prior_east_m=p[0], prior_north_m=p[1], prior_up_pressure_m=p[2])
    write_csv(WORK / "frame_priors.csv", frames)
    defaults = ROOT / "calibration/camera_defaults.json"
    cal_path = ROOT / json.loads(defaults.read_text())["underwater_intrinsics"]["C_MAIN_CAM"]
    k, d, intrinsic_provenance = load_camera_intrinsics(ROOT, "C_MAIN_CAM", (1280, 720), "proxy")
    manifest = {"experiment_id": "20261028_ngt_004", "source": str(video.relative_to(ROOT)),
                "source_sha256": sha256(video), "sampled_interval_parent_s": [1415, 1540],
                "full_move_interval_parent_s": [1260, 1540], "images": len(frames),
                "sample_hz": SAMPLE_HZ, "geometry": "1280x720 original LRF; no crop or resize",
                "enhancement": "LAB luminance CLAHE clip2 grid8; originals preserved",
                "feature_mask": "x832:1260 y36:684; right-hand pool-wall ROI; no planar constraint",
                "camera_matrix": k.tolist(), "distortion": d.tolist(),
                "intrinsic_provenance": intrinsic_provenance,
                "calibration": str(cal_path.relative_to(ROOT)), "calibration_sha256": sha256(cal_path),
                "synchronization_sha256": sha256(sync_path), "position_csv_sha256": sha256(positions_path),
                "prior_count": int(np.isfinite(priors).all(axis=1).sum()),
                "prior_sigma_m": [1., 1., .3],
                "prior_status": "zero declination/lever-arm approximation, pressure for vertical; not independent ground truth",
                "known_measured_wall_dimensions": None, "nominal_25m_used_as_scale": False,
                "pycolmap": colmap.__version__, "generator_sha256": sha256(Path(__file__))}
    write_json(OUT / "manifest.json", manifest)
    print(json.dumps({"images": len(frames), "priors": manifest["prior_count"]}), flush=True)


def features():
    manifest = json.loads((OUT / "manifest.json").read_text())
    k, d = np.array(manifest["camera_matrix"]), manifest["distortion"]
    reader = colmap.ImageReaderOptions(camera_model="OPENCV",
        camera_params=",".join(map(str, [k[0, 0], k[1, 1], k[0, 2], k[1, 2], *d[:4]])),
        camera_mask_path=str(WORK / "wall_mask.png"))
    extractor = colmap.FeatureExtractionOptions(num_threads=2, use_gpu=False, max_image_size=1280)
    extractor.sift.max_num_features = 2500
    extractor.sift.peak_threshold = .004
    token = colmap.CancellationToken()
    deadline = threading.Timer(600, token.cancel)
    deadline.daemon = True
    deadline.start()
    try:
        colmap.extract_features(WORK / "features.db", WORK / "images",
            camera_mode=colmap.CameraMode.SINGLE, reader_options=reader,
            extraction_options=extractor, device=colmap.Device.cpu, cancellation_token=token)
        colmap.match_sequential(WORK / "features.db",
            matching_options=colmap.FeatureMatchingOptions(num_threads=2, use_gpu=False),
            pairing_options=colmap.SequentialPairingOptions(
                overlap=8 if SAMPLE_HZ == 2 else 12, quadratic_overlap=False, num_threads=2),
            device=colmap.Device.cpu, cancellation_token=token)
        if token.is_cancelled:
            raise TimeoutError("Feature/matching 600s deadline reached")
    finally:
        deadline.cancel()


def reconstruct(prior):
    mode = "aquabeacon_prior" if prior else "visual_only"
    db_path = WORK / f"{mode}.db"
    if not db_path.exists():
        shutil.copy2(WORK / "features.db", db_path)
    if prior:
        frames = {r["image_name"]: r for r in read_csv(WORK / "frame_priors.csv")}
        with colmap.Database.open(db_path) as db:
            if not db.read_all_pose_priors():
                for im in db.read_all_images():
                    row = frames[im.name]
                    if row["prior_available"] != "True":
                        continue
                    p = colmap.PosePrior()
                    p.corr_data_id = im.data_id
                    p.position = [float(row[k]) for k in ["prior_east_m", "prior_north_m", "prior_up_pressure_m"]]
                    p.position_covariance = np.diag([1., 1., .3])**2
                    p.coordinate_system = colmap.PosePriorCoordinateSystem.CARTESIAN
                    db.write_pose_prior(p)
    options = colmap.IncrementalPipelineOptions(
        num_threads=2, random_seed=7, max_num_models=5, min_model_size=10,
        init_num_trials=80, ba_refine_focal_length=False, ba_refine_extra_params=False,
        ba_refine_principal_point=False, ba_local_max_num_iterations=20,
        ba_global_max_num_iterations=40, max_runtime_seconds=240,
        use_prior_position=prior, use_robust_loss_on_prior_position=prior,
        ba_min_num_residuals_for_cpu_multi_threading=1,
    )
    options.mapper.num_threads = 2
    options.mapper.init_min_num_inliers = 50
    options.mapper.init_min_tri_angle = 3.
    options.mapper.abs_pose_refine_focal_length = False
    options.mapper.abs_pose_refine_extra_params = False
    options.mapper.abs_pose_min_num_inliers = 20
    start = time.monotonic()
    models = colmap.incremental_mapping(db_path, WORK / "images", WORK / mode, options=options)
    metrics = []
    for i, model in models.items():
        folder = WORK / mode / str(i)
        model.export_PLY(folder / "points.ply")
        model.write_text(folder)
        frame_lookup = {r["image_name"]: r for r in read_csv(WORK / "frames.csv")}
        cameras = []
        for im in model.images.values():
            if not im.has_pose:
                continue
            center = im.projection_center()
            cameras.append({"image_name": im.name,
                            "parent_video_s": frame_lookup[im.name]["parent_video_s"],
                            "x": center[0], "y": center[1], "z": center[2]})
        cameras.sort(key=lambda r: float(r["parent_video_s"]))
        write_csv(folder / "camera_centers.csv", cameras)
        points = np.array([p.xyz for p in model.points3D.values()])
        # SVD plane is a diagnostic, never supplied as a reconstruction constraint.
        centered = points-np.mean(points, axis=0)
        _, _, vh = np.linalg.svd(centered, full_matrices=False)
        residual = centered@vh[-1]
        span = np.percentile(centered@vh[0], 95)-np.percentile(centered@vh[0], 5)
        metrics.append({"model": int(i), "registered_images": model.num_reg_images(),
                        "point_count": model.num_points3D(), "mean_reprojection_px": model.compute_mean_reprojection_error(),
                        "wall_plane_rms_model_units": float(np.sqrt(np.mean(residual**2))),
                        "wall_span_p90_model_units": float(span),
                        "wall_plane_rms_over_span": float(np.sqrt(np.mean(residual**2))/span) if span > 0 else None,
                        "nondegenerate_structure": len(points) >= 50,
                        "first_parent_video_s": float(cameras[0]["parent_video_s"]),
                        "last_parent_video_s": float(cameras[-1]["parent_video_s"]),
                        "metric_scale": "prior-constrained conditional meters" if prior else "unobservable monocular scale"})
    result = {"mode": mode, "elapsed_s": time.monotonic()-start, "models": metrics,
              "registered_sum": sum(m["registered_images"] for m in metrics),
              "input_images": len(read_csv(WORK / "frames.csv")), "options": str(options),
              "plane_status": "all sparse features in declared wall ROI; may include floating particles/ropes",
              "dimension_error_m": None}
    write_json(OUT / f"{mode}_metrics.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


def main():
    global WORK, OUT, SAMPLE_HZ
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261028_ngt_004"])
    parser.add_argument("--phase", choices=["prepare", "features", "visual-only", "prior"], required=True)
    parser.add_argument("--dense-sampling", action="store_true", help="Separate 5Hz overlap diagnostic")
    args = parser.parse_args()
    if args.dense_sampling:
        WORK = WORK / "five_hz"
        OUT = OUT / "five_hz"
        SAMPLE_HZ = 5
    OUT.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    cv2.setNumThreads(2)
    record = {"pid": os.getpid(), "started_utc": datetime.now(UTC).isoformat(),
              "owner": "wall_mapping", "phase": args.phase, "purpose": __doc__}
    write_json(OUT / f"{args.phase}.process.json", record)
    print(json.dumps(record), flush=True)
    try:
        if args.phase == "prepare":
            prepare()
        elif args.phase == "features":
            features()
        else:
            reconstruct(args.phase == "prior")
        record["status"] = "completed"
    except BaseException as e:
        record["status"] = "failed"
        record["error"] = str(e)
        raise
    finally:
        record["ended_utc"] = datetime.now(UTC).isoformat()
        write_json(OUT / f"{args.phase}.process.json", record)


if __name__ == "__main__":
    main()
