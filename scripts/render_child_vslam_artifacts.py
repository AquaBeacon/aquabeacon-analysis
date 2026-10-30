"""Render child-only VSLAM video, stills, and native ORB-SLAM3 point maps."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import subprocess
from pathlib import Path

import cv2
import matplotlib

from aquabeacon_analysis.editable_plots import save_editable_figure

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def longest_saved_span(rows: list[dict[str, str]]) -> tuple[int, int]:
    """Return the longest half-open interval with a pose in the exported Atlas map."""
    best_start = best_end = current_start = 0
    for index, row in enumerate(rows):
        if row["pose_status"] == "saved":
            if index == 0 or rows[index - 1]["pose_status"] != "saved":
                current_start = index
            if index + 1 - current_start > best_end - best_start:
                best_start, best_end = current_start, index + 1
    if best_end - best_start < 30:
        raise ValueError("no sustained saved-pose interval for a VSLAM video")
    return best_start, best_end


def read_ply_xyz(path: Path) -> np.ndarray:
    with path.open(encoding="utf-8") as stream:
        if stream.readline().strip() != "ply" or stream.readline().strip() != "format ascii 1.0":
            raise ValueError(f"expected ASCII PLY: {path}")
        count = None
        for line in stream:
            if line.startswith("element vertex "):
                count = int(line.split()[2])
            if line.strip() == "end_header":
                break
        if count is None:
            raise ValueError(f"PLY vertex count missing: {path}")
        points = np.array([[float(value) for value in stream.readline().split()[:3]]
                           for _ in range(count)], dtype=float)
    return points.reshape((-1, 3))


def plot_map(points: np.ndarray, poses: np.ndarray, output: Path) -> None:
    fig = plt.figure(figsize=(8, 6), layout="constrained")
    ax = fig.add_subplot(111, projection="3d")
    if len(points):
        lower, upper = np.percentile(points, (2, 98), axis=0)
        central = points[np.all((points >= lower) & (points <= upper), axis=1)]
        ax.scatter(central[:, 0], central[:, 1], central[:, 2], s=0.8,
                   c="#278c84", alpha=0.38, rasterized=True, label="ORB map points")
    if len(poses):
        ax.plot(poses[:, 0], poses[:, 1], poses[:, 2], color="#d3523d",
                linewidth=1.5, label="exported camera poses")
    ax.set(xlabel="X (SLAM units)", ylabel="Y (SLAM units)", zlabel="Z (SLAM units)")
    ax.set_title("Largest Atlas map and saved camera poses (central point preview)")
    ax.legend(loc="upper left")
    save_editable_figure(fig, output, dpi=170)
    plt.close(fig)


def plot_coverage(rows: list[dict[str, str]], output: Path) -> None:
    time = np.array([(int(row["utc_ns"]) - int(rows[0]["utc_ns"])) * 1e-9 for row in rows])
    saved = np.array([row["pose_status"] == "saved" for row in rows])
    fig, ax = plt.subplots(figsize=(10, 2.3), layout="constrained")
    ax.fill_between(time, 0, 1, where=saved, step="mid", color="#147d64")
    ax.set(xlim=(0, time[-1]), ylim=(0, 1), yticks=[], xlabel="Elapsed child camera time (s)",
           title="Pose saved in the exported largest Atlas map")
    ax.grid(axis="x", color="#dddddd")
    save_editable_figure(fig, output, dpi=170)
    plt.close(fig)


def video_overlay(frame: np.ndarray, index: int, rows: list[dict[str, str]],
                  path_xy: np.ndarray, bounds: tuple[float, float, float, float]) -> np.ndarray:
    image = cv2.resize(frame, (960, 540), interpolation=cv2.INTER_AREA)
    image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    shade = image.copy()
    cv2.rectangle(shade, (0, 0), (960, 65), (0, 0, 0), -1)
    cv2.rectangle(shade, (660, 330), (960, 540), (0, 0, 0), -1)
    image = cv2.addWeighted(shade, 0.65, image, 0.35, 0)
    elapsed = (int(rows[index]["utc_ns"]) - int(rows[0]["utc_ns"])) * 1e-9
    cv2.putText(image, f"Child camera | {elapsed:.1f} s | frame {index}", (18, 28),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
    saved = rows[index]["pose_status"] == "saved"
    cv2.putText(image, "Pose saved in exported map" if saved else "No pose in exported map",
                (18, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (115, 225, 170) if saved else (130, 180, 255), 2, cv2.LINE_AA)
    cv2.putText(image, "Camera path (SLAM units)", (676, 355), cv2.FONT_HERSHEY_SIMPLEX,
                0.46, (255, 255, 255), 1, cv2.LINE_AA)
    xmin, xmax, ymin, ymax = bounds
    scale_x = 260 / max(xmax - xmin, 1e-6)
    scale_y = 155 / max(ymax - ymin, 1e-6)
    valid_indices = np.flatnonzero(np.isfinite(path_xy[:index + 1]).all(axis=1))
    for indices in np.split(valid_indices, np.flatnonzero(np.diff(valid_indices) > 1) + 1):
        if len(indices) < 2:
            continue
        path = path_xy[indices]
        pixels = np.column_stack((675 + (path[:, 0] - xmin) * scale_x,
                                  520 - (path[:, 1] - ymin) * scale_y)).astype(np.int32)
        cv2.polylines(image, [pixels], False, (120, 225, 200), 1, cv2.LINE_AA)
    if saved:
        current = path_xy[index]
        pixel = (round(675 + (current[0] - xmin) * scale_x),
                 round(520 - (current[1] - ymin) * scale_y))
        cv2.circle(image, pixel, 4, (70, 100, 255), -1, cv2.LINE_AA)
    return image


def render_video(dataset: Path, rows: list[dict[str, str]], output: Path,
                 still: Path) -> dict[str, int | float]:
    best_start, best_end = longest_saved_span(rows)
    first, last = 0, len(rows)
    path_xy = np.array([[float(row["x_slam"]), float(row["y_slam"])]
                        if row["pose_status"] == "saved" else [math.nan, math.nan]
                        for row in rows])
    visible = path_xy[first:last][np.isfinite(path_xy[first:last]).all(axis=1)]
    xmin, ymin = np.min(visible, axis=0)
    xmax, ymax = np.max(visible, axis=0)
    margin = max(xmax - xmin, ymax - ymin, 1e-6) * 0.05
    bounds = (xmin - margin, xmax + margin, ymin - margin, ymax + margin)
    times = np.array([int(row["utc_ns"]) for row in rows], dtype=np.int64)
    frame_rate = 1e9 / float(np.median(np.diff(times)))
    step = 3
    writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*"mp4v"),
                             frame_rate / step, (960, 540))
    if not writer.isOpened():
        raise RuntimeError("OpenCV could not open MP4 writer")
    selected = list(range(first, last, step))
    still_target = (best_start + best_end) // 2
    still_index = min(selected, key=lambda index: abs(index - still_target))
    try:
        for index in selected:
            image_path = dataset / "mav0/cam0/data" / f"{rows[index]['utc_ns']}.png"
            frame = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
            if frame is None:
                raise FileNotFoundError(image_path)
            overlay = video_overlay(frame, index, rows, path_xy, bounds)
            writer.write(overlay)
            if index == still_index and not cv2.imwrite(str(still), overlay):
                raise RuntimeError(f"could not save {still}")
    finally:
        writer.release()
    return {"start_frame": first, "end_frame_exclusive": last,
            "start_utc_ns": int(rows[first]["utc_ns"]),
            "end_utc_ns": int(rows[last - 1]["utc_ns"]),
            "video_frames": len(selected), "video_fps": frame_rate / step}


def render(root: Path, experiment_id: str, run_name: str) -> Path:
    output = root / "results" / experiment_id / "vislam_depth"
    run = output / run_name
    dataset = root / "data/interim/orbslam3_full" / experiment_id
    with (run / "camera_trajectory_status.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    with (run / "MapPoints_manifest.csv").open(encoding="utf-8", newline="") as stream:
        maps = list(csv.DictReader(stream))
    if not maps or max(int(row["map_points"]) for row in maps) < 10:
        raise ValueError("ORB-SLAM3 did not export a usable point map")
    outputs = []
    for item in maps:
        source = run / item["ply_file"]
        vertices = read_ply_xyz(source)
        if len(vertices) != int(item["map_points"]):
            raise ValueError(f"PLY point count mismatch: {source}")
        destination = output / f"map_{item['map_id']}.ply"
        shutil.copyfile(source, destination)
        outputs.append(destination)
    largest = max(maps, key=lambda row: int(row["keyframes"]))
    points = read_ply_xyz(output / f"map_{largest['map_id']}.ply")
    poses = np.array([[float(row[key]) for key in ("x_slam", "y_slam", "z_slam")]
                      for row in rows if row["pose_status"] == "saved"])
    map_image = output / "map_3d.png"
    plot_map(points, poses, map_image)
    coverage_image = output / "pose_coverage.png"
    plot_coverage(rows, coverage_image)
    video = output / "vslam_run.mp4"
    still = output / "vslam_frame.png"
    video_info = render_video(dataset, rows, video, still)
    outputs.extend((map_image, coverage_image, video, still))
    run_manifest = json.loads((run / "run_manifest.json").read_text(encoding="utf-8"))
    coverage = json.loads((run / "coverage_summary.json").read_text(encoding="utf-8"))
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                            text=True, check=False)
    metadata = {
        "experiment_id": experiment_id,
        "run_name": run_name,
        "git_commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "run_manifest_sha256": sha256(run / "run_manifest.json"),
        "camera_calibration_sha256": run_manifest.get("camera_calibration_sha256"),
        "settings_sha256": run_manifest["settings_sha256"],
        "source_export_manifest_sha256": run_manifest["source_export_manifest_sha256"],
        "map_export_patch_sha256": sha256(root / "docker/orbslam3/export_map_points.patch"),
        "pose_coverage_fraction": coverage["coverage_fraction"],
        "map_counts": maps,
        "largest_map_id": largest["map_id"],
        "video": video_info,
        "artifacts_sha256": {path.name: sha256(path) for path in outputs},
    }
    (output / "artifact_manifest.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    report = [f"# 子機VSLAM可視化: {experiment_id}", "",
              f"ORB-SLAM3実行: `{run_name}`。観測入力は子機カメラのみ。",
              (f"全{coverage['input_frames']}画像のうち最大地図に姿勢が保存された画像は"
               f"{coverage['matched_frames']}枚（{coverage['coverage_fraction']:.1%}）。"),
              "地図が分かれた場合、各PLYの座標系を直接重ねない。単眼地図の尺度は任意のSLAM単位。",
              "", "## 動画・画像", "",
              "- [実行映像（姿勢保存状態の後処理表示）](vslam_run.mp4)",
              (f"  全{coverage['input_frames']}画像を3枚に1枚で再生"
               f"（{video_info['video_frames']}映像フレーム、約{video_info['video_fps']:.1f} fps）。"
               "保存姿勢がない区間も含む。"),
              "- [代表フレーム](vslam_frame.png)",
              "- [3Dマップ画像](map_3d.png)",
              "- [姿勢保存区間](pose_coverage.png)", "", "## 3Dマップ", ""]
    for item in maps:
        report.append(f"- [map {item['map_id']} PLY](map_{item['map_id']}.ply): "
                      f"{item['map_points']}点、{item['keyframes']}キーフレーム")
    report += ["", f"最大地図はmap {largest['map_id']}。3D画像にはこの地図を表示。",
               "", "## 根拠と制約", "",
               (f"- [実行manifest]({run_name}/run_manifest.json) / "
                f"[姿勢対応集計]({run_name}/coverage_summary.json) / "
                "[成果物SHA-256](artifact_manifest.json)"),
               "- 映像は保存済み子機画像に軌跡を重ねた後処理であり、ORB-SLAM3 GUIの録画ではない。",
               "- `not_saved`は最大地図に姿勢がないことを示し、画像別の追跡失敗を確定しない。",
               "- 3D点群はORB-SLAM3の終了時Atlas MapPoint。表面メッシュや測量精度はない。",
               "- 3D画像は各軸の2〜98パーセンタイル内の点を表示する。PLYには全点を保存。",
               "- [横断レポート](../../../docs/reports/child_vslam_calibrated_20260922.md)", ""]
    report_path = output / "report.md"
    report_path.write_text("\n".join(report), encoding="utf-8")
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_id")
    parser.add_argument("--run-name", default="calibrated_mono_map_full")
    args = parser.parse_args()
    print(render(Path(__file__).resolve().parents[1], args.experiment_id, args.run_name))


if __name__ == "__main__":
    main()
