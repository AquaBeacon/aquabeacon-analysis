"""Render actual ORB-SLAM3 keypoints and persistent MapPoint matches at source rate."""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
from render_child_vslam_artifacts import plot_coverage, plot_map, read_ply_xyz


def grouped_features(path: Path):
    with path.open(encoding="utf-8", newline="") as stream:
        rows = csv.DictReader(stream)
        for index, group in itertools.groupby(rows, key=lambda row: int(row["frame_index"])):
            yield index, [(float(row["x_px"]), float(row["y_px"]),
                           row["map_point_id"], int(row["tracking_state"])) for row in group]


def annotate(frame: np.ndarray, features: list[tuple[float, float, str, int]],
             previous: dict[str, tuple[float, float]], index: int,
             elapsed_s: float, detected_count: int,
             associated_count: int, state: int) -> tuple[np.ndarray, dict[str, tuple[float, float]], int]:
    image = cv2.resize(frame, (960, 540))
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    scale = 0.75
    current = ({item[2]: (item[0], item[1]) for item in features if item[2]}
               if state in (2, 5) else {})
    matches = [(point, previous[ident]) for ident, point in current.items()
               if ident in previous]
    # Thin only drawn markers; the counts and ORB-SLAM3 input retain all points.
    stride = max(1, math.ceil(len(features) / 500))
    for x, y, ident, _ in features[::stride]:
        cv2.circle(image, (round(x * scale), round(y * scale)), 1,
                   (50, 185, 255) if ident else (255, 160, 65), -1)
    match_stride = max(1, math.ceil(len(matches) / 180))
    for (x, y), (px, py) in matches[::match_stride]:
        cv2.line(image, (round(px * scale), round(py * scale)),
                 (round(x * scale), round(y * scale)), (85, 255, 95), 1, cv2.LINE_AA)
        cv2.circle(image, (round(x * scale), round(y * scale)), 2,
                   (65, 255, 65), -1, cv2.LINE_AA)
    cv2.rectangle(image, (0, 0), (960, 63), (0, 0, 0), -1)
    cv2.putText(image, f"Child ORB-SLAM3 | source 119.88 fps | frame {index} | {elapsed_s:.2f}s",
                (12, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
    label = {1: "INIT", 2: "OK", 3: "RECENTLY LOST", 4: "LOST", 5: "OK KLT"}.get(state, str(state))
    cv2.putText(image, f"Detected {detected_count} | 3D IDs {associated_count} | previous-frame matches {len(matches)} | {label}",
                (12, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return image, current, len(matches)


def matching_panel(previous_frame: np.ndarray, frame: np.ndarray,
                   previous: dict[str, tuple[float, float]],
                   features: list[tuple[float, float, str, int]]) -> np.ndarray:
    left = cv2.resize(previous_frame, (960, 540))
    right = cv2.resize(frame, (960, 540))
    if left.ndim == 2:
        left = cv2.cvtColor(left, cv2.COLOR_GRAY2BGR)
    if right.ndim == 2:
        right = cv2.cvtColor(right, cv2.COLOR_GRAY2BGR)
    panel = np.concatenate((left, right), axis=1)
    current = {item[2]: (item[0], item[1]) for item in features if item[2]}
    matches = [(previous[ident], point) for ident, point in current.items()
               if ident in previous]
    stride = max(1, math.ceil(len(matches) / 120))
    for (px, py), (x, y) in matches[::stride]:
        a = (round(px * 0.75), round(py * 0.75))
        b = (960 + round(x * 0.75), round(y * 0.75))
        cv2.line(panel, a, b, (70, 210, 90), 1, cv2.LINE_AA)
        cv2.circle(panel, a, 2, (50, 240, 80), -1, cv2.LINE_AA)
        cv2.circle(panel, b, 2, (50, 240, 80), -1, cv2.LINE_AA)
    cv2.rectangle(panel, (0, 0), (1920, 45), (0, 0, 0), -1)
    cv2.putText(panel, f"Previous 120 fps frame | {len(matches)} shared 3D MapPoint IDs | Current frame",
                (16, 29), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)
    return panel


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def render(root: Path, experiment_id: str, run_name: str, color: bool = False) -> Path:
    output = root / "results" / experiment_id / "vislam_depth"
    run = output / run_name
    tag = "color_120fps" if color else "120fps"
    dataset_folder = "orbslam3_color_120fps" if color else "orbslam3_120fps"
    dataset = root / "data/interim" / dataset_folder / experiment_id
    export_manifest = json.loads((dataset / "export_manifest.json").read_text(encoding="utf-8"))
    timestamps = [int(line) for line in (dataset / "timestamps.txt").read_text().splitlines()]
    calibration = json.loads((root / "calibration/parent_camera_underwater_20260924_provisional.json")
                             .read_text(encoding="utf-8"))
    matrix = np.asarray(calibration["proxy_direct_camera_matrix_px"], dtype=np.float64)
    distortion = np.asarray(calibration["proxy_direct_distortion_coefficients"],
                            dtype=np.float64)
    display_matrix = matrix.copy()
    display_matrix[:2, :] *= 0.75
    undistort_x, undistort_y = cv2.initUndistortRectifyMap(
        matrix, distortion, None, display_matrix, (960, 540), cv2.CV_16SC2)
    features_path = run / "TrackedFeatures.csv"
    summary_path = run / "TrackedFrameSummary.csv"
    if not features_path.exists():
        raise FileNotFoundError(features_path)
    with summary_path.open(encoding="utf-8", newline="") as stream:
        summaries = list(csv.DictReader(stream))
    if len(summaries) != len(timestamps):
        raise ValueError("ORB-SLAM3 per-frame summary does not cover every input frame")
    states = [int(row["tracking_state"]) for row in summaries]
    state_names = {0: "NO_IMAGES_YET", 1: "NOT_INITIALIZED", 2: "OK",
                   3: "RECENTLY_LOST", 4: "LOST", 5: "OK_KLT"}
    counts = Counter(states)
    lost_spans = []
    for index, state in enumerate(states):
        if state in (3, 4):
            if lost_spans and lost_spans[-1]["end_frame_exclusive"] == index:
                lost_spans[-1]["end_frame_exclusive"] = index + 1
            else:
                lost_spans.append({"start_frame": index, "end_frame_exclusive": index + 1})
    tracking_path = output / f"tracking_state_{tag}.json"
    tracking_path.write_text(json.dumps({
        "experiment_id": experiment_id, "run_name": run_name,
        "input_frames": len(timestamps),
        "state_counts": {state_names.get(state, str(state)): count
                         for state, count in sorted(counts.items())},
        "tracking_ok_frames": counts[2] + counts[5],
        "tracking_ok_fraction": (counts[2] + counts[5]) / len(timestamps),
        "lost_spans": lost_spans,
        "definition": "ORB-SLAM3 per-frame state 3 RECENTLY_LOST or 4 LOST; initialization state 1 reported separately",
    }, indent=2), encoding="utf-8")
    feature_groups = iter(grouped_features(features_path))
    current_group = next(feature_groups, None)
    video = output / f"features_{tag}.mp4"
    still = output / f"features_{tag}_frame.png"
    match_still = output / f"matches_{tag}_frame.png"
    ffmpeg_log = output / f"features_{tag}_ffmpeg.log"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
               "-pixel_format", "bgr24", "-video_size", "960x540", "-framerate",
               "120000/1001", "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "26", "-threads", "4", "-pix_fmt", "yuv420p", "-r",
               "120000/1001", "-video_track_timescale", "120000",
               str(video)]
    previous: dict[str, tuple[float, float]] = {}
    previous_frame: np.ndarray | None = None
    total_detected = total_associated = total_matches = total_exported = 0
    best_matches = -1
    with ffmpeg_log.open("wb") as log:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log)
        try:
            for index, stamp in enumerate(timestamps):
                if current_group and current_group[0] == index:
                    features = current_group[1]
                    current_group = next(feature_groups, None)
                else:
                    features = []
                extension = export_manifest.get("image_extension", ".png")
                frame = cv2.imread(str(dataset / "mav0/cam0/data" / f"{stamp}{extension}"),
                                   cv2.IMREAD_COLOR if color else cv2.IMREAD_GRAYSCALE)
                if frame is None:
                    raise FileNotFoundError(f"image at {stamp}")
                frame = cv2.remap(frame, undistort_x, undistort_y, cv2.INTER_LINEAR)
                elapsed = (stamp - timestamps[0]) * 1e-9
                summary = summaries[index]
                if int(summary["frame_index"]) != index or int(summary["utc_ns"]) != stamp:
                    raise ValueError(f"feature summary timestamp mismatch at frame {index}")
                detected = int(summary["detected_features"])
                associated = int(summary["associated_features"])
                prior = previous
                frame, previous, matches = annotate(
                    frame, features, previous, index, elapsed, detected, associated,
                    int(summary["tracking_state"]))
                if matches > best_matches:
                    best_matches = matches
                    if not cv2.imwrite(str(still), frame):
                        raise RuntimeError(f"cannot save {still}")
                    if previous_frame is not None and matches > 0:
                        panel = matching_panel(previous_frame, frame, prior, features)
                        if not cv2.imwrite(str(match_still), panel):
                            raise RuntimeError(f"cannot save {match_still}")
                if process.stdin is None:
                    raise RuntimeError("ffmpeg input pipe unavailable")
                process.stdin.write(frame.tobytes())
                total_detected += detected
                total_associated += associated
                total_matches += matches
                total_exported += len(features)
                previous_frame = frame
                if index % 2000 == 0:
                    print(f"{experiment_id}: rendered {index + 1}/{len(timestamps)}", flush=True)
        finally:
            if process.stdin:
                process.stdin.close()
            result = process.wait()
        if result != 0:
            raise RuntimeError(f"ffmpeg exited {result}; see {ffmpeg_log}")
    with (run / "camera_trajectory_status.csv").open(encoding="utf-8", newline="") as stream:
        poses = list(csv.DictReader(stream))
    with (run / "MapPoints_manifest.csv").open(encoding="utf-8", newline="") as stream:
        maps = list(csv.DictReader(stream))
    largest = max(maps, key=lambda item: int(item["keyframes"]))
    outputs = [video, still, match_still, tracking_path]
    for item in maps:
        destination = output / f"map_{tag}_{item['map_id']}.ply"
        shutil.copyfile(run / item["ply_file"], destination)
        outputs.append(destination)
    positions = np.array([[float(row[key]) for key in ("x_slam", "y_slam", "z_slam")]
                          for row in poses if row["pose_status"] == "saved"])
    map_image = output / f"map_3d_{tag}.png"
    coverage_image = output / f"pose_coverage_{tag}.png"
    plot_map(read_ply_xyz(output / f"map_{tag}_{largest['map_id']}.ply"),
             positions, map_image)
    plot_coverage(poses, coverage_image)
    outputs.extend((map_image, coverage_image))
    manifest = {
        "experiment_id": experiment_id, "run_name": run_name,
        "source_rate_fps": 120000 / 1001, "slam_input_frames": len(timestamps),
        "input_color_channels": export_manifest.get("color_channels", 1),
        "visualization_video_frames": len(timestamps),
        "source_frames_without_utc_before": export_manifest["first_source_frame"],
        "source_frames_without_utc_after": (export_manifest["source_frame_count"] - 1 -
                                            export_manifest["last_source_frame"]),
        "input_first_to_last_duration_s": (timestamps[-1] - timestamps[0]) * 1e-9,
        "encoded_video_duration_s": len(timestamps) * 1001 / 120000,
        "detected_feature_count": total_detected,
        "associated_feature_count": total_associated,
        "exported_feature_rows": total_exported,
        "adjacent_frame_matches": total_matches, "max_matches_in_frame": best_matches,
        "feature_provenance": "ORB-SLAM3 GetTrackedKeyPointsUn and GetTrackedMapPoints after each TrackMonocular call",
        "display_geometry": "1280x720 camera frame undistorted and resized to 960x540 with calibrated camera matrix scaled by 0.75; GetTrackedKeyPointsUn coordinates scaled by 0.75",
        "match_definition": "same non-bad MapPoint ID in adjacent input frames",
        "unassociated_feature_export_stride": 10,
        "feature_marker_visual_limit": 500, "match_line_visual_limit": 180,
        "source_export_manifest_sha256": sha256(dataset / "export_manifest.json"),
        "run_manifest_sha256": sha256(run / "run_manifest.json"),
        "tracked_features_sha256": sha256(features_path),
        "tracked_frame_summary_sha256": sha256(summary_path),
        "feature_export_patch_sha256": sha256(root / "docker/orbslam3/export_tracked_features.patch"),
        "color_image_extension_patch_sha256": (
            sha256(root / "docker/orbslam3/color_image_extension.patch") if color else None
        ),
        "artifacts_sha256": {path.name: sha256(path) for path in outputs},
    }
    (output / f"artifact_manifest_{tag}.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    report = output / "report.md"
    coverage = json.loads((run / "coverage_summary.json").read_text(encoding="utf-8"))
    heading = "## 元動画カラー120 fpsによる再実行" if color else "## 元動画120 fpsによる再実行"
    section = ["", heading, "",
               f"入力は子機元動画の{len(timestamps)}フレーム、実際のレートは120000/1001 fps。",
               (f"UTC対応のない冒頭{export_manifest['first_source_frame']}枚と末尾"
                f"{export_manifest['source_frame_count'] - 1 - export_manifest['last_source_frame']}枚は"
                "解析対象から除外した。対応区間のフレームは間引いていない。"),
               (f"ORB-SLAM3追跡状態OKは{counts[2] + counts[5]}フレーム"
                f"（{(counts[2] + counts[5]) / len(timestamps):.1%}）。"
                f"最大地図に姿勢が保存されたフレームは{coverage['matched_frames']}枚"
                f"（{coverage['coverage_fraction']:.1%}）。両指標は意味が異なる。"),
               ("元動画のカラー各フレームを1280×720のJPEGとして保持し、"
                "水中校正の同解像度内部パラメータを使用した。ORB-SLAM3はカラー入力を内部で輝度画像へ変換する。"
                if color else
                "元動画の各フレームを1280×720に縮小し、水中校正の同解像度内部パラメータを使用した。"),
               "30 Hzのカメラテレメトリを4フレームごとのUTCアンカーとし、中間時刻を線形補間した。",
               "親機の映像・センサ観測は投入していない。単眼スケールは未確定。",
               "", f"- [120 fps特徴点・対応付け動画](features_{tag}.mp4)",
               f"- [特徴点・対応付け代表画像](features_{tag}_frame.png)",
               f"- [前後フレームの対応線画像](matches_{tag}_frame.png)",
               f"- [120 fps実行の3D地図](map_3d_{tag}.png)",
               f"- [120 fps実行の姿勢保存区間](pose_coverage_{tag}.png)"]
    section.append(f"- [フレーム別追跡状態と喪失区間](tracking_state_{tag}.json)")
    for item in maps:
        section.append(f"- [120 fps map {item['map_id']} 点群PLY](map_{tag}_{item['map_id']}.ply): {item['map_points']}点")
    section += ["", (f"実行記録は[{run_name}]({run_name}/run_manifest.json)、"
                     "[フレーム対応検証](frame_alignment_120fps.json)、"
                     f"[成果物manifest](artifact_manifest_{tag}.json)を参照。"),
                ("背景画像は同じ水中校正値で歪み補正して特徴点座標に合わせた。"
                 "青点は検出特徴点、橙点は地図点に対応する特徴点、緑線は連続フレームで"
                 "同じ3D地図点に対応した特徴点間を示す。描画は混雑を避けて間引くが、"
                 "SLAM入力と動画フレームは間引いていない。"), ""]
    existing = report.read_text(encoding="utf-8") if report.exists() else "# 子機VSLAM\n"
    if heading not in existing:
        report.write_text(existing.rstrip() + "\n" + "\n".join(section), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_id")
    parser.add_argument("--run-name")
    parser.add_argument("--color", action="store_true", help="render color JPEG VSLAM run")
    args = parser.parse_args()
    run_name = args.run_name or ("calibrated_mono_color_120fps_full" if args.color else
                                 "calibrated_mono_120fps_full")
    print(render(Path(__file__).resolve().parents[1], args.experiment_id, run_name, args.color))


if __name__ == "__main__":
    main()
