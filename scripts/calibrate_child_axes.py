"""Fit registered child four-pose axes and export explicitly selected body attitudes."""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.child_axis_calibration import body_quaternions, fit_frd
from aquabeacon_analysis.experiments import load_experiment, repository_path
from aquabeacon_analysis.ngt_binary import load_reader, sha256
from aquabeacon_analysis.ngt_sync import angular_signal, correlation_offset, numeric_csv


def read_stream(reader, path, kind):
    with path.open("rb") as handle:
        header = reader.read_header(handle)
        rows = [row for record in reader.records(handle) if record["kind"] == kind
                for row in reader.rows(record)]
    return header, rows


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--firmware-root", type=Path, required=True)
    parser.add_argument("--target-experiment", action="append", default=[])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    exp = load_experiment(root, args.experiment)
    config_path = repository_path(root, exp.metadata["config_paths"][0])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    print(json.dumps({"pid": os.getpid(), "started_utc": datetime.now(UTC).isoformat(),
                      "owner": args.experiment, "purpose": "offline child body axis calibration"}),
          flush=True)
    reader = load_reader(args.firmware_root, "binary_log")
    paths = {k: repository_path(root, p) for k, p in config["sources"].items()}
    if any(not p.is_relative_to(exp.raw_root) for p in paths.values()):
        raise ValueError("Inputs outside registered raw root")
    head, events = read_stream(reader, paths["capture"], 13)
    expected = config["offline_marker_interpretation"]
    if (head["role"] != 2 or events[0]["event"] != expected["start_event"]
            or events[-1]["event"] != 4 or events[-1]["mask"] != 15
            or any(e["event"] == 5 for e in events)
            or any(e["boot"] != head["session"] for e in events)
            or any(b["marker_mono_us"] <= a["marker_mono_us"]
                   for a, b in pairwise(events))):
        raise ValueError("Invalid capture markers")
    for key in ("capture", "device", "imu_calibration", "mag_calibration", "log_file"):
        if len({e[key] for e in events}) != 1:
            raise ValueError(f"Mixed capture {key}")
    if paths["imu"].name != events[0]["log_file"]:
        raise ValueError("IMU filename does not match capture")
    ih, rows = read_stream(reader, paths["imu"], 1)
    hh, health = read_stream(reader, paths["health"], 3)
    if ih["session"] != head["session"] or hh["session"] != head["session"]:
        raise ValueError("Session mismatch")
    # Read all BNO packets with CRC verification; loss is reported, never filled.
    bh, _ = read_stream(reader, paths["bno_events"], 2)
    if bh["session"] != head["session"]:
        raise ValueError("BNO event session mismatch")
    poses, windows, world_gravity = [], [], []
    for phase in range(1, 5):
        starts = [e for e in events if e["event"] == 2 and e["phase"] == phase]
        ends = [e for e in events if e["event"] == 3 and e["phase"] == phase]
        if len(starts) != 1 or len(ends) != 1:
            raise ValueError("Expected one start/end per pose")
        begin, end = starts[0], ends[0]
        if end["marker_mono_us"] - begin["marker_mono_us"] < 5e6:
            raise ValueError("Short pose")
        full = [r for r in rows if begin["marker_mono_us"] <= r["acquired_us"]
                <= end["marker_mono_us"]]
        if len(full) < 20 or any(b["index"] != a["index"] + 1
                                 for a, b in pairwise(full)):
            raise ValueError("Pose IMU sequence loss")
        if max(b["acquired_us"] - a["acquired_us"]
               for a, b in pairwise(full)) > 25000:
            raise ValueError("Pose IMU timestamp gap")
        selected = [r for r in full if begin["marker_mono_us"] + 1e6 <= r["acquired_us"]
                    <= end["marker_mono_us"] - 250000]
        a = [[r[k] for k in ("ax", "ay", "az")] for r in selected if r["fresh_mask"] & 2]
        g = [[r[k] for k in ("gx", "gy", "gz")] for r in selected if r["fresh_mask"] & 4]
        t = [r["acquired_us"] / 1e6 for r in selected if r["fresh_mask"] & 2]
        poses.append((a, g, t))
        qs = np.array([[r[k] for k in ("qx", "qy", "qz", "qw")] for r in selected])
        accelerations = np.array([[r[k] for k in ("ax", "ay", "az")] for r in selected])
        gravity = Rotation.from_quat(qs).apply(accelerations).mean(0)
        if gravity[2] < 9 or np.linalg.norm(gravity[:2]) > .2:
            raise ValueError("BNO sensor-to-world quaternion convention check failed")
        world_gravity.append(gravity.tolist())
        windows.append({"phase": phase, "start_mono_s": begin["marker_mono_us"] / 1e6,
                        "end_mono_s": end["marker_mono_us"] / 1e6,
                        "marker_drop_increment": end["drops"] - begin["drops"],
                        "imu_index_gaps": 0, "total_imu_rows": len(full)})
    matrix, quality = fit_frd(poses)
    telemetry = exp.stage_path("interim") / "cameras/calibration_telemetry.csv"
    ct, cg = angular_signal(numeric_csv(telemetry, ["video_time_s", "qw", "qx", "qy", "qz"]))
    fresh = [r for r in rows if r["fresh_mask"] & 4]
    bt = np.array([r["acquired_us"] / 1e6 for r in fresh])
    bg = np.array([[r[k] for k in ("gx", "gy", "gz")] for r in fresh])
    camera_checks = []
    for window in config["camera_check_windows_s"]:
        coarse = correlation_offset(ct, np.linalg.norm(cg, axis=1), bt,
                                    np.linalg.norm(bg, axis=1), window,
                                    np.arange(250, 600, .1))
        offset = coarse["offset_s"]
        fine = correlation_offset(ct, np.linalg.norm(cg, axis=1), bt,
                                  np.linalg.norm(bg, axis=1), window,
                                  np.arange(offset - .15, offset + .151, .002))
        fine = {k: v for k, v in fine.items() if k not in ("offsets_s", "scores")}
        fine["competing_peak"] = coarse["competing_correlation_outside_1s"]
        camera_checks.append(fine)
    result = exp.stage_path("results") / "axis_calibration"
    solution_path = root / "calibration" / f"{args.experiment}_imu_frd.json"
    solution = {"schema_version": 1, "status": "offline_geometric_fit_with_logged_caveats",
                "experiment_id": args.experiment, "device": events[0]["device"],
                "sensor_frame": "BNO085 reported sensor axes", "body_frame": "FRD",
                "equation": "v_body = R_body_from_sensor @ v_sensor",
                "R_body_from_sensor": matrix.tolist(), "pose_quality": quality,
                "world_specific_force_per_pose_m_s2": world_gravity,
                "pose_windows": windows, "config": config,
                "camera_checks": camera_checks, "camera_transform_accepted": False,
                "camera_limit": "Late motion disagrees; no full-clip clock or extrinsic accepted.",
                "firmware_importable": False,
                "marker_limit": "Event7/mask15 interpreted offline as four labeled poses; not firmware profile validation.",
                "drop_review": "Marker increment retained. Full 100Hz pose stream has contiguous indices/times; no pose data removed due to drop counter. Other stream losses are not repaired.",
                "heading": "BNO game-world yaw remains arbitrary; no magnetic/geographic heading calibration.",
                "reuse": "Same device, sensor mounting and FRD definition only; camera/light swapped vs CAD per user.",
                "provenance": {k: {"path": p.relative_to(root).as_posix(), "sha256": sha256(p)}
                               for k, p in paths.items()},
                "config_sha256": sha256(config_path), "telemetry_sha256": sha256(telemetry),
                "script_sha256": sha256(Path(__file__)),
                "module_sha256": sha256(root / "src/aquabeacon_analysis/child_axis_calibration.py"),
                "reader_sha256": sha256(args.firmware_root / "scripts/binary_log.py"),
                "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                                                       text=True).strip(),
                "worktree_dirty": True}
    save(solution_path, solution)
    save(result / "solution.json", solution)
    save(result / "health_review.json", [r for r in health if 543e6 < r["mono_us"] < 559e6])
    for target_id in [args.experiment, *args.target_experiment]:
        target = load_experiment(root, target_id)
        if solution_path.relative_to(root).as_posix() not in target.metadata["calibration_paths"]:
            raise ValueError("Target registry must explicitly reference this calibration")
        source = target.raw_root / "child" / f"C_MAIN_IMU_100Hz_{target.metadata['c_main_suffix']}.bin"
        th, target_rows = read_stream(reader, source, 1)
        if th["role"] != 2:
            raise ValueError("Not a child IMU")
        q = np.array([[r[k] for k in ("qw", "qx", "qy", "qz")] for r in target_rows])
        qbody = body_quaternions(q, matrix)
        out = target.stage_path("results") / "child_body_attitude"
        out.mkdir(parents=True, exist_ok=True)
        with (out / "attitude.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["index", "acquired_us", "utc_us", "utc_source", "fresh_mask",
                             "body_qw", "body_qx", "body_qy", "body_qz", "quaternion_valid"])
            for row, qb in zip(target_rows, qbody, strict=True):
                writer.writerow([*[row[k] for k in ("index", "acquired_us", "utc_us",
                                                    "utc_source", "fresh_mask")],
                                 *qb, bool(np.isfinite(qb).all())])
        save(out / "manifest.json", {"experiment_id": target_id, "source": str(source),
             "source_sha256": sha256(source), "calibration": str(solution_path),
             "calibration_sha256": sha256(solution_path), "rows": len(target_rows),
             "git_commit": solution["git_commit"], "source_header": th,
             "frame": "body FRD to BNO game-world, scalar-first quaternion",
             "equation": "R_world_body = R_world_sensor @ R_body_sensor.T",
             "mounting_basis": "User confirms calibration and production physical arrangement; no independently measured mounting repeatability.",
             "camera_fusion": False, "absolute_heading": False,
             "freshness": "All original samples retained; use fresh_mask & 1 for fresh quaternion."})
        (out / "report.md").write_text(
            "# 子機の機体座標姿勢\n\n"
            f"{len(target_rows):,}行を `attitude.csv` に保存。機体FRDからBNOゲーム世界への"
            "四元数（w,x,y,z）。世界yawは任意で、北基準の方位ではない。\n\n"
            "原本の時刻・fresh_maskを保持し、補間や融合を行わず機体軸へ変換した。"
            "`fresh_mask & 1` で新規四元数を選択できる。未更新値も原本通り保持する。\n\n"
            "校正時と本番で同じ実機配置というユーザー確認を適用根拠とし、"
            "取り付け再現性の独立測定はない。カメラとライトはCADと逆の固定位置。"
            "カメラ座標・磁気方位・センサ中心の校正は含まない。\n\n"
            "入力・校正のSHA256と変換式は `manifest.json`、詳細は"
            "[軸校正レポート](../../20261029_child_axis_003/axis_calibration/report.md)。\n",
            encoding="utf-8")
        print(json.dumps({"target": target_id, "rows": len(target_rows)}), flush=True)
    print(json.dumps({"pose_residuals_deg": [q["direction_residual_deg"] for q in quality],
                      "camera_checks": camera_checks}), flush=True)


if __name__ == "__main__":
    main()
