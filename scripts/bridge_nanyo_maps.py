"""Densify temporal breaks and extend an existing visual fragment without touching it."""

import argparse
import json
import os
import shutil
import sqlite3
from pathlib import Path

import cv2
import numpy as np
import pycolmap as pc
from analyze_nanyo import read, write

from aquabeacon_analysis.ngt_binary import sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiment", required=True, choices=["20261031_nanyo_001", "20261031_nanyo_002"]
    )
    args = parser.parse_args()
    eid = args.experiment
    cv2.setNumThreads(2)
    old = Path(f"data/interim/{eid}/visual_mapping")
    work = Path(f"data/interim/{eid}/position_mapping")
    out = Path(f"results/{eid}/position_mapping")
    out.mkdir(parents=True, exist_ok=True)
    images = work / "images"
    images.mkdir(parents=True, exist_ok=True)
    for path in (old / "images").glob("*.jpg"):
        dest = images / path.name
        if not dest.exists():
            os.link(path, dest)
    database = work / "features.db"
    if not database.exists():
        shutil.copy2(old / "features.db", database)
    times = read(Path(f"results/{eid}/visual_mapping/frame_times.csv"))
    clock = next(
        c
        for k, c in json.loads(
            Path("results/20261031_nanyo_000/synchronization/manifest.json").read_text()
        )["cameras"].items()
        if k.startswith("child")
    )
    segment = json.loads(Path(f"experiments/{eid}/experiment.json").read_text())
    cap = cv2.VideoCapture(str(Path(clock["source_video"]["path"]).with_suffix(".LRF")))
    cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 0)
    clahe = cv2.createCLAHE(2, (8, 8))
    offsets = (
        np.r_[np.arange(820, 851, 0.2), np.arange(928, 943, 0.2)]
        if eid.endswith("001")
        else np.arange(22, 86, 0.2)
    )
    seed = "3" if eid.endswith("001") else "2"
    for j, offset in enumerate(offsets):
        source_s = (segment["utc_interval_s"][0] + offset - clock["utc_at_video_zero_s"]) / clock[
            "time_scale"
        ]
        cap.set(cv2.CAP_PROP_POS_MSEC, source_s * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        actual = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
        name = f"bridge_{j:06d}.jpg"
        frame = cv2.resize(frame, (960, 540), interpolation=cv2.INTER_AREA)
        lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        cv2.imwrite(str(images / name), cv2.cvtColor(lab, cv2.COLOR_LAB2BGR))
        times.append(
            {
                "image": name,
                "source_video_s": actual,
                "sync_utc_s": clock["utc_at_video_zero_s"] + clock["time_scale"] * actual,
                "input_pts": "",
                "input_time_base": "OpenCV decoded PTS",
            }
        )
    cap.release()
    write(out / "frame_times.csv", times)
    with sqlite3.connect(database) as db:
        camera_id = db.execute(
            "select camera_id from cameras order by camera_id limit 1"
        ).fetchone()[0]
    reader = pc.ImageReaderOptions()
    reader.existing_camera_id = camera_id
    features = pc.FeatureExtractionOptions()
    features.num_threads = 2
    features.sift.max_num_features = 2048
    pc.extract_features(
        database, images, reader_options=reader, extraction_options=features, device=pc.Device.cpu
    )
    times.sort(key=lambda r: float(r["sync_utc_s"]))
    pairs = []
    for i, a in enumerate(times):
        for b in times[i + 1 : i + 13]:
            if float(b["sync_utc_s"]) - float(a["sync_utc_s"]) > 3:
                continue
            if a["image"].startswith("bridge") or b["image"].startswith("bridge"):
                pairs.append(a["image"] + " " + b["image"])
    pairfile = work / "pairs.txt"
    pairfile.write_text("\n".join(pairs))
    pairing = pc.ImportedPairingOptions()
    pairing.match_list_path = str(pairfile)
    matching = pc.FeatureMatchingOptions()
    matching.num_threads = 2
    pc.match_image_pairs(
        database, matching_options=matching, pairing_options=pairing, device=pc.Device.cpu
    )
    options = pc.IncrementalPipelineOptions()
    options.num_threads = 2
    options.random_seed = 0
    options.ba_refine_focal_length = False
    options.ba_refine_principal_point = False
    options.ba_refine_extra_params = False
    options.mapper.abs_pose_refine_focal_length = False
    options.mapper.abs_pose_refine_extra_params = False
    options.multiple_models = False
    options.max_runtime_seconds = 600
    models = pc.incremental_mapping(
        database, images, work / "models", options=options, input_path=old / "models" / seed
    )
    manifest = {
        "experiment_id": eid,
        "method": f"5 Hz image bridges, extension of visual model {seed}",
        "added_images": sum(r["image"].startswith("bridge") for r in times),
        "pairs": len(pairs),
        "code_sha256": sha256(Path(__file__)),
        "models": [],
    }
    for mid, m in models.items():
        names = sorted(x.name for x in m.images.values())
        manifest["models"].append(
            {
                "id": mid,
                "registered": m.num_reg_images(),
                "points": m.num_points3D(),
                "original_registered": sum(not x.startswith("bridge") for x in names),
                "mean_reprojection_error_px": m.compute_mean_reprojection_error(),
                "names": names,
            }
        )
    (out / "bridge_manifest.json").write_text(json.dumps(manifest, indent=2))
    print("BRIDGE_RESULT", [(r["registered"], r["points"]) for r in manifest["models"]], flush=True)


if __name__ == "__main__":
    main()
