"""Compare repeatable image features for an ORB-SLAM3 experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import cv2
import numpy as np


def evaluate_pair(first: np.ndarray, second: np.ndarray, method: str) -> dict[str, int]:
    if method in ("orb", "orb_clahe"):
        if method == "orb_clahe":
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            first, second = clahe.apply(first), clahe.apply(second)
        detector = cv2.ORB_create(nfeatures=2500 if method == "orb_clahe" else 1500,
                                  fastThreshold=3 if method == "orb_clahe" else 5)
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    elif method == "sift_clahe":
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        first, second = clahe.apply(first), clahe.apply(second)
        detector = cv2.SIFT_create(nfeatures=2000, contrastThreshold=0.01)
        matcher = cv2.BFMatcher(cv2.NORM_L2)
    else:
        raise ValueError(method)
    keypoints1, desc1 = detector.detectAndCompute(first, None)
    keypoints2, desc2 = detector.detectAndCompute(second, None)
    good = []
    if desc1 is not None and desc2 is not None and len(desc2) >= 2:
        for candidates in matcher.knnMatch(desc1, desc2, k=2):
            if len(candidates) == 2 and candidates[0].distance < 0.75 * candidates[1].distance:
                good.append(candidates[0])
    inliers = 0
    if len(good) >= 8:
        p1 = np.float32([keypoints1[m.queryIdx].pt for m in good])
        p2 = np.float32([keypoints2[m.trainIdx].pt for m in good])
        k = np.array([[640.0, 0.0, 640.0], [0.0, 640.0, 360.0], [0.0, 0.0, 1.0]])
        _, mask = cv2.findEssentialMat(p1, p2, k, cv2.RANSAC, 0.999, 1.5)
        inliers = int(mask.sum()) if mask is not None else 0
    return {"first_keypoints": len(keypoints1), "second_keypoints": len(keypoints2),
            "ratio_matches": len(good), "essential_inliers": inliers}


def diagnose(experiment_id: str, dataset_root: Path, output_root: Path,
             sample_count: int = 18, step: int = 3) -> Path:
    dataset = dataset_root / experiment_id
    timestamps = (dataset / "timestamps.txt").read_text(encoding="utf-8").splitlines()
    candidates = np.linspace(0, len(timestamps) - step - 1, sample_count, dtype=int)
    image_dir = dataset / "mav0/cam0/data"
    samples = []
    for index in candidates:
        first = cv2.imread(str(image_dir / f"{timestamps[index]}.png"), cv2.IMREAD_GRAYSCALE)
        second = cv2.imread(str(image_dir / f"{timestamps[index + step]}.png"),
                            cv2.IMREAD_GRAYSCALE)
        if first is None or second is None:
            raise ValueError(f"missing image near index {index}")
        samples.append({"image_index": int(index), "step_frames": step,
                        "blur_laplacian_variance": float(cv2.Laplacian(first, cv2.CV_64F).var()),
                        "orb": evaluate_pair(first, second, "orb"),
                        "orb_clahe": evaluate_pair(first, second, "orb_clahe"),
                        "sift_clahe": evaluate_pair(first, second, "sift_clahe")})
    root = Path(__file__).resolve().parents[1]
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                            text=True, check=False)
    fused = root / "results" / experiment_id / "localization/child_fused_attitude.csv"
    settings = root / "calibration" / f"child_orbslam3_provisional_{experiment_id[-3:]}.yaml"
    summary = {method: {
        metric + "_median": float(np.median([s[method][metric] for s in samples]))
        for metric in ("first_keypoints", "ratio_matches", "essential_inliers")
    } for method in ("orb", "orb_clahe", "sift_clahe")}
    result = {"experiment_id": experiment_id, "git_commit": commit.stdout.strip(),
              "dataset": str(dataset), "sample_count": len(samples),
              "camera_focal_px_assumed": 640.0,
              "calibration_settings": str(settings),
              "calibration_settings_sha256": hashlib.sha256(settings.read_bytes()).hexdigest(),
              "child_fused_attitude_sha256": hashlib.sha256(fused.read_bytes()).hexdigest(),
              "summary": summary, "samples": samples,
              "note": "Essential inlier counts use provisional intrinsics; this is feature diagnostics, not pose accuracy"}
    destination = output_root / experiment_id / "vislam_depth/feature_diagnostic.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_id")
    parser.add_argument("--dataset-root", type=Path, default=Path("data/interim/orbslam3_full"))
    parser.add_argument("--output-root", type=Path, default=Path("results"))
    parser.add_argument("--sample-count", type=int, default=18)
    parser.add_argument("--step", type=int, default=3)
    args = parser.parse_args()
    print(diagnose(args.experiment_id, args.dataset_root, args.output_root,
                   args.sample_count, args.step))


if __name__ == "__main__":
    main()
