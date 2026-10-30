"""Verify portable calibration evidence and refit cameras/axes without raw or firmware."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

import numpy as np

from aquabeacon_analysis.child_axis_calibration import fit_frd
from aquabeacon_analysis.experiments import repository_path


def verify(root: Path, refit: bool = False) -> dict:
    bundle = root / "calibration/reference_data"
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest["files"]:
        path = repository_path(root, item["path"])
        if not path.is_file():
            raise ValueError(f"Missing calibration evidence: {item['path']}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"Calibration evidence checksum mismatch: {item['path']}")
    result = {"verified_files": len(manifest["files"]), "axis_refits": {}}
    if not refit:
        return result
    for role, solution, key, prefix, clock in (
        ("child", "20261029_child_axis_003_imu_frd.json", "R_body_from_sensor", "", "acquired_us"),
        ("parent", "20261029_parent_axis_011_axes.json", "R_body_from_spresense",
         "corrected_", "parent_mono_s"),
    ):
        calibration = json.loads((root / "calibration" / solution).read_text(encoding="utf-8"))
        poses = []
        for phase, window in enumerate(calibration["pose_windows"], 1):
            with (bundle / role / f"pose_{phase}_imu.csv").open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            lo = window["start_mono_s" if role == "child" else "parent_start_s"] + 1
            hi = window["end_mono_s" if role == "child" else "parent_end_s"] - .25
            timed = [(r, float(r[clock]) / (1e6 if role == "child" else 1)) for r in rows]
            selected = [(r, t) for r, t in timed if lo <= t <= hi]
            accel = [(r, t) for r, t in selected
                     if role == "parent" or int(r["fresh_mask"]) & 2]
            gyro = [r for r, _ in selected if role == "parent" or int(r["fresh_mask"]) & 4]
            poses.append(([[float(r[prefix + k]) for k in ("ax", "ay", "az")] for r, _ in accel],
                          [[float(r[prefix + k]) for k in ("gx", "gy", "gz")] for r in gyro],
                          [t for _, t in accel]))
        rotation, quality = fit_frd(poses)
        difference = float(np.max(np.abs(rotation - np.asarray(calibration[key]))))
        if difference > 1e-8:
            raise ValueError(f"{role} axis refit differs from published calibration: {difference}")
        result["axis_refits"][role] = {"max_matrix_difference": difference,
                                     "max_direction_residual_deg": max(
                                         q["direction_residual_deg"] for q in quality)}
    import cv2

    calibration = json.loads((root / "calibration" /
                              "parent_camera_underwater_20260924_provisional.json").read_text())
    objects = np.zeros((20, 3), dtype=np.float32)
    objects[:, :2] = np.mgrid[0:4, 0:5].T.reshape(-1, 2) * .04
    result["camera_refits"] = {}
    for name, times_key, size, matrix_key, distortion_key in (
        ("observations.npz", "selected_frame_times_s", (1920, 1080), "camera_matrix_px",
         "distortion_coefficients"),
        ("proxy_selected_observations.npz", "proxy_selected_frame_times_s", (1280, 720),
         "proxy_direct_camera_matrix_px", "proxy_direct_distortion_coefficients"),
    ):
        with np.load(bundle / "camera" / name, allow_pickle=False) as observations:
            indices = []
            for time in calibration[times_key]:
                matches = np.flatnonzero(np.isclose(observations["times"], time, atol=1e-9,
                                                   rtol=0))
                if len(matches) != 1:
                    raise ValueError(f"Missing or duplicate checkerboard observation at {time}")
                indices.append(int(matches[0]))
            rms, matrix, distortion, _, _ = cv2.calibrateCamera(
                [objects] * len(indices), [observations["corners"][i] for i in indices],
                size, None, None, flags=cv2.CALIB_FIX_K3)
        difference = float(np.max(np.abs(matrix - np.asarray(calibration[matrix_key]))))
        if difference > .01 or not np.allclose(
                distortion.ravel(), calibration[distortion_key], atol=1e-4, rtol=0):
            raise ValueError(f"Camera refit differs from published calibration: {name}")
        result["camera_refits"][name] = {"views": len(indices), "rms_px": rms,
                                         "max_matrix_difference_px": difference}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refit", action="store_true", help="also refit cameras and four-pose axes")
    args = parser.parse_args()
    print(json.dumps(verify(Path(__file__).resolve().parents[1], args.refit), indent=2))


if __name__ == "__main__":
    main()
