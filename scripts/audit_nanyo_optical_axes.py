"""Audit native, unrotated optical axes against this run's DJI rotations."""

import argparse
import itertools
import json
from pathlib import Path

import cv2
import numpy as np
import pycolmap
from analyze_nanyo import read
from scipy.spatial.transform import Rotation, Slerp

from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_sync import numeric_csv


def rank(records):
    a = np.array([r["optical"] for r in records])
    b = np.array([r["dji"] for r in records])
    candidates = []
    for perm in itertools.permutations(range(3)):
        for sign in itertools.product([-1, 1], repeat=3):
            m = np.eye(3)[:, perm] @ np.diag(sign)
            if np.linalg.det(m) < 0:
                continue
            res = np.linalg.norm(a @ m.T - b, axis=1) / np.linalg.norm(b, axis=1)
            candidates.append(
                {
                    "matrix": m.tolist(),
                    "median_relative_residual": float(np.median(res)),
                    "consistent": int((res < 0.35).sum()),
                }
            )
    candidates.sort(key=lambda r: r["median_relative_residual"])
    return candidates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261031_nanyo_000"])
    parser.add_argument("--apply-existing", action="store_true")
    args = parser.parse_args()
    eid = args.experiment
    if args.apply_existing:
        apply(
            eid, json.loads(Path(f"results/{eid}/absolute_position/optical_axes.json").read_text())
        )
        return
    cv2.setNumThreads(2)
    sync = json.loads(Path(f"results/{eid}/synchronization/manifest.json").read_text())["cameras"]
    splines = {}
    for name in sync:
        a = numeric_csv(
            Path(f"data/interim/{eid}/synchronization/{name}.csv"),
            ["video_time_s", "qw", "qx", "qy", "qz"],
        )
        splines[name] = Slerp(a[:, 0], Rotation.from_quat(a[:, [2, 3, 4, 1]]))
    child_name = next(k for k in sync if k.startswith("child"))
    child_records = []
    segment = "20261031_nanyo_001"
    times = {
        r["image"]: float(r["source_video_s"])
        for r in read(Path(f"results/{segment}/visual_mapping/frame_times.csv"))
    }
    for mid in [1, 2, 3]:
        model = pycolmap.Reconstruction(f"data/interim/{segment}/visual_mapping/models/{mid}")
        images = sorted(model.images.values(), key=lambda i: i.name)
        for a, b in itertools.pairwise(images):
            ta, tb = times[a.name], times[b.name]
            if tb - ta > 2:
                continue
            # world_from_optical poses: R0.T @ R1 is camera1 axes expressed in camera0.
            optical = Rotation.from_matrix(
                a.cam_from_world().rotation.matrix() @ b.cam_from_world().rotation.matrix().T
            ).as_rotvec()
            dji = (splines[child_name](ta).inv() * splines[child_name](tb)).as_rotvec()
            if 0.02 < np.linalg.norm(dji) < 1:
                child_records.append(
                    {
                        "model": mid,
                        "t0": ta,
                        "t1": tb,
                        "optical": optical.tolist(),
                        "dji": dji.tolist(),
                    }
                )
    parent_records = []
    parent_name = next(k for k in sync if k.startswith("parent") and "0188" in k)
    source = Path(sync[parent_name]["source_video"]["path"]).with_suffix(".LRF")
    cap = cv2.VideoCapture(str(source))
    cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 0)
    # Onboard/air samples provide texture; these are axis audits, not water calibration.
    for t in np.r_[np.arange(20, 550, 8), np.arange(1530, 1690, 4)]:
        frames = []
        pts = []
        for tt in [t, t + 0.4]:
            cap.set(cv2.CAP_PROP_POS_MSEC, tt * 1000)
            ok, frame = cap.read()
            if not ok:
                break
            assert frame.shape[:2] == (720, 1280)
            frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
            pts.append(cap.get(cv2.CAP_PROP_POS_MSEC) / 1000)
        if len(frames) < 2:
            continue
        dji = (splines[parent_name](pts[0]).inv() * splines[parent_name](pts[1])).as_rotvec()
        if not 0.025 < np.linalg.norm(dji) < 0.7:
            continue
        p = cv2.goodFeaturesToTrack(frames[0], 700, 0.01, 12)
        if p is None:
            continue
        q, st, _ = cv2.calcOpticalFlowPyrLK(frames[0], frames[1], p, None)
        back, st2, _ = cv2.calcOpticalFlowPyrLK(frames[1], frames[0], q, None)
        good = (
            (st[:, 0] == 1) & (st2[:, 0] == 1) & (np.linalg.norm(back[:, 0] - p[:, 0], axis=1) < 1)
        )
        p, q = p[good, 0], q[good, 0]
        if len(p) < 50:
            continue
        k = np.array([[650.0, 0, 640], [0, 650.0, 360], [0, 0, 1]])
        e, mask = cv2.findEssentialMat(p, q, k, cv2.RANSAC, 0.999, 1)
        if e is None or e.shape != (3, 3):
            continue
        count, r, _, _ = cv2.recoverPose(e, p, q, k, mask=mask)
        optical = Rotation.from_matrix(r.T).as_rotvec()
        if count < 30 or np.linalg.norm(optical) > 0.8:
            continue
        parent_records.append(
            {
                "t0": pts[0],
                "t1": pts[1],
                "inliers": count,
                "optical": optical.tolist(),
                "dji": dji.tolist(),
            }
        )
    cap.release()
    result = {
        "parent": {"records": parent_records, "ranked": rank(parent_records)},
        "child": {"records": child_records, "ranked": rank(child_records)},
        "image_geometry": "Native 1280x720 raster; no previous 90 degree correction",
        "status": "Discrete axis audit, not continuous subdegree extrinsic calibration",
        "code_sha256": sha256(Path(__file__)),
    }
    out = Path(f"results/{eid}/absolute_position")
    out.mkdir(exist_ok=True, parents=True)
    (out / "optical_axes.json").write_text(json.dumps(result, indent=2))
    apply(eid, result)


def apply(eid, result):
    cfgpath = Path(f"config/{eid}/absolute_position.json")
    cfg = json.loads(cfgpath.read_text())
    for role in ["parent", "child"]:
        best = result[role]["ranked"][0]
        minimum = 2 if role == "parent" else 10
        if best["consistent"] < minimum:
            raise ValueError(f"Insufficient independent visual support: {role}")
        if role == "parent" and best["matrix"] != result["child"]["ranked"][0]["matrix"]:
            raise ValueError("Parent/child native optical conventions disagree")
        cfg[f"R_dji_from_{role}_optical"] = best["matrix"]
        cfg[f"{role}_optical_axis_support"] = {
            "pairs": len(result[role]["records"]),
            "consistent": best["consistent"],
            "status": "Sparse parent corroboration of child-supported same-device convention"
            if role == "parent"
            else "Same-run visual motion audit",
        }
        print(role, len(result[role]["records"]), result[role]["ranked"][:2], flush=True)
    cfgpath.write_text(json.dumps(cfg, indent=2))


if __name__ == "__main__":
    main()
