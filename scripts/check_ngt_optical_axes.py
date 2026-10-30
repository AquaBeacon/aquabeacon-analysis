"""Check optical/DJI IMU axis conventions against same-run visual rotations."""
from __future__ import annotations

import argparse
import itertools
import json
import os
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("OPENCV_FFMPEG_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
import cv2
import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from aquabeacon_analysis.ngt_sync import numeric_csv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261028_ngt_000"])
    parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = root / "results/20261028_ngt_000/position_evaluation"
    out.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"pid": os.getpid(), "started": datetime.now(UTC).isoformat(),
                      "purpose": "same-run optical axis audit"}), flush=True)
    cv2.setNumThreads(2)
    a = numeric_csv(root / "data/interim/20261028_ngt_000/cameras/parent_telemetry.csv",
                    ["video_time_s", "qw", "qx", "qy", "qz"])
    slerp = Slerp(a[:, 0], Rotation.from_quat(a[:, [2, 3, 4, 1]]))
    video = root / "data/raw/ngtHighSchoolExperiment1/parent/DJI/DJI_20261028140300_0024_D.LRF"
    cap = cv2.VideoCapture(str(video))
    records = []
    previous = None
    try:
        for t in np.arange(155, 205, .5):
            cap.set(cv2.CAP_PROP_POS_MSEC, t*1000)
            ok, frame = cap.read()
            if not ok:
                continue
            pts = cap.get(cv2.CAP_PROP_POS_MSEC)/1000
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if previous is not None:
                t0, g0 = previous
                p = cv2.goodFeaturesToTrack(g0, 800, .01, 12)
                if p is not None:
                    q, status, _ = cv2.calcOpticalFlowPyrLK(g0, gray, p, None)
                    back, status2, _ = cv2.calcOpticalFlowPyrLK(gray, g0, q, None)
                    keep = ((status[:, 0] == 1) & (status2[:, 0] == 1)
                            & (np.linalg.norm(back[:, 0]-p[:, 0], axis=1) < 1))
                    p, q = p[keep, 0], q[keep, 0]
                    imu = (slerp(t0).inv()*slerp(pts)).as_rotvec()
                    if len(p) > 50 and np.linalg.norm(imu) > .02:
                        for focal in [450., 550., 671.]:
                            k = np.array([[focal, 0, 640], [0, focal, 360], [0, 0, 1]])
                            e, mask = cv2.findEssentialMat(p, q, k, cv2.RANSAC, .999, 1.)
                            if e is None or e.shape != (3, 3):
                                continue
                            count, r, _, _ = cv2.recoverPose(e, p, q, k, mask=mask)
                            optical = Rotation.from_matrix(r.T).as_rotvec()
                            if count >= 30 and np.linalg.norm(optical) < .7:
                                records.append({"t0": t0, "t1": pts, "focal_px": focal,
                                                "inliers": count, "imu": imu.tolist(),
                                                "optical": optical.tolist()})
            previous = (pts, gray)
    finally:
        cap.release()
    candidates = []
    for permutation in itertools.permutations(range(3)):
        for signs in itertools.product([-1, 1], repeat=3):
            m = np.eye(3)[:, permutation] @ np.diag(signs)
            if np.linalg.det(m) < 0:
                continue
            residuals = [np.linalg.norm(np.array(r["imu"])-m@r["optical"])
                         / np.linalg.norm(r["imu"]) for r in records]
            candidates.append({"matrix": m.tolist(), "median_relative_residual":
                               float(np.median(residuals))})
    candidates.sort(key=lambda r: r["median_relative_residual"])
    best = np.array(candidates[0]["matrix"])
    good = [r for r in records if np.linalg.norm(np.array(r["imu"])-best@r["optical"])
            / np.linalg.norm(r["imu"]) < .35]
    result = {"method": "LK forward/backward <1px; essential RANSAC 1px; recoverPose >=30",
              "image_medium": "air; focal 450/550/671px sensitivity, no underwater model used",
              "purpose": "discrete axis convention audit, not subdegree extrinsic calibration",
              "source": str(video.relative_to(root)), "records": records,
              "ranked_axis_conventions": candidates,
              "consistent_records": len(good),
              "independent_pairs": len({r["t0"] for r in good})}
    (out / "optical_axis_audit.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({"records": len(records), "consistent_records": len(good),
                      "independent_pairs": result["independent_pairs"],
                      "best": candidates[:3]}), flush=True)


if __name__ == "__main__":
    main()
