"""Child-only COLMAP reconstruction with explicit fixed underwater intrinsics."""

import argparse
import json
import sqlite3
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pycolmap as pc

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.time_sync import resolve_ffmpeg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    args = parser.parse_args()
    eid = args.experiment
    root = Path.cwd()
    segment = json.loads((root / f"experiments/{eid}/experiment.json").read_text())
    clocks = json.loads(
        (root / "results/20261031_nanyo_000/synchronization/manifest.json").read_text()
    )
    clock = next(c for k, c in clocks["cameras"].items() if k.startswith("child"))
    start, end = [
        (t - clock["utc_at_video_zero_s"]) / clock["time_scale"] for t in segment["utc_interval_s"]
    ]
    source = Path(clock["source_video"]["path"]).with_suffix(".LRF")
    work = root / f"data/interim/{eid}/visual_mapping"
    out = root / f"results/{eid}/visual_mapping"
    images = work / "images"
    images.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    k, d, provenance = load_camera_intrinsics(root, "C_MAIN_CAM", (960, 540), "proxy")
    manifest = {
        "experiment_id": eid,
        "method": "COLMAP sequential SfM, child only",
        "sample_hz": 1,
        "source": str(source),
        "source_interval_s": [start, end],
        "intrinsics": provenance,
        "calibration_status": "provisional underwater model",
        "capture_conditions": "Dewarp/EIS/housing not independently verified in this run",
        "source_time_precision_s": 1.0,
        "sampling_time_note": "Nominal 1 Hz output bins; original per-frame PTS not exported. Use a 1 s bound; no pose/pressure prior is applied.",
        "scale": "arbitrary monocular units",
        "parent_position_prior_used": False,
        "pressure_scale_applied": False,
        "code_sha256": sha256(Path(__file__)),
        "pycolmap_version": pc.__version__,
    }
    if not list(images.glob("*.jpg")):
        command = [
            str(resolve_ffmpeg()),
            "-v",
            "error",
            "-threads",
            "2",
            "-ss",
            str(start),
            "-i",
            str(source),
            "-t",
            str(end - start),
            "-vf",
            "fps=1,scale=960:540",
            "-q:v",
            "2",
            "-start_number",
            "0",
            str(images / "%06d.jpg"),
        ]
        subprocess.run(command, check=True, timeout=600)
        # CLAHE is saved as the actual reconstruction input; raw LRF remains unchanged.
        clahe = cv2.createCLAHE(2.0, (8, 8))
        for path in images.glob("*.jpg"):
            img = cv2.imread(str(path))
            lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
            lab[:, :, 0] = clahe.apply(lab[:, :, 0])
            cv2.imwrite(str(path), cv2.cvtColor(lab, cv2.COLOR_LAB2BGR))
    db = work / "features.db"
    reader = pc.ImageReaderOptions()
    reader.camera_model = "OPENCV"
    reader.camera_params = ",".join(map(str, [k[0, 0], k[1, 1], k[0, 2], k[1, 2], *d[:4]]))
    extraction = pc.FeatureExtractionOptions()
    extraction.num_threads = 2
    extraction.sift.max_num_features = 2048
    pc.extract_features(
        db,
        images,
        camera_mode=pc.CameraMode.SINGLE,
        reader_options=reader,
        extraction_options=extraction,
        device=pc.Device.cpu,
    )
    matching = pc.FeatureMatchingOptions()
    matching.num_threads = 2
    pairs = pc.SequentialPairingOptions()
    pairs.overlap = 5
    pairs.quadratic_overlap = False
    pc.match_sequential(db, matching_options=matching, pairing_options=pairs, device=pc.Device.cpu)
    options = pc.IncrementalPipelineOptions()
    options.num_threads = 2
    options.ba_refine_focal_length = False
    options.ba_refine_extra_params = False
    options.ba_refine_principal_point = False
    options.mapper.abs_pose_refine_focal_length = False
    options.mapper.abs_pose_refine_extra_params = False
    options.max_runtime_seconds = 600
    options.random_seed = 0
    options.max_num_models = 20
    options.init_num_trials = 100
    models = pc.incremental_mapping(db, images, work / "models", options=options)
    manifest["models"] = []
    registered = set()
    for mid, model in models.items():
        model.export_PLY(out / f"model_{mid}.ply")
        poses = []
        for image in model.images.values():
            registered.add(image.name)
            center = image.projection_center()
            poses.append([image.name, *center.tolist()])
        import csv

        with (out / f"model_{mid}_poses.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["image", "x_arbitrary", "y_arbitrary", "z_arbitrary"])
            w.writerows(poses)
        manifest["models"].append(
            {
                "id": mid,
                "registered_images": model.num_reg_images(),
                "points": model.num_points3D(),
                "mean_reprojection_error_px": model.compute_mean_reprojection_error(),
            }
        )
    with sqlite3.connect(db) as connection:
        features = connection.execute(
            "select images.name,keypoints.rows from images join keypoints on images.image_id=keypoints.image_id order by images.name"
        ).fetchall()
        manifest["geometrically_verified_pairs"] = connection.execute(
            "select count(*) from two_view_geometries where rows>=15"
        ).fetchone()[0]
    import csv

    with (out / "frame_status.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["image", "approx_source_video_s", "sift_features", "registered"])
        w.writerows(
            [name, start + int(Path(name).stem), count, name in registered]
            for name, count in features
        )
    manifest.update(
        sampled_frames=len(features),
        registered_frames=len(registered),
        registered_fraction=len(registered) / len(features),
        feature_median=float(np.median([c for _, c in features])),
    )
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest["models"]), flush=True)


if __name__ == "__main__":
    main()
