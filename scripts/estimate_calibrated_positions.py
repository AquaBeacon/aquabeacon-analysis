"""Re-estimate 2026-09-22 parent/child poses with underwater camera intrinsics."""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np
from estimate_parent_child_positions import interpolate_gps, nearest_range
from fuse_relative_position import fuse, read_rows

from aquabeacon_analysis.attitude_light_angles import (
    _attitude_data,
    _normalized,
    _sha256,
    interpolate_attitude,
    match_first_frame,
    rotate,
    stabilized_angles,
)


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"no rows for {path}")
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def undistorted_camera_ray(x_px: float, y_px: float, camera_matrix: np.ndarray,
                           distortion: np.ndarray) -> np.ndarray:
    """Return a unit optical ray after removing calibrated image distortion."""
    pixel = np.array([[[x_px, y_px]]], dtype=float)
    nx, ny = cv2.undistortPoints(pixel, camera_matrix, distortion)[0, 0]
    ray = np.array([nx, ny, 1.], dtype=float)
    return ray / np.linalg.norm(ray)


def estimate(experiment: str, root: Path) -> dict[str, object]:
    result = root / "results" / experiment
    output = result / "calibrated_estimate"
    output.mkdir(parents=True, exist_ok=True)
    calibration_path = root / "calibration" / "parent_camera_underwater_20260924_provisional.json"
    mount_path = root / "calibration" / "parent_camera_imu_provisional.json"
    geometry_path = root / "calibration" / "experiment_body_geometry_20260922.json"
    track_path = result / "light_detection" / "tracked_light.csv"
    attitude_path = result / "localization" / "parent_fused_attitude.csv"
    gps_path = result / "localization" / "parent_gps_position.csv"
    range_path = result / "tof_depth_comparison" / "paired_observations.csv"
    preprocessing_path = result / "preprocessing" / "manifest.json"
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    mount = json.loads(mount_path.read_text(encoding="utf-8"))
    geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
    preprocessing = json.loads(preprocessing_path.read_text(encoding="utf-8"))
    if calibration["status"] != "provisional_limited_viewpoint_diversity":
        raise ValueError("unexpected camera calibration status")
    if mount["status"] != "hypothetical_not_measured":
        raise ValueError("mount status must be explicitly provisional")
    matrix = np.asarray(mount["optical_to_imu_rotation_matrix"], dtype=float)
    if not np.allclose(matrix.T @ matrix, np.eye(3), atol=1e-8) or not np.isclose(np.linalg.det(matrix), 1):
        raise ValueError("invalid optical-to-IMU rotation")
    video = next(v for v in preprocessing["video_outputs"]
                 if "P_MAIN" in v["source"] and "proxy" in v["source"])
    source, cut = root / video["source"], root / video["output"]
    capture = cv2.VideoCapture(str(cut))
    try:
        size = [int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))]
    finally:
        capture.release()
    if size != calibration["proxy_image_size_px"]:
        raise ValueError(f"proxy dimensions {size} disagree with camera calibration")
    camera_matrix = np.asarray(calibration["proxy_direct_camera_matrix_px"], dtype=float)
    distortion = np.asarray(calibration["proxy_direct_distortion_coefficients"], dtype=float)
    source_index, fps = match_first_frame(source, cut, video["source_offset_s"])
    video_start_window_s = source_index / fps - video["source_offset_s"]
    times, quaternions = _attitude_data(attitude_path)
    reference = rotate(_normalized(quaternions[0]), matrix @ np.array([0., 0., 1.]))
    forward = np.array([reference[0], reference[1], 0.])
    forward /= np.linalg.norm(forward)
    tracks, ranges, gps = read_rows(track_path), read_rows(range_path), read_rows(gps_path)
    angles: list[dict[str, object]] = []
    positions: list[dict[str, object]] = []
    for track in tracks:
        video_time_s = float(track["video_time_s"])
        window_time_s = video_start_window_s + video_time_s
        common = {
            "video_time_s": track["video_time_s"], "window_time_s": f"{window_time_s:.6f}",
            "source_frame_index": track["source_frame_index"], "tracking_status": track["status"],
            "x_px": track["x_px"], "y_px": track["y_px"],
        }
        angle: dict[str, object] = {**common, "camera_azimuth_rad": "", "camera_elevation_rad": "",
                                    "local_azimuth_rad": "", "gravity_elevation_rad": "",
                                    "angle_status": "no_light_angle"}
        position: dict[str, object] = {
            "experiment_id": experiment, "video_time_s": track["video_time_s"],
            "window_time_s": f"{window_time_s:.6f}",
            "sync_utc_us": round(preprocessing["window_start_utc_us"] + window_time_s * 1e6),
            "light_status": track["status"], "angle_status": "no_light_angle",
            "x_px": track["x_px"], "y_px": track["y_px"],
            "azimuth_rad": "", "elevation_rad": "", "local_azimuth_rad": "",
            "gravity_elevation_rad": "", "range_time_delta_s": "", "range_m": "",
            "tof_us": "", "range_qc": "", "relative_camera_x_right_m": "",
            "relative_camera_y_down_m": "", "relative_camera_z_forward_m": "",
            "relative_gravity_x_m": "", "relative_gravity_y_m": "", "relative_gravity_z_up_m": "",
            "parent_gps_east_m": "", "parent_gps_north_m": "",
            "child_east_yaw0_m": "", "child_north_yaw0_m": "", "position_status": "no_light_angle",
        }
        if track["status"] in {"anchor", "observed", "reacquired"} and track["x_px"] and track["y_px"]:
            ray = undistorted_camera_ray(float(track["x_px"]), float(track["y_px"]),
                                         camera_matrix, distortion)
            nx, ny = ray[0] / ray[2], ray[1] / ray[2]
            az_camera = math.atan(float(nx))
            el_camera = math.atan2(float(-ny), math.hypot(1, float(nx)))
            angle["camera_azimuth_rad"] = f"{az_camera:.8f}"
            angle["camera_elevation_rad"] = f"{el_camera:.8f}"
            position["azimuth_rad"] = angle["camera_azimuth_rad"]
            position["elevation_rad"] = angle["camera_elevation_rad"]
            q = interpolate_attitude(times, quaternions, window_time_s)
            if q is None:
                angle["angle_status"] = position["angle_status"] = "no_parent_attitude"
            else:
                az, el = stabilized_angles(az_camera, el_camera, q, matrix, forward)
                angle["local_azimuth_rad"] = position["local_azimuth_rad"] = f"{az:.8f}"
                angle["gravity_elevation_rad"] = position["gravity_elevation_rad"] = f"{el:.8f}"
                angle["angle_status"] = position["angle_status"] = "calibrated_intrinsics_provisional_mount"
                matched = nearest_range(ranges, window_time_s)
                if matched is None:
                    position["position_status"] = "no_nearby_valid_tof"
                else:
                    distance = float(matched["range_corrected_m"])
                    optical = distance * ray
                    gravity = distance * np.array([math.cos(el)*math.cos(az), math.cos(el)*math.sin(az), math.sin(el)])
                    position.update({
                        "range_time_delta_s": f"{float(matched['window_time_s']) - window_time_s:.6f}",
                        "range_m": f"{distance:.6f}", "tof_us": matched["tof_us"],
                        "range_qc": "inspect_short_or_spike" if distance < .2 or distance > 5 else "reported_valid",
                        "relative_camera_x_right_m": f"{optical[0]:.6f}",
                        "relative_camera_y_down_m": f"{optical[1]:.6f}",
                        "relative_camera_z_forward_m": f"{optical[2]:.6f}",
                        "relative_gravity_x_m": f"{gravity[0]:.6f}",
                        "relative_gravity_y_m": f"{gravity[1]:.6f}",
                        "relative_gravity_z_up_m": f"{gravity[2]:.6f}",
                        "position_status": "fixed_parent_gravity_frame_conditional",
                    })
                    parent_xy = interpolate_gps(gps, window_time_s)
                    if parent_xy is not None:
                        position.update({"parent_gps_east_m": f"{parent_xy[0]:.6f}",
                                         "parent_gps_north_m": f"{parent_xy[1]:.6f}",
                                         "child_east_yaw0_m": f"{parent_xy[0] + gravity[0]:.6f}",
                                         "child_north_yaw0_m": f"{parent_xy[1] + gravity[1]:.6f}",
                                         "position_status": "gps_anchored_yaw0_conditional"})
        angles.append(angle)
        positions.append(position)
    angle_path = output / "attitude_corrected_light_angles.csv"
    position_path = output / "parent_child_position.csv"
    write_csv(angle_path, angles)
    write_csv(position_path, positions)
    common_manifest = {
        "experiment_id": experiment, "camera_intrinsics_status": calibration["status"],
        "optical_to_imu_status": mount["status"], "body_geometry_status": geometry["status"],
        "video_start_window_s": video_start_window_s,
        "first_frame_source_index": source_index, "first_frame_source_fps": fps,
        "coordinate_frame": "parent gravity frame; horizontal zero is initial camera heading, not ENU",
        "limitations": ["2026-09-22 image mode was not independently compared with calibration video",
                        "camera-to-IMU rotation and sensor centers are not measured",
                        "parent antenna and child light lever arms are not corrected",
                        "GPS yaw-zero position is illustrative, not georeferenced child position"],
        "input_sha256": {str(path.relative_to(root)): _sha256(path) for path in
                         (calibration_path, mount_path, geometry_path, track_path, attitude_path,
                          gps_path, range_path, preprocessing_path, source, cut)},
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
    }
    (output / "attitude_angle_manifest.json").write_text(
        json.dumps({**common_manifest, "rows": len(angles), "source": str(track_path.relative_to(root))}, indent=2) + "\n",
        encoding="utf-8")
    (output / "manifest.json").write_text(
        json.dumps({**common_manifest, "rows": len(positions), "angles": str(angle_path.relative_to(root))}, indent=2) + "\n",
        encoding="utf-8")
    fused = fuse(experiment, root, "calibrated_estimate")
    for name in ("parent_imu_xy.png", "parent_gps_imu_diagnostic.png",
                 "parent_attitude_3d.html", "child_attitude_3d.html"):
        shutil.copy2(result / "localization" / name, output / name)
    angle_count = sum(a["angle_status"] == "calibrated_intrinsics_provisional_mount" for a in angles)
    range_count = sum(p["range_m"] != "" for p in positions)
    blender_links = (
        "- [Blender静止画](blender_preview.png) / "
        "[時刻再生できるBlenderシーン](pose_trajectory.blend) / "
        "[Blender入力・座標系](blender_scene_manifest.json)\n"
        if (output / "blender_scene_manifest.json").exists() else ""
    )
    magnetic_links = (
        "- [子機BNO085磁気診断図](magnetometer_diagnostic.png) / "
        "[磁気診断の条件と集計](magnetometer_manifest.json)\n"
        if (output / "magnetometer_manifest.json").exists() else ""
    )
    (output / "report.md").write_text(
        f"# {experiment} 親子機の姿勢・位置推定\n\n"
        "## 結果を見る\n\n"
        "- [親機・子機の圧力優先相対位置グラフ](fused_comparison.png)\n"
        "- [親機GPSとIMUの診断図](parent_gps_imu_diagnostic.png)\n"
        "- [親機IMUの積分軌跡](parent_imu_xy.png)\n"
        f"{blender_links}"
        f"{magnetic_links}"
        "- [親機姿勢の3D表示](parent_attitude_3d.html) / "
        "[子機姿勢の3D表示](child_attitude_3d.html)\n"
        "- [ToF・ライト相対位置CSV](parent_child_position.csv) / "
        "[圧力・IMU融合位置CSV](fused_position.csv) / "
        "[校正後ライト角CSV](attitude_corrected_light_angles.csv)\n"
        "- [親機融合姿勢CSV](../localization/parent_fused_attitude.csv) / "
        "[子機融合姿勢CSV](../localization/child_fused_attitude.csv) / "
        "[親機GPS水平位置CSV](../localization/parent_gps_position.csv)\n\n"
        "## 件数と条件\n\n"
        f"- 校正後ライト角: {angle_count}点。ToF一致: {range_count}点。"
        f"圧力・IMU融合位置: {fused['published_fused_samples']}点。\n"
        f"- 光学・ToFと圧力の鉛直差中央値: {fused['pressure_optical_median_disagreement_m']:.2f} m。"
        f"深度がToF距離を上回る幾何不整合: "
        f"{fused['raw_depth_vs_range_geometry_conflicts_with_0p2m_allowance']}点（0.20 mの仮の端点差を許容）。\n"
        "- 水中カメラの内部行列・歪み係数は別日撮影の暫定候補。"
        "姿勢の光学軸→IMU軸回転も仮定値。水平基準は初期カメラ向きで、ENU方位ではない。\n"
        "- 参照した実験機体モデルは取付面と概略内装を示す。"
        "カメラ光学中心、IMU中心、GPSアンテナ、ライト中心等の実測body座標は未確定。"
        "レバーアーム補正は適用していない。\n"
        "- 圧力深度を鉛直位置の主観測とし、子機IMU鉛直加速度を時間拘束に使用した。"
        "圧力とToF・ライトに幾何不整合が残るため、子機body原点の実位置や"
        "Ground Truth精度として扱わない。\n\n"
        "Blenderは発表用表示に限定する。子機姿勢の初期水平向きは親機と独立しており、"
        "Blender内では最初の子機姿勢を任意の向きに合わせている。"
        "BNO085の磁気観測は記録されているが、現在のGame Rotation Vector姿勢に磁気方位は"
        "含まれない。磁気・取付校正と親機方位基準が未確認のため方位補正に使用していない。\n\n"
        "[入力ハッシュ・座標系](manifest.json) / "
        "[融合条件・診断](fused_position_manifest.json) / "
        "[機体モデルの記録](../../../calibration/experiment_body_geometry_20260922.json)\n",
        encoding="utf-8",
    )
    return {"experiment_id": experiment, "rows": len(positions),
            "angle_rows": angle_count,
            "range_rows": range_count,
            "fused_rows": fused["published_fused_samples"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="append", dest="sessions")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    for experiment in args.sessions or ("20260922_cmain_022", "20260922_cmain_023", "20260922_cmain_024"):
        print(json.dumps(estimate(experiment, root), ensure_ascii=False))


if __name__ == "__main__":
    main()
