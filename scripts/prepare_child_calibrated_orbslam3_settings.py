"""Create child-only ORB-SLAM3 trial settings from the underwater camera calibration."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def calibrated_settings(camera: dict, template: str, source: str, digest: str,
                        source_120fps: bool = False) -> str:
    """Replace only the camera model in a provisional child IMU trial template."""
    size = camera["proxy_image_size_px"]
    if size != [1280, 720] or camera["proxy_direct_distortion_coefficients"][4] != 0:
        raise ValueError("expected direct 1280x720 proxy calibration with zero k3")
    matrix = camera["proxy_direct_camera_matrix_px"]
    distortion = camera["proxy_direct_distortion_coefficients"]
    values = dict(zip(("Camera1.fx", "Camera1.fy", "Camera1.cx", "Camera1.cy"),
                      (matrix[0][0], matrix[1][1], matrix[0][2], matrix[1][2])))
    values.update(zip(("Camera1.k1", "Camera1.k2", "Camera1.p1", "Camera1.p2"),
                      distortion[:4]))
    if source_120fps:
        values["Camera.fps"] = 120
    lines = template.splitlines()
    replaced = set()
    for index, line in enumerate(lines):
        key = line.split(":", 1)[0]
        if key in values:
            lines[index] = f"{key}: {values[key]:.12g}"
            replaced.add(key)
    if replaced != set(values):
        raise ValueError(f"template camera fields missing: {set(values) - replaced}")
    start = next(index for index, line in enumerate(lines) if line.startswith("File.version:"))
    header = ["%YAML:1.0", "# Child camera intrinsics: direct underwater 1280x720 proxy fit.",
              f"# Source: {source}; SHA-256: {digest}",
              "# Trial only: cross-date camera mode, camera-IMU extrinsic, and IMU noise unverified.",
              "# Uses child IMU and child camera only; no parent observations."]
    if source_120fps:
        header.append("# Native 120000/1001 fps source resized to 1280x720; Camera.fps rounded to 120.")
    return "\n".join(header + lines[start:]) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", choices=("022", "023", "024"))
    parser.add_argument("--source-120fps", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = root / "calibration/parent_camera_underwater_20260924_provisional.json"
    camera = json.loads(source.read_text(encoding="utf-8"))
    if "C_MAIN_CAM" not in camera["camera_device_ids_user_reported_same_camera_and_settings"]:
        raise ValueError("calibration is not documented as applicable to the child camera")
    template = root / f"calibration/child_orbslam3_provisional_{args.experiment}.yaml"
    suffix = f"_120fps_{args.experiment}" if args.source_120fps else f"_{args.experiment}"
    destination = root / f"calibration/child_orbslam3_underwater_trial{suffix}.yaml"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    destination.write_text(calibrated_settings(camera, template.read_text(encoding="utf-8"),
                                               str(source.relative_to(root)), digest,
                                               args.source_120fps),
                           encoding="utf-8")
    print(destination)


if __name__ == "__main__":
    main()
